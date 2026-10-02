"""
Central configuration: every environment variable the booth reads, with its default,
parsing and validation in one place.

`load_settings()` collects *all* problems before raising, so a mistyped .env is fixed in one
pass instead of one error per restart. Secrets are never printed (see `Settings.describe`).
"""
import functools
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).parent.parent
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("text", "json")
LOOPBACK_NAMES = {"localhost"}
# .env.example ships with "your_..._here" placeholders; a copy that was never edited is not a key
_PLACEHOLDER_MARKERS = ("your_", "_here")


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    odds_api_key: str | None
    model: str
    fast_model: str
    budget_usd_per_hour: float
    max_usd_per_agent: float
    mock_data: bool
    host: str
    port: int
    auth_token: str | None
    allow_insecure: bool
    log_level: str
    log_format: str
    odds_ttl_s: float
    odds_db: Path
    history_db: str          # a path, or ":memory:"

    def describe(self) -> dict:
        """Effective configuration for logs/--check-config, with secrets masked."""
        def mask(v: str | None) -> str:
            return "unset" if not v else ("set (…" + v[-4:] + ")" if len(v) > 8 else "set")
        return {
            "anthropic_api_key": mask(self.anthropic_api_key),
            "odds_api_key": mask(self.odds_api_key),
            "auth_token": "set" if self.auth_token else "unset",
            "model": self.model, "fast_model": self.fast_model,
            "budget_usd_per_hour": self.budget_usd_per_hour or "unlimited",
            "max_usd_per_agent": self.max_usd_per_agent or "unlimited",
            "mock_data": self.mock_data, "host": self.host, "port": self.port,
            "allow_insecure": self.allow_insecure,
            "log_level": self.log_level, "log_format": self.log_format,
            "odds_ttl_s": self.odds_ttl_s, "odds_db": str(self.odds_db), "history_db": self.history_db,
        }


# ── Parsing helpers (each appends to `errors` instead of raising) ─────────────

def _text(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _secret(env: Mapping[str, str], name: str, errors: list[str]) -> str | None:
    value = _text(env, name)
    if value and all(m in value.lower() for m in _PLACEHOLDER_MARKERS):
        return None   # untouched placeholder == not configured
    return value


def _float(env, name: str, default: float, errors: list[str], minimum: float = 0.0) -> float:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a number")
        return default
    if value < minimum:
        errors.append(f"{name}={raw} must be >= {minimum:g}")
        return default
    return value


def _int(env, name: str, default: int, errors: list[str], lo: int, hi: int) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not an integer")
        return default
    if not lo <= value <= hi:
        errors.append(f"{name}={raw} must be between {lo} and {hi}")
        return default
    return value


def _bool(env, name: str, errors: list[str]) -> bool:
    raw = _text(env, name)
    if raw is None:
        return False
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    errors.append(f"{name}={raw!r} must be 1/0, true/false, yes/no or on/off")
    return False


def _choice(env, name: str, default: str, choices: tuple[str, ...], errors: list[str]) -> str:
    raw = _text(env, name)
    if raw is None:
        return default
    value = raw.upper() if choices is LOG_LEVELS else raw.lower()
    if value not in choices:
        errors.append(f"{name}={raw!r} must be one of: {', '.join(c.lower() for c in choices)}")
        return default
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    errors: list[str] = []
    mock = _bool(env, "BOOTH_MOCK_DATA", errors)
    settings = Settings(
        anthropic_api_key=_secret(env, "ANTHROPIC_API_KEY", errors),
        odds_api_key=_secret(env, "ODDS_API_KEY", errors),
        model=_text(env, "CLAUDE_MODEL") or "claude-sonnet-4-6",
        fast_model=_text(env, "CLAUDE_MODEL_FAST") or "claude-haiku-4-5-20251001",
        budget_usd_per_hour=_float(env, "BOOTH_BUDGET_USD_PER_HOUR", 5.0, errors),
        max_usd_per_agent=_float(env, "BOOTH_MAX_USD_PER_AGENT", 0.50, errors),
        mock_data=mock,
        host=_text(env, "BOOTH_HOST") or "127.0.0.1",
        port=_int(env, "BOOTH_PORT", 8000, errors, 1, 65535),
        auth_token=_text(env, "BOOTH_AUTH_TOKEN"),
        allow_insecure=_bool(env, "BOOTH_ALLOW_INSECURE", errors),
        log_level=_choice(env, "BOOTH_LOG_LEVEL", "INFO", LOG_LEVELS, errors),
        log_format=_choice(env, "BOOTH_LOG_FORMAT", "text", LOG_FORMATS, errors),
        odds_ttl_s=_float(env, "BOOTH_ODDS_TTL", 60.0, errors),
        odds_db=Path(_text(env, "BOOTH_ODDS_DB") or ROOT / "data" / "odds.db"),
        history_db=":memory:" if mock else (_text(env, "BOOTH_HISTORY_DB") or str(ROOT / "data" / "history.db")),
    )
    if errors:
        raise ConfigError(errors)
    return settings


def is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_runtime(settings: Settings, *, demo: bool) -> tuple[list[str], list[str]]:
    """Requirements that depend on how the booth is being run. Returns (errors, warnings)."""
    errors, warnings = [], []
    if not settings.anthropic_api_key:
        errors.append("ANTHROPIC_API_KEY is not set (copy .env.example to .env and add your key; "
                      "an untouched 'your_..._here' placeholder does not count)")
    if not is_loopback(settings.host):
        if not settings.auth_token and not settings.allow_insecure:
            errors.append(
                f"Binding to {settings.host} exposes the dashboard to the network without authentication. "
                "Set BOOTH_AUTH_TOKEN, bind to 127.0.0.1, or set BOOTH_ALLOW_INSECURE=1 if something "
                "else (a reverse proxy, a loopback-only port mapping) already restricts access.")
        elif not settings.auth_token:
            warnings.append(f"Listening on {settings.host} with no BOOTH_AUTH_TOKEN (BOOTH_ALLOW_INSECURE is set).")
    if settings.auth_token and len(settings.auth_token) < 16:
        warnings.append("BOOTH_AUTH_TOKEN is shorter than 16 characters; use a long random value "
                        "(e.g. `python -c 'import secrets; print(secrets.token_urlsafe(24))'`).")
    if not demo and not settings.mock_data and not settings.odds_api_key:
        warnings.append("ODDS_API_KEY is not set: The Degenerate will report that line data is unavailable.")
    if not settings.budget_usd_per_hour:
        warnings.append("BOOTH_BUDGET_USD_PER_HOUR=0: spending is unlimited.")
    for label, name in (("CLAUDE_MODEL", settings.model), ("CLAUDE_MODEL_FAST", settings.fast_model)):
        if not name.startswith("claude-"):
            warnings.append(f"{label}={name!r} does not look like a Claude model id.")
    return errors, warnings


@functools.lru_cache(maxsize=1)
def get() -> Settings:
    """Process-wide settings, loaded once from the environment."""
    return load_settings()


def reset() -> None:
    """Forget cached settings (after the environment changed, and in tests)."""
    get.cache_clear()
