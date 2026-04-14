# Changelog

## v1.2.4
### Reliability
- Added debug mode toggle in Settings → Debug — when enabled, internal errors that were previously swallowed silently are now logged to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Normalized all existing `[DEBUG]` log calls in watcher.py to use the new `_dbg()` helper so they respect the debug flag consistently
- Annotated intentionally silent catches in screenshot.py (paint comparison, file pruning) to distinguish "safe to ignore" from "should never fail"

### Launcher
- Replaced `pgrep -f account` duplicate-launch detection with `psutil` process inspection — checks for a `java` process with both `-jar` and the account name in its command line, eliminating false positives from unrelated processes; falls back to `pgrep` if `psutil` is unavailable

### Documentation
- Added event dict contract comment block above `handle_event()` in watcher.py — documents all optional keys (`_drop_types`, `_slayer_complete`, `_raw`, `_detail`, `_task_ctx`, `_lock_name`, etc.), their types, which events produce them, and which consumers rely on them
- Added TODO markers to `_process_lines()` and `_backfill_history()` noting intentional deferral of function-split refactoring

## v1.2.3
- Added automatic history dedup on backfill completion — after each account's backfill finishes, the history file is scanned for duplicate entries (same time, type, value, and activity) and duplicates are silently removed; a cleanup message is logged to the monitor if any are found
- This serves as a backup to the v1.2.2 EOF-pin fix and also cleans up any existing duplicates from previous sessions
- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned — the standalone activity update loop was overwriting the correct task name set by the slayer_task event when both "Getting new task" and "Slayer ->" lines arrived in the same 5-second poll chunk
- Added skip notification for tasks cancelled immediately as unsupported by the script (no Slayer -> assignment line) — fires as a slayer_skip event; task ID 126 is identified as Spiritual creatures, other unknown IDs show as "Unknown task (ID X)"

## v1.2.2
- Fixed history duplication when DreamBot closes before the monitor is stopped — on monitor exit, any active log file whose DreamBot client has already closed is now EOF-pinned before saving offsets, so backfill on next startup finds nothing new to re-read

## v1.2.1
- Fixed task and activity showing blank in status tab when monitor starts mid-session — the script_running guard was too aggressive; task is now always shown from startup scan regardless of script state
- Fixed status tab not updating to new Slayer task after completing — get_account_rows was calling _get_active_log_file (proc scan) on every call, causing race conditions with live state updates; rotation check now uses fast name-based file selection
- Added dedup to history writes — identical consecutive entries (same type, value, activity, timestamp) are now silently dropped, preventing duplicates from backfill/live overlap on monitor restart
- Added "Screenshot on monitor startup" toggle in Settings — defaults to off; previously the first scheduled screenshot always fired immediately on startup with no way to disable it

## v1.2.0
- Fixed stale task and activity showing in status tab on a new session — last task is now only carried over from startup scan when the script is confirmed running
- Fixed Slayer task complete double-firing (complete + unknown) — reverted to original single-anchor detection on the GAME message only; the script-only line fix was causing double events since the two lines have different timestamps
- Fixed teleport reason showing generic "No teleport to reach area" — now captures the specific teleport item from the log line (e.g. "No teleport to reach area: Fenkenstrain's castle teleport")

## v1.2.0-beta.4
- Fixed critical thread leak in status tab — duplicate `_auto_refresh` definition was spawning exponentially multiplying refresh chains, causing the monitor to become unresponsive after a few minutes
- Status tab is now push-based — watcher events push updates directly to the status tab instead of polling on a timer; tab selection and manual Refresh button trigger a full refresh
- Uptime and break time tick every 60 seconds via a lightweight local calculation (pure math, no I/O, no threads) — consistent with HH:MM display format
- `readlink /proc/*/fd/*` is called once per refresh cycle and reused across all accounts, further reducing overhead

## v1.2.0-beta.3
- Replaced `lsof +D` with `readlink /proc/*/fd/*` for all file handle detection — eliminates the startup lag and status tab slowness; the new approach reads kernel file descriptor symlinks directly instead of doing a full system-wide process scan
- Fixed status tab showing accounts as online after DreamBot closes without a clean stop — periodic refresh and manual refresh now re-check open file handles and immediately flip stale accounts Offline
- Fixed monitor picking the wrong log file when two clients were accidentally launched for the same account — active log identified by which file has an open handle, not filename order
- Added duplicate launch prevention in the CLI Launcher — attempting to launch an account that is already running shows an error instead of opening a second client

## v1.2.0-beta.2
- Fixed status tab showing accounts as online after DreamBot closes without writing a clean stop line — status tab refresh and tab-open now re-run lsof to detect dead sessions and immediately flip them Offline
- Fixed monitor picking the wrong log file when two DreamBot clients were accidentally launched for the same account — active log file is now identified by which file actually has an open file handle, not by filename order
- Active log file selection now uses lsof at startup instead of relying on filename sorting, ensuring the correct session is always tracked

## v1.2.0-beta.1

