"""
Sports Booth — entry point.

CLI usage:
  uv run python main.py              # live NBA games, web dashboard at http://localhost:8000
  uv run python main.py --demo       # use hardcoded demo events instead of live data
  uv run python main.py --cli        # no web server, terminal output only
  uv run python main.py --interval 60  # seconds between scoreboard polls (default: 45)
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

load_dotenv()

from booth import odds
from booth.events import EventDetector, game_label, is_final
from booth.orchestrator import run_booth_commentary

# ── Demo game events (used with --demo flag) ──────────────────────────────────

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

# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Sports Booth", version="0.1.0")
_DASHBOARD = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD.read_text())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "clients": manager.count}


# ── Live NBA data fetching ────────────────────────────────────────────────────

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


def _games_payload(games: list[dict]) -> list[dict]:
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


# ── Commentary helpers ────────────────────────────────────────────────────────

async def _process_event(event: dict, cli_only: bool) -> None:
    label = event.get("event", event.get("type", "event"))
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"  {event.get('game', '')}  |  Q{event.get('quarter', '')} {event.get('time_remaining', '')}  |  {event.get('score', '')}")

    if not cli_only:
        await manager.broadcast({"type": "event", "data": event})

    print("  Fetching booth commentary (3 agents in parallel)…")
    commentary = await run_booth_commentary(event)

    print(f"\n  📊 ANALYST:    {commentary['analyst'][:200]}")
    print(f"\n  📚 HISTORIAN:  {commentary['historian'][:200]}")
    print(f"\n  🎲 DEGENERATE: {commentary['degenerate'][:200]}")

    if not cli_only:
        await manager.broadcast({"type": "commentary", "data": commentary})


# ── Live polling loop ─────────────────────────────────────────────────────────

async def live_loop(interval: int, cli_only: bool) -> None:
    """Poll the NBA live scoreboard and generate commentary on detected events."""
    detector = EventDetector()
    seen_ids: set[str] = set()
    warned_no_games = False

    print(f"  Mode: LIVE  |  Polling every {interval}s")

    while True:
        try:
            started = await fetch_started_games()
        except Exception as e:
            print(f"  ⚠️  Scoreboard fetch error: {e}. Retrying in {interval}s…")
            await asyncio.sleep(interval)
            continue

        live = [g for g in started if not is_final(g)]

        if live:
            warned_no_games = False
            # Broadcast current game list so the dashboard can render the selector
            if not cli_only:
                await manager.broadcast({"type": "games", "data": _games_payload(live)})

            # Record opening odds the first time we see a game so line movement is real
            new_ids = {g["gameId"] for g in live} - seen_ids
            if new_ids and os.getenv("ODDS_API_KEY"):
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, odds.snapshot_now, os.environ["ODDS_API_KEY"])
                except Exception as e:
                    print(f"  ⚠️  Odds snapshot failed: {e}")
            seen_ids |= new_ids

        # Includes games that just went final, so the detector can announce them once
        for event in detector.detect(started):
            await _process_event(event, cli_only)

        if not live and not warned_no_games:
            msg = "No live NBA games right now. Booth will activate automatically when games start."
            print(f"\n  ⏸  {msg}")
            if not cli_only:
                await manager.broadcast({"type": "status", "message": msg})
            warned_no_games = True

        print(f"\n  ⏱  Next poll in {interval}s…")
        await asyncio.sleep(interval)


# ── Demo loop (hardcoded events) ──────────────────────────────────────────────

async def demo_loop(interval: int, cli_only: bool) -> None:
    print(f"  Mode: DEMO  |  {len(DEMO_EVENTS)} events, {interval}s apart")
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
        await _process_event(event, cli_only)
        if i < len(DEMO_EVENTS) - 1:
            print(f"\n  ⏱  Next event in {interval}s…")
            await asyncio.sleep(interval)
    print(f"\n{'─'*60}")
    print("  Demo complete.")


# ── Entry points ──────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sports Booth — AI commentary system")
    parser.add_argument("--demo", action="store_true", help="Use hardcoded demo events instead of live NBA data")
    parser.add_argument("--cli", action="store_true", help="CLI-only mode (no web server)")
    parser.add_argument("--interval", type=int, default=45, help="Seconds between polls/events (default: 45)")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
    return parser.parse_args()


async def _run(interval: int, cli_only: bool, demo: bool, port: int) -> None:
    loop_fn = demo_loop if demo else live_loop

    if cli_only:
        await loop_fn(interval, cli_only=True)
        return

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def _start_loop() -> None:
        await asyncio.sleep(1)  # let server bind
        print(f"  Dashboard: http://localhost:{port}")
        await loop_fn(interval, cli_only=False)

    await asyncio.gather(server.serve(), _start_loop())


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY not set. Copy .env.example → .env and add your key.")
        return

    args = _parse_args()
    if args.demo:
        # Mock tool data is only allowed in demo mode; live mode reports "unavailable" instead.
        os.environ["BOOTH_MOCK_DATA"] = "1"

    print("🏀 Sports Booth starting…")
    print(f"   Model:    {os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')}")
    print(f"   Interval: {args.interval}s")
    if args.demo:
        print("   ⚠️  Demo mode — using hardcoded Lakers vs Celtics events")

    asyncio.run(_run(args.interval, args.cli, args.demo, args.port))


if __name__ == "__main__":
    main()
