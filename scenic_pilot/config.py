from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_environment(env_file: Path | None) -> dict[str, str]:
    """Read local defaults without overriding process-level configuration."""
    file_values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        file_values = {
            key: value
            for key, value in dotenv_values(env_file, encoding="utf-8").items()
            if value is not None
        }
    return {**file_values, **os.environ}


def _environment_flag(
    environment: dict[str, str], key: str, default: bool
) -> bool:
    raw = environment.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    scenic_id: str = "zhuhai-chimelong"
    data_path: Path = PROJECT_ROOT / "data" / "demo" / "zhuhai_chimelong.jsonl"
    min_retrieval_score: float = 0.12
    top_k: int = 8
    answer_context_limit: int = 3
    answer_context_token_budget: int = 2200
    answer_min_keyword_score: float = 0.08
    answer_min_specific_term_matches: int = 1
    answer_min_specific_term_coverage: float = 0.5
    answer_min_authority_level: int = 1
    answer_max_claim_chars: int = 360
    retrieval_index_path: Path | None = None
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_revision: str | None = "7999e1d3359715c523056ef9478215996d62a620"
    tool_mode: str = "offline"
    tool_timeout_seconds: float = 3.0
    tool_max_attempts: int = 2
    tool_retry_backoff_seconds: float = 0.2
    tool_cache_ttl_seconds: float = 600.0
    tool_stale_ttl_seconds: float = 3600.0
    tool_circuit_breaker_threshold: int = 3
    tool_circuit_reset_seconds: float = 30.0
    amap_api_key: str | None = field(default=None, repr=False)
    llm_provider: str = "offline"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 20.0
    nlu_model_enabled: bool = False
    openai_api_key: str | None = field(default=None, repr=False)
    deepseek_api_key: str | None = field(default=None, repr=False)
    planning_max_revisions: int = 2
    planning_default_start_time: str = "09:30"
    checkpoint_db_path: Path = (
        PROJECT_ROOT / "data" / "runtime" / "scenicpilot.sqlite3"
    )
    approval_mode: str = "on_request"
    execution_tool_call_limit: int = 24
    execution_recursion_limit: int = 40

    @classmethod
    def from_env(
        cls, env_file: Path | None = PROJECT_ROOT / ".env"
    ) -> "Settings":
        environment = _read_environment(env_file)
        defaults = cls()
        raw_path = environment.get("SCENICPILOT_DATA_PATH")
        data_path = Path(raw_path) if raw_path else defaults.data_path
        if not data_path.is_absolute():
            data_path = PROJECT_ROOT / data_path
        raw_index_path = environment.get("SCENICPILOT_RETRIEVAL_INDEX_PATH")
        retrieval_index_path = Path(raw_index_path) if raw_index_path else None
        if retrieval_index_path is not None and not retrieval_index_path.is_absolute():
            retrieval_index_path = PROJECT_ROOT / retrieval_index_path
        raw_checkpoint_path = environment.get("SCENICPILOT_CHECKPOINT_DB_PATH")
        checkpoint_db_path = (
            Path(raw_checkpoint_path)
            if raw_checkpoint_path
            else defaults.checkpoint_db_path
        )
        if not checkpoint_db_path.is_absolute():
            checkpoint_db_path = PROJECT_ROOT / checkpoint_db_path
        return cls(
            scenic_id=environment.get("SCENICPILOT_SCENIC_ID", defaults.scenic_id),
            data_path=data_path,
            min_retrieval_score=float(
                environment.get(
                    "SCENICPILOT_MIN_RETRIEVAL_SCORE",
                    defaults.min_retrieval_score,
                )
            ),
            top_k=int(environment.get("SCENICPILOT_TOP_K", defaults.top_k)),
            answer_context_limit=int(
                environment.get(
                    "SCENICPILOT_ANSWER_CONTEXT_LIMIT",
                    defaults.answer_context_limit,
                )
            ),
            answer_context_token_budget=int(
                environment.get(
                    "SCENICPILOT_ANSWER_CONTEXT_TOKEN_BUDGET",
                    defaults.answer_context_token_budget,
                )
            ),
            answer_min_keyword_score=float(
                environment.get(
                    "SCENICPILOT_ANSWER_MIN_KEYWORD_SCORE",
                    defaults.answer_min_keyword_score,
                )
            ),
            answer_min_specific_term_matches=int(
                environment.get(
                    "SCENICPILOT_ANSWER_MIN_SPECIFIC_TERM_MATCHES",
                    defaults.answer_min_specific_term_matches,
                )
            ),
            answer_min_specific_term_coverage=float(
                environment.get(
                    "SCENICPILOT_ANSWER_MIN_SPECIFIC_TERM_COVERAGE",
                    defaults.answer_min_specific_term_coverage,
                )
            ),
            answer_min_authority_level=int(
                environment.get(
                    "SCENICPILOT_ANSWER_MIN_AUTHORITY_LEVEL",
                    defaults.answer_min_authority_level,
                )
            ),
            answer_max_claim_chars=int(
                environment.get(
                    "SCENICPILOT_ANSWER_MAX_CLAIM_CHARS",
                    defaults.answer_max_claim_chars,
                )
            ),
            retrieval_index_path=retrieval_index_path,
            embedding_model=environment.get(
                "SCENICPILOT_EMBEDDING_MODEL", defaults.embedding_model
            ),
            embedding_revision=environment.get(
                "SCENICPILOT_EMBEDDING_REVISION", defaults.embedding_revision or ""
            )
            or None,
            tool_mode=environment.get("SCENICPILOT_TOOL_MODE", defaults.tool_mode),
            tool_timeout_seconds=float(
                environment.get(
                    "SCENICPILOT_TOOL_TIMEOUT_SECONDS",
                    defaults.tool_timeout_seconds,
                )
            ),
            tool_max_attempts=int(
                environment.get(
                    "SCENICPILOT_TOOL_MAX_ATTEMPTS",
                    defaults.tool_max_attempts,
                )
            ),
            tool_retry_backoff_seconds=float(
                environment.get(
                    "SCENICPILOT_TOOL_RETRY_BACKOFF_SECONDS",
                    defaults.tool_retry_backoff_seconds,
                )
            ),
            tool_cache_ttl_seconds=float(
                environment.get(
                    "SCENICPILOT_TOOL_CACHE_TTL_SECONDS",
                    defaults.tool_cache_ttl_seconds,
                )
            ),
            tool_stale_ttl_seconds=float(
                environment.get(
                    "SCENICPILOT_TOOL_STALE_TTL_SECONDS",
                    defaults.tool_stale_ttl_seconds,
                )
            ),
            tool_circuit_breaker_threshold=int(
                environment.get(
                    "SCENICPILOT_TOOL_CIRCUIT_BREAKER_THRESHOLD",
                    defaults.tool_circuit_breaker_threshold,
                )
            ),
            tool_circuit_reset_seconds=float(
                environment.get(
                    "SCENICPILOT_TOOL_CIRCUIT_RESET_SECONDS",
                    defaults.tool_circuit_reset_seconds,
                )
            ),
            amap_api_key=environment.get("AMAP_API_KEY") or None,
            llm_provider=environment.get(
                "SCENICPILOT_LLM_PROVIDER", defaults.llm_provider
            ),
            llm_base_url=environment.get("SCENICPILOT_LLM_BASE_URL") or None,
            llm_model=environment.get("SCENICPILOT_LLM_MODEL") or None,
            llm_timeout_seconds=float(
                environment.get(
                    "SCENICPILOT_LLM_TIMEOUT_SECONDS",
                    defaults.llm_timeout_seconds,
                )
            ),
            nlu_model_enabled=_environment_flag(
                environment,
                "SCENICPILOT_NLU_MODEL_ENABLED",
                defaults.nlu_model_enabled,
            ),
            openai_api_key=environment.get("OPENAI_API_KEY") or None,
            deepseek_api_key=environment.get("DEEPSEEK_API_KEY") or None,
            planning_max_revisions=max(
                0,
                int(
                    environment.get(
                        "SCENICPILOT_PLANNING_MAX_REVISIONS",
                        defaults.planning_max_revisions,
                    )
                ),
            ),
            planning_default_start_time=environment.get(
                "SCENICPILOT_PLANNING_DEFAULT_START_TIME",
                defaults.planning_default_start_time,
            ),
            checkpoint_db_path=checkpoint_db_path,
            approval_mode=environment.get(
                "SCENICPILOT_APPROVAL_MODE", defaults.approval_mode
            ),
            execution_tool_call_limit=max(
                0,
                int(
                    environment.get(
                        "SCENICPILOT_EXECUTION_TOOL_CALL_LIMIT",
                        defaults.execution_tool_call_limit,
                    )
                ),
            ),
            execution_recursion_limit=max(
                10,
                int(
                    environment.get(
                        "SCENICPILOT_EXECUTION_RECURSION_LIMIT",
                        defaults.execution_recursion_limit,
                    )
                ),
            ),
        )
