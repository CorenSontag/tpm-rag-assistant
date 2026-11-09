"""The guards that stand between a public endpoint and someone else's bill.

Not on the Phase 2 guardrail list, but they are the guardrails that matter
once a payment method is attached: per-IP limiting, a global daily ceiling,
and the admin token on the endpoint that exposes visitors' contact details.

All driven with a fake clock. Nothing here sleeps.
"""

import pytest
from fastapi import HTTPException

from tests._bootstrap import api_mod


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(api_mod.time, "time", c)
    api_mod._last_sweep = c.now
    return c


# ------------------------------------------------------------ per-IP limiting

def test_requests_are_allowed_up_to_the_limit(clock):
    assert [api_mod.rate_limited("1.1.1.1", 3) for _ in range(3)] == [False] * 3


def test_the_request_past_the_limit_is_blocked(clock):
    for _ in range(3):
        api_mod.rate_limited("1.1.1.1", 3)
    assert api_mod.rate_limited("1.1.1.1", 3) is True


def test_the_window_slides(clock):
    """One minute, not a fixed bucket. A visitor blocked at 00:59 is served
    at 01:00 without the limiter being reset for everyone."""
    for _ in range(3):
        api_mod.rate_limited("1.1.1.1", 3)
    clock.advance(61)
    assert api_mod.rate_limited("1.1.1.1", 3) is False


def test_one_ip_does_not_limit_another(clock):
    for _ in range(3):
        api_mod.rate_limited("1.1.1.1", 3)
    assert api_mod.rate_limited("2.2.2.2", 3) is False


def test_chat_is_metered_more_tightly_than_query():
    """A /chat turn is 2-4 model calls against /query's one. Sharing a budget
    lets the expensive endpoint drain it."""
    assert api_mod.CHAT_RATE_LIMIT_PER_MIN < api_mod.RATE_LIMIT_PER_MIN


def test_idle_ips_are_swept(clock):
    """_hits is a dict keyed by attacker-controlled input. Without the
    sweep it grows forever - slow, quiet, and fatal after enough traffic."""
    api_mod.rate_limited("3.3.3.3", 3)
    assert "3.3.3.3" in api_mod._hits

    clock.advance(1000)
    api_mod._last_sweep = clock.now - 301   # make the sweep due
    api_mod.rate_limited("4.4.4.4", 3)

    assert "3.3.3.3" not in api_mod._hits


def test_an_active_ip_survives_the_sweep(clock):
    api_mod.rate_limited("3.3.3.3", 3)
    clock.advance(301)
    api_mod._last_sweep = clock.now - 301
    api_mod.rate_limited("3.3.3.3", 3)

    assert "3.3.3.3" in api_mod._hits


# ------------------------------------------------------------ daily ceiling

def test_the_daily_cap_stops_traffic_at_the_ceiling(monkeypatch):
    """Per-IP limits do nothing against traffic spread over many addresses.
    This is the backstop that decides the service goes quiet rather than the
    bill going large."""
    monkeypatch.setattr(api_mod, "DAILY_TURN_CAP", 3)

    assert [api_mod.daily_cap_reached() for _ in range(3)] == [False] * 3
    assert api_mod.daily_cap_reached() is True


def test_the_daily_cap_resets_on_a_new_day(monkeypatch):
    monkeypatch.setattr(api_mod, "DAILY_TURN_CAP", 3)
    api_mod._day["stamp"] = "1999-01-01"
    api_mod._day["turns"] = 9999

    assert api_mod.daily_cap_reached() is False


def test_chat_returns_429_once_the_cap_is_reached(client, no_model, monkeypatch):
    monkeypatch.setattr(api_mod, "DAILY_TURN_CAP", 0)

    r = client.post("/chat", json={
        "messages": [{"role": "user", "content": "hello"}],
        "session_id": "test",
    })

    assert r.status_code == 429
    assert no_model == []


# ------------------------------------------------------------ admin token

def test_an_unset_admin_token_denies_everything(monkeypatch):
    """Not "matches the empty string". Forgetting the variable would
    otherwise publish the escalation list - names, emails, phone numbers -
    to anyone who found the URL."""
    monkeypatch.setattr(api_mod, "ADMIN_TOKEN", "")

    with pytest.raises(HTTPException) as exc:
        api_mod.check_admin("", "")
    assert exc.value.status_code == 503


def test_a_wrong_token_is_rejected(monkeypatch):
    monkeypatch.setattr(api_mod, "ADMIN_TOKEN", "secret")

    with pytest.raises(HTTPException) as exc:
        api_mod.check_admin("wrong", "also-wrong")
    assert exc.value.status_code == 401


def test_the_header_is_accepted(monkeypatch):
    monkeypatch.setattr(api_mod, "ADMIN_TOKEN", "secret")
    api_mod.check_admin("", "secret")


def test_the_query_parameter_still_works(monkeypatch):
    """Accepted, though the docstring prefers the header: a token in the
    query string lands in access logs, proxy logs and browser history."""
    monkeypatch.setattr(api_mod, "ADMIN_TOKEN", "secret")
    api_mod.check_admin("secret", "")
