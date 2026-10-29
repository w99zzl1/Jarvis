# Jarvis

A desktop AI assistant for Windows. Jarvis is a small always-on-top orb that
opens into a full chat console: it talks to the model you pick, runs tools on
your machine (files, shell, search, disk scan), and keeps its own memory and
chat history.

**Status: v0.1.0-beta.1** — public beta. It works, but expect rough edges; bug
reports are welcome.

> Русская версия: [README.ru.md](README.ru.md)

---

## What it does

* **Chat with streaming** — multiple providers and models, switchable at any
  time: OpenAI, Google, Groq, OpenRouter, NVIDIA NIM and any OpenAI-compatible
  endpoint.
* **Two agents**
  * the *assistant* agent: general requests, web search, memory, clipboard,
    file reading/writing/editing, shell commands;
  * the *code* agent: works inside a workspace folder — reads and edits files,
    searches content, plans multi-step work, runs git and shell, browses the web.
* **Local tools, your machine** — a native disk scanner (Rust) for "where did
  my free space go", a whole-disk filename index, shell execution and file
  operations, all done locally by the Python backend.
* **Memory and history** — long-term notes, conversations, per-run traces and
  background task progress. Everything survives restarts.
* **Two windows** — a compact orb window that stays out of the way, and a
  console window with chat, telemetry and settings.

The UI and code comments are bilingual (Russian and English); the interface
language follows the strings shipped in the build.

---

## Requirements

* Windows 10 or 11, x64.
* An API key for at least one supported provider. Jarvis does not ship keys and
  does not proxy your traffic: requests go from your machine straight to the
  provider.
* No Python or Node.js needed — the installer bundles its own interpreter.

---

## Install

1. Open [Releases](../../releases) and download the latest `Jarvis_*_x64-setup.exe`.
2. Run it. The installer is per-user and does not need administrator rights.
3. Windows SmartScreen will warn about an unknown publisher — the beta build is
   not code-signed. Choose **More info → Run anyway**.

There is **no auto-update**. To update, install a newer release over the old
one; your data in `%APPDATA%` is kept. Uninstalling removes the program, not
your data.

### First run

1. Open the settings window and paste an API key for a provider.
2. Pick a primary model.
3. Choose a workspace folder — the folder the agent's file and shell tools are
   allowed to work in. Default: `Documents\Jarvis`.

---

## Where your data lives

Everything writable lives in one per-user folder:

```
%APPDATA%\com.jarvis.assistant\
  api_keys.json          settings and API keys
  conversations\         chat history
  traces\                agent run traces
  uploads\               chat attachments
  _file_index\           whole-disk filename index
  cache\                 disk-scan cache
  memory_store.json      long-term memory
  tasks_history.json     background task log
  location_cache.json    geo lookup cache
  backend.log            Python backend log (first place to look on errors)
```

The installation folder itself is treated as read-only. Your **workspace**
(the folder the agent may edit) is separate and defaults to `Documents\Jarvis`.
Both locations can be overridden with the `JARVIS_DATA_DIR` and
`JARVIS_WORKDIR` environment variables.

---

## Build from source

Prerequisites:

* Node.js 18+ and npm
* Rust (stable, MSVC toolchain) with the **Desktop development with C++**
  workload from Visual Studio Build Tools
* Python 3.10 x64 (only to run the tests and to build the bundled backend)
* Git

```powershell
git clone <this repo> Jarvis
cd Jarvis
npm install
npm run tauri:dev
```

`npm run tauri:dev` starts Vite, compiles the Rust shell and launches the app;
in development the shell uses the `python` on your `PATH` to run
`server/main.py`.

### Building the installer

```powershell
# 1. Assemble the Python backend: embeddable CPython 3.10 + dependencies
powershell -ExecutionPolicy Bypass -File scripts\build_backend.ps1

# 2. Build the native disk scanner (Rust/PyO3) into the staged backend
powershell -ExecutionPolicy Bypass -File scripts\build_disk_scan.ps1

# 3. Build the app and the NSIS installer
npm run tauri:build
```

The result lands in `src-tauri\target\release\bundle\nsis\`.

`scripts\build_backend.ps1` downloads the embeddable CPython distribution into
`.cache\`, installs the pinned dependencies from `requirements.txt` into it and
copies the backend sources to `backend-dist\backend\`. Tauri ships that folder
as a resource; the Rust shell starts it from there. Nothing is installed
system-wide.

---

## Project layout

```
src/                  React + Vite UI (orb window, console, settings)
src-tauri/            Rust shell: windows, config, backend lifecycle, health
server/               FastAPI backend: routes, streaming, conversations
agents/               assistant agent and code agent, plus their tool sets
shared/               paths, LLM client, memory, file index, tasks, disk scan
native/jarvis-disk/   Rust disk scanner exposed to Python via PyO3
config/               agent prompts and tool configuration
scripts/              backend and native-module build scripts
tests/                plain-Python checks (no pytest needed)
backend-dist/         generated: staged backend shipped in the installer
```

The backend listens on `http://127.0.0.1:8765` only. It is started and stopped
by the Rust shell, and its status is shown in the orb window
(`STARTING BACKEND` / `SYSTEM ONLINE` / `BACKEND OFFLINE`).

---

## Troubleshooting

**`BACKEND OFFLINE` in the orb window.** The message under the title says what
happened. Open `%APPDATA%\com.jarvis.assistant\backend.log` — Python tracebacks
go there, because a packaged app has no console.

**Antivirus quarantined something.** `backend\python\python.exe` is a stock
embeddable CPython. Some scanners flag unsigned executables in an installer;
restore the file or add an exclusion, then reopen Jarvis.

**"Port 8765 is already used by another program."** Another process (often a
leftover Jarvis backend) holds the port. Close it and reopen Jarvis.

**Models will not load.** `/models/live` is fetched from the provider when the
backend starts; a missing or invalid key leaves the list empty. Check the key in
settings and the log for the provider's error.

**Reset everything.** Close Jarvis and delete `%APPDATA%\com.jarvis.assistant`.
This removes conversations, memory and settings.

---

## Contributing

Issues and pull requests are welcome. This is a hobby project on a beta
schedule, so please keep changes focused and describe what you tested. Do not
commit `api_keys.json`, `backend-dist/`, `jarvis_disk.pyd` or anything under
`%APPDATA%` — all of it is git-ignored on purpose.

## License

No license is granted yet. The source is published for transparency; all rights
are reserved. See the repository owner before reusing the code.
