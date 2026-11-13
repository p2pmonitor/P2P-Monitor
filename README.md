# P2P Monitor

A desktop monitor for P2P Master AI running on the **Detuks client** — tracks your RuneScape account in real time, posts Discord notifications for in-game events, launches/relaunches clients, and keeps a searchable event history.

Runs on **Linux** (Debian/Ubuntu) and **Windows 10/11**. Requires the Detuks client with the P2P Master AI script running.

> **v3.0.0 — moved from DreamBot to the Detuks client.** DreamBot is no longer supported. See [CHANGELOG.md](CHANGELOG.md) for what changed and what is migrated automatically.

> **Works best monitoring ONE account at a time.** The Detuks client writes every running client into a single shared `client.log`, and no log line says which account it came from. With one account running, the monitor knows exactly what that account is doing (tasks, pings, screenshots, relaunch). With several accounts running, their events are merged under one name.

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

### Account monitoring
- Reads the Detuks client's shared `client.log` (and its rotated `client_YYYY-MM-DD.N.log` parts) and attributes it to one account — **Settings → Log account**, or automatically your only Launcher account
- Detects account status: 🟢 Logged In, 🟡 Starting..., 🟡 Waiting for Start, 🟡 On Break, 🔴 Offline
- **Script start:** on Detuks, P2P Master AI opens its settings GUI after loading and waits for **Start**. The monitor shows 🟡 Waiting for Start and only posts *Script Started* when the run really begins (Start pressed, or the first task if the script starts on its own)
- **Auto-click Start** (on by default): about 10 seconds after the GUI opens, the monitor finds the Start button by its colour in that account's own GUI window, clicks it and restores your focus. It retries once; if the script still hasn't started, an alert goes to the Errors channel
- Tracks current task, activity, location, planned task duration, uptime (since the current script run started), and break time
- Log rotation is followed live — lines written just before a rotation are recovered from the rotated file, not lost
- If no client log exists on startup, monitor waits up to 10 minutes before stopping

### Launcher and relaunching (Detuks CLI)
- Launcher tab stores per-account presets for the Detuks CLI: the AppImage on Linux, `launcher.exe` on Windows, both run as `launch --jl-account <Account> -- --script "P2P Master AI" --scriptArgs <Profile>`
- `/launch <account>` starts a client only if that account is not already open
- `/launch all` starts all configured presets that are currently closed and skips already-open clients
- `/relaunch <account>` restarts that account's client — if **Respect breaks** is enabled and the client is running, the relaunch is queued and executes during the account's own break window (closed at break start, relaunched at break end) instead of interrupting an active session; with the setting off it restarts immediately
- `/relaunch all` restarts all configured launcher presets one at a time, each account following its own break/retry state independently
- Relaunch success is confirmed by the script's start message, never by process spawn alone; unconfirmed attempts retry automatically with increasing delays (5/10/20/30, capped at 60 minutes), and pending relaunches are remembered across monitor restarts
- Client window position/size is captured before closing and persisted per account, so it is restored even when the relaunch happens much later (break-end relaunch, retry, or after the monitor itself restarted) — best-effort, never blocks the relaunch if it fails
- Launcher command preview shows the exact command that will be used, quoted correctly for your platform (copy-pasteable)
- Client windows are matched by their exact title `BotClient - V… - <Account>` (case-sensitive) — a client that has not logged in yet, or any other window, can never be closed by a relaunch

### Auto restart after Script Stopped
- Optional auto-restart when P2P Master AI logs its stop (`Finished onExit`)
- ⚠ The Detuks log has no verifiable "manual stop" marker yet, so a stop you made by hand can also be auto-restarted while this is enabled (still limited by the update-window option)
- Can respect active break state so accounts are not restarted during a break
- Once auto-restart decides a relaunch is allowed, the launch itself uses the same safeguard as `/relaunch`: Script Started confirmation, automatic retry with backoff if startup isn't confirmed, restart-surviving pending state, and window-position restore
- Supports random min/max restart delay
- A selected `0` minute restart delay is treated as about **10 seconds**, not a true instant restart
- Pending auto-restart is cancelled if the script starts again before the timer fires

### Discord notifications
- Posts embeds for: tasks (with location and planned duration), Slayer tasks and completions, quest starts and completions, drops, deaths, level ups, kill/lap-count milestones (every 100), errors, severe lag spikes, script lifecycle events, launcher events, and daily summaries
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
- If your monitored account name differs from your WOM username, set the override in the account's Goals & Maxing page

