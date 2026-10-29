# src/sumgrep/cli.py
"""sumgrep command line. Grows one subcommand per phase.

Phase 1: sumgrep chunk     --repo PATH [--exclude PAT]... [--stats] [--sample N]
Phase 2: sumgrep summarize --repo PATH [--exclude PAT]... [--sample N | --dry-run]
Phase 3: sumgrep index     --repo PATH [--recreate];  sumgrep meta --repo PATH
Phase 4: sumgrep search    --repo PATH QUERY [--mode hybrid|code|summary] [--hyde]
         sumgrep eval      --repo PATH [--queries eval/queries.yaml] [--hyde]
"""
from __future__ import annotations

import os
import random
import time
from collections import Counter
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Semantic code search — build tooling.")


@app.callback()
def _root() -> None:
    """Semantic code search — build tooling."""


# ---------------------------------------------------------------- shared options

_REPO = typer.Option(..., "--repo", exists=True, file_okay=False,
                     help="Path to a git repo.")
_EXCLUDE = typer.Option([], "--exclude",
                        help="Skip paths matching this pattern (repeatable), "
                             "e.g. 'sdk/include/*'.")
_CONFIG = typer.Option(None, "--config",
                       help="sumgrep.toml (default: ./sumgrep.toml, then "
                            "~/.sumgrep/sumgrep.toml).")


def _excludes(cfg, repo: Path, cli_exclude: list[str]) -> tuple[str, ...]:
    """CLI --exclude plus any [[repos]] exclude list for this repo."""
    from .config import repo_cfg_for
    rc = repo_cfg_for(cfg, repo)
    return tuple(dict.fromkeys((rc.exclude if rc else []) + list(cli_exclude)))


# ---------------------------------------------------------------- phase 1