### Bug Fixes (reader.py)
- Fixed script start/stop/pause/resume events only emitting the first occurrence per session — all occurrences now fire correctly
- Fixed Slayer new task being deduplicated too aggressively — same monster assigned twice legitimately now both fire; dedup resets on every new task fetch
- Fixed Slayer task cancellation not detecting the monster name — backward search now scans up to 2000 lines, matching the real distance between assignment and cancellation
- Fixed Slayer complete missing tasks that only have a `Slayer task complete!` script line with no GAME message — now anchors on both lines with timestamp dedup to prevent double-firing
- Fixed pet drop showing as `Pet / Pet` when collection logs are enabled — the collection log line is now consumed by the pet event and suppressed from also firing as a separate collection event
- Fixed `Hunter - Birdhouses` being split into task=`Hunter` / activity=`Birdhouses` — added to the split exception list alongside `Questing`
- Fixed `QuestStep traversal failed` not being surfaced as the reason when a quest is abandoned — added to `_LOCK_REASON_PATTERNS`
- Fixed `Task: Break` appearing in the task column when the last `NEW TASK` block preceded a break — `slice_last_task` no longer returns `Break` as a task name

### New Features
- Added DreamBot CLI Launcher tab (between History and Settings) — save account launch presets and launch DreamBot instances directly from the monitor
  - Launcher.jar path selection saved to config
  - Per-account presets: account nickname, script, proxy nickname, memory (-Xmx), -covert, -nofresh, -fresh, -menuManipulation, -noClickWalk, world, custom args, params
  - Live command preview updates as fields are filled
  - Launch one account or select multiple and fire all at once
  - Delete confirmation dialog prevents accidental removal
  - Presets stored in config.json under `launcher_presets`

## v1.1.8
- Fixed status showing incorrect state after log rotation — logged_in, script_running and current task preserved across rotation-triggered recatchup
- Fixed break time lost on log rotation — startup catchup now scans full session file set in chronological order
- Fixed Task is X being emitted alongside Actually task is X in same timestamp block — Actually task is now correctly suppresses Task is
- Fixed monitor showing stale task, break time and uptime on startup when no DreamBot client is running — lsof used to detect active file handles; accounts with no active session start blank and show Offline immediately
- Fixed lsof active session detection — lsof returns exit code 1 even when finding files; check now relies on stdout content only
- Fixed script stop during break not showing Offline immediately — Stopped P2P Master AI! clears break state; Offline takes priority over On Break
- Fixed status stuck on Starting... when restarting script on already-logged-in account — NEW TASK correctly triggers Logged In when script is running
- Fixed NEW TASK incorrectly clearing On Break status
- Fixed uptime resetting on script restart — uptime tracks full DreamBot session from client start
- Fixed auto-updater not finding beta releases — semver parser now handles pre-release suffixes correctly; stable v1.1.8 correctly ranks above any v1.1.8-beta.x
- New account status state machine: Starting... / Logged In / On Break / Offline — removed stale Logged Out state
- Monitor waits up to 10 minutes for sessions if no account folders exist on startup
- Added py/__init__.py and ui/__init__.py to update manifest
- Removed dead code: _is_window_open, last_log_mtime, last_seen, last_seen_ts, break_expected_end, _local_ver()

## v1.2.4
### Reliability
- Added debug mode toggle in Settings → Debug — when enabled, internal errors that were previously swallowed silently are now logged to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Normalized all existing `[DEBUG]` log calls in watcher.py to use the new `_dbg()` helper so they respect the debug flag consistently
- Annotated intentionally silent catches in screenshot.py (paint comparison, file pruning) to distinguish "safe to ignore" from "should never fail"

### Launcher
- Replaced `pgrep -f account` duplicate-launch detection with `psutil` process inspection — checks for a `java` process with both `-jar` and the account name in its command line, eliminating false positives from unrelated processes; falls back to `pgrep` if `psutil` is unavailable

### Documentation
- Added event dict contract comment block above `handle_event()` in watcher.py — documents all optional keys (`_drop_types`, `_slayer_complete`, `_raw`, `_detail`, `_task_ctx`, `_lock_name`, etc.), their types, which events produce them, and which consumers rely on them
- Added TODO markers to `_process_lines()` and `_backfill_history()` noting intentional deferral of function-split refactoring

## v1.2.3
- Added automatic history dedup on backfill completion — after each account's backfill finishes, the history file is scanned for duplicate entries (same time, type, value, and activity) and duplicates are silently removed; a cleanup message is logged to the monitor if any are found
- This serves as a backup to the v1.2.2 EOF-pin fix and also cleans up any existing duplicates from previous sessions
- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned — the standalone activity update loop was overwriting the correct task name set by the slayer_task event when both "Getting new task" and "Slayer ->" lines arrived in the same 5-second poll chunk
- Added skip notification for tasks cancelled immediately as unsupported by the script (no Slayer -> assignment line) — fires as a slayer_skip event; task ID 126 is identified as Spiritual creatures, other unknown IDs show as "Unknown task (ID X)"

## v1.2.2
- Fixed history duplication when DreamBot closes before the monitor is stopped — on monitor exit, any active log file whose DreamBot client has already closed is now EOF-pinned before saving offsets, so backfill on next startup finds nothing new to re-read

