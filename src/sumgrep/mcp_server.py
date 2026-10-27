# src/sumgrep/mcp_server.py
"""Phase 5 — MCP server (stdio or HTTP, read-only).

Two tools: search_code(query) -> pointers, index_status() -> what is indexed.
No reindex tool on purpose: indexing is a long-running write that stays a
human-triggered CLI command.

A pointer is the function name + its path relative to the repo root,
exactly as stored in the index. The server never looks at checkouts
and never prints absolute paths, so an index built on one machine works
unchanged on any other, wherever the checkout lives. The agent opens the
file in its own checkout (or greps the function name).

Which repos: all indexed repos, unless `repo=` names one.

Which config: $SUMGREP_CONFIG if set, else this project's sumgrep.toml,
else the usual lookup (./sumgrep.toml, ~/.sumgrep/sumgrep.toml). Installed
as a Claude Code plugin, the server runs from the plugin cache, so only
$SUMGREP_CONFIG or ~/.sumgrep/sumgrep.toml apply.

Run:   uv run --project ~/sumgrep python -m sumgrep.mcp_server
Never print to stdout here — stdout is the MCP transport. Log to stderr.

Shared: `uv run sumgrep serve` (serve() below) serves the same tools at
http://HOST:8000/mcp, one server for a whole team (`claude mcp add
--transport http sumgrep http://HOST:8000/mcp`). Address and https (cert +
key) come from [server] in sumgrep.toml. There is no authentication either
way: anyone who can reach the port can search, so run it only on a private
network (office LAN or VPN). The shared server also writes every search to
[server] search_log (JSON lines, rotated), the one file it writes: the
teammates' own session histories never reach this machine. Over stdio
nothing is written.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# qdrant-client/httpx log every request at INFO, and mcp's HTTP transport a
# "Terminating session" line per request; that noise would land in Claude
# Code's MCP log or the shared server's log. Keep only warnings and above.
for _name in ("httpx", "httpcore", "qdrant_client", "mcp.server.streamable_http"):
    logging.getLogger(_name).setLevel(logging.WARNING)

mcp = MCPServer(
    "sumgrep",
    # How sumgrep behaves is described HERE, not in users' CLAUDE.md files:
    # Claude Code receives this on every session, so it updates with git pull.
    instructions=(
        "Semantic search over indexed repos' functions, by meaning. For "
        "'where is the code that does X' questions call search_code first; "
        "for exact identifiers, strings, or error messages use Grep instead. "
        "Results are pointers: function name + file path relative to the repo "
        "root — open that file in your checkout, or grep the function name. "
        "It searches every indexed repo (repo='name' narrows); if you are not "
        "working in one of them (index_status lists them), ignore its results "
        "and use Grep. The index is a snapshot at the commit index_status "
        "shows: line numbers may have drifted and newer code is missing — "
        "trust the file, grep if absent. Never try to index; re-indexing is a "
        "manual step a human runs."
    ),
)


def _log(msg: str) -> None:
    print(f"[sumgrep] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- search log

_searches: logging.Logger | None = None      # set by serve() only; stdio writes nothing


def _open_search_log(path: str, max_bytes: int = 10_000_000,
                     backups: int = 4) -> logging.Logger:
    """[server] search_log: one JSON line per search. At max_bytes the file
    moves to .1 (.1 to .2, ...) and the oldest beyond `backups` is deleted,
    so it never takes more than (backups + 1) x max_bytes."""
    from logging.handlers import RotatingFileHandler

    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(p, maxBytes=max_bytes, backupCount=backups,
                                      encoding="utf-8")
    except OSError as e:
        sys.exit(f"[sumgrep] [server] cannot write search_log={p}: {e}")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("sumgrep.searches")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def _record_search(ctx: Context | None, rec: dict) -> None:
    """Append one search to the shared server's log, with the asking machine."""
    if _searches is None:
        return
    try:
        req = ctx.request_context.request if ctx else None
    except ValueError:                           # no request (called outside MCP)
        req = None
    client = getattr(getattr(req, "client", None), "host", None)
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "client": client, **rec}
    _searches.info(json.dumps(line, ensure_ascii=False))


