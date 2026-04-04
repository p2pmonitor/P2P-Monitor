# P2P Monitor

A desktop monitor for [DreamBot](https://dreambot.org) P2P Master AI — tracks multiple RuneScape accounts in real time, posts Discord notifications for in-game events, and keeps a searchable event history.

Built for Debian 12. Requires DreamBot with the P2P Master AI script running.

---

## Features

### Multi-account monitoring
- Watches all accounts simultaneously from a single window
- Detects account status: 🟢 Logged In, 🟡 Starting..., 🟡 On Break, 🔴 Offline
- Status updates reflect actual script lifecycle: Starting... on script init, Logged In once in-game, Offline when script stops
- Tracks current task, activity, uptime, and break time per account
- Auto-detects new accounts when DreamBot starts a new log file
- If no active sessions exist on startup, monitor waits up to 10 minutes for logs to appear before stopping

### Discord notifications
- Posts embeds for: tasks, Slayer tasks and completions, quest starts and completions, drops, deaths, level ups, errors, and script lifecycle events (start/stop/pause/resume)
- Supports per-event-type webhooks or a single default webhook for everything
- Supports Discord bot mode with per-account monitor threads for clean organized feeds
- Mute individual accounts without stopping monitoring
- Screenshot on event: attach a game screenshot to any event type's Discord post
- `/ss [account]` — slash command to trigger an on-demand screenshot
- `/s` — slash command to post a live status summary of all accounts
- `/force <account> <action> [amount]` — slash command to force a skill, action, or time adjustment from Discord

### Event history
- Persists every event to a local JSONL file per account
- History tab shows a 24-hour rolling view, filterable by date range (up to 7 days)
- Events are grouped by account, sortable by any column
- Hover over any truncated cell to see the full text in a tooltip
- Backfill: on startup, re-reads DreamBot log files to populate history without re-pinging Discord

### Error detection
- Detects and pings on: login failures, world hop failures, pathing lockouts, stuckness, script crashes, server force-stops, GE failures, quest state loops, quest abandonment with reason, task locks with missing item detail, farming patch skips, overcrowded locations, construction errors, and impossible task detection
- Errors enriched with last known task and activity context so you know exactly what was running when the error occurred
- Deduplication prevents spam for repeated errors within the same task block

### Uptime and break tracking
- Tracks total session uptime per account
- Tracks cumulative break time across the session
- Break time persists correctly across monitor restarts

---

## Requirements

- Debian 12 (or compatible Debian-based Linux)
- Python 3 with Tkinter
- DreamBot with P2P Master AI script
- xdotool, ImageMagick (for screenshots)
- A Discord bot or webhook URL (optional, for notifications)

---

## Getting Started

Clone the repo to your server:

```bash
git clone https://github.com/p2pmonitor/P2P-Monitor.git
cd P2P-Monitor
```

Then follow the Installation steps below.

---

## Installation

```bash
chmod +x install.sh
./install.sh
```

The installer:
- Installs system dependencies (`python3-tk`, `xdotool`, `imagemagick`, etc.)
- Installs Python dependencies (`tkcalendar`, `pillow`)
- Copies all files to `~/.p2p_monitor/`
- Creates a desktop shortcut

To run manually:
```bash
python3 ~/.p2p_monitor/p2p_monitor.py
```

---

## Setup

### DreamBot logs
Set your DreamBot log folder path in **Settings → Log Folder**. Each subfolder inside that path corresponds to one account.

Default DreamBot log location: `/home/debian/DreamBot/Logs`

### Discord — webhook mode
1. Create a Discord webhook in any channel
2. Paste the URL into **Settings → Webhooks → Default Webhook**
3. Optionally add per-event webhooks (drops, deaths, errors, etc.)
4. Hit **Save**

Events post to the matching webhook, falling back to Default Webhook if no specific one is set.

### Discord — bot mode
1. Go to [discord.com/developers](https://discord.com/developers) → New Application → Bot → Reset Token → copy token
2. Enable **Message Content Intent** under Privileged Gateway Intents
3. OAuth2 → URL Generator → Scope: `bot` → Permissions: Send Messages, Read Message History, Manage Channels, Manage Webhooks, View Channels, Embed Links, Attach Files, Create Public Threads, Send Messages in Threads, Manage Threads, Use Slash Commands
4. Open the generated URL in a browser → select your server → Authorize
5. Right-click your server icon → Copy Server ID → paste into **Settings → Server ID**
6. Right-click your Discord username → Copy User ID → paste into **Settings → Discord Mention ID**
7. Paste your bot token into **Settings → Bot Token**
8. Hit **Save** then **🤖 Run Bot Setup**

Bot mode creates a dedicated `#monitor` channel with one thread per account. Slash commands register automatically on first run.

### Slash commands
| Command | Description |
|---|---|
| `/ss [account]` | Screenshot → post to account thread |
| `/s` | Post live status of all accounts |
| `/force <account> <action> [amount]` | Force a skill, action, or time adjustment |

`/ss` and `/force` used inside an account thread target that account automatically.

---

## Data and files

| Path | Contents |
|---|---|
| `~/.p2p_monitor/config.json` | All settings |
| `~/.p2p_monitor/history/<account>/history.jsonl` | Per-account event log |
| `~/.p2p_monitor/offsets.json` | Log file read positions |

### Clearing history
History is never pruned automatically. To clear an account's history, delete its subfolder in `~/.p2p_monitor/history/` — the **📂 History Folder** button in the History tab opens it directly. The monitor repopulates from DreamBot log files on next restart.

To force a re-parse of specific log files without clearing all history: open the account's `history.jsonl`, delete the `{"type":"scan","file":"logfile-XYZ.log"}` scan record line for those files, and restart. Only the unscanned files will be re-parsed.

---

## Updating

**In-app:** Settings → 🔄 Check for Update

The updater fetches all module files from GitHub and writes them to `~/.p2p_monitor/`. It backs up `p2p_monitor.py` before applying. A restart prompt appears when the update is complete.

**Via git:**

```bash
cd ~/P2P-Monitor
git pull
./install.sh
```

---

## License

GNU General Public License v3.0 — free to use, modify, and distribute under the same license. See [LICENSE](LICENSE) for full terms.