## v1.2.1
- Fixed task and activity showing blank in status tab when monitor starts mid-session — the script_running guard was too aggressive; task is now always shown from startup scan regardless of script state
- Fixed status tab not updating to new Slayer task after completing — get_account_rows was calling _get_active_log_file (proc scan) on every call, causing race conditions with live state updates; rotation check now uses fast name-based file selection
- Added dedup to history writes — identical consecutive entries (same type, value, activity, timestamp) are now silently dropped, preventing duplicates from backfill/live overlap on monitor restart
- Added "Screenshot on monitor startup" toggle in Settings — defaults to off; previously the first scheduled screenshot always fired immediately on startup with no way to disable it

## v1.2.0
- Fixed stale task and activity showing in status tab on a new session — last task is now only carried over from startup scan when the script is confirmed running
- Fixed Slayer task complete double-firing (complete + unknown) — reverted to original single-anchor detection on the GAME message only; the script-only line fix was causing double events since the two lines have different timestamps
- Fixed teleport reason showing generic "No teleport to reach area" — now captures the specific teleport item from the log line (e.g. "No teleport to reach area: Fenkenstrain's castle teleport")

## v1.2.0-beta.4
- Fixed critical thread leak in status tab — duplicate `_auto_refresh` definition was spawning exponentially multiplying refresh chains, causing the monitor to become unresponsive after a few minutes
- Status tab is now push-based — watcher events push updates directly to the status tab instead of polling on a timer; tab selection and manual Refresh button trigger a full refresh
- Uptime and break time tick every 60 seconds via a lightweight local calculation (pure math, no I/O, no threads) — consistent with HH:MM display format
- `readlink /proc/*/fd/*` is called once per refresh cycle and reused across all accounts, further reducing overhead

## v1.2.0-beta.3
- Replaced `lsof +D` with `readlink /proc/*/fd/*` for all file handle detection — eliminates the startup lag and status tab slowness; the new approach reads kernel file descriptor symlinks directly instead of doing a full system-wide process scan
- Fixed status tab showing accounts as online after DreamBot closes without a clean stop — periodic refresh and manual refresh now re-check open file handles and immediately flip stale accounts Offline
- Fixed monitor picking the wrong log file when two clients were accidentally launched for the same account — active log identified by which file has an open handle, not filename order
- Added duplicate launch prevention in the CLI Launcher — attempting to launch an account that is already running shows an error instead of opening a second client

## v1.2.0-beta.2
- Fixed status tab showing accounts as online after DreamBot closes without writing a clean stop line — status tab refresh and tab-open now re-run lsof to detect dead sessions and immediately flip them Offline
- Fixed monitor picking the wrong log file when two DreamBot clients were accidentally launched for the same account — active log file is now identified by which file actually has an open file handle, not by filename order
- Active log file selection now uses lsof at startup instead of relying on filename sorting, ensuring the correct session is always tracked

## v1.2.0-beta.1

### Bug Fixes (reader.py)
- Fixed script start/stop/pause/resume events only emitting the first occurrence per session — all occurrences now fire correctly
- Fixed Slayer new task being deduplicated too aggressively — same monster assigned twice legitimately now both fire; dedup resets on every new task fetch
- Fixed Slayer task cancellation not detecting the monster name — backward search now scans up to 2000 lines, matching the real distance between assignment and cancellation
- Fixed Slayer complete missing tasks that only have a `Slayer task complete!` script line with no GAME message — now anchors on both lines with timestamp dedup to prevent double-firing
- Fixed pet drop showing as `Pet / Pet` when collection logs are enabled — the collection log line is now consumed by the pet event and suppressed from also firing as a separate collection event
- Fixed `Hunter - Birdhouses` being split into task=`Hunter` / activity=`Birdhouses` — added to the split exception list alongside `Questing`
- Fixed `QuestStep traversal failed` not being surfaced as the reason when a quest is abandoned — added to `_LOCK_REASON_PATTERNS`
- Fixed `Task: Break` appearing in the task column when the last `NEW TASK` block preceded a break — `slice_last_task` no longer returns `Break` as a task name

### New Features
- Added DreamBot CLI Launcher tab (between History and Settings) — save account launch presets and launch DreamBot instances directly from the monitor
  - Launcher.jar path selection saved to config
  - Per-account presets: account nickname, script, proxy nickname, memory (-Xmx), -covert, -nofresh, -fresh, -menuManipulation, -noClickWalk, world, custom args, params
  - Live command preview updates as fields are filled
  - Launch one account or select multiple and fire all at once
  - Delete confirmation dialog prevents accidental removal
  - Presets stored in config.json under `launcher_presets`

