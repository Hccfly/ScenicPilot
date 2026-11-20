from __future__ import annotations

from dataclasses import dataclass

from scenic_pilot.config import Settings
from scenic_pilot.llm.contracts import (
    DisabledItineraryModelClient,
    DisabledQueryUnderstandingClient,
    ItineraryModelClient,
    QueryUnderstandingClient,
)
from scenic_pilot.llm.providers import (
    OpenAICompatibleItineraryClient,
    OpenAICompatibleQueryUnderstandingClient,
)


@dataclass(frozen=True, slots=True)
class _ProviderConfig:
    provider_id: str
    api_key: str
    base_url: str
    model: str


def _provider_config(settings: Settings) -> _ProviderConfig | str:
    """Return provider credentials, or the degradation reason as a string."""
    provider = settings.llm_provider.strip().lower()
    if provider in {"", "offline", "none", "disabled"}:
        return "provider_disabled"
    if not settings.llm_model:
        return "model_not_configured"
    if provider == "deepseek":
        if not settings.deepseek_api_key:
            return "api_key_not_configured"
        return _ProviderConfig(
            provider_id="deepseek",
            api_key=settings.deepseek_api_key,
            base_url=settings.llm_base_url or "https://api.deepseek.com",
            model=settings.llm_model,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return "api_key_not_configured"
        return _ProviderConfig(
            provider_id="openai",
            api_key=settings.openai_api_key,
            base_url=settings.llm_base_url or "https://api.openai.com/v1",
            model=settings.llm_model,
        )
    raise ValueError("SCENICPILOT_LLM_PROVIDER must be offline, deepseek, or openai")


def build_itinerary_model_client(settings: Settings) -> ItineraryModelClient:
    config = _provider_config(settings)
    if isinstance(config, str):
        return DisabledItineraryModelClient(config)
    return OpenAICompatibleItineraryClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        provider_id=config.provider_id,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def build_query_understanding_client(
    settings: Settings,
) -> QueryUnderstandingClient:
    if not settings.nlu_model_enabled:
        return DisabledQueryUnderstandingClient("nlu_disabled")
    config = _provider_config(settings)
    if isinstance(config, str):
        return DisabledQueryUnderstandingClient(config)
    return OpenAICompatibleQueryUnderstandingClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        provider_id=config.provider_id,
        timeout_seconds=settings.llm_timeout_seconds,
    )
