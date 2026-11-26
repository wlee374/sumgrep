# sumgrep — project brief for Claude Code

Semantic code search MCP server for Claude Code. It indexes a repo's
functions by LLM-written summaries (plus the raw code), and answers
conceptual queries ("where do we retry with backoff") with **pointers** —
symbol + file + line range — never source code. The agent then reads the
real file. A repo's Markdown docs can be indexed too, embedded as written
(no LLM), and are answered with pointers to a section.

- Full specification: `docs/sumgrep-build-guide.md` (Phases 0–9, each with a checkpoint)
- User manual (every command, config keys, how to read output): `README.md`.
  End-to-end install/runbook for a new machine: `SETUP.md`. Keep both
  current when adding a command, config key, or setup step.
- Teammate page (search-only, macOS + Windows, non-technical readers):
  `QUICKSTART.md`. Plain words; keep it to install → one import command.
- Claude Code plugin: `.claude-plugin/plugin.json` (the MCP server, inline)
  and `.claude-plugin/marketplace.json` (this repo is its own marketplace).
  Bump `version` in both `plugin.json` and `pyproject.toml` on a release;
  users only get an update when it changes. Validate what users get (the
  committed tree; an untracked `CLAUDE.local.md` would fail `--strict`):
  `d=$(mktemp -d) && git archive HEAD | tar -x -C $d && claude plugin validate --strict $d && claude plugin validate --strict $d/.claude-plugin/plugin.json`
- Personal notes, if any, go in `CLAUDE.local.md` (gitignored).

## Working agreement

- Explanations short and plain. Code complete and runnable — no sketches.
- Prefer the simplest design. Remove a broken feature before layering fixes.
- Do not modify `docs/*.md` unless asked.
- Python 3.12 via uv. Run everything as `uv run sumgrep <cmd> --repo PATH`.
  Excludes come from `[[repos]]` in `sumgrep.toml` (matched by `path`).
- Nothing from an indexed codebase (paths, function names, labeled
  queries) goes into this public repo. Keep eval sets elsewhere and pass
  them with `eval --queries FILE`.

## Architecture (as built)

