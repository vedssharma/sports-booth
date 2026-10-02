"""FastAPI app and WebSocket fan-out for the dashboard."""
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from booth.history import history


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        # Latest state a late joiner needs; kept current by broadcast()
        self.last_games: list[dict] | None = None
        self.last_status: str | None = None
        # Text of commentary currently being generated: {game_id: {role: text so far}}
        self.streaming: dict[str, dict[str, str]] = {}

    def snapshot(self) -> dict:
        """Everything a freshly connected dashboard needs, as ONE message (no ordering races)."""
        return {"type": "snapshot", "games": self.last_games or [],
                "status": None if self.last_games else self.last_status,
                "history": history.recent(), "streaming": self.streaming}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        # No await between building the snapshot and registering the socket: anything
        # broadcast afterwards is not in the snapshot, and nothing is both.
        snapshot = json.dumps(self.snapshot())
        self._connections.append(ws)
        await ws.send_text(snapshot)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        if payload.get("type") == "games":
            self.last_games, self.last_status = payload["data"], None
        elif payload.get("type") == "status":
            self.last_games, self.last_status = None, payload["message"]
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


app = FastAPI(title="Sports Booth", version="0.1.0")
_DASHBOARD = Path(__file__).parent.parent / "static" / "index.html"


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
    scheduler = getattr(app.state, "scheduler", None)
    from booth.pipeline import budget  # local import: pipeline imports this module
    return {"status": "ok", "clients": manager.count,
            "scheduler": scheduler.snapshot() if scheduler else None,
            "budget": budget.snapshot()}
