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


async def fetch_live_games() -> list[dict]:
    """Return all currently in-progress NBA games (excludes pre-game and final)."""
    loop = asyncio.get_event_loop()
    games = await loop.run_in_executor(None, _fetch_scoreboard_sync)
    live = []
    for g in games:
        status = g.get("gameStatusText", "")
        period = g.get("period", 0) or 0
        # period > 0 means game has started; exclude Final
        if period > 0 and "Final" not in status and "final" not in status.lower():
            live.append(g)
    return live


def _game_label(game: dict) -> str:
    home = game.get("homeTeam", {}).get("teamTricode", "?")
    away = game.get("awayTeam", {}).get("teamTricode", "?")
    return f"{away} @ {home}"


def _game_score(game: dict) -> dict:
    home = game.get("homeTeam", {})
    away = game.get("awayTeam", {})
    return {
        home.get("teamTricode", "HOME"): home.get("score", 0) or 0,
        away.get("teamTricode", "AWAY"): away.get("score", 0) or 0,
    }


def _games_payload(games: list[dict]) -> list[dict]:
    """Slim game summaries for the dashboard game selector."""
    result = []
    for g in games:
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        result.append({
            "gameId": g.get("gameId", ""),
            "label": _game_label(g),
            "homeTeam": home.get("teamTricode", ""),
            "awayTeam": away.get("teamTricode", ""),
            "homeScore": home.get("score", 0) or 0,
            "awayScore": away.get("score", 0) or 0,
            "quarter": g.get("period", 0) or 0,
            "clock": g.get("gameClock", ""),
            "status": g.get("gameStatusText", ""),
        })
    return result


# ── Event detection ───────────────────────────────────────────────────────────

# How many points one team must outscore the other since last poll to flag a run
SCORING_RUN_THRESHOLD = 7


def detect_events(prev_by_id: dict[str, dict], curr_games: list[dict]) -> list[dict]:
    """
    Compare previous game states to current and return meaningful events.
    On first call (empty prev_by_id), every live game generates a 'game_update' event.
    """
    events: list[dict] = []

    for game in curr_games:
        game_id = game.get("gameId", "")
        label = _game_label(game)
        score = _game_score(game)
        period = game.get("period", 0) or 0
        clock = game.get("gameClock", "")
        status = game.get("gameStatusText", "")

        home = game.get("homeTeam", {})
        away = game.get("awayTeam", {})
        home_score = home.get("score", 0) or 0
        away_score = away.get("score", 0) or 0
        home_code = home.get("teamTricode", "HOME")
        away_code = away.get("teamTricode", "AWAY")

        prev = prev_by_id.get(game_id)

        # ── First time we see this game ───────────────────────────────────────
        if prev is None:
            events.append({
                "type": "game_update",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"{label} is live — {status}",
                "context": f"Joining the broadcast in Q{period}. Score: {away_code} {away_score} — {home_code} {home_score}",
            })
            continue

        prev_period = prev.get("period", 0) or 0
        prev_home = prev.get("homeTeam", {})
        prev_away = prev.get("awayTeam", {})
        prev_home_score = prev_home.get("score", 0) or 0
        prev_away_score = prev_away.get("score", 0) or 0

        home_pts = home_score - prev_home_score
        away_pts = away_score - prev_away_score

        # ── Quarter change ────────────────────────────────────────────────────
        if period > prev_period:
            label_q = "Overtime!" if period > 4 else f"Q{period} underway"
            events.append({
                "type": "quarter_start",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"{label} — {label_q}",
                "context": (
                    f"End of Q{prev_period} score: {away_code} {prev_away_score} — "
                    f"{home_code} {prev_home_score}. Margin: {abs(prev_home_score - prev_away_score)} pts."
                ),
            })

        # ── Scoring run ───────────────────────────────────────────────────────
        elif home_pts - away_pts >= SCORING_RUN_THRESHOLD:
            events.append({
                "type": "scoring_run",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"{home_code} on a {home_pts}-{away_pts} run",
                "context": (
                    f"{home_code} extending lead to {home_score - away_score:+d}. "
                    f"Current score: {away_code} {away_score} — {home_code} {home_score}"
                ),
            })

        elif away_pts - home_pts >= SCORING_RUN_THRESHOLD:
            events.append({
                "type": "scoring_run",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"{away_code} on a {away_pts}-{home_pts} run",
                "context": (
                    f"{away_code} closing/extending lead to {away_score - home_score:+d}. "
                    f"Current score: {away_code} {away_score} — {home_code} {home_score}"
                ),
            })

        # ── Crunch time (Q4/OT, within 5) ────────────────────────────────────
        elif period >= 4 and abs(home_score - away_score) <= 5 and (home_pts + away_pts) > 0:
            events.append({
                "type": "close_game",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"Crunch time — game within {abs(home_score - away_score)}",
                "context": (
                    f"{away_code} {away_score} — {home_code} {home_score}. "
                    f"{'Tie game!' if home_score == away_score else f'{home_code if home_score > away_score else away_code} leads by {abs(home_score - away_score)}'}"
                ),
            })

        # ── Periodic update when nothing dramatic happened ─────────────────────
        else:
            events.append({
                "type": "game_update",
                "game": label,
                "game_id": game_id,
                "quarter": period,
                "time_remaining": clock,
                "score": score,
                "event": f"{label} — live update",
                "context": (
                    f"Current score: {away_code} {away_score} — {home_code} {home_score}. "
                    f"Q{period} | {status}"
                ),
            })

    return events


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
    prev_by_id: dict[str, dict] = {}
    warned_no_games = False

    print(f"  Mode: LIVE  |  Polling every {interval}s")

    while True:
        try:
            games = await fetch_live_games()
        except Exception as e:
            print(f"  ⚠️  Scoreboard fetch error: {e}. Retrying in {interval}s…")
            await asyncio.sleep(interval)
            continue

        if not games:
            if not warned_no_games:
                msg = "No live NBA games right now. Booth will activate automatically when games start."
                print(f"\n  ⏸  {msg}")
                if not cli_only:
                    await manager.broadcast({"type": "status", "message": msg})
                warned_no_games = True
            await asyncio.sleep(interval)
            continue

        warned_no_games = False

        # Broadcast current game list so the dashboard can render the selector
        if not cli_only:
            await manager.broadcast({"type": "games", "data": _games_payload(games)})

        events = detect_events(prev_by_id, games)

        # Update snapshot
        prev_by_id = {g["gameId"]: g for g in games}

        for event in events:
            await _process_event(event, cli_only)

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

    print("🏀 Sports Booth starting…")
    print(f"   Model:    {os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')}")
    print(f"   Interval: {args.interval}s")
    if args.demo:
        print("   ⚠️  Demo mode — using hardcoded Lakers vs Celtics events")

    asyncio.run(_run(args.interval, args.cli, args.demo, args.port))


if __name__ == "__main__":
    main()
