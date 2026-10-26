# src/sumgrep/embedder.py
"""Phase 3 — text -> vector via Ollama (bge-m3). Batched."""
from __future__ import annotations


class Embedder:
    def __init__(self, model: str, batch_size: int = 32):
        import ollama
        self.client = ollama.Client()
        self.model = model
        self.batch_size = batch_size
        self.dims: int | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            vecs = self.client.embed(model=self.model, input=batch)["embeddings"]
            if self.dims is None:
                self.dims = len(vecs[0])
            out.extend(vecs)
        return out

    def probe_dims(self) -> int:
        if self.dims is None:
            self.embed(["probe"])
        assert self.dims is not None
        return self.dims
