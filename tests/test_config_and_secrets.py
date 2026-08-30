import os

import pytest

from meetily_connector.config import ConfigError, load_config
from meetily_connector.secrets import SecretError, resolve_secret


def test_unknown_key_is_hard_error(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[watch]\nwebhok_url = 'x'\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(p)


def test_secret_in_config_refused(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[watch]\nsecret = 'hunter2'\n")
    with pytest.raises(ConfigError, match="do not belong in"):
        load_config(p)


def test_env_override(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("[watch]\npoll_interval_seconds = 60\n")
    monkeypatch.setenv("MEETILY_CONNECTOR_POLL_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("MEETILY_CONNECTOR_ALLOW_INSECURE_TRANSPORT", "true")
    cfg = load_config(p)
    assert cfg.poll_interval_seconds == 45
    assert cfg.allow_insecure_transport is True


def test_secret_from_env(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "topsecret")
    assert resolve_secret("env", env_var="MY_SECRET") == "topsecret"
    monkeypatch.delenv("MY_SECRET")
    with pytest.raises(SecretError):
        resolve_secret("env", env_var="MY_SECRET")


def test_secret_file_mode_enforced(tmp_path):
    f = tmp_path / "secret"
    f.write_text("topsecret\n")
    os.chmod(f, 0o644)
    with pytest.raises(SecretError, match="chmod 600"):
        resolve_secret("file", file_path=str(f))
    os.chmod(f, 0o600)
    assert resolve_secret("file", file_path=str(f)) == "topsecret"


def test_trigger_validation(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[watch]\ntrigger = 'transcript'\n")
    assert load_config(p).trigger == "transcript"
    p.write_text("[watch]\ntrigger = 'sumary'\n")
    with pytest.raises(ConfigError, match="trigger must be"):
        load_config(p)


def test_transcriber_validation(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[transcribe]\ntranscriber = "gemini"\ngemini_language_codes = ["ta-IN", "en-IN"]\n')
    cfg = load_config(p)
    assert cfg.transcriber == "gemini"
    assert cfg.gemini_language_codes == ["ta-IN", "en-IN"]
    p.write_text('[transcribe]\ntranscriber = "whisper"\n')
    with pytest.raises(ConfigError, match="transcriber must be"):
        load_config(p)
