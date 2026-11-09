"""Ingest the TPM PDFs straight into Qdrant, without Inngest.

    uv run python ingest_direct.py --dry-run     # check paths first, costs nothing
    uv run python ingest_direct.py               # the real run
    uv run python ingest_direct.py --scan "path/to/pdfs"

Paths come from documents.json, so filenames with awkward spacing never have
to be retyped at a shell prompt.

Durable workflow engines are the right tool for production ingestion - OCR
runs for minutes and a crash halfway is expensive - but they need a dev server
and an app running before you can trigger anything. When you just need data in
the collection, that is several moving parts too many.

Point ids are deterministic:
  - ids   uuid5(NAMESPACE_URL, f"{source_id}:{i}")   -- positional, so a
          re-run overwrites rather than duplicating
  - payload {"source": source_id, "text": chunk, "page": page}
  - same load_and_chunk_pdf, same embed_texts, same QdrantStorage.upsert

WARNING: point ids are positional. If chunk_size,
chunk_overlap or the OCR cleaner ever change, a re-run leaves the previous
run's surplus points orphaned rather than overwritten, and stale text stays
searchable forever. Pair any such change with clear_col.py and a full re-ingest.
"""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from .loader import embed_texts, load_and_chunk_pdf
from .store import QdrantStorage


def read_config(path: str) -> list[dict]:
    """Read the document list, tolerating markdown code fencing.

    {"pdfs": [{"pdf_path": "...", "source_id": "..."}]}
    """
    raw = Path(path).read_text(encoding="utf-8-sig")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    data = json.loads(raw)
    payload = data.get("data", data)
    if "pdfs" in payload:
        return payload["pdfs"]
    return [{"pdf_path": payload["pdf_path"], "source_id": payload.get("source_id")}]


def check_tesseract() -> str:
    try:
        import pytesseract

        return str(pytesseract.get_tesseract_version())
    except Exception as e:
        return f"NOT AVAILABLE ({e})"


def ingest_one(pdf_path: str, source_id: str, store: QdrantStorage) -> int:
    chunks, pages = load_and_chunk_pdf(pdf_path)
    if not chunks:
        print(f"  no chunks produced for {source_id}")
        return 0

    pages = pages or [None] * len(chunks)
    vecs = embed_texts(chunks)
    ids = [
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{i}"))
        for i in range(len(chunks))
    ]
    payloads = [
        {"source": source_id, "text": chunks[i], "page": pages[i]}
        for i in range(len(chunks))
    ]
    store.upsert(ids, vecs, payloads)
    return len(chunks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="documents.json",
                        help="JSON file listing the PDFs to ingest")
    parser.add_argument("--dry-run", action="store_true", help="check only, spend nothing")
    parser.add_argument("--scan", help="list PDFs in a folder and exit")
    parser.add_argument("--only", help="ingest a single source_id")
    args = parser.parse_args()

    if args.scan:
        folder = Path(args.scan)
        if not folder.is_dir():
            sys.exit(f"not a folder: {folder}")
        found = sorted(folder.glob("*.pdf"))
        print(f"{len(found)} PDFs in {folder}\n")
        for p in found:
            print(f"  {p.name}")
            print(f"    {p}")
        return

    try:
        entries = read_config(args.config)
    except FileNotFoundError:
        sys.exit(f"{args.config} not found - run this from the repository root")
    except json.JSONDecodeError as e:
        sys.exit(f"{args.config} is not valid JSON: {e}")

    if args.only:
        entries = [e for e in entries if e.get("source_id") == args.only]
        if not entries:
            sys.exit(f"no entry with source_id {args.only}")

    print(f"tesseract: {check_tesseract()}")
    print(f"{len(entries)} documents listed in {args.config}\n")

    missing = []
    for entry in entries:
        path = Path(entry["pdf_path"])
        exists = path.is_file()
        size = f"{path.stat().st_size / 1_048_576:.1f} MB" if exists else "-"
        print(f"  [{'ok ' if exists else 'MISSING'}] {entry.get('source_id')}  ({size})")
        print(f"      {path}")
        if not exists:
            missing.append(path)

    if missing:
        print(f"\n{len(missing)} file(s) not found.")
        print("Filenames contain double spaces - find the real ones with:")
        print('  uv run python ingest_direct.py --scan "path/to/pdfs"')
        print("then correct the paths in invoke.md.")
        sys.exit(1)

    if args.dry_run:
        print("\nDry run - every path resolves. Re-run without --dry-run to ingest.")
        return

    store = QdrantStorage()
    before = store.client.get_collection(store.collection).points_count
    print(f"\ncollection '{store.collection}' before: {before} points\n")

    total = 0
    for entry in entries:
        source_id = entry.get("source_id") or entry["pdf_path"]
        print(f"--- {source_id} ---")
        total += ingest_one(entry["pdf_path"], source_id, store)

    after = store.client.get_collection(store.collection).points_count
    print(f"\nchunks written: {total}")
    print(f"collection after: {after} points (was {before})")
    print("\nNow: uv run python run_eval.py")


if __name__ == "__main__":
    main()
