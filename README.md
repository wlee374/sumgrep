# sumgrep

Semantic code search for Claude Code. Every function in a repo gets a
one-paragraph English summary written by an LLM. You search those
summaries by meaning ("where do we merge the results of two searches")
and get back **pointers**: function name, file, line range. The tool never
returns source code; you (or Claude) read the real file.

```
> where do we merge the ranked lists from the two searches?

search_code  →  rrf  src/sumgrep/search.py:L83-114  [function]
                   The function `rrf` implements Reciprocal Rank Fusion to merge ...
Read         →  src/sumgrep/search.py, lines 83-114
```

Grep finds the names you already know. sumgrep finds the code when you
only know what it does.

## What you need

- **Docker**, running Qdrant (the vector database) on port 6333.
- **Ollama** with `bge-m3` (embeddings, ~1.2 GB) on port 11434.
- **uv**, the Python project runner (it fetches Python 3.12 itself).
- **A summarizer**, only for building the index. Default: `qwen3.5:9b`
  through Ollama (6.6 GB, ~8 GB of free RAM, any OS; ~9 s per function on
  an Apple Silicon Mac, much slower without a GPU). Or Claude Haiku
  5.5 with an Anthropic API key: faster, roughly $1-2 per 4,000 functions
  (estimate). Never Claude Code's login token.

On a team, one machine can run all of this as a shared server; teammates
then need only Claude Code (§5c).

Languages: Python, Go, TypeScript/TSX, JavaScript, C++. With a local
summarizer nothing leaves your machine. Tested on macOS (Apple Silicon);
Linux notes are in `SETUP.md`; Windows is untested.

## Try it on sumgrep's own code

Index sumgrep itself (~110 functions) with the default local model and ask
it questions: about 17 minutes of indexing plus the downloads. You need
Docker Desktop (running), Ollama and uv. No API key, and nothing leaves
your machine.

```bash
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant
ollama pull bge-m3 && ollama pull qwen3.5:9b           # 1.2 GB + 6.6 GB
git clone https://github.com/wlee374/sumgrep.git ~/sumgrep && cd ~/sumgrep
mkdir -p ~/.sumgrep && cp sumgrep.toml.example ~/.sumgrep/sumgrep.toml
uv run sumgrep index --repo .
```

The index step took 17 minutes on an Apple Silicon Mac (~9 s per
function); without a GPU expect much longer. It prints
`repo config: NONE`: that's fine, sumgrep needs no excludes. Then search:

```bash
uv run sumgrep search --repo . "merge the ranked results of several searches into one list"
uv run sumgrep search --repo . "cut off a model answer that starts repeating itself"
```

The first should point at `rrf` in `src/sumgrep/search.py`, the second at
`trim_repetition` in `src/sumgrep/summarizer.py`. To see it from Claude
Code, connect it (step 2 below), open a new session in `~/sumgrep` and ask
"where do we merge results from the two searches?"

When you later index your own repo and this trial is the only thing in the
index, add `--recreate` to that first `index` to start clean. Otherwise
sumgrep's functions stay in the index; results name their repo, and Claude
is told to ignore repos it isn't working in.

## Get started

1. Install the pieces above and build an index: **`SETUP.md`** takes a
   bare machine to working search, with a check at every stage.
2. Connect Claude Code, **one** of:
   - the plugin (this repo is its own plugin marketplace):
     ```bash
     claude plugin marketplace add wlee374/sumgrep
     claude plugin install sumgrep@sumgrep
     ```
   - or register the server from your clone (§5b).

   Not both, or Claude sees every tool twice.
3. Got a `.sumgrep` file or a server address from a teammate?
   **`QUICKSTART.md`**: a server address is one command; a file is three
   installs and one command, macOS or Windows.

This file is the command reference. Design and phase plan:
`docs/sumgrep-build-guide.md`.

## How well it works

