# Sports Booth

A live NBA commentary system powered by three specialized AI agents that watch games simultaneously and offer distinct perspectives — stats, history, and betting lines — in real time.

## The Booth

| Agent | Personality | Data source |
|---|---|---|
| **The Analyst** | Data-driven. Surfaces eFG%, plus/minus, lineup splits. | `nba_api` live boxscores |
| **The Historian** | Encyclopedic. Finds historical parallels and records. | ChromaDB vector database (RAG) |
| **The Degenerate** | Sharp bettor. Flags line overreactions and soft numbers. | The Odds API (requires `ODDS_API_KEY`) |

Each game event triggers all three agents in parallel. Their commentary appears in three columns on the dashboard, updating live via WebSocket.

## Setup

**Requirements:** Python 3.12+, `uv`, a Claude API key from [console.anthropic.com](https://console.anthropic.com).

```bash
# 1. Install dependencies
uv sync

# 2. Configure environment
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY (required) and ODDS_API_KEY (optional)

# 3. Seed the historical database (one-time setup)
uv run python rag/seed.py
```

## Running

```bash
# Live mode — polls NBA scoreboard, web dashboard at http://localhost:8000
uv run python main.py

# Demo mode — cycles through a hardcoded Lakers vs Celtics game
uv run python main.py --demo

# CLI only — no web server, prints commentary to terminal
uv run python main.py --cli

# Adjust poll interval (default: 45 seconds)
uv run python main.py --interval 30

# Spawn MCP servers per agent run instead of keeping them running (slower; for debugging)
uv run python main.py --stdio-mcp

# Validate your environment, print the effective settings (secrets masked) and exit
uv run python main.py --check-config
```

The dashboard listens on `127.0.0.1:8000` only. To reach it from another machine, set `BOOTH_AUTH_TOKEN` (see [Security](#security)) and `--host 0.0.0.0`. Prefer containers? See [Docker](#docker).

Open `http://localhost:8000` in your browser. The dashboard has an event timeline for the selected game, a **Hide** button per persona, an optional **Voice** toggle (browser speech synthesis, a distinct voice per persona; off by default) and a stacked layout on phones. Voice and hidden-persona choices are remembered in `localStorage`. If multiple games are live, use the pill selector at the top to switch between them — commentary history is stored in `data/history.db`, so it survives restarts and is replayed to anyone who opens or reloads the dashboard mid-game.

## How it works

```
NBA scoreboard (polled every N seconds)
        │
        ▼
   EventDetector.detect()      rolling-window runs, cooldowns, final scores
        │ game event
        ▼
   EventScheduler.submit()     never blocks the poll loop; one worker per game,
        │                      global concurrency cap, drops stale/low-value events
        ▼
   CommentaryPolicy.decide()   which model + which personas (or skip, if over budget)
        │
        ▼ asyncio.gather() — requested agents run in parallel
        ├── analyst    ─── nba_server.py     ─── nba_api live endpoints
        ├── historian  ─── rag_server.py     ─── ChromaDB
        └── degenerate ─── betting_server.py ─── The Odds API
                │
                ▼
   HistoryStore (SQLite) + WebSocket broadcast → dashboard
```

**Event detection** (`booth/events.py`) emits events for:
- A new live game appearing for the first time
- A quarter or overtime period starting
- One team outscoring the other by 7+ points within a rolling 3 minutes (scoring run)
- The lead changing hands (called out as a comeback when the new leader trailed by 10+)
- A player milestone (20/30/40/50/60 points, 5+ threes, double- or triple-double) or foul trouble, from live box scores
- A game within 5 points in Q4 or OT (crunch time, rate-limited)
- A game ending (final score, exactly once)
- A routine update, only after a quiet stretch

**Scheduling.** Generating commentary takes tens of seconds, so events are queued per game instead of run inline. If a game falls behind, routine updates are dropped, repeated runs are superseded by the newest one, and stale events expire. Final scores are never dropped. At most 2 games generate commentary at once.

**Streaming.** Commentary appears word by word while each persona is still writing. Text typed before a tool call ("let me check the box score…") is discarded, in the stream and in the final card. A dashboard that connects mid-sentence gets the text so far.

**Booth continuity.** Each agent is shown the booth's last three moments on that game (clipped, charts stripped) so the personas react to each other and don't repeat the same point.

**Cost controls.** Big moments (runs, crunch time, finals) get all three personas on `CLAUDE_MODEL`. Quarter starts and joins use the cheaper `CLAUDE_MODEL_FAST`. A routine update runs just one persona, rotating per game. Real spend is tracked against `BOOTH_BUDGET_USD_PER_HOUR` (default **$5**): at 75% the booth drops routine updates and uses the cheap model everywhere; at 100% only final scores get commentary until spend rolls out of the hour window. Check `/health` for scheduler and budget state. Measured with real runs: a full three-persona big moment costs roughly $0.05–0.50 depending on model and prompt-cache warmth, so the default $5/hour budget covers somewhere between 10 and 100 big moments an hour. Raise `BOOTH_BUDGET_USD_PER_HOUR` for a busy slate.

**MCP servers** (`mcp_servers/`) are FastMCP scripts. On startup `main.py` launches each once as a long-lived local HTTP server (so the Historian's embedding model loads once, not per event) and falls back to per-run stdio subprocesses if that fails or with `--stdio-mcp`. Tools run in worker threads, NBA calls are cached for 10s, and Odds API calls are rate-limited to one per minute to protect the monthly quota. In live mode, a failed or unconfigured data source returns an explicit "unavailable" result and the agents say so rather than inventing numbers. Mock data is served only in `--demo` mode.

**RAG database** (`rag/chroma_db/`) holds historical NBA facts embedded with `sentence-transformers`. The Historian searches it semantically and can narrow results with exact metadata filters (player, team, category, year range) — names are resolved forgivingly ("LeBron", "LAL", "Lakers"), and a filter that matches nothing is dropped with a note rather than returning an empty answer. Grow it three ways:

```bash
uv run python rag/seed.py                  # curated facts + any rag/data/*.jsonl
uv run python rag/seed.py --fetch-leaders  # also all-time career leaderboards from nba_api (needs network)
uv run python rag/seed.py --rebuild        # drop and re-create the collection
```

Drop your own facts in `rag/data/*.jsonl`, one JSON object per line (`#` comment lines allowed). `id` and `text` are required and ids must be unique; everything else is metadata (`player`, `team`, `year` as an integer, `category`, …). The whole import is validated before the database is touched, with errors reported as `file:line`:

```json
{"id": "example_001", "text": "Jane Doe scored 50 points in a playoff game for the Example Hawks in 1999.", "player": "Jane Doe", "team": "Example Hawks", "year": 1999, "category": "playoff_performance"}
```


## Project structure

```
main.py                   Entry point — config validation, args, MCP host + server startup
Dockerfile, docker/       Container image + entrypoint (docker-compose.yml at the root)
booth/
  events.py, players.py   EventDetector (scoreboard moments) and PlayerWatcher (box-score moments)
  scheduler.py            EventScheduler — per-game queues, concurrency cap, drop rules
  policy.py, budget.py    Model/persona selection and rolling spend guard
  orchestrator.py         Runs the agents in parallel via Claude Agent SDK
  pipeline.py             Event → policy → agents → history → broadcast
  live.py, demo.py        Live polling loop / scripted demo game
  server.py, security.py  FastAPI + WebSocket (snapshot on connect); token auth, origin check, headers
  history.py              SQLite history store
  config.py               Every environment variable: defaults, parsing, validation
  log.py, metrics.py      Structured logging with event context; Prometheus-style metrics
  health.py               Runtime health state behind /health
  mcp_host.py             Starts the MCP servers once as local HTTP processes
  sources.py, odds.py     Scoreboard access; Odds API client + snapshot store
mcp_servers/
  nba_server.py           Live scores, live boxscores, play-by-play (nba_api)
  rag_server.py           Semantic search over historical games (ChromaDB)
  betting_server.py       Consensus odds + real line movement (The Odds API)
rag/seed.py, facts.py     Populate the ChromaDB database (validation, jsonl + nba_api importers)
rag/filters.py            Resolve player/team names to stored metadata and build Chroma filters
rag/data/*.jsonl          Optional extra facts to ingest (not shipped)
static/index.html         Web dashboard — vanilla JS, no build step
tests/                    pytest suite (`uv run pytest`)
```

## Security

- **Network exposure.** The server binds `127.0.0.1` by default. Binding any other address is refused unless `BOOTH_AUTH_TOKEN` is set (or you explicitly accept the risk with `BOOTH_ALLOW_INSECURE=1`, e.g. behind a reverse proxy that does its own auth).
- **Access token.** With `BOOTH_AUTH_TOKEN` set, the dashboard, WebSocket, `/health` and `/metrics` require it, as `Authorization: Bearer <token>` or a session cookie. Open `http://host:8000/?token=<token>` once in a browser: the token is exchanged for an HttpOnly, SameSite=Strict cookie and removed from the URL. Generate a token with `python -c 'import secrets; print(secrets.token_urlsafe(24))'`. `/healthz` stays open for liveness probes.
- **Cross-site WebSocket protection.** Browser WebSocket handshakes must come from the page's own origin, even with no token, so a random website cannot read a booth running on your laptop. Behind a reverse proxy that changes the hostname, list the public origin in `BOOTH_ALLOWED_ORIGINS`.
- **Agent least privilege.** Agents read third-party text (play descriptions, odds feeds), so they are locked down: no built-in tools (no shell, file or web access), only their own read-only MCP server, and anything else is denied rather than prompted. This also makes each run about 3× cheaper.
- Responses carry `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a Content-Security-Policy.

## Operations

**Logging.** `BOOTH_LOG_LEVEL` (`debug|info|warning|error`) and `BOOTH_LOG_FORMAT=text|json`. JSON mode writes one object per line to stderr. Every line produced while handling a game event carries the same `event_id` (plus `game_id` and `event_type`), so one `grep event_id=…` or `jq 'select(.event_id=="…")'` follows an event through scheduling, the budget decision, each agent run and the broadcast. The MCP server processes follow the same format.

**Health.** `GET /healthz` is unauthenticated liveness. `GET /health` (token required if set) returns `status` (`ok` or `degraded` with `reasons`), version, uptime, mode, scheduler and budget state, and a per-persona agent summary (runs, errors, latency, tool calls). It reports `degraded` after 3 consecutive failed scoreboard polls, when no poll has succeeded for 3× the poll interval, or when the hourly budget is exhausted.

**Metrics.** `GET /metrics` serves Prometheus text format: events submitted, dropped (by reason) and processed; queue wait and processing time; agent runs, latency, cost and token usage per persona; tool calls and "data unavailable" tool results; scoreboard poll outcomes; budget spend and cap; connected clients.

```bash
curl -H "Authorization: Bearer $BOOTH_AUTH_TOKEN" localhost:8000/metrics | grep booth_agent_run
```

**Configuration.** Every variable is validated at startup and all problems are reported at once (a bad `.env` is fixed in one pass); an untouched `your_..._here` placeholder from `.env.example` counts as unset. `--check-config` prints the effective values with secrets masked.

## Docker

```bash
cp .env.example .env            # set ANTHROPIC_API_KEY (and ODDS_API_KEY for live lines)
docker compose up --build       # dashboard on http://localhost:8000, published on loopback only
```

The image runs as a non-root user, keeps all state (history, odds snapshots, the RAG database, the downloaded embedding model) in the `/data` volume, seeds the RAG database on first start (set `BOOTH_SEED_ON_START=0` to skip), and has a `HEALTHCHECK` on `/healthz`. It validates configuration before doing anything slow and exits with code 2 and a readable message if something is wrong. The build installs **CPU-only PyTorch** by default (`--build-arg TORCH_BACKEND=pypi` installs exactly what `uv.lock` pins, CUDA wheels included, which is several GB larger).

Inside a container the app must listen on all interfaces, so `docker run` without a token is refused. `docker-compose.yml` publishes the port on `127.0.0.1` only and sets `BOOTH_ALLOW_INSECURE=1` for that reason; if you expose it on a network interface, remove that line and set `BOOTH_AUTH_TOKEN` in `.env`. Extra facts for the Historian can be mounted at `/app/rag/data/*.jsonl`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `ODDS_API_KEY` | No | [The Odds API](https://the-odds-api.com) key — free tier available. Without it (outside demo mode) The Degenerate reports that line data is unavailable. |
| `CLAUDE_MODEL` | No | Model for big moments. Defaults to `claude-sonnet-4-6` |
| `CLAUDE_MODEL_FAST` | No | Model for routine events. Defaults to `claude-haiku-4-5-20251001` |
| `BOOTH_BUDGET_USD_PER_HOUR` | No | Rolling hourly spend cap in USD. Default `5`; `0` disables |
| `BOOTH_MAX_USD_PER_AGENT` | No | Cap for a single agent run in USD. Default `0.50` |
| `BOOTH_HOST` / `BOOTH_PORT` | No | Interface and port to listen on. Default `127.0.0.1:8000`. Non-loopback hosts require `BOOTH_AUTH_TOKEN` |
| `BOOTH_AUTH_TOKEN` | No | Shared secret for the dashboard, WebSocket, `/health` and `/metrics` (16+ chars recommended) |
| `BOOTH_ALLOW_INSECURE` | No | `1` to listen on a network address without a token. Only if something else restricts access |
| `BOOTH_ALLOWED_ORIGINS` | No | Extra browser origins allowed to open the WebSocket (comma-separated, e.g. behind a reverse proxy) |
| `BOOTH_LOG_LEVEL` / `BOOTH_LOG_FORMAT` | No | `info` / `text` by default; `json` for one object per line |
| `BOOTH_HISTORY_DB` / `BOOTH_ODDS_DB` / `BOOTH_RAG_DB` | No | Storage locations (default `data/history.db`, `data/odds.db`, `rag/chroma_db`) |
| `BOOTH_ODDS_TTL` | No | Seconds between Odds API requests. Default `60` |
