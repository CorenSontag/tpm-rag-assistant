"""tpm-rag — retrieval over Thai-language scanned PDFs.

The knowledge layer: OCR, chunking, embedding, storage and search. It knows
nothing about chat, HTTP or agents — those live in the consumer.

    from tpm_rag import QdrantStorage, embed_texts, load_and_chunk_pdf

    store = QdrantStorage()
    vec = embed_texts(["OEE คืออะไร"])[0]
    hits = store.search(vec, top_k=5, source_ids=["tpm_lean_factory"])

Everything is Thai-first. OCR runs `tha+eng`, the text cleaner depends on the
Thai Unicode range, and chapter headings are detected from Thai patterns such
as `บทที่ N`. It will work on English documents, but the tuning assumes Thai.
"""

from .embed import EMBED_DIM, EMBED_MODEL, embed_texts
from .rerank import fetch_width, maybe_rerank
from .store import QdrantStorage

__version__ = "0.1.0"

__all__ = [
    "QdrantStorage",
    "embed_texts",
    "load_and_chunk_pdf",
    "EMBED_MODEL",
    "EMBED_DIM",
    "maybe_rerank",
    "fetch_width",
]

# `load_and_chunk_pdf` is resolved on first attribute access, not at import.
#
# It lives in loader.py, which imports fitz, pytesseract, PIL and
# llama_index.core at module scope. Importing it eagerly here meant that
# `import tpm_rag` - which every serving process does, to get QdrantStorage
# and embed_texts - dragged the whole OCR toolchain in and into the image:
# 919 MB, re-paid on every Container Apps cold start for code that only ever
# runs during ingest.
#
# Same deal as rerank.py's `_get_model`, which imports sentence_transformers
# inside the function so torch is not a hard dependency of serving. The cost of
# deferring is that a missing ingestion dependency now surfaces as an
# ImportError on first use rather than at import - which is why the ingest
# extra exists and why setup/2-SETUP.ps1 installs it.
#
# `from tpm_rag import load_and_chunk_pdf` still works: PEP 562 routes the
# miss here. Do not "fix" this back into a top-level import. `from tpm_rag
# import *` does defeat it - it resolves every name in __all__ - so import by
# name on the serving path, which app/ already does.
_LAZY = {"load_and_chunk_pdf": "loader"}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value  # resolved once; later lookups skip __getattr__
    return value
