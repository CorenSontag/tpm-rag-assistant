"""The three tools the agent can call, and their JSON Schema declarations.

Schema and implementation live together deliberately. The `description` fields
are read by the model at decision time - they are behaviour, not documentation,
and editing one without the other is how an agent starts calling the wrong tool.
"""

import json
import logging
import re
import sqlite3
import time
import urllib.request
from collections import defaultdict

from tpm_rag import embed_texts, fetch_width, maybe_rerank

from app.config import (
    ESCALATION_WEBHOOK_URL,
    ESCALATIONS_DB,
    KNOWN_SOURCES,
    SOURCE_LABELS,
)

# Same import agent.py makes. prompts imports only config, so this is not a
# cycle - and the escalation note and the system prompt must hand out the
# same contact route or the model is given two answers.
from app.prompts import _contact_line

# Built on first use, not at import - see app/store.py. This module is
# imported by app/agent.py, so an eager construct here killed the process just
# as surely as the one in app/api.py did.
from app.store import get_store

log = logging.getLogger(__name__)

# Strips the bracketed chapter heading for display only. The prefix stays in
# the text sent to the model - it is load-bearing for chapter-scoped questions.
CHAPTER_PREFIX = re.compile(r"^\[[^\]]{0,80}\]\s*")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_tpm_docs",
            "description": (
                "Search the knowledge base: three Thai TPM reference books, "
                "plus this company's own website content (services, methods, "
                "courses, company information). Call this before stating any "
                "fact about TPM or about the company. Search the books in "
                "Thai; search website content in the visitor's language."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A complete, standalone question. Use Thai for the "
                            "books; use the visitor's language for website "
                            "content. Resolve pronouns and references from the "
                            "conversation first - the search has no memory."
                        ),
                    },
                    "source_id": {
                        "type": ["string", "null"],
                        "enum": [
                            "tpm_lean_factory",
                            "tpm_collab",
                            "content_am",
                            "website",
                            None,
                        ],
                        "description": (
                            "Restrict to one source. Use 'website' for anything "
                            "about this company, its services, courses or "
                            "prices. Use a book id when the visitor names a "
                            "book, or asks about 'this book' or its authors. "
                            "Use null to search everything."
                        ),
                    },
                },
                "required": ["query", "source_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_reading",
            "description": (
                "For a broad topic, find which book and which page ranges cover "
                "it best, so the visitor can read further themselves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic, in Thai.",
                    }
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": (
                "Hand the conversation to a person. Ask for a name and an email "
                "or phone number BEFORE calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What they need, in one or two sentences.",
                    },
                    "name": {"type": ["string", "null"]},
                    "contact": {
                        "type": ["string", "null"],
                        "description": "Email or phone, exactly as they gave it.",
                    },
                    "reason": {
                        "type": "string",
                        "enum": ["not_in_docs", "asked_for_person", "frustrated"],
                    },
                },
                "required": ["summary", "name", "contact", "reason"],
                "additionalProperties": False,
            },
        },
    },
]


def _clean(text: str) -> str:
    """Strip the bracketed heading prefix for display. Retrieval keeps it."""
    return CHAPTER_PREFIX.sub("", text or "").strip()


def _retrieve(query: str, source_id=None, top_k=5):
    vec = embed_texts([query])[0]
    source_ids = [source_id] if source_id in KNOWN_SOURCES else None
    # With RERANK on, pull a wider candidate set and let the cross-encoder
    # choose. With it off, fetch_width returns top_k unchanged and nothing
    # about this path differs from before.
    found = get_store().search(vec, fetch_width(top_k), source_ids)
    contexts = found["contexts"]
    n = len(contexts)
    pages = found.get("pages") or [None] * n
    sources = found.get("sources") or [""] * n
    urls = found.get("urls") or [None] * n
    titles = found.get("titles") or [None] * n
    items = [
        {
            "text": contexts[i],
            "display": _clean(contexts[i]),
            "page": pages[i],
            "source": sources[i],
            "url": urls[i],
            "title": titles[i],
        }
        for i in range(n)
    ]
    return maybe_rerank(query, items, top_k=top_k)


def _unavailable_note() -> str:
    """Built per call, not at import - `_contact_line()` reads configuration."""
    return (
        "The document search is unavailable right now. Tell the visitor plainly"
        " that you cannot search the documents at this moment and that they"
        " should try again shortly, replying in the language they wrote in."
        " Do not answer from memory, do not invent a passage or a page number,"
        " and do not describe an error you cannot see. Offer this contact"
        f" route: {_contact_line()}"
    )


def _safe_retrieve(query: str, source_id=None, top_k=5):
    """`_retrieve`, but a backend failure becomes a value rather than an exception.

    Incident this prevents: a Qdrant or embedding failure used to propagate out
    of the tool, through the agent loop, and out of api.py as
    `502 {"detail": "chat failed (ref ...)"}` - every turn, for the whole
    outage. Before app/store.py made the store lazy, an unreachable Qdrant
    killed the process instead, so this path was unreachable; a Qdrant Cloud
    blip now reaches it routinely, and a 502 reads as a broken product rather
    than a degraded one.

    Returns None on failure, which is deliberately distinct from `[]`. `[]`
    means the search ran and nothing matched - "this is not in the documents".
    None means the search could not run - "try again shortly". Collapsing them
    would have the bot confidently tell a visitor their question is not covered
    by the books when in fact nobody looked.
    """
    try:
        return _retrieve(query, source_id, top_k=top_k)
    except Exception:
        # Logged, not swallowed. Nothing raises past here any more, so api.py's
        # error_ref correlation never fires for this - this is the only record
        # that an outage happened at all.
        log.exception("retrieval unavailable - search tool degraded")
        return None


