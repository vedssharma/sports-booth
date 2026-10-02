"""Relays live agent text to dashboards without one WebSocket message per token."""
import asyncio

FLUSH_INTERVAL_S = 0.12


class StreamRelay:
    """
    Collects ("delta" | "reset") notices from running agents and broadcasts them in small
    batches. It also keeps the in-progress text per persona in `manager.streaming`, so a
    dashboard that connects mid-sentence gets the text so far in its snapshot.

    The agent loop and this class share the event loop, so the callback is synchronous and
    never blocks; a single flusher task does the awaiting.
    """

    def __init__(self, manager, game_id: str, interval: float | None = None) -> None:
        self._manager = manager
        self._gid = game_id
        self._interval = FLUSH_INTERVAL_S if interval is None else interval
        self._pending: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        manager.streaming[game_id] = {}

    def __call__(self, role: str, kind: str, text: str) -> None:
        slot = self._pending.setdefault(role, {"text": "", "reset": False})
        if kind == "reset":
            slot["text"], slot["reset"] = "", True
        else:
            slot["text"] += text
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._interval)
        await self._flush()

    async def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        live = self._manager.streaming.setdefault(self._gid, {})
        for role, slot in pending.items():
            live[role] = ("" if slot["reset"] else live.get(role, "")) + slot["text"]
            await self._manager.broadcast({"type": "delta", "game_id": self._gid, "role": role,
                                           "text": slot["text"], "reset": slot["reset"]})

    async def close(self) -> None:
        """Stop streaming for this event (the finished commentary replaces it)."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._pending.clear()
        self._manager.streaming.pop(self._gid, None)
