"""Lazy access to the one vector store this process uses.

app/api.py and app/tools.py each used to do `store = QdrantStorage()` at
module scope. That constructor calls collection_exists() - a real network
round-trip - so an unreachable Qdrant was not a degraded service: the process
exited 1 before uvicorn bound its socket. On Container Apps scaled to zero
every cold start depends on Qdrant Cloud answering, which turned a transient
outage into a crash loop with a failing health probe, rather than a service
that reports unhealthy and recovers on its own.

tests/test_cold_start.py is the regression guard. It boots the app in a
subprocess against a closed port and fails if the import dies.

One consequence worth knowing: there is now one store per process, not one
per module. Both modules asked for "one instance - it holds a Qdrant client"
and each got its own; this is that intent actually enforced, and it halves the
connections.

Note what the laziness does NOT buy. With create_if_missing=False this
constructor makes no network call at all, so it does not fail for an
unreachable Qdrant and the uncached-failure path below is insurance against a
malformed QDRANT_URL, not a connectivity recovery story. The recovery that
matters is that the *process* now survives to answer /health at all.

Tests reach this module two ways, and both are live:

  - `mock.patch.object(tpm_rag, "QdrantStorage", FakeStore)` around the import
    of app.store still works, because the `from tpm_rag import ...` below
    binds whatever the name points at when this module is first imported.
  - seeding the cache directly: `app.store._store = FakeStore()`.

tests/_bootstrap.py and the root unittest files do both, and assert the first,
because relying on import order alone is how an "offline" suite quietly starts
reading the live corpus.
"""

from tpm_rag import QdrantStorage

_store = None


def get_store():
    """The process-wide store, built on first use.

    create_if_missing=False because the serving path must never create the
    collection. A typo in QDRANT_COLLECTION would otherwise make an empty
    3072-dim collection and a bot that finds nothing for every question.

    This closes the typo route specifically. It does NOT make /health reject
    an empty collection: a correctly-named collection with zero points still
    answers {"ok": true, "points": 0}, and per CLAUDE.md that - Docker
    mounting the wrong folder - is the most common real failure by a wide
    margin. Deciding whether /health should fail on points == 0 changes
    Container Apps readiness semantics (you could no longer deploy before
    ingest), so it is left as a deliberate open question, not an oversight.

    Ingestion keeps the default; it genuinely needs the collection made.
    """
    global _store
    if _store is None:
        _store = QdrantStorage(create_if_missing=False)
    return _store