| File | Phase | Role |
|---|---|---|
| `src/sumgrep/chunker.py` | 1 | tree-sitter AST chunks → `Chunk(repo, path, symbol, kind, start/end_line, code, code_hash, raw_chars)`. `code_hash` = sha256 of comment-stripped tokens = the freshness key. Python, Go, TypeScript/TSX, JavaScript (`.js`/`.mjs`/`.cjs`), C++. A function over `MAX_CHUNK_CHARS` (8,000) keeps its chunk (head+tail truncated) and `split()` adds one per definition nested in it (`Outer.inner`) and one per part of the rest (`Outer#partN`, kind `part`, ≤ 8,000 chars, cut on AST boundaries, filled front to back; parts under `MIN_PART_CHARS` 400 are dropped). Measured 2026-11-23 on a React repo: 70% of oversized code was JSX `return`, 17% nested functions |
| `src/sumgrep/docs.py` | 1 | Markdown (`.md`/`.mdx`) → `Chunk`s of kind `doc`, language `markdown`, text exactly as written. `chunk_doc`: a file ≤ `MAX_DOC_CHARS` (4,000) is one piece; a bigger one is cut at headings (top level first, only as deep as needed), then blank lines, then line ends that start a table row/list item, end a sentence, or are inside a code fence; never inside a sentence, and never between a heading and the text under it; no cut point within 4,000 → one oversized piece. Symbol = heading trail (`Title > Section`, `#partN` for a section cut below its headings, ` (2)` for a repeated trail; a one-piece file = its H1 or file stem). A piece under `MIN_DOC_CHARS` (400) joins the next/previous piece if they fit and one continues the other's section or they are sibling sections; a bare heading left over is dropped. `embed_text` = `path > symbol` + blank line + text; `code_hash` = sha256 of that. Measured 2026-11-26 on 216 files: 2,349 pieces, median 1,199 chars, none over 4,000, 1,990 start at a heading, 208 are parts |
| `src/sumgrep/summarizer.py` | 2 | `Summarizer` Protocol; providers `anthropic` / `bedrock` / `ollama`; SQLite cache `~/.sumgrep/summaries.sqlite` keyed `(code_hash, model#PROMPT_HASH)`; `trim_repetition()` safety net; mush gate |
| `src/sumgrep/embedder.py` | 3 | bge-m3 via Ollama, batched, 1024 dims. Ollama reads only the first 2,048 tokens of an input and silently ignores the rest (measured 2026-11-26: same vector for 5,000 and 30,000 chars; Korean Markdown ≈ 2 chars/token, English ≈ 3.3; a per-call `num_ctx` did not change it), so the `code` vector of a function over ~5,000 chars covers only its start. About 1% of over-long inputs are rejected instead (413): `_one` then retries that input shortened by a fifth until taken |
| `src/sumgrep/indexer.py` | 3 | Qdrant collection `sumgrep`: named vectors `code` + `summary` (cosine); point id `uuid5(NAMESPACE, "repo:path:symbol")`; payload = metadata + summary + code_hash + git_commit. Unchanged hash → skip; moved lines → `set_payload`; new/changed → summarize + embed + upsert; gone → delete; meta point per repo (commit, embed model, `remote` = origin via `normalize_remote`: host/path, lowercase, no user/token/port/.git). `index_docs` does the same for doc pieces with no LLM: one vector (`code`, from `docs.embed_text`), no `summary`, payload adds `doc_date` (last commit date of the file, one `git log` per run; a changed date alone is a `set_payload`); `enabled=False` removes the repo's doc points. Each run touches only its own kind (`_existing(docs=)`), and `_write_meta` merges, so code fields (`git_commit`, `chunk_count`, …) and doc fields (`doc_commit`, `doc_count`) survive each other |
| `src/sumgrep/search.py` | 4 | `search()`: embed query once → `query_points` per arm (`code`/`summary`, repo-filtered) → `rrf()` client-side (c=60, optional per-arm weights) → cosine floor 0.35 → `Hit` pointers with per-arm rank+cosine. `hyde_rewrite()` via `Summarizer.complete()`. `docs=True` searches kind `doc` only, on the `code` vector (mode ignored); without it kind `doc` is filtered out, so functions and docs never mix. Measured 2026-11-26 (1,585 sections, mostly Korean, 12 English questions written from the file names): right file at rank 1 for 12/12; 0.7 s per section to embed |
| `src/sumgrep/rerank.py` | 7 | `[rerank]` (off by default): after retrieval, one listwise LLM call (same providers as the summarizer, own config: provider/model/think/candidates/text/timeout) reads the top 20 hits' stored summaries and returns them best-first; omitted ids keep retrieval order; any error/timeout → retrieval order + reason in `info`. Callers fetch `max(k, candidates)`, rerank, cut to k. Measured 2026-10-28 (Haiku 5.5, effort low): real hit@1 0.30→0.63, MRR 0.44→0.69, ~2 s/$0.002 per query |
| `src/sumgrep/mcp_server.py` | 5 | `MCPServer` (mcp 2.x; FastMCP was renamed) over stdio, or streamable HTTP/HTTPS via `serve(cfg)` (`sumgrep serve`; stateless, no auth; uvicorn) per `[server]` (address, https, tls_cert, tls_key; certs checked before uvicorn): one shared server for a team on a private network, teammates `claude mcp add --transport http` (SETUP §7b). `serve` only: every `search_code` call → one JSON line in `[server] search_log` (time, client address via `ctx` request, query, effective k/mode, commits, ms, hits or error; `RotatingFileHandler` 10 MB × 5 files) — the server's one file write; no per-request lines (uvicorn `access_log=False`, `mcp.server.streamable_http` silenced). Over stdio nothing is written. Tools `search_code(query,k?,mode?,repo?)` → stored pointers (`Hit.pointer`: symbol + repo-relative path + lines) + first sentence; `search_docs(query,k?,repo?)` → doc pointers (heading trail + path + lines) + the file's last-changed date, never text, no rerank, logged with `mode: "docs"`, and "no docs are indexed" when the repo's meta has no `doc_count`; `index_status(repo?)` → per repo: remote, functions, indexed commit, indexed at, and a `docs` line if it has any. One repo per search: `repo=` = a git remote in any form (`normalize_remote`, matched against meta `remote`) or the repo name (a path → its last folder); a remote never matches a repo saved with a different remote; meta without `remote` (indexed before 2026-10-27) matches by name. >1 repo indexed and no `repo=` → refused with the list (`Not searched: ...`); 1 repo → may be omitted. The instructions tell agents to always pass `git remote get-url origin`. No cwd, no checkout reads, no git calls. Config = `$SUMGREP_CONFIG`, else this project's `sumgrep.toml`, else `~/.sumgrep/sumgrep.toml` (the only one a plugin install sees). Lazy clients; httpx logs silenced; stdout is the transport, log to stderr only. No reindex tool by design |
| `src/sumgrep/config.py` | 0/3/4 | `sumgrep.toml` loader (`./sumgrep.toml`, then `~/.sumgrep/sumgrep.toml`): `[summarize]`, `[embed]`, `[qdrant]`, `[search]` (mode/k/floor/code_weight — the defaults for `search` and the MCP server; CLI flags override), `[rerank]` (`RerankCfg(SummarizeCfg)` + enabled/candidates/text; timeout 10 s, 0 retries), `[server]` (address/https/tls_cert/tls_key/search_log — `sumgrep serve` only), `[[repos]]` (name, path, exclude, `docs` = also index its Markdown files; off by default) |
| `src/sumgrep/cli.py` | all | typer app: `chunk` (`--docs` = cut the docs instead), `summarize`, `summaries`, `index` (code, then docs per `[[repos]] docs`; `--only code\|docs` leaves the other part untouched, and `--only docs` builds no summarizer; Qdrant timeout 60 s, since a 5 s write timeout on a busy machine stopped runs), `meta`, `search` (`--docs`: only `--k`/`--floor` apply), `eval`, `export`/`import` (thin wrappers over `transfer.py`), `serve` (the shared team server, `mcp_server.serve`) |
| `src/sumgrep/transfer.py` | 5 | Share an index. `.sumgrep` = zip of `index.snapshot` (Qdrant snapshot via REST download / `recover_from_uploaded_snapshot`) + `summaries.sqlite` (sqlite backup copy); no manifest, meta points carry repos/commits/model. `import`: 4 steps — check Qdrant (offer `docker start`/`run`), Ollama + bge-m3 (offer `ollama pull`), create `~/.sumgrep/sumgrep.toml` from example if no config exists → restore (confirm replace, default yes) + cache only if none exists → test search → offer `claude mcp add` → offer to append the steering paragraph (`STEERING`, = SETUP §4.2) to `~/.claude/CLAUDE.md` unless it has a `## sumgrep` section. Asks before running anything; ASCII output (Windows console). Old bare `.snapshot` still imports |
| `.claude-plugin/plugin.json` | 5 | Plugin manifest; MCP server = `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m sumgrep.mcp_server`, venv in `${CLAUDE_PLUGIN_DATA}/venv` |

