# Thread Dump Analyzer

A local, offline desktop tool for analyzing Java thread dumps (`.txt` files from `jstack`, `jcmd Thread.print`, `kill -3`, etc.) — similar in spirit to [fastthread.io](https://fastthread.io), but runs entirely on your machine, no upload required.

## Features

- **🩺 Diagnosis tab (the headline feature)** — automatically synthesizes the parsed dump into a plain-English verdict: *what's actually wrong* and *which thread is the likely root cause / stopper*. Rather than just listing raw data, it cross-references which thread holds a lock that many others are blocked on, detects request-pool saturation, flags stalls on external dependencies (database, cache, HTTP, message queues), and ranks every finding by severity (CRITICAL → HIGH → MEDIUM → LOW). A color-coded banner with the top verdict stays visible at the top of the window no matter which tab you're on. If nothing is wrong, it says so explicitly: "No stopper issue found — thread states look healthy."
- **🤖 AI Deep-Dive Report (Google Gemini)** — Integrates with the latest official `google-genai` SDK using the **`gemini-2.5-flash`** model to generate an expert-level performance engineering report. It automatically converts technical diagnostics into actionable insights (Root Cause, Business Impact, and Next Steps) in either English or Indonesian.
- **🌐 Enterprise Network Ready** — Features a built-in global SSL context and `httpx` monkeypatch to automatically bypass strict corporate proxies and self-signed SSL certificate restrictions, ensuring reliable AI analysis within secure environments.
- **Summary dashboard** — total threads, blocked/waiting counts, daemon count, deadlock count, a pie chart of thread states, and a bar chart of the largest thread pools.
- **Threads tab** — full sortable table of every thread (name, state, daemon flag, priority, top stack frame), with live search and state filtering. Click any thread to see its complete stack trace and lock info in the detail pane.
- **Issues & Deadlocks tab** — automatically detects and displays Java-level deadlocks, ranks the stack frames where the most threads are stuck (contention hotspots), and groups threads sharing an identical stack signature (a strong signal of thread-pool exhaustion or a stuck resource).
- **Multi-snapshot support** — if your file contains several dumps captured over time (e.g. one every 10 seconds), they're split automatically and selectable from a dropdown, and the diagnosis re-runs for each one.

### What the Diagnosis engine checks for

1. **Deadlocks** — always the top-ranked, most severe finding when present.
2. **Lock-holder identification** — when N threads are BLOCKED waiting on the same lock address, it finds the thread that actually holds that lock (by cross-referencing `locked <addr>` against `waiting to lock <addr>` across all threads) and names it as the likely root cause.
3. **Thread-pool saturation** — if 60%+ of a request-handling pool (`http-*`, `*exec*`, `tomcat-*`, `jetty-*`, etc.) is BLOCKED/WAITING rather than RUNNABLE, the service has little or no capacity left to accept new traffic.
4. **External dependency stalls** — threads parked in JDBC/HikariCP/Redis/Kafka/RabbitMQ/HTTP-client/socket frames suggest a slow or hung downstream dependency rather than an application bug.
5. **Generic stuck-thread clusters** — large groups of threads sharing an identical stack that aren't already explained by a more specific rule above.

## Setup

Requires Python 3.8+.

```bash
pip install -r requirements.txt

```

### Core Dependencies

Ensure your environment has the required libraries for the graphical interface and AI connectivity:

```bash
pip install matplotlib google-genai httpx

```

`tkinter` is part of the Python standard library on the official Windows and macOS installers. On Linux, install it via your package manager if it's missing:

```bash
# Debian/Ubuntu
sudo apt-get install python3-tk

# Fedora
sudo dnf install python3-tkinter

# Arch
sudo pacman -S tk

```

## Configuration

To use the AI Deep-Dive features, you need a Gemini API Key. You can supply it in two ways:

1. **In-App Configuration**: Go to **Settings > Gemini API Key...** in the application menu and paste your key.
2. **Environment Variable**: Set it globally in your terminal environment:
```bash
# Windows (CMD)
set GEMINI_API_KEY=your_api_key_here

# Windows (PowerShell)
$env:GEMINI_API_KEY="your_api_key_here"

# Linux/macOS
export GEMINI_API_KEY="your_api_key_here"

```



## Run

```bash
python app.py

```

Then click **"Open Thread Dump (.txt)"** and select your file.

## How to capture a thread dump (if you don't have one yet)

```bash
# By PID, written to stdout
jstack <pid> > dump.txt

# Or via jcmd
jcmd <pid> Thread.print > dump.txt

# Or send SIGQUIT to a running JVM (dump goes to its stdout/log)
kill -3 <pid>

```

## Project layout

```
.
├── app.py                       # Tkinter UI — run this
├── parser.py                    # Thread dump parsing logic (no UI deps)
├── diagnosis.py                 # Rule-based engine: parsed data -> plain-English verdict
├── ai_analyzer.py               # AI-powered deep-dive analysis (Google GenAI SDK)
├── requirements.txt
├── sample_dump.txt              # Example dump with a deadlock
├── sample_lock_holder_dump.txt  # Example dump where one thread blocks 4 others (no deadlock)
└── sample_healthy_dump.txt      # Example dump with no issues

```

## Notes on the parser

`parser.py` has zero dependency on Tkinter, so it can be reused or unit tested independently.

It handles dumps from HotSpot-family JVMs (Oracle JDK, OpenJDK, Temurin, Corretto, Zulu) in the standard `jstack`-style format. If your dump comes from a different tool with a noticeably different layout, some fields (priority, tid, nid) may show as blank, but thread names, states and stack traces will still parse correctly since those are matched independently.
