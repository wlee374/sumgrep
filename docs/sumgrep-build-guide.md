# sumgrep — build guide (zero-base edition)

Summary-embedding semantic code search for Claude Code. Search natural-language summaries of your functions, get back **pointers** (symbol + path + lines), let the agent grep/read the live source. Working name sumgrep; rename freely.

This edition assumes a **bare machine**: no Qdrant, no Ollama, no models, no Python project. Phase 0 takes you from nothing to a proven working loop.

**The thesis being tested** (Greptile's published finding, your independent idea): embedding an LLM-written functionality summary of each function outperforms embedding the raw code for conceptual queries. This guide builds the tool *and* the benchmark that proves or kills the thesis on your own repos.

**Architecture:**

```
                    INDEXER (CLI, runs on git commit)

repo ──git ls-files──▶ tree-sitter chunker ──▶ hash check ──▶ summarizer (LLM)
                                                  │ unchanged      │
                                                  ▼ skip           ▼
                                             (done)      embed code + summary
                                                          (Ollama bge-m3 ×2)
                                                                   │
                                                                   ▼
                                                   Qdrant: named vectors + payload

                    MCP SERVER (stdio, read-only)

Claude Code ──search_code(query)──▶ embed query ──▶ search both vectors ──▶ RRF
                                                                            │
                    ◀── pointers: symbol · path · L120-168 · summary ◀──────┘

then: Claude greps the symbol in that file / reads those lines  → ground truth
```

Design rules carried over from our discussion:

1. **Index proposes, filesystem disposes.** Never return stored source as truth.
2. **Dual vectors** (code + summary) fused with RRF — keeps exact-ish recall.
3. **Summaries must retain identifiers and notable constants** verbatim.
4. **Freshness = chunk-hash cache + commit-triggered reindex.** Stale index degrades routing, never content.
5. **No Claude Code OAuth token for the summarizer.** API key or local model.

## Phase 0 — Bootstrap from nothing (~1 hour)

Four installs, one credential, one smoke test. What each piece is for:

| Piece | Role | Runs as |
|---|---|---|
| uv | Python project + env manager | binary in your PATH |
| Docker → Qdrant | the vector database | container on :6333 |
| Ollama | serves the embedding model locally | native app/service on :11434 |
| bge-m3 | the embedding model (1024-dim) | pulled into Ollama |
| Anthropic API key *or* local Qwen | the summarizer LLM | env var / Ollama |

### 0.1 — uv

```
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

### 0.2 — Qdrant (in Docker)

```
docker --version        # if missing: https://docs.docker.com/get-docker/
                        # macOS: Docker Desktop must be running before the next step
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v qdrant_storage:/qdrant/storage \
  qdrant/qdrant
docker update --restart unless-stopped qdrant   # survive reboots
```

Verify:

```
curl -s localhost:6333/readyz    # → "all shards are ready"
```

Bookmark the dashboard: http://localhost:6333/dashboard — you will use it constantly to eyeball collections, points, and payloads while debugging.

The named volume `qdrant_storage` means your index survives container restarts. `docker start qdrant` brings it back if it's ever stopped.

### 0.3 — Ollama (native install, not Docker)

Native on purpose: on macOS, Docker containers get no GPU access, and embedding thousands of chunks on CPU-in-a-VM is painful. Native Ollama uses Metal (macOS) or CUDA (Linux) automatically.

```
# macOS
brew install ollama
brew services start ollama        # or run the Ollama.app from ollama.com
# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

Verify the server is up:

```
curl -s localhost:11434           # → "Ollama is running"
```

### 0.4 — The embedding model

```
ollama pull bge-m3                # ~1.2 GB download
```

Verify it actually embeds:

```
curl -s http://localhost:11434/api/embed \
  -d '{"model":"bge-m3","input":"hello world"}' | head -c 120
# → {"model":"bge-m3","embeddings":[[-0.032,0.011,...
```

Each input becomes **1,024 floats**. That number is now married to your Qdrant collection — changing embedding models later means re-embedding everything.

### 0.5 — Summarizer credential (pick one, interface stays swappable)

| Option | Cost | Quality | Setup |
|---|---|---|---|
| **A. Anthropic API key + claude-haiku-4-5** (default) | few $ first index, pennies after | high | below |
| B. Local qwen3:8b via Ollama | free | good | `ollama pull qwen3:8b` (~5 GB; wants 6–10 GB RAM/VRAM) |

Option A setup: create a key at console.anthropic.com → API Keys, load a small credit ($5 covers the first full index of a ~5k-function repo; verify current per-token pricing in the docs), then:

```
echo 'export ANTHROPIC_API_KEY="sk-ant-api03-..."' >> ~/.zshrc
source ~/.zshrc
```

Note the prefix: `sk-ant-api...` is a real API key. The `sk-ant-oat...` subscription token from Claude Code's login is **not** usable here — that's the ToS line we established earlier. Don't wire it in.

### 0.6 — Project scaffold

```
mkdir sumgrep && cd sumgrep && git init
uv init --package .
uv add tree-sitter tree-sitter-language-pack qdrant-client ollama \
       anthropic "mcp[cli]" typer pydantic
```

- tree-sitter-language-pack — prebuilt grammars for Go, Python, TypeScript, C++ (all your languages), no parser compilation.
- mcp[cli] — official Python SDK; FastMCP gives you a stdio server in ~20 lines.

### 0.7 — Smoke test: prove the whole loop before writing real code

`scripts/smoke.py`:

```python
import ollama
from qdrant_client import QdrantClient, models

texts = ["retries a request with exponential backoff",
         "parses a CSV file into rows"]
vecs = ollama.embed(model="bge-m3", input=texts)["embeddings"]
q = ollama.embed(model="bge-m3",
                 input=["a function that retries failed calls"])["embeddings"][0]

c = QdrantClient(url="http://localhost:6333")
if c.collection_exists("smoke"):
    c.delete_collection("smoke")
c.create_collection("smoke", vectors_config=models.VectorParams(
    size=len(vecs[0]), distance=models.Distance.COSINE))
c.upsert("smoke", points=[
    models.PointStruct(id=i, vector=v, payload={"text": t})
    for i, (v, t) in enumerate(zip(vecs, texts))])
hit = c.query_points("smoke", query=q, limit=1).points[0]
print(f"nearest: {hit.payload['text']!r}  (score {hit.score:.3f})")
c.delete_collection("smoke")
```

```
uv run python scripts/smoke.py
# expected: nearest: 'retries a request with exponential backoff'  (score 0.6-0.8)
```

If the nearest hit is the retry sentence — with zero shared words with the query beyond "retries/retry" stems — then Ollama, bge-m3, Qdrant, and both Python clients all work, and semantic matching is demonstrably happening on your machine. **Everything after this point is your code, not your environment.**

### 0.8 — Config file

`sumgrep.toml` (in the sumgrep repo, not the target repo):

```toml
[qdrant]
url = "http://localhost:6333"
collection = "sumgrep"

[embed]
provider = "ollama"          # http://localhost:11434
model = "bge-m3"
dims = 1024                  # married to the collection; assert on startup

[summarize]
provider = "anthropic"       # or "ollama"
model = "claude-haiku-4-5"   # key from env ANTHROPIC_API_KEY
max_words = 120

[[repos]]
name = "rvsim"
path = "/Users/wonjun/code/rvsim"    # adjust
```

## Phase 1 — Chunker (the foundation)

**Goal:** `chunk(repo_path) -> list[Chunk]` — one record per function, method, or class, cut on AST boundaries. Everything downstream consumes this.

```python
# sumgrep/chunker.py
from dataclasses import dataclass
import hashlib, subprocess
from tree_sitter_language_pack import get_parser

@dataclass(frozen=True)
class Chunk:
    repo: str
    path: str          # relative to repo root
    language: str
    symbol: str        # qualified: "Router.handle", "backoffLoop"
    kind: str          # "function" | "method" | "class"
    start_line: int    # 1-based, inclusive
    end_line: int
    code: str
    code_hash: str     # sha256 of normalized body — the freshness key

EXT_LANG = {".py": "python", ".go": "go", ".ts": "typescript",
            ".tsx": "tsx", ".js": "javascript", ".cpp": "cpp",
            ".cc": "cpp", ".hpp": "cpp", ".h": "cpp"}

CHUNK_NODES = {
    "python":     {"function_definition", "class_definition"},
    "go":         {"function_declaration", "method_declaration"},
    "typescript": {"function_declaration", "method_definition",
                   "class_declaration", "arrow_function"},  # arrow: named only
    "cpp":        {"function_definition", "class_specifier"},
}
```

Implementation notes (the parts that actually bite):

1. **File source = `git ls-files`.** Respects .gitignore, skips vendored deps, and defines "the repo" the same way git does:
   ```python
   files = subprocess.check_output(
       ["git", "-C", repo_path, "ls-files"], text=True).splitlines()
   ```
2. **Walk the AST; emit a chunk per matching node.** Qualified symbol names: walk up the tree collecting enclosing class/receiver names (`ClassName.method`; Go receiver type for methods). For anonymous arrow functions, only chunk those bound to a const/let name — skip inline callbacks (noise).
3. **Nested functions:** emit the outer function as one chunk *including* its nested helpers; do not double-emit inner defs. Classes: emit the class chunk as a *skeleton* (signature + docstring + method signatures), plus each method as its own full chunk — avoids giant duplicated class blobs.
4. **Normalized hash** (your staleness-precision requirement): strip comment nodes and collapse whitespace before hashing, so comment/formatting edits don't trigger re-summarization:
   ```python
   def normalized_hash(node, source: bytes) -> str:
       parts = []
       def walk(n):
           if "comment" in n.type: return
           if n.child_count == 0:
               parts.append(source[n.start_byte:n.end_byte])
           for c in n.children: walk(c)
       walk(node)
       return hashlib.sha256(b" ".join(parts)).hexdigest()
   ```
5. **Size cap:** if a chunk exceeds ~8,000 chars, keep the record but truncate code for downstream use to head + tail (~3k each) and log it. Giant functions are rare; don't engineer around them yet.

**Checkpoint (empirical):** run on one real repo.

```
uv run sumgrep chunk --repo ~/code/rvsim --stats
# expect: chunk count, per-language counts, top-10 largest
```

Sanity: Go chunk count ≈ `grep -rc "^func " --include="*.go" . | paste -sd+ | bc` (± methods). Spot-check 20 random chunks: correct boundaries, correct qualified names. **Do not proceed until this looks right** — every downstream artifact inherits chunker mistakes.

## Phase 2 — Summarizer

**Goal:** `summarize(chunk) -> str`, cached by `code_hash`.

**Provider interface** (so Haiku ↔ Qwen is a config change):

```python
class Summarizer(Protocol):
    def summarize(self, chunk: Chunk) -> str: ...
```

**The prompt** — this is the product; iterate on it, version it in git:

```
You are indexing a codebase for semantic search. Write a retrieval summary
of this {language} {kind}.

File: {path}
Code:
{code}

Rules:
- ≤ {max_words} words, plain prose, no markdown, no praise, no hedging.
- Sentence 1: what it does (purpose). Sentence 2: how (mechanism).
- Then: inputs/outputs, side effects (I/O, network, DB, globals, mutation).
- KEEP VERBATIM: the symbol name `{symbol}`, plus any constants, error
  strings, env vars, magic numbers, and external service names that appear.
- Name the domain concepts involved (e.g. "exponential backoff",
  "order book", "TLB lookup") even if the code never uses those words.
- If it is trivial glue (<5 lines), one sentence is enough.
```

The KEEP VERBATIM rule preserves exact-match recall inside the summary vector; the domain-concepts rule buys the conceptual-query win.

**Cache = incremental indexing.** SQLite table `summaries(code_hash TEXT PRIMARY KEY, summary TEXT, model TEXT, created_at)`. Look up before calling the LLM. This one table is why re-indexing after a commit costs pennies: only changed hashes miss.

**Concurrency:** asyncio + semaphore(8) for API calls; exponential backoff on 429/5xx; write cache rows as they complete so a crash resumes for free.

**Checkpoint (quality gate — do not skip):**

```
uv run sumgrep summarize --repo rvsim --sample 30   # prints 30 (code, summary) pairs
```

Manual rubric on the 30: purpose correct? identifiers retained? concepts named? Then the **mush detector**: embed all 30 summaries, compute median pairwise cosine. If it's > ~0.75, your summaries all say "this function processes the data" — fix the prompt before spending money on the full pass. Then:

```
uv run sumgrep summarize --repo rvsim        # full pass; prints token/cost total
```

## Phase 3 — Embedding + Qdrant schema

**Collection** with named vectors — one point per chunk carries both spaces:

```python
from qdrant_client import QdrantClient, models

client.create_collection(
    "sumgrep",
    vectors_config={
        "code":    models.VectorParams(size=1024, distance=models.Distance.COSINE),
        "summary": models.VectorParams(size=1024, distance=models.Distance.COSINE),
    },
)
client.create_payload_index("sumgrep", "repo", models.PayloadSchemaType.KEYWORD)
```

**Point identity:** `id = uuid5(NAMESPACE, f"{repo}:{path}:{symbol}")`. Stable across edits → an edited function **upserts over itself**.

**Payload:** repo, path, symbol, kind, start_line, end_line, summary, code_hash, git_commit, indexed_at. (No source code in v1 — pointer design. Add a snippet field later only if reranking needs it.)

**Indexing driver** (`sumgrep index --repo X`):

1. `chunks = chunk(repo)`; fetch existing `{point_id: code_hash}` for the repo (Qdrant scroll with payload filter).
2. For each chunk: hash unchanged → skip. Else summarize (cache) → `ollama.embed(model="bge-m3", input=[code, summary])` → upsert (batch 64).
3. **Deletion pass:** points whose id ∉ current chunk ids → delete. Handles removed *and renamed* symbols (rename = delete + insert; old summary work is lost — acceptable, renames are semantic changes anyway).
4. Record `git rev-parse HEAD` into a meta point → powers `index_status`.

**Checkpoint:** `sumgrep index --repo rvsim` twice. Second run must report 0 summarized, 0 embedded and finish in seconds — the incremental proof. Edit one function body, rerun: exactly 1 re-summarized. Edit only a comment: 0 (the normalized hash earning its keep). Open the Qdrant dashboard and eyeball five points' payloads.

## Phase 4 — Retrieval + fusion

```python
def search(query: str, repo: str, k: int = 5, mode="hybrid") -> list[Hit]:
    qv = embed(query)                       # ONE embedding call, reused
    flt = models.Filter(must=[models.FieldCondition(
        key="repo", match=models.MatchValue(value=repo))])
    arms = {"code": qv, "summary": qv} if mode == "hybrid" else {mode: qv}
    ranked = {name: client.query_points("sumgrep", query=v, using=name,
                                        query_filter=flt, limit=20).points
              for name, v in arms.items()}
    return rrf(ranked, k)

def rrf(ranked: dict[str, list], k: int, c: int = 60):
    scores = defaultdict(float)
    for arm in ranked.values():
        for rank, hit in enumerate(arm):
            scores[hit.id] += 1.0 / (c + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    ...  # keep per-arm rank + cosine on each Hit for debugging
```

- **Floor:** drop hits whose best cosine < 0.35 (tune with data later); better 2 good pointers than 5 with noise.
- Qdrant's Query API can do prefetch+RRF **server-side**; client-side first — you want to *see* both arms while tuning. Swap later for one round-trip.
- **HyDE flag** (`--hyde`): one small-LLM call rewrites the query as a hypothetical docstring, embed that instead. Cheap fifth benchmark arm.

**Checkpoint:** write `eval/queries.yaml` — 10 conceptual queries with known answer symbols from your repo. `sumgrep eval` prints hit@5 for code-only / summary-only / hybrid. This 20-minute mini-eval is your first real datapoint on the thesis — before any agent is involved.

## Phase 5 — MCP server

```python
# sumgrep/mcp_server.py
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("sumgrep")

@mcp.tool()
def search_code(query: str, k: int = 5, mode: str = "hybrid") -> str:
    """Semantic search over this repo's functions BY MEANING.
    Use for conceptual queries: 'code that debounces saves',
    'where retry backoff is implemented'. Do NOT use for literal
    identifiers or exact strings — use Grep for those.
    Returns pointers (symbol, file, lines, summary), never source code.
    Fetch the real code by reading the file at the given lines."""
    hits = search(query, repo=current_repo(), k=k, mode=mode)
    return "\n".join(
        f"{h.symbol}  {h.path}:L{h.start}-{h.end}  [{h.kind}]\n"
        f"   {h.summary_first_sentence}" for h in hits) or "no hits ≥ threshold"

@mcp.tool()
def index_status() -> str:
    """Freshness of the semantic index: chunk count, last indexed commit,
    commits behind HEAD, dirty files. If stale, results may point at moved
    code — trust the file over the pointer."""
    ...

if __name__ == "__main__":
    mcp.run()   # stdio
```

Notes:

- **The docstrings are load-bearing** — they're what the model reads when routing between your tool and grep. The "do NOT use for literal identifiers" line implements the two-lane design.
- **No `reindex` tool on purpose:** indexing is a long-running write; keep it a human-triggered CLI/hook, out of the agent's reach.
- Compact output (~40 tokens/hit) is the token-economics win — resist adding code snippets to the output.

**Register + steer:**

```
claude mcp add --scope user sumgrep -- \
  uv run --project ~/code/sumgrep python -m sumgrep.mcp_server
```

`~/.claude/CLAUDE.md`:

```
## sumgrep
Semantic code search by meaning. For "where is the code that does X" use
sumgrep search_code first, then Read/Grep the returned file+lines for ground
truth. For exact identifiers, strings, or error messages, use Grep directly.
Check index_status if results look off.
```

**Checkpoint:** fresh Claude Code session in the repo. Ask *"where do we handle <concept only you'd phrase this way>?"* — watch the transcript: search_code → pointer → Read/Grep on that file. That two-step is the whole design working.

## Phase 6 — Freshness hook

`.git/hooks/post-commit` in each indexed repo (`chmod +x`):

```sh
#!/bin/sh
(cd "$(git rev-parse --show-toplevel)" && \
 uv run --project ~/code/sumgrep sumgrep index --repo . --quiet &)
```

Commit-triggered, backgrounded, cheap (hash cache). Working-tree edits between commits show as "dirty" in `index_status` — and by the pointer design, lag costs routing quality only, never wrong code. Skip file-watchers in v1; add watchdog later only if commit granularity annoys you.

## Phase 7 — The benchmark (the deliverable that matters)

Two layers, run in this order:

**7a. Retrieval eval (objective, no agent variance).** Grow `eval/queries.yaml` to ~25 labeled pairs on your biggest repo: 17 conceptual, 8 literal (symbol/string) as the control group. Report hit@1 / hit@5 / MRR per arm: code, summary, hybrid, hybrid+hyde. This is the direct test of the Greptile 12% claim on *your* code. An afternoon, fully scripted, rerunnable after every prompt tweak.

**7b. End-to-end agent eval (the economics).** 12 tasks phrased as real work ("fix/explain the code that ..."). Arms: **A** grep-only (server removed), **B** hybrid sumgrep. Fresh Claude Code session per task, identical prompt template. Record per task: solved?, tool-call count, total tokens (session transcript / /context), wall time. 2 × 12 sessions ≈ one evening.

**Decision rule (pre-commit to it):** adopt if conceptual hit@5 improves materially in 7a **and** 7b shows fewer tool calls or tokens on conceptual tasks **and** the literal control group doesn't regress (it can't, if routing works — grep still owns that lane). Publish the table either way; a clean negative result on your own repos is still a strong ML-engineering artifact.