## v1.1.8
- Fixed status showing incorrect state after log rotation — logged_in, script_running, and current task preserved across rotation-triggered recatchup; state only reset on cold start
- Fixed break time being lost when log rotation occurred mid-session — startup catchup now scans full session file set (.log.1 + .log) in chronological order
- Fixed misleading "No task found in active log" warning appearing after every log rotation
- Fixed Task is X being posted alongside Actually task is X in same timestamp block — Actually task is now correctly suppresses Task is within the same second
- Fixed status staying on Starting... indefinitely when restarting script on an already-logged-in account — NEW TASK now correctly triggers Logged In when script is running
- Fixed NEW TASK incorrectly clearing On Break status — break state takes priority
- Fixed uptime resetting on script restart — uptime now tracks the full DreamBot session from client start, not individual script runs
- Fixed script stop during a break not showing Offline — Stopped P2P Master AI! now clears break state immediately; Offline takes priority over On Break in status logic
- Fixed monitor showing stale task, activity, break time and uptime on startup when no DreamBot client is running — lsof +D used to detect active file handles; accounts with no active session start with blank state and show Offline immediately
- Fixed lsof active session detection — lsof returns exit code 1 even when it finds open file handles; check now correctly relies on stdout content only
- Fixed idle wait triggering incorrectly when account folders exist but have no active session — idle wait only triggers when no account subfolders exist at all
- New account status state machine: 🟡 Starting... (script initializing), 🟢 Logged In (in game), 🔴 Offline (script stopped), 🟡 On Break (break active); removed stale Logged Out state
- Monitor no longer dies on start with no active sessions — shows waiting message and retries for up to 10 minutes
- Removed break_expected_end, _is_window_open, last_log_mtime, last_seen fields — replaced by script_running state and lsof active file detection
- Removed redundant _local_ver() method
- Added py/__init__.py and ui/__init__.py to update_manifest.txt
- Added debug logging to previously silent except blocks
- v1.1.8 stable correctly ranks above v1.1.8-beta.x in the auto-updater

## v1.1.8-beta.4
- Fixed lsof active session detection — lsof returns exit code 1 even when it successfully finds open file handles on this system; the check was incorrectly requiring exit code 0, causing all accounts to be treated as inactive on startup even when DreamBot was running; now correctly checks stdout content only
- This fix resolves: accounts incorrectly showing Offline on startup, log history being replayed as live events, false Script Started notifications, and Discord pings for old task changes on monitor start

## v1.1.8-beta.3
- Fixed version comparison not correctly ordering beta vs stable — _ver_tuple and _semver_key now use full semver-aware parsing: beta.1 < beta.2 < stable < next-stable; beta users are now correctly offered stable releases when promoted, and beta-to-beta upgrades work in order

## v1.2.4
### Reliability
- Added debug mode toggle in Settings → Debug — when enabled, internal errors that were previously swallowed silently are now logged to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Normalized all existing `[DEBUG]` log calls in watcher.py to use the new `_dbg()` helper so they respect the debug flag consistently
- Annotated intentionally silent catches in screenshot.py (paint comparison, file pruning) to distinguish "safe to ignore" from "should never fail"

### Launcher
- Replaced `pgrep -f account` duplicate-launch detection with `psutil` process inspection — checks for a `java` process with both `-jar` and the account name in its command line, eliminating false positives from unrelated processes; falls back to `pgrep` if `psutil` is unavailable

### Documentation
- Added event dict contract comment block above `handle_event()` in watcher.py — documents all optional keys (`_drop_types`, `_slayer_complete`, `_raw`, `_detail`, `_task_ctx`, `_lock_name`, etc.), their types, which events produce them, and which consumers rely on them
- Added TODO markers to `_process_lines()` and `_backfill_history()` noting intentional deferral of function-split refactoring

## v1.2.3
- Added automatic history dedup on backfill completion — after each account's backfill finishes, the history file is scanned for duplicate entries (same time, type, value, and activity) and duplicates are silently removed; a cleanup message is logged to the monitor if any are found
- This serves as a backup to the v1.2.2 EOF-pin fix and also cleans up any existing duplicates from previous sessions
- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned — the standalone activity update loop was overwriting the correct task name set by the slayer_task event when both "Getting new task" and "Slayer ->" lines arrived in the same 5-second poll chunk
- Added skip notification for tasks cancelled immediately as unsupported by the script (no Slayer -> assignment line) — fires as a slayer_skip event; task ID 126 is identified as Spiritual creatures, other unknown IDs show as "Unknown task (ID X)"

## v1.2.2
- Fixed history duplication when DreamBot closes before the monitor is stopped — on monitor exit, any active log file whose DreamBot client has already closed is now EOF-pinned before saving offsets, so backfill on next startup finds nothing new to re-read

## v1.2.1
- Fixed task and activity showing blank in status tab when monitor starts mid-session — the script_running guard was too aggressive; task is now always shown from startup scan regardless of script state
- Fixed status tab not updating to new Slayer task after completing — get_account_rows was calling _get_active_log_file (proc scan) on every call, causing race conditions with live state updates; rotation check now uses fast name-based file selection
- Added dedup to history writes — identical consecutive entries (same type, value, activity, timestamp) are now silently dropped, preventing duplicates from backfill/live overlap on monitor restart
- Added "Screenshot on monitor startup" toggle in Settings — defaults to off; previously the first scheduled screenshot always fired immediately on startup with no way to disable it

## v1.2.0
- Fixed stale task and activity showing in status tab on a new session — last task is now only carried over from startup scan when the script is confirmed running
- Fixed Slayer task complete double-firing (complete + unknown) — reverted to original single-anchor detection on the GAME message only; the script-only line fix was causing double events since the two lines have different timestamps
- Fixed teleport reason showing generic "No teleport to reach area" — now captures the specific teleport item from the log line (e.g. "No teleport to reach area: Fenkenstrain's castle teleport")

## v1.2.0-beta.4
- Fixed critical thread leak in status tab — duplicate `_auto_refresh` definition was spawning exponentially multiplying refresh chains, causing the monitor to become unresponsive after a few minutes
- Status tab is now push-based — watcher events push updates directly to the status tab instead of polling on a timer; tab selection and manual Refresh button trigger a full refresh
- Uptime and break time tick every 60 seconds via a lightweight local calculation (pure math, no I/O, no threads) — consistent with HH:MM display format
- `readlink /proc/*/fd/*` is called once per refresh cycle and reused across all accounts, further reducing overhead

