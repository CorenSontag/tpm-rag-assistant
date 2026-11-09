"""The guardrails that live in prose, asserted as prose.

Two of the Phase 2 guardrails - "the bot never states a price" and "no
invented figures" - are enforced by the system prompt, not by code. With the
model mocked, no test here can show the model obeys them. What these tests
show is narrower and still worth having: that the instruction is present, that
it still says what it is supposed to say, and that the contact route it points
at is intact.

Read that limitation literally. A green run here does NOT mean the pricing
guardrail works. Only test_chat.py, against a real model and a real bill,
measures that.
"""

import os
import re

import pytest

from tests._bootstrap import APP_DIR, api_mod, prompts_mod

AGENT_SYSTEM = prompts_mod.AGENT_SYSTEM
RENDERED = AGENT_SYSTEM.format(contact_line=prompts_mod._contact_line())


def _app_sources():
    for name in sorted(os.listdir(APP_DIR)):
        if name.endswith(".py"):
            path = os.path.join(APP_DIR, name)
            with open(path, encoding="utf-8") as fh:
                yield name, fh.read()


# ------------------------------------------------------------ pricing

def test_the_prompt_forbids_stating_a_price():
    """His dad's decision: the bot declines and routes instead. If this
    clause is edited away, the model is free to guess a training fee."""
    assert "NUMBERS AND PRICES" in AGENT_SYSTEM
    assert "A wrong price is worse than no price" in AGENT_SYSTEM


def test_the_prompt_forbids_hedged_figures():
    """"around 20,000 baht" is a stated price with a disclaimer on it."""
    for hedge in ("typically", "around", "starting from"):
        assert hedge in AGENT_SYSTEM, f"the ban on '{hedge}' was removed"


def test_the_prompt_forbids_filling_gaps():
    assert "Never fill the gap with invention" in AGENT_SYSTEM


def test_the_prompt_requires_retrieval_before_any_tpm_claim():
    assert "ALWAYS call search_tpm_docs" in AGENT_SYSTEM


# ------------------------------------------------------------ contact route

def test_the_prompt_renders_with_a_contact_route():
    """AGENT_SYSTEM is .format()-ed at every turn. An unescaped brace added
    to the prompt raises KeyError inside run_chat - at request time, in
    production, on every message."""
    assert "{contact_line}" in AGENT_SYSTEM
    assert "admin@tpmconsulting.org" in RENDERED


def test_contact_line_joins_what_is_configured():
    assert prompts_mod._contact_line() == (
        "contact page: https://tpmconsulting.org/contact; "
        "email: admin@tpmconsulting.org"
    )


def test_contact_line_says_so_when_nothing_is_configured(monkeypatch):
    """Blank is a real deploy state and must not render as an empty promise.

    This is the failure config.py warns about at length: one reordered import
    and CONTACT_EMAIL becomes "" with nothing raising.
    """
    monkeypatch.setattr(prompts_mod, "PRICING_URL", "")
    monkeypatch.setattr(prompts_mod, "CONTACT_URL", "")
    monkeypatch.setattr(prompts_mod, "CONTACT_EMAIL", "")
    assert "no contact links configured" in prompts_mod._contact_line()


# ------------------------------------------------------------ Thai register

def test_no_female_politeness_particle_anywhere_in_app():
    """ครับ, never ค่ะ. The assistant speaks as the consultancy.

    A static scan rather than a prompt assertion, because the particle could
    be introduced in a canned string, an error message or a tool description
    just as easily as in the system prompt.
    """
    offenders = [name for name, src in _app_sources() if "ค่ะ" in src]
    assert offenders == [], f"female particle ค่ะ found in: {offenders}"


def test_the_no_context_answer_is_still_thai():
    """A refusal that switches to English mid-conversation reads as a
    crash, not an answer."""
    assert re.search(r"[฀-๿]", api_mod.NO_CONTEXT_ANSWER)


# ------------------------------------------------------------ domains

def test_the_nonexistent_domain_appears_nowhere_in_app():
    """tpmconsults.com has been typed by mistake before; it does not exist."""
    offenders = [name for name, src in _app_sources() if "tpmconsults.com" in src]
    assert offenders == [], f"tpmconsults.com referenced in: {offenders}"


# ------------------------------------------------------------ query prompt

def test_the_query_prompt_still_forbids_outside_knowledge():
    """run_eval.py scores /query against this exact string. Loosening it
    would raise the eval score without improving retrieval."""
    assert "ONLY the provided context" in prompts_mod.SYSTEM_PROMPT
