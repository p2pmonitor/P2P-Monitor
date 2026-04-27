# Changelog

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
