# sumgrep — project brief for Claude Code

Semantic code search MCP server for Claude Code. It indexes a repo's
functions by LLM-written summaries (plus the raw code), and answers
conceptual queries ("where do we retry with backoff") with **pointers** —
symbol + file + line range — never source code. The agent then reads the
real file.

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
| `src/sumgrep/chunker.py` | 1 | tree-sitter AST chunks → `Chunk(repo, path, symbol, kind, start/end_line, code, code_hash, raw_chars)`. `code_hash` = sha256 of comment-stripped tokens = the freshness key. Python, Go, TypeScript/TSX, JavaScript, C++ |
| `src/sumgrep/summarizer.py` | 2 | `Summarizer` Protocol; providers `anthropic` / `bedrock` / `ollama`; SQLite cache `~/.sumgrep/summaries.sqlite` keyed `(code_hash, model#PROMPT_HASH)`; `trim_repetition()` safety net; mush gate |
| `src/sumgrep/embedder.py` | 3 | bge-m3 via Ollama, batched, 1024 dims |
| `src/sumgrep/indexer.py` | 3 | Qdrant collection `sumgrep`: named vectors `code` + `summary` (cosine); point id `uuid5(NAMESPACE, "repo:path:symbol")`; payload = metadata + summary + code_hash + git_commit. Unchanged hash → skip; moved lines → `set_payload`; new/changed → summarize + embed + upsert; gone → delete; meta point per repo |
| `src/sumgrep/search.py` | 4 | `search()`: embed query once → `query_points` per arm (`code`/`summary`, repo-filtered) → `rrf()` client-side (c=60, optional per-arm weights) → cosine floor 0.35 → `Hit` pointers with per-arm rank+cosine. `hyde_rewrite()` via `Summarizer.complete()` |
| `src/sumgrep/mcp_server.py` | 5 | `MCPServer` (mcp 2.x; FastMCP was renamed) over stdio, or streamable HTTP (stateless, no auth) when `$SUMGREP_HTTP=HOST:PORT` is set: one shared server for a team on a private network, teammates `claude mcp add --transport http` (SETUP §7b). Tools `search_code(query,k?,mode?,repo?)` → stored pointers (`Hit.pointer`: symbol + repo-relative path + lines) + first sentence, repo name prefixed only if >1 repo indexed; `index_status(repo?)` → per repo: functions, indexed commit, indexed at. Searches all indexed repos (`MatchAny`); `repo=` narrows (a path is reduced to its folder name). No cwd, no filesystem, no git calls. Config = `$SUMGREP_CONFIG`, else this project's `sumgrep.toml`, else `~/.sumgrep/sumgrep.toml` (the only one a plugin install sees). Lazy clients; httpx logs silenced; stdout is the transport, log to stderr only. No reindex tool by design |
| `src/sumgrep/config.py` | 0/3/4 | `sumgrep.toml` loader (`./sumgrep.toml`, then `~/.sumgrep/sumgrep.toml`): `[summarize]`, `[embed]`, `[qdrant]`, `[search]` (mode/k/floor/code_weight — the defaults for `search` and the MCP server; CLI flags override), `[[repos]]` |
| `src/sumgrep/cli.py` | all | typer app: `chunk`, `summarize`, `summaries`, `index`, `meta`, `search`, `eval`, `export`/`import` (thin wrappers over `transfer.py`) |
| `src/sumgrep/transfer.py` | 5 | Share an index. `.sumgrep` = zip of `index.snapshot` (Qdrant snapshot via REST download / `recover_from_uploaded_snapshot`) + `summaries.sqlite` (sqlite backup copy); no manifest, meta points carry repos/commits/model. `import`: 4 steps — check Qdrant (offer `docker start`/`run`), Ollama + bge-m3 (offer `ollama pull`), create `~/.sumgrep/sumgrep.toml` from example if no config exists → restore (confirm replace, default yes) + cache only if none exists → test search → offer `claude mcp add` → offer to append the steering paragraph (`STEERING`, = SETUP §4.2) to `~/.claude/CLAUDE.md` unless it has a `## sumgrep` section. Asks before running anything; ASCII output (Windows console). Old bare `.snapshot` still imports |
| `.claude-plugin/plugin.json` | 5 | Plugin manifest; MCP server = `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m sumgrep.mcp_server`, venv in `${CLAUDE_PLUGIN_DATA}/venv` |

## Design rules (do not violate)

1. Index proposes, filesystem disposes: search returns pointers, never stored code.
   A pointer is function name + repo-relative path (+ lines) as stored. Never
   absolute paths; the server never locates checkouts.
2. Both vectors (code + summary) are always indexed. Which one(s) a query is
   compared against is `[search].mode` in `sumgrep.toml` (summary | code |
   hybrid = RRF fusion). Default `summary`: it beat code and hybrid on every eval so far.
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
uv run sumgrep index     --repo PATH                  # Phase 3 (incremental)
uv run sumgrep meta      --repo PATH                  # index freshness
uv run sumgrep search    --repo PATH "query" [--mode summary|code|hybrid] [--k N] [--full]
uv run sumgrep eval      --repo PATH --queries FILE [--hyde] [--kind conceptual] [--code-weight W]
uv run sumgrep export    [FILE]                       # one .sumgrep file (index + summary cache), default ~/Desktop
uv run sumgrep import    FILE.sumgrep [--collection NAME]   # sets up the machine (checks, load, test, claude mcp add)
SUMGREP_HTTP=0.0.0.0:8000 uv run python -m sumgrep.mcp_server   # shared server, http://HOST:8000/mcp (SETUP §7b)
claude plugin validate --strict DIR                   # plugin manifests (see above for DIR)
```

Qdrant dashboard: http://localhost:6333/dashboard
