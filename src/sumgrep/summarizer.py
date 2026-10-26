# src/sumgrep/summarizer.py
"""Phase 2 — LLM summaries of chunks, cached by (code_hash, model).

Three providers behind one interface; config's `provider` picks one.
The SQLite cache is the incremental-indexing mechanism: a chunk whose
code_hash is already cached for this model never calls the LLM again.
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .chunker import Chunk
from .config import SummarizeCfg

# ---------------------------------------------------------------- prompt

SYSTEM_PROMPT = """You write retrieval summaries of source code for a semantic code search index.

Hard limit: at most {max_words} words. One paragraph of plain prose — no markdown, no bullets, no headings.

Cover, in order:
1. What it does (one sentence).
2. How it does it (one or two sentences).
3. Inputs, outputs, and side effects (I/O, network, database, globals, mutation) — only those visible in the code.

Keep verbatim: the symbol name, called function names, constants, error strings, env var names, magic numbers, external service names.
Name the general technique or domain concept the code implements, in standard terminology, only if the code clearly embodies one.
Do not speculate about callers, business context, or use cases not evidenced by the code.
Do not mention these rules, do not say what you kept or omitted, do not restate the file path.
If the code is trivial (under 5 lines of logic), write exactly one sentence.
Remember: at most {max_words} words."""

USER_TEMPLATE = """{language} {kind} `{symbol}` in {path}:

