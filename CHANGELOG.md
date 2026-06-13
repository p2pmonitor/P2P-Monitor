# Changelog

## v1.8.0-beta.3
### Local DreamBot Update Awareness + `/relaunch` command

**New: `/relaunch` Discord slash command**
- `/relaunch account:<name>` — restarts the named account; closes the client if open, then launches fresh (destructive counterpart to `/launch`)
- `/relaunch account:all` — restarts all preset accounts; open clients are closed first
- `/launch` is unchanged — still skips already-open accounts (non-destructive)

**New: DreamBot / P2P Master AI update awareness**
- Reads local DreamBot window titles once at monitor startup and daily at 2:00 PM PC local time
- Fetches `latest_version` from `https://p2p-sdn-watch.p2pmonitor.workers.dev/p2p-master-ai/latest`
- Compares numerically (so `v2.9` < `v2.143` correctly)
- Three alert cases posted to the main monitor Discord channel:
  - Script outdated only → ping with local vs latest version + suggest `/relaunch`
  - Script outdated + `NEW CLIENT AVAILABLE` in title → ping both updates needed
  - Script current + `NEW CLIENT AVAILABLE` in title → ping DreamBot client update only
- No alert when everything is current
- Deduplicated: state persisted in `~/.p2p_monitor/update_check_state.json`; same condition does not alert again after app restart
- If Cloudflare endpoint is unreachable, only the `NEW CLIENT AVAILABLE` alert can still fire
- Configurable: **Update Awareness** checkbox in Settings (default on)

**Auto-restart hardening: 0-minute delay**
- If the selected random delay is 0 minutes, schedules restart after 10 seconds instead of instantly
- Logged/reported as "in 10 seconds" — not "0 minutes"
- Preserves normal behavior for delays > 0

---

## v1.8.0-beta.2
### Auto Restart After Script Stopped (Stage 2 of v1.8.0)

**Auto restart client after Script Stopped**
- New setting: **Auto restart client after Script Stopped** (default off)
- When enabled, the monitor schedules a `relaunch_account()` call after detecting `Stopped P2P Master AI!` — this closes the DreamBot client, waits 10 seconds, and launches the account fresh (the script was stopped but the client is still open)
- All restarts use the Stage 1 launcher backend (`py/launcher.py`) — no duplicate process-control logic

**Relaunch safe delay shortened: 15 s → 10 s**

**Random restart delay window**
- New settings: **Restart delay min minutes** (default 1) and **Restart delay max minutes** (default 30)
- Each account gets an independently random delay within the window, so accounts do not all relaunch at the same moment
- Monitor tab logs the scheduled delay: `⏰ [Account] Auto restart scheduled in 7m after Script Stopped`

**Respect breaks on relaunch**
- New setting: **Respect breaks on relaunch** (default on)
- When enabled and the account was mid-break when the script stopped, restart is scheduled at the break's calculated end time instead of the random window
- Falls back to random delay if break end cannot be determined safely
- Monitor tab logs the schedule: `⏰ [Account] Auto restart scheduled at 6:40 AM (break end) after Script Stopped`

**Game update window gate**
- New setting: **Only auto restart during game update window (Tue/Wed 1–4 AM PT)** (default on)
- Hardcoded window: Tuesday and Wednesday 1:00 AM – 4:00 AM `America/Los_Angeles` (handles PST/PDT automatically)
- When the checkbox is off, every Script Stopped can schedule auto restart
- Monitor tab logs when skipped: `🔄 [Account] Auto restart skipped — outside game update window (Tue/Wed 1–4 AM PT)`

**Manual stop suppression**
- Detects manual stop signature in recent log lines: `User initiated script stop via Control Bar.`
- When found, auto restart is suppressed: `🔄 [Account] Auto restart skipped — manual script stop detected`
- Checked across the current log batch and a 30-line rolling buffer per account (handles split-batch edge case)

**Monitor-initiated relaunch loop prevention**
- When the monitor closes a client (via `/launch`, relaunch_account, or auto restart), a 5-minute suppress window is set before `terminate_process_tree` is called
- The resulting `Stopped P2P Master AI!` log line does not re-trigger auto restart during this window
- Monitor tab logs when suppressed: `🔄 [Account] Auto restart skipped — monitor-initiated relaunch in progress`

**Pending timer cleanup**
- If the monitor is stopped while a restart timer is pending, the timer is cancelled cleanly in `LogWatcher.stop()`
- Timer callbacks re-check `_running`, preset existence, and suppress state before calling relaunch

---

## v1.8.0-beta.1
### Safe Launcher Backend + Discord `/launch` (Stage 1 of v1.8.0)

**New: `py/launcher.py`** — shared launcher backend used by both the Launcher tab and Discord slash commands.

- `build_command(jar, preset)` — command construction moved out of the UI; single source of truth for both the tab and Discord
- `launch_account(cfg, account)` — fresh launch; refuses with a clear message if the account is already running (preserves UI safety behaviour)
- `relaunch_account(cfg, account)` — safely identifies and closes the existing DreamBot client, waits 15 seconds, then relaunches
- `smart_launch(cfg, account)` — auto-dispatch: relaunch if running, fresh launch if not (used by Discord and launch_all)
- `launch_all(cfg)` — smart-launch every preset account with a 5-second stagger between launches

**Safety contract (never violated):**
- Never kills by generic process name (`java.exe`, `DreamBot` wildcard, etc.)
- Only closes a client when the DreamBot window title can be matched to the requested account name
- If multiple windows match (ambiguous) → refuses with explanation
- If saved PID exists but window match fails → refuses with explanation
- Saved PID state (`~/.p2p_monitor/launcher_state.json`) is always validated before use; it is a cache, not truth

**New platform helpers in `py/platform_ops.py`:**
- `get_pid_for_window(window_id)` — Linux: xdotool getwindowpid; Windows: GetWindowThreadProcessId
- `is_pid_running(pid)` — psutil preferred, falls back to OS primitives
- `get_process_cmdline(pid)` — psutil preferred, falls back to /proc on Linux
- `terminate_process_tree(pid)` — psutil preferred; falls back to taskkill /T /F (Win) or SIGTERM/SIGKILL (Linux)
- `find_account_window_and_pid(account)` — window-title lookup + PID resolution; raises ValueError on ambiguous matches

**New Discord slash command: `/launch account:<name>`**
- If not running → launch it
- If running → close safely + wait 15s + relaunch
- `/launch account:all` → smart-launch all preset accounts with stagger
- Autocomplete shows all configured launcher presets + "All accounts"
- Per-account result icons: ✅ launched/relaunched, ⚠️ skipped (ambiguous/already-up), ❌ failed

