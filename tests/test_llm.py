from __future__ import annotations

import json

import httpx

from scenic_pilot.config import Settings
from scenic_pilot.llm import (
    DisabledItineraryModelClient,
    OpenAICompatibleItineraryClient,
    build_itinerary_model_client,
)


def test_responses_adapter_returns_structured_candidate_selection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.com/responses"
        payload = json.loads(request.content)
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["store"] is False
        assert payload["reasoning"] == {"effort": "none"}
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "selected_source_ids": ["spaceship"],
                                        "rationale": "更符合室内科普偏好",
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 50, "output_tokens": 12},
            },
        )

    client = OpenAICompatibleItineraryClient(
        api_key="secret-key",
        base_url="https://api.deepseek.com",
        model="configured-model",
        provider_id="deepseek",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.select_candidates(
        query="雨天半天怎么玩",
        constraints={"duration_minutes": 240},
        candidates=[
            {
                "source_id": "spaceship",
                "title": "飞船乐园",
                "category": "attraction",
                "score": 0.8,
            }
        ],
    )

    assert result["status"] == "ok"
    assert result["selected_source_ids"] == ["spaceship"]
    assert result["provider"] == "deepseek"
    assert "secret-key" not in str(result)


def test_model_http_failure_is_returned_as_degradation() -> None:
    client = OpenAICompatibleItineraryClient(
        api_key="secret-key",
        base_url="https://example.test/v1",
        model="configured-model",
        provider_id="openai",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "invalid"})
            )
        ),
    )

    result = client.select_candidates(query="test", constraints={}, candidates=[])

    assert result["status"] == "unavailable"
    assert result["degradation_reason"] == "model_http_401"


def test_model_factory_stays_disabled_without_provider_configuration() -> None:
    client = build_itinerary_model_client(Settings(llm_provider="offline"))

    assert isinstance(client, DisabledItineraryModelClient)
    result = client.select_candidates(query="test", constraints={}, candidates=[])
    assert result["degradation_reason"] == "provider_disabled"


def test_deepseek_factory_requires_key_and_model_without_crashing() -> None:
    no_model = build_itinerary_model_client(
        Settings(llm_provider="deepseek", deepseek_api_key="secret")
    )
    no_key = build_itinerary_model_client(
        Settings(llm_provider="deepseek", llm_model="configured-model")
    )

    assert no_model.select_candidates(
        query="test", constraints={}, candidates=[]
    )["degradation_reason"] == "model_not_configured"
    assert no_key.select_candidates(
        query="test", constraints={}, candidates=[]
    )["degradation_reason"] == "api_key_not_configured"
