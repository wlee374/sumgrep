# src/sumgrep/rerank.py
"""[rerank]: an LLM reorders the top retrieval hits at search time.

One listwise call per query: the model sees each candidate's symbol, path
and stored summary (never code) and returns all candidate ids best-first.
Anything it omits keeps its retrieval order; on any error or timeout the
caller gets the retrieval order back, with the reason in `info`.
Measured 2026-10-28 (claude-haiku-5-5, effort low, 20 candidates): real
agent queries hit@1 0.30 -> 0.63, MRR 0.44 -> 0.69; ~2 s and ~$0.002 per query.
"""
from __future__ import annotations

import json
import re
import time

from .config import RerankCfg
from .search import PER_ARM, Hit
from .summarizer import Summarizer, make_summarizer

SYSTEM = """You rank candidate functions from a code search index for a developer's query.
Each candidate shows its name, file path, and the index summary of what it does.
Return the ids of ALL candidates ordered from the most to the least likely to be the code the query is looking for. Judge by what the function does, not by surface word overlap.
Output only a JSON array of integers, nothing else."""

_ARRAY = re.compile(r"\[[\d,\s]*\]")


def make_reranker(cfg: RerankCfg) -> Summarizer:
    """The LLM client for [rerank] (same providers as the summarizer)."""
    return make_summarizer(cfg)


def _parse(text: str, n: int) -> tuple[list[int], bool]:
    """Longest JSON array of ids in the reply -> 1-based order; ok if it names
    at least half the candidates. Missing ids are appended in retrieval order."""
    order: list[int] = []
    ok = False
    for arr in sorted(_ARRAY.findall(text), key=len, reverse=True):
        try:
            ids = [int(x) for x in json.loads(arr)]
        except ValueError:
            continue
        order = [i for i in dict.fromkeys(ids) if 1 <= i <= n]
        ok = len(order) >= n // 2
        break
    order += [i for i in range(1, n + 1) if i not in order]
    return order, ok


def rerank(query: str, hits: list[Hit], llm: Summarizer, cfg: RerankCfg,
           ) -> tuple[list[Hit], dict]:
    """Reorder hits[:cfg.candidates] by the LLM; the rest follow in retrieval
    order. Returns (hits, info); info has model + ms and either ok or error."""
    n = min(cfg.candidates, PER_ARM, len(hits))
    info: dict = {"model": cfg.model, "n": n}
    t0 = time.monotonic()
    if n < 2:
        info.update(ms=0, ok=True)
        return hits, info
    head, tail = hits[:n], hits[n:]
    lines = [f"[{i}] {h.symbol} — {h.path}\n"
             f"{h.first_sentence if cfg.text == 'first' else h.summary}"
             for i, h in enumerate(head, 1)]
    user = (f"Query: {query}\n\nCandidates ({n}):\n\n" + "\n\n".join(lines)
            + f"\n\nReturn all {n} ids, each exactly once, best first.")
    try:
        text = llm.complete(SYSTEM, user).text
    except Exception as e:                       # timeout, auth, network, provider down
        info.update(ms=round((time.monotonic() - t0) * 1000),
                    error=f"{type(e).__name__}: {str(e)[:200]}")
        return hits, info
    order, ok = _parse(text, n)
    info.update(ms=round((time.monotonic() - t0) * 1000), ok=ok)
    return [head[i - 1] for i in order] + tail, info