## v1.2.0-beta.3
- Replaced `lsof +D` with `readlink /proc/*/fd/*` for all file handle detection — eliminates the startup lag and status tab slowness; the new approach reads kernel file descriptor symlinks directly instead of doing a full system-wide process scan
- Fixed status tab showing accounts as online after DreamBot closes without a clean stop — periodic refresh and manual refresh now re-check open file handles and immediately flip stale accounts Offline
- Fixed monitor picking the wrong log file when two clients were accidentally launched for the same account — active log identified by which file has an open handle, not filename order
- Added duplicate launch prevention in the CLI Launcher — attempting to launch an account that is already running shows an error instead of opening a second client

## v1.2.0-beta.2
- Fixed status tab showing accounts as online after DreamBot closes without writing a clean stop line — status tab refresh and tab-open now re-run lsof to detect dead sessions and immediately flip them Offline
- Fixed monitor picking the wrong log file when two DreamBot clients were accidentally launched for the same account — active log file is now identified by which file actually has an open file handle, not by filename order
- Active log file selection now uses lsof at startup instead of relying on filename sorting, ensuring the correct session is always tracked

## v1.2.0-beta.1

### Bug Fixes (reader.py)
- Fixed script start/stop/pause/resume events only emitting the first occurrence per session — all occurrences now fire correctly
- Fixed Slayer new task being deduplicated too aggressively — same monster assigned twice legitimately now both fire; dedup resets on every new task fetch
- Fixed Slayer task cancellation not detecting the monster name — backward search now scans up to 2000 lines, matching the real distance between assignment and cancellation
- Fixed Slayer complete missing tasks that only have a `Slayer task complete!` script line with no GAME message — now anchors on both lines with timestamp dedup to prevent double-firing
- Fixed pet drop showing as `Pet / Pet` when collection logs are enabled — the collection log line is now consumed by the pet event and suppressed from also firing as a separate collection event
- Fixed `Hunter - Birdhouses` being split into task=`Hunter` / activity=`Birdhouses` — added to the split exception list alongside `Questing`
- Fixed `QuestStep traversal failed` not being surfaced as the reason when a quest is abandoned — added to `_LOCK_REASON_PATTERNS`
- Fixed `Task: Break` appearing in the task column when the last `NEW TASK` block preceded a break — `slice_last_task` no longer returns `Break` as a task name

### New Features
- Added DreamBot CLI Launcher tab (between History and Settings) — save account launch presets and launch DreamBot instances directly from the monitor
  - Launcher.jar path selection saved to config
  - Per-account presets: account nickname, script, proxy nickname, memory (-Xmx), -covert, -nofresh, -fresh, -menuManipulation, -noClickWalk, world, custom args, params
  - Live command preview updates as fields are filled
  - Launch one account or select multiple and fire all at once
  - Delete confirmation dialog prevents accidental removal
  - Presets stored in config.json under `launcher_presets`

## v1.1.8
- Fixed status showing incorrect state after log rotation — logged_in, script_running and current task preserved across rotation-triggered recatchup
- Fixed break time lost on log rotation — startup catchup now scans full session file set in chronological order
- Fixed Task is X being emitted alongside Actually task is X in same timestamp block — Actually task is now correctly suppresses Task is
- Fixed monitor showing stale task, break time and uptime on startup when no DreamBot client is running — lsof used to detect active file handles; accounts with no active session start blank and show Offline immediately
- Fixed lsof active session detection — lsof returns exit code 1 even when finding files; check now relies on stdout content only
- Fixed script stop during break not showing Offline immediately — Stopped P2P Master AI! clears break state; Offline takes priority over On Break
- Fixed status stuck on Starting... when restarting script on already-logged-in account — NEW TASK correctly triggers Logged In when script is running
- Fixed NEW TASK incorrectly clearing On Break status
- Fixed uptime resetting on script restart — uptime tracks full DreamBot session from client start
- Fixed auto-updater not finding beta releases — semver parser now handles pre-release suffixes correctly; stable v1.1.8 correctly ranks above any v1.1.8-beta.x
- New account status state machine: Starting... / Logged In / On Break / Offline — removed stale Logged Out state
- Monitor waits up to 10 minutes for sessions if no account folders exist on startup
- Added py/__init__.py and ui/__init__.py to update manifest
- Removed dead code: _is_window_open, last_log_mtime, last_seen, last_seen_ts, break_expected_end, _local_ver()

## v1.2.4
### Reliability
- Added debug mode toggle in Settings → Debug — when enabled, internal errors that were previously swallowed silently are now logged to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Normalized all existing `[DEBUG]` log calls in watcher.py to use the new `_dbg()` helper so they respect the debug flag consistently
- Annotated intentionally silent catches in screenshot.py (paint comparison, file pruning) to distinguish "safe to ignore" from "should never fail"

### Launcher
- Replaced `pgrep -f account` duplicate-launch detection with `psutil` process inspection — checks for a `java` process with both `-jar` and the account name in its command line, eliminating false positives from unrelated processes; falls back to `pgrep` if `psutil` is unavailable

