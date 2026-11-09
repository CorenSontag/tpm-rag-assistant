import os
from typing import List, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchAny


class QdrantStorage:
    def __init__(self, url=None, collection=None, dim=3072, create_if_missing=True):
        url = url or os.getenv("QDRANT_URL", "http://localhost:6333")
        collection = collection or os.getenv("QDRANT_COLLECTION", "docs")
        self.client = QdrantClient(url=url, timeout=80)
        self.collection = collection
        # create_if_missing=False is what the serving path passes, for two
        # reasons. It must never create the collection: a typo in
        # QDRANT_COLLECTION would otherwise produce an empty 3072-dim
        # collection and a bot that confidently finds nothing for every
        # question asked of it. (This closes the typo route only - a
        # correctly-named empty collection still reports healthy. See
        # app/store.py.)
        # And collection_exists() is the only network call in this
        # constructor, so skipping it is what lets the API process start while
        # Qdrant is unreachable instead of exiting 1 before uvicorn binds
        # (see app/store.py and tests/test_cold_start.py).
        # Ingestion keeps the default - it genuinely needs the collection made.
        if create_if_missing and not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert(self, ids: List[str], vectors: List[List[float]], payloads: List[dict], batch_size: int = 100):
        total = len(ids)
        for i in range(0, total, batch_size):
            batch_ids = ids[i:i + batch_size]
            batch_vectors = vectors[i:i + batch_size]
            batch_payloads = payloads[i:i + batch_size]

            points = [
                PointStruct(id=batch_ids[j], vector=batch_vectors[j], payload=batch_payloads[j])
                for j in range(len(batch_ids))
            ]
            self.client.upsert(self.collection, points=points)
            print(f"[QdrantStorage] upserted {min(i + batch_size, total)}/{total} points")

    def search(self, query_vector, top_k: int = 5, source_ids: Optional[List[str]] = None):
        query_filter = None
        if source_ids:
            query_filter = Filter(
                must=[FieldCondition(key="source", match=MatchAny(any=source_ids))]
            )

        response = self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            query_filter=query_filter,
            with_payload=True,
            limit=top_k
        )
        results = response.points

        contexts = []
        sources = []
        pages = []
        urls = []
        titles = []

        # All four lists are POSITIONAL: index i of each describes the same
        # point. `sources` was previously a deduplicated set, which silently
        # broke per-citation attribution as soon as one result set spanned
        # more than one document. Callers that need the distinct set should
        # use `source_set` below rather than re-deduplicating this one.
        for r in results:
            payload = getattr(r, "payload", None) or {}
            text = payload.get("text", "")
            if not text:
                continue
            contexts.append(text)
            sources.append(payload.get("source", ""))
            pages.append(payload.get("page"))
            urls.append(payload.get("url"))
            titles.append(payload.get("title"))

        return {
            "contexts": contexts,
            "sources": sources,
            "pages": pages,
            "urls": urls,
            "titles": titles,
            "source_set": sorted(set(sources)),
        }