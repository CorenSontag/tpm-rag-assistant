"""Cross-encoder reranking. Optional, off by default, degrades to a no-op.

Dense retrieval scores a question against a *single* vector per chunk, which
was computed without ever seeing the question. A cross-encoder reads the pair
together and scores the actual match. It is far more accurate and far too slow
to run over a whole collection — so the pattern is: retrieve widely with
vectors, rerank narrowly with the cross-encoder.

    RERANK=1               enable
    RERANK_MULTIPLIER=4    fetch top_k * 4, rerank down to top_k

`bge-reranker-v2-m3` runs locally and handles Thai, so this costs no API spend
per query. It does cost a 1-2 GB model download on first use and roughly
100-400 ms per query on CPU.

Requires the optional extra:

    pip install "tpm-rag[rerank]"

Without it, or with RERANK unset, `maybe_rerank` returns the input untouched.
That is deliberate: a missing optional dependency must never take retrieval
down, and enabling it must be an explicit act so an A/B stays honest.
"""

import os

_model = None
_state = "unloaded"  # unloaded | ready | unavailable

MODEL_NAME = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def enabled() -> bool:
    return os.getenv("RERANK", "").strip().lower() in ("1", "true", "yes", "on")


def multiplier() -> int:
    try:
        return max(1, int(os.getenv("RERANK_MULTIPLIER", "4")))
    except ValueError:
        return 4


def _get_model():
    """Load once. Returns None if the extra is not installed."""
    global _model, _state
    if _state == "ready":
        return _model
    if _state == "unavailable":
        return None
    try:
        from sentence_transformers import CrossEncoder
    except ImportError:
        _state = "unavailable"
        print("[rerank] sentence-transformers not installed; "
              'reranking disabled. pip install "tpm-rag[rerank]"')
        return None
    print(f"[rerank] loading {MODEL_NAME} (first call downloads ~1-2 GB)...")
    try:
        _model = CrossEncoder(MODEL_NAME)
    except Exception as e:
        _state = "unavailable"
        print(f"[rerank] could not load the model, continuing without it: {e}")
        return None
    _state = "ready"
    print("[rerank] ready")
    return _model


def rerank_items(question, items, top_k=5, text_key="text"):
    """Reorder `items` by cross-encoder relevance. Falls back to the input."""
    if not items:
        return items
    model = _get_model()
    if model is None:
        return items[:top_k]

    pairs = [[question, str(it.get(text_key, ""))] for it in items]
    try:
        scores = model.predict(pairs)
    except Exception as e:
        print(f"[rerank] scoring failed, using vector order: {e}")
        return items[:top_k]

    ranked = sorted(zip(items, scores), key=lambda pair: pair[1], reverse=True)
    out = []
    for item, score in ranked[:top_k]:
        item = dict(item)
        item["rerank_score"] = float(score)
        out.append(item)
    return out


def maybe_rerank(question, items, top_k=5, text_key="text"):
    """Rerank only when RERANK is set. Otherwise truncate and return."""
    if not enabled():
        return items[:top_k]
    return rerank_items(question, items, top_k=top_k, text_key=text_key)


def fetch_width(top_k: int) -> int:
    """How many to pull from the vector store before reranking."""
    return top_k * multiplier() if enabled() else top_k
