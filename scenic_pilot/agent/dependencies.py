from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from scenic_pilot.answering import AnswerPolicy, GroundedAnswerer
from scenic_pilot.agent.services import (
    IntentClassifier,
    ItineraryCandidateGenerator,
    ItineraryToolEnricher,
)
from scenic_pilot.agent.validation import ItineraryPlanReviser, ItineraryVerifier
from scenic_pilot.config import Settings
from scenic_pilot.knowledge.ingestion import build_index
from scenic_pilot.knowledge.retrieval import HybridRetriever, SentenceTransformerEmbedding
from scenic_pilot.llm import ItineraryModelClient, build_itinerary_model_client
from scenic_pilot.tools.contracts import BudgetTool, DistanceTool, OpeningHoursTool, WeatherTool
from scenic_pilot.tools.factory import build_tool_stack
from scenic_pilot.tools.managed import ToolSuite
from scenic_pilot.tools.resilience import ResilientToolRunner


class RetrievalIndex(Protocol):
    def search(self, query: str, **kwargs: Any): ...

    def search_with_trace(self, query: str, **kwargs: Any): ...


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    settings: Settings
    index: RetrievalIndex
    classifier: IntentClassifier
    answerer: GroundedAnswerer
    candidate_generator: ItineraryCandidateGenerator
    tool_enricher: ItineraryToolEnricher
    verifier: ItineraryVerifier
    reviser: ItineraryPlanReviser
    model_client: ItineraryModelClient
    weather_tool: WeatherTool
    distance_tool: DistanceTool
    budget_tool: BudgetTool
    opening_hours_tool: OpeningHoursTool
    tool_suite: ToolSuite
    tool_runner: ResilientToolRunner


def default_dependencies(settings: Settings | None = None) -> RuntimeDependencies:
    selected = settings or Settings.from_env()
    tools = build_tool_stack(selected)
    model_client = build_itinerary_model_client(selected)
    if selected.retrieval_index_path is not None:
        embedding = SentenceTransformerEmbedding(
            selected.embedding_model,
            revision=selected.embedding_revision,
            local_files_only=True,
        )
        index: RetrievalIndex = HybridRetriever.load(
            selected.retrieval_index_path, embedding=embedding
        )
    else:
        index = build_index(selected.data_path)
    return RuntimeDependencies(
        settings=selected,
        index=index,
        classifier=IntentClassifier(),
        answerer=GroundedAnswerer(
            AnswerPolicy(
                min_keyword_score=selected.answer_min_keyword_score,
                min_specific_term_matches=selected.answer_min_specific_term_matches,
                min_specific_term_coverage=selected.answer_min_specific_term_coverage,
                minimum_authority_level=selected.answer_min_authority_level,
                max_claim_chars=selected.answer_max_claim_chars,
            )
        ),
        candidate_generator=ItineraryCandidateGenerator(model_client),
        tool_enricher=ItineraryToolEnricher(tools.suite),
        verifier=ItineraryVerifier(
            tools.opening_hours,
            default_start_time=selected.planning_default_start_time,
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
