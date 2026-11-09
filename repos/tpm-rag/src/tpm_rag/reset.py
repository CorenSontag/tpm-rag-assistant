"""Delete the whole collection. Destructive, and requires typing a confirmation.

    tpm-rag-reset --list      # what exists, how many points
    tpm-rag-reset --yes       # actually delete

Use when a snapshot restore has gone wrong and you want a clean slate before
re-restoring or re-ingesting. For removing a single document, use
`tpm-rag-purge` instead - it deletes by `source` and leaves everything else.

Recovering from this needs either a snapshot or the source PDFs plus Tesseract
and roughly an hour of OCR. There is no undo.
"""

import argparse
import sys

from .store import QdrantStorage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="actually delete")
    parser.add_argument("--list", action="store_true", help="show and exit")
    args = parser.parse_args()

    store = QdrantStorage()
    name = store.collection

    try:
        info = store.client.get_collection(name)
        points = info.points_count
    except Exception:
        print(f"Collection '{name}' does not exist. Nothing to do.")
        return

    print(f"collection '{name}': {points} points")

    if args.list:
        return

    if not args.yes:
        print("\nAdd --yes to delete it. This cannot be undone.")
        print("To remove one document instead, use tpm-rag-purge.")
        return

    print(f"\nThis deletes all {points} points in '{name}'.")
    print("Recovery needs a snapshot, or the PDFs plus a full re-ingest.")
    if input(f"Type the collection name to confirm: ").strip() != name:
        sys.exit("Not confirmed. Nothing deleted.")

    store.client.delete_collection(collection_name=name)
    print(f"\nDeleted '{name}'.")
    print("Restore a snapshot, or run tpm-rag-ingest.")


if __name__ == "__main__":
    main()
