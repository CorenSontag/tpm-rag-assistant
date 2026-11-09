# tpm-rag

Retrieval over Thai-language scanned PDFs. OCR, chunking, embedding, vector
search — and the diagnostic tools you need when retrieval goes wrong, which it
will.

Built for a corpus of three Thai manuals on Total Productive Maintenance:
1,415 chunks across 900-odd pages, none of which had a text layer.

```python
from tpm_rag import QdrantStorage, embed_texts

store = QdrantStorage()
vec = embed_texts(["OEE คืออะไร ใช้วัดอะไร"])[0]
hits = store.search(vec, top_k=5, source_ids=["tpm_lean_factory"])
```

## Why this exists separately

The retrieval layer knows nothing about chat, HTTP or agents. That boundary is
deliberate: it means the same index can serve a chatbot, a search page or an
offline evaluation without any of them knowing about each other. The
conversational agent lives in
[tpm-chatbot-api](../tpm-chatbot-api).

## Thai is not an afterthought here

Most RAG tutorials assume English. Thai breaks several assumptions at once, and
the handling is spread through the code rather than bolted on:

- **No spaces between words.** Naive word-boundary chunking produces nonsense,
  so chunking is character-based with sentence-aware boundaries.
- **Stacked diacritics.** Text renders wrong without adequate line-height —
  which is a downstream UI concern, but it starts here in how text is preserved.
- **OCR runs `tha+eng`.** The manuals mix Thai prose with English terms like
  `OEE` and `Availability`. Single-language OCR mangles one or the other.
- **The cleaner depends on the Thai Unicode range.** `_clean_ocr_text` scores
  each line by the ratio of real characters to noise, where "real" includes
  `฀-๿`. Change that regex and the filter stops working.
- **Chapter headings are Thai patterns** — `บทที่ N`, `ขั้นตอนที่ N`.

## Install

```bash
pip install -e .            # search and embed: what a serving process needs
pip install -e ".[ingest]"  # + OCR and chunking: what ingesting a PDF needs
```

The OCR toolchain is an extra rather than a required dependency because it is
heavy — PyMuPDF, pytesseract, Pillow and llama-index-core were most of a 919 MB
container image, re-pulled on every scale-from-zero cold start, for code a
serving process never calls. `tpm_rag/__init__.py` imports `loader` lazily to
match, so a bare install is a complete retrieval client: `QdrantStorage`,
`embed_texts`, `maybe_rerank`, and the `grep` / `inspect` / `purge` / `reset`
commands all work. Only `tpm-rag-ingest` needs `[ingest]`, and without it says
so by failing on `import fitz`.

Needs Qdrant, and Tesseract with Thai data if your PDFs are scans.

```bash
docker run -p 6333:6333 -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant
```

> Use an **absolute** path for that volume. A relative `./qdrant_storage`
> resolves against your shell's working directory, and Docker silently creates
> an empty one if you run it from elsewhere — which looks exactly like a
> corrupted index.

Set what you need:

```bash
OPENAI_API_KEY=sk-...
QDRANT_URL=http://localhost:6333     # optional
QDRANT_COLLECTION=docs               # optional
TESSERACT_CMD=/opt/homebrew/bin/tesseract   # only if not on PATH
```

## Ingest

Describe your documents:

```json
{
  "pdfs": [
    {"pdf_path": "path/to/book.pdf", "source_id": "tpm_lean_factory"}
  ]
}
```

```bash
tpm-rag-ingest --dry-run     # verify paths and Tesseract, costs nothing
tpm-rag-ingest               # the real run
tpm-rag-ingest --scan ~/Downloads   # find PDFs and their exact paths
```

Always dry-run first. OCR over hundreds of scanned pages takes many minutes and
a typo in a path shouldn't be discovered on minute nine.

## Diagnostics

These exist because retrieval failures are ambiguous by nature. A bad answer
could be missing text, bad ranking, or the model ignoring what it was given —
and the fixes are completely different. Each tool isolates one layer.

```bash
tpm-rag-grep Sekine Arai            # is the text in the collection at all?
tpm-rag-inspect "OEE คืออะไร" --top-k 20   # what ranks, and with what score?
tpm-rag-purge --list                # what sources exist, how many points each
```

Use them in that order. `grep` answers a question vector search cannot: a term
that is absent is an ingestion or OCR problem; a term that is present but never
retrieved is an embedding or chunking problem.

While debugging the original corpus, `tpm-rag-grep` found the author names
present in 17 chunks while search returned worksheet pages instead — which
ruled out ingestion in one command and saved a pointless re-ingest.

## Design notes

**Point ids are deterministic.** `uuid5(NAMESPACE_URL, f"{source_id}:{i}")`, so
re-ingesting overwrites rather than duplicating.

The catch: that only holds while chunk count and order hold. Change
`chunk_size`, `chunk_overlap` or the cleaner, and the previous run's surplus
points are **orphaned, not overwritten** — stale text stays searchable forever,
silently. Pair any such change with a collection reset and a full re-ingest.

**Embedding dimension is fixed at collection creation.** `EMBED_DIM` is 3072 for
`text-embedding-3-large`. Qdrant rejects mismatched vectors, so changing model
means changing the dimension *and* recreating the collection.

**Chapter headings are prepended to chunks.** `_extract_heading` finds the
current heading and prefixes every chunk with it, which makes chapter-scoped
questions answerable. The trade-off is real: the heading persists until a new
one is found, so pages hundreds later can inherit a stale prefix, and many
unrelated chunks end up sharing identical opening text.

**Vectors are normalised at creation**, so cosine similarity is a dot product.

## Not done

- `reranker.py` (a local `bge-reranker-v2-m3` cross-encoder) is written but not
  wired into `search()`. Retrieve `top_k * 4`, rerank down — the highest-value
  retrieval improvement still available, and it costs nothing per query since
  it runs locally.
- No hybrid search. Pure dense retrieval struggles with rare exact terms like
  worksheet numbers.
- No payload index on `source`, so filtering scans. Irrelevant at this size.

## Licence

MIT.
