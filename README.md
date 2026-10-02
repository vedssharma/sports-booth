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
```

Open `http://localhost:8000` in your browser. If multiple games are live, use the pill selector at the top to switch between them — commentary history is stored in `data/history.db`, so it survives restarts and is replayed to anyone who opens or reloads the dashboard mid-game.

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

**Cost controls.** Big moments (runs, crunch time, finals) get all three personas on `CLAUDE_MODEL`. Quarter starts and joins use the cheaper `CLAUDE_MODEL_FAST`. A routine update runs just one persona, rotating per game. Real spend is tracked against `BOOTH_BUDGET_USD_PER_HOUR` (default **$5**): at 75% the booth drops routine updates and uses the cheap model everywhere; at 100% only final scores get commentary until spend rolls out of the hour window. Check `/health` for scheduler and budget state.

**MCP servers** (`mcp_servers/`) are FastMCP scripts. On startup `main.py` launches each once as a long-lived local HTTP server (so the Historian's embedding model loads once, not per event) and falls back to per-run stdio subprocesses if that fails or with `--stdio-mcp`. Tools run in worker threads, NBA calls are cached for 10s, and Odds API calls are rate-limited to one per minute to protect the monthly quota. In live mode, a failed or unconfigured data source returns an explicit "unavailable" result and the agents say so rather than inventing numbers. Mock data is served only in `--demo` mode.

**RAG database** (`rag/chroma_db/`) is seeded with 18 historical NBA facts using `sentence-transformers` embeddings. The Historian agent queries it semantically — pass a game event description and it returns the most contextually relevant historical precedents.

## Project structure

```
main.py                   Entry point — args, MCP host + server startup
booth/
  events.py, players.py   EventDetector (scoreboard moments) and PlayerWatcher (box-score moments)
  scheduler.py            EventScheduler — per-game queues, concurrency cap, drop rules
  policy.py, budget.py    Model/persona selection and rolling spend guard
  orchestrator.py         Runs the agents in parallel via Claude Agent SDK
  pipeline.py             Event → policy → agents → history → broadcast
  live.py, demo.py        Live polling loop / scripted demo game
  server.py, history.py   FastAPI + WebSocket (snapshot on connect), SQLite history
  mcp_host.py             Starts the MCP servers once as local HTTP processes
  sources.py, odds.py     Scoreboard access; Odds API client + snapshot store
mcp_servers/
  nba_server.py           Live scores, live boxscores, play-by-play (nba_api)
  rag_server.py           Semantic search over historical games (ChromaDB)
  betting_server.py       Consensus odds + real line movement (The Odds API)
rag/seed.py               Populates the ChromaDB historical database
static/index.html         Web dashboard — vanilla JS, no build step
tests/                    pytest suite (`uv run pytest`)
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `ODDS_API_KEY` | No | [The Odds API](https://the-odds-api.com) key — free tier available. Without it (outside demo mode) The Degenerate reports that line data is unavailable. |
| `CLAUDE_MODEL` | No | Model for big moments. Defaults to `claude-sonnet-4-6` |
| `CLAUDE_MODEL_FAST` | No | Model for routine events. Defaults to `claude-haiku-4-5-20251001` |
| `BOOTH_BUDGET_USD_PER_HOUR` | No | Rolling hourly spend cap in USD. Default `5`; `0` disables |
| `BOOTH_MAX_USD_PER_AGENT` | No | Cap for a single agent run in USD. Default `0.50` |
