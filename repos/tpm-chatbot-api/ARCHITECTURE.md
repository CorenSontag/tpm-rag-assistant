# Architecture

> Written to be handed to someone — or something — that has to work on this
> code without having built it. Front-loads the things that are surprising.

---

## 1. The one thing to understand first

**Retrieval is a tool the model chooses to call, not a pipeline every message
runs through.**

The earlier version embedded every message and searched unconditionally, so
"สวัสดีครับ" retrieved five random passages from a maintenance manual and the
model tried to answer a greeting from them.

Now the model holds three tools and decides. A greeting costs one model call
and zero searches. A TPM question costs two calls and one search.

The second-order effect matters more than the first: **multi-turn works for
free.** Ask a question, then follow up with `แล้วบทที่ 8 ล่ะ`, and the model —
seeing the whole conversation — issues a complete standalone search query.
There is no query-rewriting module. The behaviour falls out of handing the
model the history and a search tool.

---

## 2. Files

| File | Lines | Responsibility |
|---|---|---|
| `app/config.py` | 73 | Environment-driven settings. No machine-specific values |
| `app/prompts.py` | 109 | Both system prompts. The `/query` one and the agent one |
| `app/tools.py` | 400 | Tool schemas *and* their implementations |
| `app/agent.py` | 262 | The loop: ask, dispatch, append, repeat |
| `app/api.py` | 515 | HTTP: routing, rate limits, guardrail, CORS, logging |
| `app/store.py` | 62 | Lazy `get_store()`. The only place the store is built |
| `cli.py` | 106 | Terminal chat. Imports `run_chat` directly, no HTTP |
| `run_eval.py` | 139 | Retrieval regression test, 15 Thai questions |
| `test_chat.py` | 301 | Agent behaviour test, 12 cases |

Line counts are a snapshot (measured 2026-11-09) and nothing checks them, so
treat them as rough orientation only. Every number in this table was stale
before that date - `tools.py` was listed as 223 when it was 400.

Retrieval lives in a separate package, `tpm_rag`. This repo never touches
Qdrant directly — it calls `store.search()` and `embed_texts()` and knows
nothing else about how documents are stored.

**Schema and implementation share `tools.py` on purpose.** The `description`
fields are read by the model when deciding what to call. They are behaviour,
not documentation. Editing one without the other is how an agent starts
calling the wrong tool.

---

## 3. One turn, end to end

```
POST /chat  {session_id, messages[]}
  api.py:  rate limit -> daily cap -> guardrail on EVERY user turn
  agent.py: convo = [system] + last 12 messages

  loop, up to MAX_TOOL_ROUNDS (4):
      client.chat.completions.create(messages=convo, tools=TOOLS)

      no tool_calls?  -> that is the answer, break
      tool_calls?     -> append the request to convo
                         run the tool (tools.py)
                         append the result as {"role": "tool", ...}
                         loop

  dedupe citations -> {reply, citations, searched, escalated, language}
```

The whole agent is a `for` loop, an `if`, and a list that grows.

**No agent framework.** With three tools there is nothing for one to abstract,
and a hand-written loop means every failure shows up in a stack trace rather
than inside someone else's control flow.

---

## 4. Where the documents enter the prompt

This is the question people get wrong. **The model has no connection to
Qdrant.** It cannot query anything.

`tools.py` runs the search, and `agent.py` appends the result as a message:

```python
convo.append({
    "role": "tool",
    "tool_call_id": call.id,
    "content": json.dumps(result, ensure_ascii=False),   # <- the handoff
})
```

That `json.dumps` is the entire integration between the vector database and
the language model. Everything downstream sees a string. Replace Qdrant with a
text file and nothing else changes.

---

## 5. Traps

These look like cleanup opportunities and are not.

**The corpus is Thai. All of it.** OCR runs `tha+eng`; the text cleaner depends
on the `฀-๿` Unicode range; chapter headings are Thai patterns. The agent must
pass `query` to `search_tpm_docs` **in Thai** even when the visitor wrote
English — that instruction is in the prompt, and removing it silently halves
retrieval quality with no error.

