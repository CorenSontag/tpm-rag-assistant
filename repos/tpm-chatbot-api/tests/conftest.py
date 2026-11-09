"""Shared fixtures.

The rate limiter and the daily counter are module-level globals in app/api.py
(_hits, _day, _last_sweep). They survive between tests, so without a reset the
suite's result depends on the order pytest happened to run it in - the exact
class of flake that gets a real failure dismissed as "just the tests again".
"""

import time

import pytest
from fastapi.testclient import TestClient

from tests._bootstrap import api_mod

# What a mocked turn returns. Shape copied from run_chat's real return value;
# if that contract changes, ChatResponse construction fails here first.
MOCK_REPLY = {
    "reply": "mocked reply",
    "citations": [],
    "searched": False,
    "escalated": False,
    "language": "en",
}


@pytest.fixture(autouse=True)
def reset_limiter_state():
    """Give every test a clean limiter, before and after."""
    def clear():
        api_mod._hits.clear()
        api_mod._day["stamp"] = time.strftime("%Y-%m-%d")
        api_mod._day["turns"] = 0
        api_mod._last_sweep = time.time()

    clear()
    yield
    clear()


@pytest.fixture
def client():
    return TestClient(api_mod.app)


@pytest.fixture
def no_model(monkeypatch):
    """Replace the agent and record whether anything reached it.

    api.py does `from app.agent import run_chat`, so the name that the
    endpoint actually calls is api_mod.run_chat. Patching app.agent.run_chat
    instead would leave the endpoint holding its original reference and the
    test would quietly spend money.
    """
    calls = []

    def fake_run_chat(messages, session_id="anon"):
        calls.append({"messages": messages, "session_id": session_id})
        return dict(MOCK_REPLY)

    monkeypatch.setattr(api_mod, "run_chat", fake_run_chat)
    return calls
