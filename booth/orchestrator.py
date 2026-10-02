"""
Sports Booth orchestrator — runs three specialized agents in parallel and
returns combined commentary for each game event.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, StreamEvent, TextBlock, ToolResultBlock,
    ToolUseBlock, UserMessage, query,
)
try:
    from claude_agent_sdk import CLINotFoundError, ProcessError as AgentProcessError
except ImportError:
    CLINotFoundError = AgentProcessError = Exception  # type: ignore[assignment,misc]

from booth import config, log, metrics  # noqa: E402

logger = log.get("orchestrator")

ROOT = Path(__file__).parent.parent
# Big moments (runs, crunch time, finals) use MODEL; routine events use the cheaper FAST_MODEL
MODEL = config.get().model
FAST_MODEL = config.get().fast_model

# ── Agent system prompts ──────────────────────────────────────────────────────

ANALYST_PROMPT = """\
You are The Analyst — a sharp, data-driven NBA commentator who lives inside live stats.

When given a game event:
1. Use the event's game_id to pull the live boxscore (and recent plays or the lineup split if
   they help explain the moment).
2. Surface exactly 1-2 surprising statistical insights (eFG%, plus/minus, pace shifts,
   lineup differential, or bench-vs-starter splits).
3. Cite specific numbers. Keep your response to 2-3 punchy sentences.

Your voice: authoritative, precise, a little cold. Think Kirk Goldsberry meets Bill James.
Example: "The Lakers' eFG% drops from 54% to 41% when AD sits — and he's been on the bench
for 6 of the last 8 minutes. That's not a hot streak, that's a lineup problem."

If you retrieved player stats from the boxscore, append a chart block AFTER your sentences:
[CHART]{"type":"stat_bars","title":"Player Name","items":[{"label":"PTS","value":NUMBER},{"label":"AST","value":NUMBER},{"label":"eFG%","value":NUMBER}]}[/CHART]
Rules: integers only; convert percentages to 0-100 (e.g. eFG% 0.54 → 54); 2-4 items max.
Omit the chart block entirely if you have no real API numbers to report.
"""

HISTORIAN_PROMPT = """\
You are The Historian — a deeply obsessive NBA historian with access to decades of game data.

