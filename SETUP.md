# sumgrep — end-to-end setup guide

From a bare machine to Claude Code answering "where is the code that does X"
with sumgrep. Follow it top to bottom. Every stage ends with a check; do
not move on until the check passes.

Command reference for day-to-day use: `README.md`.
Design and rationale: `docs/sumgrep-build-guide.md`.

---

## 0. What you are setting up

```
 your repo ──► sumgrep index ──► Qdrant (vector DB, Docker)
                   │                  ▲
                   │ summaries        │ search
                   ▼                  │
             Ollama (LLM + bge-m3) ◄──┤
                                      │
 Claude Code ──► sumgrep MCP server ──┘  ──► pointers (symbol, file, lines)
```

| Piece | What it is | Needed for |
|---|---|---|
| uv | Python project runner | everything |
| Docker + Qdrant | the vector database, port 6333 | everything |
| Ollama + bge-m3 | embedding model, port 11434 | search and index |
| Ollama + qwen3.5:9b (or Anthropic API) | writes the summaries | index only, when functions change |
| Claude Code CLI | registers the MCP server | Claude Code integration |

Tested on macOS Apple Silicon, 32 GB. Linux notes inline.

For a team, one machine can run all of this and serve everyone over the
network; teammates then need only Claude Code (§7b).

---

## 1. Prerequisites

### 1.1 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

### 1.2 Docker and Qdrant

Install Docker Desktop from https://docs.docker.com/get-docker/ and
**open it**. Then:

```bash
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v qdrant_storage:/qdrant/storage \
  qdrant/qdrant
docker update --restart unless-stopped qdrant
```

`--restart unless-stopped` brings Qdrant back whenever Docker starts.
Docker itself must start too: Docker Desktop → Settings → General →
**Start Docker Desktop when you sign in**. On Linux, `systemctl enable
docker` does the same.

The named volume `qdrant_storage` holds your index across restarts and
container upgrades.

**Check:**

```bash
curl -s localhost:6333/readyz     # → all shards are ready
```

### 1.3 Ollama and the embedding model

Native install, not Docker, so it can use the GPU.

```bash
# macOS
brew install ollama
brew services start ollama          # starts now and at every login
# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

If you use the Ollama desktop app instead of brew, set it to open at login.

```bash
ollama pull bge-m3                  # ~1.2 GB
```

**Check:**

```bash
curl -s localhost:11434             # → Ollama is running
curl -s localhost:11434/api/embed -d '{"model":"bge-m3","input":"hello"}' | head -c 80
# → {"model":"bge-m3","embeddings":[[...
```

### 1.4 The summarizer (only if you will index on this machine)

Skip this if you will only load an exported index and search. Searching
never needs the summarizer. Indexing needs it for every new or changed
function.

Option A, local and free, the default (~9 s per function on Apple
Silicon, much slower without a GPU; ~8 GB RAM free):

```bash
ollama pull qwen3.5:9b
```

With 20+ GB free you can use `qwen3.8:27b` (~40 s per function), the
model behind the README's results; set it as `[summarize].model` (§2).

Option B, Anthropic API (Claude Haiku 5.5), fast, roughly $1-2 per 4k
functions (estimate):

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-api03-..."' >> ~/.zshrc && source ~/.zshrc
```

Must be a real API key from console.anthropic.com. The Claude Code login
token is not usable here.

### 1.5 Claude Code CLI

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

Even if you use Claude Code inside VS Code, you need the CLI once to
register the MCP server. They share the same configuration.

---

## 2. Install sumgrep

```bash
git clone https://github.com/wlee374/sumgrep.git ~/sumgrep
cd ~/sumgrep
uv sync
mkdir -p ~/.sumgrep && cp sumgrep.toml.example ~/.sumgrep/sumgrep.toml
```

The settings file lives in `~/.sumgrep/`, where the CLI, a registered
server and the plugin all find it. Edit `~/.sumgrep/sumgrep.toml`:

