import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.api import app
from scenic_pilot.config import Settings
from scenic_pilot.persistence import IdempotencyConflict
from scenic_pilot.runtime import ScenicPilotRuntime


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scenic_id"], "zhuhai-chimelong")

        live = self.client.get("/health/live")
        ready = self.client.get("/health/ready")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {"status": "ok", "version": "1.0.0"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertTrue(all(ready.json()["checks"].values()))

    def test_product_page_and_assets(self) -> None:
        page = self.client.get("/")
        stylesheet = self.client.get("/assets/app.css")
        script = self.client.get("/assets/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("ScenicPilot", page.text)
        self.assertIn("/assets/app.js", page.text)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("--green", stylesheet.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn("/v1/sessions/stream", script.text)
        self.assertIn("/v1/sessions/resume", script.text)

    def test_chat_contract(self) -> None:
        runtime = ScenicPilotRuntime(
            default_dependencies(Settings(tool_mode="offline", llm_provider="offline"))
        )
        try:
            with patch("scenic_pilot.api.get_runtime", return_value=runtime):
                response = self.client.post(
                    "/v1/chat", json={"message": "交通信息在哪里核验？"}
                )
        finally:
            runtime.close()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["answer"])
        self.assertIn(body["answer_status"], {"answered", "requires_live_check"})
        self.assertIn("citation_marker_coverage", body["answer_trace"])
        self.assertEqual(body["trace"][-1], "grounded_answer")

    def test_sse_contract(self) -> None:
        runtime = ScenicPilotRuntime(
            default_dependencies(Settings(tool_mode="offline", llm_provider="offline"))
        )
        try:
            with patch("scenic_pilot.api.get_runtime", return_value=runtime):
                response = self.client.post(
                    "/v1/chat/stream", json={"message": "交通怎么查？"}
                )
        finally:
            runtime.close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: started", response.text)
        self.assertIn("event: completed", response.text)

    def test_weather_contract_completes_without_itinerary_approval(self) -> None:
        runtime = ScenicPilotRuntime(
            default_dependencies(Settings(tool_mode="offline", llm_provider="offline"))
        )
        try:
            with patch("scenic_pilot.api.get_runtime", return_value=runtime):
                response = self.client.post(
                    "/v1/sessions/stream",
                    json={
                        "message": "明天珠海天气怎么样？",
                        "require_approval": True,
                    },
                )
        finally:
            runtime.close()

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: completed", response.text)
        self.assertNotIn("event: approval_required", response.text)
        self.assertIn('"intent": "weather"', response.text)
        self.assertIn('"tool": "weather"', response.text)

    def test_session_start_resume_and_status_contracts(self) -> None:
        runtime = Mock()
        runtime.start.return_value = {
            "status": "awaiting_approval",
            "thread_id": "api-thread",
            "idempotency_key": "start-1",
            "idempotency_replayed": False,
            "interrupts": [{"action": "execute_itinerary_tools"}],
            "result": {"approval_status": "pending"},
        }
        runtime.resume.return_value = {
            "status": "completed",
            "thread_id": "api-thread",
            "idempotency_key": "resume-1",
            "idempotency_replayed": False,
            "interrupts": [],
            "result": {"approval_status": "approved"},
        }
        runtime.get_session.return_value = {
            "status": "completed",
            "thread_id": "api-thread",
            "next_nodes": [],
            "interrupts": [],
            "result": {"approval_status": "approved"},
            "checkpoint": {"checkpoint_id": "checkpoint-1"},
        }
        with patch("scenic_pilot.api.get_runtime", return_value=runtime):
            started = self.client.post(
                "/v1/sessions",
                json={
                    "message": "安排一天行程",
                    "thread_id": "api-thread",
                    "idempotency_key": "start-1",
                    "require_approval": True,
                },
            )
            resumed = self.client.post(
                "/v1/sessions/resume",
                json={
                    "thread_id": "api-thread",
                    "idempotency_key": "resume-1",
                    "approved": True,
                },
            )
            status = self.client.get("/v1/sessions/api-thread")

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["status"], "awaiting_approval")
        self.assertEqual(resumed.json()["status"], "completed")
        self.assertEqual(status.json()["checkpoint"]["checkpoint_id"], "checkpoint-1")

    def test_session_sse_exposes_approval_boundary(self) -> None:
        runtime = Mock()
        runtime.start.return_value = {
            "status": "awaiting_approval",
            "thread_id": "stream-thread",
            "idempotency_key": "stream-start-1",
            "idempotency_replayed": False,
            "interrupts": [{"action": "execute_itinerary_tools"}],
            "result": {"approval_status": "pending"},
        }
        with patch("scenic_pilot.api.get_runtime", return_value=runtime):
            response = self.client.post(
                "/v1/sessions/stream",
                json={
                    "message": "安排一天行程",
                    "thread_id": "stream-thread",
                    "idempotency_key": "stream-start-1",
                    "require_approval": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("event: started", response.text)
        self.assertIn("event: approval_required", response.text)
        self.assertIn('"action": "execute_itinerary_tools"', response.text)

    def test_session_sse_serializes_failure(self) -> None:
        runtime = Mock()
        runtime.start.side_effect = RuntimeError("offline tool failure")
        with patch("scenic_pilot.api.get_runtime", return_value=runtime):
            response = self.client.post(
                "/v1/sessions/stream",
                json={"message": "安排一天行程"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: failed", response.text)
        self.assertIn("session_start_failed", response.text)
        self.assertIn("offline tool failure", response.text)

    def test_session_idempotency_conflict_returns_409(self) -> None:
        runtime = Mock()
        runtime.start.side_effect = IdempotencyConflict("key conflict")
        with patch("scenic_pilot.api.get_runtime", return_value=runtime):
            response = self.client.post(
                "/v1/sessions",
                json={
                    "message": "安排一天行程",
                    "thread_id": "api-thread",
                    "idempotency_key": "duplicate",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "key conflict")


if __name__ == "__main__":
    unittest.main()
