"""
Sports Booth orchestrator — runs three specialized agents in parallel and
returns combined commentary for each game event.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock
try:
    from claude_agent_sdk import CLINotFoundError, ProcessError as AgentProcessError
except ImportError:
    CLINotFoundError = AgentProcessError = Exception  # type: ignore[assignment,misc]

ROOT = Path(__file__).parent.parent
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Agent system prompts ──────────────────────────────────────────────────────

ANALYST_PROMPT = """\
You are The Analyst — a sharp, data-driven NBA commentator who lives inside live stats.

When given a game event:
1. Pull the live scoreboard, then dig into the boxscore for the relevant game.
2. Surface exactly 1-2 surprising statistical insights (eFG%, plus/minus, pace shifts,
   lineup differential, or bench-vs-starter splits).
3. Cite specific numbers. Keep your response to 2-3 punchy sentences.

Your voice: authoritative, precise, a little cold. Think Kirk Goldsberry meets Bill James.
Example: "The Lakers' eFG% drops from 54% to 41% when AD sits — and he's been on the bench
for 6 of the last 8 minutes. That's not a hot streak, that's a lineup problem."
"""

HISTORIAN_PROMPT = """\
You are The Historian — a deeply obsessive NBA historian with access to decades of game data.

When given a game event:
1. Search the historical database for the closest precedent or record being approached.
2. Lead with the most surprising or obscure historical fact you can find.
3. Keep your response to 2-3 sentences. Make listeners feel like they're witnessing history.

Your voice: enthusiastic, encyclopedic, slightly nerdy. Think Bill Simmons at his best.
Example: "This is only the third time in franchise history a Celtics rookie has posted
back-to-back 20-point games — the last was Paul Pierce in 1998."
"""

DEGENERATE_PROMPT = """\
You are The Degenerate — a sharp, caustic sports bettor who monitors live lines
for overreactions and soft numbers.

When given a game event:
1. Check the current odds and any line movement.
2. Flag whether the market is overreacting (or underreacting) to what just happened.
3. Keep your response to 2-3 sentences. Be colorful, specific about numbers, and opinionated.

Your voice: cynical, confident, slightly unhinged. Think a sharp who's been doing this 20 years.
Example: "The spread jumped 3 points after a single timeout? That's the public panicking.
Books already moved to -5.5 — fade the square money, the dog is the play."
"""

# ── MCP server config helpers ─────────────────────────────────────────────────

def _mcp_config(server_script: str) -> dict:
    script_path = str(ROOT / "mcp_servers" / server_script)
    return {"type": "stdio", "command": sys.executable, "args": [script_path]}


# ── Individual agent runners ──────────────────────────────────────────────────

async def _collect_text(aiter) -> str:
    """Drain an async message iterator and return all assistant text."""
    parts: list[str] = []
    async for msg in aiter:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts).strip()


async def run_analyst(event_text: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=ANALYST_PROMPT,
        mcp_servers={"nba": _mcp_config("nba_server.py")},
        model=MODEL,
        max_turns=10,  # needs scoreboard + boxscore = ≥2 tool calls
        permission_mode="bypassPermissions",
    )
    return await _collect_text(query(prompt=event_text, options=options))


async def run_historian(event_text: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=HISTORIAN_PROMPT,
        mcp_servers={"rag": _mcp_config("rag_server.py")},
        model=MODEL,
        max_turns=6,
        permission_mode="bypassPermissions",
    )
    return await _collect_text(query(prompt=event_text, options=options))


async def run_degenerate(event_text: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=DEGENERATE_PROMPT,
        mcp_servers={"betting": _mcp_config("betting_server.py")},
        model=MODEL,
        max_turns=6,
        permission_mode="bypassPermissions",
    )
    return await _collect_text(query(prompt=event_text, options=options))


# ── Public interface ──────────────────────────────────────────────────────────

async def run_booth_commentary(event: dict) -> dict:
    """
    Run all three booth agents in parallel for a game event.
    Returns a dict with keys: event, analyst, historian, degenerate.
    """
    event_text = (
        f"Game event:\n{json.dumps(event, indent=2)}\n\n"
        "Provide your expert commentary on this moment."
    )

    analyst, historian, degenerate = await asyncio.gather(
        run_analyst(event_text),
        run_historian(event_text),
        run_degenerate(event_text),
        return_exceptions=True,
    )

    def _safe(result, fallback: str) -> str:
        if isinstance(result, CLINotFoundError):
            return "[Error: Claude CLI not found — is claude installed and on PATH?]"
        if isinstance(result, AgentProcessError):
            return f"[Agent process error: {result}]"
        if isinstance(result, Exception):
            return f"[Error: {result}]"
        return result or fallback

    return {
        "event": event,
        "analyst": _safe(analyst, "Stats analysis unavailable."),
        "historian": _safe(historian, "Historical context unavailable."),
        "degenerate": _safe(degenerate, "Line data unavailable."),
    }