## Design rules (do not violate)

1. Index proposes, filesystem disposes: search returns pointers, never stored code.
   A pointer is function name + repo-relative path (+ lines) as stored. Never
   absolute paths; the server never locates checkouts.
2. Both vectors (code + summary) are always indexed. Which one(s) a query is
   compared against is `[search].mode` in `sumgrep.toml` (summary | code |
   hybrid = RRF fusion). Default `summary`: it beat code and hybrid on every eval so far.
   Docs are the exception: one vector (`code`), their text as written. No
   LLM touches a doc, at index time or at search time.
3. Summaries keep identifiers, constants, error strings verbatim; no speculation; no narrating the rules.
4. Freshness = content hash + manual reindex. A stale index degrades routing only, never content.
5. Never use Claude Code's OAuth token for the summarizer (ToS). API key, Bedrock, or local model only.
6. The embedding model is married to the collection. Changing it means `sumgrep index --recreate`.
7. Line numbers are refreshed for every function on every index run; between runs they may drift — the symbol+file pointer is the reliable part.
8. How the server should be used lives in its `instructions` (ships with the code), never in users' CLAUDE.md.
9. Re-indexing is manual by design. No hook by default: a commit touching N functions would run the summarizer N times.

## Commands

```bash
uv run sumgrep chunk     --repo PATH --stats          # Phase 1 checkpoint
uv run sumgrep summarize --repo PATH --sample 30      # Phase 2 gate
uv run sumgrep summarize --repo PATH --dry-run        # cache hits vs misses
uv run sumgrep summaries --repo PATH [--symbol NAME]  # view cached summaries
uv run sumgrep index     --repo PATH [--only code|docs]   # Phase 3 (incremental); docs if [[repos]] docs = true
uv run sumgrep chunk     --repo PATH --docs --stats   # how the Markdown docs would be cut
uv run sumgrep meta      --repo PATH                  # index freshness
uv run sumgrep search    --repo PATH "query" [--mode summary|code|hybrid] [--k N] [--full] [--rerank]
uv run sumgrep search    --repo PATH "query" --docs [--k N]   # the repo's docs instead of its functions
uv run sumgrep eval      --repo PATH --queries FILE [--hyde] [--kind conceptual] [--code-weight W]
uv run sumgrep export    [FILE]                       # one .sumgrep file (index + summary cache), default ~/Desktop
uv run sumgrep import    FILE.sumgrep [--collection NAME]   # sets up the machine (checks, load, test, claude mcp add)
uv run sumgrep serve     [--config FILE]               # shared team server per [server]: http(s)://HOST:8000/mcp (SETUP §7b)
claude plugin validate --strict DIR                   # plugin manifests (see above for DIR)
```

Qdrant dashboard: http://localhost:6333/dashboard
