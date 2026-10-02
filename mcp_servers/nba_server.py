#!/usr/bin/env python3
"""NBA stats MCP server — wraps nba_api's *live* endpoints (cdn.nba.com).

The live endpoints (scoreboard / boxscore / playbyplay) update in near real time and are not
rate-limited like stats.nba.com. They expose raw counting stats, so efficiency metrics
(eFG%, TS%) are computed here.
"""
import json
import re

from mcp.server.fastmcp import FastMCP

from _common import mock_enabled, offload, serve, unavailable  # noqa: I001 (adds repo root to sys.path)
from booth.cache import ttl_cache

mcp = FastMCP("nba-stats")


# ── Pure helpers (unit-testable, no network) ──────────────────────────────────

def _minutes(iso: str | None) -> float:
    """Parse the live API's ISO-8601 duration ('PT25M01.00S') into minutes."""
    m = re.match(r"PT(\d+)M(?:([\d.]+)S)?", iso or "")
    return round(int(m.group(1)) + float(m.group(2) or 0) / 60, 1) if m else 0.0


def _pct(num: float, den: float) -> float | None:
    return round(100 * num / den, 1) if den else None


def efg_pct(fgm: float, fg3m: float, fga: float) -> float | None:
    return _pct(fgm + 0.5 * fg3m, fga)


def ts_pct(pts: float, fga: float, fta: float) -> float | None:
    denom = 2 * (fga + 0.44 * fta)
    return _pct(pts, denom)


def summarize_player(p: dict) -> dict:
    s = p.get("statistics", {})
    fga = s.get("fieldGoalsAttempted", 0)
    return {
        "name": p.get("name"),
        "starter": p.get("starter") == "1",
        "onCourt": p.get("oncourt") == "1",
        "minutes": _minutes(s.get("minutesCalculated") or s.get("minutes")),
        "points": s.get("points", 0),
        "rebounds": s.get("reboundsTotal", 0),
        "assists": s.get("assists", 0),
        "turnovers": s.get("turnovers", 0),
        "fouls": s.get("foulsPersonal", 0),
        "fg": f"{s.get('fieldGoalsMade', 0)}-{fga}",
        "fg3": f"{s.get('threePointersMade', 0)}-{s.get('threePointersAttempted', 0)}",
        "ft": f"{s.get('freeThrowsMade', 0)}-{s.get('freeThrowsAttempted', 0)}",
        "efgPct": efg_pct(s.get("fieldGoalsMade", 0), s.get("threePointersMade", 0), fga),
        "tsPct": ts_pct(s.get("points", 0), fga, s.get("freeThrowsAttempted", 0)),
        "plusMinus": s.get("plusMinusPoints", 0),
    }


def _played(p: dict) -> bool:
    return p.get("played") == "1"


def summarize_team(team: dict) -> dict:
    players = [p for p in team.get("players", []) if _played(p)]
    totals: dict[str, float] = {}
    for p in players:
        for k, v in p.get("statistics", {}).items():
            if isinstance(v, (int, float)):
                totals[k] = totals.get(k, 0) + v
    summaries = [summarize_player(p) for p in players]
    summaries.sort(key=lambda x: x["minutes"], reverse=True)
    return {
        "team": team.get("teamTricode"),
        "score": team.get("score"),
        "timeoutsRemaining": team.get("timeoutsRemaining"),
        "inBonus": team.get("inBonus") == "1",
        "efgPct": efg_pct(totals.get("fieldGoalsMade", 0), totals.get("threePointersMade", 0),
                          totals.get("fieldGoalsAttempted", 0)),
        "tsPct": ts_pct(totals.get("points", 0), totals.get("fieldGoalsAttempted", 0),
                        totals.get("freeThrowsAttempted", 0)),
        "turnovers": totals.get("turnovers", 0),
        "pointsInPaint": totals.get("pointsInThePaint", 0),
        "fastBreakPoints": totals.get("pointsFastBreak", 0),
        "topPlayers": summaries[:6],
    }


def lineup_split(team: dict) -> dict:
    """Starters vs bench: points, +/- and eFG% (the live feed has no per-lineup net rating)."""
    out: dict = {"team": team.get("teamTricode")}
    for label, is_starter in (("starters", True), ("bench", False)):
        group = [p for p in team.get("players", [])
                 if _played(p) and (p.get("starter") == "1") == is_starter]
        stats = [p.get("statistics", {}) for p in group]
        sum_ = lambda k: sum(s.get(k, 0) for s in stats)  # noqa: E731
        out[label] = {
            "players": [p.get("name") for p in group],
            "points": sum_("points"),
            "plusMinusTotal": sum_("plusMinusPoints"),
            "efgPct": efg_pct(sum_("fieldGoalsMade"), sum_("threePointersMade"),
                              sum_("fieldGoalsAttempted")),
        }
    out["onCourtNow"] = [p.get("name") for p in team.get("players", []) if p.get("oncourt") == "1"]
    return out


# The server is long-lived and shared, so cache upstream calls briefly: several agents (and
# several tool calls per agent) ask about the same game within seconds.
@ttl_cache(10)
def _fetch_game(game_id: str) -> dict:
    from nba_api.live.nba.endpoints import boxscore
    return boxscore.BoxScore(game_id=game_id).game.get_dict()