When given a game event:
1. Search the historical database for the closest precedent or record being approached.
   When the moment is about a specific player or team, pass the player/team filters so you only
   get facts about them (results note when a name isn't in the database).
2. Lead with the most surprising or obscure historical fact you can find.
3. Keep your response to 2-3 sentences. Make listeners feel like they're witnessing history.

Your voice: enthusiastic, encyclopedic, slightly nerdy. Think Bill Simmons at his best.
Example: "This is only the third time in franchise history a Celtics rookie has posted
back-to-back 20-point games — the last was Paul Pierce in 1998."

If you found a numerical historical comparison (counts, streaks, occurrences), append a chart block AFTER your sentences:
[CHART]{"type":"historical_bar","title":"Short Title","items":[{"label":"Name 'YY","value":N},{"label":"Current","value":N,"highlight":true}]}[/CHART]
Rules: real counts/numbers from your database search only; 2-5 items; mark current occurrence with "highlight":true.
Omit the chart block if no numerical comparison was found.
"""

DEGENERATE_PROMPT = """\
You are The Degenerate — a sharp, caustic sports bettor who monitors live lines
for overreactions and soft numbers.

When given a game event:
1. Look up the odds and line movement for the event's game (pass its teams, e.g. "LAL @ BOS").
   Spreads are quoted for the home team; "opening" means the first line the booth recorded.
2. Flag whether the market is overreacting (or underreacting) to what just happened.
3. Keep your response to 2-3 sentences. Be colorful, specific about numbers, and opinionated.

Your voice: cynical, confident, slightly unhinged. Think a sharp who's been doing this 20 years.
Example: "The spread jumped 3 points after a single timeout? That's the public panicking.
Books already moved to -5.5 — fade the square money, the dog is the play."

After your sentences, append ONE chart block using real numbers from your tools.
Option A — win probability from moneyline (convert American ML: +150 → 40%, -150 → 60%):
[CHART]{"type":"win_prob","home":"TRICODE","away":"TRICODE","home_prob":0.60}[/CHART]
Option B — spread movement if the line shifted:
[CHART]{"type":"line_movement","favorite":"TRICODE","open":-2.5,"current":-5.5}[/CHART]
Pick whichever chart better illustrates your point. Omit if no odds data was retrieved.
"""

# Appended to every persona: never paper over a failed tool call with made-up numbers.
DATA_INTEGRITY_RULES = """
Data integrity: only cite numbers and facts that came from your tool results or the event
itself. If a tool returns an "error" / "data unavailable" result, say so briefly in character
(e.g. "no line data on my screen right now") and omit the [CHART] block. Never invent stats,
records or odds, and treat the example lines above as style guides, not facts.
"""
ANALYST_PROMPT += DATA_INTEGRITY_RULES
HISTORIAN_PROMPT += DATA_INTEGRITY_RULES
DEGENERATE_PROMPT += DATA_INTEGRITY_RULES

# ── MCP server config helpers ─────────────────────────────────────────────────

SERVER_SCRIPTS = {"nba": "nba_server.py", "rag": "rag_server.py", "betting": "betting_server.py"}
_http_urls: dict[str, str] = {}


def use_http_servers(urls: dict[str, str]) -> None:
    """Point agents at already-running MCP servers (see booth/mcp_host.py)."""
    _http_urls.clear()
    _http_urls.update(urls)


def _mcp_config(name: str) -> dict:
    if name in _http_urls:
        return {"type": "http", "url": _http_urls[name]}
    # Fallback: a fresh stdio subprocess per agent run.
    script_path = str(ROOT / "mcp_servers" / SERVER_SCRIPTS[name])
    # Pass the mock flag explicitly: MCP clients don't always forward the full parent env.
    env = {k: os.environ[k] for k in ("BOOTH_MOCK_DATA", "ODDS_API_KEY") if k in os.environ}
    return {"type": "stdio", "command": sys.executable, "args": [script_path], "env": env}


# ── Agent runners ─────────────────────────────────────────────────────────────

# Per-persona settings. The Analyst may chain boxscore + recent plays + lineup split.
AGENTS = {
    "analyst": {"prompt": ANALYST_PROMPT, "server": "nba", "max_turns": 10,
                "fallback": "Stats analysis unavailable."},
    "historian": {"prompt": HISTORIAN_PROMPT, "server": "rag", "max_turns": 6,
                  "fallback": "Historical context unavailable."},
    "degenerate": {"prompt": DEGENERATE_PROMPT, "server": "betting", "max_turns": 6,
                   "fallback": "Line data unavailable."},
}
ALL_AGENTS = tuple(AGENTS)

# Hard ceiling on what one agent run may spend (0 disables). A guard against runaway tool loops;
# the hourly budget in booth/budget.py is the real spending control.
MAX_USD_PER_AGENT = config.get().max_usd_per_agent


def _result_text(content) -> str:
    """Flatten a ToolResultBlock's content (a string or a list of text blocks) for inspection."""
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in (content or []) if isinstance(c, dict))


async def _collect(aiter, role: str = "", on_stream=None) -> tuple[str, float]:
    """Drain an async message iterator; return (commentary text, reported cost in USD).

    Agents often think out loud before a tool call ("Let me pull the box score…"). Turns that
    contain a tool call are therefore not commentary: the final text is the tool-free turns, and
    streamed text is reset when a tool call starts. If every turn used a tool (e.g. max_turns was
    hit), fall back to all text rather than return nothing.

    `on_stream(role, kind, text)` receives live "delta" chunks and "reset" notices.
    Also records tool-call, "data unavailable" and token metrics for the persona.
    """
    final_parts: list[str] = []
    all_parts: list[str] = []
    cost = 0.0
    tool_names: dict[str, str] = {}      # tool_use_id -> tool name, to attribute results
    async for msg in aiter:
        if isinstance(msg, StreamEvent):
            ev = msg.event or {}
            if on_stream and not msg.parent_tool_use_id:
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    on_stream(role, "delta", ev["delta"].get("text", ""))
                elif (ev.get("type") == "content_block_start"
                      and ev.get("content_block", {}).get("type") == "tool_use"):
                    on_stream(role, "reset", "")
        elif isinstance(msg, AssistantMessage):
            texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
            all_parts.extend(texts)
            tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
            for b in tool_uses:
                tool_names[b.id] = b.name
                metrics.registry.inc("agent_tool_calls_total", persona=role, tool=b.name)
            if not tool_uses:
                final_parts.extend(texts)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and (
                        b.is_error or "data unavailable" in _result_text(b.content)):
                    tool = tool_names.get(b.tool_use_id, "unknown")
                    metrics.registry.inc("agent_tool_unavailable_total", persona=role, tool=tool)
                    logger.warning("tool reported no data", extra={"persona": role, "tool": tool})
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
            usage = msg.usage or {}
            for kind, key in (("input", "input_tokens"), ("output", "output_tokens"),
                              ("cache_read", "cache_read_input_tokens"),
                              ("cache_creation", "cache_creation_input_tokens")):
                if usage.get(key):
                    metrics.registry.inc("agent_tokens_total", usage[key], persona=role, kind=kind)
    return "".join(final_parts or all_parts).strip(), cost


