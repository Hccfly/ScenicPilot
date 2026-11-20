from __future__ import annotations

import json
from dataclasses import replace

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.agent.services import IntentClassifier
from scenic_pilot.answering import AnswerPolicy, GroundedAnswerer
from scenic_pilot.answering.evaluation import load_refusal_cases
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.runtime import ScenicPilotRuntime


class StubUnderstandingClient:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls = 0

    def understand(self, *, query: str) -> dict[str, object]:
        self.calls += 1
        return self.result


class ExplodingUnderstandingClient:
    def understand(self, *, query: str) -> dict[str, object]:
        raise RuntimeError("injected model failure")


def model_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "ok",
        "provider": "stub",
        "model": "stub-model",
        "intent": "faq",
        "constraints": {},
        "requires_live_data": False,
        "rewritten_query": "",
    }
    result.update(overrides)
    return result


def test_rule_intent_and_constraints_stay_authoritative() -> None:
    client = StubUnderstandingClient(
        model_result(intent="faq", constraints={"party_size": 9, "budget": 1})
    )
    classifier = IntentClassifier(understanding_client=client)

    intent, constraints, live, trace = classifier.understand(
        "一家三口玩一天，预算 1000 元，怎么安排？"
    )

    assert intent == "itinerary"
    assert constraints["duration_minutes"] == 480
    assert constraints["party_size"] == 3
    assert constraints["budget"] == 1000.0
    assert live is False
    assert trace["mode"] == "rule+model"
    assert trace["intent_source"] == "rule"
    assert trace["model_constraint_fields"] == []
    assert client.calls == 1


def test_model_rescues_paraphrased_itinerary() -> None:
    client = StubUnderstandingClient(
        model_result(
            intent="itinerary",
            rewritten_query="带父母轻松游园 行程",
        )
    )
    classifier = IntentClassifier(understanding_client=client)

    intent, _, live, trace = classifier.understand("带爸妈随便逛逛，别太累")

    assert intent == "itinerary"
    assert live is False
    assert trace["mode"] == "rule+model"
    assert trace["intent_source"] == "model"
    assert trace["rewritten_query"] == "带父母轻松游园 行程"
    assert client.calls == 1


def test_model_fills_missing_constraints_but_rule_values_win() -> None:
    client = StubUnderstandingClient(
        model_result(
            intent="itinerary",
            constraints={
                "duration_minutes": 600,
                "party_size": 3,
                "budget": 500,
            },
        )
    )
    classifier = IntentClassifier(understanding_client=client)

    _, constraints, _, trace = classifier.understand(
        "两大一小逛 6 个小时，预算 800 元"
    )

    assert constraints["duration_minutes"] == 360
    assert constraints["budget"] == 800.0
    assert constraints["party_size"] == 3
    assert trace["model_constraint_fields"] == ["party_size"]


def test_invalid_model_constraints_are_dropped() -> None:
    client = StubUnderstandingClient(
        model_result(
            constraints={
                "duration_minutes": "long",
                "party_size": 999,
                "budget": -5,
                "visit_date": "2026-13-40",
                "origin": "x",
                "unknown_field": 1,
            }
        )
    )
    classifier = IntentClassifier(understanding_client=client)

    _, constraints, _, trace = classifier.understand("帮我看看这个")

    assert constraints["duration_minutes"] == 240
    assert constraints["party_size"] == 1
    assert "budget" not in constraints
    assert trace["model_constraint_fields"] == []


def test_model_failure_falls_back_to_rules() -> None:
    classifier = IntentClassifier(
        understanding_client=ExplodingUnderstandingClient()
    )

    intent, constraints, live, trace = classifier.understand("鲸鲨馆现在要排多久？")

    assert intent == "faq"
    assert constraints["duration_minutes"] == 240
    assert live is False
    assert trace["mode"] == "rule"
    assert trace["model"]["degradation_reason"] == "client_exception"


def test_model_live_flag_short_circuits_keyword_list() -> None:
    client = StubUnderstandingClient(model_result(requires_live_data=True))
    classifier = IntentClassifier(understanding_client=client)
    answerer = GroundedAnswerer(AnswerPolicy())

    _, _, live, _ = classifier.understand("鲸鲨馆现在要排多久？")
    result = answerer.answer_with_trace(
        "鲸鲨馆现在要排多久？",
        [],
        min_score=0.12,
        requires_live_data=live,
    )

    assert live is True
    assert result.status == "refused"
    assert result.trace["decision_reasons"] == ["live_data_required"]


def test_graph_uses_model_live_flag_and_intent() -> None:
    base = default_dependencies(Settings(tool_mode="offline", llm_provider="offline"))
    live_client = StubUnderstandingClient(model_result(requires_live_data=True))
    live_runtime = ScenicPilotRuntime(
        replace(
            base,
            classifier=IntentClassifier(understanding_client=live_client),
        )
    )

    live_result = live_runtime.invoke("鲸鲨馆现在要排多久？")

    assert live_result["requires_live_data"] is True
    assert live_result["answer_status"] == "refused"
    assert live_result["answer_trace"]["decision_reasons"] == ["live_data_required"]

    itinerary_client = StubUnderstandingClient(model_result(intent="itinerary"))
    itinerary_runtime = ScenicPilotRuntime(
        replace(
            base,
            classifier=IntentClassifier(understanding_client=itinerary_client),
        )
    )

    itinerary_result = itinerary_runtime.invoke("带爸妈随便逛逛，别太累")

    assert itinerary_result["intent"] == "itinerary"
    assert "generate_candidates" in itinerary_result["trace"]


def test_nlu_catalog_is_well_formed() -> None:
    path = PROJECT_ROOT / "data" / "eval" / "nlu_cases.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(rows) >= 20
    assert len({row["case_id"] for row in rows}) == len(rows)
    for row in rows:
        assert row["expected_intent"] in {"faq", "weather", "itinerary"}
        assert isinstance(row["expected_live_data"], bool)
        assert set(row.get("expected_constraints", {})) <= {
            "duration_minutes",
            "party_size",
            "budget",
            "visit_date",
            "origin",
            "weather_location",
        }


def test_rule_classifier_returns_valid_slots_for_every_catalog_case() -> None:
    path = PROJECT_ROOT / "data" / "eval" / "nlu_cases.jsonl"
    classifier = IntentClassifier()
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    for row in rows:
        intent, constraints, live, trace = classifier.understand(row["query"])
        assert intent in {"faq", "weather", "itinerary"}
        assert isinstance(constraints["duration_minutes"], int)
        assert isinstance(constraints["party_size"], int)
        assert isinstance(live, bool)
        assert trace["mode"] == "rule"


def test_colloquial_live_cases_stay_outside_the_keyword_list() -> None:
    path = PROJECT_ROOT / "data" / "eval" / "answer_refusal_cases.jsonl"
    cases = [case for case in load_refusal_cases(path) if case.requires_live_data]

    assert len(cases) == 10
    classifier = IntentClassifier()
    answerer = GroundedAnswerer(AnswerPolicy())
    for case in cases:
        assert not any(
            term in case.query for term in GroundedAnswerer._live_only_terms
        ), case.query
        _, _, live, _ = classifier.understand(case.query)
        assert live is False, case.query
        result = answerer.answer_with_trace(
            case.query,
            [],
            min_score=0.12,
            requires_live_data=case.requires_live_data,
        )
        assert result.status == "refused"
        assert result.trace["decision_reasons"] == ["live_data_required"]