@ttl_cache(10)
def _fetch_scoreboard() -> list[dict]:
    from nba_api.live.nba.endpoints import scoreboard as live_sb
    return live_sb.ScoreBoard().games.get_dict()


@ttl_cache(10)
def _fetch_actions(game_id: str) -> list[dict]:
    from nba_api.live.nba.endpoints import playbyplay
    return playbyplay.PlayByPlay(game_id=game_id).actions.get_dict()


# ── Tools ─────────────────────────────────────────────────────────────────────

@offload(mcp)
def get_live_scoreboard() -> str:
    """Get today's NBA live scoreboard with all game scores and status."""
    try:
        games = _fetch_scoreboard()
        if not games:
            return json.dumps({"note": "No live NBA games right now.", "games": []})
        return json.dumps([{
            "gameId": g.get("gameId"),
            "homeTeam": g.get("homeTeam", {}).get("teamTricode"),
            "awayTeam": g.get("awayTeam", {}).get("teamTricode"),
            "homeScore": g.get("homeTeam", {}).get("score"),
            "awayScore": g.get("awayTeam", {}).get("score"),
            "period": g.get("period"),
            "gameClock": g.get("gameClock"),
            "gameStatus": g.get("gameStatusText"),
        } for g in games], indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("NBA scoreboard", str(e))
        return json.dumps({"games": [{
            "gameId": "0022401001", "homeTeam": "LAL", "awayTeam": "BOS",
            "homeScore": 87, "awayScore": 79, "period": 3, "gameClock": "4:23",
            "gameStatus": "3rd Qtr",
        }]}, indent=2)


@offload(mcp)
def get_boxscore(game_id: str) -> str:
    """Get live team and top-player stats for a game (eFG%, TS%, plus/minus, paint/fast-break points)."""
    try:
        game = _fetch_game(game_id)
        return json.dumps({
            "status": game.get("gameStatusText"),
            "period": game.get("period"),
            "clock": game.get("gameClock"),
            "home": summarize_team(game.get("homeTeam", {})),
            "away": summarize_team(game.get("awayTeam", {})),
        }, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("NBA boxscore", str(e))
        return json.dumps({
            "home": {"team": "LAL", "efgPct": 54.2, "tsPct": 60.1, "topPlayers": [
                {"name": "LeBron James", "points": 28, "plusMinus": 18, "efgPct": 61.1, "tsPct": 68.4},
                {"name": "Anthony Davis", "points": 22, "plusMinus": 12, "efgPct": 58.3, "tsPct": 63.1}]},
            "away": {"team": "BOS", "efgPct": 49.1, "tsPct": 56.3, "topPlayers": [
                {"name": "Jayson Tatum", "points": 19, "plusMinus": -8, "efgPct": 49.1, "tsPct": 54.1}]},
        }, indent=2)


@offload(mcp)
def get_player_game_stats(game_id: str, player_name: str) -> str:
    """Get detailed live stats for a specific player in a game (partial name match)."""
    try:
        game = _fetch_game(game_id)
        needle = player_name.lower()
        matches = [summarize_player(p)
                   for side in ("homeTeam", "awayTeam")
                   for p in game.get(side, {}).get("players", [])
                   if needle in (p.get("name") or "").lower()]
        if not matches:
            return json.dumps({"error": f"Player '{player_name}' not found in game {game_id}."})
        return json.dumps(matches, indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("NBA player stats", str(e))
        return json.dumps([{"name": player_name, "points": 28, "assists": 9, "rebounds": 7,
                            "plusMinus": 18, "efgPct": 61.1, "tsPct": 68.4}], indent=2)


@offload(mcp)
def get_team_lineup_impact(game_id: str, team_tricode: str) -> str:
    """Compare starters vs bench (points, plus/minus, eFG%) and who is on court right now."""
    try:
        game = _fetch_game(game_id)
        for side in ("homeTeam", "awayTeam"):
            team = game.get(side, {})
            if (team.get("teamTricode") or "").upper() == team_tricode.upper():
                return json.dumps(lineup_split(team), indent=2)
        return json.dumps({"error": f"Team '{team_tricode}' not in game {game_id}."})
    except Exception as e:
        if not mock_enabled():
            return unavailable("NBA lineup", str(e))
        return json.dumps({"team": team_tricode, "starters": {"plusMinusTotal": 31, "efgPct": 55.0},
                           "bench": {"plusMinusTotal": -14, "efgPct": 41.3}}, indent=2)


@offload(mcp)
def get_recent_plays(game_id: str, n: int = 10) -> str:
    """Get the last N play-by-play actions (clock, team, description, running score)."""
    try:
        actions = _fetch_actions(game_id)
        n = max(1, min(n, 25))
        return json.dumps([{
            "period": a.get("period"),
            "clock": a.get("clock"),
            "team": a.get("teamTricode"),
            "play": a.get("description"),
            "score": f"{a.get('scoreAway')}-{a.get('scoreHome')}",
        } for a in actions[-n:]], indent=2)
    except Exception as e:
        if not mock_enabled():
            return unavailable("NBA play-by-play", str(e))
        return json.dumps([{"period": 4, "clock": "PT03M05.00S", "team": "BOS",
                            "play": "Tatum 26' 3PT", "score": "99-101"}], indent=2)


if __name__ == "__main__":
    serve(mcp)
