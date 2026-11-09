"""Retrieval-only inspector - see what comes back before generation muddies it.

    uv run python inspect_ret.py "OEE คืออะไร ใช้วัดอะไร และประกอบด้วยองค์ประกอบกี่ส่วน"
    uv run python inspect_ret.py "5ส คืออะไร" --source content_am
    uv run python inspect_ret.py --eval 3

Read-only. Touches nothing, changes nothing, calls no chat model.

Why it exists: CHATBOT_HANDOFF and CLAUDE.md both say to separate "is the right
chunk retrieved" from "did the model use it". check_ret.py does this already but
with a hardcoded question and no scores. This queries Qdrant directly so the
similarity score is visible - that number is what tells you whether a weak
answer is a retrieval problem or a generation problem.

Rough reading of the score: above ~0.5 is a solid match, 0.35-0.5 is marginal,
below ~0.3 usually means the query was too short or the topic is not covered.
"""

import argparse
import json
import sys

from qdrant_client.models import FieldCondition, Filter, MatchAny

from .embed import embed_texts  # .embed, not .loader: no OCR deps needed to inspect
from .store import QdrantStorage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", help="question to embed and search")
    parser.add_argument("--source", help="restrict to one source_id")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--eval", type=int, help="use question N from eval_q.json")
    parser.add_argument("--chars", type=int, default=300, help="chunk preview length")
    args = parser.parse_args()

    if args.eval is not None:
        with open("eval_q.json", encoding="utf-8-sig") as f:
            question = json.load(f)[args.eval]["question"]
    elif args.question:
        question = args.question
    else:
        sys.exit("Give a question, or --eval N")

    store = QdrantStorage()

    info = store.client.get_collection(store.collection)
    print(f"collection '{store.collection}': {info.points_count} points")

    query_filter = None
    if args.source:
        query_filter = Filter(
            must=[FieldCondition(key="source", match=MatchAny(any=[args.source]))]
        )
        print(f"filtered to source: {args.source}")

    print(f"\nquestion ({len(question)} chars): {question}")
    if len(question) < 25:
        print("  ^ short queries embed vaguely and match generic pages. "
              "Compare against a fuller phrasing before concluding anything.")

    vec = embed_texts([question])[0]

    response = store.client.query_points(
        collection_name=store.collection,
        query=vec,
        query_filter=query_filter,
        with_payload=True,
        limit=args.top_k,
    )

    if not response.points:
        print("\nNo points returned at all.")
        return

    for i, point in enumerate(response.points, 1):
        payload = getattr(point, "payload", None) or {}
        text = (payload.get("text") or "").replace("\n", " ")
        print(f"\n{'=' * 68}")
        print(
            f"[{i}] score {point.score:.4f}   "
            f"source {payload.get('source', '?')}   "
            f"หน้า {payload.get('page', '?')}"
        )
        print("=" * 68)
        print(text[: args.chars] + ("…" if len(text) > args.chars else ""))

    scores = [p.score for p in response.points]
    print(f"\n{'-' * 68}")
    print(f"best {max(scores):.4f}   worst {min(scores):.4f}")
    if max(scores) < 0.35:
        print("Nothing scored well. Either the topic is not in the corpus, or the "
              "question is too short or phrased unlike the source text.")


if __name__ == "__main__":
    main()