### Documentation
- Added event dict contract comment block above `handle_event()` in watcher.py — documents all optional keys (`_drop_types`, `_slayer_complete`, `_raw`, `_detail`, `_task_ctx`, `_lock_name`, etc.), their types, which events produce them, and which consumers rely on them
- Added TODO markers to `_process_lines()` and `_backfill_history()` noting intentional deferral of function-split refactoring

## v1.2.3
- Added automatic history dedup on backfill completion — after each account's backfill finishes, the history file is scanned for duplicate entries (same time, type, value, and activity) and duplicates are silently removed; a cleanup message is logged to the monitor if any are found
- This serves as a backup to the v1.2.2 EOF-pin fix and also cleans up any existing duplicates from previous sessions
- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned — the standalone activity update loop was overwriting the correct task name set by the slayer_task event when both "Getting new task" and "Slayer ->" lines arrived in the same 5-second poll chunk
- Added skip notification for tasks cancelled immediately as unsupported by the script (no Slayer -> assignment line) — fires as a slayer_skip event; task ID 126 is identified as Spiritual creatures, other unknown IDs show as "Unknown task (ID X)"

## v1.2.2
- Fixed history duplication when DreamBot closes before the monitor is stopped — on monitor exit, any active log file whose DreamBot client has already closed is now EOF-pinned before saving offsets, so backfill on next startup finds nothing new to re-read

## v1.2.1
- Fixed task and activity showing blank in status tab when monitor starts mid-session — the script_running guard was too aggressive; task is now always shown from startup scan regardless of script state
- Fixed status tab not updating to new Slayer task after completing — get_account_rows was calling _get_active_log_file (proc scan) on every call, causing race conditions with live state updates; rotation check now uses fast name-based file selection
- Added dedup to history writes — identical consecutive entries (same type, value, activity, timestamp) are now silently dropped, preventing duplicates from backfill/live overlap on monitor restart
- Added "Screenshot on monitor startup" toggle in Settings — defaults to off; previously the first scheduled screenshot always fired immediately on startup with no way to disable it

## v1.2.0
- Fixed stale task and activity showing in status tab on a new session — last task is now only carried over from startup scan when the script is confirmed running
- Fixed Slayer task complete double-firing (complete + unknown) — reverted to original single-anchor detection on the GAME message only; the script-only line fix was causing double events since the two lines have different timestamps
- Fixed teleport reason showing generic "No teleport to reach area" — now captures the specific teleport item from the log line (e.g. "No teleport to reach area: Fenkenstrain's castle teleport")

## v1.2.0-beta.4
- Fixed critical thread leak in status tab — duplicate `_auto_refresh` definition was spawning exponentially multiplying refresh chains, causing the monitor to become unresponsive after a few minutes
- Status tab is now push-based — watcher events push updates directly to the status tab instead of polling on a timer; tab selection and manual Refresh button trigger a full refresh
- Uptime and break time tick every 60 seconds via a lightweight local calculation (pure math, no I/O, no threads) — consistent with HH:MM display format
- `readlink /proc/*/fd/*` is called once per refresh cycle and reused across all accounts, further reducing overhead

## v1.2.0-beta.3
- Replaced `lsof +D` with `readlink /proc/*/fd/*` for all file handle detection — eliminates the startup lag and status tab slowness; the new approach reads kernel file descriptor symlinks directly instead of doing a full system-wide process scan
- Fixed status tab showing accounts as online after DreamBot closes without a clean stop — periodic refresh and manual refresh now re-check open file handles and immediately flip stale accounts Offline
- Fixed monitor picking the wrong log file when two clients were accidentally launched for the same account — active log identified by which file has an open handle, not filename order
- Added duplicate launch prevention in the CLI Launcher — attempting to launch an account that is already running shows an error instead of opening a second client

## v1.2.0-beta.2
- Fixed status tab showing accounts as online after DreamBot closes without writing a clean stop line — status tab refresh and tab-open now re-run lsof to detect dead sessions and immediately flip them Offline
- Fixed monitor picking the wrong log file when two DreamBot clients were accidentally launched for the same account — active log file is now identified by which file actually has an open file handle, not by filename order
- Active log file selection now uses lsof at startup instead of relying on filename sorting, ensuring the correct session is always tracked

## v1.2.0-beta.1

### Bug Fixes (reader.py)
- Fixed script start/stop/pause/resume events only emitting the first occurrence per session — all occurrences now fire correctly
- Fixed Slayer new task being deduplicated too aggressively — same monster assigned twice legitimately now both fire; dedup resets on every new task fetch
- Fixed Slayer task cancellation not detecting the monster name — backward search now scans up to 2000 lines, matching the real distance between assignment and cancellation
- Fixed Slayer complete missing tasks that only have a `Slayer task complete!` script line with no GAME message — now anchors on both lines with timestamp dedup to prevent double-firing
- Fixed pet drop showing as `Pet / Pet` when collection logs are enabled — the collection log line is now consumed by the pet event and suppressed from also firing as a separate collection event
- Fixed `Hunter - Birdhouses` being split into task=`Hunter` / activity=`Birdhouses` — added to the split exception list alongside `Questing`
- Fixed `QuestStep traversal failed` not being surfaced as the reason when a quest is abandoned — added to `_LOCK_REASON_PATTERNS`
- Fixed `Task: Break` appearing in the task column when the last `NEW TASK` block preceded a break — `slice_last_task` no longer returns `Break` as a task name

