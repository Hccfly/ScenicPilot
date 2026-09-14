from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Sequence

from scenic_pilot.answering import AnswerPolicy, GroundedAnswerer
from scenic_pilot.answering.evaluation import CITATION_MARKER, load_refusal_cases
from scenic_pilot.agent.dependencies import RuntimeDependencies
from scenic_pilot.agent.services import (
    IntentClassifier,
    ItineraryCandidateGenerator,
    ItineraryToolEnricher,
)
from scenic_pilot.agent.validation import ItineraryPlanReviser, ItineraryVerifier
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.knowledge.retrieval import (
    DeterministicHashingEmbedding,
    HybridRetriever,
    SentenceTransformerEmbedding,
)
from scenic_pilot.knowledge.retrieval.contracts import RetrievalResponse
from scenic_pilot.knowledge.retrieval.evaluation import (
    RetrievalCase,
    evaluate_retriever,
    load_retrieval_cases,
)
from scenic_pilot.knowledge.ingestion import build_index
from scenic_pilot.llm import build_itinerary_model_client
from scenic_pilot.persistence import IdempotencyConflict
from scenic_pilot.runtime import ScenicPilotRuntime
from scenic_pilot.tools.factory import build_tool_stack
from scenic_pilot.tools.managed import ToolSuite


EVALUATION_GROUP_COUNTS = {
    "retrieval": 30,
    "trusted_answer": 10,
    "planning": 5,
    "fault_injection": 7,
    "recovery_idempotency": 4,
    "call_budget": 2,
    "loop_limit": 2,
}


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    group: str
    description: str
    fixture: str | None = None
    expectation: str | None = None
    scenario: str | None = None
    query: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluationCase":
        return cls(
            case_id=str(value["case_id"]),
            group=str(value["group"]),
            description=str(value["description"]),
            fixture=str(value["fixture"]) if value.get("fixture") else None,
            expectation=(
                str(value["expectation"]) if value.get("expectation") else None
            ),
            scenario=str(value["scenario"]) if value.get("scenario") else None,
            query=str(value["query"]) if value.get("query") else None,
        )


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    passed: bool
    expected: Any
    actual: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    case_id: str
    group: str
    description: str
    assertions: tuple[AssertionResult, ...]
    observation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        failures = [item.name for item in self.assertions if not item.passed]
        return {
            "case_id": self.case_id,
            "group": self.group,
            "description": self.description,
            "passed": not failures,
            "failure_reasons": failures,
            "assertions": [item.to_dict() for item in self.assertions],
            "observation": self.observation,
        }


def _check(name: str, passed: bool, *, expected: Any, actual: Any) -> AssertionResult:
    return AssertionResult(name, bool(passed), expected, actual)


def load_case_catalog(path: Path) -> list[EvaluationCase]:
    cases = [
        EvaluationCase.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        duplicates = sorted(
            case_id for case_id, count in Counter(case_ids).items() if count > 1
        )
        raise ValueError(f"duplicate offline evaluation case IDs: {duplicates}")
    actual_counts = dict(Counter(case.group for case in cases))
    if actual_counts != EVALUATION_GROUP_COUNTS:
        raise ValueError(
            f"offline evaluation catalog group counts must be {EVALUATION_GROUP_COUNTS}, got {actual_counts}"
        )
    for case in cases:
        if case.group in {"retrieval", "trusted_answer"} and not case.fixture:
            raise ValueError(f"{case.case_id} requires fixture")
        if case.group not in {"retrieval", "trusted_answer"}:
            if not case.scenario or not case.query:
                raise ValueError(f"{case.case_id} requires scenario and query")
    return cases


def _load_retriever(index_path: Path) -> HybridRetriever:
    manifest = json.loads((index_path / "manifest.json").read_text(encoding="utf-8"))
    model_id = str(manifest["embedding_model"])
    embedding = (
        DeterministicHashingEmbedding(int(model_id.rsplit("-", 1)[-1]))
        if model_id.startswith("hashing-v1-")
        else SentenceTransformerEmbedding(
            model_id,
            revision=manifest.get("embedding_revision"),
            local_files_only=True,
        )
    )
    return HybridRetriever.load(index_path, embedding=embedding)


class RaisingWeather:
    def forecast(self, *, location: str, date: str | None = None) -> dict[str, object]:
        raise TimeoutError("injected weather timeout")


class RaisingBudget:
    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]:
        raise RuntimeError("injected budget exception")


class EmptyDistance:
    def estimate(self, *, origin: str, destination: str) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "eval-empty-distance",
            "origin": origin,
            "destination": destination,
            "distance_meters": None,
            "duration_minutes": None,
        }


class DirtyOpeningHours:
    def check(self, *, venue: str, date: str | None = None) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "eval-dirty-hours",
            "venue": venue,
            "date": date,
            "is_open": "yes",
            "opens_at": "09:00",
            "closes_at": "20:00",
        }


class NegativeBudget:
    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]:
        return {
            "status": "estimated",
            "provider": "eval-negative-budget",
            "estimated_cost": -100.0,
            "currency": "CNY",
        }


class UnknownSourceModel:
    def select_candidates(self, **_: Any) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "eval-fake-model",
            "model": "unknown-source-injector",
            "selected_source_ids": ["hallucinated-source"],
        }


