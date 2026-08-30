"""Configuration: TOML file + MEETILY_CONNECTOR_* env overrides.

Secrets are never stored here — config names only where to find them
(see secrets.py). Unknown keys are a hard error to catch typos.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG_DIR = Path("~/.meetily-connector").expanduser()
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"

# Known locations of Meetily's DB; auto-discovery probes these in order.
DB_CANDIDATES = [
    "~/Library/Application Support/com.meetily.ai/meeting_minutes.db",
    "~/Library/Application Support/Meetily/meeting_minutes.db",
    "~/.config/meetily/meeting_minutes.db",
]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    # [meetily]
    db_path: str = ""  # empty = auto-discover from DB_CANDIDATES

    # [watch]
    poll_interval_seconds: int = 30
    min_duration_seconds: int = 60
    settle_seconds: int = 30  # summary must be this old before firing
    webhook_url: str = ""
    allow_insecure_transport: bool = False
    max_payload_bytes: int = 1_000_000
    language_hint: str = ""
    secret_source: str = "env"  # env | keychain | file
    secret_env: str = "MEETILY_WEBHOOK_SECRET"
    secret_keychain_service: str = "meetily-webhook"
    secret_keychain_account: str = ""
    secret_file: str = ""

    # [retry]
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    retry_max_attempts: int = 0  # 0 = retry forever (transient errors only)

    state_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)

    def resolve_db_path(self) -> Path:
        if self.db_path:
            path = Path(self.db_path).expanduser()
            if not path.exists():
                raise ConfigError(f"configured db_path does not exist: {path}")
            return path
        for candidate in DB_CANDIDATES:
            path = Path(candidate).expanduser()
            if path.exists():
                return path
        raise ConfigError(
            "Meetily database not found. Probed:\n  "
            + "\n  ".join(str(Path(c).expanduser()) for c in DB_CANDIDATES)
            + "\nSet [meetily] db_path in config.toml, or run Meetily once first."
        )

    def validate_webhook_url(self) -> None:
        if not self.webhook_url:
            raise ConfigError("[watch] webhook_url is required for watch mode")
        check_transport(self.webhook_url, self.allow_insecure_transport)


# Flat TOML section -> dataclass field mapping.
_SECTION_KEYS = {
    "meetily": {"db_path": "db_path"},
    "watch": {
        "poll_interval_seconds": "poll_interval_seconds",
        "min_duration_seconds": "min_duration_seconds",
        "settle_seconds": "settle_seconds",
        "webhook_url": "webhook_url",
        "allow_insecure_transport": "allow_insecure_transport",
        "max_payload_bytes": "max_payload_bytes",
        "language_hint": "language_hint",
        "secret_source": "secret_source",
        "secret_env": "secret_env",
        "secret_keychain_service": "secret_keychain_service",
        "secret_keychain_account": "secret_keychain_account",
        "secret_file": "secret_file",
    },
    "retry": {
        "base_seconds": "retry_base_seconds",
        "max_seconds": "retry_max_seconds",
        "max_attempts": "retry_max_attempts",
    },
}

_SECRETLIKE_TOML_KEYS = {"secret", "token", "password", "api_key", "webhook_secret"}


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    config_path = path or DEFAULT_CONFIG_PATH
    if config_path.exists():
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
        _apply_toml(cfg, data, config_path)
    _apply_env(cfg)
    _validate(cfg)
    return cfg


def _apply_toml(cfg: Config, data: dict, config_path: Path) -> None:
    for section, values in data.items():
        if section not in _SECTION_KEYS:
            raise ConfigError(f"{config_path}: unknown section [{section}]")
        if not isinstance(values, dict):
            raise ConfigError(f"{config_path}: [{section}] must be a table")
        for key, value in values.items():
            if key in _SECRETLIKE_TOML_KEYS:
                raise ConfigError(
                    f"{config_path}: [{section}] {key} — secrets do not belong in "
                    "config files; use secret_source = env|keychain|file"
                )
            if key not in _SECTION_KEYS[section]:
                raise ConfigError(f"{config_path}: unknown key [{section}] {key}")
            setattr(cfg, _SECTION_KEYS[section][key], value)


def _apply_env(cfg: Config) -> None:
    for f in fields(Config):
        if f.name == "state_dir":
            continue
        env_name = f"MEETILY_CONNECTOR_{f.name.upper()}"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        if f.type in ("int",):
            try:
                setattr(cfg, f.name, int(raw))
            except ValueError:
                raise ConfigError(f"{env_name} must be an integer, got {raw!r}")
        elif f.type in ("bool",):
            setattr(cfg, f.name, raw.lower() in ("1", "true", "yes"))
        else:
            setattr(cfg, f.name, raw)


def _validate(cfg: Config) -> None:
    if cfg.secret_source not in ("env", "keychain", "file"):
        raise ConfigError(
            f"secret_source must be env|keychain|file, got {cfg.secret_source!r}"
        )
    if cfg.poll_interval_seconds < 5:
        raise ConfigError("poll_interval_seconds must be >= 5")
    if cfg.max_payload_bytes < 10_000:
        raise ConfigError("max_payload_bytes must be >= 10000")


def check_transport(url: str, allow_insecure: bool) -> None:
    """https:// always allowed; http:// only to loopback unless explicitly opted in.

    Called per-request, not just at config load, so a config reload can't
    silently downgrade transport safety.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme != "http":
        raise ConfigError(f"webhook_url must be http(s), got {parts.scheme!r}")
    host = parts.hostname or ""
    if _is_loopback(host):
        return
    if not allow_insecure:
        raise ConfigError(
            f"refusing to send transcripts over plain http to non-loopback host "
            f"{host!r}; use https, or set allow_insecure_transport = true if you "
            "understand the risk"
        )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
