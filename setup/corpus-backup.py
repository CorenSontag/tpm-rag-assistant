"""Dump / restore the Qdrant collection as a portable file.

Why this exists, and why it is not setup/MANIFEST.ps1's job or Qdrant's own
snapshot feature:

On 2026-11-09 a Qdrant snapshot of `docs` was created, copied out of the volume
and test-restored into a clean qdrant/qdrant:v1.19.0 container. It FAILED:

    Wal error: Can't init WAL: failed to read first-index file:
    Failed to deserialize .../0/wal/first-index: expected value at line 1 column 1

The archive was a valid GNU tar and read cleanly, but `0/wal/first-index`
inside it was 16 ZERO bytes where the live volume holds `{"ack_index":22}`.
Qdrant warns about this volume on every boot - "Unrecognized filesystem -
cannot guarantee data safety" - because data/qdrant_storage is a Windows bind
mount. A snapshot that does not restore is not a backup.

This script bypasses all of that. It reads points through the HTTP scroll API
and writes vectors + payloads to a .npz, which is plain data with no WAL, no
segment files and no filesystem semantics to get wrong. Restore is an upsert.

The 349 `website` points are the reason this matters: they were ingested by an
external web team and NO tool in this bundle rebuilds them. A re-ingest
restores the 1415 book chunks and nothing else.

The output contains the full corpus text and is COPYRIGHTED. It belongs under
data/ (gitignored) and must never be committed or published.

    python setup/corpus-backup.py dump    data/snapshot/corpus.npz
    python setup/corpus-backup.py verify  data/snapshot/corpus.npz
    python setup/corpus-backup.py restore data/snapshot/corpus.npz --url http://localhost:6399
"""

import argparse
import json
import sys
import urllib.request

import numpy as np

DEFAULT_URL = "http://localhost:6333"
COLLECTION = "docs"
BATCH = 256


def _req(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.load(resp)["result"]


def dump(url, collection, out):
    """Scroll every point, with vector and payload, into a .npz.

    The collection config travels with the points. Restoring vectors alone is
    not a restore: the live collection carries on_disk_payload and a keyword
    payload index on `source`, and without that index the source_ids filter
    that /query and search_tpm_docs rely on has no index to use.
    """
    info = _req(f"{url}/collections/{collection}")
    schema = {
        "on_disk_payload": info["config"]["params"].get("on_disk_payload"),
        "payload_schema": {
            k: v.get("data_type") for k, v in (info.get("payload_schema") or {}).items()
        },
    }
    print(f"  schema: {schema}")
    ids, vectors, payloads = [], [], []
    offset = None
    while True:
        body = {"limit": BATCH, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        res = _req(f"{url}/collections/{collection}/points/scroll", body, "POST")
        for p in res["points"]:
            ids.append(str(p["id"]))
            vectors.append(p["vector"])
            payloads.append(p.get("payload") or {})
        offset = res.get("next_page_offset")
        print(f"  scrolled {len(ids)} points", flush=True)
        if offset is None:
            break

    arr = np.asarray(vectors, dtype=np.float32)
    # Payloads go in as one JSON blob rather than an object array: ensure_ascii
    # False keeps the Thai readable, and np.save of dicts would need pickle.
    np.savez_compressed(
        out,
        ids=np.asarray(ids),
        vectors=arr,
        payloads=np.asarray(json.dumps(payloads, ensure_ascii=False)),
        schema=np.asarray(json.dumps(schema)),
    )
    print(f"  wrote {out}: {len(ids)} points, vectors {arr.shape} {arr.dtype}")
    return len(ids)


def _load(path):
    z = np.load(path, allow_pickle=False)
    # schema is absent from files written before 2026-11-09; treat that as
    # "unknown" rather than as "no indexes", so a restore from an old file says
    # so instead of silently producing an unindexed collection.
    schema = json.loads(str(z["schema"])) if "schema" in z.files else None
    return z["ids"], z["vectors"], json.loads(str(z["payloads"])), schema


def verify(path):
    """Re-read the file and report what is in it. Catches a truncated write."""
    ids, vectors, payloads, schema = _load(path)
    assert len(ids) == len(vectors) == len(payloads), "length mismatch in file"
    counts = {}
    for p in payloads:
        counts[p.get("source", "?")] = counts.get(p.get("source", "?"), 0) + 1
    print(f"  points: {len(ids)}")
    print(f"  vectors: {vectors.shape} {vectors.dtype}")
    print(f"  norms: min {np.linalg.norm(vectors, axis=1).min():.3f} "
          f"max {np.linalg.norm(vectors, axis=1).max():.3f}  (0.0 would mean empty vectors)")
    for k in sorted(counts):
        print(f"  {k:<18} {counts[k]:>5}")
    print(f"  schema: {schema if schema is not None else 'MISSING - written by an older version'}")
    return len(ids)


def restore(path, url, collection):
    ids, vectors, payloads, schema = _load(path)
    dim = int(vectors.shape[1])
    if schema is None:
        print("  WARNING: this file carries no schema. The collection will be "
              "created without on_disk_payload or payload indexes - re-dump "
              "from a live collection to get a complete backup.")
    try:
        _req(f"{url}/collections/{collection}")
        print(f"  collection {collection!r} already exists - upserting into it")
    except Exception:
        body = {"vectors": {"size": dim, "distance": "Cosine"}}
        if schema and schema.get("on_disk_payload") is not None:
            body["on_disk_payload"] = schema["on_disk_payload"]
        _req(f"{url}/collections/{collection}", body, "PUT")
        print(f"  created collection {collection!r} dim={dim} Cosine "
              f"on_disk_payload={body.get('on_disk_payload')}")
        # The index, not just the field. source_ids filtering in
        # QdrantStorage.search is a MatchAny on `source`; an unindexed keyword
        # field still answers, by scanning, which is the kind of regression
        # that shows up as "retrieval got slow" and never as an error.
        for field, data_type in sorted((schema or {}).get("payload_schema", {}).items()):
            _req(f"{url}/collections/{collection}/index?wait=true",
                 {"field_name": field, "field_schema": data_type}, "PUT")
            print(f"  created payload index {field!r} ({data_type})")

    for i in range(0, len(ids), BATCH):
        pts = [
            {"id": ids[j], "vector": vectors[j].tolist(), "payload": payloads[j]}
            for j in range(i, min(i + BATCH, len(ids)))
        ]
        _req(f"{url}/collections/{collection}/points?wait=true", {"points": pts}, "PUT")
        print(f"  upserted {min(i + BATCH, len(ids))}/{len(ids)}", flush=True)

    n = _req(f"{url}/collections/{collection}/points/count", {"exact": True}, "POST")["count"]
    print(f"  collection now holds {n} points")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["dump", "verify", "restore"])
    ap.add_argument("path")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--collection", default=COLLECTION)
    a = ap.parse_args()

    if a.action == "dump":
        dump(a.url, a.collection, a.path)
    elif a.action == "verify":
        verify(a.path)
    else:
        restore(a.path, a.url, a.collection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
