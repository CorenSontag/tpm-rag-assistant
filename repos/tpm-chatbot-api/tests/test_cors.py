"""CORS: who the browser is allowed to hear a reply from.

The deployed service must accept exactly four origins - tpmconsulting.org
(primary) and tpmconsults.org (serving the site until DNS moves), each as apex
and www. tpmconsults.com does not exist and must never appear.

These tests run against the real middleware with the real ALLOWED_ORIGINS that
tests/_bootstrap.py sets, so they exercise parsing and matching together
rather than trusting that a correctly parsed list is also correctly applied.
"""

import pytest

from tests._bootstrap import api_mod

PRODUCTION_ORIGINS = [
    "https://tpmconsulting.org",
    "https://www.tpmconsulting.org",
    "https://tpmconsults.org",
    "https://www.tpmconsults.org",
]


def _preflight(client, origin):
    return client.options(
        "/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )


# ------------------------------------------------------------ parsing

def test_spaces_after_commas_do_not_break_an_origin():
    """The incident this prevents: a widget that works on one domain and not
    the next one in the list, because that one was typed with a space."""
    parsed = api_mod.parse_origins("https://a.org, https://b.org,  https://c.org")
    assert parsed == ["https://a.org", "https://b.org", "https://c.org"]


def test_trailing_comma_does_not_add_an_empty_origin():
    """An empty string in the list is not harmless: it is an origin value
    that can never match, sitting in a config the operator believes is
    correct."""
    assert api_mod.parse_origins("https://a.org,") == ["https://a.org"]


def test_all_four_production_origins_parse_exactly():
    raw = ", ".join(PRODUCTION_ORIGINS)
    assert api_mod.parse_origins(raw) == PRODUCTION_ORIGINS


# ------------------------------------------------------------ configuration

def test_wildcard_is_never_allowed():
    """allow_origins=["*"] with a public endpoint means any page on the
    internet can spend this service's model budget from a visitor's browser."""
    assert "*" not in api_mod.ALLOWED_ORIGINS


def test_no_origin_carries_stray_whitespace():
    for origin in api_mod.ALLOWED_ORIGINS:
        assert origin == origin.strip()
        assert origin


def test_the_domain_that_does_not_exist_is_absent():
    """tpmconsults.com has been written by mistake before. It resolves to
    nobody, so an entry for it is a silently dead origin."""
    assert not any("tpmconsults.com" in o for o in api_mod.ALLOWED_ORIGINS)


# ------------------------------------------------------------ enforcement

@pytest.mark.parametrize("origin", PRODUCTION_ORIGINS)
def test_each_configured_origin_is_accepted(client, origin):
    r = _preflight(client, origin)
    assert r.headers.get("access-control-allow-origin") == origin


def test_unknown_origin_is_rejected(client):
    r = _preflight(client, "https://attacker.example")
    assert r.headers.get("access-control-allow-origin") is None


def test_apex_and_www_are_matched_separately(client):
    """Exact matching, not suffix matching.

    A middleware that matched by suffix would also accept
    https://tpmconsulting.org.attacker.example.
    """
    r = _preflight(client, "https://tpmconsulting.org.attacker.example")
    assert r.headers.get("access-control-allow-origin") is None


def test_http_is_not_accepted_for_a_production_origin(client):
    """The scheme is part of the origin. An http:// entry would let a
    man-in-the-middle page talk to the API."""
    r = _preflight(client, "http://tpmconsulting.org")
    assert r.headers.get("access-control-allow-origin") is None
