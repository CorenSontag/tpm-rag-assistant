"""Synchronous query API for the chatbot frontend.

    uv run uvicorn app.api:app --reload --port 8001

Why this file exists: the Inngest handler in main.py is event-driven and
asynchronous. A chat turn needs request/response, and at ~1-3s per query
durable orchestration buys nothing. See CHATBOT_HANDOFF.md section 1.

Verified against the real modules rather than assumed:
  - vector_db.QdrantStorage.search accepts source_ids and applies a
    MatchAny filter on the `source` payload field.
  - data_loader calls load_dotenv() at import, so importing run_eval (which
    imports data_loader before constructing OpenAI()) resolves the key.
  - run_eval guards main() behind __name__, so importing it runs no eval.
  - SYSTEM_PROMPT in main.py and run_eval.py are currently byte-identical.

Deviations from the handoff snippet, all because this endpoint is going to be
publicly reachable rather than localhost-only - see INTEGRATION_NOTES.md.
"""

import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel, Field

from tpm_rag import embed_texts

# The same three values prompts._contact_line() reads. Imported rather than
# re-read from the environment, so the startup check below cannot disagree
# with what the model is actually told.
from app.config import CONTACT_EMAIL, CONTACT_URL, PRICING_URL

# Single source of truth for the prompt. In the original codebase this string
# was duplicated across two files and had to be kept in sync by hand.
from app.prompts import SYSTEM_PROMPT

# The conversational agent. Kept in its own module so /query stays a plain
# pipeline and run_eval.py keeps measuring the same thing.
from app.agent import run_chat

# Built on first use, not at import. See app/store.py: this constructor used
# to open a Qdrant connection before uvicorn bound its socket.
from app.store import get_store

load_dotenv()  # data_loader already did this; explicit here so it is obvious

client = OpenAI()

KNOWN_SOURCES = {"tpm_lean_factory", "tpm_collab", "content_am"}


def parse_origins(raw: str) -> list[str]:
    """Split ALLOWED_ORIGINS, tolerating the spaces a human actually types.

    CORSMiddleware exact-matches origins, so one space after a comma in
    `a.org, b.org` yields " https://b.org", which matches nothing. The failure
    is silent on this side - the request arrives, the handler returns 200, the
    log looks clean - and the browser drops the response. It reads as "the
    widget is broken", not "one env var has a space in it", and the four
    origins this service needs are long enough that they will be typed with
    spaces. Also drops the empty string a trailing comma leaves behind.
    """
    return [o.strip() for o in raw.split(",") if o.strip()]