# ---------------------------------------------------------------- lazy state

def _config_path() -> Path | None:
    env = os.environ.get("SUMGREP_CONFIG")
    if env:
        return Path(env).expanduser()
    local = PROJECT_ROOT / "sumgrep.toml"
    return local if local.is_file() else None


@lru_cache(maxsize=1)
def _state():
    """Config + clients, created on first tool call so startup is instant."""
    from qdrant_client import QdrantClient
    from .config import load_config
    from .embedder import Embedder

    cfg = load_config(_config_path())
    _log(f"config {cfg.source} · qdrant {cfg.qdrant_url} · embed {cfg.embed_model}")
    return cfg, QdrantClient(url=cfg.qdrant_url), Embedder(cfg.embed_model)


def _metas(cfg, client, repo: str | None) -> tuple[list[dict], str | None]:
    """(meta of each repo to use, error message). All repos unless `repo`."""
    from .indexer import list_meta

    metas = list_meta(client, cfg.collection)
    if not metas:
        return [], "nothing is indexed yet"
    if repo is None:
        return metas, None
    name = Path(repo.rstrip("/")).name          # tolerate a path: use its folder name
    picked = [m for m in metas if m.get("repo") == name]
    if not picked:
        return [], (f"repo {repo!r} is not indexed. Indexed: "
                    f"{', '.join(m.get('repo', '?') for m in metas)}")
    return picked, None


# ---------------------------------------------------------------- tools

@mcp.tool()
def search_code(query: str, k: int | None = None, mode: str | None = None,
                repo: str | None = None, ctx: Context | None = None) -> str:
    """Semantic search over indexed repos' functions BY MEANING.

    Use for conceptual questions where you don't know the name of the
    code: "where do we cache the signing keys", "code that retries
    deleting a temp file", "how are search scores from two lists merged".
    Do NOT use for literal identifiers, exact strings, or error messages
    — use Grep for those.

    Searches every indexed repo; pass repo="name" to search only one.

    Returns up to k pointers: function name, file path relative to the
    repo root, line range, kind, and the first sentence of an LLM-written
    summary. Never returns source code: open the file in your checkout
    (or grep the function name) to get the truth. The index is a snapshot
    at one commit (see index_status): line numbers may have drifted, and
    code written after that commit is not in it. Similarity is not
    confidence — if the results look unrelated to each other, the concept
    probably isn't in the index.

    mode: "summary" (default, compares the query to each function's
    summary), "code" (to the raw source), or "hybrid" (both, fused).
    """
    rec: dict = {"query": query, "k": k, "mode": mode, "repo": repo}
    t0 = time.monotonic()
    out = _search_code(query, k, mode, repo, rec)
    rec["ms"] = round((time.monotonic() - t0) * 1000)
    if "hits" not in rec:                        # no search ran: `out` says why
        rec["error"] = out
    _record_search(ctx, rec)
    return out


def _search_code(query: str, k: int | None, mode: str | None, repo: str | None,
                 rec: dict) -> str:
    """search_code's work; fills `rec` (k, mode, commits, hits) for the search log."""
    from .search import MODES, search

    try:
        cfg, client, embedder = _state()
    except FileNotFoundError as e:
        return f"sumgrep is not configured: {e}"
    sc = cfg.search
    mode = rec["mode"] = mode or sc.mode
    if mode not in MODES:
        return f"mode must be one of {MODES}, got {mode!r}"
    k = rec["k"] = k or sc.k

    try:
        metas, err = _metas(cfg, client, repo)
    except Exception as e:
        return f"cannot reach the index: {type(e).__name__}: {e}"
    if err:
        return err

    names = [m["repo"] for m in metas]
    rec["commits"] = {m["repo"]: m.get("git_commit", "")[:10] for m in metas}
    try:
        hits = search(client, cfg.collection, embedder, query,
                      repo=names if len(names) > 1 else names[0],
                      k=k, mode=mode, floor=sc.floor, code_weight=sc.code_weight)
    except Exception as e:                       # Qdrant/Ollama down, etc.
        return f"search failed: {type(e).__name__}: {e}"
    rec["hits"] = [{"symbol": h.symbol, "path": h.path, "start": h.start_line,
                    "end": h.end_line, "kind": h.kind, "cos": round(h.best_cos, 3),
                    **({"repo": h.repo} if len(names) > 1 else {})} for h in hits]

    if not hits:
        return f"no hits above the similarity floor (searched: {', '.join(names)})"
    label = len(names) > 1                       # name the repo only when it's ambiguous
    return "\n".join(
        f"{h.repo + ': ' if label else ''}{h.pointer}\n   {h.first_sentence}"
        for h in hits)


