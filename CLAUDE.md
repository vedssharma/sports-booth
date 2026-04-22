# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Seed the historical RAG database (required before first run)
uv run python rag/seed.py

# Run with live NBA data + web dashboard (http://localhost:8000)
uv run python main.py

# Run with hardcoded demo events (no live games required)
uv run python main.py --demo

# CLI-only mode (no web server)
uv run python main.py --cli

# Adjust poll interval (default: 45s)
uv run python main.py --interval 30
```

## Architecture

**Three-agent commentary system.** Each NBA game event triggers three parallel `query()` calls via the Claude Agent SDK — one per agent persona. Results are broadcast over WebSocket to the dashboard.

```
main.py                      FastAPI server + live polling loop
  └── booth/orchestrator.py  asyncio.gather() of three parallel query() calls
        ├── run_analyst()     → mcp_servers/nba_server.py    (nba_api live stats)
        ├── run_historian()   → mcp_servers/rag_server.py    (ChromaDB RAG)
        └── run_degenerate()  → mcp_servers/betting_server.py (The Odds API)
```

**MCP servers** run as stdio subprocesses (via `sys.executable`). Each is a standalone FastMCP script that the agent spawns on demand. They all gracefully fall back to mock data when external APIs are unavailable.

**Live event detection** (`main.py: detect_events()`): compares consecutive scoreboard snapshots and emits events for quarter changes, scoring runs (≥7-point swing), crunch time (Q4/OT within 5), or a generic update. First poll always fires one event per live game.

**WebSocket protocol** — message types the server sends:
- `games` — list of live game summaries; triggers selector re-render
- `event` — a detected game moment; triggers "thinking" indicators in the feeds
- `commentary` — the three agents' text; keyed by `event.game_id`
- `status` — informational string (e.g. "no live games")

**Dashboard** (`static/index.html`): pure vanilla JS, no build step. Stores all commentary cards in memory keyed by `gameId`, so switching games is instant. Auto-selects the first game on arrival.

**RAG database** lives at `rag/chroma_db/` (gitignored). Re-seed any time with `uv run python rag/seed.py`; it is idempotent.

## Environment

```
ANTHROPIC_API_KEY   required
ODDS_API_KEY        optional — betting agent uses realistic mock data without it
CLAUDE_MODEL        optional — defaults to claude-sonnet-4-6
```

Copy `.env.example` → `.env`.

## Key constraints

- `permission_mode="bypassPermissions"` is intentional — MCP servers only make outbound read-only API calls, never touch the filesystem.
- The Analyst agent uses `max_turns=10` (vs 6 for others) because it makes at least two sequential tool calls: scoreboard then boxscore.
- `rag/seed.py` uses `collection.get()["ids"]` (not `["metadatas"]`) to check for existing records — ChromaDB stores IDs and metadata separately.
