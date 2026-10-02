"""Shared helpers for the MCP servers (each server runs as a standalone script)."""
import json
import os


def mock_enabled() -> bool:
    """Mock/demo data is only served when explicitly requested (demo mode)."""
    return os.getenv("BOOTH_MOCK_DATA") == "1"


def unavailable(source: str, reason: str) -> str:
    """Tool result for when real data can't be fetched. Never fabricates numbers."""
    return json.dumps({
        "error": f"{source} data unavailable: {reason}",
        "instruction": (
            "Do not invent or estimate numbers. Tell the listener this data is "
            "unavailable right now and comment only on what you can verify. "
            "Omit any [CHART] block."
        ),
    }, indent=2)