@mcp.tool()
def index_status(repo: str | None = None) -> str:
    """What is indexed: for each repo (or only repo="name"), how many
    functions, at which commit, and when.

    The index is a snapshot at that commit. Compare it with `git log` in
    your checkout: code changed after it may have moved, and code added
    after it is not in the index — grep for it. Re-indexing is a manual
    CLI step, not a tool.
    """
    try:
        cfg, client, _ = _state()
    except FileNotFoundError as e:
        return f"sumgrep is not configured: {e}"
    try:
        metas, err = _metas(cfg, client, repo)
    except Exception as e:
        return f"cannot reach the index: {type(e).__name__}: {e}"
    if err:
        return err

    out = []
    for m in metas:
        out += [
            f"repo            {m.get('repo')}",
            f"functions       {m.get('chunk_count')}",
            f"indexed commit  {m.get('git_commit', '')[:10]}",
            f"indexed at      {m.get('indexed_at')}",
            "",
        ]
    out += [f"search mode     {cfg.search.mode}",
            "reindex         uv run sumgrep index --repo <checkout>   "
            "(a human runs this in the sumgrep project folder)"]
    return "\n".join(out)


def _http_address(value: str) -> tuple[str, int]:
    """'HOST:PORT' (or just 'PORT' = all interfaces) -> (host, port)."""
    host, _, port = value.rpartition(":")
    if not port.isdigit():
        sys.exit(f"[sumgrep] [server] address must be HOST:PORT, got {value!r}")
    return host.strip("[]") or "0.0.0.0", int(port)


def _tls_files(sc) -> tuple[str, str]:
    """[server] tls_cert / tls_key, checked here so errors aren't uvicorn tracebacks."""
    import ssl

    if not (sc.tls_cert and sc.tls_key):
        sys.exit("[sumgrep] [server] https = true needs tls_cert and tls_key")
    cert, key = str(Path(sc.tls_cert).expanduser()), str(Path(sc.tls_key).expanduser())
    try:
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:
        sys.exit(f"[sumgrep] [server] cannot use tls_cert={cert} / tls_key={key}: {e}")
    return cert, key


def serve(cfg) -> None:
    """The shared team server (`sumgrep serve`): the same tools over HTTP or
    HTTPS, as set in [server]. Stateless: the tools keep nothing between
    calls, so each request stands alone and clients survive a restart.
    Its own log is the start lines and errors only (no line per request);
    searches go to [server] search_log."""
    global _searches
    import uvicorn

    sc = cfg.server
    host, port = _http_address(sc.address)
    cert, key = _tls_files(sc) if sc.https else (None, None)
    if sc.search_log:
        _searches = _open_search_log(sc.search_log)
    os.environ["SUMGREP_CONFIG"] = str(cfg.source)   # tools read the same file
    _log(f"serving {'https' if sc.https else 'http'}://{host}:{port}/mcp · config "
         f"{cfg.source} · no auth: private network only")
    _log(f"search log {Path(sc.search_log).expanduser() if sc.search_log else 'off'}")
    app = mcp.streamable_http_app(stateless_http=True, host=host)
    uvicorn.run(app, host=host, port=port, ssl_certfile=cert, ssl_keyfile=key,
                log_level=mcp.settings.log_level.lower(), access_log=False)


def main() -> None:
    _log(f"starting · project {PROJECT_ROOT}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
