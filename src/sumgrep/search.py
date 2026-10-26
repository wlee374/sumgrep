# src/sumgrep/search.py
"""Phase 4 — retrieval + fusion.

search(): embed the query ONCE, run it against the `code` and/or `summary`
named vectors (each filtered to one repo), fuse the arms with reciprocal
rank fusion, drop hits below a cosine floor, return pointers.

Everything is client-side on purpose: while tuning you want to see each
arm's rank and cosine on every hit. Qdrant can do prefetch+RRF server-side;
swap in later for one round-trip.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from .embedder import Embedder
from .summarizer import Summarizer

MODES = ("hybrid", "code", "summary")
PER_ARM = 20            # candidates fetched from each arm before fusion
RRF_C = 60              # standard RRF constant
DEFAULT_FLOOR = 0.35    # drop hits whose best cosine is below this (tune with data)


@dataclass
class Hit:
    id: str
    repo: str
    symbol: str
    path: str
    kind: str
    start_line: int
    end_line: int
    summary: str
    score: float                        # fused RRF score
    best_cos: float                     # max cosine across arms
    arms: dict[str, tuple[int, float]]  # arm -> (rank 1-based, cosine)

    @property
    def first_sentence(self) -> str:
        s = self.summary.strip()
        for i, ch in enumerate(s):
            if ch == "." and (i + 1 == len(s) or s[i + 1].isspace()):
                return s[:i + 1]
        return s

    @property
    def pointer(self) -> str:
        return f"{self.symbol}  {self.path}:L{self.start_line}-{self.end_line}  [{self.kind}]"

    def arms_str(self) -> str:
        return " · ".join(f"{a}#{r} {c:.2f}" for a, (r, c) in sorted(self.arms.items()))


# ---------------------------------------------------------------- arms

def repo_filter(repo: str | list[str]) -> models.Filter:
    match = (models.MatchValue(value=repo) if isinstance(repo, str)
             else models.MatchAny(any=list(repo)))
    return models.Filter(must=[
        models.FieldCondition(key="repo", match=match),
        models.FieldCondition(key="kind", match=models.MatchExcept(**{"except": ["meta"]})),
    ])


def query_arms(client: QdrantClient, collection: str, qv: list[float],
               repo: str | list[str], arms: tuple[str, ...], limit: int = PER_ARM,
               ) -> dict[str, list[models.ScoredPoint]]:
    flt = repo_filter(repo)
    return {
        name: client.query_points(collection, query=qv, using=name,
                                  query_filter=flt, limit=limit,
                                  with_payload=True).points
        for name in arms
    }


# ---------------------------------------------------------------- fusion

def rrf(ranked: dict[str, list[models.ScoredPoint]], k: int,
        c: int = RRF_C, floor: float = DEFAULT_FLOOR,
        weights: dict[str, float] | None = None) -> list[Hit]:
    """Reciprocal rank fusion over the arms. Keeps per-arm (rank, cosine)
    on every Hit so you can see which arm found what. `weights` scales an
    arm's contribution (default 1.0 each) — a Phase 7 tuning knob."""
    scores: dict[str, float] = defaultdict(float)
    arms: dict[str, dict[str, tuple[int, float]]] = defaultdict(dict)
    payload: dict[str, dict] = {}
    for arm, points in ranked.items():
        w = (weights or {}).get(arm, 1.0)
        for rank, p in enumerate(points, start=1):
            pid = str(p.id)
            scores[pid] += w / (c + rank)
            arms[pid][arm] = (rank, float(p.score))
            payload.setdefault(pid, p.payload or {})
    hits: list[Hit] = []
    for pid in sorted(scores, key=lambda i: (-scores[i], i)):
        pl = payload[pid]
        best = max(cos for _, cos in arms[pid].values())
        if best < floor:
            continue
        hits.append(Hit(
            id=pid, repo=pl.get("repo", ""), symbol=pl["symbol"],
            path=pl["path"], kind=pl["kind"],
            start_line=pl["start_line"], end_line=pl["end_line"],
            summary=pl.get("summary", ""), score=scores[pid],
            best_cos=best, arms=dict(arms[pid]),
        ))
        if len(hits) == k:
            break
    return hits


# ---------------------------------------------------------------- HyDE

HYDE_SYSTEM = """You write a hypothetical retrieval summary for a semantic code search index.

Given a natural-language question about a codebase, write the one-paragraph summary that the matching function's index entry would most likely have: what the function does, how it does it, and its inputs, outputs and side effects. Use standard technical terminology for the concept involved. Invent plausible identifier names only if the question implies them. Plain prose, no markdown, at most 80 words. Output only the summary."""


def hyde_rewrite(query: str, summarizer: Summarizer) -> str:
    """One small-LLM call: question -> hypothetical docstring. Embed that
    instead of the raw question (HyDE). Falls back to the query on failure."""
    try:
        text = summarizer.complete(HYDE_SYSTEM, query).text.strip()
    except Exception:
        return query
    return text or query


# ---------------------------------------------------------------- entry point

def search(client: QdrantClient, collection: str, embedder: Embedder,
           query: str, repo: str | list[str], k: int = 5, mode: str = "hybrid",
           floor: float = DEFAULT_FLOOR, per_arm: int = PER_ARM,
           embed_text: str | None = None, code_weight: float = 1.0) -> list[Hit]:
    """Pointers for `query` in `repo` (one name, or a list to search several
    repos at once). `embed_text` overrides what gets
    embedded (HyDE rewrite); `query` is only used for that when it is None.
    `code_weight` scales the code arm in hybrid fusion (summary stays 1.0)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    qv = embedder.embed([embed_text or query])[0]          # ONE embedding call
    arms = ("code", "summary") if mode == "hybrid" else (mode,)
    ranked = query_arms(client, collection, qv, repo, arms, limit=per_arm)
    return rrf(ranked, k, floor=floor, weights={"code": code_weight})
