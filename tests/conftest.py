import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(scope="session")
def chromium_ok():
    """Skip browser tests when Playwright's Chromium isn't installed."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium not available: {exc}")
    return True


@pytest.fixture(scope="session")
def mock_site():
    from mock_server import serve

    server, base = serve()
    yield base
    server.shutdown()