### Event history
- Persists every event to a local JSONL file per account
- History tab shows a 24-hour rolling view by default, with a custom date-range filter (any range, no maximum) plus quick presets (today, 7 days, 30 days, all time)
- **Runtime Stats** button per account: shows total running time, active play time, break time, and break % — filterable by all-time, today, 7 days, or 30 days
- Backfill: on startup, re-reads the client log to populate history without re-pinging Discord. The very first run only imports the current `client.log` — older rotated parts can contain other accounts' lines

### Error detection
- Detects and pings on: task locks (with the missing item/teleport), login time-outs, repeating script task-loop failures, pathing lockouts, script crashes, quest state loops, farming patch skips, severe lag spikes (10s–5min freezes while playing), and more
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
- Tracks uptime since the current script run started, and cumulative break time within that run
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

### Detuks logs
Set **Settings → General Settings → Detuks Logs Folder** to the Detuks client's logs folder — the folder that contains `client.log` (it appears once the client has been started). It lives under the client's user folder: inside the Detuks wine prefix on Linux (`…/wineprefix/drive_c/users/<you>/…/logs`), under your user profile on Windows. A warning appears in Settings if no `client.log` is found.

Then set **Log account** to the account you are monitoring — the exact Launcher account name (case-sensitive). Leave it blank if you have exactly one Launcher account; it is used automatically.

### Paint reference (hide-paint screenshots)
Screenshots can hide the P2P Master AI paint first. To know whether the paint is currently shown, the monitor compares the panel's **Hide/Show** button with a reference: with the client open and the paint **shown** (button reads *Hide*), press **Settings → Paint Reference → Snap Paint Reference** once. After updating from v2.x, snap it again — the DreamBot reference is not used.

### Required OSRS message settings
For several pings to work correctly, the game must actually write those events into the client log. In OSRS, make sure the relevant messages are enabled before relying on Discord alerts.

Recommended settings to turn on:

- **Collection log notifications** — needed for collection log pings
- **Valuable loot / drop notifications** — needed for valuable loot and drop pings
- **Level-up and level 99 messages** — needed for normal level pings and special 99 pings

If these are disabled in-game, the monitor may be running correctly but never see the log lines it needs to send those Discord notifications.

### Launcher tab setup
The Launcher tab is used by `/launch`, `/relaunch`, and auto-restart after a script stop.

1. Set the launcher path at the top of the tab: the **Detuks Client AppImage** on Linux (make it executable first: `chmod +x DetuksClient.AppImage`), or **`launcher.exe`** on Windows (default install: `%LOCALAPPDATA%\BotClient\launcher.exe`).
2. Add one preset per account:

| Field | What to enter |
|---|---|
| `Account (--jl-account)` | The account's display name **exactly** as saved in the Detuks launcher — **case-sensitive** (`MyAccount` ≠ `myaccount`). Spaces are fine; enter it without quotes |
| `Script (--script)` | `P2P Master AI` |
| `Script profile (--scriptArgs)` | Your P2P Master AI profile, e.g. `Default`. The script still opens its GUI and waits for Start — the monitor clicks it for you (see below) |
| `--software-rendering` | Optional — force software rendering |
| `Settings file (--rl-properties)` | Optional — a specific `settings.properties` for this account |
| `Custom client args` | Optional — extra client arguments (passed after `--`) |

The resulting command, which you can also run yourself to test:

```bash
# Linux
./DetuksClient.AppImage launch --jl-account "MyAccount" -- --script "P2P Master AI" --scriptArgs "Default"
```
```powershell
# Windows (PowerShell)
& "$env:LOCALAPPDATA\BotClient\launcher.exe" launch --jl-account "MyAccount" -- --script "P2P Master AI" --scriptArgs "Default"
```
```bat
:: Windows (Command Prompt)
"%LOCALAPPDATA%\BotClient\launcher.exe" launch --jl-account "MyAccount" -- --script "P2P Master AI" --scriptArgs "Default"
```

Options before `--` go to the launcher; everything after `--` goes to the client. Quote names that contain spaces when typing the command yourself — the monitor passes them correctly on its own.

