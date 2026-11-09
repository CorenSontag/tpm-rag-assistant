# TPM RAG Assistant

A production Thai-language RAG chatbot: a FastAPI tool-calling agent over a
Qdrant vector store of OCR'd Thai manufacturing manuals, with a vanilla-JS
widget that mounts on a WordPress site.

Built for a Total Productive Maintenance consultancy. The corpus is three
copyrighted Thai manuals and is **not** included here — see
[Corpus](#corpus-not-included).

```
visitor → widget → FastAPI /chat → agent tool-loop ─┬─ search_tpm_docs → Qdrant
                                                    ├─ recommend_reading
                                                    └─ escalate_to_human → SQLite + webhook
```

## Retrieval quality, measured

Dense search, `text-embedding-3-large` (3072-dim), 1,764 passages, on a
44-question hand-labelled Thai benchmark built for this repo
(`eval_recall.py`):

| Relevance basis | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| exact labelled chunk | 31.8% | 52.3% | 56.8% | 0.418 |
| same source + page | 38.6% | 61.4% | 63.6% | 0.489 |

Both are reported because the number moves seven points between them and the
choice is a judgement call — the harness prints which one it used. Gold labels
are a deliberate **lower bound**: a miss means the known-good chunk was not
surfaced, not that nothing useful was. Measurement is retrieval-only, so a full
run is one embedding call per question and no completions.

The question set itself is derived from the copyrighted manuals and is not
published. `eval_recall.py` is.

## Engineering notes

The parts that were less obvious than they looked.

**The agent is a `for` loop, not a framework.** Up to four rounds: ask the
model with tools attached, run any tool calls, append results, ask again. A
greeting costs one call and zero searches. Multi-turn query rewriting falls out
free, because the model sees the history. There is no classifier and no keyword
list — routing is the prompt plus the tool descriptions, which means a
`description` edit is a behaviour change with nothing to compile.

**64 offline tests that cost nothing.** The LLM is mocked and the vector store
is faked, so prompt-injection screening, CORS matching, rate limits, the daily
cap and the admin-token guard all run in CI on every push with no key and no
spend. The two paid suites are deliberately excluded from `pytest`'s collection
so a bare `pytest` can never bill the account.

**Cold start survives a vector-store outage.** `QdrantStorage.__init__` makes a
network call, and two modules used to build one at import — so an unreachable
Qdrant killed the process *before* uvicorn bound its socket. On a scale-to-zero
host that turns a transient outage into a crash loop with a failing health
probe. The store is now built on first use. Measured in the container both
ways: the pre-change image exits 1 on `Connection refused`; the current one
stays up and serves `/health`.

**`/health` tells two failures apart.** `get_collection` raises identically for
a refused socket and for a missing collection, and the serving path
deliberately never creates the collection — so "collection not found, run
ingest" is a real state a fresh deployment is in. Reporting that as
"unreachable" sends an operator to debug a firewall that is fine.

**A retrieval outage degrades instead of failing the turn.** The search helper
returns `None`, kept distinct from `[]`: empty means "the search ran and the
documents don't cover this", `None` means "the search could not run". Collapse
them and a database blip has the bot confidently telling people their question
isn't in the manuals when nobody looked.

**The image went from 919 MB to 410 MB** by fixing one import. The retrieval
package's `__init__` eagerly imported the OCR module, which pulls PyMuPDF,
pytesseract, Pillow and llama-index at module scope — so every serving process
carried the entire OCR toolchain to call `embed_texts`. Embedding moved to its
own module, PDF loading is deferred behind a PEP 562 `__getattr__`, and the
heavy packages are an `[ingest]` extra the container does not install. The
guard test asserts the heavy packages *are installed* in CI, because
"`fitz` is not in `sys.modules`" is vacuously true where fitz was never there.

**Backups that restore.** Qdrant's snapshot writer treats a file whose
`st_blocks` is 0 as entirely a hole and writes zeros without reading it, and
Docker Desktop's 9p bind mount reports `st_blocks == 0` for every small
NTFS-resident file. Snapshots therefore contained correct-length, all-zero JSON
members: a valid tar that read cleanly and would not restore. `corpus-backup.py`
goes through the HTTP scroll/upsert API instead, carries the collection schema,
and is verified by restoring into a clean container and comparing ids *and*
scores against the live store.

## Thai-specific invariants

None of these raise when broken; they just quietly halve answer quality.

- The agent must query in **Thai** even when the visitor writes English. That
  instruction lives in the prompt.
- Chunks carry a bracketed chapter heading prefix, prepended at ingest, which
  is load-bearing for chapter-scoped questions. It is stripped for display
  only, never upstream.
- OCR needs the `tessdata_best` Thai model. The standard model inserts spaces
  between Thai characters, which never matches the heading patterns — and
  degrades the whole corpus with no error.
- Responses force a UTF-8 charset, because PowerShell 5.1 otherwise decodes
  Thai as ISO-8859-1 and shows mojibake.
- Citations dedupe on `(source, page, url)`. Website chunks all share one
  source and have no page, so a `(source, page)` key collapses them into one.

## Corpus, not included

The three Thai manuals, the OCR'd passages, the Qdrant snapshots and the
benchmark question set are copyrighted and are not in this repository. The code
reads a `documents.json` you point at your own PDFs — see
`config/documents.example.json`.

## Running the free tests

```bash
cd repos && pip install "./tpm-rag[ingest]" && pip install "./tpm-chatbot-api[dev]"
cd tpm-chatbot-api
pytest                                      # 64 tests, LLM mocked, no spend
python -m unittest test_agent_loop test_api test_run_eval test_tools_escalation
python -m unittest test_pacing
```

`run_eval.py`, `eval_recall.py` and `test_chat.py` call real models and cost
money. CI runs only the free suites.

## Layout

| Path | What |
|---|---|
| `repos/tpm-rag` | retrieval: OCR → chunk → embed → Qdrant, plus grep/inspect/purge diagnostics |
| `repos/tpm-chatbot-api` | FastAPI service: agent loop, tool definitions, prompts, escalations |
| `repos/tpm-chat-widget` | single-file vanilla-JS widget |
| `setup/` | bring-up scripts, integrity manifest, corpus backup/restore |
| `docs/` | architecture pointer and the website integration handoff |

Architecture detail, including the list of traps that look like cleanup
opportunities and are not, is in
[`repos/tpm-chatbot-api/ARCHITECTURE.md`](repos/tpm-chatbot-api/ARCHITECTURE.md).

## Stack

Python 3.12, FastAPI, OpenAI (`gpt-4o-mini`, `text-embedding-3-large`), Qdrant,
PyMuPDF + Tesseract for OCR, llama-index for sentence splitting,
`bge-reranker-v2-m3` wired but off by default, Docker, GitHub Actions.

MIT licensed.