### New Features
- Added DreamBot CLI Launcher tab (between History and Settings) — save account launch presets and launch DreamBot instances directly from the monitor
  - Launcher.jar path selection saved to config
  - Per-account presets: account nickname, script, proxy nickname, memory (-Xmx), -covert, -nofresh, -fresh, -menuManipulation, -noClickWalk, world, custom args, params
  - Live command preview updates as fields are filled
  - Launch one account or select multiple and fire all at once
  - Delete confirmation dialog prevents accidental removal
  - Presets stored in config.json under `launcher_presets`

## v1.1.8
- Fixed status showing incorrect state after log rotation — logged_in, script_running, and current task preserved across rotation-triggered recatchup; state only reset on cold start
- Fixed break time being lost when log rotation occurred mid-session — startup catchup now scans full session file set (.log.1 + .log) in chronological order
- Fixed misleading "No task found in active log" warning appearing after every log rotation
- Fixed Task is X being posted alongside Actually task is X in same timestamp block — Actually task is now correctly suppresses Task is within the same second
- Fixed status staying on Starting... indefinitely when restarting script on an already-logged-in account — NEW TASK now correctly triggers Logged In when script is running
- Fixed NEW TASK incorrectly clearing On Break status — break state takes priority
- Fixed uptime resetting on script restart — uptime now tracks the full DreamBot session from client start, not individual script runs
- Fixed script stop during a break not showing Offline — Stopped P2P Master AI! now clears break state immediately; Offline takes priority over On Break in status logic
- Fixed monitor showing stale task, activity, break time and uptime on startup when no DreamBot client is running — lsof +D used to detect active file handles; accounts with no active session start with blank state and show Offline immediately
- Fixed lsof active session detection — lsof returns exit code 1 even when it finds open file handles; check now correctly relies on stdout content only
- Fixed idle wait triggering incorrectly when account folders exist but have no active session — idle wait only triggers when no account subfolders exist at all
- New account status state machine: 🟡 Starting... (script initializing), 🟢 Logged In (in game), 🔴 Offline (script stopped), 🟡 On Break (break active); removed stale Logged Out state
- Monitor no longer dies on start with no active sessions — shows waiting message and retries for up to 10 minutes
- Removed break_expected_end, _is_window_open, last_log_mtime, last_seen fields — replaced by script_running state and lsof active file detection
- Removed redundant _local_ver() method
- Added py/__init__.py and ui/__init__.py to update_manifest.txt
- Added debug logging to previously silent except blocks
- v1.1.8 stable correctly ranks above v1.1.8-beta.x in the auto-updater

## v1.1.8-beta.4
- Fixed lsof active session detection — lsof returns exit code 1 even when it successfully finds open file handles on this system; the check was incorrectly requiring exit code 0, causing all accounts to be treated as inactive on startup even when DreamBot was running; now correctly checks stdout content only
- This fix resolves: accounts incorrectly showing Offline on startup, log history being replayed as live events, false Script Started notifications, and Discord pings for old task changes on monitor start

## v1.1.8-beta.3
- Fixed monitor showing stale task, activity, break time and uptime on startup when no DreamBot client is running — accounts with no active log file handle (detected via lsof) now start with a completely blank state and show Offline immediately
- Fixed idle wait triggering incorrectly when account folders exist but have no active session — idle wait now only triggers when no account subfolders exist at all; stale accounts show Offline immediately without waiting
- Fixed script stop during break not showing Offline — Stopped P2P Master AI! now clears on_break and _break_start_ts immediately; Offline takes priority over On Break in status logic
- Fixed status staying on Starting... indefinitely when restarting script on an already-logged-in account — NEW TASK now correctly triggers Logged In when script is running
- Fixed NEW TASK incorrectly clearing On Break status — break state takes priority
- Fixed uptime resetting on script restart — uptime now tracks the full DreamBot session from client start, not individual script runs
- Removed redundant _local_ver() method — version read directly from VERSION constant
- v1.1.8 stable will correctly rank above v1.1.8-beta.x in the auto-updater

## v1.1.8-beta.2
- Fixed status staying on Starting... indefinitely when restarting script on an already-logged-in account — NEW TASK now correctly triggers Logged In when script_running is True (was broken by a case mismatch: 'new task' in b.upper() never matched)
- Fixed NEW TASK incorrectly clearing on_break — break state now takes priority; NEW TASK only updates logged_in when not currently on a break
- Fixed uptime resetting on script restart — script_start_ts is now only set from the DreamBot client start time (Connecting to server) and is never overwritten by subsequent Starting P2P Master AI now! lines; uptime now tracks the full DreamBot session regardless of script restarts