Measured on one Go codebase of ~5,100 functions with 55 labeled
questions, comparing each question with the summaries (the default mode),
with and without the optional LLM reranker (`[rerank]`, §6: one
`claude-haiku-5-5` call per search that reads the top 20 summaries and
puts the most likely first; about 2 s and $0.002 per query). The
summaries came from `qwen3.8:27b`; the smaller default `qwen3.5:9b`
hasn't been measured this way yet.

| Questions | found 1st | in top 5 | MRR@10 |
|---|---|---|---|
| 25 hand-written | 0.76 | 0.92 | 0.81 |
| 25 hand-written, reranked | 0.78 | 0.96 | 0.87 |
| 30 questions from real use | 0.30 | 0.60 | 0.43 |
| 30 questions from real use, reranked | **0.63** | 0.73 | **0.69** |

Reranked rows are the mean of two runs (the reranker's order varies a
little run to run: ±1 question at "found 1st"). Real questions are much
harder than invented ones; the reranker's gain is in ordering, not
recall — the right function was usually in the top 20 already, and a
model that reads the summaries can put it first where the embedding
could not. Comparing questions with the raw code instead of the
summaries did worse on every run (top 5: 0.64 vs 0.96 on the first
25-question eval). Similarity scores are not confidence; see §5.

---

## 1. Before anything: is the machine ready?

Three services must be up. Check all three when something suddenly fails.

```bash
curl -s localhost:6333/readyz          # Qdrant (vector DB, Docker)  -> "all shards are ready"
curl -s localhost:11434                # Ollama (models)              -> "Ollama is running"
ollama list                            # must show bge-m3 and your summarizer model
```

If Qdrant is down: start Docker Desktop, then `docker start qdrant`.
If Ollama is down: open the Ollama app, or `brew services start ollama`.

Dashboard for eyeballing the stored data: http://localhost:6333/dashboard

Every command below is run from your sumgrep clone (`~/sumgrep`) as
`uv run sumgrep <command> --repo PATH`. Below, `$REPO` stands for the
repo you index:

```bash
export REPO=/path/to/your/repo
```

The examples use names from sumgrep's own code; put in your own.

---

## 2. Everyday commands

### Search

```bash
uv run sumgrep search --repo $REPO "where do we retry with backoff"
```

Options:

| Flag | Meaning | Default |
|---|---|---|
| `--mode summary\|code\|hybrid` | what the question is compared against (see §5) | `[search].mode` in sumgrep.toml |
| `--k N` | how many pointers to return | `[search].k` (10) |
| `--full` | print the whole summary, not just the first sentence | off |
| `--floor 0.4` | drop hits whose best score is below this | `[search].floor` (0.35) |
| `--code-weight 0.2` | hybrid only: scale the code arm's vote | `[search].code_weight` (1.0) |
| `--rerank` / `--no-rerank` | reorder the top hits with the `[rerank]` LLM, which reads their summaries (§6); ~2 s and ~$0.002 per query with Haiku | `[rerank].enabled` (off) |
| `--hyde` | rewrite the question with the summarizer LLM first (slow, ~35 s) | off |
| `--rerank` | also score every arm after the `[rerank]` LLM reorders its top 20 (one LLM call per arm per query) | off |

Examples:

```bash
uv run sumgrep search --repo $REPO "in-memory store with expiring entries" --k 10
uv run sumgrep search --repo $REPO "merge two ranked lists into one" --mode hybrid
uv run sumgrep search --repo $REPO "trim_repetition" --full --k 1     # look one function up by name
```

### Re-index after the repo changed

```bash
uv run sumgrep index --repo $REPO
```

Incremental. Only functions whose body changed are re-summarized
(~9 s each with the default local model). Unchanged functions, comment
edits, and moved code cost nothing. A no-op run takes ~3 s.

The last line tells you what happened:

```
done in 41s · chunks 4215 · unchanged 4214 · lines refreshed 0 · embedded 1 · deleted 0 · LLM calls 1 · failed 0
```

| Field | Meaning |
|---|---|
| `unchanged` | fingerprint identical, skipped |
| `lines refreshed` | same code, moved to different lines; line numbers updated |
| `embedded` | new or changed functions written to the DB |
| `deleted` | functions that no longer exist (renames count as delete + new) |
| `LLM calls` | summaries actually generated (cache misses) |

