"""FastAPI app and WebSocket fan-out for the dashboard."""
import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from booth import config, metrics
from booth.health import runtime
from booth.history import history
from booth.security import COOKIE_NAME, SECURITY_HEADERS, Auth


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
app.state.auth = Auth(config.get().auth_token, config.get().allowed_origins)
_DASHBOARD = Path(__file__).parent.parent / "static" / "index.html"

_UNAUTHORIZED_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Sports Booth</title>"
    "<body style='font:16px system-ui;background:#080b12;color:#e2e8f0;padding:3rem'>"
    "<h1>Sports Booth</h1><p>This booth needs an access token. Open "
    "<code>/?token=&lt;BOOTH_AUTH_TOKEN&gt;</code> once; the browser remembers it.</p>")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def require_auth(request: Request) -> None:
    if not request.app.state.auth.is_authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    auth: Auth = request.app.state.auth
    candidate = request.query_params.get("token")
    if candidate is not None:
        if not auth.token_matches(candidate):
            return HTMLResponse(_UNAUTHORIZED_PAGE, status_code=401)
        # Trade the token for a session cookie and drop it from the URL
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, auth.session_value(), httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=30 * 24 * 3600, path="/")
        return response
    if not auth.is_authorized(request):
        return HTMLResponse(_UNAUTHORIZED_PAGE, status_code=401)
    return HTMLResponse(_DASHBOARD.read_text())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    auth: Auth = ws.app.state.auth
    if not (auth.origin_allowed(ws) and auth.is_authorized(ws)):
        # Accept-then-close so the browser sees close code 1008 (policy violation) and the
        # dashboard can tell "unauthorized" from "server down" and stop retrying.
        await ws.accept()
        await ws.close(code=1008, reason="unauthorized")
        return
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness only (no details), unauthenticated so container/orchestrator probes work."""
    return {"status": "ok"}


def _refresh_gauges() -> dict:
    """Gauges that are cheapest to compute at scrape time. Returns the budget snapshot."""
    from booth.pipeline import budget  # local import: pipeline imports this module
    snap = budget.snapshot()
    metrics.registry.set("websocket_clients", manager.count)
    metrics.registry.set("budget_spent_usd", snap["spent_last_hour_usd"])
    metrics.registry.set("budget_cap_usd", snap["cap_usd_per_hour"] or 0)
    return snap


@app.get("/health", dependencies=[Depends(require_auth)])
async def health() -> dict:
    scheduler = getattr(app.state, "scheduler", None)
    budget_snap = _refresh_gauges()
    return runtime.report(clients=manager.count, scheduler=scheduler.snapshot() if scheduler else None,
                          budget=budget_snap)


@app.get("/metrics", dependencies=[Depends(require_auth)])
async def prometheus_metrics() -> Response:
    _refresh_gauges()
    return Response(metrics.registry.render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")
