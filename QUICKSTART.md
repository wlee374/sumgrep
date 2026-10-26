# sumgrep quick start

You got a `.sumgrep` file. This page sets up your computer so Claude Code
can search that code **by meaning** ("where do we handle login?").
It takes about 15 minutes, once. Works on macOS and Windows.

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
