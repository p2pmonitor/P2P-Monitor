# P2P Monitor

A desktop monitor for [DreamBot](https://dreambot.org) P2P Master AI — tracks multiple RuneScape accounts in real time, posts Discord notifications for in-game events, and keeps a searchable event history.

Runs on **Linux** (Debian/Ubuntu) and **Windows 10/11**. Requires DreamBot with the P2P Master AI script running.

---

## Download

Go to the [**Releases**](https://github.com/p2pmonitor/P2P-Monitor/releases/latest) page and download the file for your platform:

| Platform | File |
|---|---|
| Linux (Debian/Ubuntu) | `P2P-Monitor-vX.X.X.zip` |
| Windows 10/11 | `P2P Monitor.exe` |

> **Windows note:** Windows may show a SmartScreen warning ("Windows protected your PC") when you first run the `.exe`. This is normal for unsigned applications — click **More info → Run anyway** to proceed. The app contains no malware. You can verify by uploading the `.exe` to [VirusTotal](https://virustotal.com) before running.
>
> If you prefer to build from source instead of using the pre-built `.exe`, see [WINDOWS_BUILD.md](WINDOWS_BUILD.md).

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
- Self-healing: automatically detects and recovers from deleted Discord threads, channels, and webhooks — no restart needed
- Level 99 detection: special "🎆 Level 99! 🎆" embed title, always notifies regardless of the level-up interval

### Event history
- Persists every event to a local JSONL file per account
- History tab shows a 24-hour rolling view, filterable by date range (up to 7 days)
- Backfill: on startup, re-reads DreamBot log files to populate history without re-pinging Discord

### Error detection
- Detects and pings on: login failures, world hop failures, pathing lockouts, stuckness, script crashes, server force-stops, GE failures, quest state loops, task locks, farming patch skips, overcrowded locations, and more
- Errors enriched with last known task and activity context
- Error detection patterns fetched from GitHub on startup — simple pattern fixes apply without a new build

### Inferno tracking
- Gear-check outcome detection: passed, requirements not met, missing gear/supplies, or unknown failure
- Resource check failures are buffered during gear prep — only the final outcome is sent, not every checked item
- Active attempt wave tracking: start, milestone updates (waves 7 15 24 31 41 48 56 63 67 68 69), death, and success
- Wave milestones deduplicated — replayed log lines never double-send
- High ping cached and merged into the attempt start message
- Success requires TzKal-Zuk kill count confirmation — Wave 69 alone is only a milestone
- Status tab shows current wave in real time; Discord receives milestone events only

### Uptime and break tracking
- Tracks total session uptime and cumulative break time per account
- Break time persists correctly across monitor restarts

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

### Option A — Pre-built executable (recommended)
1. Download `P2P Monitor.exe` from the [Releases](https://github.com/p2pmonitor/P2P-Monitor/releases/latest) page
2. Place it anywhere — your Desktop, a `P2P Monitor` folder, wherever you like
3. Double-click to run
4. If Windows SmartScreen appears, click **More info → Run anyway**

No Python, no dependencies, no installer needed.

### Option B — Run from source
If you prefer to run from source or build your own `.exe`:

```
pip install -r requirements-windows.txt
python p2p_monitor.py
```

To build your own executable:
```
pip install pyinstaller
pyinstaller p2p_monitor.spec
```

Output will be at `dist/P2P Monitor.exe`. See [WINDOWS_BUILD.md](WINDOWS_BUILD.md) for full details.

---

## Setup

### DreamBot logs
Set your DreamBot log folder path in **Settings → Log Folder**. This should be the **parent Logs folder** that contains your account subfolders — not an individual account folder.

- Linux default: `/home/debian/DreamBot/Logs`
- Windows default: `C:\Users\<you>\DreamBot\Logs`

> **Note:** If you set the path to an account subfolder (e.g. `...\Logs\Accname`) the monitor will still work for that single account, but Discord thread IDs will be lost on every restart causing new threads to be created. A warning will appear in Settings if this is detected.

### Discord — webhook mode
1. Create a Discord webhook in any channel
2. Paste the URL into **Settings → Webhooks → Default Webhook**
3. Optionally add per-event webhooks (drops, deaths, errors, etc.)
4. Hit **Save**

### Discord — bot mode
1. Go to [discord.com/developers/home](https://discord.com/developers/home) → New Application → Bot → Reset Token → copy token
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

Config is automatically sanitized on startup: stale keys from older versions are removed, types are validated, and thread IDs for deleted accounts are pruned.

| Path | Contents |
|---|---|
| `~/.p2p_monitor/config.json` | All settings |
| `~/.p2p_monitor/history/<account>/history.jsonl` | Per-account event log |
| `~/.p2p_monitor/offsets.json` | Log file read positions |
| `~/.p2p_monitor/screenshots/` | Screenshot files (auto-deleted after 24h) |

On Windows, `~` resolves to `C:\Users\<you>`.

---

## Updating

**In-app (Linux):** Settings → 🔄 Check for Update — downloads and applies the update automatically, then prompts to restart.

**In-app (Windows packaged):** Settings → 🔄 Check for Update — downloads the new `.exe` directly from the GitHub release, replaces the current binary, and prompts to relaunch. No manual steps needed.

**Manual fallback:** Download the latest release from the [Releases](https://github.com/p2pmonitor/P2P-Monitor/releases/latest) page and replace the existing file.

**Via git (Linux):**
```bash
cd ~/P2P-Monitor
git pull
./install.sh
```

---

## License

GNU General Public License v3.0 — free to use, modify, and distribute under the same license. See [LICENSE](LICENSE) for full terms.
