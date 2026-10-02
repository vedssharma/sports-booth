"""FastAPI app and WebSocket fan-out for the dashboard."""
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse


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
    return {"status": "ok", "clients": manager.count,
            "scheduler": scheduler.snapshot() if scheduler else None}