**Launcher tab refactored:**
- `_do_launch` now delegates entirely to `launcher.launch_account`
- `_build_command` removed from the tab; UI imports `build_command` from `py/launcher.py`
- Existing UI behaviour preserved: error dialog if already running, log on success/failure

**Wiring:**
- `p2p_monitor.py` creates `on_launch_cb` / `on_launch_all_cb` lambdas using `_launcher.smart_launch` and `_launcher.launch_all`
- `LogWatcher` accepts and forwards these callbacks to `GatewayRunner` as thin passthrough (no launcher logic in watcher)

---

## v1.7.0
### Inferno Tracker

Added a stateful Inferno tracker that monitors gear checks and active Inferno attempts, posting outcomes and milestones to the Tasks Discord channel.

**Two tracked concepts:**

**Inferno Gear Check**
- Opens a gear-check window on `You have the stats and quests needed for Inferno` or `Possible gear clear for Infernal Cape`
- `Inferno requirements not met` emits its event unconditionally — no gear-check window required, covering the real-world path where only `Bossing Step 0` precedes the failure
- Buffers `Resource check failed N [...]` lines (no-colon format only — prevents false positives from Fishing/Questing resource checks)
- Emits exactly one outcome per gear-check window:
  - `Inferno gear check passed` — on `You have the gear needed for Inferno!` (resource buffer discarded)
  - `Inferno requirements not met` — on requirements failure line (unconditional)
  - `Inferno gear check failed: missing usable gear/supplies — <detail>` — if window closes with buffered failures; detail is capped and included in the message
  - `Inferno gear check failed: unknown reason — <detail>` — if window closes with suspicious lines but no resource failures or known pass/fail
- Status/monitor tab updates to `Task: Inferno / Activity: Gear Check` when window opens

**Inferno Attempt**
- Starts only on `[GAME] Wave: 1` — not on `Jumping in`
- Caches high-ping data and merges it into the start message: `Inferno started — Wave 1, ping 194ms, high ping override used`
- Tracks every wave internally; status tab shows current wave for all waves
- Discord milestone events sent only at waves: 7, 15, 24, 31, 41, 48, 56, 63, 67, 68, 69
- Each milestone deduplicated per attempt — replayed or duplicate log lines never double-send
- Death: `Inferno failed — died on Wave X after Yh Mm Ss` (highest wave before death)
- Success requires `Your TzKal-Zuk kill count is: X` — Wave 69 alone is only a milestone
- State resets cleanly on death, success, and script stop

**Architecture:**
- Hard state machine in `py/inferno.py` (`InfernoTracker` class)
- Soft regex patterns and milestone list in `inferno_patterns.json`, fetched from GitHub on startup (same fallback chain as `error_rules.json`: remote → cache → packaged → emergency)
- Pattern loader in `py/inferno_rules.py` — mirrors `py/error_rules.py`; bad remote JSON never crashes the monitor
- One `InfernoTracker` instance per `AccountState` in `watcher.py`
- All events route to the Tasks Discord channel via `post_task()`
- History tab records Inferno events as task-type entries

**Files changed:**
- `py/inferno.py` — new: `InfernoTracker` state machine, `_CompiledPatterns`
- `py/inferno_rules.py` — new: remote pattern loader (GitHub → cache → packaged → emergency)
- `inferno_patterns.json` — new: soft regex/milestone config (bundle with each release)
- `py/watcher.py` — added `InfernoTracker` to `AccountState`; feeds lines in `_process_lines`; resets on script stop
- `p2p_monitor.py` — version bump to 1.7.0; wires `inferno_rules.start_background_fetch()`
- `p2p_monitor.spec` — added `inferno_patterns.json` to bundled datas
- `install.sh` — added `inferno_patterns.json`, `py/inferno.py`, `py/inferno_rules.py`
- `update_manifest.txt` — added all new Inferno files
- `README.md` — added Inferno tracking to features; updated Windows updating section

---

## v1.6.0
### Windows Packaged Self-Updater & Source Update Improvements

