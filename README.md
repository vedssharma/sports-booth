# Sports Booth

A live NBA commentary system powered by three specialized AI agents that watch games simultaneously and offer distinct perspectives — stats, history, and betting lines — in real time.

## The Booth

| Agent | Personality | Data source |
|---|---|---|
| **The Analyst** | Data-driven. Surfaces eFG%, plus/minus, lineup splits. | `nba_api` live boxscores |
| **The Historian** | Encyclopedic. Finds historical parallels and records. | ChromaDB vector database (RAG) |
| **The Degenerate** | Sharp bettor. Flags line overreactions and soft numbers. | The Odds API (mock data if no key) |

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
```

Open `http://localhost:8000` in your browser. If multiple games are live, use the pill selector at the top to switch between them — commentary history for each game is preserved in memory.

## How it works

```
NBA scoreboard (polled every N seconds)
        │
        ▼
   detect_events()          ← compares consecutive snapshots
        │
        ▼ game event (quarter change, scoring run, crunch time, …)
        │
        ├── run_analyst()   ─── nba_server.py   (MCP) ─── nba_api
        ├── run_historian() ─── rag_server.py   (MCP) ─── ChromaDB
        └── run_degenerate()─── betting_server.py (MCP) ─── The Odds API
                │
                ▼ asyncio.gather() — all three run in parallel
                │
                ▼
        WebSocket broadcast → dashboard
```

**Event detection** compares the previous scoreboard snapshot to the current one and emits events for:
- A new live game appearing for the first time
- A quarter or overtime period starting
- One team outscoring the other by 7+ points since the last poll (scoring run)
- A game within 5 points in Q4 or OT (crunch time)
- A periodic update when nothing dramatic was detected

**MCP servers** (`mcp_servers/`) are standalone Python scripts that run as stdio subprocesses. Each exposes a small set of tools via FastMCP. All three fall back to realistic mock data when their external API is unavailable, so the booth always produces commentary.

**RAG database** (`rag/chroma_db/`) is seeded with 18 historical NBA facts using `sentence-transformers` embeddings. The Historian agent queries it semantically — pass a game event description and it returns the most contextually relevant historical precedents.

## Project structure

```
main.py                   Entry point — FastAPI server, polling loop, event detection
booth/orchestrator.py     Runs the three agents in parallel via Claude Agent SDK
mcp_servers/
  nba_server.py           Live scores, advanced boxscores (nba_api)
  rag_server.py           Semantic search over historical games (ChromaDB)
  betting_server.py       Live odds and line movement (The Odds API)
rag/seed.py               Populates the ChromaDB historical database
static/index.html         Web dashboard — vanilla JS, no build step
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `ODDS_API_KEY` | No | [The Odds API](https://the-odds-api.com) key — free tier available. Without it, The Degenerate uses mock line data. |
| `CLAUDE_MODEL` | No | Defaults to `claude-sonnet-4-6` |
