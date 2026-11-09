"""Literal text search across every stored chunk. No embeddings involved.

    uv run python grep_chunks.py Sekine Arai
    uv run python grep_chunks.py "ผู้เขียน" --source tpm_lean_factory
    uv run python grep_chunks.py OEE --count-only

Answers the question vector search cannot: is this text in the collection at
all? A term that is absent is an ingestion or OCR problem. A term that is
present but never retrieved is an embedding or chunking problem. Those need
completely different fixes, and CLAUDE.md's diagnostic order - is the text
right, is the right chunk retrieved, did the model use it - depends on
separating them.

Read-only. Scrolls the collection and does plain substring matching.
"""

import argparse
import re
from collections import Counter

from qdrant_client.models import FieldCondition, Filter, MatchAny

from .store import QdrantStorage


def scroll_all(store: QdrantStorage, source: str | None):
    query_filter = None
    if source:
        query_filter = Filter(
            must=[FieldCondition(key="source", match=MatchAny(any=[source]))]
        )
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection,
            scroll_filter=query_filter,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            yield p.payload or {}
        if offset is None:
            break


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("terms", nargs="+", help="literal strings to look for")
    parser.add_argument("--source", help="restrict to one source_id")
    parser.add_argument("--ignore-case", action="store_true", default=True)
    parser.add_argument("--count-only", action="store_true")
    parser.add_argument("--chars", type=int, default=220)
    parser.add_argument("--max-hits", type=int, default=5, help="shown per term")
    args = parser.parse_args()

    store = QdrantStorage()
    total = store.client.get_collection(store.collection).points_count
    print(f"collection '{store.collection}': {total} points")
    if args.source:
        print(f"restricted to: {args.source}")

    hits = {t: [] for t in args.terms}
    by_source = {t: Counter() for t in args.terms}
    scanned = 0

    for payload in scroll_all(store, args.source):
        scanned += 1
        text = payload.get("text") or ""
        haystack = text.lower() if args.ignore_case else text
        for term in args.terms:
            needle = term.lower() if args.ignore_case else term
            if needle in haystack:
                by_source[term][payload.get("source", "?")] += 1
                if len(hits[term]) < args.max_hits:
                    hits[term].append(payload)

    print(f"scanned {scanned} chunks\n")

    for term in args.terms:
        found = sum(by_source[term].values())
        print("=" * 68)
        print(f"'{term}': {found} chunk(s)")
        print("=" * 68)
        if not found:
            print("  NOT PRESENT. Either it was never ingested, or OCR mangled it.")
            print("  Try a shorter fragment - OCR often drops or swaps characters.\n")
            continue
        for source, n in by_source[term].most_common():
            print(f"  {n:>5}  {source}")
        if args.count_only:
            print()
            continue
        for payload in hits[term]:
            text = re.sub(r"\s+", " ", payload.get("text") or "")
            idx = text.lower().find(term.lower())
            start = max(0, idx - args.chars // 3)
            print(f"\n  หน้า {payload.get('page', '?')}  [{payload.get('source', '?')}]")
            print(f"  ...{text[start:start + args.chars]}...")
        print()


if __name__ == "__main__":
    main()