## Pitfalls (read before Phase 1)

- **Embedding model is married to the collection.** Switching from bge-m3 later = full re-embed of every point. Record the model name in collection meta and assert on startup.
- **Multi-repo:** one collection + repo payload filter (already in the schema). Never search un-filtered.
- **First index of a big repo is the expensive step** — run the Phase 2 sample + mush gate *before* the full pass, not after.
- **Don't index sumgrep with itself until it works.** Bootstrap on a repo you know cold, so you can judge retrieval quality by eye.
- **Renames** lose their summary cache entry (id changes). Correct and fine.
- **C++ headers:** declarations without bodies produce empty-ish chunks — filter chunks whose body has < 3 statements.
- **Laptop reboot amnesia:** Qdrant restarts via Docker policy; Ollama via brew services (macOS) or systemd (Linux). If searches suddenly 500, check both with the two Phase 0 verify curls before debugging your code.

## Build order recap

0. bootstrap + smoke test (~1 hr) → 1. chunker (evening) → 2. summarizer + quality gate (evening) → 3. schema + incremental index (evening) → 4. retrieval + mini-eval (evening) → 5. MCP + CLAUDE.md (an hour) → 6. hook (10 min) → 7. benchmark (afternoon + evening).

Each phase ends runnable and testable. Nothing in 5–7 starts until the Phase 4 mini-eval shows the summary arm doing *something* — if it doesn't beat code-only on even 10 queries, stop and fix the prompt, not the plumbing.