def tool_search(args, collected):
    hits = _safe_retrieve(args.get("query", ""), args.get("source_id"), top_k=5)
    if hits is None:
        return {"found": 0, "available": False, "note": _unavailable_note()}
    if not hits:
        return {"found": 0, "note": "Nothing matched. Do not invent an answer."}
    for h in hits:
        collected.append(h)
    return {
        "found": len(hits),
        "passages": [
            {"page": h["page"], "source": h["source"], "url": h["url"], "title": h["title"],
             "text": h["text"][:1200]}
            for h in hits
        ],
    }


def tool_recommend(args, collected):
    """Search wide, then report which book and page ranges cluster together."""
    hits = _safe_retrieve(args.get("topic", ""), None, top_k=20)
    if hits is None:
        return {"found": 0, "available": False, "note": _unavailable_note()}
    if not hits:
        return {"found": 0}
    by_source = defaultdict(list)
    for h in hits:
        if h["page"]:
            by_source[h["source"]].append(h["page"])
    out = []
    for source, pages in sorted(
        by_source.items(), key=lambda kv: -len(kv[1])
    ):
        pages = sorted(set(pages))
        out.append(
            {
                "source": source,
                "label": SOURCE_LABELS.get(source, source),
                "pages": pages[:12],
                "range": f"{min(pages)}-{max(pages)}",
                "hits": len(pages),
            }
        )
    return {"found": len(hits), "recommendations": out}


def init_escalations():
    con = sqlite3.connect(ESCALATIONS_DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS escalations ("
        "id INTEGER PRIMARY KEY, session_id TEXT, name TEXT, contact TEXT,"
        " reason TEXT, summary TEXT, transcript TEXT, ts REAL)"
    )
    con.commit()
    con.close()


def has_open_escalation(session_id: str) -> bool:
    """Has this session already been handed to a person?

    One indexed-by-nothing lookup on a table that holds tens of rows, once per
    turn. If that table ever grows enough for this to matter, the read is not
    the problem you have.
    """
    con = sqlite3.connect(ESCALATIONS_DB)
    try:
        row = con.execute(
            "SELECT 1 FROM escalations WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # No table yet - same answer as no row, and not worth failing a turn.
        return False
    finally:
        con.close()
    return row is not None


def _post_json(url: str, payload: dict) -> None:
    """POST one JSON body, and read the response so the socket closes.

    Its own function so a test can replace it without opening a socket, and
    so the retry/timeout policy for notifications lives in one obvious place.
    stdlib urllib rather than a new dependency: this is one POST.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


# What the model is told to say, and the promise it is allowed to make.
#
# The second one is not a formatting nicety. With no notifier configured, a
# "someone will be in touch" note is a lie the model then repeats - warmly and
# convincingly - to a real person who is now waiting for a call that nobody
# has been asked to make.
NOTE_NOTIFIED = (
    "Saved, and the team has been notified. Confirm to the visitor that "
    "someone will be in touch."
)
NOTE_NOT_NOTIFIED = (
    "Saved to the escalation list, but nobody has been notified automatically. "
    "Do NOT promise a callback or a time frame. Tell the visitor their request "
    "is recorded and give them this contact route so they can reach the team "
    "themselves - {contact_line}"
)


def _note_not_notified() -> str:
    """The route is interpolated, not referred to.

    "Use the contact details in your instructions" asks the model to go and
    find them at the moment it is being told not to promise a callback. This
    is the turn where a real person is waiting for an answer; the address goes
    in the tool result, next to the instruction that needs it.

    Built per call, not at import: _contact_line() reads configuration that is
    only loaded once the process is up.
    """
    return NOTE_NOT_NOTIFIED.format(contact_line=_contact_line())


def tool_escalate(args, session_id, transcript):
    contact = (args.get("contact") or "").strip()
    if not contact:
        return {
            "saved": False,
            "note": "No contact given. Ask for an email or phone, then retry.",
        }
    con = sqlite3.connect(ESCALATIONS_DB)
    con.execute(
        "INSERT INTO escalations (session_id, name, contact, reason, summary,"
        " transcript, ts) VALUES (?,?,?,?,?,?,?)",
        (
            session_id,
            args.get("name"),
            contact,
            args.get("reason"),
            args.get("summary"),
            transcript[-6000:],
            time.time(),
        ),
    )
    con.commit()
    con.close()
    print(f"[escalation] {contact} - {args.get('reason')}")

    # Notify AFTER the commit, never before, and never inside the transaction.
    # A webhook that times out must cost us a notification, not the row - the
    # row is the only durable record that this person asked for help.
    #
    # The transcript is deliberately NOT in the payload. It is in the database
    # and at /escalations; a third-party endpoint does not need the whole
    # conversation to know who to call back.
    notified = False
    if ESCALATION_WEBHOOK_URL:
        try:
            _post_json(
                ESCALATION_WEBHOOK_URL,
                {
                    "session_id": session_id,
                    "name": args.get("name"),
                    "contact": contact,
                    "reason": args.get("reason"),
                    "summary": args.get("summary"),
                    "ts": time.time(),
                },
            )
            notified = True
        except Exception:
            # Warning, not error: nothing was lost, and the row is still
            # readable at /escalations. But somebody has to find out that the
            # notification path is broken before the next one is missed too.
            log.warning(
                "escalation for %s saved but the webhook failed", contact,
                exc_info=True,
            )

    return {"saved": True, "note": NOTE_NOTIFIED if notified else _note_not_notified()}


