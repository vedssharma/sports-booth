import asyncio
import io
import json
import time

import pytest

import main
from booth import log


def test_returns_as_soon_as_the_server_stops_even_if_the_loop_would_sleep_for_an_hour():
    """Regression: gather(serve(), loop()) waited for the loop, so `docker stop` hit its kill timeout."""
    state = {"loop_cancelled": False}

    async def serve():
        await asyncio.sleep(0.05)          # uvicorn returns from serve() on SIGTERM

    async def loop():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["loop_cancelled"] = True
            raise

    start = time.monotonic()
    asyncio.run(main.serve_until_stopped(serve(), loop()))
    assert time.monotonic() - start < 2 and state["loop_cancelled"]


def test_loop_finishing_on_its_own_does_not_stop_the_server():
    """The demo loop ends after its last event; the dashboard must stay up."""
    order = []

    async def serve():
        await asyncio.sleep(0.2)
        order.append("server stopped")

    async def loop():
        order.append("loop finished")      # returns immediately

    asyncio.run(main.serve_until_stopped(serve(), loop()))
    assert order == ["loop finished", "server stopped"]


def test_a_crashing_loop_is_logged_and_does_not_take_the_server_down():
    stream = io.StringIO()
    log.setup_logging("INFO", "json", stream)
    served = []

    async def serve():
        await asyncio.sleep(0.2)
        served.append(True)

    async def loop():
        raise RuntimeError("scoreboard exploded")

    asyncio.run(main.serve_until_stopped(serve(), loop()))
    assert served == [True]
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(r["msg"] == "background loop crashed" and "scoreboard exploded" in r["exc"] for r in rows)


def test_a_failing_server_still_cancels_the_loop_and_propagates():
    state = {"cancelled": False}

    async def serve():
        await asyncio.sleep(0.05)          # let the loop start running first
        raise OSError("address already in use")

    async def loop():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    with pytest.raises(OSError):
        asyncio.run(main.serve_until_stopped(serve(), loop()))
    assert state["cancelled"]
