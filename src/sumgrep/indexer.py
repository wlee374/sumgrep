# src/sumgrep/indexer.py
"""Phase 3 — the incremental index driver.

For each function in the repo:
  fingerprint unchanged, lines unchanged  -> nothing
  fingerprint unchanged, lines moved      -> cheap payload update (no LLM, no embed)
  fingerprint new/changed                 -> summarize (cache) + embed x2 + upsert
Functions no longer present               -> delete their points
Then a meta point records the indexed commit and embedding model.
"""
from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from qdrant_client import QdrantClient, models

from .chunker import Chunk, chunk_repo
from .embedder import Embedder
from .summarizer import Summarizer, SummaryCache, cache_key, summarize_chunks

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")   # never change
UPSERT_BATCH = 64


def point_id(repo: str, path: str, symbol: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{repo}:{path}:{symbol}"))


def meta_id(repo: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"meta:{repo}"))


def git_head(repo_path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True).strip()


def git_dirty_count(repo_path: Path) -> int:
    out = subprocess.check_output(
        ["git", "-C", str(repo_path), "status", "--porcelain"], text=True)
    return sum(1 for line in out.splitlines() if line.strip())


# ---------------------------------------------------------------- collection

def ensure_collection(client: QdrantClient, name: str, dims: int,
                      embed_model: str, recreate: bool = False) -> None:
    if recreate and client.collection_exists(name):
        client.delete_collection(name)
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config={
                "code":    models.VectorParams(size=dims, distance=models.Distance.COSINE),
                "summary": models.VectorParams(size=dims, distance=models.Distance.COSINE),
            },
        )
        client.create_payload_index(name, "repo", models.PayloadSchemaType.KEYWORD)
        client.create_payload_index(name, "kind", models.PayloadSchemaType.KEYWORD)
        return
    # existing collection: the embedding model is married to it — verify
    info = client.get_collection(name)
    have = info.config.params.vectors["code"].size
    if have != dims:
        raise RuntimeError(
            f"collection {name!r} has {have}-dim vectors but {embed_model} "
            f"produces {dims}. Changing embedding models requires "
            f"`sumgrep index --recreate` (full re-embed).")


def read_meta(client: QdrantClient, name: str, repo: str) -> dict | None:
    if not client.collection_exists(name):
        return None
    pts = client.retrieve(name, ids=[meta_id(repo)], with_payload=True,
                          with_vectors=False)
    return pts[0].payload if pts else None


def list_meta(client: QdrantClient, name: str) -> list[dict]:
    """Meta payloads of every indexed repo."""
    if not client.collection_exists(name):
        return []
    flt = models.Filter(must=[
        models.FieldCondition(key="kind", match=models.MatchValue(value="meta"))])
    out: list[dict] = []
    offset = None
    while True:
        pts, offset = client.scroll(name, scroll_filter=flt, limit=100,
                                    offset=offset, with_payload=True,
                                    with_vectors=False)
        out.extend(p.payload for p in pts)
        if offset is None:
            return out


def _write_meta(client: QdrantClient, name: str, repo: str, dims: int,
                payload: dict) -> None:
    unit = [1.0] + [0.0] * (dims - 1)          # valid unit vector, never matched
    client.upsert(name, points=[models.PointStruct(
        id=meta_id(repo), vector={"code": unit, "summary": unit},
        payload={"kind": "meta", "repo": repo, **payload})])


def _existing(client: QdrantClient, name: str, repo: str) -> dict[str, dict]:
    """{point_id: {code_hash, start_line, end_line}} for this repo."""
    out: dict[str, dict] = {}
    flt = models.Filter(must=[
        models.FieldCondition(key="repo", match=models.MatchValue(value=repo)),
        models.FieldCondition(key="kind", match=models.MatchExcept(**{"except": ["meta"]})),
    ])
    offset = None
    while True:
        pts, offset = client.scroll(
            name, scroll_filter=flt, limit=1000, offset=offset,
            with_payload=["code_hash", "start_line", "end_line"], with_vectors=False)
        for p in pts:
            out[str(p.id)] = p.payload
        if offset is None:
            break
    return out


# ---------------------------------------------------------------- driver

