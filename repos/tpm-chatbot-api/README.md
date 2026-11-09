# tpm-chatbot-api

A bilingual conversational agent over a Thai technical corpus. Retrieval is a
tool the model chooses to use, not a pipeline every message is forced through.

Answers TPM questions from three Thai manuals with page citations, replies in
whichever language the visitor used, declines questions the documents don't
cover, and escalates to a human when asked.

Retrieval comes from [tpm-rag](../tpm-rag).
The browser widget is
[tpm-chat-widget](../tpm-chat-widget).

## The design decision that matters

The first version was a fixed pipeline: embed the question, search, generate.
Every message. Which meant "สวัสดีครับ" got embedded, retrieved five random
passages from a maintenance manual, and the model dutifully tried to answer a
greeting from them.

Now the model holds three tools and decides which, if any, to use:

| Message | Model calls | Retrieval |
|---|---|---|
| `สวัสดีครับ` | 1 | none |
| `OEE คืออะไร` | 2 | 1 search |
| `How much does training cost?` | 1 | none — declines, links the contact page |
| Broad topic | 2–3 | search plus a reading recommendation |

That change also solved multi-turn for free. Ask a question, then follow up with
`แล้วบทที่ 8 ล่ะ` — the model reads the history and issues a complete standalone
search query. No separate query-rewriting layer; the behaviour falls out of
giving the model the conversation and a search tool.

The cost is honest: turns that search take 4–6 seconds instead of 2–3, and
behaviour is less deterministic than a fixed pipeline.

## Two endpoints, on purpose

- **`POST /chat`** — the agent. What the widget talks to.
- **`POST /query`** — a plain pipeline: retrieve, then generate, once. No tools.

`/query` isn't dead code. When an answer looks wrong, it isolates retrieval from
the agent's judgment. That separation is what identified a request-encoding bug
that was silently mangling Thai before it reached the embedding model — the
agent path and the eval disagreed, and only a tool-free endpoint could show why.

Also `/health`, `/sources` and `/escalations`.

## Run it

```bash
pip install -e .
cp .env.example .env        # add your key
uvicorn app.api:app --port 8001
```

Needs Qdrant with a populated collection — see
[tpm-rag](../tpm-rag).

Talk to it with no browser, no HTTP, no CORS:

```bash
python cli.py
```

That imports `run_chat` directly, so it exercises the same agent the website
does with three fewer moving parts.

## Testing

Two suites, measuring different things.

```bash
python run_eval.py     # retrieval quality: 15 Thai questions, keyword scoring
python test_chat.py    # agent behaviour: 11 cases
```

`run_eval.py` tests `/query`, deliberately. It's a regression guard on
retrieval, not a measure of the product — the model, temperature and
`max_tokens` are byte-identical to the endpoint's so the comparison holds.

`test_chat.py` tests the decisions: does it skip retrieval on small talk, does
it ground TPM claims, does it carry context across turns, does it refuse to
escalate without a contact detail, does the guardrail stop an injection.

Both score by keyword containment, so a correct answer phrased unexpectedly
registers as a false failure. Read the reply before believing a `FAIL`.

## Grounding

The system prompt splits in two: a persona layer governing tone, and a
grounding rule scoped to *factual claims about TPM*. That split is what lets it
say good morning without also being free to invent a price.

```
- Greetings, thanks and small talk get a normal human reply. Do NOT search.
- ALWAYS call search_tpm_docs before stating any fact about TPM.
- The books are in Thai. ALWAYS pass `query` in Thai — translate if needed.
- Nothing about pricing, availability or company details. Point them at the
  contact page instead of guessing.
```

Worth stating plainly: **the routing logic is prose, not code.** The tool
`description` fields are read by the model at decision time. Rewriting one
changes behaviour with nothing to compile.

## Production notes

Exposed to the internet, so:

- Per-IP rate limiting, metered separately for `/chat` (2–4 model calls) and
  `/query` (1)
- A global daily turn cap — per-IP limits do nothing against distributed traffic
- Every user turn screened, not just the newest, since the client supplies the
  whole history array
- A 30-second OpenAI timeout, because a stalled connection otherwise holds a
  threadpool worker until the OS gives up
- `ALLOWED_ORIGINS` exact-matched; never `*`
- Structured logging: one line per turn with `searched`, `escalated` and latency

`searched=False` on a real question is the field to watch. It means the model
answered from memory rather than the documents — the failure mode that produces
confident, plausible, uncited nonsense.

## Known limitations

**Conversation history is client-supplied.** The browser sends the whole array,
so a crafted request can include forged assistant turns. Small blast radius —
no privileged data, no destructive tools — but the correct fix is server-side
session storage.

**No streaming.** A searching turn shows 4–6 seconds of a waiting state.

**Escalation is fire-and-forget.** Rows land in SQLite; nothing notifies anyone.

## Licence

MIT.