**Chunks carry a bracketed heading prefix.** `[บทที่ N]` is prepended at ingest
and is load-bearing for chapter-scoped questions. Strip it for *display* only
— `CHAPTER_PREFIX` in `tools.py`. Never upstream.

**`QdrantStorage.search` returns `contexts`, `sources`, `pages`, `urls` and
`titles` all positionally** — index `i` of each describes the same point, so
zipping them is correct. `sources` *used* to be a deduplicated set, which
silently broke per-citation attribution the moment one result set spanned more
than one document; `store.py:55-70` fixed that and says so in a comment. If you
want the distinct set, use `source_set`, which the same return dict provides —
do not re-deduplicate `sources`.

(This entry previously claimed the opposite and told you not to zip them. It
was describing code that no longer exists.)

**`/query` is not dead code.** It is a tool-free pipeline kept for diagnosis:
when an answer looks wrong, it isolates retrieval from the agent's judgment.
That separation is what identified a request-encoding bug that was mangling
Thai before it reached the embedding model.

**`run_eval.py` measures `/query`, not `/chat`.** Deliberately. It is a
regression guard on retrieval. The model, temperature and `max_tokens` are
byte-identical to the endpoint's so the comparison holds. Change one and change
both, or the eval stops measuring anything real.

**The routing logic is prose.** Whether a message triggers a search is decided
by the model reading the tool descriptions and the system prompt. There is no
classifier and no keyword list. Rewriting a `description` changes behaviour
with nothing to compile.

**Every user turn is guardrailed, not just the newest.** The client sends the
whole array, so an injection can be planted in an earlier turn.

**A retrieval outage must stay a degraded answer, not a failed turn.**
`_safe_retrieve` in `tools.py` catches backend failures and returns `None`, and
the tools turn that into `{"found": 0, "available": False, "note": ...}` so the
model can tell the visitor search is down. It returns `None` rather than `[]`
deliberately: `[]` means the search ran and matched nothing, which the bot
reports as "not in the documents". Collapse the two and a Qdrant blip has the
bot confidently telling visitors their question is not covered by the books
when nobody looked. Before the store was lazy this path was unreachable - the
process died instead - so a Qdrant outage surfaced as `502` on every turn.

**The store is lazy on purpose.** `app/store.py` builds one `QdrantStorage` per
process on first use. `QdrantStorage.__init__` calls `collection_exists()` - a
network round-trip - and `api.py` and `tools.py` each used to build one at
module scope, so an unreachable Qdrant exited the process before uvicorn bound
its socket. Scaled to zero on Container Apps, that is a crash loop on every
cold start during a Qdrant outage rather than a service that reports unhealthy
and recovers. Measured in the container: the pre-change image exits 1 with
`Connection refused`; the current one stays up and serves `/health`. Moving the
construction back to module scope reintroduces the whole failure.

**The serving path passes `create_if_missing=False`; ingestion does not.** Left
on, a typo in `QDRANT_COLLECTION` makes an empty 3072-dim collection and a bot
that answers nothing while reporting healthy. Verified: pointing a container at
a non-existent collection creates nothing.

**`/health` has two distinct 503s and they must stay distinct.**
`get_collection` raises identically for a refused socket and for a 404, and the
handler used to call both "vector store unreachable". Since nothing in the
serving path creates the collection now, the second case is just "a fresh
Qdrant Cloud before ingest" - and an operator told "unreachable" goes looking
for a network fault that is not there. `test_api.py` asserts both directions.
A correctly-named *empty* collection still returns `{"ok": true, "points": 0}`:
a known hole, left open because closing it would change readiness semantics.

**`tpm_rag/__init__.py` defers the `loader` import, and `embed.py` exists for
weight, not tidiness.** `loader.py` pulls PyMuPDF, pytesseract, Pillow and
llama-index at module scope; importing it eagerly put all of that in the
serving image (919 MB, now 410 MB). The ingestion deps are a `tpm-rag[ingest]`
extra: `setup/2-SETUP.ps1` and CI install it, the Dockerfile deliberately does
not. `tests/test_import_weight.py` guards it - and asserts the heavy packages
*are* installed, because "fitz is not in sys.modules" is vacuously true in an
environment that never had fitz.

