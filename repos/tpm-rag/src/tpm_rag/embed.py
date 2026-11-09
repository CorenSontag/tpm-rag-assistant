"""Embedding: the one piece of the pipeline the serving path actually calls.

Split out of loader.py, which it used to live in. The reason is weight, not
tidiness: `loader.py` imports fitz, pytesseract, PIL and llama_index at module
scope, so a serving process that only ever embeds a query was importing the
whole OCR toolchain and carrying it in the image - 919 MB, paid on every
Container Apps cold start. Nothing here imports anything heavier than the
OpenAI client.

Keep it that way. If this module ever needs an ingestion-side import, it
belongs in loader.py instead.

`load_dotenv()` runs before OpenAI() because the client reads OPENAI_API_KEY at
construction. That is the only reason it is here. An earlier version of this
docstring claimed app/config.py depended on this call; it does not - it calls
load_dotenv() itself, and the two resolve different .env files (this one the
bundle root's, config.py's the API package's).
"""

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 3072


def embed_texts(texts: list[str]) -> list[list[float]]:
    all_embeddings = []
    batch_size = 100

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        response = client.embeddings.create(
            model=EMBED_MODEL,
            input=batch,
        )
        all_embeddings.extend(item.embedding for item in response.data)
        print(f"[embed_texts] embedded {min(i + batch_size, len(texts))}/{len(texts)} chunks")

    return all_embeddings
