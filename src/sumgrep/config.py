# src/sumgrep/config.py
"""Loads sumgrep.toml. Looks in ./sumgrep.toml, then ~/.sumgrep/sumgrep.toml.

Sections: [qdrant], [embed], [summarize], [search], [rerank], [server], [[repos]]."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATHS = (Path("sumgrep.toml"), Path.home() / ".sumgrep" / "sumgrep.toml")


@dataclass
class SummarizeCfg:
    provider: str = "bedrock"       # "anthropic" | "bedrock" | "ollama"
    model: str = ""
    max_words: int = 120
    aws_region: str | None = None   # bedrock only; falls back to $AWS_REGION
    concurrency: int | None = None  # default: 8 for API providers, 2 for ollama
    # generation knobs — omitted = provider default
    temperature: float | None = None # ollama only (Claude models reject it)
    top_p: float | None = None      # ollama only
    presence_penalty: float | None = None   # ollama only; Qwen recommends 1.5 (stops loops)
    repeat_penalty: float | None = None     # ollama only; Ollama default 1.1
    think: bool | str | None = None # false = no reasoning (ollama); "low"/"medium"/"high" = effort level (all providers)
    keep_alive: str = "10m"         # ollama only; keep the model loaded between calls
    num_ctx: int | None = None      # ollama only; context window — set it, defaults are small
    max_tokens: int = 400           # hard cap on summary length (all providers)
    timeout: float | None = None    # seconds per request; omitted = client default (10 min for Anthropic)
    max_retries: int | None = None  # retries on 429/5xx/connection errors; omitted = 6 (summarizer) / client default


@dataclass
class RerankCfg(SummarizeCfg):
    """[rerank]: an LLM reorders the top retrieval hits at search time, reading
    their stored summaries (never code). Independent of [summarize]: a local
    model can write the summaries and a cloud model rerank, or the reverse."""
    enabled: bool = False           # off: search returns the retrieval order, as before
    provider: str = "anthropic"
    model: str = "claude-haiku-5-5"
    think: bool | str | None = "low"   # reasoning effort; low is what the evals used
    max_tokens: int = 2048          # the reply is a list of ids; thinking counts toward this
    timeout: float | None = 10.0    # past this the retrieval order is returned
    max_retries: int | None = 0     # no retries on the search path
    num_ctx: int | None = 16384     # ollama: 20 summaries are ~14k tokens
    candidates: int = 20            # how many retrieval hits it reads (max 20 = PER_ARM)
    text: str = "summary"           # "summary" = full stored summary · "first" = first sentence only (small local models)


@dataclass
class SearchCfg:
    mode: str = "hybrid"            # "summary" | "code" | "hybrid"
    k: int = 10                     # pointers returned
    floor: float = 0.35             # drop hits whose best cosine is below this
    code_weight: float = 1.0        # hybrid only: scale of the code arm (summary = 1.0)


@dataclass
class ServerCfg:
    address: str = "0.0.0.0:8000"   # HOST:PORT the shared server (`sumgrep serve`) listens on
    https: bool = False             # true: serve https with tls_cert + tls_key
    tls_cert: str = ""              # PEM certificate (the full chain, if it came with one)
    tls_key: str = ""               # PEM private key, no password
    search_log: str = "~/.sumgrep/searches.jsonl"   # one JSON line per search; "" = off


@dataclass
class RepoCfg:
    name: str
    path: str
    exclude: list[str] = field(default_factory=list)


@dataclass
class Config:
    summarize: SummarizeCfg = field(default_factory=SummarizeCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    rerank: RerankCfg = field(default_factory=RerankCfg)
    server: ServerCfg = field(default_factory=ServerCfg)
    repos: list[RepoCfg] = field(default_factory=list)
    embed_model: str = "bge-m3"
    qdrant_url: str = "http://localhost:6333"
    collection: str = "sumgrep"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".sumgrep")
    source: Path | None = None      # which file was loaded


def _pick(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: Path | None = None) -> Config:
    candidates = [path] if path else list(DEFAULT_PATHS)
    for p in candidates:
        if p is not None and p.is_file():
            data = tomllib.loads(p.read_text())
            cfg = Config(
                summarize=_pick(SummarizeCfg, data.get("summarize", {})),
                search=_pick(SearchCfg, data.get("search", {})),
                rerank=_pick(RerankCfg, data.get("rerank", {})),
                server=_pick(ServerCfg, data.get("server", {})),
                repos=[_pick(RepoCfg, r) for r in data.get("repos", [])],
                embed_model=data.get("embed", {}).get("model", "bge-m3"),
                qdrant_url=data.get("qdrant", {}).get("url", "http://localhost:6333"),
                collection=data.get("qdrant", {}).get("collection", "sumgrep"),
                source=p,
            )
            return cfg
    looked = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"no sumgrep.toml found (looked in: {looked})")


def repo_cfg_for(cfg: Config, repo_path: Path) -> RepoCfg | None:
    """The [[repos]] entry whose path matches, if any."""
    target = repo_path.resolve()
    for r in cfg.repos:
        if Path(r.path).expanduser().resolve() == target:
            return r
    return None
