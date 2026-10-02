import json

import pytest

import betting_server
import nba_server
import rag_server
from booth import odds


# ── Mock gating ───────────────────────────────────────────────────────────────

def test_live_mode_never_returns_mock_data(monkeypatch):
    monkeypatch.delenv("BOOTH_MOCK_DATA", raising=False)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr("nba_api.live.nba.endpoints.boxscore.BoxScore",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    for result in (nba_server.get_boxscore("1"), betting_server.get_live_odds("LAL"),
                   betting_server.get_betting_context("LAL"), betting_server.get_line_movement("LAL")):
        assert "unavailable" in json.loads(result)["error"]


def test_demo_mode_serves_mock_odds(monkeypatch):
    monkeypatch.setenv("BOOTH_MOCK_DATA", "1")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert "MOCK" in betting_server.get_live_odds("")


def test_rag_unavailable_when_db_errors(monkeypatch):
    monkeypatch.delenv("BOOTH_MOCK_DATA", raising=False)
    monkeypatch.setattr(rag_server, "_get_store", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert "unavailable" in json.loads(rag_server.search_historical_games("q"))["error"]


# ── NBA stat math ─────────────────────────────────────────────────────────────

def test_shooting_percentages():
    assert nba_server.efg_pct(6, 2, 12) == 58.3
    assert nba_server.ts_pct(21, 12, 7) == 69.6
    assert nba_server.efg_pct(0, 0, 0) is None


def test_minutes_parsing():
    assert nba_server._minutes("PT25M01.00S") == 25.0
    assert nba_server._minutes("PT25M") == 25.0
    assert nba_server._minutes(None) == 0.0


def _player(name, starter, pts, fgm, fga, tpm=0, plus=0, oncourt="0"):
    return {"name": name, "starter": starter, "played": "1", "oncourt": oncourt, "statistics": {
        "points": pts, "fieldGoalsMade": fgm, "fieldGoalsAttempted": fga,
        "threePointersMade": tpm, "plusMinusPoints": plus, "minutesCalculated": "PT20M"}}


def test_lineup_split_separates_starters_and_bench():
    team = {"teamTricode": "LAL", "players": [
        _player("A", "1", 20, 8, 15, plus=10, oncourt="1"),
        _player("B", "0", 4, 1, 8, plus=-6),
    ]}
    split = nba_server.lineup_split(team)
    assert split["starters"]["points"] == 20 and split["bench"]["plusMinusTotal"] == -6
    assert split["onCourtNow"] == ["A"]


# ── Odds ──────────────────────────────────────────────────────────────────────

def _event(spread, total, ml_home, books=3):
    market = [
        {"key": "spreads", "outcomes": [{"name": "Boston Celtics", "point": spread, "price": -110},
                                        {"name": "Los Angeles Lakers", "point": -spread, "price": -110}]},
        {"key": "totals", "outcomes": [{"name": "Over", "point": total, "price": -110}]},
        {"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": ml_home},
                                    {"name": "Los Angeles Lakers", "price": 120}]},
    ]
    return {"id": "e1", "home_team": "Boston Celtics", "away_team": "Los Angeles Lakers",
            "commence_time": "t", "bookmakers": [{"markets": market}] * books}


def test_resolve_teams():
    assert odds.resolve_teams("LAL @ BOS") == {"Los Angeles Lakers", "Boston Celtics"}
    assert odds.resolve_teams("Lakers vs Celtics") == {"Los Angeles Lakers", "Boston Celtics"}
    assert odds.resolve_teams("nothing here") == set()


def test_normalize_uses_median_across_books():
    ev = _event(-2.5, 221.5, -130, books=0)
    ev["bookmakers"] = [{"markets": _event(s, 220, -120)["bookmakers"][0]["markets"]} for s in (-2, -3, -9)]
    assert odds.normalize_event(ev)["spread_home"] == -3


def test_implied_probability():
    assert odds.american_to_prob(-200) == pytest.approx(2 / 3)
    assert odds.american_to_prob(150) == pytest.approx(0.4)


def test_line_movement_from_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(odds, "DB_PATH", tmp_path / "odds.db")
    assert odds.movement("e1") is None
    odds.record_snapshots([odds.normalize_event(_event(-2.5, 221.5, -130))], now=0)
    assert odds.movement("e1") is None  # a single snapshot is not movement
    odds.record_snapshots([odds.normalize_event(_event(-5.5, 225.5, -215))], now=2520)
    mv = odds.movement("e1")
    assert mv["spread_move"] == -3.0 and mv["total_move"] == 4.0 and mv["tracked_minutes"] == 42