class EmptyIndex:
    def search(self, query: str, **_: Any) -> list[Any]:
        return []

    def search_with_trace(self, query: str, **_: Any) -> RetrievalResponse:
        return RetrievalResponse(
            hits=(),
            trace={
                "schema_version": "scenicpilot-retrieval-v1",
                "query": query,
                "reason": "injected_empty_result",
            },
        )


class _CountingWeather:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0

    def forecast(self, **arguments: Any) -> dict[str, object]:
        self.calls += 1
        return self.delegate.forecast(**arguments)


class _CountingDistance:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0

    def estimate(self, **arguments: Any) -> dict[str, object]:
        self.calls += 1
        return self.delegate.estimate(**arguments)


class _CountingBudget:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0

    def estimate(self, **arguments: Any) -> dict[str, object]:
        self.calls += 1
        return self.delegate.estimate(**arguments)


class _CountingHours:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0

    def check(self, **arguments: Any) -> dict[str, object]:
        self.calls += 1
        return self.delegate.check(**arguments)


def _replace_dependencies(
    dependencies: RuntimeDependencies,
    *,
    weather: Any | None = None,
    distance: Any | None = None,
    budget: Any | None = None,
    opening_hours: Any | None = None,
    model: Any | None = None,
    index: Any | None = None,
) -> RuntimeDependencies:
    selected_weather = weather or dependencies.weather_tool
    selected_distance = distance or dependencies.distance_tool
    selected_budget = budget or dependencies.budget_tool
    selected_hours = opening_hours or dependencies.opening_hours_tool
    suite = ToolSuite(
        weather=selected_weather,
        distance=selected_distance,
        budget=selected_budget,
        opening_hours=selected_hours,
    )
    updates: dict[str, Any] = {
        "weather_tool": selected_weather,
        "distance_tool": selected_distance,
        "budget_tool": selected_budget,
        "opening_hours_tool": selected_hours,
        "tool_suite": suite,
        "tool_enricher": ItineraryToolEnricher(suite),
        "verifier": ItineraryVerifier(selected_hours),
    }
    if model is not None:
        updates["model_client"] = model
        updates["candidate_generator"] = ItineraryCandidateGenerator(model)
    if index is not None:
        updates["index"] = index
    return replace(dependencies, **updates)


def _counted_dependencies(
    base: RuntimeDependencies,
) -> tuple[RuntimeDependencies, dict[str, Any]]:
    tools: dict[str, Any] = {
        "weather": _CountingWeather(base.weather_tool),
        "distance": _CountingDistance(base.distance_tool),
        "budget": _CountingBudget(base.budget_tool),
        "opening_hours": _CountingHours(base.opening_hours_tool),
    }
    return (
        _replace_dependencies(
            base,
            weather=tools["weather"],
            distance=tools["distance"],
            budget=tools["budget"],
            opening_hours=tools["opening_hours"],
        ),
        tools,
    )


def _tool_call_count(tools: dict[str, Any]) -> int:
    return sum(int(tool.calls) for tool in tools.values())


def _violation_codes(result: dict[str, Any]) -> list[str]:
    return [str(item.get("code")) for item in result.get("violations", [])]


def _tool_outputs(result: dict[str, Any], tool_name: str) -> list[dict[str, Any]]:
    return [
        dict(item.get("output") or {})
        for item in result.get("tool_results", [])
        if item.get("tool") == tool_name
    ]