@app.command()
def chunk(
    repo: Path = _REPO,
    exclude: list[str] = _EXCLUDE,
    config: Path | None = _CONFIG,
    stats: bool = typer.Option(False, "--stats",
                               help="Per-language/kind tables + top-10 largest."),
    sample: int = typer.Option(0, "--sample",
                               help="Print N random chunks for spot-checks."),
) -> None:
    """Chunk a repo on AST boundaries and report what was found."""
    from .chunker import chunk_repo
    from .config import load_config

    try:                                   # chunking works without a config file
        ex = _excludes(load_config(config), repo, exclude)
    except FileNotFoundError:
        ex = tuple(exclude)
    try:
        chunks, skipped = chunk_repo(repo, ex)
    except RuntimeError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"{len(chunks)} chunks from {repo.resolve().name}")
    if skipped:
        typer.echo(f"{len(skipped)} files skipped:")
        for s in skipped[:20]:
            typer.echo(f"  - {s}")
        if len(skipped) > 20:
            typer.echo(f"  … and {len(skipped) - 20} more")

    if stats:
        by_lang = Counter(c.language for c in chunks)
        by_kind = Counter(c.kind for c in chunks)
        typer.echo("\nby language: "
                   + ", ".join(f"{k}={v}" for k, v in by_lang.most_common()))
        typer.echo("by kind:     "
                   + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
        truncated = [c for c in chunks if c.raw_chars > len(c.code)]
        typer.echo(f"truncated:   {len(truncated)}")
        typer.echo("\ntop-10 largest:")
        for c in sorted(chunks, key=lambda c: c.raw_chars, reverse=True)[:10]:
            mark = "  (truncated)" if c.raw_chars > len(c.code) else ""
            typer.echo(f"  {c.raw_chars:>7} chars  {c.symbol}  "
                       f"{c.path}:L{c.start_line}-{c.end_line}{mark}")

    if sample:
        for c in random.sample(chunks, min(sample, len(chunks))):
            typer.echo("\n" + "=" * 72)
            typer.echo(f"{c.symbol}  [{c.kind}]  "
                       f"{c.path}:L{c.start_line}-{c.end_line}  "
                       f"hash={c.code_hash[:12]}")
            typer.echo("-" * 72)
            typer.echo(c.code)


# ---------------------------------------------------------------- phase 2

def _stopped() -> None:
    """Ctrl-C: don't wait for worker threads stuck in HTTP calls — exit now.
    Finished summaries are already committed to the cache."""
    typer.echo("\nstopped — finished summaries are cached; rerun to resume")
    os._exit(130)


def _report(st) -> None:
    typer.echo(f"\ncache hits {st.hits} · summarized {st.misses - st.failed}"
               f" · failed {st.failed}")
    typer.echo(f"tokens in {st.in_tokens:,} · out {st.out_tokens:,}")
    est = st.in_tokens / 1e6 * 1.0 + st.out_tokens / 1e6 * 5.0
    typer.echo(f"≈ ${est:.2f} at Haiku-class rates ($1/$5 per MTok — "
               f"verify your actual rate)")
    for e in st.errors[:10]:
        typer.echo(f"  ! {e}")


def _elapsed(t0: float) -> str:
    """Time since t0 as '45s', '12m 05s' or '3h 07m' (full runs take hours)."""
    s = int(time.time() - t0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


@app.command()
def summarize(
    repo: Path = _REPO,
    exclude: list[str] = _EXCLUDE,
    config: Path | None = _CONFIG,
    sample: int = typer.Option(0, "--sample",
                               help="Summarize N random chunks, print the "
                                    "(code, summary) pairs, run the mush gate."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Count cache hits/misses; call nothing."),
    seed: int = typer.Option(0, "--seed",
                             help="Sample selection seed. Same seed = same "
                                  "chunks, so an interrupted sample resumes."),
    symbol: list[str] = typer.Option([], "--symbol",
                                     help="Only these symbols (repeatable); "
                                          "exact name, or a path substring."),
    force: bool = typer.Option(False, "--force",
                               help="Ignore cached summaries; regenerate."),
) -> None:
    """Write an LLM summary for every chunk (cached; only misses cost money)."""
    from .chunker import chunk_repo
    from .config import load_config
    from .summarizer import (SummaryCache, default_concurrency,
                             make_summarizer, mush_score, summarize_chunks)

    try:
        cfg = load_config(config)
        summarizer = make_summarizer(cfg.summarize)
        chunks, _ = chunk_repo(repo, _excludes(cfg, repo, exclude))
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    cache = SummaryCache(cfg.cache_dir / "summaries.sqlite")
    conc = default_concurrency(cfg.summarize)
    typer.echo(f"{len(chunks)} chunks · provider={cfg.summarize.provider} "
               f"model={cfg.summarize.model} · concurrency={conc} "
               f"· config={cfg.source}")

    if symbol:
        picks = [ch for ch in chunks
                 if ch.symbol in symbol
                 or any(s in f"{ch.path}:{ch.symbol}" for s in symbol)]
        if not picks:
            typer.echo(f"no chunk matches {symbol}", err=True)
            raise typer.Exit(1)
        typer.echo(f"matched {len(picks)} chunk(s)")
    elif sample:
        picks = random.Random(seed).sample(chunks, min(sample, len(chunks)))
    else:
        picks = []

    if picks:
        t0 = time.time()
        typer.echo(f"summarizing {len(picks)} sample chunks "
                   f"(first call includes model load)…")

        def tick(done: int, total: int) -> None:
            typer.echo(f"  {done}/{total} done · {time.time() - t0:.0f}s elapsed")

        try:
            st = summarize_chunks(picks, summarizer, cache, conc,
                                  on_progress=tick, force=force)
        except KeyboardInterrupt:
            _stopped()
        for c in picks:
            s = st.summaries.get(c.chunk_id)
            if s is None:
                continue
            typer.echo("\n" + "=" * 72)
            typer.echo(f"{c.symbol}  [{c.kind}]  "
                       f"{c.path}:L{c.start_line}-{c.end_line}")
            typer.echo("-" * 72)
            code = c.code if len(c.code) <= 1200 else c.code[:1200] + "\n…"
            typer.echo(code)
            typer.echo("--- summary ---")
            typer.echo(s)
        _report(st)
        texts = list(st.summaries.values())
        if len(texts) >= 5:
            m = mush_score(texts, cfg.embed_model)
            verdict = ("MUSH — fix the prompt before the full pass"
                       if m > 0.75 else "ok — summaries are distinct")
            typer.echo(f"\nmush gate: median pairwise cosine = {m:.3f} → {verdict}")
        return

    if dry_run:
        st = summarize_chunks(chunks, summarizer, cache, conc, dry_run=True,
                              force=force)
        est = st.misses * 500 / 1e6 * 1.0 + st.misses * 150 / 1e6 * 5.0
        typer.echo(f"cache hits {st.hits} · would summarize {st.misses}"
                   f" · rough cost ≈ ${est:.2f}")
        return

    t0 = time.time()

    def progress(done: int, total: int) -> None:
        if done % 10 == 0 or done == total:
            typer.echo(f"  {done}/{total} summarized · {_elapsed(t0)}")

    try:
        st = summarize_chunks(chunks, summarizer, cache, conc,
                              on_progress=progress, force=force)
    except KeyboardInterrupt:
        _stopped()
    _report(st)
    typer.echo(f"done in {_elapsed(t0)}")


@app.command()
def summaries(
    repo: Path = _REPO,
    exclude: list[str] = _EXCLUDE,
    config: Path | None = _CONFIG,
    limit: int = typer.Option(50, "--limit", help="Max summaries to print."),
    symbol: list[str] = typer.Option([], "--symbol",
                                     help="Only these (repeatable): exact symbol "
                                          "name, or a substring of path:symbol, "
                                          "e.g. 'search.py' or 'SummaryCache.'"),
) -> None:
    """Show cached summaries for this repo's chunks (no LLM calls)."""
    from .chunker import chunk_repo
    from .config import load_config
    from .summarizer import SummaryCache, cache_key

    try:
        cfg = load_config(config)
        chunks, _ = chunk_repo(repo, _excludes(cfg, repo, exclude))
    except (FileNotFoundError, RuntimeError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    if symbol:
        chunks = [ch for ch in chunks
                  if ch.symbol in symbol
                  or any(s in f"{ch.path}:{ch.symbol}" for s in symbol)]
        if not chunks:
            typer.echo(f"no chunk matches {symbol}", err=True)
            raise typer.Exit(1)
    cache = SummaryCache(cfg.cache_dir / "summaries.sqlite")
    key = cache_key(cfg.summarize.model)
    shown = found = 0
    for c in chunks:
        s = cache.get(c.code_hash, key)
        if s is None:
            continue
        found += 1
        if shown < limit:
            shown += 1
            typer.echo("\n" + "=" * 72)
            typer.echo(f"{c.symbol}  [{c.kind}]  "
                       f"{c.path}:L{c.start_line}-{c.end_line}")
            typer.echo("-" * 72)
            typer.echo(s)
    typer.echo(f"\n{found} of {len(chunks)} chunks have a cached summary "
               f"for model {cfg.summarize.model} (showing {shown})")


# ---------------------------------------------------------------- phase 3

@app.command()
def index(
    repo: Path = _REPO,
    exclude: list[str] = _EXCLUDE,
    config: Path | None = _CONFIG,
    recreate: bool = typer.Option(False, "--recreate",
                                  help="Drop and rebuild the collection "
                                       "(required after changing the embedding model)."),
) -> None:
    """Bring the Qdrant index up to date with the repo (incremental)."""
    from qdrant_client import QdrantClient
    from .config import load_config
    from .embedder import Embedder
    from .indexer import index_repo
    from .summarizer import SummaryCache, default_concurrency, make_summarizer

    try:
        cfg = load_config(config)
        summarizer = make_summarizer(cfg.summarize)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)

    from .config import repo_cfg_for
    rc = repo_cfg_for(cfg, repo)
    ex = _excludes(cfg, repo, exclude)
    typer.echo(f"repo config: {rc.name if rc else 'NONE — no [[repos]] entry matches '
               + str(repo.resolve())} · excludes: {list(ex) or 'none'}")

    t0 = time.time()
    try:
        st = index_repo(
            repo, ex,
            QdrantClient(url=cfg.qdrant_url), cfg.collection,
            Embedder(cfg.embed_model), summarizer,
            SummaryCache(cfg.cache_dir / "summaries.sqlite"),
            default_concurrency(cfg.summarize),
            log=lambda s: typer.echo(s), recreate=recreate)
    except RuntimeError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        _stopped()

    typer.echo(f"\ndone in {time.time() - t0:.0f}s · chunks {st.chunks} · "
               f"unchanged {st.unchanged} · lines refreshed {st.lines_refreshed} · "
               f"embedded {st.upserted} · deleted {st.deleted} · "
               f"LLM calls {st.summarized} · failed {st.failed}")
    for e in st.errors[:10]:
        typer.echo(f"  ! {e}")


@app.command()
def meta(
    repo: Path = _REPO,
    config: Path | None = _CONFIG,
    last_commit: bool = typer.Option(False, "--last-commit",
                                     help="Print only the indexed commit hash."),
) -> None:
    """Show what the index knows about this repo (freshness)."""
    from qdrant_client import QdrantClient
    from .config import load_config
    from .indexer import git_dirty_count, git_head, read_meta

    cfg = load_config(config)
    m = read_meta(QdrantClient(url=cfg.qdrant_url), cfg.collection,
                  repo.resolve().name)
    if m is None:
        typer.echo("not indexed" if not last_commit else "")
        raise typer.Exit(1 if last_commit else 0)
    if last_commit:
        typer.echo(m["git_commit"])
        return
    head = git_head(repo)
    typer.echo(f"repo            {m['repo']}")
    typer.echo(f"remote          {m.get('remote') or '- (saved by the next index run, if the repo has an origin)'}")
    typer.echo(f"chunks          {m['chunk_count']}")
    typer.echo(f"indexed commit  {m['git_commit'][:10]}  "
               f"({'current' if m['git_commit'] == head else 'BEHIND HEAD ' + head[:10]})")
    typer.echo(f"dirty files     {git_dirty_count(repo)}")
    typer.echo(f"embed model     {m['embed_model']} ({m['dims']} dims)")
    typer.echo(f"summarizer      {m['summarize_model']}")
    typer.echo(f"indexed at      {m['indexed_at']}")


# ---------------------------------------------------------------- phase 4

def _open_index(config: Path | None):
    """(cfg, client, embedder) — shared by search and eval."""
    from qdrant_client import QdrantClient
    from .config import load_config
    from .embedder import Embedder

    cfg = load_config(config)
    return cfg, QdrantClient(url=cfg.qdrant_url), Embedder(cfg.embed_model)


@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language question."),
    repo: Path = _REPO,
    config: Path | None = _CONFIG,
    k: int = typer.Option(None, "--k",
                          help="Max pointers to return (default [search].k)."),
    mode: str = typer.Option(None, "--mode",
                             help="summary | code | hybrid "
                                  "(default [search].mode in sumgrep.toml)."),
    floor: float = typer.Option(None, "--floor",
                                help="Drop hits whose best cosine is below this "
                                     "(default [search].floor)."),
    hyde: bool = typer.Option(False, "--hyde",
                              help="Rewrite the query as a hypothetical summary "
                                   "with the summarizer LLM, embed that instead."),
    full: bool = typer.Option(False, "--full",
                              help="Print the whole summary, not just the "
                                   "first sentence."),
    code_weight: float = typer.Option(None, "--code-weight",
                                      help="Scale the code arm in hybrid RRF "
                                           "(default [search].code_weight)."),
    rerank: bool = typer.Option(None, "--rerank/--no-rerank",
                                help="Reorder the top hits with the [rerank] LLM, "
                                     "which reads their summaries (default "
                                     "[rerank].enabled)."),
) -> None:
    """Semantic search: pointers (symbol, file, lines), never source code."""
    from .search import MODES, hyde_rewrite, search as run_search

    try:
        cfg, client, embedder = _open_index(config)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    sc = cfg.search
    mode = mode or sc.mode
    k = k or sc.k
    floor = sc.floor if floor is None else floor
    code_weight = sc.code_weight if code_weight is None else code_weight
    if mode not in MODES:
        typer.echo(f"error: mode must be one of {MODES}, got {mode!r} "
                   f"(--mode or [search].mode in {cfg.source})", err=True)
        raise typer.Exit(1)

    embed_text = None
    if hyde:
        from .summarizer import make_summarizer
        t0 = time.time()
        embed_text = hyde_rewrite(query, make_summarizer(cfg.summarize))
        typer.echo(f"hyde ({time.time() - t0:.1f}s): {embed_text}\n")

    rc = cfg.rerank
    use_rerank = rc.enabled if rerank is None else rerank
    t0 = time.time()
    hits = run_search(client, cfg.collection, embedder, query,
                      repo=repo.resolve().name, floor=floor, mode=mode,
                      k=max(k, rc.candidates) if use_rerank else k,
                      embed_text=embed_text, code_weight=code_weight)
    ms = (time.time() - t0) * 1000
    info = None
    if use_rerank and hits:
        from .rerank import make_reranker, rerank as run_rerank
        try:
            llm = make_reranker(rc)
        except Exception as e:
            info = {"model": rc.model, "error": f"{type(e).__name__}: {e}"}
        else:
            hits, info = run_rerank(query, hits, llm, rc)
    hits = hits[:k]
    if not hits:
        typer.echo(f"no hits ≥ floor  ({ms:.0f} ms)")
        return
    for h in hits:
        typer.echo(h.pointer)
        typer.echo(f"   {h.summary if full else h.first_sentence}")
        typer.echo(f"   rrf {h.score:.4f} · {h.arms_str()}")
    typer.echo(f"\n{len(hits)} hits · mode={mode}"
               f"{f' code_weight={code_weight}' if mode == 'hybrid' else ''}"
               f" · floor {floor} · {ms:.0f} ms")
    if info is not None:
        if "error" in info:
            typer.echo(f"rerank skipped ({info['error']}); retrieval order shown")
        else:
            typer.echo(f"reranked by {info['model']} · {info['n']} candidates · "
                       f"{info['ms']} ms")


@app.command()
def eval(
    repo: Path = _REPO,
    config: Path | None = _CONFIG,
    queries: Path = typer.Option(Path("eval/queries.yaml"), "--queries",
                                 exists=True, dir_okay=False,
                                 help="Labeled queries (see eval/queries.yaml)."),
    k: int = typer.Option(10, "--k", help="Ranks past this count as a miss."),
    hyde: bool = typer.Option(False, "--hyde",
                              help="Also run every arm with a HyDE-rewritten "
                                   "query (one LLM call per query)."),
    kind: str | None = typer.Option(None, "--kind",
                                    help="Only queries of this kind "
                                         "(conceptual | literal)."),
    code_weight: float = typer.Option(None, "--code-weight",
                                      help="Scale the code arm in hybrid RRF "
                                           "(default [search].code_weight)."),
    rerank: bool = typer.Option(False, "--rerank",
                                help="Also score every arm after the [rerank] "
                                     "LLM reorders its top candidates (one LLM "
                                     "call per arm per query)."),
) -> None:
    """hit@1 / hit@5 / MRR per retrieval arm over eval/queries.yaml."""
    import yaml
    from .search import MODES, hyde_rewrite, search as run_search

    spec = yaml.safe_load(queries.read_text())
    qs = [q for q in spec["queries"] if kind is None or q.get("kind") == kind]
    if not qs:
        typer.echo("no queries selected", err=True)
        raise typer.Exit(1)
    try:
        cfg, client, embedder = _open_index(config)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    repo_name = repo.resolve().name
    if code_weight is None:
        code_weight = cfg.search.code_weight
    if spec.get("repo") and spec["repo"] != repo_name:
        typer.echo(f"warning: queries.yaml is for repo {spec['repo']!r}, "
                   f"searching {repo_name!r}", err=True)

    def matches(hit, answers: list[str]) -> bool:
        for a in answers:
            path, _, sym = a.rpartition(":")
            if hit.symbol == sym and (not path or hit.path == path):
                return True
        return False

    def rank_of(hits, answers) -> int | None:
        for i, h in enumerate(hits, start=1):
            if matches(h, answers):
                return i
        return None

    arms = list(MODES)                      # hybrid, code, summary
    variants = [(a, False) for a in arms] + ([(a, True) for a in arms] if hyde else [])
    labels = [f"{a}{'+hyde' if h else ''}" for a, h in variants]
    rc, llm = cfg.rerank, None
    if rerank:
        from .rerank import make_reranker
        try:
            llm = make_reranker(rc)
        except Exception as e:
            typer.echo(f"error: cannot build the [rerank] client: {e}", err=True)
            raise typer.Exit(1)
        labels += [f"rerank({lb})" for lb in labels]
    ranks: dict[str, list[int | None]] = {lb: [] for lb in labels}

    summarizer = None
    if hyde:
        from .summarizer import make_summarizer
        summarizer = make_summarizer(cfg.summarize)

    w = max(len(lb) for lb in labels)
    typer.echo(f"{len(qs)} queries · repo {repo_name} · k={k}"
               f" · code_weight={code_weight}\n")
    for q in qs:
        text, answers = q["q"], q["answers"]
        embed_hyde = hyde_rewrite(text, summarizer) if hyde else None
        row, rr_row = [], []
        for (arm, use_hyde), lb in zip(variants, labels):
            hits = run_search(client, cfg.collection, embedder, text,
                              repo=repo_name, mode=arm, floor=0.0,
                              k=max(k, rc.candidates) if llm else k,
                              embed_text=embed_hyde if use_hyde else None,
                              code_weight=code_weight)
            r = rank_of(hits[:k], answers)
            ranks[lb].append(r)
            row.append(f"{lb}={r if r else '-':<3}")
            if llm:
                from .rerank import rerank as run_rerank
                rr, _info = run_rerank(text, hits, llm, rc)
                r2 = rank_of(rr[:k], answers)
                ranks[f"rerank({lb})"].append(r2)
                rr_row.append(f"rerank({lb})={r2 if r2 else '-':<3}")
        typer.echo(f"[{q.get('kind', '?')[:4]}] {text[:64]:<64}  " + "  ".join(row + rr_row))
        if embed_hyde:
            typer.echo(f"       hyde: {embed_hyde[:120]}")

    def stat(rs: list[int | None]):
        n = len(rs)
        h1 = sum(1 for r in rs if r == 1) / n
        h5 = sum(1 for r in rs if r and r <= 5) / n
        mrr = sum(1.0 / r for r in rs if r) / n
        return h1, h5, mrr

    typer.echo(f"\n{'arm':<{w}}  hit@1  hit@5  MRR@{k}")
    for lb in labels:
        h1, h5, mrr = stat(ranks[lb])
        typer.echo(f"{lb:<{w}}  {h1:5.2f}  {h5:5.2f}  {mrr:5.2f}")
    kinds = {q.get("kind", "?") for q in qs}
    if len(kinds) > 1:
        for kd in sorted(kinds):
            idx = [i for i, q in enumerate(qs) if q.get("kind", "?") == kd]
            typer.echo(f"\n{kd} ({len(idx)})")
            for lb in labels:
                h1, h5, mrr = stat([ranks[lb][i] for i in idx])
                typer.echo(f"  {lb:<{w}}  {h1:5.2f}  {h5:5.2f}  {mrr:5.2f}")


# ---------------------------------------------------------------- transfer

@app.command(name="export")
def export_cmd(
    out: Path | None = typer.Argument(
        None, help="Where to write it (default: Desktop/sumgrep-index-DATE.sumgrep)."),
    config: Path | None = _CONFIG,
) -> None:
    """Write the whole index (all repos) + summary cache to ONE .sumgrep file to share."""
    from .config import load_config
    from .transfer import default_export_path, export_bundle

    export_bundle(load_config(config), out or default_export_path())


@app.command(name="import")
def import_cmd(
    file: Path = typer.Argument(..., exists=True, dir_okay=False,
                                help="The .sumgrep file (an older .snapshot also works)."),
    config: Path | None = _CONFIG,
    collection: str | None = typer.Option(None, "--collection",
                                          help="Restore under this name instead "
                                               "of the one in sumgrep.toml."),
) -> None:
    """Set up this computer from a shared .sumgrep file: checks Qdrant and
    Ollama (offers to fix), loads the index, test-searches, connects Claude Code."""
    from .transfer import import_bundle

    import_bundle(file, config, collection)


# ---------------------------------------------------------------- shared server

@app.command()
def serve(config: Path | None = _CONFIG) -> None:
    """Run the shared team server: search over HTTP or HTTPS for teammates'
    Claude Code, as set in the server section of sumgrep.toml. Ctrl-C stops it."""
    from .config import load_config
    from .mcp_server import serve as serve_http

    try:
        cfg = load_config(config)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    serve_http(cfg)


if __name__ == "__main__":
    app()