```toml
[[repos]]
name = "myrepo"
path = "/path/to/myrepo"              # ← this machine's checkout
exclude = ["third_party/*"]           # vendored code you don't want indexed
```

If you chose the Anthropic summarizer, also change `[summarize]`:

```toml
provider = "anthropic"
model = "claude-haiku-5-5"
max_tokens = 4000          # it thinks before writing; thinking counts toward this
```

**Check:**

```bash
uv run sumgrep --help               # lists the commands
uv run python scripts/smoke.py      # → nearest: 'retries a request with exponential backoff'
```

The smoke test proves Ollama, bge-m3, Qdrant, and the Python clients all
work together. After this, any failure is configuration, not environment.

---

## 3. Get the index

Two paths. Take **A** if you have an export from another machine, **B**
to build from scratch.

### Path A: load an export

On the source machine, `uv run sumgrep export` writes one file,
`~/Desktop/sumgrep-index-DATE.sumgrep` (index + summary cache). On this
machine:

```bash
cd ~/sumgrep
uv run sumgrep import /path/to/sumgrep-index-DATE.sumgrep
```

`import` checks Qdrant and Ollama/bge-m3 (offers to start or download
what's missing), loads the index, installs the summary cache if this
machine has none, creates `~/.sumgrep/sumgrep.toml` if there is no
settings file, test-searches, and
offers to run `claude mcp add` and to append the §4.2 paragraph to
`~/.claude/CLAUDE.md` (so §4.1 and §4.2 are done if you accept). Teammates
who only search can follow `QUICKSTART.md` instead of this file.

The checkout can live at **any path** on this machine: search results are
repo-relative pointers, so the MCP server needs nothing machine-specific.
The CLI (`meta`, `search`, `index`) keys the repo by its folder name, so
keep the same folder name if you will run those here.

### Path B: index from scratch

Three steps, each a gate.

```bash
cd ~/sumgrep
export REPO=/path/to/your/repo

# 1. Does the chunker see the code correctly?
uv run sumgrep chunk --repo $REPO --stats
#    Expect a plausible function count (≈ number of `func` in Go), no
#    parse-error storm. Fix excludes in sumgrep.toml if a vendored SDK shows up.

# 2. Are the summaries good? (10 LLM calls, ~7 min local / seconds on API)
uv run sumgrep summarize --repo $REPO --sample 10
#    Read the pairs. Mush gate at the end must say "ok" (median cosine < 0.75).

# 3. Full run. Default local model: ~9 s × functions on Apple Silicon
#    (a 27B: ~40 s, 4k functions ≈ 2 days). Anthropic Haiku: ~20 min, ~$1-2
#    (estimate). Ctrl-C any time; rerun resumes.
uv run sumgrep index --repo $REPO
```

Tip for the local model: run step 3 overnight. Finished summaries are
cached as they complete, so an interrupted run loses nothing.

### Check (both paths)

```bash
uv run sumgrep meta --repo $REPO
# repo myrepo · chunks N · indexed commit XXXX (current)

uv run sumgrep search --repo $REPO "where do we <something your code does>"
# SomeFunction  path/to/file.go:L120-160  [function]   ← pointers, best first
```

If `meta` says "not indexed" after Path A, the folder name differs from
the source machine's. Rename it, or run Path B.

---

## 4. Connect Claude Code

### 4.1 Connect the MCP server

Pick **one** of the two; both at once gives Claude every tool twice.

Option A, the plugin (updates with `claude plugin update sumgrep@sumgrep`):

```bash
claude plugin marketplace add wlee374/sumgrep
claude plugin install sumgrep@sumgrep
```

Its first start installs the Python dependencies (~120 MB, a few seconds
on a fast connection). It reads settings only from
`~/.sumgrep/sumgrep.toml` (§2), never from the clone.

Option B, register the clone:

```bash
claude mcp add --scope user sumgrep -- \
  uv run --project ~/sumgrep python -m sumgrep.mcp_server
claude mcp list
# sumgrep: uv run --project ... - ✔ Connected
```

Either way, every project on this machine gets it. The server
searches every indexed repo (agents can pass `repo="name"` to narrow) and
returns paths relative to the repo root, so nothing is per-project. In a
project that isn't indexed, the results belong to other repos; the
server's instructions tell the agent to ignore them and use Grep.

### 4.2 Tell Claude when to use it

The server sends Claude Code its own instructions (how to read results,
what to do when the index is stale) at the start of every session. They
live in `mcp_server.py` and update with the code (`git pull`, or
`claude plugin update` for the plugin), so you never edit
them by hand.

Once per machine, add this to `~/.claude/CLAUDE.md` (`import` offers to
do it for you; keep the text in sync with `STEERING` in
`src/sumgrep/transfer.py`). It only says *when*
to reach for sumgrep, so it never needs updating:

```markdown
## sumgrep
Semantic code search by meaning. For "where is the code that does X" use
sumgrep search_code first, then Read/Grep the returned file+lines for ground
truth. For exact identifiers, strings, or error messages, use Grep directly.
Check index_status if results look off.
```

Keep details of how sumgrep works out of this paragraph; put them in the
server's `instructions` instead.

### 4.3 Check: the two-step

Open a **new** Claude Code session in the indexed repo (`cd $REPO &&
claude`, or open the folder in VS Code and start a fresh chat). Type
`/mcp`: `sumgrep` (Option B) or `plugin:sumgrep:sumgrep` (Option A)
should be listed with two tools.

Then ask a question in your own words, for example:

> where do we retry failed requests?

Watch the transcript. You should see `search_code` called, then a Read
or Grep on the file it returned. That is the design working: the index
proposes, the file decides.

If Claude greps instead and never calls `search_code`, check that `/mcp`
shows it connected and that the CLAUDE.md paragraph is in place.

---

## 5. Daily use

**After your code changes**, re-index when convenient. It only touches
functions whose body changed; comment edits and moves are free.

```bash
cd ~/sumgrep && uv run sumgrep index --repo $REPO
```

Nothing re-indexes automatically. Between runs the index is stale, which
is safe: results can point at drifted line numbers, but Claude reads the
real file, and `index_status` tells it how far behind the index is.

**Is it stale?** `uv run sumgrep meta --repo $REPO`

**Look at a summary:** `uv run sumgrep summaries --repo $REPO --symbol FunctionName`

**Search from the terminal:** `uv run sumgrep search --repo $REPO "question"`

---

## 6. Adding another repo

```bash
# 1. add a [[repos]] block to sumgrep.toml (name, path, excludes)
# 2. gate, sample, index — same as Path B:
uv run sumgrep chunk --repo /path/to/other --stats
uv run sumgrep summarize --repo /path/to/other --sample 10
uv run sumgrep index --repo /path/to/other
# 3. open Claude Code in that repo; nothing else to configure
```

All repos live in one Qdrant collection, separated by folder name. Two
repos with the same folder name would collide.

---

## 7. Moving to yet another machine

Source machine:

```bash
cd ~/sumgrep
uv run sumgrep export        # ~90 MB per 4k functions, all repos + summary cache
```

Copy the `.sumgrep` file over, then on the target follow §1, §2, §3 Path A
(`import` handles §4.1). Search-only teammates: `QUICKSTART.md`.

---

## 7b. One shared server for a team

One machine runs Qdrant, Ollama and the sumgrep server over HTTP or
HTTPS. Teammates connect Claude Code to it and install nothing else.
Everyone searches the same index.

There is **no login**: anyone who can reach the server's port can search.
Run it only where just your team can reach it (office network or VPN).
The index holds no source code, but it does hold every function name,
file path and summary.

### On the server machine (once)

1. Follow §1.1–1.3 and §2: uv, Docker + Qdrant, Ollama + bge-m3, the
   sumgrep folder. No summarizer, no `[[repos]]` entry and no GPU: the
   server never indexes, it only embeds the question. One change in §1.2:
   publish Qdrant on this machine only, so nobody on the network can
   reach it (it has no login either, and anyone reaching it could delete
   the index; on Linux, Docker's published ports bypass `ufw`):

   ```bash
   docker run -d --name qdrant \
     -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
     -v qdrant_storage:/qdrant/storage \
     qdrant/qdrant
   docker update --restart unless-stopped qdrant
   ```

   Ollama listens on this machine only by default; leave it that way.

2. Load the index: copy a `.sumgrep` export over and run §3 Path A. Say
   no when it offers `claude mcp add`.

3. Start the server:

   ```bash
   cd ~/sumgrep
   SUMGREP_HTTP=0.0.0.0:8000 uv run python -m sumgrep.mcp_server
   # [sumgrep] serving http://0.0.0.0:8000/mcp (no auth: private network only)
   ```

   `SUMGREP_HTTP` is `HOST:PORT`. `0.0.0.0` listens on every network the
   machine is on; put one of its addresses there to listen on that one
   only. Without `SUMGREP_HTTP` the server speaks stdio as in §4.1.

   **Or HTTPS**, if you have a certificate for the server's hostname: add
   its two PEM files (the key without a password):

   ```bash
   cd ~/sumgrep
   SUMGREP_HTTP=0.0.0.0:8000 SUMGREP_TLS_CERT=/path/to/cert.pem SUMGREP_TLS_KEY=/path/to/key.pem \
     uv run python -m sumgrep.mcp_server
   # [sumgrep] serving https://0.0.0.0:8000/mcp (no auth: private network only)
   ```

   Teammates then use `https://HOSTNAME:8000/mcp` everywhere this
   section says `http://SERVER:8000/mcp`, with the hostname written on the
   certificate. A certificate from a public authority (Let's Encrypt and
   the like) works on their machines as is. A self-signed one is refused
   until each machine trusts it (`NODE_EXTRA_CA_CERTS=/path/to/cert.pem`
   in the environment Claude Code starts from); one from a company
   authority may need the same. HTTPS encrypts the traffic; it adds no
   login.

4. **Check** from another machine on the same network (with HTTPS, the
   `https://` address):

   ```bash
   claude mcp add --transport http sumgrep-test http://SERVER:8000/mcp
   claude mcp list
   # sumgrep-test: http://SERVER:8000/mcp (HTTP) - ✔ Connected
   claude mcp remove sumgrep-test -s local
   ```

5. Keep it running (stop the one from step 3 first). The path to `uv` is
   what `which uv` prints.

   **Linux**, `/etc/systemd/system/sumgrep.service`:

   ```ini
   [Unit]
   Description=sumgrep shared search server
   After=network-online.target docker.service ollama.service
   Wants=network-online.target

   [Service]
   User=YOU
   Environment=SUMGREP_HTTP=0.0.0.0:8000
   # HTTPS: also these two (YOU must be able to read the key)
   # Environment=SUMGREP_TLS_CERT=/path/to/cert.pem
   # Environment=SUMGREP_TLS_KEY=/path/to/key.pem
   ExecStart=/home/YOU/.local/bin/uv run --project /home/YOU/sumgrep python -m sumgrep.mcp_server
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now sumgrep
   journalctl -u sumgrep -f            # its log
   ```

   **macOS**, `~/Library/LaunchAgents/com.sumgrep.server.plist` (runs
   while you are logged in):

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>
     <string>com.sumgrep.server</string>
     <key>ProgramArguments</key>
     <array>
       <string>/Users/YOU/.local/bin/uv</string>
       <string>run</string>
       <string>--project</string>
       <string>/Users/YOU/sumgrep</string>
       <string>python</string>
       <string>-m</string>
       <string>sumgrep.mcp_server</string>
     </array>
     <key>EnvironmentVariables</key>
     <dict>
       <key>SUMGREP_HTTP</key>
       <string>0.0.0.0:8000</string>
       <!-- HTTPS: also these two
       <key>SUMGREP_TLS_CERT</key>
       <string>/path/to/cert.pem</string>
       <key>SUMGREP_TLS_KEY</key>
       <string>/path/to/key.pem</string>
       -->
     </dict>
     <key>RunAtLoad</key>
     <true/>
     <key>KeepAlive</key>
     <true/>
     <key>StandardErrorPath</key>
     <string>/Users/YOU/.sumgrep/server.log</string>
   </dict>
   </plist>
   ```

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sumgrep.server.plist
   tail -f ~/.sumgrep/server.log       # its log
   ```

   If macOS asks whether Python may accept incoming connections, allow it.

### Teammates

Recommended: teammates run nothing. Commit two files to the root of the
indexed repo:

1. `.mcp.json`, so Claude Code connects. It asks each teammate once, in
   that repo, whether to use it (with HTTPS, the `https://` address):

   ```json
   {
     "mcpServers": {
       "sumgrep": { "type": "http", "url": "http://SERVER:8000/mcp" }
     }
   }
   ```

2. The §4.2 paragraph in the repo's `CLAUDE.md` (create it if there is
   none). A connected server alone rarely gets used: Claude Code follows
   `CLAUDE.md` as instructions and treats the server's own text as
   background. It only says *when* to use sumgrep, so it never needs
   updating.

Each teammate then: be on the office network or VPN, pull the repo, open
Claude Code in it, and say yes when it asks to use `sumgrep`.

Without the committed `.mcp.json`, each teammate runs this once instead
(and still gets the note from the repo's `CLAUDE.md`):

```bash
claude mcp add --scope user --transport http sumgrep http://SERVER:8000/mcp
```

Anyone who set sumgrep up locally before removes that first:
`claude mcp remove --scope user sumgrep` (after `import` or §4.1 Option
B; otherwise the add fails on the name) or `claude plugin uninstall
sumgrep@sumgrep` (otherwise every tool shows up twice). Teammates
need no Docker, Ollama, uv, sumgrep folder or `.sumgrep` file;
`QUICKSTART.md` has the plain-words version.

### Updating the shared index

Index where the summarizer and the summary cache are (your machine),
straight into the server's Qdrant through an SSH tunnel. Both machines
must use the same embedding model (bge-m3).

```bash
ssh -N -L 16333:localhost:6333 you@SERVER    # leave it running in its own terminal
```

In a second terminal, once: a copy of your settings that points at the
tunnel, so your own settings stay on your local Qdrant. Copy the file you
use (`~/.sumgrep/sumgrep.toml`, or `sumgrep.toml` in the sumgrep folder
if you keep one there):

```bash
cp ~/.sumgrep/sumgrep.toml ~/.sumgrep/shared.toml
# edit ~/.sumgrep/shared.toml:  [qdrant] url = "http://localhost:16333"
```

Then, after each round of changes:

```bash
cd ~/sumgrep
uv run sumgrep index --repo $REPO --config ~/.sumgrep/shared.toml
uv run sumgrep meta  --repo $REPO --config ~/.sumgrep/shared.toml   # the server's index commit
```

It is incremental, as in §5. Teammates get it on their next search; the
server needs no restart. Without SSH: `export` here, copy the file,
`import` on the server (replaces the whole index).

Everyone shares the index at one commit. That is the same staleness as
§5: the server's instructions tell Claude to trust the file, and
`index_status` shows the commit.

---

## 8. Changing models (read before you do it)

| Change | Effect | Command |
|---|---|---|
| summarizer model or the prompt | every function needs a new summary (cache is keyed per model + prompt); plain `index` skips unchanged code, so it would keep the old ones | edit `[summarize]` or `SYSTEM_PROMPT`, then `summarize` (long; search keeps working), then `index --recreate` |
| embedding model | vector size changes; the collection must be rebuilt; summaries are reused | edit `[embed].model`, then `index --recreate` |
| `[search].mode` | which vector questions are compared to; no re-index | edit and go |

Always run `summarize --sample 10` before a full run with a new
summarizer.

---

## 9. Troubleshooting

Run these three first whenever anything fails suddenly:

```bash
curl -s localhost:6333/readyz     # Qdrant
curl -s localhost:11434           # Ollama
ollama list                       # models present
```

| Symptom | Cause | Fix |
|---|---|---|
| `Connection refused` :6333, or `search failed: ConnectError` inside Claude | Qdrant/Docker not running | open Docker Desktop; `docker start qdrant` |
| `Connection refused` :11434 | Ollama not running | `brew services start ollama` or open the app |
| `model 'bge-m3' not found` | not pulled on this machine | `ollama pull bge-m3` |
| `no sumgrep.toml found` | config missing | in `~/sumgrep`: `mkdir -p ~/.sumgrep && cp sumgrep.toml.example ~/.sumgrep/sumgrep.toml` |
| `repo 'x' is not indexed` | `repo=` names a repo that was never indexed | the reply lists indexed names; or run `index` |
| `repo config: NONE` during index | `--repo` path ≠ `[[repos]].path` | fix the path; excludes are ignored otherwise |
| `collection has 1024-dim vectors but ... produces N` | embedding model changed | `index --recreate` |
| `0 of 1 chunks have a cached summary` | function edited since last index | run `index` |
| Claude never calls `search_code` | server not registered for this scope, or steering missing | `claude mcp list`; `/mcp` in session; §4.2 |
| search results all look unrelated to each other | the concept probably isn't in the repo | the score can't tell you; read the top file to confirm |
| summaries repeat themselves in loops | Qwen without penalties | keep `presence_penalty = 1.5`, `think = false` |
| `index` is slow | a local model: ~9 s per changed function with the default, ~40 s with a 27B, more without a GPU | let it run; or switch `[summarize]` to Anthropic |
| shared server: `claude mcp list` shows `✘ Failed to connect` | server not running, wrong address, or this machine isn't on the server's network | on the server: `systemctl status sumgrep` (Linux) or the log (§7b); here: office network or VPN on? |
| `SUMGREP_HTTP must be HOST:PORT` | malformed value | e.g. `0.0.0.0:8000` |
| `https needs both SUMGREP_TLS_CERT and SUMGREP_TLS_KEY` | only one is set | set both for HTTPS, neither for HTTP |
| `cannot use SUMGREP_TLS_CERT=… / SUMGREP_TLS_KEY=…` | file missing or unreadable, key doesn't match the certificate, or key has a password | fix the path or permissions; use the matching key without a password |
| HTTPS: `✘ Failed to connect` with a certificate error (e.g. `SELF_SIGNED_CERT`) | the address isn't the hostname on the certificate, or that machine doesn't trust the issuer | use the hostname on the certificate; for a self-signed or company certificate set `NODE_EXTRA_CA_CERTS` (§7b step 3) |
| `address already in use` when the shared server starts | another program has the port | pick another port in `SUMGREP_HTTP`, and in the teammates' URL |

---

## 10. Uninstall

```bash
claude mcp remove --scope user sumgrep        # or: claude plugin uninstall sumgrep@sumgrep
# shared server (§7b): sudo systemctl disable --now sumgrep
#   or on macOS: launchctl bootout gui/$(id -u)/com.sumgrep.server
docker rm -f qdrant && docker volume rm qdrant_storage      # deletes the index
rm -rf ~/sumgrep ~/.sumgrep                                 # code + summary cache
# remove the sumgrep section from ~/.claude/CLAUDE.md
```
