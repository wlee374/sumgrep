# sumgrep quick start

This page sets up Claude Code so it can search your team's code **by
meaning** ("where do we handle login?").

- **You got a server address** (like `http://10.0.0.5:8000/mcp`, or one
  starting with `https://`): your
  team runs one shared sumgrep. You need only Claude Code. Do
  **Shared server** just below, then steps 4 and 5.
- **You got a `.sumgrep` file:** do steps 1–5. It takes about 15
  minutes, once. Tested on macOS; the Windows steps are included but not
  tested yet, so tell whoever sent you the file if one fails.

---

## Shared server

Be on the office network or VPN, where the server is.

**If your team set it up in the project** (the project has a
`.mcp.json` file): pull the project, open Claude Code in it, and say yes
when it asks whether to use `sumgrep`. Then go to step 4 (your team has
usually done it for you too).

**Otherwise**, in Terminal (macOS) or PowerShell (Windows), paste this
with your address at the end, exactly as you were given it (`http://` or
`https://`):

```bash
claude mcp add --scope user --transport http sumgrep http://10.0.0.5:8000/mcp
```

Then go to step 4. Nothing else to install.

The server keeps a log of what is searched: the question, the results,
and your computer's network address. Whoever runs it uses that to make
search better.

If it says `already exists`, sumgrep was set up on this computer before.
Run `claude mcp remove --scope user sumgrep`, then the command again.

---

## 1. Install three apps (once)

| App | Get it | After installing |
|---|---|---|
| **Docker Desktop** | https://www.docker.com/products/docker-desktop/ | Open it. On Windows it may ask to restart. |
| **Ollama** | https://ollama.com/download | Open it. |
| **uv** | see the command below | Close and reopen your terminal. |

You also need **Claude Code** (you probably have it already).

Install uv. On **macOS**, open the *Terminal* app and paste:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On **Windows**, open *PowerShell* and paste:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 2. Get the sumgrep folder (once)

**If you have git**, in Terminal / PowerShell:

```bash
git clone https://github.com/wlee374/sumgrep.git
```

**If you got `sumgrep-code.zip`:** unzip it and move the `sumgrep` folder
into your home folder (macOS: Finder → Go → Home; Windows: `C:\Users\<you>`).
On Windows, *Extract All* puts it inside a `sumgrep-code` folder; move the
inner `sumgrep` folder.

## 3. Load the index (one command)

In Terminal / PowerShell, go into the folder:

```bash
cd sumgrep
```

Then type `uv run sumgrep import ` (with a space at the end), **drag the
`.sumgrep` file onto the window**, and press Enter:

```bash
uv run sumgrep import /path/to/sumgrep-index-DATE.sumgrep
```

It checks your computer in four steps. When it asks a question, press
**Enter** to say yes. It may set up the database, download a 1.2 GB model,
connect itself to Claude Code, and add the note from step 4 for you. The
first run takes a few minutes. It ends with `Done.`

If it says `STOPPED:`, do the one thing it tells you, then run the same
command again.

## 4. Tell Claude when to use it (once)

Being connected isn't enough: Claude keeps using its usual text search
unless a note tells it to try sumgrep first. **Skip this step** if:

- you did step 3 and pressed Enter when it offered to add the note, or
- your project's `CLAUDE.md` file already has a `## sumgrep` section
  (your team may have added it there for everyone).

On **macOS**, in Terminal, paste all of it:

```bash
mkdir -p ~/.claude && cat >> ~/.claude/CLAUDE.md <<'EOF'

## sumgrep
Semantic code search by meaning. For "where is the code that does X" use
sumgrep search_code first, then Read/Grep the returned file+lines for ground
truth. For exact identifiers, strings, or error messages, use Grep directly.
Check index_status if results look off.
EOF
```

On **Windows**, in PowerShell, paste all of it:

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude" | Out-Null
Add-Content "$HOME\.claude\CLAUDE.md" @'

## sumgrep
Semantic code search by meaning. For "where is the code that does X" use
sumgrep search_code first, then Read/Grep the returned file+lines for ground
truth. For exact identifiers, strings, or error messages, use Grep directly.
Check index_status if results look off.
'@
```

This adds the note to your personal Claude Code instructions file. Do it
once; running it again adds a second copy.

## 5. Use it

Open a **new** Claude Code session in your project and ask a question in
your own words, for example *"where is the code that refreshes the token?"*
Claude looks it up with sumgrep, then opens the file.

---

## Later

- **Got a newer `.sumgrep` file?** Run step 3 again with the new file and
  press Enter to replace the old index.
- **sumgrep itself updated?** GitHub: `git pull` inside the `sumgrep`
  folder. Zip: replace the folder with the new one. Then start new
  Claude Code sessions.

## If something goes wrong

| You see | Do this |
|---|---|
| `command not found: uv` / `'uv' is not recognized` | Close and reopen the terminal (after installing uv). |
| `STOPPED: Docker Desktop is not running` | Open Docker Desktop, wait until it says *Engine running*. |
| `STOPPED: Ollama is not running` | Open the Ollama app. |
| Claude never uses sumgrep | Start a **new** session. Type `/mcp` in Claude Code: `sumgrep` should be listed. Then check step 4 is done. |
| Shared server: `/mcp` says sumgrep failed | Turn on the VPN (or join the office network), then start a new session. Still failing, or the message mentions a certificate: ask whoever runs the server. |

---

## For the person sharing the index

```bash
cd ~/sumgrep
uv run sumgrep export                 # writes Desktop/sumgrep-index-DATE.sumgrep
```

Send that one file (Slack, Drive, USB). For teammates without GitHub
access, also send the code as a zip:

```bash
git -C ~/sumgrep archive --format=zip --prefix=sumgrep/ -o ~/Desktop/sumgrep-code.zip HEAD
```

Or run one shared server for everyone (`SETUP.md` §7b) and send people
only its address. It logs every search; tell people.
