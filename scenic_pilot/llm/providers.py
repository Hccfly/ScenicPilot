from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx


class OpenAICompatibleItineraryClient:
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
        payload = {
            "model": self.model,
            "instructions": (
                "你是景区行程候选排序器。只能从输入 candidates 中选择 source_id，"
                "不得创建景点、价格、开放时间或交通事实。根据用户约束返回最多 4 个候选，"
                "按推荐顺序排列。"
            ),
            "input": json.dumps(
                {
                    "query": query,
                    "constraints": constraints,
                    "candidates": candidate_view,
                },
                ensure_ascii=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "itinerary_candidate_selection",
                    "strict": True,
                    "schema": self._selection_schema,
                }
            },
            "max_output_tokens": 300,
            "store": False,
        }
        # DeepSeek enables thinking by default and counts reasoning tokens against
        # max_output_tokens. Candidate ranking is a bounded structured-output task,
        # so disable thinking to avoid truncating the JSON response before parsing.
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
            return self._unavailable("timeout")
        except httpx.RequestError:
            return self._unavailable("network_error")
        if response.status_code >= 400:
            return self._unavailable(f"model_http_{response.status_code}")
        try:
            body = response.json()
            text = self._output_text(body)
            selection = json.loads(text)
        except (TypeError, ValueError, KeyError, IndexError):
            return self._unavailable("invalid_model_output")
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