Re-indexing is manual by design. There is no git hook; a local
summarizer is too heavy to run on every commit.

### Is the index stale?

```bash
uv run sumgrep meta --repo $REPO
```

Shows the indexed commit vs. your current HEAD and how many files are
dirty. A stale index is safe: pointers may name wrong lines, but the
reader always opens the real file.

### Read the summaries

```bash
uv run sumgrep summaries --repo $REPO --symbol trim_repetition   # one function
uv run sumgrep summaries --repo $REPO --symbol SummaryCache.     # all methods of a class
uv run sumgrep summaries --repo $REPO --symbol search.py         # everything in a file
uv run sumgrep summaries --repo $REPO --limit 20                 # first 20, unfiltered
```

Reads the function as it is on disk now. If you edited it since the last
index there is no summary yet ("0 of 1 chunks have a cached summary");
run `index` first.

---

## 3. Evaluating search quality

Write labeled questions in a YAML file. Each has a question, a `kind`,
and the function(s) that count as a correct answer:

```yaml
repo: sumgrep                      # folder name of the repo (a mismatch only warns)
queries:
  - q: merge the ranked results of several searches into one list
    kind: conceptual               # says what the code does, never its name
    answers:
      - src/sumgrep/search.py:rrf  # "path:symbol", or just "symbol" for any file
  - q: trim_repetition
    kind: literal                  # the identifier itself: grep's lane, a control group
    answers:
      - trim_repetition
```

A question is a hit if any of its answers is in the top k. Label the
answers from the code, not from search results, or the eval only
measures itself. Questions your agents really typed are worth more than
invented ones (see "How well it works").

```bash
uv run sumgrep eval --repo $REPO --queries my-queries.yaml                    # all arms, no LLM calls, ~10 s
uv run sumgrep eval --repo $REPO --queries my-queries.yaml --kind conceptual  # subset
uv run sumgrep eval --repo $REPO --queries my-queries.yaml --hyde             # adds the HyDE arms, ~35 s per query
uv run sumgrep eval --repo $REPO --queries my-queries.yaml --code-weight 0.2  # try a different hybrid weight
```

Without `--queries` it reads `eval/queries.yaml`. It prints, per
question, the rank at which the right answer appeared in each arm
(`-` = not in the top 10), then a table:

| Metric | Meaning |
|---|---|
| hit@1 | fraction of questions where the right answer was first |
| hit@5 | ...in the top 5 |
| MRR | mean of 1/rank; 1.0 is perfect |

---

## 4. Build-time commands (rarely needed)

```bash
uv run sumgrep chunk --repo $REPO --stats               # how the repo splits into functions
uv run sumgrep chunk --repo $REPO --sample 5            # print 5 random function chunks
uv run sumgrep summarize --repo $REPO --dry-run         # how many summaries are missing
uv run sumgrep summarize --repo $REPO --sample 10       # summarize 10 random functions, print them, mush gate
uv run sumgrep summarize --repo $REPO --symbol NAME --force   # regenerate one summary
uv run sumgrep summarize --repo $REPO                   # summarize everything missing (no DB write)
uv run sumgrep index --repo $REPO --recreate            # drop the DB collection and rebuild (after changing the embedding model)
uv run sumgrep --help                                   # all commands
uv run sumgrep search --help                            # flags for one command
uv run sumgrep export [FILE]                            # index + summary cache -> one .sumgrep file (default: Desktop)
uv run sumgrep import FILE.sumgrep                      # set up another machine from it (see §5c, QUICKSTART.md)
```

`summarize` only fills the summary cache; `index` does summarize + embed
+ write to the DB. You normally just run `index`.

---

## 5. Reading search output

```
rrf  src/sumgrep/search.py:L83-114  [function]
   The function `rrf` implements Reciprocal Rank Fusion to merge multiple ranked lists ...
   rrf 0.0164 · summary#1 0.52
```

