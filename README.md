# P2P Monitor

A desktop monitor for [DreamBot](https://dreambot.org) P2P Master AI — tracks multiple RuneScape accounts in real time, posts Discord notifications for in-game events, launches/relaunches DreamBot clients, watches for script/client updates, and keeps a searchable event history.

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

### DreamBot launcher and relaunching
- Launcher tab stores DreamBot launch presets per account
- `/launch <account>` starts a client only if that account is not already open
- `/launch all` starts all configured presets that are currently closed and skips already-open clients
- `/relaunch <account>` restarts that account's DreamBot client — if **Respect breaks** is enabled and the client is running, the relaunch is queued and executes during the account's own break window (closed at break start, relaunched at break end) instead of interrupting an active session; with the setting off it restarts immediately
- `/relaunch all` restarts all configured launcher presets one at a time, each account following its own break/retry state independently
- Relaunch success is confirmed by the script's start message, never by process spawn alone; unconfirmed attempts retry automatically with increasing delays (5/10/20/30, capped at 60 minutes), and pending relaunches are remembered across monitor restarts
- Client window position/size is captured before closing and persisted per account, so it is restored even when the relaunch happens much later (break-end relaunch, retry, or after the monitor itself restarted) — best-effort, never blocks the relaunch if it fails
- Launcher command preview shows the exact Java/DreamBot command that will be used

### Auto restart after Script Stopped
- Optional auto-restart when P2P Master AI logs `Script Stopped`
- Skips auto-restart when the stop was manually initiated from the Control Bar
- Can respect active break state so accounts are not restarted during a break
- Once auto-restart decides a relaunch is allowed, the launch itself uses the same safeguard as `/relaunch`: Script Started confirmation, automatic retry with backoff if startup isn't confirmed, restart-surviving pending state, and window-position restore
- Supports random min/max restart delay
- A selected `0` minute restart delay is treated as about **10 seconds**, not a true instant restart
- Pending auto-restart is cancelled if the script starts again before the timer fires

### Update awareness
- Checks the DreamBot SDN API (`sdn.dreambot.org/scripts/all`) for the latest P2P Master AI version — falls back silently to the Cloudflare Worker cache if SDN is unavailable
- Runs once when the monitor starts, then on a configurable interval (default 6 hours, minimum 1 minute) — set in Settings → Restarts & Updates → Update Awareness
- Detects when the local P2P Master AI script version is behind the SDN version; handles trailing-zero versions correctly (SDN `2.15` = 2.150, newer than 2.149)
- Detects DreamBot client update banners such as `(NEW CLIENT AVAILABLE)`
- Sends **one grouped Discord alert** to the main monitor channel listing all accounts that need an update, organised by type (script only, client only, or both)
- Per-account deduplication — same update state does not re-alert after restart; a new version resets the alert

### Discord notifications
- Posts embeds for: tasks, Slayer tasks and completions, quest starts and completions, drops, deaths, level ups, errors, script lifecycle events, launcher events, update alerts, and daily summaries
- Supports webhook mode with per-event-type webhooks or a single default webhook
- Supports Discord bot mode with per-account monitor threads
- **Discord webhooks are not required when using Discord bot mode**
- Mute individual accounts without stopping monitoring
- **Notify / Screenshot / Ping** per event type — each controlled independently in Settings
  - **Notify** — whether the event sends a Discord message
  - **Screenshot** — whether that event attaches a screenshot
  - **Ping** — whether that event triggers a real Discord notification (mention in top-level message content)
- Real pings use `allowed_mentions` so Discord actually notifies — not just a visual `<@user>` in the embed
- Mention ID field accepts `123456789`, `<@123456789>`, or `<@!123456789>` — all normalise automatically
- Self-healing: automatically detects and recovers from deleted Discord threads, channels, and webhooks — no restart needed
- Level 99 detection: special "🎆 Level 99! 🎆" embed title, always notifies regardless of the level-up interval
- **Skip level notifications below N** (Settings → Event Notifications) — Discord-only suppression of low-level level-up messages. Default `1` relays everything (current behavior); `50` suppresses Discord messages for levels 1–49, including the startup catch-up. Suppressed level-ups are still recorded in History and shown in the Event Log, and level 99 / Total Level milestones always post.
- Repeated related failures (e.g. multiple farming lock failures at once) are grouped into one alert instead of spamming separate messages

### Stats — levels overview
- Daily Levels Gained chart, Levels by Skill breakdown, and Top Accounts — filterable by account, skill, and date range (today, 7/30 days, 1 year, all time, or a custom range)
- Built entirely from local event history — no external API calls

### Stats — Goals & Maxing (Wise Old Man integration)
- Per-account and all-accounts skill progress, pulled from the [Wise Old Man](https://wiseoldman.net) API (`Refresh WOM` button) and cached locally between refreshes
- Time-to-max estimate per account, computed from current XP and per-skill XP/hr rates
- Closest-to-99 and time-remaining estimate per skill
- "Last 99 Achieved" — combines local event history with the WOM cache; Combat is intentionally excluded since it's a derived/composite level, not a real trainable skill
- XP/hr rates are editable defaults (`Edit XP Rates`), globally or per account — not fetched from WOM, since WOM does not report personal rates
- If your DreamBot account name differs from your WOM username, set the override in the account's Goals & Maxing page

### Event history
- Persists every event to a local JSONL file per account
- History tab shows a 24-hour rolling view by default, with a custom date-range filter (any range, no maximum) plus quick presets (today, 7 days, 30 days, all time)
- **Runtime Stats** button per account: shows total running time, active play time, break time, and break % — filterable by all-time, today, 7 days, or 30 days
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

### Option B — Run from source or build your own `.exe`
Use this option only if you cloned/downloaded the full source repository. The release `.zip` is intended for normal app installation and may not include every Windows build helper file.

Run from source:

```powershell
pip install -r requirements-windows.txt
python p2p_monitor.py
```

Build your own executable:

```powershell
pip install pyinstaller
pyinstaller p2p_monitor.spec
```

Output will be at:

```text
dist/P2P Monitor.exe
```

See [WINDOWS_BUILD.md](WINDOWS_BUILD.md) for full Windows build details.

---

## Setup

### DreamBot logs
Set your DreamBot log folder path in **Settings → General Settings → DreamBot Logs Folder**. This should be the **parent Logs folder** that contains your account subfolders — not an individual account folder.

- Linux default: `/home/debian/DreamBot/Logs`
- Windows default: `C:\Users\<you>\DreamBot\Logs`

> **Note:** If you set the path to an account subfolder (e.g. `...\Logs\Accname`) the monitor will still work for that single account, but Discord thread IDs will be lost on every restart causing new threads to be created. A warning will appear in Settings if this is detected.

### Required OSRS / DreamBot message settings
For several pings to work correctly, the game/client must actually write those events into the DreamBot log. In OSRS, make sure the relevant messages are enabled before relying on Discord alerts.

Recommended settings to turn on:

- **Collection log notifications** — needed for collection log pings
- **Valuable loot / drop notifications** — needed for valuable loot and drop pings
- **Level-up and level 99 messages** — needed for normal level pings and special 99 pings

If these are disabled in-game, the monitor may be running correctly but never see the log lines it needs to send those Discord notifications.

### Launcher tab setup
The Launcher tab is used by `/launch`, `/relaunch`, and auto-restart after `Script Stopped`.

For each account preset, set up the launcher fields to match the same account, script, and proxy information you use in the DreamBot client:

| Field | What to enter |
|---|---|
| `Account (-account)` | The DreamBot account name/preset account |
| `Script (-script)` | `P2P Master AI` |
| `Proxy (-proxy)` | The same proxy name configured in DreamBot, if that account uses one |
| `Memory -Xmx (MB)` | Recommended: `1024` |
| `-covert` | Enable if you have DreamBot VIP and use Covert Mode |
| `Params (-params, last)` | Your P2P Master AI profile/params so the script can auto-start correctly |

The **Params** field is important. If the correct P2P Master AI profile/params are not set, the launcher can still restart DreamBot, but the script may not press Start or load the intended profile automatically. That makes `/relaunch` and auto-restart after `Script Stopped` much less useful because the client may reopen and sit idle.

Before relying on auto-restart, test one preset manually:

```text
/launch <account>
```

Confirm that DreamBot opens, selects the correct account/script/proxy, and starts P2P Master AI with the intended profile. Then test:

```text
/relaunch <account>
```

Confirm the client closes, reopens, and starts the script again without manual clicks.

### Auto restart setup
Auto restart is configured in Settings → Restarts & Updates → Auto Restart.

Recommended starting settings:

- Enable auto restart after `Script Stopped`
- Use a small random delay range, such as `1` to `30` minutes
- Enable break-respect behavior if you do not want accounts restarted during active breaks

If you set the minimum and maximum delay to `0`, the monitor treats that as about **10 seconds**. This avoids relaunching instantly before Windows/DreamBot fully closes the old process.

### Update awareness setup
Update awareness is enabled by default.

The monitor reads local DreamBot window titles, for example:

```text
DreamBot 4.1.67 - <account> - P2P Master AI v2.141 - <proxy name> (NEW CLIENT AVAILABLE)
```

It checks:
- The account name and local P2P Master AI script version from the title
- The DreamBot client version from the title
- Whether `(NEW CLIENT AVAILABLE)` is present
- The latest P2P Master AI script version from the DreamBot SDN API, with Cloudflare Worker as a silent fallback if SDN is unavailable

Checks run once at startup, then on the configured interval (default 6h, minimum 1m).

Primary source is the DreamBot SDN API; the Cloudflare Worker is a silent fallback.

When any account needs an update, **one grouped Discord message** is sent to the main monitor channel listing each account and what it needs:

| Account state | Latest SDN version | Alert group |
|---|---:|---|
| `P2P Master AI v2.141` | `2.143` | P2P Master AI script update needed |
| `P2P Master AI v2.141` + `NEW CLIENT AVAILABLE` | `2.143` | Both script + DreamBot update needed |
| `P2P Master AI v2.143` + `NEW CLIENT AVAILABLE` | `2.143` | DreamBot client update needed |
| `P2P Master AI v2.143` | `2.143` | No alert |

Multiple accounts needing updates appear in the same message. The embed recommends `/relaunch <account>` to restart and load the latest version.

**Ping on update:** if the `Ping` setting is enabled in the Update Awareness section, the configured user is pinged once per check.

**Auto-relaunch on update (optional, default off):** when enabled, affected accounts are relaunched automatically as soon as an update is detected. Accounts without a launcher preset are listed for manual action. This can interrupt any current activity — enable only if that is acceptable.

### Discord — webhook mode
Webhook mode is the simplest setup if you do not want to create a Discord bot.

1. Create a Discord webhook in any channel
2. Paste the URL into **Settings → Discord Alerts → Webhooks → Default Webhook**
3. Optionally add per-event webhooks (drops, deaths, errors, etc.)
4. Hit **Save**

### Discord — bot mode
Bot mode is recommended if you want slash commands, account monitor threads, and cleaner Discord management.

Discord webhooks are **not required** when using bot mode. You can leave webhook fields blank unless you intentionally want webhook fallback behavior.

1. Go to [discord.com/developers/home](https://discord.com/developers/home) → New Application → Bot → Reset Token → copy token
2. Enable **Message Content Intent** under Privileged Gateway Intents
3. OAuth2 → URL Generator → Scope: `bot` → Permissions: Send Messages, Read Message History, Manage Channels, Manage Webhooks, View Channels, Embed Links, Attach Files, Create Public Threads, Send Messages in Threads, Manage Threads, Use Slash Commands
4. Open the generated URL → select your server → Authorize
5. Right-click your server icon → Copy Server ID → paste into **Settings → Discord Alerts → Server ID**
6. Paste your bot token into **Settings → Discord Alerts → Bot Token**
7. Hit **Save** then **🤖 Run Bot Setup**

### Slash commands
Slash commands are available when using Discord bot mode.

| Command | Description |
|---|---|
| `/ss [account]` | Screenshot → post to account thread |
| `/s` | Post live status of all accounts |
| `/force <account> <action> [amount]` | Force a non-skill action: Stats, Loot, -10m, +10m, Skip, Quest |
| `/train <account> <skill>` | Force-train a specific skill (the skill options previously under `/force`) |
| `/stats <account> <view>` | WOM stats — `current` shows all 24 skill levels; a skill name shows estimated time to 99 for that skill |
| `/max <account>` | Estimated time to max for that account (same calculation as the Stats tab) |
| `/wom refresh <account or All>` | Refresh Wise Old Man data — same backend as the Refresh WOM button, with a short cooldown |
| `/launch <account>` | Launch an account only if its DreamBot client is currently closed |
| `/launch all` | Launch all configured presets that are currently closed |
| `/relaunch <account>` | Restart an account's DreamBot client — honors Respect Break (see below) |
| `/relaunch all` | Restart all configured launcher presets, one at a time |

`/stats` and `/max` read the cached Wise Old Man data. If no data is cached yet (or it looks stale), run `/wom refresh` first.

`/launch` is safe and non-destructive. It skips already-open clients.

`/relaunch` is intentionally destructive — it closes and reopens the matching DreamBot client so it can load the latest script/client state. How it behaves depends on the **Respect breaks** setting (Settings → Restarts & Updates):

- **Respect breaks off:** the client is closed and restarted immediately. No queue, no break checks.
- **Respect breaks on, account not on break:** the relaunch is queued. The client stays open until its next break starts, is closed for the duration of that break, and relaunches at the break's end.
- **Respect breaks on, account already on break:** the client is closed now and relaunches at the break's end.

Every relaunch is confirmed by watching for the script's start message — a spawned process alone never counts as success. If startup is not confirmed within a few minutes (for example, DreamBot/Java is mid-update), the relaunch retries automatically with increasing delays (5, 10, 20, 30, then every 60 minutes) until it succeeds or the script is started another way. Pending relaunches survive a monitor restart. `/relaunch all` processes accounts one at a time; an account waiting out a retry delay or its break window never blocks the others.

---

## Data and files

Config is automatically sanitized on startup: stale keys from older versions are removed, types are validated, and thread IDs for deleted accounts are pruned.

| Path | Contents |
|---|---|
| `~/.p2p_monitor/config.json` | All settings |
| `~/.p2p_monitor/history/<account>/history.jsonl` | Per-account event log |
| `~/.p2p_monitor/offsets.json` | Log file read positions |
| `~/.p2p_monitor/screenshots/` | Screenshot files (auto-deleted after 24h) |
| `~/.p2p_monitor/update_check_state.json` | Deduping state for script/client update alerts |
| `~/.p2p_monitor/update_relaunch_state.json` | Deduping state for auto-relaunch on update |

On Windows, `~` resolves to `C:\Users\<you>`.

---

## Updating

**In-app (Linux):** Settings → General Settings → Manual Update Check → 🔄 Check for Updates — downloads and applies the update automatically, then prompts to restart.

**In-app (Windows packaged):** Settings → General Settings → Manual Update Check → 🔄 Check for Updates — downloads the new `.exe` directly from the GitHub release, replaces the current binary, and prompts to relaunch. No manual steps needed.

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
