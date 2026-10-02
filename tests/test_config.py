import os
import subprocess
import sys
from pathlib import Path

import pytest

from booth import config
from booth.history import HistoryStore

ROOT = Path(__file__).parent.parent


def load(**env):
    return config.load_settings(env)


def test_defaults():
    s = load()
    assert (s.host, s.port, s.model, s.fast_model) == ("127.0.0.1", 8000, "claude-sonnet-4-6", "claude-haiku-4-5-20251001")
    assert (s.budget_usd_per_hour, s.max_usd_per_agent, s.log_level, s.log_format) == (5.0, 0.5, "INFO", "text")
    assert s.anthropic_api_key is None and s.auth_token is None and not s.mock_data


def test_overrides_are_parsed():
    s = load(BOOTH_PORT="9001", BOOTH_BUDGET_USD_PER_HOUR="0", BOOTH_LOG_LEVEL="debug", BOOTH_LOG_FORMAT="JSON",
             BOOTH_MOCK_DATA="yes", CLAUDE_MODEL="claude-opus-5-5", BOOTH_HOST="0.0.0.0")
    assert (s.port, s.budget_usd_per_hour, s.log_level, s.log_format) == (9001, 0.0, "DEBUG", "json")
    assert s.mock_data and s.model == "claude-opus-5-5" and s.host == "0.0.0.0"


def test_empty_values_count_as_unset():
    s = load(BOOTH_PORT="", ODDS_API_KEY="  ", BOOTH_BUDGET_USD_PER_HOUR="")
    assert s.port == 8000 and s.odds_api_key is None and s.budget_usd_per_hour == 5.0


def test_all_problems_are_reported_together():
    with pytest.raises(config.ConfigError) as e:
        load(BOOTH_PORT="99999", BOOTH_BUDGET_USD_PER_HOUR="abc", BOOTH_MAX_USD_PER_AGENT="-1",
             BOOTH_LOG_LEVEL="loud", BOOTH_LOG_FORMAT="xml", BOOTH_MOCK_DATA="maybe", BOOTH_ODDS_TTL="x")
    assert len(e.value.errors) == 7
    text = str(e.value)
    for name in ("BOOTH_PORT", "BOOTH_BUDGET_USD_PER_HOUR", "BOOTH_MAX_USD_PER_AGENT", "BOOTH_LOG_LEVEL",
                 "BOOTH_LOG_FORMAT", "BOOTH_MOCK_DATA", "BOOTH_ODDS_TTL"):
        assert name in text


def test_placeholder_keys_from_env_example_are_not_keys():
    s = load(ANTHROPIC_API_KEY="your_anthropic_api_key_here", ODDS_API_KEY="your_odds_api_key_here")
    assert s.anthropic_api_key is None and s.odds_api_key is None
    assert load(ANTHROPIC_API_KEY="sk-ant-real-key-1234").anthropic_api_key == "sk-ant-real-key-1234"


def test_describe_never_leaks_secrets():
    s = load(ANTHROPIC_API_KEY="sk-ant-supersecret-1234", ODDS_API_KEY="odds-secret-9876", BOOTH_AUTH_TOKEN="tok-abcdefghijklmnop")
    shown = str(s.describe())
    assert "supersecret" not in shown and "odds-secret" not in shown and "abcdefghijklmnop" not in shown
    assert "…1234" in shown and s.describe()["auth_token"] == "set"


@pytest.mark.parametrize("host,loop", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.5", True),
                                       ("0.0.0.0", False), ("192.168.1.5", False), ("example.com", False)])
def test_is_loopback(host, loop):
    assert config.is_loopback(host) is loop


def runtime(demo=False, **env):
    return config.check_runtime(load(**env), demo=demo)


