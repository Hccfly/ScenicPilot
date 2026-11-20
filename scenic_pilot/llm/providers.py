from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx


class _OpenAICompatibleResponsesClient:
    """Shared Responses API transport for JSON-schema constrained calls."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        provider_id: str,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip() or not model.strip() or not base_url.strip():
            raise ValueError("model api_key, base_url and model are required")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider_id = provider_id
        self.timeout_seconds = timeout_seconds
        self.client = client or httpx.Client(
            headers={"User-Agent": "ScenicPilot/0.1 itinerary-model"}
        )

    def _request_structured(
        self,
        *,
        instructions: str,
        payload_input: dict[str, Any],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, object]:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(payload_input, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        # DeepSeek enables thinking by default and counts reasoning tokens against
        # max_output_tokens. These are bounded structured-output tasks, so disable
        # thinking to avoid truncating the JSON response before parsing.
        if self.provider_id == "deepseek":
            payload["reasoning"] = {"effort": "none"}
        try:
            response = self.client.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            return {"status": "unavailable", "degradation_reason": "timeout"}
        except httpx.RequestError:
            return {"status": "unavailable", "degradation_reason": "network_error"}
        if response.status_code >= 400:
            return {
                "status": "unavailable",
                "degradation_reason": f"model_http_{response.status_code}",
            }
        try:
            body = response.json()
            selection = json.loads(self._output_text(body))
        except (TypeError, ValueError, KeyError, IndexError):
            return {
                "status": "unavailable",
                "degradation_reason": "invalid_model_output",
            }
        if not isinstance(selection, dict):
            return {
                "status": "unavailable",
                "degradation_reason": "invalid_model_schema",
            }
        return {
            "status": "ok",
            "selection": selection,
            "usage": body.get("usage", {}),
        }

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        direct = body.get("output_text")
        if isinstance(direct, str):
            return direct
        for item in body.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    value = content.get("text")
                    if isinstance(value, str):
                        return value
        raise ValueError("response contains no output text")

    def _unavailable(self, reason: str) -> dict[str, object]:
        return {
            "status": "unavailable",
            "provider": self.provider_id,
            "model": self.model,
            "selected_source_ids": [],
            "degradation_reason": reason,
        }


class OpenAICompatibleItineraryClient(_OpenAICompatibleResponsesClient):
    """Small Responses API adapter shared by OpenAI-compatible providers."""

    _selection_schema = {
        "type": "object",
        "properties": {
            "selected_source_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "rationale": {"type": "string"},
        },
        "required": ["selected_source_ids", "rationale"],
        "additionalProperties": False,
    }

    def select_candidates(
        self,
        *,
        query: str,
        constraints: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> dict[str, object]:
        candidate_view = [
            {
                "source_id": str(item.get("source_id", "")),
                "title": str(item.get("title", "")),
                "category": str(item.get("category", "")),
                "score": float(item.get("score", 0.0)),
            }
            for item in candidates
        ]
        outcome = self._request_structured(
            instructions=(
                "你是景区行程候选排序器。只能从输入 candidates 中选择 source_id，"
                "不得创建景点、价格、开放时间或交通事实。根据用户约束返回最多 4 个候选，"
                "按推荐顺序排列。"
            ),
            payload_input={
                "query": query,
                "constraints": constraints,
                "candidates": candidate_view,
            },
            schema=self._selection_schema,
            schema_name="itinerary_candidate_selection",
            max_output_tokens=300,
        )
        if outcome["status"] != "ok":
            return self._unavailable(str(outcome.get("degradation_reason")))
        selection = outcome["selection"]
        source_ids = selection.get("selected_source_ids")
        rationale = selection.get("rationale")
        if not isinstance(source_ids, list) or not isinstance(rationale, str):
            return self._unavailable("invalid_model_schema")
        return {
            "status": "ok",
            "provider": self.provider_id,
            "model": self.model,
            "selected_source_ids": [str(item) for item in source_ids[:4]],
            "rationale": rationale[:500],
            "usage": outcome["usage"],
        }


class OpenAICompatibleQueryUnderstandingClient(_OpenAICompatibleResponsesClient):
    """Fills intent, constraints and live-data flags; never produces facts."""

    _understanding_schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["faq", "weather", "itinerary"],
            },
            "requires_live_data": {"type": "boolean"},
            "rewritten_query": {"type": "string"},
            "constraints": {
                "type": "object",
                "properties": {
                    "duration_minutes": {"type": ["integer", "null"]},
                    "party_size": {"type": ["integer", "null"]},
                    "budget": {"type": ["number", "null"]},
                    "visit_date": {"type": ["string", "null"]},
                    "origin": {"type": ["string", "null"]},
                    "weather_location": {"type": ["string", "null"]},
                },
                "required": [
                    "duration_minutes",
                    "party_size",
                    "budget",
                    "visit_date",
                    "origin",
                    "weather_location",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "intent",
            "requires_live_data",
            "rewritten_query",
            "constraints",
        ],
        "additionalProperties": False,
    }
    _constraint_keys = {
        "duration_minutes",
        "party_size",
        "budget",
        "visit_date",
        "origin",
        "weather_location",
    }

    def understand(self, *, query: str) -> dict[str, object]:
        outcome = self._request_structured(
            instructions=(
                "你是景区问句理解器。只抽取用户明确表达的信息，不得猜测或补全。"
                "intent 只能是 faq、weather、itinerary；requires_live_data 表示回答是否依赖"
                "实时房态、客流、排队、余票、票价、开放时间、演出场次等需要官方当日核验的信息；"
                "rewritten_query 用于检索召回，必须保留原句中的专名，不要加入新事实。"
                "用户没有提到的约束输出 null。"
            ),
            payload_input={"query": query},
            schema=self._understanding_schema,
            schema_name="query_understanding",
            max_output_tokens=400,
        )
        if outcome["status"] != "ok":
            return self._unavailable(str(outcome.get("degradation_reason")))
        selection = outcome["selection"]
        intent = selection.get("intent")
        live = selection.get("requires_live_data")
        rewritten = selection.get("rewritten_query")
        constraints = selection.get("constraints")
        if (
            intent not in {"faq", "weather", "itinerary"}
            or not isinstance(live, bool)
            or not isinstance(rewritten, str)
            or not isinstance(constraints, dict)
        ):
            return self._unavailable("invalid_model_schema")
        filled = {
            key: value
            for key, value in constraints.items()
            if key in self._constraint_keys and value is not None
        }
        return {
            "status": "ok",
            "provider": self.provider_id,
            "model": self.model,
            "intent": intent,
            "constraints": filled,
            "requires_live_data": live,
            "rewritten_query": rewritten[:200],
            "usage": outcome["usage"],
        }

