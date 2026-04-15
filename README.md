# P2P Monitor

A desktop monitor for [DreamBot](https://dreambot.org) P2P Master AI — tracks multiple RuneScape accounts in real time, posts Discord notifications for in-game events, and keeps a searchable event history.

Runs on **Linux** (Debian/Ubuntu) and **Windows** (packaged `.exe` or source). Requires DreamBot with the P2P Master AI script running.

---

## Features

### Multi-account monitoring
- Watches all accounts simultaneously from a single window
- Detects account status: 🟢 Logged In, 🟡 Starting..., 🟡 On Break, 🔴 Offline
- Tracks current task, activity, uptime, and break time per account
- Auto-detects new accounts when DreamBot starts a new log file
- If no active sessions exist on startup, monitor waits up to 10 minutes before stopping

### Discord notifications
- Posts embeds for: tasks, Slayer tasks and completions, quest starts and completions, drops, deaths, level ups, errors, and script lifecycle events
- Supports per-event-type webhooks or a single default webhook
- Supports Discord bot mode with per-account monitor threads
- Mute individual accounts without stopping monitoring
- Screenshot on event: attach a game screenshot to any Discord post
- `/ss [account]` — on-demand screenshot
- `/s` — live status summary of all accounts
- `/force <account> <action> [amount]` — force a skill, action, or time adjustment from Discord

### Event history
- Persists every event to a local JSONL file per account
- History tab shows a 24-hour rolling view, filterable by date range (up to 7 days)
- Backfill: on startup, re-reads DreamBot log files to populate history without re-pinging Discord

### Error detection
- Detects and pings on: login failures, world hop failures, pathing lockouts, stuckness, script crashes, server force-stops, GE failures, quest state loops, task locks, farming patch skips, overcrowded locations, and more
- Errors enriched with last known task and activity context

### Uptime and break tracking
- Tracks total session uptime and cumulative break time per account
- Break time persists correctly across monitor restarts

---

## Requirements

### Linux
- Debian 12 or compatible Ubuntu/Debian-based distro
- Python 3.10+ with Tkinter (`python3-tk`)
- `xdotool`, `imagemagick` (for screenshots and paint detection)
- `psutil`, `pystray`, `pillow`, `tkcalendar` (installed by `install.sh`)

### Windows
- Windows 10/11 (64-bit)
- Run from source: Python 3.10+ with dependencies from `requirements-windows.txt`
- Packaged `.exe`: no Python required — see [WINDOWS_BUILD.md](WINDOWS_BUILD.md)

---

## Installation — Linux

```bash
git clone https://github.com/p2pmonitor/P2P-Monitor.git
cd P2P-Monitor
chmod +x install.sh
./install.sh
```

The installer installs system and Python dependencies, copies files to `~/.p2p_monitor/`, and creates a desktop shortcut.

To run manually:
```bash
python3 ~/.p2p_monitor/p2p_monitor.py
```

---

## Installation — Windows

See [WINDOWS_BUILD.md](WINDOWS_BUILD.md) for full build instructions.

Quick start from source:
```
pip install -r requirements-windows.txt
python p2p_monitor.py
```

---

## Setup

### DreamBot logs
Set your DreamBot log folder path in **Settings → Log Folder**. Each subfolder inside that path corresponds to one account.

Default DreamBot log location: `/home/debian/DreamBot/Logs` (Linux) or `C:\Users\<you>\DreamBot\Logs` (Windows)

### Discord — webhook mode
1. Create a Discord webhook in any channel
2. Paste the URL into **Settings → Webhooks → Default Webhook**
3. Optionally add per-event webhooks (drops, deaths, errors, etc.)
4. Hit **Save**

### Discord — bot mode
1. Go to [discord.com/developers](https://discord.com/developers) → New Application → Bot → Reset Token → copy token
2. Enable **Message Content Intent** under Privileged Gateway Intents
3. OAuth2 → URL Generator → Scope: `bot` → Permissions: Send Messages, Read Message History, Manage Channels, Manage Webhooks, View Channels, Embed Links, Attach Files, Create Public Threads, Send Messages in Threads, Manage Threads, Use Slash Commands
4. Open the generated URL → select your server → Authorize
5. Right-click your server icon → Copy Server ID → paste into **Settings → Server ID**
6. Paste your bot token into **Settings → Bot Token**
7. Hit **Save** then **🤖 Run Bot Setup**

### Slash commands
| Command | Description |
|---|---|
| `/ss [account]` | Screenshot → post to account thread |
| `/s` | Post live status of all accounts |
| `/force <account> <action> [amount]` | Force a skill, action, or time adjustment |

---

## Data and files

| Path | Contents |
|---|---|
| `~/.p2p_monitor/config.json` | All settings |
| `~/.p2p_monitor/history/<account>/history.jsonl` | Per-account event log |
| `~/.p2p_monitor/offsets.json` | Log file read positions |
| `~/.p2p_monitor/screenshots/` | Screenshot files (auto-deleted after 24h) |

---

## Updating

**In-app (Linux source install):** Settings → 🔄 Check for Update

The updater fetches all module files from GitHub and writes them to `~/.p2p_monitor/`. A restart prompt appears when complete.

**Windows packaged `.exe`:** The update prompt opens the GitHub releases page so you can download the new version manually.

**Via git (Linux):**
```bash
cd ~/P2P-Monitor
git pull
./install.sh
```

---

## License

GNU General Public License v3.0 — free to use, modify, and distribute under the same license. See [LICENSE](LICENSE) for full terms.
