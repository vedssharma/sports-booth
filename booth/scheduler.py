"""
Per-game event scheduler.

Generating commentary takes tens of seconds (three agent runs). Running events inline in the
poll loop made commentary lag further behind the game with every extra live game. Instead:

  - the poll loop only calls `submit()` (never blocks)
  - each game has its own worker, so events for one game stay in order
  - a global semaphore caps how many events run agents at once (cost / CPU / rate limits)
  - when a game falls behind, low-value events are dropped or superseded instead of queued up
"""
import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

# Higher = more important. Priority-3 events are never dropped or expired.
PRIORITY = {"game_final": 3, "quarter_start": 2, "scoring_run": 2, "close_game": 2, "game_update": 1}
# A fresher event of the same type makes an older pending one redundant
SUPERSEDABLE = {"scoring_run", "close_game", "game_update"}

MAX_CONCURRENT = 2
MAX_PENDING_PER_GAME = 2
STALE_AFTER_S = 120


def priority(event: dict) -> int:
    return PRIORITY.get(event.get("type", ""), 2)


class EventScheduler:
    def __init__(
        self,
        handler: Callable[[dict], Awaitable[None]],
        max_concurrent: int = MAX_CONCURRENT,
        max_pending: int = MAX_PENDING_PER_GAME,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler = handler
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_pending = max_pending
        self._stale_after = stale_after_s
        self._clock = clock
        self._pending: dict[str, deque[tuple[float, dict]]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self.stats = {"submitted": 0, "processed": 0, "failed": 0,
                      "dropped_busy": 0, "dropped_superseded": 0,
                      "dropped_overflow": 0, "dropped_stale": 0}

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, event: dict) -> bool:
        """Queue an event without blocking. Returns False if it was dropped on arrival."""
        self.stats["submitted"] += 1
        gid = event.get("game_id", "")
        pending = self._pending.setdefault(gid, deque())

        # A routine update is pointless while the game is already busy
        if event.get("type") == "game_update" and (pending or gid in self._workers):
            self.stats["dropped_busy"] += 1
            return False

        if event.get("type") in SUPERSEDABLE:
            kept = deque(item for item in pending if item[1].get("type") != event["type"])
            self.stats["dropped_superseded"] += len(pending) - len(kept)
            pending.clear()
            pending.extend(kept)

        pending.append((self._clock(), event))

        while len(pending) > self._max_pending:
            droppable = [item for item in pending if priority(item[1]) < 3]
            if not droppable:
                break
            # lowest priority first, oldest first among equals
            victim = min(droppable, key=lambda item: (priority(item[1]), item[0]))
            pending.remove(victim)
            self.stats["dropped_overflow"] += 1
            if victim[1] is event:
                return False

        if gid not in self._workers:
            self._workers[gid] = asyncio.create_task(self._work(gid))
        return True

    async def join(self) -> None:
        """Wait until every queued event has been handled (used by tests and shutdown)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)

    def snapshot(self) -> dict:
        return {**self.stats, "pending": sum(len(p) for p in self._pending.values()),
                "active_games": len(self._workers)}

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _work(self, gid: str) -> None:
        pending = self._pending[gid]
        try:
            while pending:
                queued_at, event = pending.popleft()
                if priority(event) < 3 and self._clock() - queued_at > self._stale_after:
                    self.stats["dropped_stale"] += 1
                    print(f"  ⏭  Dropped stale event: {event.get('event', event.get('type'))}")
                    continue
                async with self._sem:
                    try:
                        await self._handler(event)
                        self.stats["processed"] += 1
                    except Exception as e:  # never let one bad event kill the game's worker
                        self.stats["failed"] += 1
                        print(f"  ⚠️  Event handler failed: {e}")
        finally:
            self._workers.pop(gid, None)
            if not pending:
                self._pending.pop(gid, None)
