import unittest

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.config import Settings
from scenic_pilot.runtime import ScenicPilotRuntime


class GraphTests(unittest.TestCase):
    def runtime(self) -> ScenicPilotRuntime:
        return ScenicPilotRuntime(
            default_dependencies(Settings(tool_mode="offline", llm_provider="offline"))
        )

    def test_faq_has_citations_and_trace(self) -> None:
        result = self.runtime().invoke("珠海长隆有哪些园区主题？")

        self.assertEqual(result["intent"], "faq")
        self.assertIn(result["answer_status"], {"answered", "requires_live_check"})
        self.assertTrue(result["citations"])
        self.assertTrue(result["claims"])
        self.assertEqual(result["answer_trace"]["citation_marker_coverage"], 1.0)
        self.assertIn("[1]", result["answer"])
        self.assertEqual(result["trace"], ["classify", "retrieve", "grounded_answer"])

    def test_unknown_question_refuses_without_fabricated_citation(self) -> None:
        result = self.runtime().invoke("量子纠缠课程的教室编号是什么？")

        self.assertIn("资料不足", result["answer"])
        self.assertEqual(result["answer_status"], "refused")
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["claims"], [])

    def test_itinerary_runs_through_verifier(self) -> None:
        result = self.runtime().invoke("一家三口玩一天，预算 1000 元，怎么安排？")

        self.assertEqual(result["intent"], "itinerary")
        self.assertIsNotNone(result["plan"])
        self.assertTrue(result["plan"]["stops"])
        self.assertEqual(
            result["trace"],
            [
                "classify",
                "retrieve",
                "generate_candidates",
                "approval_gate",
                "enrich_tools",
                "validate",
                "finalize",
            ],
        )
        self.assertTrue(result["tool_results"])
        self.assertEqual(
            result["planning_trace"]["candidate_generation"]["selection_mode"],
            "rule_fallback",
        )
        self.assertTrue(any(item["code"] == "hours_unverified" for item in result["violations"]))

    def test_blank_message_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            self.runtime().invoke("  ")


if __name__ == "__main__":
    unittest.main()
