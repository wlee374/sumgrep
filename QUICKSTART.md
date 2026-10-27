# sumgrep quick start

This page sets up Claude Code so it can search your team's code **by
meaning** ("where do we handle login?").

- **You got a server address** (like `http://10.0.0.5:8000/mcp`): your
  team runs one shared sumgrep. You need only Claude Code. Do
  **Shared server** just below, then step 4.
- **You got a `.sumgrep` file:** do steps 1–4. It takes about 15
  minutes, once. Tested on macOS; the Windows steps are included but not
  tested yet, so tell whoever sent you the file if one fails.

---

## Shared server (one command)

Be on the office network or VPN, where the server is. In Terminal
(macOS) or PowerShell (Windows), paste this with your address at the end:

```bash
claude mcp add --scope user --transport http sumgrep http://10.0.0.5:8000/mcp
```

Then go to step 4. Nothing else to install.

If it says `already exists`, sumgrep was set up on this computer before.
Run `claude mcp remove --scope user sumgrep`, then the command again.

If Claude Code asks whether to use the `sumgrep` server from your
project's `.mcp.json`, say yes: your team set it up there for you, and
you don't need the command at all.

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
and connect itself to Claude Code. The first run takes a few minutes.
It ends with `Done.`

If it says `STOPPED:`, do the one thing it tells you, then run the same
command again.

## 4. Use it

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
| Claude never uses sumgrep | Start a **new** session. Type `/mcp` in Claude Code: `sumgrep` should be listed. |
| Shared server: `/mcp` says sumgrep failed | Turn on the VPN (or join the office network), then start a new session. Still failing: ask whoever runs the server. |

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
only its address.