Line 1: the pointer. Function, file, lines, kind.
Line 2: first sentence of its summary (`--full` for all of it).
Line 3: how it was found.

- `summary#1 0.52`: in the summary search it ranked 1st with similarity 0.52.
- `code#2 0.49`: in the raw-code search it ranked 2nd with similarity 0.49.
- `rrf`: the merged score that sets the final order in hybrid mode
  (each arm adds 1/(60+rank)). In summary or code mode it's just the rank.

**Modes.** Every function is stored as two vectors: its source and its
English summary. `summary` compares your question to the paragraphs only.
`code` compares it to the source only. `hybrid` does both and merges.
On the first 15-question eval, summary found the answer in the top 5 93%
of the time, hybrid 67%, code 53%, so the default is `summary`.

**Similarity scores are not confidence.** With bge-m3 everything lands
between 0.45 and 0.60. A question about something that does not exist
still gets a top hit around 0.50; a real answer can be 0.52. You cannot
tell "not found" from the number. Signs it's a miss: the top results
have nothing in common, and the scores are flat. The real check is
reading the file.

---

## 5b. Using it from Claude Code (MCP server)

The same search, exposed as two tools Claude Code can call:
`search_code(query, k?, mode?, repo?)` and `index_status(repo?)`. There is
no re-index tool on purpose.

Connect it **one** way, not both (two registrations give every tool twice).

**As a plugin** (updates with `claude plugin update sumgrep@sumgrep`):

```bash
claude plugin marketplace add wlee374/sumgrep
claude plugin install sumgrep@sumgrep
```

Or inside a session: `/plugin marketplace add wlee374/sumgrep`, then
`/plugin install sumgrep@sumgrep`. Notes:

- The first start installs the Python dependencies (~120 MB, a few
  seconds on a fast connection) into Claude Code's plugin data folder.
  If `/mcp` shows it failed in that very first session, reconnect it there.
- `uv` must be on the PATH Claude Code starts with.
- The plugin runs from Claude Code's plugin cache, so it reads its
  settings from `~/.sumgrep/sumgrep.toml` (or `$SUMGREP_CONFIG`), never
  from your clone. `import` and `SETUP.md` put the file there.
- Its tools are named `mcp__plugin_sumgrep_sumgrep__search_code` and
  `mcp__plugin_sumgrep_sumgrep__index_status`.

**Or register it from your clone** (user scope, so it's available in every
project). Run in a terminal where the `claude` command works:

```bash
claude mcp add --scope user sumgrep -- \
  uv run --project ~/sumgrep python -m sumgrep.mcp_server
```

If `claude` is not on your PATH, add this to `~/.claude.json` by hand while
Claude Code is **not** running (it rewrites that file on exit):

```json
"mcpServers": {
  "sumgrep": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", "/path/to/sumgrep", "python", "-m", "sumgrep.mcp_server"]
  }
}
```

Its tools are named `mcp__sumgrep__search_code` and
`mcp__sumgrep__index_status`. Check it's registered: `claude mcp list`,
or `/mcp` inside a session.

**Or connect to a shared server** your team runs (§5c), instead of
either of the above. Nothing else to install:

```bash
claude mcp add --scope user --transport http sumgrep http://SERVER:8000/mcp
```

If the server uses HTTPS, the address is `https://HOSTNAME:8000/mcp`.
Same tool names as the clone registration.

**What it returns.** Pointers, exactly as stored in the index: function
name, file path relative to the repo root, line range, kind, e.g.
`rrf  src/sumgrep/search.py:L83-114  [function]`. The server never
touches the filesystem and never prints absolute paths, so the same
index works on any machine, from any checkout, wherever it lives. The
agent opens the file in its own checkout, or greps the function name.

**Which repo.** One per search. The agent passes `repo=` with its
checkout's git remote (`git remote get-url origin`; https or ssh, any user
name) or the repo's name. `index` saves each repo's remote, so it matches
on every machine whatever the folder is called, and a different repo that
only shares the name doesn't match. With more than one repo indexed, a
search without `repo=` is refused with the list of repos; with one, it may
be left out. `index_status` lists the repos, their remotes and the commit
each was indexed at; code added after that commit is not in the index.

