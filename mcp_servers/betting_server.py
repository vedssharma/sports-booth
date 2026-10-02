#!/usr/bin/env python3
"""Live betting lines MCP server — wraps The Odds API for NBA spreads and totals."""
import json
import os

from mcp.server.fastmcp import FastMCP

from _common import mock_enabled, offload, serve, unavailable
from booth import odds

mcp = FastMCP("betting-lines")

# Realistic mock data used when no API key is set
_MOCK_GAMES = [
    {
        "game": "Lakers vs Celtics",
        "commence_time": "2026-04-21T23:00:00Z",
        "opening": {"spread": -2.5, "total": 221.5, "moneyline_fav": -130, "moneyline_dog": 110},
        "current": {"spread": -5.5, "total": 225.5, "moneyline_fav": -215, "moneyline_dog": 178},
        "movement": {
            "spread_move": "-3.0 pts (sharp money on LAL after BOS timeout)",
            "total_move": "+4.0 pts (pace-up, foul trouble on both centers)",
            "last_updated": "2026-04-21T21:34:00Z",
        },
    },
]


def _api_key() -> str | None:
    return os.getenv("ODDS_API_KEY")


def _matching(events: list[dict], game: str) -> list[dict]:
    """Events involving the teams named in `game`; all events if no team is recognised."""
    teams = odds.resolve_teams(game)
    if not teams:
        return events
    both = [e for e in events if {e["home"], e["away"]} <= teams]
    return both or [e for e in events if e["home"] in teams or e["away"] in teams]


def _describe(e: dict) -> dict:
    out = {
        "game": f"{e['away']} @ {e['home']}",
        "commence_time": e["commence_time"],
        "home_spread": e["spread_home"],
        "total": e["total"],
        "moneyline": {e["home"]: e["ml_home"], e["away"]: e["ml_away"]},
        "books_averaged": e["books"],
    }
    if e["ml_home"] is not None:
        out["home_implied_win_prob"] = round(odds.american_to_prob(e["ml_home"]), 3)
    return out


def _live_events(game: str) -> list[dict] | str:
    """Fetch + record current odds. Returns events for `game`, or an 'unavailable' result string."""
    key = _api_key()
    if not key:
        return unavailable("Betting odds", "ODDS_API_KEY is not set")
    try:
        events = _matching(odds.snapshot_now(key), game)
    except Exception as e:
        return unavailable("Betting odds", str(e))
    if not events:
        return unavailable("Betting odds", f"no odds listed for '{game}' (game may be finished)")
    return events


@offload(mcp)
def get_live_odds(game: str = "") -> str:
    """Current consensus odds (median across US books) — spread, total, moneyline.
    `game` may be team tricodes/names, e.g. "LAL @ BOS"; omit for all games. Spread is the home team's."""
    if mock_enabled() and not _api_key():
        return json.dumps({"note": "MOCK DATA (demo mode)", "games": _MOCK_GAMES}, indent=2)
    events = _live_events(game)
    if isinstance(events, str):
        return events
    return json.dumps([_describe(e) for e in events[:6]], indent=2)


@offload(mcp)
def get_line_movement(game: str) -> str:
    """How the line has moved since the booth first saw this game (opening → current).
    `game` is team tricodes/names, e.g. "LAL @ BOS"."""
    if mock_enabled() and not _api_key():
        return json.dumps({"note": "MOCK DATA (demo mode)", "game": game,
                           "movement": _MOCK_GAMES[0]["movement"],
                           "opening": _MOCK_GAMES[0]["opening"],
                           "current": _MOCK_GAMES[0]["current"]}, indent=2)
    events = _live_events(game)
    if isinstance(events, str):
        return events
    results = []
    for e in events[:3]:
        mv = odds.movement(e["event_id"])
        results.append({
            "game": f"{e['away']} @ {e['home']}",
            "movement": mv or "Only one snapshot so far — no movement to report yet.",
            "note": "'Opening' = first line this booth recorded, not the book's true opener.",
        })
    return json.dumps(results, indent=2)


@offload(mcp)
def get_betting_context(game: str) -> str:
    """Derived context for a game: implied win probability, total, and spread relative to key numbers.
    `game` is team tricodes/names, e.g. "LAL @ BOS"."""
    if mock_enabled() and not _api_key():
        return json.dumps({"note": "MOCK DATA (demo mode)", "game": game,
                           "context": {"key_number_proximity": "Spread -5.5 sits between 3 and 7.",
                                       "recommendation": "Wait for a hook before backing the favorite."}},
                          indent=2)
    events = _live_events(game)
    if isinstance(events, str):
        return events
    return json.dumps([{**_describe(e), "key_numbers": odds.key_number_note(e["spread_home"])}
                       for e in events[:3]], indent=2)


if __name__ == "__main__":
    serve(mcp)
