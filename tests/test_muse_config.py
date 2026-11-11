"""Muse AI backend configuration — .env loading and validation.

Muse is configured ONLY through backend environment variables.
These tests pin the defaults (verified against
https://dev.meta.ai/docs/overview), the optional-parameter parsing,
and the failure mode: a missing key yields a clear configuration
error, never a crash and never a silent substitute model.
"""

from muse_ai import load_env_file, load_muse_config


def test_defaults_when_only_key_is_set():
    config = load_muse_config({"MUSE_API_KEY": "key-abc"})
    assert config.configured is True
    assert config.base_url == "https://api.meta.ai/v1"
    assert config.model == "muse-spark-1.3"
    # Timeouts were removed: there is no request timeout.
    assert config.timeout_seconds is None
    assert config.max_output_tokens is None
    assert config.temperature is None
    assert config.vision_enabled is True


def test_missing_key_is_a_clear_error_not_a_crash():
    config = load_muse_config({})
    assert config.configured is False
    assert "MUSE_API_KEY" in (config.configuration_error or "")


def test_optional_parameters_parse_when_provided():
    config = load_muse_config({
        "MUSE_API_KEY": "k", "MUSE_BASE_URL": "https://example.test/v1/",
        "MUSE_MODEL": "muse-spark-1.2", "MUSE_TIMEOUT_SECONDS": "30.5",
        "MUSE_MAX_OUTPUT_TOKENS": "1234", "MUSE_TEMPERATURE": "0.3",
        "MUSE_VISION_ENABLED": "false"})
    assert config.configured is True
    assert config.base_url == "https://example.test/v1"
    assert config.model == "muse-spark-1.2"
    # MUSE_TIMEOUT_SECONDS is no longer read (timeouts removed):
    # even when present in the environment it is ignored.
    assert config.timeout_seconds is None
    assert config.max_output_tokens == 1234
    assert config.temperature == 0.3
    assert config.vision_enabled is False


def test_invalid_values_are_reported_and_block_configuration():
    config = load_muse_config({
        "MUSE_API_KEY": "k", "MUSE_TEMPERATURE": "9"})
    assert config.configured is False
    error = config.configuration_error or ""
    assert "MUSE_TEMPERATURE" in error
    # A stray MUSE_TIMEOUT_SECONDS value is ignored, never an
    # error: the setting no longer exists.
    config2 = load_muse_config({
        "MUSE_API_KEY": "k", "MUSE_TIMEOUT_SECONDS": "soon"})
    assert config2.configured is True
    assert config2.timeout_seconds is None


def test_public_status_never_contains_the_key():
    config = load_muse_config({"MUSE_API_KEY": "super-secret-value"})
    status = config.public_status()
    assert status["provider"] == "Muse AI"
    assert status["configured"] is True
    assert "super-secret-value" not in str(status)
    assert "api_key" not in status


def test_env_file_loading_fills_only_missing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nMUSE_API_KEY=from-file\nMUSE_MODEL=muse-spark-1.1\n",
        encoding="utf-8")
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    monkeypatch.setenv("MUSE_MODEL", "from-real-env")
    used = load_env_file(env_file)
    assert used == env_file
    import os
    try:
        assert os.environ["MUSE_API_KEY"] == "from-file"
        assert os.environ["MUSE_MODEL"] == "from-real-env"  # not overridden
    finally:
        # load_env_file writes into os.environ directly; do not leak
        # the key into other tests in this process.
        os.environ.pop("MUSE_API_KEY", None)