Presets from v2.x (DreamBot) are converted automatically on first start: the old **Params** value becomes the **Script profile**; proxy, memory, covert, fresh/nofresh, menu manipulation, no-click-walk, world and custom args are removed (DreamBot-only).

Before relying on auto-restart, test one preset manually:

```text
/launch <account>
```

Confirm that the client opens, logs into the right account, and starts P2P Master AI with the intended profile. Then test:

```text
/relaunch <account>
```

Confirm the client closes, reopens, and starts the script again without manual clicks (the monitor clicks **Start** on the script GUI — Settings → Restarts & Start → Auto-click Start). The client window is only found once that account has **logged in** (its title then ends in `- <Account>`).

### Auto restart setup
Auto restart is configured in Settings → Restarts & Start → Auto Restart.

### Script Start setup
On Detuks, `--scriptArgs <Profile>` selects the profile, but P2P Master AI still opens its GUI and waits for **Start** (Enter does nothing). With **Settings → Restarts & Start → Auto-click Start** on (the default), the monitor:

1. waits about 10 seconds after the log shows `Waiting for GUI completion` (a click you make yourself wins),
2. finds the `P2P Master AI <version>` window owned by the **same process** as that account's `BotClient` window — another account's GUI is never touched,
3. locates the Start button by its colour (works wherever the window opens and at any size), clicks it, and restores the window you were using,
4. confirms via `Start button pressed`, retries once, then posts **⏸ Waiting for Start — needs a click** to the Errors channel.

With Auto-click Start off, the same alert is sent after 5 minutes of waiting. If the client was closed while sitting at the GUI, no alert is sent.

Recommended starting settings:

- Enable auto restart after a script stop
- Use a small random delay range, such as `1` to `30` minutes
- Enable break-respect behavior if you do not want accounts restarted during active breaks

If you set the minimum and maximum delay to `0`, the monitor treats that as about **10 seconds**. This avoids relaunching instantly before the old client process has fully closed.

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
| `/force <account> <action> [amount]` | Force a non-skill action: Stats, Logs, -10m, +10m, Skip, Pause (toggle), Quest |
| `/train <account> <skill>` | Force-train a specific skill (the skill options previously under `/force`) |
| `/stats <account> <view>` | WOM stats — `current` shows all 24 skill levels; a skill name shows estimated time to 99 for that skill |
| `/max <account>` | Estimated time to max for that account (same calculation as the Stats tab) |
| `/wom refresh <account or All>` | Refresh Wise Old Man data — same backend as the Refresh WOM button, with a short cooldown |
| `/launch <account>` | Launch an account only if its client is currently closed |
| `/launch all` | Launch all configured presets that are currently closed |
| `/relaunch <account>` | Restart an account's client — honors Respect Break (see below) |
| `/relaunch all` | Restart all configured launcher presets, one at a time |

`/stats` and `/max` read the cached Wise Old Man data. If no data is cached yet (or it looks stale), run `/wom refresh` first.

`/launch` is safe and non-destructive. It skips already-open clients.

`/relaunch` is intentionally destructive — it closes and reopens the matching client so it can load the latest script/client state. How it behaves depends on the **Respect breaks** setting (Settings → Restarts & Start):

- **Respect breaks off:** the client is closed and restarted immediately. No queue, no break checks.
- **Respect breaks on, account not on break:** the relaunch is queued. The client stays open until its next break starts, is closed for the duration of that break, and relaunches at the break's end.
- **Respect breaks on, account already on break:** the client is closed now and relaunches at the break's end.

Every relaunch is confirmed by watching for the script's start message — a spawned process alone never counts as success. If startup is not confirmed within a few minutes (for example, the client is mid-update), the relaunch retries automatically with increasing delays (5, 10, 20, 30, then every 60 minutes) until it succeeds or the script is started another way. Pending relaunches survive a monitor restart. `/relaunch all` processes accounts one at a time; an account waiting out a retry delay or its break window never blocks the others.

---

## Data and files

Config is automatically sanitized on startup: stale keys from older versions are removed and types are validated. Discord thread IDs are never pruned in the standard shared-log layout.

| Path | Contents |
|---|---|
| `~/.p2p_monitor/config.json` | All settings |
| `~/.p2p_monitor/history/<account>/history.jsonl` | Per-account event log |
| `~/.p2p_monitor/offsets.json` | Log file read positions |
| `~/.p2p_monitor/screenshots/` | Screenshot files (auto-deleted after 24h) |

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
