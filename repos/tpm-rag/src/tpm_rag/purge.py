"""Delete every point belonging to one source_id. Targeted, unlike clear_col.py.

    uv run python purge_source.py --list
    uv run python purge_source.py "C:\\Users\\K\\Downloads\\dc-608_milking_fresh_cows.pdf"

Use when a stray document got ingested and you want it gone without wiping the
collection. clear_col.py deletes everything and forces a full re-ingest - which,
with OCR over hundreds of scanned pages, is not a small ask.

Requires --yes to actually delete. Deletion is not reversible.
"""

import argparse
import sys
from collections import Counter

from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

from .store import QdrantStorage


def list_sources(store: QdrantStorage) -> Counter:
    """Scroll the whole collection and tally the `source` payload field."""
    counts = Counter()
    offset = None
    while True:
        points, offset = store.client.scroll(
            collection_name=store.collection,
            limit=1000,
            offset=offset,
            with_payload=["source"],
            with_vectors=False,
        )
        for p in points:
            counts[(p.payload or {}).get("source", "<none>")] += 1
        if offset is None:
            break
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", help="source_id to delete")
    parser.add_argument("--list", action="store_true", help="show sources and counts")
    parser.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()

    store = QdrantStorage()
    total = store.client.get_collection(store.collection).points_count
    print(f"collection '{store.collection}': {total} points\n")

    counts = list_sources(store)
    for source, n in counts.most_common():
        print(f"  {n:>7}  {source}")

    if args.list or not args.source:
        if not args.list:
            print("\nPass a source_id to delete one, or --list to just look.")
        return

    if args.source not in counts:
        sys.exit(f"\nNo points with source '{args.source}'. Copy it exactly from above.")

    n = counts[args.source]
    if not args.yes:
        print(f"\nWould delete {n} points with source '{args.source}'.")
        print("Add --yes to do it.")
        return

    store.client.delete(
        collection_name=store.collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=args.source))]
            )
        ),
    )
    after = store.client.get_collection(store.collection).points_count
    print(f"\ndeleted {n} points. collection now: {after} (was {total})")


if __name__ == "__main__":
    main()