**Which settings.** `$SUMGREP_CONFIG` if set, else `sumgrep.toml` in the
sumgrep folder, else `~/.sumgrep/sumgrep.toml`. `[search]` sets the
defaults (mode, k, floor).

**Steering.** The server's `instructions` (in `mcp_server.py`, sent to
Claude Code every session, updated with the code) say how to use the
results. A four-line generic paragraph in `~/.claude/CLAUDE.md` (SETUP
§4.2) says when to reach for it; it never needs updating. Behavior details
go in the server, never in that paragraph.

**Checkpoint.** Open a fresh Claude Code session in your indexed repo
and ask, in your own words, "where do we handle <something>". In the
transcript you should see `search_code` called, then a Read or Grep on
the returned file. That two-step is the design working.

**Testing the server without Claude Code:**

```bash
uv run --project ~/sumgrep python -m sumgrep.mcp_server
# waits on stdin (that's the MCP transport); Ctrl-C to quit. Startup errors print here.
```

Logs from a running server: `~/.claude/` debug logs, or start the server
from a terminal (above) to see its stderr.

The same tools over HTTP or HTTPS, as a shared server runs them (§5c):

```bash
cd ~/sumgrep && uv run sumgrep serve
# [sumgrep] serving http://0.0.0.0:8000/mcp · config ... ·   Ctrl-C to quit
# [sumgrep] search log /Users/you/.sumgrep/searches.jsonl
```

Address, HTTPS and the search log come from `[server]` in `sumgrep.toml` (§6).

---

## 5c. Using it for real: more repos, other machines, agents

### Add another repo

1. Add a `[[repos]]` entry to your `sumgrep.toml` (name, path, excludes).
2. Sample first, then index:
   ```bash
   uv run sumgrep chunk --repo /path/to/repo --stats          # sane function count?
   uv run sumgrep summarize --repo /path/to/repo --sample 10  # summaries look right?
   uv run sumgrep index --repo /path/to/repo                  # full run: ~9 s per function with the default model
   ```
3. Nothing else to configure. Agents name the repo they work in (its git
   remote, saved by `index`), and sumgrep searches only that one.
   `uv run sumgrep meta --repo /path/to/repo` shows the saved `remote`;
   `-` means a repo indexed before sumgrep saved remotes: run `index` on
   it once more (seconds, no LLM calls).

All repos share one Qdrant collection, filtered by repo name. Two repos
with the same folder name would collide; rename one.

### Move the index to another machine

One file carries everything: a `.sumgrep` file is a zip of the Qdrant
snapshot (vectors + pointers, all repos) and the summary cache (so future
re-indexes skip the LLM). Its extension isn't `.zip` so browsers don't
unzip it on download.

On the source machine:

```bash
uv run sumgrep export                      # ~/Desktop/sumgrep-index-DATE.sumgrep, ~90 MB for 4k functions
```

On the target machine, once Docker Desktop, Ollama and uv are installed
and the sumgrep code is there (step by step for macOS and Windows:
`QUICKSTART.md`):

```bash
cd ~/sumgrep
uv run sumgrep import /path/to/sumgrep-index-DATE.sumgrep
```

`import` works in four steps and asks before running anything:

1. **Checks** Qdrant (offers to start or create the Docker container),
   Ollama and `bge-m3` (offers to download it), and creates
   `~/.sumgrep/sumgrep.toml` from the example if there is no settings
   file. Anything it can't fix stops with one sentence saying what to do.
2. **Loads** the index, replacing the existing one if you say yes, and
   installs the summary cache only if this machine has none.
3. **Test-searches** to prove Qdrant + Ollama + the index work together.
4. **Connects** Claude Code (`claude mcp add`, if not already done). Say
   no if you use the plugin. Then offers to append the steering paragraph
   (`SETUP.md` §4.2) to `~/.claude/CLAUDE.md`; skipped if that file
   already has a `## sumgrep` section.