@dataclass
class IndexStats:
    chunks: int = 0
    unchanged: int = 0
    lines_refreshed: int = 0
    upserted: int = 0          # new or changed functions embedded
    deleted: int = 0
    summarized: int = 0        # actual LLM calls
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def index_repo(
    repo_path: Path,
    excludes: tuple[str, ...],
    client: QdrantClient,
    collection: str,
    embedder: Embedder,
    summarizer: Summarizer,
    cache: SummaryCache,
    concurrency: int,
    log: Callable[[str], None] = lambda s: None,
    recreate: bool = False,
) -> IndexStats:
    st = IndexStats()
    repo_path = repo_path.resolve()
    repo = repo_path.name
    commit = git_head(repo_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    dims = embedder.probe_dims()
    ensure_collection(client, collection, dims, embedder.model, recreate)

    chunks, skipped = chunk_repo(repo_path, excludes)
    st.chunks = len(chunks)
    log(f"{len(chunks)} chunks · HEAD {commit[:10]}")
    existing = _existing(client, collection, repo)

    # ---- classify
    todo: list[Chunk] = []
    refresh: list[tuple[str, Chunk]] = []
    current_ids: set[str] = set()
    for c in chunks:
        pid = point_id(repo, c.path, c.symbol)
        current_ids.add(pid)
        old = existing.get(pid)
        if old is None or old.get("code_hash") != c.code_hash:
            todo.append(c)
        elif (old.get("start_line"), old.get("end_line")) != (c.start_line, c.end_line):
            refresh.append((pid, c))
        else:
            st.unchanged += 1
    log(f"unchanged {st.unchanged} · moved {len(refresh)} · new/changed {len(todo)}"
        f" · to delete {len(existing) - len(current_ids & set(existing))}")

    # ---- embed + upsert helper
    def flush(ready: list[Chunk], summaries: dict[str, str]) -> None:
        for i in range(0, len(ready), UPSERT_BATCH):
            batch = ready[i:i + UPSERT_BATCH]
            code_vecs = embedder.embed([c.code for c in batch])
            sum_vecs = embedder.embed([summaries[c.chunk_id] for c in batch])
            client.upsert(collection, points=[
                models.PointStruct(
                    id=point_id(repo, c.path, c.symbol),
                    vector={"code": cv, "summary": sv},
                    payload={
                        "repo": repo, "path": c.path, "symbol": c.symbol,
                        "kind": c.kind, "language": c.language,
                        "start_line": c.start_line, "end_line": c.end_line,
                        "summary": summaries[c.chunk_id],
                        "code_hash": c.code_hash,
                        "git_commit": commit, "indexed_at": now,
                    })
                for c, cv, sv in zip(batch, code_vecs, sum_vecs)
            ])
            st.upserted += len(batch)
            log(f"  embedded {st.upserted}/{len(todo)}")

    if todo:
        # cached summaries first — fast, useful work before any LLM call
        key = cache_key(summarizer.model)
        cached: dict[str, str] = {}
        misses: list[Chunk] = []
        for c in todo:
            s_ = cache.get(c.code_hash, key)
            if s_ is None:
                misses.append(c)
            else:
                cached[c.chunk_id] = s_
        hits = [c for c in todo if c.chunk_id in cached]
        log(f"summaries cached {len(hits)} · need LLM {len(misses)}")
        flush(hits, cached)

        if misses:
            def progress(done: int, total: int) -> None:
                if done % 25 == 0 or done == total:
                    log(f"  summarized {done}/{total}")
            ss = summarize_chunks(misses, summarizer, cache, concurrency,
                                  on_progress=progress)
            st.summarized = ss.misses - ss.failed
            st.failed = ss.failed
            st.errors.extend(ss.errors)
            flush([c for c in misses if c.chunk_id in ss.summaries], ss.summaries)

    # ---- cheap line-number refresh for moved-but-unchanged functions
    for pid, c in refresh:
        client.set_payload(collection, points=[pid], payload={
            "start_line": c.start_line, "end_line": c.end_line,
            "git_commit": commit, "indexed_at": now})
    st.lines_refreshed = len(refresh)

    # ---- deletions
    gone = [pid for pid in existing if pid not in current_ids]
    if gone:
        client.delete(collection, points_selector=models.PointIdsList(points=gone))
    st.deleted = len(gone)

    # ---- meta
    _write_meta(client, collection, repo, dims, {
        "git_commit": commit, "embed_model": embedder.model, "dims": dims,
        "indexed_at": now, "chunk_count": len(chunks),
        "summarize_model": cache_key(summarizer.model),
    })
    return st
