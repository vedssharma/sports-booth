import os

# Tests must never touch the real history database
os.environ.setdefault("BOOTH_HISTORY_DB", ":memory:")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_observability():
    """Metrics and runtime health are process-wide; start every test from a clean slate."""
    from booth import health, metrics

    metrics.registry.reset()
    health.runtime.__init__()
    yield