def test_api_key_is_required():
    errors, _ = runtime()
    assert any("ANTHROPIC_API_KEY" in e for e in errors)
    assert runtime(ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxx")[0] == []


def test_network_binding_needs_a_token_or_an_explicit_opt_out():
    base = {"ANTHROPIC_API_KEY": "sk-ant-xxxxxxxxxx", "BOOTH_HOST": "0.0.0.0"}
    errors, _ = runtime(**base)
    assert len(errors) == 1 and "BOOTH_AUTH_TOKEN" in errors[0]
    assert runtime(**base, BOOTH_AUTH_TOKEN="a-long-random-token-value")[0] == []
    errors, warnings = runtime(**base, BOOTH_ALLOW_INSECURE="1")
    assert errors == [] and any("no BOOTH_AUTH_TOKEN" in w for w in warnings)


def test_warnings():
    _, w = runtime(ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxx", BOOTH_AUTH_TOKEN="short", BOOTH_BUDGET_USD_PER_HOUR="0",
                   CLAUDE_MODEL="gpt-9")
    text = " | ".join(w)
    assert "ODDS_API_KEY" in text and "shorter than 16" in text and "unlimited" in text and "gpt-9" in text
    # demo mode doesn't need an Odds key
    assert not any("ODDS_API_KEY" in x for x in runtime(demo=True, ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxx")[1])


def test_mock_mode_uses_in_memory_history_and_ignores_the_configured_path():
    assert load(BOOTH_MOCK_DATA="1", BOOTH_HISTORY_DB="/tmp/x.db").history_db == ":memory:"
    assert load(BOOTH_HISTORY_DB="/tmp/x.db").history_db == "/tmp/x.db"


# ── History store: lazy connection, demo mode must not touch the disk ─────────

def test_history_store_is_lazy_and_use_memory_avoids_the_disk(tmp_path):
    path = tmp_path / "sub" / "h.db"
    store = HistoryStore(path)
    assert not path.parent.exists()                      # nothing opened at construction
    store.use_memory()
    store.add({"event": {"game_id": "G"}})
    assert store.recent() and not path.parent.exists()   # still nothing on disk
    with pytest.raises(RuntimeError):
        store.use_memory()                               # too late once in use


def test_history_store_creates_parent_directories_on_first_use(tmp_path):
    path = tmp_path / "deep" / "dir" / "h.db"
    HistoryStore(path).add({"event": {"game_id": "G"}})
    assert path.exists()


# ── The real entry point ──────────────────────────────────────────────────────

def run_main(*args, **env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BOOTH_", "ANTHROPIC", "ODDS", "CLAUDE_"))}
    return subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, env={**clean, **env},
                          capture_output=True, text=True, timeout=60)


def test_main_check_config_ok_and_masks_secrets():
    r = run_main("--check-config", ANTHROPIC_API_KEY="sk-ant-supersecret-1234")
    assert r.returncode == 0 and "Effective configuration" in r.stdout
    assert "supersecret" not in r.stdout + r.stderr


def test_main_exits_2_with_every_problem_listed():
    r = run_main("--check-config", BOOTH_PORT="nope", BOOTH_LOG_LEVEL="loud")
    assert r.returncode == 2 and "BOOTH_PORT" in r.stderr and "BOOTH_LOG_LEVEL" in r.stderr


def test_main_refuses_unauthenticated_network_binding():
    r = run_main("--host", "0.0.0.0", ANTHROPIC_API_KEY="sk-ant-xxxxxxxxxx")
    assert r.returncode == 2 and "BOOTH_AUTH_TOKEN" in r.stderr


def test_main_refuses_to_start_without_an_api_key():
    r = run_main()
    assert r.returncode == 2 and "ANTHROPIC_API_KEY" in r.stderr


def test_allowed_origins_are_parsed_and_validated():
    s = load(BOOTH_ALLOWED_ORIGINS="https://Booth.Example.com/, http://localhost:3000")
    assert s.allowed_origins == ("https://booth.example.com", "http://localhost:3000")
    assert load().allowed_origins == ()
    with pytest.raises(config.ConfigError) as e:
        load(BOOTH_ALLOWED_ORIGINS="booth.example.com,https://ok.example.com/path")
    assert len(e.value.errors) == 2