An older `.snapshot` from before 2026-10-12 still imports (no cache in
it). For teammates without git, send the code as a zip:
`git -C ~/sumgrep archive --format=zip --prefix=sumgrep/ -o ~/Desktop/sumgrep-code.zip HEAD`.

The snapshot holds every indexed repo. For the MCP server the target's
checkout can be at any path (pointers are repo-relative). The CLI
(`meta`, `search`, `index --repo`) keys a repo by its folder name, so keep
the same folder name if you will run it on the target. Only the
embedding model must match (bge-m3); the summarizer
model can differ, but a different one will re-summarize on the next
`index`, since the cache is keyed per model.

Without the snapshot, the summary cache alone is enough: the first
`index` on the new machine re-embeds everything (~13 min for 4k
functions) but makes no LLM calls.

### One shared server for the team

Instead of every teammate running Docker, Ollama and an imported index,
one machine runs them plus the sumgrep server over HTTP or HTTPS, and
everyone's Claude Code connects to it:

```bash
# on the server machine (Qdrant, Ollama + bge-m3, index loaded)
cd ~/sumgrep && uv run sumgrep serve
```

HTTP on port 8000 by default. For HTTPS, set `https = true`, `tls_cert`
and `tls_key` in `[server]` of `~/.sumgrep/sumgrep.toml` (§6).

It writes every search to `~/.sumgrep/searches.jsonl` on the server: the
question, the results, and which machine asked (50 MB at most; it
rotates itself). Teammates' own session histories never reach you, so
this is where you see what they search for and where it misses. Tell
them it's logged. Format and use: `SETUP.md` §7b, Logs.

Then commit two files to the indexed repo, and teammates run nothing:

- `.mcp.json` with `{"mcpServers": {"sumgrep": {"type": "http", "url":
  "http://SERVER:8000/mcp"}}}` (or the `https://HOSTNAME:8000/mcp`
  address). Claude Code asks each teammate once whether to use it.
- The steering paragraph (`SETUP.md` §4.2) in the repo's `CLAUDE.md`.
  Without it, agents mostly keep using Grep.

Without the `.mcp.json`, each teammate runs once:
`claude mcp add --scope user --transport http sumgrep http://SERVER:8000/mcp`.

There is **no login**: anyone who can reach the port can search, and the
index holds every function name, path and summary. Run it only on the
office network or a VPN. Keep Qdrant and Ollama unreachable from the
network. You re-index from your own machine straight into the server's
Qdrant through an SSH tunnel. Full runbook (keeping it running on
Linux/macOS, the tunnel, checks): `SETUP.md` §7b.

### Make Claude agents use it

**Every interactive session** already has it once it's connected (§5b):
the server's built-in instructions tell Claude when to call it.

