# src/sumgrep/embedder.py
"""Phase 3 — text -> vector via Ollama (bge-m3). Batched.

Ollama reads only as much of an input as the model's context holds (2,048
tokens for bge-m3 as it ships: measured 2026-11-26, the same vector for
the first 5,000 and the first 30,000 characters of a file) and ignores the
rest without an error. Now and then it rejects such an input instead (413,
seen with Korean text); `_one` then does the cutting here.
"""
from __future__ import annotations

MIN_RETRY_CHARS = 500       # an input this short that is still rejected is a real error


class Embedder:
    def __init__(self, model: str, batch_size: int = 32):
        import ollama
        self._too_long = ollama.ResponseError
        self.client = ollama.Client()
        self.model = model
        self.batch_size = batch_size
        self.dims: int | None = None

    def _call(self, texts: list[str]) -> list[list[float]]:
        return self.client.embed(model=self.model, input=texts)["embeddings"]

    def _one(self, text: str) -> list[float]:
        """One input the batch call rejected as too long: drop a fifth off
        its end until it is taken."""
        while True:
            try:
                return self._call([text])[0]
            except self._too_long as e:
                if e.status_code != 413 or len(text) < MIN_RETRY_CHARS:
                    raise
                text = text[:len(text) * 4 // 5]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            try:
                vecs = self._call(batch)
            except self._too_long as e:
                if e.status_code != 413:
                    raise
                vecs = [self._one(t) for t in batch]
            if self.dims is None:
                self.dims = len(vecs[0])
            out.extend(vecs)
        return out

    def probe_dims(self) -> int:
        if self.dims is None:
            self.embed(["probe"])
        assert self.dims is not None
        return self.dims
