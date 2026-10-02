"""NBA scoreboard data access."""
import asyncio

from booth.events import game_label


def _fetch_scoreboard_sync() -> list[dict]:
    """Blocking call — run in a thread executor."""
    from nba_api.live.nba.endpoints import scoreboard as live_sb
    board = live_sb.ScoreBoard()
    return board.games.get_dict()


async def fetch_started_games() -> list[dict]:
    """Return every NBA game that has started today — live and final (excludes pre-game)."""
    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _fetch_scoreboard_sync)
    return [g for g in games if (g.get("period", 0) or 0) > 0]


def games_payload(games: list[dict]) -> list[dict]:
    """Slim game summaries for the dashboard game selector."""
    result = []
    for g in games:
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        result.append({
            "gameId": g.get("gameId", ""),
            "label": game_label(g),
            "homeTeam": home.get("teamTricode", ""),
            "awayTeam": away.get("teamTricode", ""),
            "homeScore": home.get("score", 0) or 0,
            "awayScore": away.get("score", 0) or 0,
            "quarter": g.get("period", 0) or 0,
            "clock": g.get("gameClock", ""),
            "status": g.get("gameStatusText", ""),
        })
    return result
