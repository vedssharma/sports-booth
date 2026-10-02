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

# Spawn MCP servers per agent run instead of keeping them running
uv run python main.py --stdio-mcp

# Tests / lint
uv run pytest
uv run ruff check --select F .
```

## Architecture

**Three-agent commentary system.** Game events trigger up to three parallel `query()` calls via the Claude Agent SDK — one per persona. Results are stored in SQLite and broadcast over WebSocket.

```
main.py                    args; starts MCP servers (booth/mcp_host.py) then web server + loop
  booth/live.py            poll scoreboard → EventDetector → EventScheduler.submit()
  booth/scheduler.py       per-game workers, concurrency cap, drop/supersede/stale rules
  booth/pipeline.py        process_event: policy → orchestrator → history → broadcast
    booth/policy.py        model + personas per event; skips when over budget (booth/budget.py)
    booth/orchestrator.py  asyncio.gather() of the requested agents
      analyst    → mcp_servers/nba_server.py     (nba_api live endpoints)
      historian  → mcp_servers/rag_server.py     (ChromaDB RAG)
      degenerate → mcp_servers/betting_server.py (The Odds API; booth/odds.py snapshots)
  booth/server.py          FastAPI + WebSocket; booth/history.py SQLite replay store
```

**MCP servers** are FastMCP scripts. `McpHost` starts each once as a local streamable-HTTP server and the orchestrator points agents at them (`use_http_servers`); if startup fails (or `--stdio-mcp`), `_mcp_config` falls back to a fresh stdio subprocess per run. Tools are registered with `@offload(mcp)` (runs the sync function in a thread; returns it unchanged so tests can call it directly). In live mode a failed or unconfigured data source returns an explicit "unavailable" result (never fabricated data); mock data is served only in `--demo` mode via `BOOTH_MOCK_DATA=1`. Servers import `booth.*`, so import `_common` first (it adds the repo root to `sys.path`).

**Booth continuity.** `process_event` loads the game's last 3 stored moments (`history.recent_for_game`) and `orchestrator.build_prompt` shows them to every agent (chart blocks stripped, lines clipped, labelled context-only) so personas build on or push back against each other instead of repeating points. Per-game events run serially in the scheduler, so earlier commentary is always stored before the next event starts.

**Cost controls.** `CommentaryPolicy` maps event type → (model, personas): big moments = all personas on `CLAUDE_MODEL`; quarter starts/joins = all on `CLAUDE_MODEL_FAST`; routine `game_update` = one rotating persona on the fast model. `BudgetGuard` sums `ResultMessage.total_cost_usd` over a rolling hour vs `BOOTH_BUDGET_USD_PER_HOUR` (default 5): ≥75% "saver", ≥100% "exhausted" (finals only). Unknown event types (e.g. demo milestones) count as big. Demo mode bypasses the scheduler and plays events in order.

**Live event detection** (`booth/events.py: EventDetector`): stateful, compares scoreboard snapshots and emits events for quarter changes, scoring runs (≥7-point net swing within a rolling 3-minute window), lead changes (real flips only, after the opening minutes, rate-limited; `comeback` when the new leader had trailed by ≥10), crunch time (Q4/OT within 5, rate-limited), final scores (once per game), and a routine update only after a quiet stretch. A newly seen live game fires one join event. `booth/players.py: PlayerWatcher` adds player milestones (20/30/40/50/60 pts, 5+ threes, double/triple-double) and foul trouble from one live box score per game per poll; the first box score is a silent baseline and output is capped per poll. Run `uv run pytest` for the unit tests.

**WebSocket protocol** — message types the server sends:
- `snapshot` — sent once on connect: `{games, status, history}`. Authoritative; the client replaces local state with it (so reconnects don't duplicate cards)
- `games` — list of live game summaries; triggers selector re-render
- `event` — a detected game moment; `agents` lists which personas will respond (only those show "thinking")
- `commentary` — text per persona that ran (absent personas are omitted, not empty); includes `event`, `model`, `cost_usd`
- `status` — informational string (e.g. "no live games", budget notices)

**Dashboard** (`static/index.html`): pure vanilla JS, no build step. Stores commentary cards in memory keyed by `gameId`, so switching games is instant; history survives reloads via the `snapshot` message. Auto-selects the first game on arrival.

**RAG database** lives at `rag/chroma_db/` (gitignored). Re-seed any time with `uv run python rag/seed.py` (idempotent; `--rebuild` drops first, `--fetch-leaders` adds nba_api all-time leaderboards). Facts come from `rag/seed.py`'s curated list plus `rag/data/*.jsonl`, validated by `rag/facts.py` before anything is written. The RAG server's tools resolve player/team wording to stored metadata (`rag/filters.py`) and pass Chroma `where` filters; unmatched filters are dropped with a note. Tests use an in-process Chroma with a fake hashing embedder (no model download).

## Environment

```
ANTHROPIC_API_KEY   required
ODDS_API_KEY        needed for live betting data (demo mode uses mock odds)
CLAUDE_MODEL        optional — big moments; defaults to claude-sonnet-4-6
CLAUDE_MODEL_FAST   optional — routine events; defaults to claude-haiku-4-5-20251001
BOOTH_BUDGET_USD_PER_HOUR  optional — default 5, 0 = unlimited
BOOTH_MAX_USD_PER_AGENT    optional — default 0.50
```

Copy `.env.example` → `.env`.

## Key constraints

- `permission_mode="bypassPermissions"` is intentional — MCP servers only make outbound read-only API calls, never touch the filesystem.
- The Analyst agent uses `max_turns=10` (vs 6 for others) because it may chain several tool calls (boxscore, recent plays, lineup split).
- `rag/seed.py` uses `collection.get()["ids"]` (not `["metadatas"]`) to check for existing records — ChromaDB stores IDs and metadata separately.
