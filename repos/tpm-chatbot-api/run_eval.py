import json
import os
import sys
from openai import OpenAI
from tpm_rag import QdrantStorage, embed_texts, fetch_width, maybe_rerank

from app.prompts import SYSTEM_PROMPT
    

client = OpenAI()



# The eval measures retrieval over the three books, which is what its 13/15
# baseline was established against. Once the website corpus was added, an
# unfiltered search meant 349 new chunks competing for the same five slots -
# a score change would then be ambiguous between "retrieval regressed" and
# "the corpus grew". Filtering keeps the guard honest.
#
# Note this measures a narrower path than /chat, which searches everything.
# It is a regression guard, not a measure of the product.
BOOK_SOURCES = ["tpm_lean_factory", "tpm_collab", "content_am"]


def run_query(question: str, top_k: int = 5, source_ids=BOOK_SOURCES) -> dict:
    query_vec = embed_texts([question])[0]
    # RERANK=1 makes this over-fetch and rerank, so the eval measures the same
    # retrieval the agent uses. Run it both ways to get an honest delta.
    found = QdrantStorage().search(query_vec, fetch_width(top_k), source_ids)

    # Retrieval returned nothing. The context block would be an empty string
    # and the model would be asked to answer from it - a paid call, once per
    # question, that cannot do anything but fail. That is the state you are in
    # when the collection is empty or the source filter matches nothing, and
    # it deserves to be said out loud rather than billed for.
    if not found["contexts"]:
        return {"answer": "", "contexts": [], "skipped": "empty context"}

    if len(found["contexts"]) > top_k:
        items = [
            {"text": c, "page": found["pages"][i], "source": found["sources"][i]}
            for i, c in enumerate(found["contexts"])
        ]
        items = maybe_rerank(question, items, top_k=top_k)
        found = {
            "contexts": [it["text"] for it in items],
            "pages": [it["page"] for it in items],
            "sources": [it["source"] for it in items],
        }

    context_lines = []
    for i, c in enumerate(found["contexts"]):
        page = found.get("pages", [])[i] if i < len(found.get("pages", [])) else None
        page_label = f" (หน้า {page})" if page else ""
        context_lines.append(f"- {c}{page_label}")
    context_block = "\n\n".join(context_lines)

    user_content = (
        "Use the following context to answer the question.\n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n"
        "Answer concisely using the context above."
    )

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1024,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    )
    return {"answer": res.choices[0].message.content.strip(), "contexts": found["contexts"]}


def check_keyword(expected, answer: str) -> bool:
    """expected can be a plain string (must appear) or a list of alternatives
    (at least one must appear)."""
    if isinstance(expected, list):
        return any(alt in answer for alt in expected)
    return expected in answer


def label_for(expected) -> str:
    if isinstance(expected, list):
        return " / ".join(expected)
    return expected


def main():
    with open("eval_q.json", "r", encoding="utf-8") as f:
        eval_set = json.load(f)

    passed = 0
    for item in eval_set:
        result = run_query(item["question"])
        answer = result["answer"]

        if result.get("skipped"):
            # No keyword can be present in an answer that was never generated.
            # Report the reason instead of a list of everything it "missed".
            missing = [f"skipped: {result['skipped']}"]
        else:
            missing = [label_for(kw) for kw in item["expected_keywords"] if not check_keyword(kw, answer)]
        status = "PASS" if not missing else "FAIL"
        if status == "PASS":
            passed += 1

        print(f"\n{'='*60}")
        print(f"[{status}] {item['question']}")
        print('='*60)
        print(f"Answer: {answer[:400]}")
        if missing:
            print(f"Missing expected keywords: {missing}")

    print(f"\n\n{passed}/{len(eval_set)} passed")

    # Exit code, so this can gate something. It used to only print, and
    # VERIFY.ps1 ignored $LASTEXITCODE anyway — between them, the check could
    # report 0/15 and still exit 0. A gate that cannot fail is not a gate.
    #
    # The threshold is the known-good baseline rather than "all must pass":
    # scoring is keyword containment, so two of these have always failed on
    # phrasing rather than on retrieval. Raise EVAL_MIN_PASS if you improve it;
    # do not lower it to make a red run green.
    # Return the code; do not sys.exit() here. main() is called directly by
    # test_run_eval.py, and raising SystemExit out of it turns a passing test
    # into an error. The process exit lives in __main__ where it belongs.
    minimum = int(os.getenv("EVAL_MIN_PASS", "13"))
    if passed < minimum:
        print(f"\nFAIL: {passed}/{len(eval_set)} is below the baseline of {minimum}.")
        print("Retrieval regressed, or the collection is not what you think it is.")
        print("Check per-source point counts before assuming it is the chunking.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())