class OfflineEvaluator:
    def __init__(
        self,
        *,
        catalog_path: Path = PROJECT_ROOT / "data" / "eval" / "offline_cases.jsonl",
        retrieval_cases_path: Path = (
            PROJECT_ROOT / "data" / "eval" / "retrieval_cases.jsonl"
        ),
        refusal_cases_path: Path = (
            PROJECT_ROOT / "data" / "eval" / "answer_refusal_cases.jsonl"
        ),
        index_path: Path = (
            PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval"
        ),
    ) -> None:
        self.catalog_path = catalog_path
        self.index_path = index_path
        self.cases = load_case_catalog(catalog_path)
        self.retrieval_cases = {
            case.case_id: case for case in load_retrieval_cases(retrieval_cases_path)
        }
        self.refusal_cases = {
            case.case_id: case for case in load_refusal_cases(refusal_cases_path)
        }
        self.settings = Settings(
            tool_mode="offline",
            llm_provider="offline",
            retrieval_index_path=None,
            approval_mode="on_request",
        )
        self._retriever: HybridRetriever | None = None
        self._demo_index: Any | None = None
        self._runtime_root: Path | None = None

    @property
    def retriever(self) -> HybridRetriever:
        if self._retriever is None:
            self._retriever = _load_retriever(self.index_path)
        return self._retriever

    def _fresh_dependencies(self, settings: Settings) -> RuntimeDependencies:
        if self._demo_index is None:
            self._demo_index = build_index(settings.data_path)
        tools = build_tool_stack(settings)
        model_client = build_itinerary_model_client(settings)
        return RuntimeDependencies(
            settings=settings,
            index=self._demo_index,
            classifier=IntentClassifier(),
            answerer=GroundedAnswerer(
                AnswerPolicy(
                    min_keyword_score=settings.answer_min_keyword_score,
                    min_specific_term_matches=settings.answer_min_specific_term_matches,
                    min_specific_term_coverage=settings.answer_min_specific_term_coverage,
                    minimum_authority_level=settings.answer_min_authority_level,
                    max_claim_chars=settings.answer_max_claim_chars,
                )
            ),
            candidate_generator=ItineraryCandidateGenerator(model_client),
            tool_enricher=ItineraryToolEnricher(tools.suite),
            verifier=ItineraryVerifier(
                tools.opening_hours,
                default_start_time=settings.planning_default_start_time,
            ),
            reviser=ItineraryPlanReviser(),
            model_client=model_client,
            weather_tool=tools.weather,
            distance_tool=tools.distance,
            budget_tool=tools.budget,
            opening_hours_tool=tools.opening_hours,
            tool_suite=tools.suite,
            tool_runner=tools.runner,
        )

    def evaluate(
        self,
        *,
        case_ids: Sequence[str] = (),
        groups: Sequence[str] = (),
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        selected_ids = set(case_ids)
        selected_groups = set(groups)
        known_ids = {case.case_id for case in self.cases}
        missing_ids = sorted(selected_ids - known_ids)
        if missing_ids:
            raise ValueError(f"unknown offline evaluation case IDs: {missing_ids}")
        unknown_groups = sorted(selected_groups - set(EVALUATION_GROUP_COUNTS))
        if unknown_groups:
            raise ValueError(f"unknown offline evaluation groups: {unknown_groups}")
        selected = [
            case
            for case in self.cases
            if (not selected_ids or case.case_id in selected_ids)
            and (not selected_groups or case.group in selected_groups)
        ]
        results: list[dict[str, Any]] = []
        with TemporaryDirectory(prefix="scenicpilot-eval-") as temporary:
            self._runtime_root = Path(temporary)
            for case in selected:
                result = self._evaluate_safely(case).to_dict()
                results.append(result)
                if fail_fast and not result["passed"]:
                    break
            self._runtime_root = None

        group_summaries = {}
        for group in EVALUATION_GROUP_COUNTS:
            group_cases = [item for item in results if item["group"] == group]
            if not group_cases:
                continue
            passed_count = sum(bool(item["passed"]) for item in group_cases)
            group_summaries[group] = {
                "case_count": len(group_cases),
                "passed_count": passed_count,
                "failed_count": len(group_cases) - passed_count,
                "pass_rate": round(passed_count / len(group_cases), 6),
                "failed_case_ids": [
                    item["case_id"] for item in group_cases if not item["passed"]
                ],
            }
        passed_count = sum(bool(item["passed"]) for item in results)
        catalog_hash = hashlib.sha256(self.catalog_path.read_bytes()).hexdigest()
        return {
            "schema_version": "scenicpilot-offline-eval-v1",
            "execution": {
                "mode": "offline",
                "network_calls_allowed": False,
                "llm_provider": "offline",
                "tool_mode": "offline",
                "state_isolation": "one SQLite database and thread_id per agent case",
                "catalog_sha256": catalog_hash,
            },
            "summary": {
                "catalog_case_count": len(self.cases),
                "selected_case_count": len(selected),
                "executed_case_count": len(results),
                "passed_count": passed_count,
                "failed_count": len(results) - passed_count,
                "pass_rate": round(passed_count / len(results), 6) if results else 0.0,
                "all_passed": bool(results) and passed_count == len(results),
                "failed_case_ids": [
                    item["case_id"] for item in results if not item["passed"]
                ],
            },
            "groups": group_summaries,
            "failed_cases": [item for item in results if not item["passed"]],
            "cases": results,
        }

    def _evaluate_safely(self, case: EvaluationCase) -> EvaluationCaseResult:
        try:
            if case.group == "retrieval":
                return self._evaluate_retrieval(case)
            if case.group == "trusted_answer":
                return self._evaluate_answer(case)
            if case.group == "planning":
                return self._evaluate_planning(case)
            if case.group == "fault_injection":
                return self._evaluate_fault(case)
            if case.group == "recovery_idempotency":
                return self._evaluate_recovery(case)
            if case.group == "call_budget":
                return self._evaluate_call_budget(case)
            if case.group == "loop_limit":
                return self._evaluate_loop_limit(case)
            raise ValueError(f"unsupported group: {case.group}")
        except Exception as error:
            return EvaluationCaseResult(
                case.case_id,
                case.group,
                case.description,
                (
                    _check(
                        "evaluation_completed",
                        False,
                        expected="no exception",
                        actual=type(error).__name__,
                    ),
                ),
                {
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error)[:500],
                    }
                },
            )

    def _evaluate_retrieval(self, case: EvaluationCase) -> EvaluationCaseResult:
        fixture = self.retrieval_cases[case.fixture or ""]
        report = evaluate_retriever(
            self.retriever,
            [fixture],
            answer_context_k=self.settings.answer_context_limit,
            max_context_tokens=self.settings.answer_context_token_budget,
        )
        detail = report["cases"][0]
        parent_coverage = detail["parent_span_coverage"]
        assertions = (
            _check(
                "expected_source_recall_at_5",
                detail["recall@5"] == 1.0,
                expected=1.0,
                actual=detail["recall@5"],
            ),
            _check(
                "forbidden_sources_absent",
                not detail["forbidden_hits"],
                expected=[],
                actual=detail["forbidden_hits"],
            ),
            _check(
                "scenic_filter_isolated",
                detail["cross_scenic_leaks"] == 0,
                expected=0,
                actual=detail["cross_scenic_leaks"],
            ),
            _check(
                "demo_sources_absent",
                detail["demo_leaks"] == 0,
                expected=0,
                actual=detail["demo_leaks"],
            ),
            _check(
                "parent_evidence_complete",
                bool(parent_coverage) and all(parent_coverage),
                expected="all gold spans covered",
                actual=parent_coverage,
            ),
            _check(
                "context_token_budget_respected",
                detail["estimated_context_tokens"]
                <= self.settings.answer_context_token_budget,
                expected=f"<= {self.settings.answer_context_token_budget}",
                actual=detail["estimated_context_tokens"],
            ),
        )
        observation = {
            "fixture": fixture.case_id,
            "query": fixture.query,
            "expected_source_ids": list(fixture.expected_source_ids),
            "returned_source_ids": detail["returned_source_ids"],
            "relevant_ranks": detail["relevant_ranks"],
            "recall_at_5": detail["recall@5"],
            "reciprocal_rank_at_10": detail["reciprocal_rank@10"],
            "evidence_completeness": detail["evidence_completeness"],
            "estimated_context_tokens": detail["estimated_context_tokens"],
            "isolation_key": case.case_id,
        }
        return EvaluationCaseResult(
            case.case_id, case.group, case.description, assertions, observation
        )

    def _evaluate_answer(self, case: EvaluationCase) -> EvaluationCaseResult:
        answerer = GroundedAnswerer(
            AnswerPolicy(
                min_keyword_score=self.settings.answer_min_keyword_score,
                min_specific_term_matches=(
                    self.settings.answer_min_specific_term_matches
                ),
                min_specific_term_coverage=(
                    self.settings.answer_min_specific_term_coverage
                ),
                minimum_authority_level=self.settings.answer_min_authority_level,
                max_claim_chars=self.settings.answer_max_claim_chars,
            )
        )
        if case.expectation == "answerable":
            fixture = self.retrieval_cases[case.fixture or ""]
            query = fixture.query
            scenic_id = fixture.scenic_id
            expected_sources = set(fixture.expected_source_ids)
        else:
            fixture = self.refusal_cases[case.fixture or ""]
            query = fixture.query
            scenic_id = fixture.scenic_id
            expected_sources = set()
        hits = self.retriever.search(
            query,
            top_k=self.settings.top_k,
            filters={"scenic_id": scenic_id},
        )
        result = answerer.answer_with_trace(
            query,
            hits,
            min_score=self.settings.min_retrieval_score,
            context_limit=self.settings.answer_context_limit,
            max_context_tokens=self.settings.answer_context_token_budget,
        )
        citation_ids = {
            int(item.citation_id)
            for item in result.citations
            if item.citation_id is not None
        }
        cited_sources = {item.source_id for item in result.citations}
        marker_ids = {int(value) for value in CITATION_MARKER.findall(result.answer)}
        claims_bound = all(
            claim.citation_ids
            and set(claim.citation_ids) <= citation_ids
            for claim in result.claims
        )
        if case.expectation == "answerable":
            assertions = (
                _check(
                    "answer_status",
                    result.status in {"answered", "requires_live_check"},
                    expected=["answered", "requires_live_check"],
                    actual=result.status,
                ),
                _check(
                    "expected_source_cited",
                    bool(cited_sources & expected_sources),
                    expected=sorted(expected_sources),
                    actual=sorted(cited_sources),
                ),
                _check(
                    "citation_markers_bound",
                    bool(marker_ids) and marker_ids <= citation_ids,
                    expected="non-empty markers contained in citation IDs",
                    actual=sorted(marker_ids),
                ),
                _check(
                    "claims_structurally_grounded",
                    bool(result.claims) and claims_bound,
                    expected=True,
                    actual=claims_bound,
                ),
                _check(
                    "answer_trace_has_decision",
                    bool((result.trace or {}).get("decision_reasons")),
                    expected="non-empty decision_reasons",
                    actual=(result.trace or {}).get("decision_reasons", []),
                ),
            )
        else:
            assertions = (
                _check(
                    "answer_status",
                    result.status == "refused",
                    expected="refused",
                    actual=result.status,
                ),
                _check(
                    "refusal_has_no_citations",
                    not result.citations,
                    expected=0,
                    actual=len(result.citations),
                ),
                _check(
                    "refusal_has_no_claims",
                    not result.claims,
                    expected=0,
                    actual=len(result.claims),
                ),
                _check(
                    "refusal_trace_has_reason",
                    bool((result.trace or {}).get("decision_reasons")),
                    expected="non-empty decision_reasons",
                    actual=(result.trace or {}).get("decision_reasons", []),
                ),
            )
        observation = {
            "fixture": case.fixture,
            "query": query,
            "answer_status": result.status,
            "answer_preview": result.answer[:180],
            "citation_source_ids": sorted(cited_sources),
            "citation_ids": sorted(citation_ids),
            "marker_ids": sorted(marker_ids),
            "claim_count": len(result.claims),
            "decision_reasons": (result.trace or {}).get("decision_reasons", []),
            "isolation_key": case.case_id,
        }
        return EvaluationCaseResult(
            case.case_id, case.group, case.description, assertions, observation
        )

    def _runtime(
        self,
        case: EvaluationCase,
        dependencies: RuntimeDependencies,
    ) -> ScenicPilotRuntime:
        if self._runtime_root is None:
            raise RuntimeError("evaluation runtime root is not initialized")
        return ScenicPilotRuntime(
            dependencies,
            checkpoint_path=self._runtime_root / case.case_id / "runtime.sqlite3",
        )

    def _run_agent(
        self,
        case: EvaluationCase,
        dependencies: RuntimeDependencies,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime = self._runtime(case, dependencies)
        try:
            envelope = runtime.start(
                case.query or "",
                thread_id=case.case_id,
                idempotency_key=f"{case.case_id}-start",
            )
        finally:
            runtime.close()
        result = dict(envelope.get("result") or {})
        return envelope, result

    @staticmethod
    def _agent_observation(
        case: EvaluationCase,
        envelope: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        plan = result.get("plan") or {}
        return {
            "query": case.query,
            "envelope_status": envelope.get("status"),
            "thread_id": envelope.get("thread_id"),
            "answer_status": result.get("answer_status"),
            "intent": result.get("intent"),
            "constraint_status": result.get("constraint_status"),
            "revision_count": result.get("revision_count"),
            "plan_source_ids": [
                stop.get("source_id") for stop in plan.get("stops", [])
            ],
            "plan_duration_minutes": plan.get("duration_minutes"),
            "estimated_cost": plan.get("estimated_cost"),
            "violation_codes": _violation_codes(result),
            "tool_statuses": [
                {
                    "tool": item.get("tool"),
                    "status": (item.get("output") or {}).get("status"),
                    "degradation_reason": (
                        item.get("output") or {}
                    ).get("degradation_reason"),
                }
                for item in result.get("tool_results", [])
            ],
            "tool_call_count": result.get("tool_call_count"),
            "tool_call_limit": result.get("tool_call_limit"),
            "trace": result.get("trace", []),
            "governance_trace": result.get("governance_trace", {}),
            "isolation_key": case.case_id,
        }

    def _evaluate_planning(self, case: EvaluationCase) -> EvaluationCaseResult:
        dependencies = self._fresh_dependencies(self.settings)
        envelope, result = self._run_agent(case, dependencies)
        plan = result.get("plan") or {}
        stops = list(plan.get("stops") or [])
        retrieved = {
            item.get("chunk", {}).get("source_id") for item in result.get("hits", [])
        }
        assertions = [
            _check(
                "execution_completed",
                envelope.get("status") == "completed",
                expected="completed",
                actual=envelope.get("status"),
            ),
            _check(
                "itinerary_intent",
                result.get("intent") == "itinerary",
                expected="itinerary",
                actual=result.get("intent"),
            ),
            _check(
                "plan_is_structured",
                bool(stops)
                and all(
                    {
                        "name",
                        "category",
                        "duration_minutes",
                        "source_id",
                        "start_time",
                        "end_time",
                    }
                    <= set(stop)
                    for stop in stops
                ),
                expected=True,
                actual=bool(stops),
            ),
            _check(
                "plan_sources_are_retrieved",
                bool(stops)
                and all(stop.get("source_id") in retrieved for stop in stops),
                expected="all plan source IDs in retrieved hits",
                actual=[stop.get("source_id") for stop in stops],
            ),
            _check(
                "graph_trace_terminates",
                bool(result.get("trace"))
                and result["trace"][0] == "classify"
                and result["trace"][-1] == "finalize",
                expected="classify ... finalize",
                actual=result.get("trace", []),
            ),
            _check(
                "tool_budget_respected",
                int(result.get("tool_call_count", 0))
                <= int(result.get("tool_call_limit", 0)),
                expected=f"<= {result.get('tool_call_limit')}",
                actual=result.get("tool_call_count"),
            ),
        ]
        scenario = case.scenario
        if scenario == "family_day":
            assertions.append(
                _check(
                    "day_duration_respected",
                    int(plan.get("duration_minutes", 0)) <= 480,
                    expected="<= 480",
                    actual=plan.get("duration_minutes"),
                )
            )
        elif scenario == "indoor_half_day":
            source_ids = {stop.get("source_id") for stop in stops}
            assertions.extend(
                [
                    _check(
                        "half_day_duration_respected",
                        int(plan.get("duration_minutes", 0)) <= 240,
                        expected="<= 240",
                        actual=plan.get("duration_minutes"),
                    ),
                    _check(
                        "indoor_preference_selected",
                        "demo-spaceship" in source_ids,
                        expected="demo-spaceship",
                        actual=sorted(str(item) for item in source_ids),
                    ),
                ]
            )
        elif scenario == "marine_half_day":
            source_ids = {stop.get("source_id") for stop in stops}
            assertions.append(
                _check(
                    "marine_preference_selected",
                    "demo-ocean" in source_ids,
                    expected="demo-ocean",
                    actual=sorted(str(item) for item in source_ids),
                )
            )
        elif scenario == "senior_with_origin":
            assertions.extend(
                [
                    _check(
                        "route_tool_was_planned",
                        bool(_tool_outputs(result, "distance")),
                        expected=True,
                        actual=len(_tool_outputs(result, "distance")),
                    ),
                    _check(
                        "senior_stop_duration",
                        all(int(stop.get("duration_minutes", 0)) <= 90 for stop in stops),
                        expected="each stop <= 90 minutes",
                        actual=[stop.get("duration_minutes") for stop in stops],
                    ),
                ]
            )
        elif scenario == "insufficient_budget":
            assertions.extend(
                [
                    _check(
                        "unsafe_plan_refused",
                        result.get("answer_status") == "refused",
                        expected="refused",
                        actual=result.get("answer_status"),
                    ),
                    _check(
                        "constraint_rejected",
                        result.get("constraint_status") == "rejected",
                        expected="rejected",
                        actual=result.get("constraint_status"),
                    ),
                ]
            )
        return EvaluationCaseResult(
            case.case_id,
            case.group,
            case.description,
            tuple(assertions),
            self._agent_observation(case, envelope, result),
        )

    def _evaluate_fault(self, case: EvaluationCase) -> EvaluationCaseResult:
        dependencies = self._fresh_dependencies(self.settings)
        scenario = case.scenario
        if scenario == "weather_timeout":
            dependencies = _replace_dependencies(dependencies, weather=RaisingWeather())
        elif scenario == "budget_exception":
            dependencies = _replace_dependencies(dependencies, budget=RaisingBudget())
        elif scenario == "distance_empty":
            dependencies = _replace_dependencies(dependencies, distance=EmptyDistance())
        elif scenario == "hours_dirty":
            dependencies = _replace_dependencies(
                dependencies, opening_hours=DirtyOpeningHours()
            )
        elif scenario == "budget_negative":
            dependencies = _replace_dependencies(dependencies, budget=NegativeBudget())
        elif scenario == "model_unknown_source":
            dependencies = _replace_dependencies(
                dependencies, model=UnknownSourceModel()
            )
        elif scenario == "empty_retrieval":
            dependencies = _replace_dependencies(dependencies, index=EmptyIndex())
        else:
            raise ValueError(f"unsupported fault scenario: {scenario}")
        envelope, result = self._run_agent(case, dependencies)
        assertions = [
            _check(
                "failure_is_contained",
                envelope.get("status") == "completed",
                expected="completed",
                actual=envelope.get("status"),
            ),
            _check(
                "result_is_structured",
                bool(result) and isinstance(result.get("trace"), list),
                expected=True,
                actual=bool(result),
            ),
        ]
        if scenario == "weather_timeout":
            weather = _tool_outputs(result, "weather")
            budget = _tool_outputs(result, "budget")
            assertions.extend(
                [
                    _check(
                        "timeout_is_classified",
                        bool(weather)
                        and weather[0].get("degradation_reason")
                        == "uncaught_tool_exception"
                        and weather[0].get("error_type") == "TimeoutError",
                        expected="uncaught_tool_exception/TimeoutError",
                        actual=weather[0] if weather else None,
                    ),
                    _check(
                        "same_batch_budget_continues",
                        bool(budget)
                        and budget[0].get("status") in {"ok", "estimated"},
                        expected=["ok", "estimated"],
                        actual=budget[0].get("status") if budget else None,
                    ),
                ]
            )
        elif scenario == "budget_exception":
            budget = _tool_outputs(result, "budget")
            codes = _violation_codes(result)
            assertions.extend(
                [
                    _check(
                        "budget_exception_is_classified",
                        bool(budget)
                        and budget[0].get("degradation_reason")
                        == "uncaught_tool_exception",
                        expected="uncaught_tool_exception",
                        actual=budget[0] if budget else None,
                    ),
                    _check(
                        "budget_constraint_not_falsely_verified",
                        "budget_unverified" in codes,
                        expected="budget_unverified",
                        actual=codes,
                    ),
                ]
            )
        elif scenario == "distance_empty":
            assertions.append(
                _check(
                    "missing_duration_warns",
                    "route_unverified" in _violation_codes(result),
                    expected="route_unverified",
                    actual=_violation_codes(result),
                )
            )
        elif scenario == "hours_dirty":
            hours = _tool_outputs(result, "opening_hours")
            assertions.extend(
                [
                    _check(
                        "dirty_hours_rejected",
                        bool(hours)
                        and all(
                            item.get("degradation_reason") == "invalid_tool_result"
                            for item in hours
                        ),
                        expected="invalid_tool_result",
                        actual=hours,
                    ),
                    _check(
                        "dirty_hours_warns",
                        "hours_unverified" in _violation_codes(result),
                        expected="hours_unverified",
                        actual=_violation_codes(result),
                    ),
                ]
            )
        elif scenario == "budget_negative":
            budget = _tool_outputs(result, "budget")
            assertions.extend(
                [
                    _check(
                        "negative_cost_rejected",
                        bool(budget)
                        and budget[0].get("degradation_reason")
                        == "invalid_tool_result",
                        expected="invalid_tool_result",
                        actual=budget[0] if budget else None,
                    ),
                    _check(
                        "negative_cost_not_written_to_plan",
                        float((result.get("plan") or {}).get("estimated_cost", 0)) >= 0,
                        expected=">= 0",
                        actual=(result.get("plan") or {}).get("estimated_cost"),
                    ),
                ]
            )
        elif scenario == "model_unknown_source":
            generation = result.get("planning_trace", {}).get(
                "candidate_generation", {}
            )
            assertions.extend(
                [
                    _check(
                        "unknown_source_rejected",
                        "hallucinated-source"
                        in generation.get("rejected_model_source_ids", []),
                        expected="hallucinated-source",
                        actual=generation.get("rejected_model_source_ids", []),
                    ),
                    _check(
                        "candidate_generation_falls_back",
                        generation.get("selection_mode") == "rule_fallback",
                        expected="rule_fallback",
                        actual=generation.get("selection_mode"),
                    ),
                ]
            )
        elif scenario == "empty_retrieval":
            assertions.extend(
                [
                    _check(
                        "empty_retrieval_refuses",
                        result.get("answer_status") == "refused",
                        expected="refused",
                        actual=result.get("answer_status"),
                    ),
                    _check(
                        "empty_retrieval_has_no_citations",
                        result.get("citations") == [] and result.get("claims") == [],
                        expected={"citations": [], "claims": []},
                        actual={
                            "citations": result.get("citations"),
                            "claims": result.get("claims"),
                        },
                    ),
                ]
            )
        return EvaluationCaseResult(
            case.case_id,
            case.group,
            case.description,
            tuple(assertions),
            self._agent_observation(case, envelope, result),
        )

    def _evaluate_recovery(self, case: EvaluationCase) -> EvaluationCaseResult:
        dependencies, tools = _counted_dependencies(
            self._fresh_dependencies(self.settings)
        )
        runtime = self._runtime(case, dependencies)
        scenario = case.scenario
        assertions: list[AssertionResult] = []
        observation: dict[str, Any] = {
            "query": case.query,
            "isolation_key": case.case_id,
        }
        try:
            if scenario == "restart_then_approve":
                paused = runtime.start(
                    case.query or "",
                    thread_id=case.case_id,
                    idempotency_key="start-1",
                    require_approval=True,
                )
                calls_before = _tool_call_count(tools)
                runtime.close()
                runtime = self._runtime(case, dependencies)
                recovered = runtime.get_session(case.case_id)
                completed = runtime.resume(
                    thread_id=case.case_id,
                    approved=True,
                    idempotency_key="resume-1",
                )
                assertions.extend(
                    [
                        _check(
                            "paused_before_tools",
                            paused.get("status") == "awaiting_approval"
                            and calls_before == 0,
                            expected={"status": "awaiting_approval", "calls": 0},
                            actual={
                                "status": paused.get("status"),
                                "calls": calls_before,
                            },
                        ),
                        _check(
                            "checkpoint_recovers_after_restart",
                            recovered.get("status") == "awaiting_approval",
                            expected="awaiting_approval",
                            actual=recovered.get("status"),
                        ),
                        _check(
                            "approval_completes",
                            completed.get("status") == "completed"
                            and (completed.get("result") or {}).get(
                                "approval_status"
                            )
                            == "approved",
                            expected="completed/approved",
                            actual={
                                "status": completed.get("status"),
                                "approval_status": (
                                    completed.get("result") or {}
                                ).get("approval_status"),
                            },
                        ),
                        _check(
                            "tools_run_only_after_approval",
                            _tool_call_count(tools) > 0,
                            expected="> 0",
                            actual=_tool_call_count(tools),
                        ),
                    ]
                )
                observation.update(
                    {
                        "paused_status": paused.get("status"),
                        "recovered_status": recovered.get("status"),
                        "completed_status": completed.get("status"),
                        "tool_calls": _tool_call_count(tools),
                    }
                )
            elif scenario == "reject_without_tools":
                paused = runtime.start(
                    case.query or "",
                    thread_id=case.case_id,
                    idempotency_key="start-1",
                    require_approval=True,
                )
                rejected = runtime.resume(
                    thread_id=case.case_id,
                    approved=False,
                    reason="evaluation rejection",
                    idempotency_key="resume-1",
                )
                result = rejected.get("result") or {}
                assertions.extend(
                    [
                        _check(
                            "approval_was_requested",
                            paused.get("status") == "awaiting_approval",
                            expected="awaiting_approval",
                            actual=paused.get("status"),
                        ),
                        _check(
                            "rejection_terminates",
                            rejected.get("status") == "completed"
                            and result.get("approval_status") == "rejected",
                            expected="completed/rejected",
                            actual={
                                "status": rejected.get("status"),
                                "approval_status": result.get("approval_status"),
                            },
                        ),
                        _check(
                            "rejection_has_zero_side_effects",
                            _tool_call_count(tools) == 0
                            and result.get("tool_call_count") == 0,
                            expected=0,
                            actual={
                                "provider_calls": _tool_call_count(tools),
                                "state_calls": result.get("tool_call_count"),
                            },
                        ),
                    ]
                )
                observation.update(
                    {
                        "status": rejected.get("status"),
                        "approval_status": result.get("approval_status"),
                        "tool_calls": _tool_call_count(tools),
                        "trace": result.get("trace", []),
                    }
                )
            elif scenario == "duplicate_start":
                arguments = {
                    "thread_id": case.case_id,
                    "idempotency_key": "start-1",
                }
                first = runtime.start(case.query or "", **arguments)
                calls_after_first = _tool_call_count(tools)
                replay = runtime.start(case.query or "", **arguments)
                assertions.extend(
                    [
                        _check(
                            "duplicate_start_is_replayed",
                            replay.get("idempotency_replayed") is True,
                            expected=True,
                            actual=replay.get("idempotency_replayed"),
                        ),
                        _check(
                            "duplicate_start_preserves_result",
                            replay.get("result") == first.get("result"),
                            expected="same result",
                            actual=replay.get("result") == first.get("result"),
                        ),
                        _check(
                            "duplicate_start_does_not_repeat_tools",
                            _tool_call_count(tools) == calls_after_first,
                            expected=calls_after_first,
                            actual=_tool_call_count(tools),
                        ),
                    ]
                )
                observation.update(
                    {
                        "first_status": first.get("status"),
                        "replayed": replay.get("idempotency_replayed"),
                        "tool_calls": _tool_call_count(tools),
                    }
                )
            elif scenario == "duplicate_resume_and_conflict":
                runtime.start(
                    case.query or "",
                    thread_id=case.case_id,
                    idempotency_key="start-1",
                    require_approval=True,
                )
                first = runtime.resume(
                    thread_id=case.case_id,
                    approved=True,
                    reason="approved",
                    idempotency_key="resume-1",
                )
                calls_after_first = _tool_call_count(tools)
                replay = runtime.resume(
                    thread_id=case.case_id,
                    approved=True,
                    reason="approved",
                    idempotency_key="resume-1",
                )
                conflict = False
                try:
                    runtime.resume(
                        thread_id=case.case_id,
                        approved=False,
                        reason="changed",
                        idempotency_key="resume-1",
                    )
                except IdempotencyConflict:
                    conflict = True
                assertions.extend(
                    [
                        _check(
                            "duplicate_resume_is_replayed",
                            replay.get("idempotency_replayed") is True
                            and replay.get("result") == first.get("result"),
                            expected=True,
                            actual=replay.get("idempotency_replayed"),
                        ),
                        _check(
                            "duplicate_resume_does_not_repeat_tools",
                            _tool_call_count(tools) == calls_after_first,
                            expected=calls_after_first,
                            actual=_tool_call_count(tools),
                        ),
                        _check(
                            "same_key_different_input_conflicts",
                            conflict,
                            expected=True,
                            actual=conflict,
                        ),
                    ]
                )
                observation.update(
                    {
                        "first_status": first.get("status"),
                        "replayed": replay.get("idempotency_replayed"),
                        "conflict": conflict,
                        "tool_calls": _tool_call_count(tools),
                    }
                )
            else:
                raise ValueError(f"unsupported recovery scenario: {scenario}")
        finally:
            runtime.close()
        return EvaluationCaseResult(
            case.case_id,
            case.group,
            case.description,
            tuple(assertions),
            observation,
        )

    def _evaluate_call_budget(self, case: EvaluationCase) -> EvaluationCaseResult:
        limit = 0 if case.scenario == "zero_budget" else 1
        settings = replace(self.settings, execution_tool_call_limit=limit)
        dependencies, tools = _counted_dependencies(
            self._fresh_dependencies(settings)
        )
        envelope, result = self._run_agent(case, dependencies)
        actual_provider_calls = _tool_call_count(tools)
        skipped = [
            item
            for item in result.get("tool_results", [])
            if (item.get("output") or {}).get("degradation_reason")
            == "call_budget_exceeded"
        ]
        assertions = (
            _check(
                "execution_completed",
                envelope.get("status") == "completed",
                expected="completed",
                actual=envelope.get("status"),
            ),
            _check(
                "provider_call_limit_enforced",
                actual_provider_calls == limit,
                expected=limit,
                actual=actual_provider_calls,
            ),
            _check(
                "state_call_count_matches",
                result.get("tool_call_count") == limit,
                expected=limit,
                actual=result.get("tool_call_count"),
            ),
            _check(
                "excess_calls_are_structurally_skipped",
                bool(skipped)
                and all(
                    (item.get("output") or {}).get("status") == "unavailable"
                    for item in skipped
                ),
                expected="unavailable/call_budget_exceeded",
                actual=len(skipped),
            ),
            _check(
                "unverified_constraints_are_visible",
                "budget_unverified" in _violation_codes(result)
                and "hours_unverified" in _violation_codes(result),
                expected=["budget_unverified", "hours_unverified"],
                actual=_violation_codes(result),
            ),
            _check(
                "answer_requires_live_check",
                result.get("answer_status") == "requires_live_check",
                expected="requires_live_check",
                actual=result.get("answer_status"),
            ),
        )
        observation = self._agent_observation(case, envelope, result)
        observation.update(
            {
                "provider_call_count": actual_provider_calls,
                "skipped_call_count": len(skipped),
            }
        )
        return EvaluationCaseResult(
            case.case_id, case.group, case.description, assertions, observation
        )

    def _evaluate_loop_limit(self, case: EvaluationCase) -> EvaluationCaseResult:
        max_revisions = 0 if case.scenario == "zero_revisions" else 1
        settings = replace(self.settings, planning_max_revisions=max_revisions)
        envelope, result = self._run_agent(
            case, self._fresh_dependencies(settings)
        )
        trace = list(result.get("trace", []))
        revision_count = int(result.get("revision_count", -1))
        assertions = (
            _check(
                "execution_terminates",
                envelope.get("status") == "completed" and trace[-1:] == ["finalize"],
                expected="completed at finalize",
                actual={"status": envelope.get("status"), "last": trace[-1:]},
            ),
            _check(
                "revision_limit_enforced",
                revision_count == max_revisions
                and trace.count("revise_plan") == max_revisions,
                expected=max_revisions,
                actual={
                    "revision_count": revision_count,
                    "revision_nodes": trace.count("revise_plan"),
                },
            ),
            _check(
                "unsafe_result_is_refused",
                result.get("constraint_status") == "rejected"
                and result.get("answer_status") == "refused",
                expected="rejected/refused",
                actual={
                    "constraint_status": result.get("constraint_status"),
                    "answer_status": result.get("answer_status"),
                },
            ),
            _check(
                "recursion_limit_not_reached",
                len(trace) < settings.execution_recursion_limit,
                expected=f"< {settings.execution_recursion_limit}",
                actual=len(trace),
            ),
            _check(
                "revision_history_matches",
                len(result.get("revision_history", [])) == max_revisions,
                expected=max_revisions,
                actual=len(result.get("revision_history", [])),
            ),
        )
        return EvaluationCaseResult(
            case.case_id,
            case.group,
            case.description,
            assertions,
            self._agent_observation(case, envelope, result),
        )
