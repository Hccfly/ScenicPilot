from __future__ import annotations

from scenic_pilot.config import Settings


def test_settings_loads_project_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCENICPILOT_TOOL_MODE", raising=False)
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCENICPILOT_TOOL_MODE=live\nAMAP_API_KEY=file-key\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.tool_mode == "live"
    assert settings.amap_api_key == "file-key"
    assert "file-key" not in repr(settings)


def test_process_environment_overrides_env_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCENICPILOT_TOOL_MODE=offline\nAMAP_API_KEY=file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCENICPILOT_TOOL_MODE", "live")
    monkeypatch.setenv("AMAP_API_KEY", "process-key")

    settings = Settings.from_env(env_file)

    assert settings.tool_mode == "live"
    assert settings.amap_api_key == "process-key"


def test_missing_env_file_keeps_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCENICPILOT_TOOL_MODE", raising=False)
    monkeypatch.delenv("AMAP_API_KEY", raising=False)

    settings = Settings.from_env(tmp_path / "missing.env")

    assert settings.tool_mode == "offline"
    assert settings.amap_api_key is None


def test_llm_configuration_is_loaded_and_secrets_are_hidden(
    tmp_path, monkeypatch
) -> None:
    for key in (
        "SCENICPILOT_LLM_PROVIDER",
        "SCENICPILOT_LLM_BASE_URL",
        "SCENICPILOT_LLM_MODEL",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCENICPILOT_LLM_PROVIDER=deepseek\n"
        "SCENICPILOT_LLM_BASE_URL=https://api.deepseek.com\n"
        "SCENICPILOT_LLM_MODEL=configured-model\n"
        "DEEPSEEK_API_KEY=deepseek-secret\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.llm_provider == "deepseek"
    assert settings.llm_model == "configured-model"
    assert settings.deepseek_api_key == "deepseek-secret"
    assert "deepseek-secret" not in repr(settings)


def test_constraint_planning_limits_are_loaded(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCENICPILOT_PLANNING_MAX_REVISIONS", raising=False)
    monkeypatch.delenv("SCENICPILOT_PLANNING_DEFAULT_START_TIME", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCENICPILOT_PLANNING_MAX_REVISIONS=3\n"
        "SCENICPILOT_PLANNING_DEFAULT_START_TIME=10:00\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.planning_max_revisions == 3
    assert settings.planning_default_start_time == "10:00"


def test_governance_governance_settings_are_loaded(tmp_path, monkeypatch) -> None:
    keys = (
        "SCENICPILOT_CHECKPOINT_DB_PATH",
        "SCENICPILOT_APPROVAL_MODE",
        "SCENICPILOT_EXECUTION_TOOL_CALL_LIMIT",
        "SCENICPILOT_EXECUTION_RECURSION_LIMIT",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCENICPILOT_CHECKPOINT_DB_PATH=data/runtime/test.sqlite3\n"
        "SCENICPILOT_APPROVAL_MODE=always\n"
        "SCENICPILOT_EXECUTION_TOOL_CALL_LIMIT=9\n"
        "SCENICPILOT_EXECUTION_RECURSION_LIMIT=18\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)

    assert settings.checkpoint_db_path.name == "test.sqlite3"
    assert settings.checkpoint_db_path.is_absolute()
    assert settings.approval_mode == "always"
    assert settings.execution_tool_call_limit == 9
    assert settings.execution_recursion_limit == 18
