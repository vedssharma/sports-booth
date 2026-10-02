"""Tiny thread-safe TTL cache for expensive upstream calls (NBA feeds, Odds API quota)."""
import functools
import threading
import time


def ttl_cache(seconds: float, clock=time.monotonic):
    """Cache a function's results per positional-args key for `seconds`.
    Exceptions are never cached. `fn.cache_clear()` empties it."""
    def deco(fn):
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args):
            now = clock()
            with lock:
                hit = store.get(args)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args)
            with lock:
                store[args] = (clock(), value)
            return value

        wrapper.cache_clear = lambda: store.clear()
        return wrapper
    return deco