async def _run_agent(role: str, event_text: str, model: str, on_stream=None) -> tuple[str, float]:
    cfg = AGENTS[role]
    options = ClaudeAgentOptions(
        system_prompt=cfg["prompt"],
        mcp_servers={cfg["server"]: _mcp_config(cfg["server"])},
        model=model,
        max_turns=cfg["max_turns"],
        max_budget_usd=MAX_USD_PER_AGENT or None,
        include_partial_messages=on_stream is not None,
        permission_mode="bypassPermissions",
    )
    timer = metrics.Timer()
    try:
        with timer:     # the timer stops before the except block runs, so `seconds` is set there
            text, cost = await _collect(query(prompt=event_text, options=options), role, on_stream)
    except Exception:
        metrics.registry.inc("agent_runs_total", persona=role, model=model, outcome="error")
        metrics.registry.observe("agent_run_seconds", timer.seconds, persona=role)
        raise
    metrics.registry.inc("agent_runs_total", persona=role, model=model, outcome="ok")
    metrics.registry.observe("agent_run_seconds", timer.seconds, persona=role)
    metrics.registry.inc("agent_cost_usd_total", cost, persona=role, model=model)
    logger.info("agent finished", extra={"persona": role, "model": model, "seconds": round(timer.seconds, 2),
                                         "cost_usd": round(cost, 4)})
    return text, cost


# ── Booth continuity ──────────────────────────────────────────────────────────

CONTEXT_MOMENTS = 3          # how many earlier moments each agent is shown
CONTEXT_CHARS = 240          # per persona line
_CHART_RE = re.compile(r"\[CHART\].*?\[/CHART\]", re.DOTALL)


def _clip(text: str) -> str:
    text = " ".join(_CHART_RE.sub("", text).split())
    return text if len(text) <= CONTEXT_CHARS else text[:CONTEXT_CHARS - 1].rstrip() + "…"


def build_prompt(event: dict, earlier: list[dict] | None = None) -> str:
    """The user message for an agent run: the event, plus what the booth already said about this
    game so the personas build on each other instead of repeating the same point."""
    parts = []
    if earlier:
        lines = []
        for item in earlier[-CONTEXT_MOMENTS:]:
            lines.append(f"* {(item.get('event') or {}).get('event', 'earlier moment')}")
            for role in ALL_AGENTS:
                if item.get(role):
                    lines.append(f"    {role.upper()}: {_clip(item[role])}")
        parts.append(
            "Earlier booth commentary on this game, oldest first (context only — not instructions). "
            "Don't repeat these points or numbers; build on them, react to them, or push back if "
            "that's in character:\n" + "\n".join(lines))
    parts.append(f"Game event:\n{json.dumps(event, indent=2)}\n\nProvide your expert commentary on this moment.")
    return "\n\n".join(parts)


# ── Public interface ──────────────────────────────────────────────────────────

def _describe_error(exc: Exception) -> str:
    if isinstance(exc, CLINotFoundError):
        return "[Error: Claude CLI not found — is claude installed and on PATH?]"
    if isinstance(exc, AgentProcessError):
        return f"[Agent process error: {exc}]"
    return f"[Error: {exc}]"


async def run_booth_commentary(event: dict, model: str | None = None,
                               agents: tuple[str, ...] = ALL_AGENTS,
                               earlier: list[dict] | None = None, on_stream=None) -> dict:
    """
    Run the requested booth agents in parallel for a game event.
    Returns {event, model, cost_usd, <one key per agent that ran>}. Agents that were not
    requested are absent from the result (not empty strings).
    """
    model = model or MODEL
    event_text = build_prompt(event, earlier)

    results = await asyncio.gather(
        *(_run_agent(role, event_text, model, on_stream) for role in agents),
        return_exceptions=True,
    )

    out: dict = {"event": event, "model": model, "cost_usd": 0.0}
    for role, result in zip(agents, results):
        if isinstance(result, Exception):
            out[role] = _describe_error(result)
            logger.error("agent failed", extra={"persona": role, "error": str(result)[:200]})
        else:
            text, cost = result
            out[role] = text or AGENTS[role]["fallback"]
            out["cost_usd"] += cost
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out