## v1.1.8-beta.1
- Fixed status showing Logged Out after log rotation — startup catchup now preserves logged_in, script_running, last_task, and last_activity on rotation-triggered recatchup; state is only reset on cold start
- Fixed break time and task being lost when log rotates mid-session — startup catchup now scans full session file set (.log.1 + .log) in chronological order
- Fixed "No task found in active log" warning firing incorrectly after log rotation — task lookup is now suppressed on rotation-triggered catchup since new log starts empty
- Fixed Task is X being emitted alongside Actually task is X in same timestamp block — slice_tasks now suppresses Task is if Actually task is appears within the same second/15-line window
- New account status state machine: 🟡 Starting... (script initializing), 🟢 Logged In (Solvers all finished), 🔴 Offline (script stopped or no script running), 🟡 On Break (break active); removed stale Logged Out state
- Idle wait on startup: if no active log sessions are found, monitor shows Waiting for active sessions... and retries every 5 seconds for up to 10 minutes before stopping with an alert
- Removed break_expected_end from AccountState — field was set but never consumed; removed all 4 set/clear references
- Added debug logging to 7 previously silent except blocks — startup catchup errors, break timestamp parse failures, file offset failures, mtime read failures, and summary time parse failures now appear in the monitor log
- Added py/__init__.py and ui/__init__.py to update_manifest.txt so auto-updates include package marker files

## v1.1.7
- Fixed root cause of on_break never being set correctly — backwards scan was stopping at a previous completed 'Break over N' line before reaching the current BREAK START; replaced with a forward scan that tracks the last unmatched BREAK START, which is the correct algorithm
- Added _is_break_start() and _is_break_over() helper functions — used consistently in startup scan, session file scan, and live poll loop
- Fixed logged_in detection in startup scan — was incorrectly set as 'not on_break'; now tracked properly through logged in/out/break events
- All previous v1.1.6 fixes remain: _startup_done guard, status priority (on_break before Offline), _break_start_ts seeded once from log timestamp, logout no longer starts break timer

## v1.1.7
- Fixed break detection — backwards scan was replaced with forward scan to correctly find the last unmatched BREAK START; backwards scan failed when a completed 'Break over N' line appeared after the last BREAK START (i.e. a prior completed break existed in the same log)
- Fixed case bug in forward scan — 'break start' was checked against line.upper() which never matched; corrected to 'BREAK START'
- Uptime now shows during breaks regardless of window_open state

## v1.1.6
- Fixed root cause of break time not accumulating and status flipping between Offline/On Break — _startup_catchup was being called on every status refresh, resetting _break_start_ts and total_break_secs each time
- Added _startup_done flag to AccountState — _startup_catchup now runs only once per state object; session file rotation still triggers it correctly
- Fixed status priority — on_break now checked before window_open so accounts on break always show On Break, never Offline
- Fixed _break_start_ts seeding — set once from the parsed log timestamp (break_start_log_ts) so break timer starts from actual break start, not monitor startup; not overwritten on subsequent calls
- Removed interacting (widget) logout from break timing — normal logout no longer starts the break timer

## v1.1.5
- Fixed root cause of account showing Offline during a break — backwards scan was treating 'Break over -> Startup' (a skip notification) as a completed break; fixed in backwards scan, live poll loop, and timestamp pair math to only match 'Break over N' (with a number)
- Fixed break time overwrite — completed break total from session files was overwriting the current in-progress elapsed time; now sets total_break_secs from completed breaks first, then adds current break elapsed on top
- Fixed BREAK START forward scan finding first instead of last — break_expected_end was calculated from an old break; now always uses the last BREAK START in the file
- Status tab auto-refreshes every 30 seconds so uptime and break time tick live
- Screenshots now wait up to 60 seconds for Discord gateway before bot delivery; webhook delivery unaffected
- Auto-updater now uses update_manifest.txt from the release zip to determine which files to apply — no more hardcoded file list; add new files to the manifest and they ship automatically
- Update apply is now staged — files are extracted to a temp dir on the same filesystem, manifest is verified, then files are copied to install dir; staging dir always cleaned up on success or failure
- Release selection now sorts by parsed semver instead of published_at date
- Zip asset selection now prefers P2P-Monitor-*.zip by name prefix before falling back to any .zip
- _local_ver() now reads the VERSION constant directly instead of scraping file text

## v1.1.4
- Fixed account showing Offline during a break when the log has multiple break sessions — startup catchup was finding the first BREAK START instead of the last
- Fixed screenshots failing before the Discord gateway connects — bot screenshots now wait up to 60s for the gateway; webhook screenshots unaffected

## v1.1.3
- Fixed silent startup update check passing boolean False as asset URL when user accepted the update prompt — caused "unknown url type: 'False'" download error

## v1.1.2
- Fixed auto-updater incorrectly extracting all files flat into root install directory instead of preserving py/ and ui/ subfolder structure

## v1.1.1
- Fixed break time calculation — now uses BREAK START / Break over timestamps instead of the logged ms value; DreamBot logs -100ms for manually skipped breaks which was corrupting the total

## v1.1.0
- Auto-updater now uses GitHub Releases — downloads full release zip, applies only changed files, cleans up after itself
- Added beta opt-in checkbox in Settings — manual update checks include pre-release versions when enabled; silent startup check always uses stable releases only
- Fixed break time not accumulating correctly after monitor restart
- Fixed paint overlay incorrectly hiding on startup screenshots when hide option is unchecked — caused by window not being fully rendered before button state was read; now waits for window to settle and verifies state after each click
- Added CHANGELOG.md, LICENSE (GPL v3), .gitignore

## v1.0.0
- Initial public release
