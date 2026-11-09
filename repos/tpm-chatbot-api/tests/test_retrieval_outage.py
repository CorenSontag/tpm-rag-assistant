"""A Qdrant outage must degrade the answer, not fail the turn.

Incident this prevents: a Qdrant or embedding failure propagated out of
`tool_search`, through the agent loop, and out of `api.py` as
`502 {"detail": "chat failed (ref ...)"}` - on every turn, for the whole
outage. It was unreachable before `app/store.py` made the store lazy, because
an unreachable Qdrant killed the process at import instead. Scaled to zero on
Container Apps, a Qdrant Cloud blip now reaches it routinely.

The distinction these tests pin down is `None` versus `[]`. `[]` means the
search ran and matched nothing - "that is not in the documents". `None` means
the search could not run - "try again shortly". If those ever collapse, the bot
starts telling visitors their question is not covered by the books when in fact
nobody looked.
"""

import json
from unittest import mock

import pytest

from tests._bootstrap import store_mod  # noqa: F401  (seeds the fake store)

from app import tools as tools_mod


class _DeadStore:
    """A store whose search always fails the way a refused socket does."""

    collection = "docs"
    client = None

    def search(self, *a, **kw):
        raise RuntimeError("Connection refused to qdrant at localhost:6333")


class _EmptyStore:
    """A store that answers, with nothing in it."""

    collection = "docs"
    client = None

    def search(self, *a, **kw):
        return {"contexts": [], "sources": [], "pages": [], "urls": [], "titles": []}


@pytest.fixture
def dead_store():
    # embed_texts is patched to SUCCEED on purpose. Left real it would try a
    # live OpenAI call - which would both cost money and make these tests pass
    # for the wrong reason, since the helper catches that failure too.
    with mock.patch.object(store_mod, "_store", _DeadStore()), \
            mock.patch.object(tools_mod, "embed_texts", lambda t: [[0.1] * 4]):
        yield


@pytest.fixture
def empty_store():
    with mock.patch.object(store_mod, "_store", _EmptyStore()), \
            mock.patch.object(tools_mod, "embed_texts", lambda t: [[0.1] * 4]):
        yield


def test_search_reports_unavailable_instead_of_raising(dead_store):
    collected = []
    out = tools_mod.tool_search({"query": "OEE คืออะไร"}, collected)

    assert out["available"] is False
    assert out["found"] == 0
    assert out["note"]
    # No citations may be collected from a search that never ran.
    assert collected == []


def test_recommend_reports_unavailable_instead_of_raising(dead_store):
    out = tools_mod.tool_recommend({"topic": "TPM"}, [])
    assert out["available"] is False
    assert out["found"] == 0


def test_an_empty_result_is_not_reported_as_an_outage(empty_store):
    """`[]` and `None` must not collapse - this is the other half of the pair."""
    out = tools_mod.tool_search({"query": "OEE คืออะไร"}, [])
    assert out["found"] == 0
    # The telling difference: no `available` key, and the note tells the model
    # the documents do not cover it rather than to try again later.
    assert "available" not in out
    assert "invent" in out["note"]


def test_the_outage_note_leaks_no_infrastructure_detail(dead_store):
    """The note reaches the model, which may echo it to a visitor."""
    blob = json.dumps(tools_mod.tool_search({"query": "x"}, []), ensure_ascii=False).lower()
    for secret in ("qdrant", "6333", "connection refused", "traceback"):
        assert secret not in blob, f"outage note leaked {secret!r}"


def test_the_outage_note_tells_the_model_not_to_invent(dead_store):
    note = tools_mod.tool_search({"query": "x"}, [])["note"]
    assert "do not answer from memory" in note.lower()
    assert "do not invent" in note.lower()
