"""Measure recall@k on a labelled Thai question set. Retrieval only.

WHY THIS EXISTS, SEPARATELY FROM run_eval.py

`run_eval.py` does not measure recall, despite being widely described as the
retrieval gate. It retrieves top-5, generates an answer with gpt-4o-mini, and
checks whether `expected_keywords` appear in that ANSWER. Its 13/15 baseline is
an end-to-end answer-keyword pass rate: a correct answer phrased unexpectedly
scores FAIL, and a wrong answer that happens to contain the keyword scores
PASS. The string "recall" appears nowhere in it.

So the resume figure "recall@5 38% -> 78% on a 44-question Thai test set" could
not be reproduced from this repository by any means. This file is the harness
that makes a recall number real, and the one it prints is the only one that may
be quoted.

WHAT IT MEASURES

For each question the gold evidence is an explicit list of chunk ids - the
chunks that actually contain the answer, labelled by hand while reading the
corpus. recall@k is the fraction of questions for which at least one gold chunk
appears in the top k. No model judges anything; no keyword matching is
involved.

Gold labels are a LOWER BOUND on relevance. A question is labelled with the
chunks confirmed to answer it, and the corpus may well hold others that also
would. So a miss is not proof the retrieved passages were useless - it is proof
the known-good one was not surfaced. Reported recall is therefore pessimistic,
which is the right direction for a number that goes on a CV.

COST

One embedding call per question (text-embedding-3-large) and nothing else. No
chat completions. Running the full set costs a fraction of a cent, which is
what makes it safe to re-run on every retrieval change - unlike run_eval.py.

SCOPE

Restricted to BOOK_SOURCES, matching run_eval.py, so the two are measuring the
same corpus slice. The 349 `website` chunks are excluded.

The question set is derived from copyrighted books. It is fine in this private
repo; it must NOT travel to a public one. See the note in eval_recall.json.

    python eval_recall.py                 # recall@1/@3/@5 over the whole set
    python eval_recall.py --k 10          # also report @10
    python eval_recall.py -v              # per-question detail
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from tpm_rag import QdrantStorage, embed_texts

load_dotenv()

BOOK_SOURCES = ["tpm_lean_factory", "tpm_collab", "content_am"]
QUESTION_FILE = "eval_recall.json"


def _search_ids(store, vec, k):
    """Top-k point ids for a query vector, book sources only.

    Goes to the client directly rather than through QdrantStorage.search: that
    returns payload fields positionally and never exposes the point id, and the
    id is the whole ground truth here.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    res = store.client.query_points(
        collection_name=store.collection,
        query=vec,
        query_filter=Filter(
            must=[FieldCondition(key="source", match=MatchAny(any=BOOK_SOURCES))]
        ),
        with_payload=True,
        limit=k,
    )
    return [(str(p.id), p.payload or {}) for p in res.points]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="largest k to report")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--file", default=QUESTION_FILE)
    ap.add_argument(
        "--relevance", choices=["chunk", "page"], default="chunk",
        help="chunk: only the labelled chunk counts (strict, the default). "
             "page: any chunk from the same source and page counts. Both are "
             "defensible; report which one you used. Chunking is positional, "
             "so one page spans several chunks and an answer can legitimately "
             "sit in the neighbour of the one that was labelled.",
    )
    a = ap.parse_args(argv)

    with open(a.file, encoding="utf-8") as f:
        payload = json.load(f)
    questions = payload["questions"] if isinstance(payload, dict) else payload

    if not questions:
        print("no questions in", a.file)
        return 1

    store = QdrantStorage(create_if_missing=False)
    ks = sorted({1, 3, a.k})
    hits = {k: 0 for k in ks}
    rr_total = 0.0

    for n, item in enumerate(questions, 1):
        gold = set(item["gold_chunk_ids"])
        vec = embed_texts([item["question"]])[0]
        found = _search_ids(store, vec, max(ks))
        if a.relevance == "chunk":
            ok = lambda pid, pay: pid in gold
        else:
            ok = lambda pid, pay: (
                pay.get("source") == item.get("gold_source")
                and pay.get("page") == item.get("page")
            )
        ranks = [i + 1 for i, (pid, pay) in enumerate(found) if ok(pid, pay)]
        best = min(ranks) if ranks else None

        for k in ks:
            if best is not None and best <= k:
                hits[k] += 1
        rr_total += (1.0 / best) if best else 0.0

        if a.verbose or best is None:
            status = f"rank {best}" if best else "MISS"
            print(f"\n[{n:>2}] {status:>8}  {item['question']}")
            if best is None:
                print(f"       gold: {sorted(gold)}")
                for i, (pid, pl) in enumerate(found[:3], 1):
                    print(f"       got {i}: {pid} p.{pl.get('page')} {pl.get('source')}")

    total = len(questions)
    print(f"\n{'=' * 60}")
    basis = ("exact labelled chunk" if a.relevance == "chunk"
             else "any chunk on the labelled source+page")
    print(f"{total} questions, {len(BOOK_SOURCES)} book sources, relevance = {basis}")
    for k in ks:
        print(f"  recall@{k:<3} {hits[k]}/{total} = {100.0 * hits[k] / total:.1f}%")
    print(f"  MRR      {rr_total / total:.3f}")
    print("\nGold labels are a lower bound: a MISS means the known-good chunk was")
    print("not in the top k, not that nothing useful was retrieved.")

    minimum = int(os.getenv("RECALL_MIN_PERCENT", "0"))
    got = 100.0 * hits[a.k] / total
    if minimum and got < minimum:
        print(f"\nFAIL: recall@{a.k} {got:.1f}% is below the {minimum}% floor.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
