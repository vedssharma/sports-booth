"""Hardcoded Lakers vs Celtics game for --demo mode (no live games or NBA API needed)."""
import asyncio

from booth import log

from booth.pipeline import process_event
from booth.server import manager

logger = log.get("demo")

DEMO_EVENTS = [
    {
        "type": "game_start",
        "game": "Lakers vs Celtics",
        "game_id": "0022401001",
        "venue": "TD Garden, Boston",
        "quarter": 1,
        "time_remaining": "12:00",
        "score": {"LAL": 0, "BOS": 0},
        "context": "Both teams healthy. LeBron James listed as probable.",
    },
    {
        "type": "scoring_run",
        "game": "Lakers vs Celtics",
        "game_id": "0022401001",
        "quarter": 2,
        "time_remaining": "5:41",
        "score": {"LAL": 48, "BOS": 37},
        "event": "Lakers go on 14-2 run over 4 minutes",
        "key_player": "LeBron James",
        "key_stat": "12 points in Q2, 6-8 FG, 3 assists",
        "context": "Anthony Davis sitting with 2 fouls. LeBron dominating without him.",
    },
    {
        "type": "player_milestone",
        "game": "Lakers vs Celtics",
        "game_id": "0022401001",
        "quarter": 3,
        "time_remaining": "8:12",
        "score": {"LAL": 72, "BOS": 68},
        "event": "Rookie Austin Reaves posts 20th point — 20+ in 3rd consecutive game",
        "key_player": "Austin Reaves",
        "key_stat": "20 pts, 6 ast, 4 reb on 8-12 FG",
        "context": "The Celtics had no answer for Reaves cutting off ball screens.",
    },
    {
        "type": "momentum_shift",
        "game": "Lakers vs Celtics",
        "game_id": "0022401001",
        "quarter": 4,
        "time_remaining": "3:05",
        "score": {"LAL": 101, "BOS": 99},
        "event": "Jayson Tatum hits back-to-back threes — Celtics within 2",
        "key_player": "Jayson Tatum",
        "key_stat": "9 points in 90 seconds, 3-3 from three in Q4",
        "context": "Lakers called timeout. AD is back in. Crowd is deafening.",
    },
    {
        "type": "buzzer_beater",
        "game": "Lakers vs Celtics",
        "game_id": "0022401001",
        "quarter": 4,
        "time_remaining": "0:00",
        "score": {"LAL": 108, "BOS": 106},
        "event": "LeBron James hits go-ahead layup with 1.4 seconds left",
        "key_player": "LeBron James",
        "key_stat": "Final line: 38 pts, 10 ast, 8 reb. 15-22 FG.",
        "context": "Celtics inbounds play fails. Lakers win.",
    },
]


async def demo_loop(interval: int, cli_only: bool) -> None:
    logger.info("demo mode", extra={"events": len(DEMO_EVENTS), "interval_s": interval})
    if not cli_only:
        # Send a synthetic games list so the selector renders in demo mode
        demo_games = [{
            "gameId": "0022401001",
            "label": "LAL @ BOS",
            "homeTeam": "BOS",
            "awayTeam": "LAL",
            "homeScore": 0,
            "awayScore": 0,
            "quarter": 1,
            "clock": "12:00",
            "status": "Demo game",
        }]
        await manager.broadcast({"type": "games", "data": demo_games})
    for i, event in enumerate(DEMO_EVENTS):
        await process_event(event, cli_only)
        if i < len(DEMO_EVENTS) - 1:
            logger.debug("next demo event", extra={"in_s": interval})
            await asyncio.sleep(interval)
    logger.info("demo complete")