ALLOWED_ORIGINS = parse_origins(
    os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# /chat is metered separately: one call costs 2-4 model round trips against
# /query's one, so sharing a budget lets the expensive endpoint drain it.
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
CHAT_RATE_LIMIT_PER_MIN = int(os.getenv("CHAT_RATE_LIMIT_PER_MIN", "6"))
DAILY_TURN_CAP = int(os.getenv("DAILY_TURN_CAP", "2000"))

NO_CONTEXT_ANSWER = "ไม่พบข้อมูลที่เกี่ยวข้องในเอกสาร"
CHAPTER_PREFIX = re.compile(r"^\[บทที่\s*\d+\]\s*")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tpm-api")


# ---------------------------------------------------------------- errors

def error_ref() -> str:
    """A short id that appears in the response body AND in the log line.

    The caller gets something they can quote; the operator gets the whole
    exception. Interpolating the exception into the response instead handed
    out the Qdrant URL, OpenAI error detail and anything else it happened to
    carry - to anyone who could reach the endpoint, including whoever was
    probing it.
    """
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- startup checks

def warn_if_no_contact_configured() -> None:
    """Say so at startup when the bot has nowhere to send anyone.

    With all three blank the assistant still runs and still answers document
    questions, so nothing looks wrong from the outside - but every question it
    cannot answer is declined with no next step, and an escalated visitor is
    told a person will see their request with no route to reach one. That is a
    deploy mistake that currently makes no sound at all.

    A warning, not a failure: the values are the operator's to supply and the
    service is genuinely usable without them.
    """
    if not (PRICING_URL or CONTACT_URL or CONTACT_EMAIL):
        log.warning(
            "no PRICING_URL, CONTACT_URL or CONTACT_EMAIL is set - the "
            "assistant will decline what it cannot answer with no next step, "
            "and escalations have nowhere to point the visitor. Set at least "
            "one in .env."
        )


warn_if_no_contact_configured()


# ---------------------------------------------------------------- rate limit

_hits = defaultdict(deque)
_last_sweep = time.time()

_day = {"stamp": time.strftime("%Y-%m-%d"), "turns": 0}


def _sweep(now: float) -> None:
    """Drop IPs with no recent activity.

    Without this, _hits gains a permanent deque per unique IP. On a public
    endpoint that is an unbounded dictionary keyed by attacker-controlled
    input - slow, quiet, and fatal after enough traffic.
    """
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    for ip in [k for k, w in _hits.items() if not w or now - w[-1] > 600]:
        del _hits[ip]


def rate_limited(ip: str, limit: int = None) -> bool:
    """Sliding one-minute window per IP.

    In-process: resets on restart, does not span replicas. Enough to stop a
    bored visitor draining the OpenAI budget. Not a substitute for a gateway.
    """
    limit = RATE_LIMIT_PER_MIN if limit is None else limit
    now = time.time()
    _sweep(now)
    window = _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= limit:
        return True
    window.append(now)
    return False


def daily_cap_reached() -> bool:
    """A global ceiling on paid turns.

    Per-IP limits do nothing against traffic spread over many addresses. This
    is the backstop that decides the service goes quiet rather than the bill
    going large.
    """
    today = time.strftime("%Y-%m-%d")
    if _day["stamp"] != today:
        _day["stamp"] = today
        _day["turns"] = 0
    if _day["turns"] >= DAILY_TURN_CAP:
        return True
    _day["turns"] += 1
    return False


def check_admin(token: str, header_token: str) -> None:
    """Prefer the header. A token in the query string lands in access logs,
    proxy logs and browser history.

    An unset ADMIN_TOKEN denies everything rather than matching the empty
    string - otherwise forgetting the variable silently publishes your
    escalation list to the internet.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if header_token != ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(401, "bad token")


# ---------------------------------------------------------------- guardrail

# Cheap string checks that run before any paid call. Not a jailbreak defence -
# the real grounding is in the system prompt - but it stops the obvious probes
# and the accidental 50KB paste without spending a token on either.
BLOCKED = (
    "ignore previous",
    "ignore all previous",
    "disregard your instructions",
    "disregard previous",
    "reveal your system prompt",
    "show me your system prompt",
    "you are now",
)


def guardrail(text: str) -> str | None:
    """Return a refusal string, or None to let the message through."""
    if len(text) > 2000:
        return "That message is a bit long - could you shorten it?"
    lowered = text.lower()
    if any(phrase in lowered for phrase in BLOCKED):
        return "I can only help with questions about the TPM documents."
    return None

# ---------------------------------------------------------------- schemas

class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    source_ids: Optional[List[str]] = None


class Message(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: List[Message] = Field(min_length=1, max_length=40)
    session_id: str = Field(default="anon", max_length=64)


class Citation(BaseModel):
    text: str
    source: str
    page: Optional[int] = None
    # Web-sourced chunks have a URL and a page title instead of a page number.
    url: Optional[str] = None
    title: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    citations: List[Citation]
    num_contexts: int


# Declared after Citation so the annotation resolves without a forward
# reference - FastAPI builds the OpenAPI schema at startup and an unresolved
# ref fails there rather than at first request.
class ChatResponse(BaseModel):
    reply: str
    citations: List[Citation]
    searched: bool
    escalated: bool
    language: str


class UTF8JSONResponse(JSONResponse):
    """Declare the charset explicitly.

    JSON is UTF-8 by spec and browsers get this right, but several HTTP
    clients - Windows PowerShell 5.1 among them - fall back to ISO-8859-1
    when the Content-Type header omits a charset, which turns every Thai
    answer into mojibake.
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(title="TPM RAG", default_response_class=UTF8JSONResponse)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- endpoints

@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, request: Request) -> QueryResponse:
    ip = request.client.host if request.client else "unknown"
    if rate_limited(ip):
        raise HTTPException(429, "too many requests - please slow down")

    if req.source_ids:
        unknown = set(req.source_ids) - KNOWN_SOURCES
        if unknown:
            raise HTTPException(400, f"unknown source_ids: {sorted(unknown)}")

    try:
        query_vec = embed_texts([req.question])[0]
        found = get_store().search(query_vec, req.top_k, req.source_ids)
    except Exception as e:
        ref = error_ref()
        log.exception("retrieval failed ref=%s", ref)
        raise HTTPException(503, f"retrieval failed (ref {ref})") from e

    contexts = found["contexts"]
    pages = found.get("pages") or [None] * len(contexts)
    sources = found.get("sources") or [""] * len(contexts)
    urls = found.get("urls") or [None] * len(contexts)
    titles = found.get("titles") or [None] * len(contexts)

    if not contexts:
        return QueryResponse(answer=NO_CONTEXT_ANSWER, citations=[], num_contexts=0)

    # Raw text - [บทที่ N] prefix intact - goes to the model. That prefix is
    # load-bearing for retrieval and chapter-scoped questions; it is stripped
    # for display only, below. Do not strip it upstream.
    context_block = "\n\n".join(
        f"- {c}" + (f" (หน้า {pages[i]})" if pages[i] else "")
        for i, c in enumerate(contexts)
    )

    user_content = (
        "Use the following context to answer the question.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {req.question}\n"
        "Answer concisely using the context above."
    )

    # Model, temperature and max_tokens deliberately match run_eval.run_query.
    # Change them and run_eval.py stops measuring what users actually get.
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:
        ref = error_ref()
        log.exception("generation failed ref=%s", ref)
        raise HTTPException(502, f"generation failed (ref {ref})") from e

    # `sources` is now positional, so index i genuinely describes chunk i.
    # The previous version could not rely on that and left the field blank
    # rather than mislabel a passage.
    return QueryResponse(
        answer=res.choices[0].message.content.strip(),
        citations=[
            Citation(
                text=CHAPTER_PREFIX.sub("", contexts[i]).strip(),
                source=sources[i],
                page=pages[i],
                url=urls[i],
                title=titles[i],
            )
            for i in range(len(contexts))
        ],
        num_contexts=len(contexts),
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """Conversational turn. Retrieval happens only if the model asks for it.

    /query above stays as it was, so run_eval.py keeps measuring the same
    pipeline. This endpoint is what the widget talks to.
    """
    started = time.time()
    ip = request.client.host if request.client else "unknown"
    if rate_limited(ip, CHAT_RATE_LIMIT_PER_MIN):
        raise HTTPException(429, "too many requests - please slow down")
    if daily_cap_reached():
        log.warning("daily turn cap reached (%s)", DAILY_TURN_CAP)
        raise HTTPException(429, "the assistant is resting - please try tomorrow")

    if req.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")

    # Screen EVERY user turn, not just the newest. The client supplies the
    # whole array, so an injection can be planted in an earlier turn and the
    # last message left innocent.
    refusal = next(
        (r for r in (guardrail(m.content) for m in req.messages
                     if m.role == "user") if r),
        None,
    )
    if refusal:
        return ChatResponse(
            reply=refusal,
            citations=[],
            searched=False,
            escalated=False,
            language="en",
        )

    try:
        result = run_chat(
            [m.model_dump() for m in req.messages], session_id=req.session_id
        )
    except Exception as e:
        ref = error_ref()
        log.exception("chat failed for session %s ref=%s", req.session_id, ref)
        raise HTTPException(502, f"chat failed (ref {ref})") from e

    log.info(
        "chat session=%s turns=%d searched=%s escalated=%s lang=%s %.2fs",
        req.session_id,
        len(req.messages),
        result["searched"],
        result["escalated"],
        result["language"],
        time.time() - started,
    )

    return ChatResponse(
        reply=result["reply"],
        citations=[Citation(**c) for c in result["citations"]],
        searched=result["searched"],
        escalated=result["escalated"],
        language=result["language"],
    )


@app.get("/escalations")
def escalations(
    token: str = "", x_admin_token: str = Header(default="")
) -> UTF8JSONResponse:
    """Requests handed to a person.

    Prefer the X-Admin-Token header; the query-string form is kept only so
    a quick curl still works, and it leaks into logs.
    """
    check_admin(token, x_admin_token)
    import sqlite3 as _sqlite3

    con = _sqlite3.connect(os.getenv("ESCALATIONS_DB", "escalations.db"))
    try:
        rows = con.execute(
            "SELECT session_id, name, contact, reason, summary, ts FROM"
            " escalations ORDER BY ts DESC LIMIT 200"
        ).fetchall()
    except _sqlite3.OperationalError:
        rows = []
    con.close()
    keys = ("session_id", "name", "contact", "reason", "summary", "ts")
    # UTF8JSONResponse, not JSONResponse: returning a Response instance
    # bypasses the app's default_response_class, so a bare one omits the
    # charset - and this endpoint is read from a terminal, by an operator, to
    # find out a Thai person's name. PowerShell 5.1 then decodes it as
    # ISO-8859-1 and shows mojibake, which is the exact failure UTF8JSONResponse
    # was written for.
    return UTF8JSONResponse([dict(zip(keys, r)) for r in rows])


@app.get("/health")
def health() -> dict:
    """Cheap liveness check. Confirms Qdrant is reachable and populated."""
    store = get_store()
    try:
        info = store.client.get_collection(store.collection)
    except Exception as e:
        ref = error_ref()
        log.exception("health check failed ref=%s", ref)
        # Distinguish "Qdrant is not answering" from "Qdrant answered, and the
        # collection is not there". get_collection raises the same way for a
        # refused socket and for a 404, and since the serving path passes
        # create_if_missing=False nothing creates the collection anymore - so a
        # fresh Qdrant Cloud before the first ingest reports the second case.
        # Blaming the network for it sends the operator to debug TLS and
        # firewalls for a collection that was simply never ingested.
        try:
            missing = not store.client.collection_exists(store.collection)
        except Exception:
            missing = False  # cannot ask; the first error stands
        if missing:
            raise HTTPException(
                503, f"collection {store.collection!r} not found - run ingest (ref {ref})"
            ) from e
        raise HTTPException(503, f"vector store unreachable (ref {ref})") from e

    # points == 0 still answers ok. See app/store.py's get_store(): making it
    # unhealthy is a readiness-semantics decision, not a bug fix.
    return {"ok": True, "points": getattr(info, "points_count", None)}


@app.get("/sources")
def sources() -> dict:
    """Lets the frontend build its document filter without hardcoding Thai."""
    return {
        "sources": [
            {"id": "tpm_lean_factory", "label": "TPM สำหรับโรงงานแบบลีน"},
            {"id": "tpm_collab", "label": "TPM การมีส่วนร่วม"},
            {"id": "content_am", "label": "AM (Autonomous Maintenance)"},
        ]
    }