**Custom subagents** (`.claude/agents/*.md`) only get the tools you list.
Add the MCP tool names (the plugin's names are in §5b):

```yaml
---
name: code-locator
description: Finds where a concept is implemented in this repo
tools: mcp__sumgrep__search_code, mcp__sumgrep__index_status, Read, Grep, Glob
---
For "where is X" questions call search_code first, then Read the pointed
file to confirm. Use Grep only for exact identifiers.
```

**Claude Agent SDK** (your own programs): pass the server in `mcpServers`
and allow the tools:

```python
from claude_agent_sdk import query, ClaudeAgentOptions

opts = ClaudeAgentOptions(
    cwd="/path/to/indexed/repo",
    mcp_servers={"sumgrep": {
        "type": "stdio", "command": "uv",
        "args": ["run", "--project", "/path/to/sumgrep",
                 "python", "-m", "sumgrep.mcp_server"],
    }},
    allowed_tools=["mcp__sumgrep__search_code", "mcp__sumgrep__index_status",
                   "Read", "Grep"],
)
```

**Project scope for a team**: commit a `.mcp.json` in the target repo
with the same stdio block, or have everyone install the plugin. Each
teammate then still needs Qdrant, Ollama, and an index on their own
machine. With a shared server, the `.mcp.json` holds the `http` entry
above and teammates need nothing else.

---

## 6. Configuration: `sumgrep.toml`

Put it at `~/.sumgrep/sumgrep.toml`, where every route finds it:

```bash
mkdir -p ~/.sumgrep && cp ~/sumgrep/sumgrep.toml.example ~/.sumgrep/sumgrep.toml
```

A `sumgrep.toml` in the sumgrep folder (or `--config FILE`) wins for the
CLI and for a `claude mcp add` server, but the plugin doesn't see it.

```toml
[qdrant]
url = "http://localhost:6333"
collection = "sumgrep"

[embed]
model = "bge-m3"          # married to the collection; changing it = index --recreate

[search]
mode = "summary"          # summary | code | hybrid
k = 10
floor = 0.35
code_weight = 1.0

[rerank]                  # an LLM reorders the top hits at search time (off by default)
enabled = false           # true: search = retrieval (0.2 s) + one LLM call (~2 s with Haiku)
provider = "anthropic"    # ollama | anthropic | bedrock — independent of [summarize]
model = "claude-haiku-5-5"
think = "low"             # reasoning effort
candidates = 20           # retrieval hits it reads (max 20)
text = "summary"          # "summary" = the stored summary; "first" = first sentence only (small local models)
timeout = 10              # seconds; past this, or on any error, the retrieval order is returned

[server]                  # only for the shared team server: uv run sumgrep serve (§5c)
address = "0.0.0.0:8000"  # HOST:PORT it listens on
https = false             # true: https with the two files below
tls_cert = ""             # PEM certificate (the full chain, if it came with one)
tls_key = ""              # PEM key, no password
search_log = "~/.sumgrep/searches.jsonl"   # every search, one JSON line; 50 MB at most; "" = off

[summarize]
provider = "ollama"       # ollama | anthropic | bedrock
model = "qwen3.5:9b"      # with 20+ GB free: qwen3.8:27b, the model behind the results above
max_words = 300
num_ctx = 12000           # ollama: context window; must fit an 8k-char function
think = false             # ollama: skip the reasoning pass; "low" | "medium" | "high" = reasoning effort (any provider)
temperature = 0.7
top_p = 0.8
presence_penalty = 1.5    # ollama: stops Qwen looping on tiny functions
keep_alive = "10m"        # ollama: keep the model loaded between calls
max_tokens = 1500

[[repos]]
name = "myrepo"
path = "/path/to/myrepo"             # matched against --repo
exclude = ["third_party/*"]          # glob patterns skipped when chunking
```

### Reranking with an LLM (`[rerank]`)

Retrieval alone puts the right function in the top 20 far more often
than it puts it first. With `[rerank] enabled = true`, every search ends
with one LLM call that reads the top 20 candidates (name, path, stored
summary — never code) and returns them best-first; the rest keep their
retrieval order. Measured on 30 real agent queries (claude-haiku-5-5,
effort low): the right answer is first 63% of the time instead of 30%,
MRR 0.44 → 0.69, hand-written queries unchanged or better. Cost: about
2 s (p90 3.8 s) and $0.002 per query.

`[rerank]` is independent of `[summarize]`: summarize locally and rerank
with a cloud model, or the reverse. Credentials work the same way
(`ANTHROPIC_API_KEY` in the environment of the process that searches,
the AWS chain for Bedrock, nothing for Ollama). If the call fails, times
out, the key is missing, or the reply is not a usable ranking, the
retrieval order is returned; the CLI says so under the hits, and the
shared server records it in the search log (`"rerank": {...}` with `ok`
or `error`). A local model works through `provider = "ollama"` (set
`num_ctx` and consider `text = "first"` for small models). Measured
2026-10-29 on the same queries: a 27B model from first sentences gets
close to Haiku (real hit@1 0.53, MRR 0.61) at 26 s per query on a
32 GB Mac; 4B and smaller models do not improve the order or fail to
answer with a ranking. Use `eval --rerank` before trusting one.

### Changing the summarizer model

1. Edit `[summarize]`. Local: `ollama pull <model>` then set `model`.
   Anthropic API: `provider = "anthropic"`, `model = "claude-haiku-5-5"`,
   `max_tokens = 4000` (it thinks before writing, and thinking counts toward
   the cap), `think = "low"` (summaries need no deep reasoning; thinking is
   billed as output), and `export ANTHROPIC_API_KEY=sk-ant-api...` in your shell.
   Never the Claude Code login token.
2. Test on a sample before committing:
   `uv run sumgrep summarize --repo $REPO --sample 10`
   Read the pairs. The mush score at the end should be well under 0.75.
3. `uv run sumgrep summarize --repo $REPO`. Summaries are cached per
   model, so a new model re-summarizes everything. For 4k functions:
   hours on a local model (~9 s each with the default, ~40 s with
   `qwen3.8:27b`), or roughly 20 min and $1-2 on Haiku (estimate). Resumable;
   search keeps using the old summaries meanwhile.
4. `uv run sumgrep index --repo $REPO --recreate` (~13 min, no LLM calls:
   every summary is now a cache hit). A plain `index` is not enough: it
   skips every function whose code hasn't changed, so the old summaries
   would stay in the index.

Changing the prompt (`SYSTEM_PROMPT` in `src/sumgrep/summarizer.py`)
works the same way, steps 2–4: the cache key includes a hash of the prompt.

### Changing the embedding model

Different story: the vector size is baked into the DB collection.
Set `[embed].model`, then `uv run sumgrep index --repo $REPO --recreate`.
Summaries are reused from cache; only the embedding step reruns (~13 min).

---

## 7. Where things live

| Path | What |
|---|---|
| `~/.sumgrep/sumgrep.toml` | settings (§6) |
| `~/.sumgrep/summaries.sqlite` | summary cache, keyed by (code fingerprint, model, prompt hash). Survives everything; safe to back up |
| Qdrant volume `qdrant_storage` | the vector DB. Survives container restarts |
| `src/sumgrep/summarizer.py` | the summary prompt |
| `src/sumgrep/search.py` | retrieval and fusion |
| `src/sumgrep/mcp_server.py` | the MCP server Claude Code talks to |
| `.claude-plugin/` | the Claude Code plugin and its marketplace entry |
| `~/.claude/CLAUDE.md` | global steering paragraph for Claude Code |

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Connection refused` on :6333 | Qdrant not running | Docker Desktop, then `docker start qdrant` |
| `Connection refused` on :11434 | Ollama not running | open Ollama app / `brew services start ollama` |
| `sumgrep is not configured: no sumgrep.toml found` | no settings file where the server looks | create `~/.sumgrep/sumgrep.toml` (§6) |
| plugin shows failed in `/mcp` | first start was still installing, or `uv` not on PATH | reconnect in `/mcp`; check `uv --version` in a new terminal |
| search returns junk for a question you know has an answer | try `--mode summary`; check the target's summary with `summaries --symbol` | fix the summary prompt if the paragraph is bad |
| `0 of 1 chunks have a cached summary` | function edited since last index | `uv run sumgrep index --repo $REPO` |
| index: `collection has 1024-dim vectors but ... produces N` | embedding model changed | `index --recreate` |
| `repo config: NONE` in index output | `--repo` path doesn't match a `[[repos]]` entry | fix the path in sumgrep.toml; excludes won't apply otherwise |
| summaries loop / repeat themselves | Qwen without penalty | keep `presence_penalty = 1.5`, `think = false` |
| shared server: `claude mcp list` shows `✘ Failed to connect` | server down, wrong address, or not on its network | office network / VPN on? server running? (`SETUP.md` §7b) |
| shared server over HTTPS: `✘ Failed to connect` with a certificate error | address isn't the hostname on the certificate, or the machine doesn't trust its issuer | `SETUP.md` §7b step 3 (`NODE_EXTRA_CA_CERTS`) |

---

## License

MIT, see `LICENSE`.