**Windows packaged auto-update**
- The Windows `.exe` can now detect and apply updates from within the app (Settings → 🔄 Check for Update)
- On update, the new `.exe` is downloaded from the GitHub release asset, the old binary is replaced, and the app prompts to relaunch — no manual download required
- Manual download from the [Releases](https://github.com/p2pmonitor/P2P-Monitor/releases/latest) page remains available as a safe fallback

**Update manifest (`update_manifest.txt`)**
- Introduced `update_manifest.txt` at the repo root — a plain list of files the updater applies from each release zip
- Linux/source updates now apply only the files listed in the manifest rather than replacing everything, allowing selective updates without re-running `install.sh`
- The manifest is checked on every update so new files added in future versions are automatically included

**Reliability fixes**
- Discord thread self-healing: if a monitored thread is deleted while the bot is running, the monitor detects the 404 on next post, invalidates the stale thread ID, and re-creates the thread automatically — no restart needed
- Discord channel and webhook self-healing follows the same pattern: stale IDs are evicted and re-created on detection

**Files changed:**
- `p2p_monitor.py` — Windows updater logic; version bump to 1.6.0
- `update_manifest.txt` — new: manifest for selective file updates
- `py/discord.py` — thread/channel/webhook self-healing on post failure

---

### Remote Error Rules

Error detection patterns (`ERROR_TRIGGERS`, `_LOCK_REASON_PATTERNS`, `_SILENT_LOCK_NAMES`) have been moved out of `py/reader.py` into a GitHub-hosted JSON file (`error_rules.json`). Error patterns can now be added or updated without requiring users to upgrade the monitor.

**Fallback order:**
1. **GitHub remote** — fetched from repo on startup in a background thread
2. **Local cache** — `~/.p2p_monitor/error_rules_cache.json` — last valid downloaded copy
3. **Packaged JSON** — `error_rules.json` bundled with the app release
4. **Emergency fallback** — tiny hardcoded Python dict (empty rule set); monitor never crashes even if all other sources fail

**How it works:**
- On startup, `start_background_fetch()` is called after config loads — non-blocking, UI is never delayed
- Initial rules load synchronously from packaged JSON before the background fetch completes, so the first log poll always has a valid rule set
- If remote fetch succeeds, in-memory rules are replaced immediately and the result is saved to cache
- `parse_lines()` calls `get_rules()` at parse time — remote updates apply to future log polls without restarting
- Full-file validation on every source — bad regex or missing fields reject the entire file and fall back safely
- Debug mode logs which source was used: `[ERROR_RULES] Loaded from remote / cache / packaged JSON / emergency fallback`

**Files changed:**
- `py/error_rules.py` — new module: loader, validator, compiler, cache manager, fallback handler
- `py/reader.py` — removed hardcoded rule data; imports `get_rules()` from `error_rules`
- `error_rules.json` — new file in repo root; upload to GitHub and bundle with each release
- `p2p_monitor.spec` — updated `datas` to bundle `error_rules.json` with the Windows exe
- `update_manifest.txt` — added `py/error_rules.py` and `error_rules.json`
- `p2p_monitor.py` — wires `start_background_fetch()` in `App.__init__` after config loads

## v1.4.2
### Discord Thread Duplication & Deleted Thread Recovery Fix

**Issue 1 — sanitize_config bad prune (thread duplication on restart)**

- Fixed `sanitize_config()` incorrectly pruning `bot_thread_ids` when `logs_root` is set directly to an account folder (e.g. `C:\Users\<user>\DreamBot\Logs\Accname`) instead of the parent Logs folder
- Root cause: step 5 of sanitize iterated `logs_root` looking for account subfolders; when `logs_root` is itself an account folder it contains only log files, so no subfolders are found and all thread IDs are pruned as "missing" — every monitor restart created a full new set of 8 Discord threads
- Fix: sanitize now detects this condition via new helper `is_logs_root_account_folder()` — if `logs_root` contains `logfile-*.log` files directly, the subfolder-based prune is skipped entirely
- Single-account mode (pointing `logs_root` at an account folder directly) continues to work for monitoring — only the destructive pruning is suppressed
- Added a yellow warning label in Settings → DreamBot Logs Folder that appears whenever `logs_root` points to an account folder, explaining that thread IDs will be lost on restart and suggesting the parent Logs folder path
- Affected files: `py/config.py`, `ui/settings_tab.py`

**Issue 2 — deleted Discord thread recovery gap**

- Fixed `_bot_add_user_to_thread()` silently ignoring 404 / 10003 errors when a Discord thread has been manually deleted
- Previous behavior: stale thread ID stayed in config, membership check failed silently, running Bot Setup again had no effect because `_ensure_threads_for_account()` saw all 8 channel entries present and skipped thread creation entirely
- Fix: `_bot_add_user_to_thread(account, thread_id, token)` now accepts `account` as a first parameter and detects 404 / 10003 on both the GET membership check and the PUT add call
- On 404: new `_recover_deleted_thread(account, thread_id, token)` removes only the specific stale thread ID from config, evicts the account from `_threads_verified`, saves config, and spawns `_ensure_threads_for_account(account)` in a background thread to recreate only the missing thread
- Channel IDs and webhook URLs are not touched — recovery is targeted to the deleted thread only
- All call sites updated to pass `account` to `_bot_add_user_to_thread()`
- Fixed `_bot_force_panel()` incorrectly passing `channel_id` (a plain channel or fallback ID) to `_bot_add_user_to_thread()` — membership add now only fires when a confirmed saved monitor thread ID exists for the account
- Affected files: `py/watcher.py`

**Manual workaround for users already affected**

Close the monitor, open `config.json`, delete the account entry under `bot_thread_ids` (or clear `bot_thread_ids` entirely), save, reopen the monitor, and press Start. The monitor will find or create one clean set of threads. Also correct `logs_root` in Settings to point to the parent Logs folder.

## v1.4.1
### Config Sanitization Bugfix

- Fixed `sanitize_config()` deleting legitimate config keys that were never declared in `DEFAULT_CFG` — `launcher_jar`, `launcher_presets`, `hist_col_widths`, `screenshot_on_startup`, `ui_section_discord_open`, and `ui_section_notifications_open` are now included with correct defaults so the sanitizer preserves them
- Affected files: `p2p_monitor.py`

## v1.4.0
### Discord Self-Healing, Config Cleanup & Level 99 Detection

---

**Discord self-healing**

- When a Discord thread, channel, or webhook is deleted (error 10003 Unknown Channel / 10015 Unknown Webhook), the monitor now automatically detects the 404, invalidates the stale ID from config, recreates the missing resource, and retries the failed message once — no manual intervention or restart needed
- Retried messages include a footer note: "⚠ Thread/channel was recreated — screenshot may be delayed"
- Thread recovery: removes stale thread ID → evicts account from verified set → re-creates thread → retries post
- Channel recovery: removes channel ID + webhook URL + all associated threads → re-runs bot setup → retries post
- Webhook recovery: removes stale webhook URL → re-runs bot setup to recreate → retries post
- 401 Unauthorized: flags `bot_setup_done = False` and logs "update token in Settings and re-run Bot Setup"
- 403 Forbidden / 50001 Missing Access: logs "re-invite the bot and re-run Bot Setup" without clearing setup state
- Recovery limited to one retry per failure — no infinite retry loops
- Screenshot-queued posts also self-heal: `ScreenshotService` receives a `handle_post_error` callback and wraps both `post_discord` and `post_bot_image` calls with recovery; the retry lambda captures the image path so the screenshot is included in the retry post; file cleanup happens only after recovery completes
- New DiscordRouter callbacks: `invalidate_threads`, `ensure_threads`, `run_bot_setup`, `save_cfg`
- Affected files: `py/discord.py`, `py/watcher.py`, `py/screenshot.py`

---

**Config cleanup on startup**

- New `sanitize_config()` function runs at app startup after `load_config()`
- Removes unknown/stale config keys (e.g. old `github_token` from prior versions)
- Validates types: coerces int-like strings to int, 0/1 to bool where expected; resets values that can't be coerced
- Validates `bot_channel_ids` and `bot_webhook_urls`: removes entries with invalid channel names or empty values
- Validates `bot_thread_ids` structure: removes malformed entries, invalid channel names, empty IDs; normalizes int IDs to strings
- Prunes `bot_thread_ids` for accounts whose log folders no longer exist (only when `logs_root` is set and valid)
- Logs all corrections in debug mode; saves config only if corrections were made
- Affected files: `py/config.py`, `p2p_monitor.py`

---

**Level 99 detection**

- New regex `SKILL_99_RE` detects the special game message: "Congratulations, you've reached the highest possible {skill} level of 99"
- Level 99 events use the title "🎆 Level 99! 🎆" instead of "🎉 Level Up!" in Discord embeds (gold color unchanged)
- Level 99 **always notifies** regardless of the `levelup_every` interval setting — you never miss a max level achievement
- Log prefix uses 🎆 for level 99, 🎉 for regular level ups
- The `_is_99` flag flows from reader → watcher → discord cleanly; regular level ups are completely unaffected
- Affected files: `py/reader.py`, `py/discord.py`, `py/watcher.py`

---

**Other changes**

- Version bump: v1.3.16 → v1.4.0
- Updated `_log` tag detection in `p2p_monitor.py` to recognize 🎆 emoji for level 99 log coloring
- Updated DiscordRouter docstring to document new recovery callbacks


## v1.3.16
### Code Quality, Reliability & Windows Click/Screenshot Fixes

---

**Code quality & reliability (all platforms)**

- Fixed `set_last_seen` doing a full `load_offsets` + `save_offsets` disk cycle on every poll tick — now skips the write entirely if the stored value is already current; prevents unnecessary disk wear and reduces corruption risk on ungraceful shutdown (`py/history.py`)
- Fixed `slice_tasks` BREAK START entries getting an incorrect line index of `len(arr)` — the search hint passed to `_find_ts` was `"Break"` which never matches any real log line; now passes the actual log line as the search hint so timestamps and sort order are correct by design rather than by accident (`py/reader.py`)
- Fixed dead column condition `col == 'action'` in status tab column setup — column named `'action'` does not exist; corrected to `col == 'account'` so the account column gets its intended `minwidth` (`ui/status_tab.py`)
- Fixed unused `log_files = _get_log_files(d)` glob call in the main poll loop — result was immediately discarded on every tick; removed to eliminate redundant disk I/O (`py/watcher.py`)
- Fixed dir cache not invalidating when log folder is changed in Settings — `_dirs_last_check` is now reset to 0 on save so the new path takes effect within one poll cycle instead of up to 30 seconds later (`ui/settings_tab.py`, `py/watcher.py`)
- Fixed `_startup_catchup` on log rotation running synchronously on the status refresh thread — moved to a daemon thread; prevents UI stutter on large log files during rotation (`py/watcher.py`)
- Removed `BotRunner` tombstone class — no longer imported anywhere (`py/discord.py`)
- Removed redundant `import threading` inside `_send_startup_ping` — already imported at module level (`p2p_monitor.py`)
- Removed unused top-level imports `shutil` and `re` from `p2p_monitor.py`
- Fixed blank line between `def load_offsets` and its docstring (`py/history.py`)
- Added comment documenting the intentional deferred import of `discord.py` inside `ScreenshotService._worker` to prevent circular import (`py/discord.py`)

---

**Windows screenshot & click coordinate fixes**

**Root cause:** `get_window_geometry()` uses `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` which includes the invisible drop shadow border around DreamBot windows. This made the geometry origin and height larger than the actual clickable client area — clicking at DWM-based coordinates landed below the real window bottom (e.g. y=788 when client bottom was 680), causing clicks to hit whatever was underneath DreamBot. Crop and reference capture were unaffected because they operate in DWM/image space consistently.

- Added `get_window_title(window_id)` helper to `platform_ops.py` — retrieves actual window title via `GetWindowTextW` on Windows / `xdotool getwindowname` on Linux
- `take_screenshot()` now verifies both `"dreambot"` and the account name appear in the chosen HWND's title before any capture attempt; aborts with a clear error if not matched — prevents Discord or other windows from being captured via stale HWNDs (`py/screenshot.py`)
- Added `_get_paint_click_coords(wid)` — uses `ClientToScreen(hwnd, 0,0)` as anchor to get the real client area origin on the desktop, then adds `PAINT_BTN_X/Y_OFFSET` as client-relative offsets with no DPI scaling; `take_screenshot()` now uses this for all paint toggle clicks; crop/reference/visibility detection continues using DWM-based `_get_paint_btn_coords` unchanged (`py/screenshot.py`)
- Added `_get_client_click_pos(wid, offset_x, offset_y)` — same `ClientToScreen` anchor approach for force clicks; on Linux falls back to existing DWM geometry + DPI-scaled offset path which was already correct (`py/paint.py`)
- Updated all four force-click functions (`click_at_offset`, `do_force_skill`, `do_force_panel`, `do_force`) to use `_get_client_click_pos` on Windows (`py/paint.py`)
- Replaced `GetSystemMetrics(76/77/78/79)` virtual desktop bounds in `_click_at_windows` with `EnumDisplayMonitors` union — `GetSystemMetrics` returns logical pixels on scaled monitors causing wrong normalization on multi-monitor setups; `EnumDisplayMonitors` returns physical pixel rects regardless of DPI context; `GetSystemMetrics` retained as fallback (`py/platform_ops.py`)
- Restored DPI context after click injection via `SetThreadDpiAwarenessContext(old_ctx)` — previously the context was set but never restored (`py/platform_ops.py`)
- `_capture_btn_crop()` Windows path now validates crop box bounds before calling `img.crop()` — returns `None` if crop box falls outside image bounds; `_paint_full_cap.png` temp file cleaned up after each crop (`py/screenshot.py`)
- `_save_paint_reference()` now verifies crop file exists and has non-zero size before calling `shutil.move`; an existing valid reference can no longer be overwritten by a failed or empty crop (`py/screenshot.py`)


### Windows Screenshot & Click Coordinate Fixes

**Root cause:** `get_window_geometry()` uses `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` which includes the invisible drop shadow border around DreamBot windows. This made the geometry origin and height larger than the actual clickable client area — clicking at DWM-based coordinates landed below the real window bottom (e.g. y=788 when client bottom was 680), causing clicks to hit whatever was underneath DreamBot (ChatGPT, taskbar, etc.). Crop and reference capture were unaffected because they operate in DWM/image space consistently.

**Screenshot paint hide/show click fix (`py/screenshot.py`):**
- Added `_get_paint_click_coords(wid)` — uses `ClientToScreen(hwnd, 0,0)` as anchor to get the real client area origin on the desktop, then adds `PAINT_BTN_X/Y_OFFSET` as client-relative offsets with no DPI scaling; returns `None` on Linux where client area equals frame
- `take_screenshot()` now uses `_get_paint_click_coords` for all three paint toggle click sites, falling back to `btn_coords` on Linux; crop/reference/visibility detection continues using `_get_paint_btn_coords` (DWM-based) unchanged

**Force click fix (`py/paint.py`):**
- Added `_get_client_click_pos(wid, offset_x, offset_y)` — same `ClientToScreen` anchor approach; on Linux falls back to existing DWM geometry + DPI-scaled offset path which was already correct
- Updated all four force-click functions (`click_at_offset`, `do_force_skill`, `do_force_panel`, `do_force`) to use `_get_client_click_pos` on Windows

**Click injection fix (`py/platform_ops.py`):**
- Replaced `GetSystemMetrics(76/77/78/79)` virtual desktop bounds with `EnumDisplayMonitors` union — `GetSystemMetrics` returns logical pixels on scaled monitors causing wrong normalization; `EnumDisplayMonitors` returns physical pixel rects for every monitor regardless of DPI context; `GetSystemMetrics` retained as fallback
- Restored DPI context after click via `SetThreadDpiAwarenessContext(old_ctx)`

**Screenshot HWND guard (`py/screenshot.py`):**
- Added `get_window_title(window_id)` to `platform_ops.py` — retrieves actual window title via `GetWindowTextW` on Windows / `xdotool getwindowname` on Linux
- `take_screenshot()` now verifies both `"dreambot"` and the account name appear in the chosen HWND's title before any capture; aborts with a clear error if not matched — prevents Discord or other windows from being captured via stale HWNDs

**Crop/reference safety guards (`py/screenshot.py`):**
- `_capture_btn_crop()` Windows path now validates crop box bounds before calling `img.crop()` — returns `None` if `rel_x0 < 0`, `rel_y0 < 0`, or crop extends past image edge; `_paint_full_cap.png` temp file cleaned up after each crop
- `_save_paint_reference()` now verifies crop file exists and has non-zero size before calling `shutil.move`; an existing valid reference can no longer be overwritten by a failed or empty crop


### Code Quality & Reliability Fixes

- Fixed `set_last_seen` doing a full `load_offsets` + `save_offsets` disk cycle on every call — now reads and writes only when the stored value has actually changed, and skips the write entirely if the value is already current; prevents unnecessary disk wear and reduces corruption risk on ungraceful shutdown (`py/history.py`)
- Fixed `slice_tasks` BREAK START entries getting an incorrect line index of `len(arr)` — the search hint passed to `_find_ts` was `"Break"` (the synthesised task name) which never matches any real log line; now passes the actual log line as the search hint so timestamps and sort order are correct by design rather than by accident (`py/reader.py`)
- Fixed dead column condition `col == 'action'` in status tab column setup — column named `'action'` does not exist; corrected to `col == 'account'` so the account column gets its intended `minwidth` (`ui/status_tab.py`)
- Fixed unused `log_files = _get_log_files(d)` glob call in the main poll loop — result was immediately discarded on every tick; removed to eliminate redundant disk I/O every poll interval (`py/watcher.py`)
- Fixed dir cache not invalidating when log folder is changed in Settings — `_dirs_last_check` is now reset to 0 on save so the new path takes effect within one poll cycle instead of up to 30 seconds later (`ui/settings_tab.py`, `py/watcher.py`)
- Fixed `_startup_catchup` on log rotation running synchronously on the status refresh thread — moved to a daemon thread matching the initial startup catchup path; prevents UI stutter on large log files during rotation (`py/watcher.py`)
- Removed `BotRunner` tombstone class — no longer imported anywhere; dead code removed (`py/discord.py`)
- Removed redundant `import threading` inside `_send_startup_ping` — `threading` is already imported at module level (`p2p_monitor.py`)
- Removed unused top-level imports `shutil` and `re` from `p2p_monitor.py` — both are used only in submodules
- Fixed blank line between `def load_offsets` and its docstring — cosmetic editing artifact (`py/history.py`)
- Added comment documenting the intentional deferred import of `discord.py` inside `ScreenshotService._worker` to prevent circular import; guards against future refactors accidentally moving it to module level (`py/discord.py`)


### Windows DPI Scaling Fix for Button Clicks
- Added `get_window_dpi_scale(window_id)` helper in `platform_ops.py` — queries `GetDpiForWindow` for the actual DPI of the monitor the window is on; returns scale factor (1.0 at 100%, 1.25 at 125%, 1.5 at 150% etc); returns 1.0 on Linux and on any error
- Fixed paint hide/show click landing in wrong position at non-100% DPI — `PAINT_BTN_X_OFFSET` and `PAINT_BTN_Y_OFFSET` were hardcoded at 100% DPI assumptions; now scaled by DPI factor using `round()` for accurate physical pixel positions
- Fixed paint reference crop capturing wrong area at non-100% DPI — `PAINT_BTN_CROP_W` and `PAINT_BTN_CROP_H` now scaled by DPI factor; affects both Linux ImageMagick crop and Windows BitBlt crop
- Fixed all force commands (`/force Stats`, `/force Loot`, `/force Hide`, `/force +10m`, `/force -10m`, `/force Skip`) clicking wrong position at non-100% DPI — all `CLICK_OFFSETS` in `paint.py` now scaled by DPI factor at point of use
- Linux unaffected — `get_window_dpi_scale` always returns 1.0 on Linux, all multiplications are no-ops
- Note: only hardcoded UI offsets are scaled; window bounds are already physical pixels from the v1.3.14 DWM fix and are not scaled

### Paint Constants Centralized
- Moved `PAINT_BTN_X_OFFSET`, `PAINT_BTN_Y_OFFSET`, `PAINT_BTN_CROP_W`, `PAINT_BTN_CROP_H` from `paint.py` and `screenshot.py` into `platform_ops.py` as the single source of truth; both files import from there
- Added `paint_ref_scale.txt` companion file saved alongside the paint reference image — stores the DPI scale at snap time; `_paint_is_visible` reads this on every call and auto-resnaps the reference if scale has changed by more than 0.05 (e.g. window moved to different monitor or user changed DPI setting); guarded against resnap loops

## v1.3.14
### Windows Screenshot Overhaul
- Added `WindowBounds` NamedTuple for clean geometry results across all capture/geometry callers
- Added `_get_window_bounds(hwnd, debug_log=None)` shared helper — tries `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` first (true visible rendered frame), falls back to `GetWindowRect`; both use screen coordinate space consistent with `GetDC(NULL)` + BitBlt; DPI awareness set and restored per call
- Fixed Windows screenshot capturing wrong position and wrong content at non-100% DPI scaling and multi-monitor setups — root cause was mixed coordinate spaces (`GetClientRect`/`ClientToScreen` vs screen DC); now uses DWM bounds + `GetDC(NULL)` + BitBlt in consistent Pattern A
- Added proper `argtypes`/`restype` on all GDI/user32 calls (`GetDC`, `CreateCompatibleDC`, `CreateCompatibleBitmap`, `SelectObject`, `BitBlt`, `GetDIBits`, `DeleteObject`, `DeleteDC`, `ReleaseDC`, `PrintWindow`) — prevents handle truncation on 64-bit Windows where handles are 8 bytes but ctypes defaults to 4-byte `c_int`
- Added three-path fallback: DWM + BitBlt → GetWindowRect + BitBlt → PrintWindow last resort; silent to normal users, detailed logging in Debug Mode
- `get_window_geometry()` now uses `_get_window_bounds` — paint button coordinates and force command click positions use the same geometry as capture, ensuring alignment
- `capture_window_image()` accepts optional `debug_log` callback — passed through to capture backend for per-call geometry logging in Debug Mode
- Fixed stale comment in `screenshot.py` referencing PrintWindow; updated `platform_ops.py` docstring to accurately describe DWM-first capture backend
- Cleaned duplicate entries in CHANGELOG.md

## v1.3.13
### Windows Screenshot Fix
- Fixed screenshot capturing wrong position and cutting off content at non-100% DPI scaling (e.g. 125%) — replaced `GetClientRect` + `ClientToScreen` with `GetWindowRect` which returns the true physical pixel position and size of the window directly from Windows, no coordinate conversion or DPI math needed; works correctly at any DPI scaling setting

## v1.3.12
### Bug Fix
- Fixed launcher not working after v1.3.11 — the new `os.path.isfile(jar)` check introduced in v1.3.11 used `os` without importing it at the module level, causing a silent `NameError` that prevented any launch from completing on both Linux and Windows

## v1.3.11
### Bug Fix
- Fixed last-seen marker being lost on monitor shutdown — `save_offsets` was overwriting `offsets.json` with only in-memory byte offsets, clobbering `__last_seen` keys written directly to disk by `set_last_seen`; shutdown now merges disk contents with in-memory offsets before saving so both are preserved

## v1.3.10
### Bug Fixes
- Fixed backfill writing history entries with current time instead of the actual log timestamp — `parse_lines` returns the timestamp in the `ts` field but the backfill was calling `ev.get('time')` which always returned `None`, causing `append_history` to fall back to the current time; now correctly passes `ev.get('ts')`
- Fixed last-seen marker not advancing to end of file — marker is now always set to the final raw line of each file after all chunks are processed
- Fixed last-seen marker being lost on monitor shutdown — `save_offsets` was overwriting `offsets.json` with only in-memory byte offsets, clobbering `__last_seen` keys written directly to disk; shutdown now merges disk contents with in-memory offsets before saving

## v1.3.9
### Bug Fixes
- Fixed `append_history` call in backfill using wrong argument format — was passing the full event dict as the second argument instead of unpacking `type`, `value`, `activity`, `timestamp` as separate positional args; caused backfill error on startup
- Fixed break time showing in status tab for offline accounts — break time now shows `—` when account is offline, matching uptime behavior
- Fixed backfill last-seen marker not advancing to end of file — `new_last_seen` was only updated inside `_process_chunk` when events were found; now always set to the final line of each file after all chunks are processed, preventing re-processing of already-seen content on next startup

## v1.3.8
### History Duplication Fix
- Fixed root cause of duplicate history entries — `_backfill_history` was using filename sort to determine the active log file, which disagreed with `_get_active_log_file`'s mtime-based selection; the real active file was being processed from byte 0 as a rotated file, re-writing events already recorded live; backfill now uses mtime consistently with the poll loop
- Reverted `_base_log_name` rotation-suffix stripping — DreamBot `.log.1` files are independent older session files not rotated versions of `.log`; stripping the suffix caused incorrect scanned-set lookups
- Fixed break time persisting in status tab when account goes offline — `break_time` is now cleared when an account is detected as having no active session

### Windows Screenshot Fix
- Replaced `PrintWindow` with `BitBlt` from screen DC — `PrintWindow` was triggering DreamBot's Java renderer to repaint multiple times causing visible flickering and occasional black frames; `BitBlt` reads the screen compositor output directly with no repaints; window is already focused by caller so it is guaranteed to be on screen

## v1.3.7
### Windows Core Fixes
- Fixed monitor not detecting script activity on Windows — active log file selection was using newest-by-filename as fallback when handle scan is unreliable; DreamBot creates a new log file on each launch so the newest filename is often a nearly-empty new session file while the real active file is older; now uses most-recently-modified file (mtime) which correctly identifies the file DreamBot is actively writing to
- Fixed clicks landing on wrong window/monitor — replaced `mouse_event` with `MOUSEEVENTF_ABSOLUTE` with `PostMessage(WM_LBUTTONDOWN/UP)` sent directly to the target window handle; coordinates are client-relative via `ScreenToClient`, no normalization math, works correctly on any monitor layout and any DPI scaling
- Fixed intermittent black screenshots — added 150ms sleep after `PrintWindow` before reading the bitmap; DreamBot uses hardware-accelerated Java2D (DirectX) and the GPU needs time to composite the frame into the capture buffer

### Duplicate Launch Fix
- Replaced psutil cmdline inspection for duplicate launch detection with window title lookup using `find_window_ids_by_name` — psutil cmdline access fails silently on both Linux and Windows due to process access restrictions; window title matching is already proven to work correctly on both platforms

## v1.3.6
### Critical Windows Fix — Active Log File Detection
- Fixed monitor not detecting any script activity on Windows — the fallback log file selection was using newest-by-filename which picked a newly created empty file instead of the file DreamBot was actively writing to; fallback now uses most-recently-modified mtime which correctly identifies the actively-written file regardless of filename order; also naturally ignores rotated .log.1 files which are not touched after rotation

### Windows Click Fix — Multi-Monitor and DPI
- Replaced mouse_event(MOUSEEVENTF_ABSOLUTE) with PostMessage(WM_LBUTTONDOWN/UP) for all Windows clicks — old approach required coordinate normalization across the virtual desktop which broke on multi-monitor setups; new approach uses WindowFromPoint to find the exact window under the target coordinates, converts to client-relative coords, and sends directly to the window message queue; works correctly on any monitor layout and any DPI scaling, cursor does not physically move

### Windows Screenshot Fix
- Added 150ms sleep after PrintWindow before reading the bitmap — DreamBot uses hardware-accelerated Java2D rendering; without the sleep the captured bitmap was black mid-frame while the GPU was still compositing

### History Duplicate Fix
- Fixed duplicate history entries after log rotation — scanned records now use base filename without rotation suffix

### Startup Fix  
- Fixed startup task appearing for offline accounts — offline accounts now have _startup_done=True set immediately

### Force Command Fixes
- Fixed all three force commands failing when DreamBot window was minimized — now restores before getting geometry
- Fixed PrintWindow returning 0x0 client area for minimized windows

### Duplicate Launch Fix
- Fixed duplicate DreamBot client detection on both Linux and Windows

### Screenshot During Break Fix
- Fixed scheduled screenshots firing during breaks or for offline accounts

### UI Fixes
- Launcher tab: selection persists on account rows for Launch Selected; deselects only on action clicks
- History tab: deselects when clicking empty space
- Path browse dialogs normalize separators on Windows

### Bug Fixes
- Fixed duplicate history entries after log rotation — `logfile-X.log.1` was not recognised as already scanned after rotating from `logfile-X.log`; scanned records now use the base filename (without rotation suffix) so rotated files are correctly skipped
- Fixed startup task appearing for offline accounts — `get_account_rows` was calling `_startup_catchup` on accounts where `_startup_done=False` because they were skipped at startup; offline accounts now have `_startup_done=True` set immediately
- Fixed all three force commands (`do_force_panel`, `do_force_skill`, `do_force`) getting window geometry before focusing — if the window was minimized, geometry returned `None` and the command bailed silently; now restores if minimized, focuses, waits, then gets geometry
- Fixed `capture_window_image` (`PrintWindow`) failing with `window has no client area (0x0)` when window is minimized — now restores window before calling `GetClientRect`
- Increased sleep after restore from minimized to 0.5s (was 0.3s) — gives window time to render before geometry query and click

### UI Fixes
- Fixed launcher tab deselecting account immediately on click — selection now persists for account rows so "Launch Selected" works; deselect only fires on action column clicks (Launch/Edit/Delete) and clicks outside rows
- Fixed history tab not deselecting on click outside a row — now deselects when clicking empty space or non-row regions
- Added os.path.normpath to path browse callbacks in Settings and Launcher — normalises forward/back slashes from filedialog on Windows
- Fixed duplicate launch detection on both Linux and Windows — psutil cmdline fetched upfront via process_iter can return empty/None for processes that change state during iteration; now filters to java processes first then calls proc.cmdline() individually with per-process error handling
- Fixed scheduled screenshots firing during breaks — break check now also tests _break_start_ts (covers transition states) and script_running (skips offline accounts)

## v1.3.5
### Window Matching & Screenshot Fixes
- Fixed wrong window being captured on Windows — `find_window_ids_by_name` now requires both "DreamBot" AND the account name in the window title, preventing false matches against Discord threads or other windows containing the account name
- Replaced `ImageGrab.grab()` with `PrintWindow` Win32 API for window capture — captures the window's own render buffer regardless of whether it's in the background, minimized, or covered; eliminates black screenshots
- Paint button crop on Windows now uses `PrintWindow` + client-relative coordinate crop instead of screen region grab — works correctly regardless of window position or occlusion
- Added `SetThreadDpiAwarenessContext` to window geometry and click operations — coordinates are physical pixels on all DPI scaling settings; fixes click positions on 125%, 150%, 200% scaled displays

### Paint Reference
- Added "Snap Paint Reference" button in Settings → Paint Reference — focuses a DreamBot window, captures the paint button region, saves as reference, restores focus; includes instructions for correct state
- Paint reference capture now passes window handle (`wid`) through the detection chain so crops use `PrintWindow` rather than screen grabs

### UI Polish
- Split status tab action column into separate Mute and Screenshot columns — each has its own click zone, no more guessing the midpoint
- Split launcher tab action column into separate Launch, Edit, and Delete columns — same fix
- Treeview rows deselect on click and on refresh — no persistent blue highlight

## v1.3.4
### Windows Debug Noise Fix
- Fixed repeated `[DEBUG] handle scan unreliable` messages appearing in the monitor log on Windows — the message now only logs once per session on first occurrence instead of every time `check_active_sessions` or `_is_folder_active` runs

## v1.3.3
### Discord Rate Limit Fix
- Fixed HTTP 429 rate limit errors when adding the mention user to multiple Discord threads on startup — now checks thread membership first (GET before PUT) and skips the add if already a member; on subsequent startups where the user is already in all threads, no PUT requests are made at all
- If a PUT does hit a 429, waits the `retry_after` duration from the response and retries once before logging failure
- Fixed membership check not running for threads that already existed in config — `_ensure_threads_for_account` now verifies membership for all threads on every startup, not just newly created ones; the GET check is cheap so existing members are detected and skipped instantly

### Windows Launcher Fix
- Fixed blank CMD window appearing when launching DreamBot from the Launcher tab on Windows — added `CREATE_NO_WINDOW` creation flag on Windows so the process launches silently in the background; closing the CMD no longer kills DreamBot; Linux behavior unchanged

## v1.3.2
### Windows Performance Fix
- Fixed severe Windows lag on monitor start — `open_files()` on Windows queries NtQueryObject for every handle in every Java process; a running DreamBot JVM keeps hundreds of handles open, causing psutil to block for minutes even when filtering to java processes only
- Windows `get_open_log_handles()` now returns `reliable=False` immediately instead of attempting the scan; watcher uses name-based log file selection (newest `logfile-*.log`) as fallback, which already worked correctly
- Linux behavior completely unchanged — still uses `readlink /proc/*/fd/*`

## v1.3.1
### Bug Fixes
- Fixed Windows UI becoming unresponsive after starting the monitor — `push_refresh()` was spawning a new thread on every watcher event with no guard; threads accumulated faster than they completed, flooding the Tkinter event queue with pending treeview rebuilds; fixed with `_refresh_in_flight` flag
- Fixed status tab treeview doing a full delete+rebuild on every refresh — now updates rows in place; only rebuilds if the account set changes; significantly reduces Tkinter rendering work on every event
- Fixed task and activity showing `--` when restarting the monitor mid-break — now correctly shows "Break" with the break length in the activity column
- Fixed Discord developer link in README and Settings tab — `discord.com/developers` → `discord.com/developers/home`

## v1.3.0
### Performance Fix
- Fixed severe lag on Windows — `get_open_log_handles()` was scanning all running processes via psutil `open_files()` on every call, which is expensive on Windows
- Windows scan now filtered to `java`/`javaw` processes only — DreamBot always runs as Java; reduces scan from 100+ processes to 2-5
- Added 15-second result cache to `get_open_log_handles()` — scan runs at most once per 15 seconds regardless of how many times it's called; worst-case delay detecting a closed client is 15 seconds
- Linux behavior unchanged — `readlink /proc/*/fd/*` is already fast and benefits from the cache as a bonus

## v1.3.0-beta.1
### Windows Port — First Beta

This release introduces cross-platform support. Linux behavior is unchanged.
Windows is a first beta and requires manual validation before production use.

**Platform abstraction layer**
- Created `py/platform_ops.py` — owns all OS-specific operations; rest of codebase never calls platform tools directly
- `get_open_log_handles()` — Linux: `readlink /proc/*/fd/*`; Windows: psutil process scan; returns `HandleScanResult` with `paths`, `reliable`, and `reason` fields so callers can distinguish a confirmed empty scan from an unreliable one
- `is_account_process_running()` — psutil-based duplicate launch detection on both platforms; pgrep no longer used
- `open_path()` — Linux: xdg-open; Windows: os.startfile()
- `find_window_ids_by_name()` — Linux: xdotool; Windows: Win32 EnumWindows + title matching via ctypes
- `capture_window_image()` — Linux: ImageMagick import; Windows: PIL.ImageGrab + Win32 GetWindowRect
- `get_focused_window()`, `get_window_geometry()`, `is_window_minimized()`, `restore_window()`, `raise_and_focus_window()`, `minimize_window()`, `click_at()` — full window control API with Linux (xdotool/xprop) and Windows (ctypes Win32) backends
- `supports_paint_detection()` — returns True on Linux (ImageMagick) and True on Windows when Pillow is available; False otherwise
- `normalize_path()` — cross-platform path normalization for open-handle comparisons
- All platform_ops functions fail safely and never raise

**Watcher reliability on Windows**
- `HandleScanResult` distinguishes reliable scans (trust empty = no handles) from unreliable scans (psutil missing, scan failed)
- `check_active_sessions()` and `_is_folder_active()` fail open on unreliable scans — never force sessions offline based on an untrusted result
- `stop()` EOF pin skipped when scan is unreliable
- Path comparisons normalized with `os.sep` throughout

**Screenshot — end-to-end on Windows**
- `take_screenshot()` fully wired through platform_ops: find window → check minimized → restore → raise/focus → wait → capture → restore prior window
- Paint hide/show detection on Windows uses Pillow `ImageGrab.grab()` + `ImageChops.difference()` — same threshold as Linux; no ImageMagick required
- Paint detection skipped gracefully on Windows if Pillow unavailable; screenshot still succeeds
- `SCREENSHOT_DIR` moved from `/tmp/screenshots` to `~/.p2p_monitor/screenshots` — persistent across reboots
- `os.devnull` replaces `/dev/null` in ImageMagick compare call

**Force commands on Windows**
- `paint.py` fully routed through platform_ops — no direct xdotool/X11 calls
- `do_force()`, `do_force_skill()`, `do_force_panel()` use `get_window_geometry()`, `raise_and_focus_window()`, `click_at()` from platform_ops
- Windows `raise_and_focus_window()` uses AttachThreadInput for reliable foreground activation
- Windows `click_at()` uses `MOUSEEVENTF_ABSOLUTE` with normalized 0–65535 coordinates

**Updater — frozen/packaged builds**
- `_is_frozen()` helper detects PyInstaller packaged execution
- `SCRIPT_PATH` uses `sys.executable` when frozen, `__file__` for source installs
- Frozen builds: update check opens GitHub releases page in browser; no in-place file patching; no `os.execv`
- Source installs: existing staged patch + restart behavior unchanged

**Runtime dependency handling**
- Both discord.py pip-install paths (`discord.py` gateway runner and settings bot setup) detect frozen mode and emit a clear error instead of attempting pip install
- `is_frozen()` centralized in `py/util.py`; imported by all callers

**Windows packaging**
- `requirements-windows.txt` — psutil, Pillow, pystray, discord.py
- `p2p_monitor.spec` — PyInstaller spec with hidden imports for all bundled deps; `console=False`
- `WINDOWS_BUILD.md` — build steps, known beta limitations, distribution, troubleshooting

**Debug logging**
- Debug mode toggle in Settings → Debug — routes previously silent internal failures to monitor tab
- `_dbg()` helper on LogWatcher; `log_fn + debug` params on history/config functions
- `config.py` now logs load/save failures in debug mode

**Anonymous usage stats**
- Startup ping sends version + OS to `stats.p2pmonitor.workers.dev` on app open; background thread; 3s timeout; no retry; no personal data
- Can be disabled in Settings → Debug → Enable anonymous usage stats

**Install fix**
- `install.sh` now copies `py/platform_ops.py` and `ui/launcher_tab.py` — previously missing, causing ImportError on fresh installs
- Added `psutil` to pip install step in `install.sh`
- `install.sh`, `update_manifest.txt`, and actual repo files are fully in sync

---

## v1.2.4
### Reliability & Launcher

- Added debug mode toggle in Settings — routes previously silent internal failures to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Replaced `pgrep -f account` in launcher with `psutil` process inspection — checks for java + `-jar` + account name in command line, reducing false positives; falls back to `pgrep` if psutil unavailable
- Added event dict contract documentation above `handle_event()` in watcher.py
- Added TODO markers to large functions noting intentional deferral of refactoring

---

## v1.2.3
### Slayer & History Fixes

- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned
- Added skip notification for tasks cancelled immediately as unsupported — task ID 126 identified as Spiritual creatures; other unknown IDs show as "Unknown task (ID X)"
- Added automatic history dedup after backfill — cleans duplicate entries left by previous versions
- Fixed duplicate log message ("Removed" + "Cleaned") for history dedup

---

## v1.2.2
### History Dedup Fix

- Fixed history duplication when DreamBot closes before the monitor is stopped — active log files are EOF-pinned on monitor exit so backfill on next startup finds nothing new to re-read

---

## v1.2.1
### Status & Stability Fixes

- Fixed task and activity showing blank in status tab when monitor starts mid-session
- Fixed status tab not updating to new Slayer task after completing
- Added dedup to history writes — identical consecutive entries are silently dropped
- Added "Screenshot on monitor startup" toggle in Settings (defaults off)

---

## v1.2.0
### CLI Launcher & Log Detection

- Added CLI Launcher tab for launching DreamBot clients directly from the monitor
- Replaced `lsof` with `readlink /proc/*/fd/*` for active log file detection — faster and no external dependency
- Fixed duplicate `_auto_refresh` thread leak in status tab
- Fixed teleport reason now captures item name correctly
- Eight reader.py bug fixes: script events all occurrences, slayer dedup resets, cancel search range, single-anchor complete, pet+collection dedup, QuestStep traversal reason, slice_last_task no Break, teleport reason from brackets
