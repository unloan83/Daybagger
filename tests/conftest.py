from __future__ import annotations

import urllib.request

import pytest


class UnmockedExternalNetworkAttempt(BaseException):
    """Fail the test immediately; production code cannot swallow this guard."""


@pytest.fixture(autouse=True)
def isolate_test_credentials_and_network(monkeypatch):
    """No test may discover real Telegram credentials or use real HTTP."""
    import trading_contracts.telemetry as telemetry

    monkeypatch.setattr(telemetry, "_load_env", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def blocked_urlopen(*args, **kwargs):
        raise UnmockedExternalNetworkAttempt(
            "tests must inject or mock every external HTTP transport"
        )

    monkeypatch.setattr(urllib.request, "urlopen", blocked_urlopen)
    yield