{code}"""

# Any edit to the prompt changes this hash, which is part of the cache key —
# so old summaries are never served for a new prompt.
PROMPT_HASH = hashlib.sha256((SYSTEM_PROMPT + USER_TEMPLATE).encode()).hexdigest()[:8]


def cache_key(model: str) -> str:
    return f"{model}#{PROMPT_HASH}"


def render_prompt(chunk: Chunk, max_words: int) -> tuple[str, str]:
    """Returns (system, user) messages."""
    return (SYSTEM_PROMPT.format(max_words=max_words),
            USER_TEMPLATE.format(language=chunk.language, kind=chunk.kind,
                                 symbol=chunk.symbol, path=chunk.path,
                                 code=chunk.code))


# ---------------------------------------------------------------- interface

@dataclass
class SummaryResult:
    text: str
    in_tokens: int = 0
    out_tokens: int = 0


class Summarizer(Protocol):          # structural interface (Go-style)
    model: str
    def complete(self, system: str, user: str) -> SummaryResult: ...
    def summarize(self, chunk: Chunk) -> SummaryResult: ...


# ---------------------------------------------------------------- providers

class _AnthropicLike:
    """Shared by the direct API and Bedrock: same Messages API shape."""

    def __init__(self, client, cfg: SummarizeCfg):
        self.client = client
        self.model = cfg.model
        self.max_words = cfg.max_words
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens

    def complete(self, system: str, user: str) -> SummaryResult:
        """One system+user turn -> text. Shared by summaries and HyDE rewrites."""
        extra = {} if self.temperature is None else {"temperature": self.temperature}
        r = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            **extra,
        )
        text = "".join(b.text for b in r.content
                       if getattr(b, "type", "") == "text").strip()
        return SummaryResult(text, r.usage.input_tokens, r.usage.output_tokens)

    def summarize(self, chunk: Chunk) -> SummaryResult:
        return self.complete(*render_prompt(chunk, self.max_words))


class AnthropicSummarizer(_AnthropicLike):
    def __init__(self, cfg: SummarizeCfg):
        import anthropic                           # key from $ANTHROPIC_API_KEY
        super().__init__(anthropic.Anthropic(max_retries=6), cfg)


class BedrockSummarizer(_AnthropicLike):
    def __init__(self, cfg: SummarizeCfg):
        from anthropic import AnthropicBedrock     # AWS credential chain
        super().__init__(AnthropicBedrock(aws_region=cfg.aws_region,
                                          max_retries=6), cfg)


class OllamaSummarizer:
    def __init__(self, cfg: SummarizeCfg):
        import ollama
        self.client = ollama.Client()
        self.model = cfg.model
        self.max_words = cfg.max_words
        self.options = {k: v for k, v in (("temperature", cfg.temperature),
                                          ("top_p", cfg.top_p),
                                          ("presence_penalty", cfg.presence_penalty),
                                          ("repeat_penalty", cfg.repeat_penalty),
                                          ("num_ctx", cfg.num_ctx),
                                          ("num_predict", cfg.max_tokens))
                        if v is not None}
        self.think = cfg.think
        self.keep_alive = cfg.keep_alive

    def complete(self, system: str, user: str) -> SummaryResult:
        """One system+user turn -> text. Shared by summaries and HyDE rewrites."""
        kwargs = dict(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            keep_alive=self.keep_alive,
        )
        if self.options:
            kwargs["options"] = self.options
        if self.think is not None:          # only sent when configured
            kwargs["think"] = self.think
        r = self.client.chat(**kwargs)
        return SummaryResult(r["message"]["content"].strip(),
                             getattr(r, "prompt_eval_count", 0) or 0,
                             getattr(r, "eval_count", 0) or 0)

    def summarize(self, chunk: Chunk) -> SummaryResult:
        return self.complete(*render_prompt(chunk, self.max_words))


PROVIDERS = {"anthropic": AnthropicSummarizer,
             "bedrock": BedrockSummarizer,
             "ollama": OllamaSummarizer}


def make_summarizer(cfg: SummarizeCfg) -> Summarizer:
    try:
        cls = PROVIDERS[cfg.provider]
    except KeyError:
        raise ValueError(f"unknown provider {cfg.provider!r}; "
                         f"expected one of {sorted(PROVIDERS)}") from None
    if not cfg.model:
        raise ValueError("[summarize].model is empty in sumgrep.toml")
    return cls(cfg)


def default_concurrency(cfg: SummarizeCfg) -> int:
    return cfg.concurrency or (2 if cfg.provider == "ollama" else 8)


# ---------------------------------------------------------------- cache

class SummaryCache:
    """SQLite, keyed on (code_hash, model). Thread-safe via one lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                code_hash  TEXT NOT NULL,
                model      TEXT NOT NULL,
                summary    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (code_hash, model)
            )""")
        self._db.commit()

    def get(self, code_hash: str, model: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT summary FROM summaries WHERE code_hash=? AND model=?",
                (code_hash, model)).fetchone()
        return row[0] if row else None

    def put(self, code_hash: str, model: str, summary: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO summaries (code_hash, model, summary) "
                "VALUES (?, ?, ?)", (code_hash, model, summary))
            self._db.commit()


# ---------------------------------------------------------------- post-processing

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def trim_repetition(text: str) -> str:
    """Cut at the first sentence that repeats an earlier one — the signature
    of a degenerate generation loop. Everything before it is kept as-is."""
    seen: set[str] = set()
    kept: list[str] = []
    for sent in _SENTENCE_SPLIT.split(text.strip()):
        key = re.sub(r"\W+", " ", sent.lower()).strip()
        if not key:
            continue
        if key in seen:
            break
        seen.add(key)
        kept.append(sent)
    return " ".join(kept)


# ---------------------------------------------------------------- runner

@dataclass
class RunStats:
    hits: int = 0
    misses: int = 0
    failed: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    summaries: dict[str, str] = field(default_factory=dict)  # chunk_id -> text
    errors: list[str] = field(default_factory=list)


def summarize_chunks(
    chunks: list[Chunk],
    summarizer: Summarizer,
    cache: SummaryCache,
    concurrency: int,
    on_progress: Callable[[int, int], None] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> RunStats:
    stats = RunStats()
    key = cache_key(summarizer.model)
    todo: list[Chunk] = []
    for c in chunks:
        cached = None if force else cache.get(c.code_hash, key)
        if cached is not None:
            stats.hits += 1
            stats.summaries[c.chunk_id] = cached
        else:
            todo.append(c)
    stats.misses = len(todo)
    if dry_run or not todo:
        return stats

    def work(c: Chunk):
        r = summarizer.summarize(c)
        r.text = trim_repetition(r.text)
        cache.put(c.code_hash, key, r.text)                # persist as we go
        return c, r

    done = 0
    ex = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                c, r = fut.result()
                stats.summaries[c.chunk_id] = r.text
                stats.in_tokens += r.in_tokens
                stats.out_tokens += r.out_tokens
            except Exception as e:
                stats.failed += 1
                stats.errors.append(f"{c.chunk_id}: {e}")
            done += 1
            if on_progress:
                on_progress(done, len(todo))
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)   # drop the queue, exit now
        raise
    ex.shutdown(wait=True)
    return stats


# ---------------------------------------------------------------- mush gate

def mush_score(summaries: list[str], embed_model: str) -> float:
    """Median pairwise cosine similarity between summaries.
    Above ~0.75 means they all say the same generic thing — fix the prompt."""
    import ollama
    vecs = ollama.embed(model=embed_model, input=summaries)["embeddings"]
    normed = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        normed.append([x / n for x in v])
    sims = [sum(a * b for a, b in zip(normed[i], normed[j]))
            for i in range(len(normed)) for j in range(i + 1, len(normed))]
    return statistics.median(sims) if sims else 0.0