**`app/config.py` loads its own `.env`, and does not depend on `tpm_rag`.**
This entry previously claimed the opposite: that `tpm_rag.embed`'s
`load_dotenv()` was what kept `CONTACT_EMAIL` populated, and that losing it
would blank the contact route silently. Measured 2026-11-09 - a script that
imports `app.config` without ever touching `tpm_rag` resolves `CONTACT_EMAIL`
correctly, because `config.py` calls `load_dotenv()` itself above its first
`os.getenv`. The two calls resolve *different* files: `config.py`'s the API
package's `.env`, `embed.py`'s the bundle root's.

Verify this kind of claim with a real `.py` file, never `python -c`: with no
`__file__` to walk up from, `find_dotenv()` falls back to the current working
directory and appears to confirm whatever you expected.

---

## 6. Verifying a change

```bash
python cli.py            # fastest sanity check - no HTTP, no CORS
python test_chat.py      # 12 behaviour cases against a running API
python run_eval.py       # 15 retrieval cases, expect ~13/15
```

Both suites score by keyword containment, so a correct answer phrased
unexpectedly registers as a false failure. Read the reply before believing a
`FAIL`.

**Run `test_chat.py` before trusting any change to `api.py`.** `cli.py` bypasses
the HTTP layer entirely, so it will pass happily while an endpoint is broken —
that is exactly how a `NameError` in `/chat` survived undetected until an
automated check found it.

---

## 7. Do not

- Answer TPM questions without calling `search_tpm_docs`. The whole point is
  grounding
- Strip the `[บทที่ N]` prefix upstream of display
- Change `model`, `temperature` or `max_tokens` in `/query` without changing
  `run_eval.py` to match
- Add a fourth tool without adding a `test_chat.py` case for it
- Re-deduplicate `sources` — it is positional on purpose; use `source_set`
- Put credentials anywhere the browser can reach
- Trust `cli.py` passing as evidence that the API works

---

## 8. Known limitations

**Conversation history is client-supplied.** The browser sends the whole array,
so forged assistant turns are possible. Small blast radius — no privileged
data, no destructive tools — but server-side session storage is the correct
fix.

**No streaming.** A searching turn is 4–6 seconds of a waiting state.

**The reranker is wired but off by default.** `bge-reranker-v2-m3` lives in the
`tpm-rag` package and *is* connected: `tools.py` imports `fetch_width` and
`maybe_rerank`, and `_retrieve` over-fetches with `fetch_width(top_k)` then
reranks back down with `maybe_rerank`. (Cited by function on purpose - the line
numbers that used to be here, `:151` and `:169`, had drifted to 154 and 172.) Set `RERANK=1` to turn it on. With it off, `fetch_width`
returns `top_k` unchanged and `maybe_rerank` is a no-op, and if
`sentence-transformers` is missing it degrades to a plain truncation rather
than failing.

It runs locally, so it costs nothing per query, and it is still the largest
retrieval improvement available — but the work needed is *evaluating* it with
`RERANK=1`, not wiring it.

Note `/query` does **not** rerank — its handler in `api.py` calls `store.search`
directly, with no `fetch_width` and no `maybe_rerank` — while `run_query` in
`run_eval.py` uses both. So with `RERANK=1` the eval measures a reranked pipeline
and `/query` serves an unreranked one. `/chat` is the path that matches the eval.

(This entry previously said the reranker was "never wired in". It then cited
`api.py:248` and `run_eval.py:28,35`, which had already drifted to 294 and 29,44
by the time anyone checked. Cite the function, not the line: a symbol survives
edits above it and a line number does not.)

**OCR noise.** Some passages are visibly garbled. The density filter keeps
mangled Thai because the characters are still in the Thai Unicode range. The
model reads around it; expanded citations look rough.
