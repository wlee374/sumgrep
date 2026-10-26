# src/sumgrep/transfer.py
"""Share an index with teammates.

`sumgrep export` writes ONE .sumgrep file; `sumgrep import FILE` sets up the
receiving computer from it: checks Qdrant (Docker) and Ollama, offers to fix
what's missing, loads the index, runs a test search, connects Claude Code.
It asks before running anything on the machine.

A .sumgrep file is a zip with two entries:
  index.snapshot     Qdrant snapshot of the whole collection (all repos)
  summaries.sqlite   summary cache (only used if this machine re-indexes)
Repos, commits and the embedding model are in the meta points, so there is
no manifest. Not called .zip so Safari doesn't unzip it on download.

Messages are plain ASCII so they print on a Windows console.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

import httpx
import typer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXT = ".sumgrep"
SNAPSHOT, CACHE = "index.snapshot", "summaries.sqlite"
QDRANT_RUN = ["run", "-d", "--name", "qdrant", "--restart", "unless-stopped",
              "-p", "6333:6333", "-p", "6334:6334",
              "-v", "qdrant_storage:/qdrant/storage", "qdrant/qdrant"]


def _say(msg: str = "") -> None:
    typer.echo(msg)


def _fail(msg: str) -> None:
    typer.echo(f"\nSTOPPED: {msg}\nThen run the same command again.", err=True)
    raise typer.Exit(1)


def _ask(question: str) -> bool:
    return typer.confirm(f"  {question}", default=True)


def _run(cmd: list[str], quiet: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=quiet, text=True, errors="replace")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _shell_line(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


# ---------------------------------------------------------------- export

def default_export_path() -> Path:
    desktop = Path.home() / "Desktop"
    folder = desktop if desktop.is_dir() else Path.home()
    return folder / f"sumgrep-index-{date.today().isoformat()}{EXT}"


def export_bundle(cfg, out: Path) -> None:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=cfg.qdrant_url)
    if not client.collection_exists(cfg.collection):
        _fail(f"there is no index to export (collection {cfg.collection!r} not found).")
    _say(f"Exporting the index ({client.count(cfg.collection).count} entries)...")
    snap = client.create_snapshot(cfg.collection, wait=True)
    url = f"{cfg.qdrant_url.rstrip('/')}/collections/{cfg.collection}/snapshots/{snap.name}"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snap_file = Path(tmp) / SNAPSHOT
        try:
            with httpx.stream("GET", url, timeout=600) as r, snap_file.open("wb") as f:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    f.write(chunk)
        finally:
            client.delete_snapshot(cfg.collection, snap.name)   # server-side copy not needed
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap_file, SNAPSHOT)
            cache = cfg.cache_dir / CACHE
            if cache.is_file():
                copy = Path(tmp) / CACHE           # consistent copy even mid-index
                src, dst = sqlite3.connect(cache), sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                z.write(copy, CACHE)
    _say(f"Wrote {out} ({out.stat().st_size / 1e6:.0f} MB)")
    _say("Share this one file. Teammates follow QUICKSTART.md in the sumgrep repo.")


# ---------------------------------------------------------------- import

def _config(path: Path | None):
    """Load the config, creating ~/.sumgrep/sumgrep.toml from the example if
    there is none. That spot, not this folder, so the plugin's MCP server
    (which runs from Claude Code's plugin cache) reads the same file."""
    from .config import DEFAULT_PATHS, load_config

    if path is None:
        local = PROJECT_ROOT / "sumgrep.toml"
        if not local.is_file() and not any(p.is_file() for p in DEFAULT_PATHS):
            home = DEFAULT_PATHS[-1]
            home.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(PROJECT_ROOT / "sumgrep.toml.example", home)
            _say("  created the settings file ~/.sumgrep/sumgrep.toml (no edits needed)")
        path = local if local.is_file() else None
    return load_config(path)


def _reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


def _ensure_qdrant(url: str) -> None:
    ready = f"{url.rstrip('/')}/readyz"
    if _reachable(ready):
        _say("  Qdrant (stores the index): running")
        return
    if "localhost" not in url and "127.0.0.1" not in url:
        _fail(f"cannot reach Qdrant at {url} (set in sumgrep.toml).")
    docker = shutil.which("docker")
    if not docker:
        _fail("Docker Desktop is not installed. Install it from "
              "https://www.docker.com/products/docker-desktop/ and open it.")
    if _run([docker, "info"], quiet=True).returncode != 0:
        _fail("Docker Desktop is not running. Open it and wait until it says "
              "'Engine running'.")
    if _run([docker, "inspect", "qdrant"], quiet=True).returncode == 0:
        if not _ask("Qdrant is installed in Docker but stopped. Start it?"):
            _fail("Qdrant must be running. Start the 'qdrant' container in Docker Desktop.")
        cmd = [docker, "start", "qdrant"]
    else:
        if not _ask("Qdrant (stores the index) is not set up. Set it up in Docker now? "
                    "(one-time download)"):
            _fail("Qdrant is needed. Set it up with: " + _shell_line(["docker", *QDRANT_RUN]))
        cmd = [docker, *QDRANT_RUN]
    if _run(cmd).returncode != 0:
        _fail("Docker could not start Qdrant (see the message above).")
    for _ in range(60):
        if _reachable(ready):
            _say("  Qdrant: running")
            return
        time.sleep(1)
    _fail("Qdrant started but is not answering yet. Wait a minute.")


def _ensure_ollama(model: str) -> None:
    import ollama

    try:
        names = {m.model for m in ollama.Client().list().models}
    except Exception:
        _fail("Ollama is not running. Install it from https://ollama.com/download "
              "and open it (if it is installed, just open it).")
    if model in names or f"{model}:latest" in names:
        _say(f"  Ollama with {model} (reads your questions): ready")
        return
    exe = shutil.which("ollama")
    if not exe:
        _fail(f"Ollama needs the {model} model. Open a new terminal and run: "
              f"ollama pull {model}")
    if not _ask(f"Ollama needs the {model} model (about 1.2 GB, one-time). Download it now?"):
        _fail(f"the {model} model is needed. Download it with: ollama pull {model}")
    if _run([exe, "pull", model]).returncode != 0:
        _fail(f"the download failed. Try again, or run: ollama pull {model}")
    _say(f"  Ollama with {model}: ready")


def _unpack(file: Path, tmp: Path) -> tuple[Path, Path | None]:
    """(snapshot, summary cache or None). Accepts an older bare .snapshot too."""
    if not zipfile.is_zipfile(file):
        return file, None
    with zipfile.ZipFile(file) as z:
        names = set(z.namelist())
        if SNAPSHOT not in names:
            _fail(f"{file.name} is not a sumgrep export. Ask for the .sumgrep file.")
        z.extract(SNAPSHOT, tmp)
        if CACHE in names:
            z.extract(CACHE, tmp)
    return tmp / SNAPSHOT, (tmp / CACHE if CACHE in names else None)


def _restore(client, name: str, snapshot: Path) -> None:
    if client.collection_exists(name):
        n = client.count(name).count
        if not _ask(f"This computer already has an index ({n} entries). "
                    f"Replace it with the one in this file?"):
            _say("Nothing changed.")
            raise typer.Exit(0)
    _say(f"  loading ({snapshot.stat().st_size / 1e6:.0f} MB)...")
    with snapshot.open("rb") as f:
        client.http.snapshots_api.recover_from_uploaded_snapshot(
            collection_name=name, snapshot=f, wait=True, priority="snapshot")


def _place_cache(cfg, cache: Path | None) -> None:
    if cache is None:
        return
    dest = cfg.cache_dir / CACHE
    if dest.exists():
        _say("  kept the summary cache already on this computer")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(cache, dest)
    _say("  installed the summary cache")


def _test_search(cfg, client, name: str) -> list[dict]:
    from .embedder import Embedder
    from .indexer import list_meta
    from .search import search

    metas = list_meta(client, name)
    if not metas:
        _fail("the file contains no index. Ask for a new export.")
    other = {m.get("embed_model") for m in metas} - {cfg.embed_model, None}
    if other:
        _fail(f"this index was built with {', '.join(other)}, but sumgrep.toml "
              f"uses {cfg.embed_model}. Ask whoever sent the file which to use.")
    repos = [m["repo"] for m in metas]
    hits = search(client, name, Embedder(cfg.embed_model), "error handling",
                  repo=repos if len(repos) > 1 else repos[0], k=1,
                  mode=cfg.search.mode, floor=0.0)
    if not hits:
        _fail("the test search found nothing; the file may be damaged. Ask for a new export.")
    _say(f"  test search OK (found {hits[0].symbol})")
    return metas


def _connect_claude() -> None:
    uv = shutil.which("uv") or "uv"
    add = ["mcp", "add", "--scope", "user", "sumgrep", "--",
           uv, "run", "--project", str(PROJECT_ROOT), "python", "-m", "sumgrep.mcp_server"]
    manual = "  Run this once Claude Code is installed:\n    " + _shell_line(["claude", *add])
    exe = shutil.which("claude")
    if not exe:
        _say("  The 'claude' command was not found.")
        _say(manual)
        return
    if _run([exe, "mcp", "get", "sumgrep"], quiet=True).returncode == 0:
        _say("  already connected")
        return
    if not _ask("Connect sumgrep to Claude Code (for all your projects)?"):
        _say(manual)
        return
    if _run([exe, *add]).returncode != 0:
        _say("  Connecting failed (see the message above).")
        _say(manual)
        return
    _say("  connected")


def import_bundle(file: Path, config: Path | None, collection: str | None) -> None:
    from qdrant_client import QdrantClient

    _say("Step 1 of 4: checking this computer")
    cfg = _config(config)
    _ensure_qdrant(cfg.qdrant_url)
    _ensure_ollama(cfg.embed_model)

    _say("Step 2 of 4: loading the index")
    name = collection or cfg.collection
    client = QdrantClient(url=cfg.qdrant_url)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot, cache = _unpack(file, Path(tmp))
        _restore(client, name, snapshot)
        _place_cache(cfg, cache)

    _say("Step 3 of 4: test search")
    metas = _test_search(cfg, client, name)

    _say("Step 4 of 4: connecting to Claude Code")
    _connect_claude()

    _say("\nDone. This computer can now search:")
    for m in metas:
        _say(f"  {m.get('repo')}: {m.get('chunk_count')} functions, "
             f"commit {str(m.get('git_commit', ''))[:8]}, indexed {str(m.get('indexed_at', ''))[:10]}")
    _say("Next: open a NEW Claude Code session in your project and ask something like\n"
         '  "where is the code that handles login?"')
