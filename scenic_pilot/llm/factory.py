from __future__ import annotations

from scenic_pilot.config import Settings
from scenic_pilot.llm.contracts import (
    DisabledItineraryModelClient,
    ItineraryModelClient,
)
from scenic_pilot.llm.providers import OpenAICompatibleItineraryClient


def build_itinerary_model_client(settings: Settings) -> ItineraryModelClient:
    provider = settings.llm_provider.strip().lower()
    if provider in {"", "offline", "none", "disabled"}:
        return DisabledItineraryModelClient()
    if not settings.llm_model:
        return DisabledItineraryModelClient("model_not_configured")
    if provider == "deepseek":
        if not settings.deepseek_api_key:
            return DisabledItineraryModelClient("api_key_not_configured")
        return OpenAICompatibleItineraryClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.llm_base_url or "https://api.deepseek.com",
            model=settings.llm_model,
            provider_id="deepseek",
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return DisabledItineraryModelClient("api_key_not_configured")
        return OpenAICompatibleItineraryClient(
            api_key=settings.openai_api_key,
            base_url=settings.llm_base_url or "https://api.openai.com/v1",
            model=settings.llm_model,
            provider_id="openai",
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ValueError("SCENICPILOT_LLM_PROVIDER must be offline, deepseek, or openai")
