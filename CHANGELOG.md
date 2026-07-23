# Changelog

## v2.2.0

### Added

**1. Ban detection.**
Two DreamBot ban signatures are now detected ("Account is being set to
banned status" and "High severity server response ... Response: DISABLED").
When either appears, the account's status shows 🔨 Banned instead of
Offline, a ping is sent to the account's monitor thread, and the event is
recorded in history and the Event Log. A successful login clears the state
(so 2-day temporary bans self-clear when the account returns), and it is
reconstructed during startup catch-up so it survives monitor restarts.
Banned accounts are excluded from auto-restart and update auto-relaunch.

**2. Live "Last 99 Achieved" updates.**
Stats → Goals & Maxing now receives 99s live from the event pipeline.
Previously the page ran its history scan once at first build, so a new 99
only appeared after a monitor restart.

### Fixed

**3. Window position restore after relaunch failed roughly half the time.**
The post-relaunch discovery poll gave up after a fixed 30 seconds, but slow
launcher/client boots (proxy delays, jav_config retries) routinely take
longer — and on timeout the launcher's own Popen PID was cached as a
fallback, which poisoned the PID-first screenshot lookup with a non-client
PID. Discovery now waits up to 120s, then keeps watching at low frequency
(every 30s, up to 30 minutes) and completes the PID cache + position
restore when the window finally appears. The Popen-PID fallback is removed.

**4. Missing screenshots now retry once and always explain themselves.**
Event pings arriving without their image are the capture-failure fallback
path. Capture now gets one quick in-place retry (1.5s), and when it still
fails the reason is attached to the fallback embed as a footer
("📷 Screenshot failed: …") and logged to the monitor tab (throttled,
one line per account per 10 min) — previously the reason was debug-only.
Rate-limited (HTTP 429) screenshot posts now honor Discord's retry_after
and retry once instead of being dropped.

**5. Group/clan broadcasts no longer trigger quest pings (GitHub issue #3).**
"<player> has completed a quest: X" from group chat matched the loose
'completed a quest' substring and fired a quest-complete ping for the wrong
player. Quest completion/start and level/total-level patterns are now
anchored to the first-person "[GAME] Congratulations, you've…" form, so
other players' broadcasts (and player-typed chat) can never match.

**6. Slash commands could stay unregistered after first bot setup.**
First-time setup drains the Discord API budget on channel/thread creation
right before command registration; registration retries capped each wait at
30s even when Discord asked for longer, then gave up permanently until the
next monitor start. Waits now honor retry_after (up to 15 min per wait),
and after a rate-limited give-up the monitor re-arms registration in the
background (every 5 minutes, up to 12 attempts) instead of waiting for a
restart.

### Carried from unreleased v2.1.4


**7. Monitor loop could die silently, stopping events and scheduled screenshots.**
The main monitor thread ran all periodic checks (screenshots, daily summary,
update awareness, pruning, status refresh) inline with no exception guard.
One unhandled exception anywhere in that chain — a network hiccup during a
Discord post, an odd window title during an update check — killed the thread
permanently and silently. The failure was fully masked: the status tab
(UI-driven), the gateway bot, and the screenshot worker run on separate
threads, so accounts still showed green and on-demand /ss still worked while
scheduled screenshots and event processing were dead. Observed in the field
after 100+ hours of uptime; a restart "fixed" it. The loop body is now
guarded: each periodic check catches its own exceptions (logged with full
traceback in debug mode, one-line notice in the monitor log) and an outer
guard protects the whole cycle. One bad cycle can no longer kill the monitor.

**8. Scheduled-screenshot skips are no longer silent (debug mode).**
Every gate that skips a scheduled screenshot (account on break, offline,
muted, screenshots disabled, startup screenshot disabled, enqueue refused)
previously dropped the request without a trace, making "screenshots stopped,
no errors anywhere" undiagnosable. Each skip reason is now logged in debug
mode, throttled to one line per account per 10 minutes. Minor behavior
change: a muted account no longer has its schedule timestamp advanced, so it
becomes due immediately on unmute (matching on-break/offline semantics).

**9. 24-hour heartbeat.**
The monitor log now gets one line per day ("Monitor loop alive — N
account(s), up …") so a dead loop is diagnosable at a glance: if the last
heartbeat is older than a day, the loop stopped then. Logged regardless of
debug mode — one line per day, works when debug is off (which is when it
will matter).

## v2.1.3

### Fixed

**1. Slash commands re-registered on every monitor start.**
The _slash_commands_hash fingerprint (added in v2.1.1 so unchanged command
sets skip registration) was being deleted at every startup by
sanitize_config's unknown-key pruning — it was never added to DEFAULT_CFG,
so each launch re-synced all commands ("Slash command registration complete
— 9 commands synced" on every start). Harmless in practice (a single
rate-limit-safe bulk PUT), but it defeated the skip-when-unchanged design.
The key is now declared in DEFAULT_CFG; after one successful sync, later
starts log "Slash commands unchanged — skipping registration". Verified by
a sanitize_config regression test: the hash survives, junk keys are still
pruned.

**2. Multi-type drops merge again (Valuable + Collection).**
A single drop that is both a valuable drop and a collection-log entry was
producing two separate events — two Event Log rows and two Discord pings —
instead of one "Drop (Valuable + Collection)". Cause: the drop merger keyed
on the raw item text, and DreamBot writes the same item differently per
line ("Eternal gem (7,480,942 coins)" on the valuable line, "Eternal gem"
on the collection line), so the keys never matched. This has been the case
since the merger was written — Untradeable + Collection pairs merged fine
because their names are identical, which is why it appeared to have
"worked before". The grouping key now strips the trailing "(N coins)"
suffix and case for matching; the displayed item keeps the coin-valued
form, and the merged label renders as "Valuable + Collection". Items whose
names legitimately contain parentheses are unaffected (suffix must match
"(N coins)" exactly), and the pet/collection consumption path is unchanged.

**3. Backfill checkpoint writes consolidated.**
Each backfill checkpoint previously wrote offsets.json twice back-to-back
(plain marker + structured meta). set_last_seen now accepts optional
file_key/line_index and writes both in a single merge-only pass;
set_last_seen_meta was removed. Behavior is byte-for-byte equivalent —
same keys, same values, same self-invalidation on live-loop updates — and
the full v2.1.2 backfill test suite (idempotency, skip-proof resume,
stale-meta fallback) passes unchanged.

### Removed (dead code — full audit sweep, all verified zero callers)

- launcher.smart_launch and launcher.relaunch_all — orphaned when v2.1.0
  routed /relaunch through the RelaunchManager; relaunch_all's only
  remaining references were its own log strings, and smart_launch was only
  called by relaunch_all. A stale launcher_tab docstring reference was
  updated.
- The scanned-logs mechanism (history.record_log_scanned,
  history.get_scanned_logs, and the rotation-handler call that fed it) —
  write-only state nothing ever read. Existing scanned entries in
  offsets.json are simply ignored.
- watcher._base_log_name — its sole caller was the removed
  record_log_scanned block.
- launcher.validate_account_pid, paint.click_at_offset,
  platform_ops.is_account_process_running, platform_ops.get_process_cmdline,
  util.get_window_geom (plus its module-docstring mention) — zero callers.
- The unused psutil import fallback block in watcher.py (neither _psutil
  nor _PSUTIL_AVAILABLE was ever referenced).
- Unused locals: EnumDisplayMonitors callback params renamed to
  _hmon/_hdc (signature is ctypes-required, values unused); the unused
  force_full parameter removed from HistoryTab.load().

The intentional availability probes (import discord in the two _ensure
checks, PIL ImageGrab/ImageChops on the Windows screenshot path) are NOT
dead code and were left in place.

### Changed

**Launcher tab Relaunch dialog labeled as a manual override.** The
already-running dialog now states that Relaunch here closes and restarts
immediately, ignoring Respect Break, and points to Discord /relaunch for
break-aware queuing. (A Queue-for-Break option in this dialog is a
candidate for a future minor release.)

### Files changed
p2p_monitor.py, py/watcher.py, py/history.py, py/launcher.py,
py/platform_ops.py, py/paint.py, py/reader.py, py/util.py,
ui/launcher_tab.py, ui/history_tab.py, CHANGELOG.md

## v2.1.2

### Fixed

**1. Startup history duplication (append-then-dedupe on every restart).**
Root cause: the backfill last-seen marker was persisted only once, after the
ENTIRE backfill finished — a monitor restart mid-backfill (large rotated logs
take a while) left the old marker in place, so the next startup replayed the
whole span and _dedup_history_file removed the copies afterward. Fixes, all
in _backfill_history:
- Incremental marker persistence — last_seen is written after every file and
  every ~2,500 lines within a file, so an interrupted backfill resumes where
  it stopped instead of replaying.
- Idempotent appends — existing history keys (time+type+value+activity) are
  preloaded and any already-present event (normal or Inferno) is skipped
  before append. Dedupe is now a rare repair net, not the startup path.
- Single-flight guard — one backfill per account per session; a concurrent
  duplicate spawn returns immediately.
- Last-occurrence marker matching — DreamBot logs contain many exact
  duplicate lines within one file (5,000+ observed); first-occurrence
  matching could rewind the marker and replay the span in between.
- Structured checkpoint with skip-proof resume — every marker persist also
  writes a structured checkpoint (marker line + file identity + absolute
  line index) to offsets.json. Resume uses it for an exact restart position;
  if it's stale (e.g. the live loop moved the plain marker, which writes no
  checkpoint) resume falls back to FIRST-occurrence text matching. Direction
  of safety: a replay is a harmless no-op under the idempotent preload, but
  a skip loses events forever — so resume can land at-or-before the true
  checkpoint, never after it. In particular, a mid-file checkpoint whose
  exact line text repeats later in the same file (thousands of duplicate
  lines observed in real logs) can no longer jump forward past unprocessed
  events.
- Structured 'backfill' diagnostics in debug.jsonl per run: marker
  present/found and in which file, resume mode (checkpoint vs
  first-occurrence), first file/line processed, entries appended per file,
  entries skipped as already-existing, dupes removed.

**2. Settings → Event Notifications flash on Linux (root-caused and fixed).**
Instrumentation showed every settings page takes a full X11 Expose repaint
storm on tkraise — each classic Tk widget is its own X window repainting
individually — and Event Notifications was simply the heaviest page (94
widget windows vs 50–69), making its bottom-up progressive paint visible.
The three checkbox groups (script events row, event-type matrix, hide-paint
grid) are now drawn on three Canvas widgets on Linux — one X window each
instead of ~60 — same layout, labels, dark theme, and indicator style,
with hover highlight, hand cursor, and accent checkmark; the page dropped
to 35 widget windows, the lightest in Settings. The canvases drive the same
BooleanVars registered in _vars, so save()/load_fields()/config keys are
completely unchanged. Windows keeps the native checkbutton widgets
untouched — it repaints them invisibly fast and should not change
appearance to fix a Linux-only issue.

**3. Event Log wording — badges carry the category, messages drop the
redundant prefix.** Display-only: History entries, Discord embeds, and
parser behavior are unchanged.
- "New Slayer task: 53 Suqah" → SLAYER badge, "New Task: 53 Suqah" (was
  mislabeled SYSTEM — the 🗡 prefix was grouped with heartbeat lines in the
  classifier; it now has its own slayer_task tag)
- "Slayer complete: X — pts" → "Task complete: X — pts" (classifier keys
  updated in lockstep; relaunch-success ✅ lines are unaffected)
- "Slayer skipped: X" → "Skipped: X"
- "Task: Farming / (H) Ranarr" → "Farming / (H) Ranarr"
- "Level up: Fishing → 93" → "Fishing → 93" (Total Level milestones render
  as "Total Level → 2125")
- "Quest started/completed: X" → "Started: X" / "Completed: X"
- Event Log filter: SLAYER rows (new task / task complete / skipped) now
  belong to the Tasks filter category instead of falling through to Other.

### Files changed
p2p_monitor.py, py/watcher.py, py/history.py, ui/monitor_tab.py,
ui/settings_tab.py, CHANGELOG.md

## v2.1.1

### Fixed

**1. Slash command registration no longer fails on Discord rate limits.**
Startup could leave commands (/max, /wom, /train, /relaunch, ...) permanently
unregistered when Discord returned HTTP 429 during the one-POST-per-command
registration loop — each 429 was logged as a hard failure and the loop kept
hammering the remaining commands. Registration is now a single bulk-overwrite
call (PUT .../guilds/{guild}/commands with the full command array), which
syncs the whole set atomically in one request and removes stale commands in
the same call. On HTTP 429 the retry_after value is parsed from the response,
slept out (plus 0.25-0.75s jitter, capped at 30s per wait), and the same
request is retried — bounded at 6 attempts, never a rapid loop, with clear
log lines ("Rate limited registering slash commands — retrying in 1.9s",
"Slash command registration complete"). A SHA-256 fingerprint of the
registered set (app id + guild id + full command JSON) is persisted in config
(_slash_commands_hash) after a successful sync; when nothing has changed,
subsequent monitor starts and internal bot reconnects skip registration
entirely instead of re-registering an unchanged set. Non-429 errors (e.g.
missing permissions) still fail immediately with the real error, and the
fingerprint is only saved on success so the next start retries.

**2. Event Log column spacing.**
EVENT sits closer to ACCOUNT and MESSAGE starts further left (tab stops
88/215/305 → 88/192/258), giving the message column ~47px more room.

**3. Event Log separator lines.**
The '=' * 60 session divider wrapped into two lines under the new column
layout. Separator-only messages now render as a single short muted rule with
no dot/time/badge columns.

### Files changed
p2p_monitor.py, py/discord.py, py/watcher.py, ui/monitor_tab.py,
CHANGELOG.md

## v2.1.0

### New features

**1. Skip level notifications below N (Settings → Event Notifications).**
New setting directly under "Notify every N levels". Default 1 preserves
current behavior; setting e.g. 50 suppresses Discord level-up messages for
levels 1–49, including the startup catch-up relay. Suppression is
Discord-only — the level-up is still logged to the Event Log and recorded in
History (implemented as a `_suppress_discord` annotation consumed by the
Discord dispatch leg in `handle_event()`, never a dropped event). Level 99
and Total Level milestones are never suppressed; an unparsed level preserves
existing behavior rather than incorrectly suppressing. Key:
`levelup_skip_below`.

**2. New Discord slash commands, restructured to fit Discord's 25-choice
limit.**
- `/force <account> <action> [amount]` now offers only its six non-skill
  actions (Stats, Loot, -10m, +10m, Skip, Quest) as fixed, always-visible
  choices instead of autocomplete.
- `/train <account> <skill>` — the 23 skill options previously buried in
  `/force`, moved verbatim to their own command. Same backend
  (`on_force_skill`), same strings the script expects; no functionality
  added or removed.
- `/stats <account> <view>` — `current` returns all 24 skill levels + total
  level in a code block; a skill name returns estimated time to 99 for that
  skill (e.g. "Estimated time to 99: ~16h"). 25 choices exactly.
- `/max <account>` — estimated time to max plus the closest 99, using the
  same `py/wom.py` calculations the Stats tab uses.
- `/wom refresh <account|All>` — calls the same
  `wom.refresh_account_in_cache()` backend as the Refresh WOM button, with a
  shared in-flight lock and a 60s per-account cooldown so repeated calls
  cannot stack or hammer the WOM API. All of these read/write the WOM cache
  directly — no Tk dependency in the Discord path.

**3. Relaunch safeguard system (new `py/relaunch.py`, RelaunchManager).**
All `/relaunch` requests now flow through one coordinator:
- Startup confirmation: an attempt only counts as successful when the
  watcher detects the script's start line — a spawned process is never
  treated as success.
- Retry/backoff: unconfirmed attempts retry at 5, 10, 20, 30, then 60-minute
  (cap) intervals, with Discord + monitor notifications on each failure and
  on eventual success. A Script Started from any source (manual start,
  auto-restart) clears the pending state.
- Sequential worker: exactly one launch/confirmation attempt at a time;
  accounts waiting out a retry delay or a break window never occupy the
  worker, so one stuck account can't block the rest of a `/relaunch all`.
- Persistence: pending relaunch/retry state — including the absolute
  `resume_at` timestamp of an armed break-end or retry timer — is saved to
  `~/.p2p_monitor/pending_relaunches.json`. On the next start, a future
  `resume_at` re-arms a timer for exactly the remaining delay (a restart
  during a 30-minute backoff waits out the remainder, it does not attempt
  immediately); an entry without usable timing waits ~30 seconds for
  startup catch-up to reconstruct live break state, then routes through the
  normal request logic — so a restart during a break can never relaunch
  mid-break.
- Respect Break correctness: break state is evaluated before the
  running-client check, so an account that is on break with its client
  already closed waits for the break's end instead of launching
  immediately mid-break.
- Auto-restart integration: once the existing auto-restart gates
  (manual-stop detection, game-update window, suppress window,
  respect-break delay — all unchanged) allow a restart, the actual launch
  now routes through the RelaunchManager too, so auto-restarts get the same
  confirmation, retry/backoff, persisted state, geometry restore, and
  one-at-a-time safety. Falls back to the previous direct launcher call if
  the manager is unavailable.
- Shutdown safety: stopping the monitor while a queued relaunch is waiting
  on break-length parsing aborts before the destructive close — a stopped
  monitor never closes a client afterward.
- Process safety unchanged: ownership always validated via window title
  before terminating; never kills by generic process name.
- Duplicate-launch guard: if window discovery finds nothing but the
  account's saved PID is still alive (observed in the field as a transient
  discovery failure against a genuinely running client), the manager
  neither closes by PID (killing blind) nor launches a second client for
  the same account. It dumps the visible DreamBot window titles to
  debug.jsonl, notifies clearly, and retries on the normal backoff — a
  transient failure self-heals on the next attempt instead of leaving the
  account offline or duplicated.

**3b. Linux window matching: case-insensitive, with ownership guard and
refusal diagnostics.**
`xdotool search --name` patterns are case-sensitive POSIX regexes, and the
matcher lowercased only the search needle — so any DreamBot title showing
the account name with capital letters silently failed discovery on Linux
(a latent failure feeding screenshots, relaunch, and auto-restart alike).
The pattern is now built per-character (`[aA][bB]…`) with regex
metacharacters escaped, making matching case-insensitive and safe for
account names containing regex specials. Each candidate window's actual
title is then verified via `xdotool getwindowname` and must also contain
"dreambot" (case-insensitive) — parity with the Windows matcher's existing
guard, so a terminal or editor whose title merely mentions an account name
can never be matched or terminated. Additionally, when a relaunch refuses
because a saved PID is alive but no window matched, the titles of every
visible DreamBot window at that moment are written to `debug.jsonl`
(`launcher_dpi`/`launcher` category) so a false-negative match is provable
from the log instead of guessed at.

### Fixed

**4. `/relaunch` now honors Respect Break before closing a running client.**
Previously the respect-breaks setting only applied in the auto-restart path
(script already stopped); `/relaunch` against a live client restarted it
immediately. Now, with Respect Break enabled: a running, not-on-break
account queues — the client is closed at its next break start, stays closed
for the break, and relaunches at the break's end (mirroring
`_compute_restart_delay`'s snapshot behavior); an account already on break
is closed now and relaunched at break end; if the break length never parses,
falls back to the configured random restart delay. With Respect Break
disabled, `/relaunch` restarts immediately with no break checks or queue.
Each account in `/relaunch all` follows its own independent break window.

**5. Client window position persisted per account
(`~/.p2p_monitor/window_geometry.json`).**
A delayed relaunch (break-end, retry, or after the monitor itself
restarted) previously launched the client at the default center position
because the captured geometry only lived in memory. Geometry is now
persisted at capture-before-close, on a ~2-minute background sweep of
running clients (so manual window moves are remembered), and best-effort at
monitor shutdown. Fresh launches and delayed relaunches restore from the
persisted value; a live capture always wins when present. Saved geometry is
re-validated on read (existing `_geometry_is_sane` checks) — invalid or
off-range data falls back to default placement, and a good saved value is
never overwritten with null/empty data.

**6. Event Log rebuilt to the intended column/badge design.**
The Monitor tab's Event Log now renders as TIME / ACCOUNT / EVENT / MESSAGE
columns with a status dot per row and colored event badges (SYSTEM, TASK,
LEVEL UP, ERROR, …), replacing the raw terminal-style text lines. Still a
single `tk.Text` under the hood — pixel tab stops for columns, background
tags for badges, `lmargin2` so wrapped messages stay aligned under the
MESSAGE column — so the existing 2000-line prune, elide-based category
filter, and debounced search all work unchanged. Leading emoji are dropped
from messages (the badge carries the type) and the `[account]` token becomes
the ACCOUNT column.

**7. Stats donut: 'Other' excluded from the ring.**
The bucketed "Other (17 skills)" slice dominated the donut and drowned out
the top skills the chart exists to show. Wedges now cover only the named
top skills; Other remains visible as its own row in the skill-bar list, the
center total still counts it (the total is real, only the slice is hidden),
and a small "top skills" caption sits under TOTAL when the exclusion is
active.

**8. History tab: Severity visible by default; single scrollbar.**
Two fixes: (a) column widths — the persisted `hist_col_widths` restore
re-applied the Activity/Details column's *rendered* stretched width from a
wide window as a fixed request, pushing Severity off-screen; the stretch
column is no longer restored from saved values, fixed columns are clamped
to sane ranges, all columns got minwidths, and the defaults were resized so
all five columns fit the 960px minimum window width. (b) the per-account
tree no longer has its own vertical scrollbar or 18-row height cap — each
tree renders full-height and the History page scrolls as one surface via
the outer canvas; wheel events over a tree are routed to that canvas
(with 'break') so nothing double-scrolls.

**9. Settings (Linux): Event Notifications flash + white outline boxes.**
The `_scrollable_body` sync handler recomputed the scroll region on every
child `<Configure>` — ~35 checkbuttons' worth of layout passes when the
page was raised, a visible blank-flash on Linux. It now no-ops unless the
measured content/viewport heights actually changed. The "white outlined
boxes" were Linux Tk's default focus-highlight rings (light
highlightbackground at highlightthickness ≥ 1) around classic widgets;
`option_add('*<class>.highlightThickness', 0)` at App init removes them for
Checkbutton/Radiobutton/Spinbox/Entry/Button/Listbox app-wide — a no-op on
Windows, which renders these invisibly.

**10. Settings → Discord help dialog updated** to the v2.1.0 slash-command
set (/force actions-only, /train, /stats, /max, /wom refresh, /launch,
/relaunch) — the old text still described /force as covering skills.

### Files changed
p2p_monitor.py, py/watcher.py, py/launcher.py, py/discord.py,
py/relaunch.py (new), py/platform_ops.py, ui/monitor_tab.py,
ui/stats_tab.py, ui/history_tab.py, ui/settings_tab.py, README.md,
CHANGELOG.md, update_manifest.txt, install.sh

## v2.0.2
### Fixed progress bar under Max Progress

## v2.0.1
### Updated WOM default XP rates for more accurate time to max. Also changed Max progress tile to be next 99 instead of last 99.

## v2.0.0
### Stable release

**1. Status tab: the actual blank Mute button bug, found and fixed.** Root
cause confirmed directly in the live code: `_flash_row()` (the brief
highlight when a row updates) forces every child widget's background to
`app.ACC`, including both action buttons. `_restore_row_bg()` deliberately
excluded `mute_btn` from the generic restore loop (its background is
always `app.BG4` regardless of mute state, never the generic row
background) — but nothing ever restored it afterward at all. Left at
`bg=app.ACC` with `fg=app.ACC` in the unmuted state, foreground equals
background, which is exactly what made the text disappear. `ss_btn`
(Screenshot) was never returned from `_build_row()` in the first place,
so it couldn't even be referenced for the same fix. Fixed both: `_build_row()`
now returns `screenshot_btn` too, and `_restore_row_bg()` explicitly
restores both buttons to `app.BG4` after excluding them from the generic
loop. Reproduced the exact bug mechanism in an isolated test (forces the
flash, confirms both buttons really do land on `app.ACC`, confirms the
fix brings them back to `app.BG4` with foreground no longer equal to
background) before trusting it was fixed.

**2. Privacy/identifying-info sweep.** Full repo sweep for real account
names, usernames, Discord IDs/webhook URLs, and file paths containing a
real name — checked code, comments, README, and the entire CHANGELOG
history. Found and fixed one: a real account name used as an example in
a beta.21 CHANGELOG entry, replaced with a generic placeholder. Webhook
URL patterns found are all dynamically-constructed code (f-strings
referencing config values), not hardcoded real tokens. No Discord
snowflake IDs, email addresses, or real Windows usernames found anywhere
— the few `C:\Users\...` references in README/CHANGELOG were already
generic placeholders (`<you>`/`<user>`).

**3. README accuracy pass.** Fixed a stale claim that directly
contradicted a real, shipped behavior change: "filterable by date range
(up to 7 days)" — the 7-day cap was removed several versions ago. Added
two entirely missing feature sections: Stats Overview (daily levels
chart, skill/account breakdowns, filters) and Goals & Maxing (the Wise
Old Man integration — time-to-max, time-to-99, Last 99 Achieved with
Combat correctly excluded, editable XP rates) — both substantial,
already-shipped feature areas that had no README coverage at all before
this pass.

**4. Version: beta tag dropped.** `VERSION` is now `"2.0.0"`. Checked the
self-updater's own version-comparison logic (`_ver_tuple()`) before
making this change — its regex already treats the `-beta.N` suffix as
optional and explicitly ranks a stable version above any beta of the
same major.minor.patch, so no code changes were needed there; this was
purely a string change.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean;
`pyflakes` zero new warnings. 153 checks across 25 test scripts (1 new
this pass, reproducing the exact mute-button bug mechanism and
confirming the fix). Full repeat of the existing regression suite after
every change in this entry.

**Files changed:** `p2p_monitor.py` (version: stable, no beta suffix),
`ui/status_tab.py` (the mute-button fix), `README.md` (stale 7-day
reference, new Stats/Goals & Maxing sections), `CHANGELOG.md` (this
entry, plus the one redacted account name from a prior entry).
`update_manifest.txt`/`install.sh` unchanged — no new files added.

---

## v2.0.0-beta.22
### Status alignment (real fix this time), Settings Windows/Linux font split, Max Progress diagnostics

beta.21's Status column recalibration and Settings padding reduction were
both real attempts, but neither actually worked once tested against real
screenshots — this entry replaces both with mechanisms that don't depend
on guessing font metrics.

**1. Status tab column alignment — the actual fix.** beta.21 tried
recalibrating character-count widths a second time; a real Windows
screenshot confirmed UPTIME/BREAK were still visibly shifted right of
their headers. Character-width Labels can never reliably align across
two different fonts (header uses `SANSS`, row values use `SANS`) — the
same declared character count renders to a different pixel width in
each, no matter how carefully the numbers are tuned. Replaced every
column, both header and row, with deterministic pixel-width frames sized
to the actual measured widget width. Verified directly at the widget
level: every column's x-position now matches header-to-row exactly, to
the pixel, regardless of font.

Caught a real, serious bug while verifying this with an actual
screenshot (not just widget introspection): `pack_propagate(False)` on a
frame with only `width=` set and no `fill='y'`/explicit `height=`
renders as corrupted, illegible dotted text under Tk — confirmed with
an isolated four-way comparison reproducing the exact mechanism. Fixed
(row cells use `fill='y'`, inheriting height from the row the way the
account-name cell already did; header cells use an explicit `height=19`
instead, since the header row has nothing else to anchor a height the
way the data row does). Swept every other `pack_propagate(False)` usage
in the codebase for the same pattern — confirmed no other instance is
vulnerable.

**2. Settings: Windows/Linux font split.** Investigated the actual
mechanism behind Windows needing a scrollbar on Event
Notifications/Restarts & Updates that Linux didn't, since the previous
two rounds of padding reduction (down to `pady=0` on every single
Checkbutton — confirmed, all 8 of them) hadn't closed the gap. Root
cause is the font, not the padding: Segoe UI (Windows) has a
meaningfully taller line-height than DejaVu Sans (Linux) at the *same*
point size — not something any amount of padx/pady tuning can fix, since
it's not a spacing setting. Added Settings-specific platform-conditional
font sizes (scoped to Settings only, not touching Monitor/Status/Stats):
9pt/8pt on Windows (down from 10pt/9pt), 11pt/10pt on Linux (up from
10pt/9pt, using some of Linux's existing spare margin rather than
leaving it unused while Windows still overflows). Caught and reverted a
self-inflicted regression before it shipped: the same substitution also
touched the sidebar's navigation labels, which then truncated "General
Settings"/"Discord Alerts" at the larger size — reverted just the
sidebar to its original, unconditional font, since it was never the
actual target. Re-verified every Settings page still fits comfortably
on the Linux/larger-font path (worst case: Restarts at −19px, still
genuinely under budget, not just within the scroll tolerance).

**3. Monitor Active Accounts / Max Progress on Linux — diagnostics
clarified, root cause still open.** Confirmed directly in the live code
(not from memory) that `on_tab_shown()` already calls both
`refresh_highlights()` and `refresh_max_progress()` together — this was
fixed in beta.20 as one combined hook, not two separate fixes at
different times. Added a debug-gated log line directly inside
`on_tab_shown()` itself, separate from the WOM-computation logging
already inside `refresh_max_progress()` — this lets a future report
distinguish "the hook never fired" from "the hook fired but found
nothing useful in the cache," which the existing logging alone couldn't
tell apart. No behavior change; this is purely diagnostic, since no
concrete bug has been found in this code path yet.

**4. Event Notifications "rebuilds on Linux" — empirically disproven,
not just re-asserted.** Rather than re-read the code and restate the
same conclusion, built an actual test that instruments the real
`_build_notifications_page` method and runs a realistic sequence:
switching between Settings sections 3×, then switching away to Monitor
and back to Settings→Notifications 3×. Result: the build method is
never called again (0 times), the page's widget count is identical
before and after every switch (90, unchanged), and a specific
checkbox's `BooleanVar` is confirmed to be the literal same Python
object throughout — not recreated. `load_fields()` (which would reload
config into existing widgets, a different thing from a structural
rebuild) also only ever runs once, at startup. No rebuild mechanism
exists in this code for this or any other Settings page. What's
actually being observed remains unidentified — flagged as open, pending
more specific reproduction detail, rather than a fix attempted against
a mechanism that doesn't exist.

**Deliberately not changed this entry:** the blank Mute button
visible in one row of a real Status screenshot — could not be
reproduced, and chasing a one-off visual artifact without a way to
trigger it risks a speculative, unverifiable "fix." Flagged, not
touched.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean;
`pyflakes` zero new warnings. 142 checks across 24 test scripts (3 new
this pass: deterministic pixel-width alignment + the rendering-glitch
reproduction, platform-conditional font resolution for both code paths,
and the Event Notifications no-rebuild proof). Every Settings page
re-measured on the Linux/larger-font path — all still fit with real
margin. Status tab re-screenshotted after every change in this entry,
specifically because that's what caught the rendering bug — it never
showed up in any widget-level measurement, only in an actual rendered
screenshot.

**Known limitations:** Settings font split is unverified on real Windows
(Segoe UI specifically) — cannot render it from this sandbox. Max
Progress/Active Accounts root cause on Linux remains genuinely
undiagnosed; the new logging should make the next reproduction
conclusive rather than ambiguous. The blank Mute button and the
described Event-Notifications sensation are both still open, pending
more specific information.

**Files changed:** `p2p_monitor.py` (version bump to beta.22 only —
no other changes), `ui/status_tab.py` (deterministic pixel-width column
alignment, the `pack_propagate(False)` rendering-bug fix),
`ui/settings_tab.py` (platform-conditional fonts, sidebar-regression
revert), `ui/monitor_tab.py` (diagnostic log line in `on_tab_shown()`),
`CHANGELOG.md`. `update_manifest.txt`/`install.sh` unchanged — no new
files added.

---

## v2.0.0-beta.21
### Final cleanup pass — release candidate for 2.0.0 stable

Scoped intentionally small per request — no redesigns, all targeted fixes
against a specific, named ask.

**1. Save/restore main window size.** First launch still defaults to
960×680 exactly. If the user manually resizes larger and then actually
quits (not minimize-to-tray), that width/height is saved and restored on
next launch. Deliberately width/height only, never x/y — saving position
risks launching off-screen after a monitor gets disconnected/reconfigured;
size alone carries no such risk. `_save_window_size()` refuses to save
below the 960×680 minimum (a transient bad read should never become
tomorrow's permanent floor) and refuses to save while minimized/withdrawn
(checks `self.state()`, only saves on `'normal'`/`'zoomed'`). Hooked into
`_do_quit()` specifically — confirmed minimize-to-tray never touches it,
since `_minimize_to_tray()` doesn't call `_do_quit()` at all.

**2. Status tab: removed the redundant "View history" hint.** The footer
already says this; having it twice was noise. Double-click binding now
attaches to just `name_lbl`/`text_col`, not the removed hint label.

**3. Status tab: column alignment recalibration.** Applied the requested
header-width tuple (ACCOUNT 22, ACTIVITY 18, UPTIME 9) and the matching
row-width adjustment (activity 16, uptime 8).

**4. Settings: compacted Windows-specific spacing further.** Applied
every padding reduction from the request exactly as specified — shared
card padding, all three row helpers, Event Notifications' per-event grid
and "Notify every N levels" row, the hide-paint grid, Restarts & Updates'
"Restart Delay" label, and the warning banner's inner padding. Net
effect on Linux (measured, can't verify Windows directly): every Settings
page now fits with real margin instead of being borderline — General
−51px, Discord −92px, Notifications −33px (was −2px), Daily Summary
−158px, Restarts −37px (was −11px). Negative = comfortably under the
available height, not just within the scroll tolerance.

**5. Last 99 Achieved: Combat is now skipped.** Combat is a derived/
composite level (computed from Attack/Strength/Defence/Hitpoints/Ranged/
Magic/Prayer), not a real trainable skill — it should never be reported
as a "Last 99 Achieved" in its own right. Fixed in both paths
`determine_last_99()` actually has: the history-row loop and the cache
fallback loop. Tested directly: Combat-99-only returns nothing (correct —
there's no real skill to report), Combat alongside a real skill's 99
returns the real skill even when Combat's own timestamp is more recent,
and both "Combat" and "Combat Level" spellings are caught case-
insensitively. The user's own uploaded `wom.py` was checked directly
against the live repo file — byte-identical aside from CRLF line endings,
meaning the intended change had not actually been saved into it; this
entry implements it directly instead, adapted to the actual existing
code structure (the request's reference implementation assumed a
`skill = r.get('value', '')` extraction that didn't yet exist in the
history loop — added it, in both the check and the existing `best{}`
construction, rather than introducing a parallel/duplicate variable).

**6. History date-range filter: removed the hard 7-day cap.** The custom
date-range picker's "Maximum range is 7 days" validation is gone
entirely — user picks any From/To range now. Popup title updated to drop
the now-incorrect "(max 7 days)" mention. The quick-filter preset buttons
(All time / Today / 7 days / 30 days) were left untouched — "All time" is
already an existing, always-available option there, so those were never
actually a cap, just shortcut presets; the 7-day *hard limit* lived
exclusively in the custom picker's validation, which is what was removed.
Verified directly: a 20-day custom range now applies successfully with no
error and is not silently clamped back down.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean;
`pyflakes` zero new warnings. 137 checks across 22 test scripts (8 new
this pass) — covering: window-size save/restore (first-launch default,
valid restore, below-minimum clamping, garbage-value fallback, the
minimized/withdrawn save-guard — verified by mocking `state()` directly,
since Xvfb has no real window manager to honor a real `iconify()` call,
confirmed by direct comparison), the Combat-skip logic in both code
paths, and the date-range cap removal (a 20-day range applies and is not
clamped). All 6 tabs + Settings sub-pages re-measured at 960×680.

**Known limitations:** Settings/Status spacing changes are measured on
Linux/Xvfb only — real Windows confirmation (Segoe UI metrics) still
needs a user test, same caveat as every prior UI-spacing pass. DPI
restore (beta20) and the broader Windows-specific items from beta19/20
are unchanged this pass and remain pending real Windows verification.

**Files changed:** `p2p_monitor.py` (`window_size` config key,
`_restore_window_size()`/`_save_window_size()`, version bump to
beta.21), `py/wom.py` (Combat skip in `determine_last_99()`),
`ui/status_tab.py` (hint label removed, column recalibration),
`ui/settings_tab.py` (spacing reductions throughout), `ui/history_tab.py`
(7-day cap removed from the custom date-range picker), `CHANGELOG.md`.
`update_manifest.txt`/`install.sh` unchanged — no new files added.

---

## v2.0.0-beta.20
### Final beta19 polish: Monitor sidebar, launch size, Highlights format, Active Accounts sync, alignment, scroll thresholds

**1. Monitor sidebar scrollbar on Windows.** Linux had margin to spare, so
this was a font-metric difference, not a structural problem like beta19's
fix. Trimmed card padding, button padding, and font sizes throughout the
sidebar (Session Control, the status card, Active Accounts, Max Progress)
for additional safety margin — total sidebar content height down another
~8% on top of beta19's reduction. Cannot verify the exact margin needed
for Segoe UI's metrics without a real Windows test, but the Canvas+
Scrollbar safety net from beta19 means even if this isn't quite enough,
the failure mode is a legitimate scrollbar, never silent clipping —
Max Progress staying fully visible was the one hard requirement here and
that's unaffected either way.

**2. App launch size.** Root cause: `minsize(960, 680)` only sets a floor
on manual resizing — it never controlled the *initial* size, which Tk
computed from whichever tab's packed content needed the most width (all
6 tabs share one grid cell). Added an explicit `self.geometry("960x680")`
right after all tabs are built, which is what actually pins the launch
size. Verified directly: the app now measures exactly 960×680 on launch,
every time, with minsize still enforced as the resize floor.

**3. Highlights row format.** Previously inconsistent: Latest Task/Last
Level Up/Latest Drop never showed an account at all, while Last Error/
Last 99 Achieved buried it inside the time line ("SomeAccount • 2d ago").
Every highlight tile now consistently shows tile name → account → info →
time as four separate lines, account included on every tile (data was
always there — `app._highlights[key]['account']` is populated for every
type; this was purely a rendering gap). Falls back to "Unknown account"
if it's ever genuinely missing.

**4. Monitor Active Accounts not matching Status.** Real root cause, not
just a timing fluke: every other tab (Status, Stats, History, Launcher)
has an `on_tab_shown()` hook that proactively re-queries its data the
moment you switch to it. Monitor never had one — its Active Accounts
card only ever refreshed *reactively*, debounced after a live event. If
nothing new happened to trigger that debounce (e.g. accounts already
running quietly, or right after launch before the first event), the
card could show stale "No accounts yet" indefinitely even though Status,
querying the exact same `get_account_rows()` on demand, showed the truth
immediately. Added Monitor to the tab-switch dispatch with its own
`on_tab_shown()`, pulling from the identical source Status uses — no new
data source, no duplicated layout, per the scope note. Verified directly:
simulated a live account with zero events fired, confirmed Monitor still
showed "No accounts yet" until the tab was switched into, then correctly
updated.

**5. Running card timestamp.** Dropped seconds from "Started" (`%H:%M:%S`
→ `%H:%M`). Scoped narrowly — Status's "Last updated" and Goals & Maxing's
"Last WOM refresh" timestamps serve a different purpose (data freshness)
and were left untouched.

**6. Status table column alignment.** Found two compounding, very
concrete causes, not just "needs better spacing": the ACCOUNT header
used a character-count width (17 chars) while the row's `name_cell`
below it was sized in raw pixels (170) — two different unit systems for
the same column. On top of that, headers render in `SANSS` (9pt) while
row values render in `SANS` (10pt) — the same declared character count
in two different fonts doesn't produce the same pixel width, so every
column drifted further right than the one before it (drift ranged
30–66px by the STATUS column). Recalibrated every header's character
width against the actual measured pixel width of its corresponding row
content. Drift is now 2–6px across all columns — visually aligned.

**7. Settings tiny scroll regions.** `needs_scroll = content_h > visible_h
> 1` had zero tolerance — even 1px of overflow showed a scrollbar.
Confirmed this was real, not theoretical: General Settings' baseline
overflow measured exactly 13px. Added a shared `_SCROLL_TOLERANCE_PX`
(16px) to the *same* scroll-container pattern everywhere it's used
(Settings, Monitor sidebar, Status, History, Launcher) — per the
instruction to fix this everywhere the pattern exists, not just Settings.

**8. Event Notifications / Restarts & Updates height.** Moved each page's
subtitle onto the title row (`_page_header`, shared by every Settings
page) with a dynamically-bounded wraplength so longer subtitles
(Discord Alerts, Restarts & Updates) can't push the row wider than
available. Combined with tightened card/row padding throughout, every
Settings page now fits with real margin, not just within tolerance:
General −40px, Discord −72px, Notifications −2px, Summary −158px,
Restarts −11px (negative = comfortably under the available height).

**9. Windows Settings rendering bigger than Linux.** Addressed by the same
general tightening as items 7–8 — card padding, row padding, and page-
header height all reduced, which helps both platforms identically since
none of it is Linux-specific. Cannot independently confirm Windows parity
without a real Windows test.

**10. DPI restore — actual root cause found, confirmed against real
measured data.** Real Win32 calls tested directly on a 125%-scaled
Windows machine: `GetDpiForWindow` → 120 dpi (scale 1.25, correct),
`GetWindowRect` → 773×571, DWM extended frame bounds → 966×714. 966÷773
≈ 714÷571 ≈ 1.25 — the ratio between the two *measurements* matches the
DPI scale factor almost exactly, which is what made this look like a
DPI-awareness bug. It isn't one. DWM bounds and GetWindowRect are
different rects by definition — DWM bounds exclude the invisible resize-
border padding that GetWindowRect includes — and launcher restore
capture was using `get_window_geometry()` (DWM-first, correct for
screenshots/clicks) to capture the rect, then handing it to
`set_window_geometry()`, which calls `SetWindowPos` — an API that
operates in GetWindowRect's coordinate space, not DWM's. Capturing in
one space and restoring via an API that reads the other was wrong at any
DPI; it only *looked* DPI-proportional because the invisible border being
excluded/included also happens to scale with DPI. This is also exactly
why beta19's fallback correction could never fire: it compares restored
size against *captured* size, and if both the capture and the eventual
restored result are measured the same (wrong) way, the ratio is 1.0 —
no mismatch ever shows up to correct.

Added `get_window_geometry_for_restore()` — GetWindowRect only, no DWM
fallback, running in the identical PER_MONITOR_AWARE_V2 thread context
`SetWindowPos` uses. Launcher restore capture (`_discover_and_cache`'s
pre-close geometry read) and both post-restore/post-correction
verification re-queries now all use this consistently — capture and
verification must agree on coordinate space, or the same mismatch just
reappears one level up. Screenshot and paint-button-click paths are
completely untouched: they keep `get_window_geometry()`'s DWM-first
behavior, which is the *correct* choice for them — BitBlt and click-
target math want the visually-true rendered bounds, not the wider raw
window rect this fix deliberately avoids for restore specifically.

Verified directly against the real measured data: a test reproduces the
exact 773×571-vs-966×714 scenario and confirms `get_window_geometry_for_
restore()` returns the GetWindowRect value, never the DWM value, and
that `_get_window_bounds` (the screenshot/click path) is completely
unmodified and still DWM-first. Cannot independently confirm this fully
resolves the on-screen symptom without a real Windows relaunch test —
but the mechanism this targets is no longer a guess; it's the one the
real measured data actually showed.

**Found and fixed one more real, previously-invisible bug along the way:**
a `tk.Label` with `justify='left'` but no explicit `anchor='w'` on the
Label itself defaults to `anchor='center'` — invisible as long as the
label has room to render at its natural size, but the instant it gets
compressed below that (which is what most of this pass has been about),
Tk center-anchors the text within the smaller box and clips content from
*both* edges. Caught directly in a screenshot: "P2P Monitor does not
auto-update." was rendering as "P Monitor does not auto-update." with no
error, no warning, nothing in any measurement to catch it. Found and fixed
every instance of this exact pattern across the codebase (4 labels, 3
files) via a properly paren-matched search — confirmed zero remaining
instances of `justify='left'` without `anchor=` anywhere. Reproduced the
exact mechanism in isolation as a regression test.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean;
`pyflakes` zero new warnings. 115 checks across 20 test scripts (10 new
this pass) — covering: launch size (exact 960×680 confirmed), Monitor
Active Accounts sync (the stale-vs-live scenario reproduced directly),
the scroll-tolerance behavior (both the suppressed-tiny-overflow and the
still-triggers-on-genuine-overflow cases), the label-anchor-clipping bug
reproduced and confirmed fixed in isolation, and the DPI restore-capture
fix reproduced against the exact real 773×571-vs-966×714 measured
scenario, confirming the new function returns the GetWindowRect value
and that the screenshot/click path remains completely untouched.
Status/Settings/Monitor re-screenshotted with realistic populated data
(not empty placeholders) after every fix in this entry, specifically
because that's what caught both the alignment drift and the
label-clipping bug — neither showed up in any numeric measurement.

**Known limitations:** Items 1 and 9 (Windows-specific sidebar/Settings
sizing) are addressed with real, meaningful reductions but not verified
on actual Windows hardware — there is no way to do so from this sandbox.
Item 10 (DPI restore) now targets a confirmed mechanism rather than a
hypothesis, but whether it fully resolves the on-screen symptom still
needs a real Windows relaunch test to know for certain.

**Files changed:** `p2p_monitor.py` (explicit launch geometry, version
bump to beta.20), `py/platform_ops.py` (new `get_window_geometry_for_
restore()`/`_get_window_rect_for_restore()` — GetWindowRect-only capture
for the restore path), `py/launcher.py` (restore capture and both
verification re-queries switched to the new function), `ui/monitor_tab.py`
(sidebar trim, Highlights 4-line format, `on_tab_shown`, scroll
tolerance), `ui/status_tab.py` (column header recalibration, scroll
tolerance), `ui/settings_tab.py` (inline page-header subtitle, card/row
trim, scroll tolerance, 3 label-anchor fixes), `ui/history_tab.py`
(scroll tolerance, 1 label-anchor fix), `ui/launcher_tab.py` (scroll
tolerance), `ui/wom_goals.py` (1 label-anchor fix), `CHANGELOG.md`.
`update_manifest.txt`/`install.sh` unchanged — no new files added.

---

## v2.0.0-beta.19
### UI polish pass (Windows + Linux) + DPI restore re-investigation

Driven entirely by real screenshots from both platforms at minimum window
size, not the Xvfb requested-size harness alone — the harness's numeric
"fits" checks turned out to have real blind spots (see Monitor below),
exactly as flagged going into this pass. Every fix in this entry was
verified against an actual rendered screenshot, not just a measurement.

**1. Monitor tab — structural root cause found and fixed, not just resized.**
The left sidebar (Session Control, the status card, Active Accounts, Max
Progress) used `pack_propagate(False)` with no explicit height, sized via
`fill='y'` to whatever the window's real height happened to be — completely
decoupled from its own children's actual content needs. With real data
(a real account, real WOM cache values) the four cards needed ~647px
combined, well past what's available, and silently clipped at the bottom
with **zero numeric signal anywhere** — every `winfo_reqheight()` check
kept reporting a small, misleading number. This is exactly why Max
Progress was getting cut off despite earlier checks saying the tab "fit."
Fixed two ways: added the same Canvas+Scrollbar safety net already used
elsewhere in this app (so future overflow degrades to a scrollbar instead
of silent clipping), and separately tightened every card's padding, the
sidebar width (−25%), the header/nav chrome (~−17%), the stat strip, and
the Highlights row (bold removed, smaller font) so in practice the
scrollbar should never need to actually appear. Found and fixed two more
missing-`wraplength` overflow bugs along the way (Max Progress's own
labels, the Highlights row's sub-labels) that only showed up once real
(non-empty) data was used to test.

**2. Status tab.** Left column removed entirely, per spec — everything in
it either duplicated Monitor or was visible on Status itself already. The
account table's columns were widened to use the freed space and re-tuned
to line up with the actual row content. Re-discovered and fixed a real
rendering bug while verifying this visually: the status badge (Logged
In / On Break / Offline) was built via `pack(in_=badge_wrap)` — a
separate-wrapper-frame indirection — which silently failed to render at
all once that wrapper became width-constrained. Rewritten to parent the
badge directly to its wrapper (simpler, and the actual standard pattern),
which fixed it outright.

**3. Stats Overview.** The "Daily Levels Gained" chart's bottom-clipping
bug had a real, specific cause: the chart canvas had no `<Configure>`
binding at all, unlike the donut chart on the same page — it only ever
drew once, at whatever size it happened to have at that exact moment, and
never redrew if the page later settled into a different (typically
smaller) final layout. Fixed by caching the last-drawn series/breakdown
and redrawing (debounced) whenever the canvas's real size changes —
verified with an actual resize test confirming the chart's plotted
coordinates update correctly. KPI tiles shortened vertically.

**4. Stats → Goals & Maxing (both All Accounts and single-account views).**
Header, account selector, and all three buttons condensed onto one
toolbar row — required trimming font sizes and a couple of button labels
("Refresh WOM" → "Refresh") to actually fit at 960px width once title and
controls shared one line instead of two. Summary tiles de-bolded and
shrunk (affects all of: Closest to Max, Closest 99, Last 99 Achieved,
Last WOM Refresh, and the three footer cards). WOM Username row
shrunk. The three bottom footer cards (Estimated Time to Max, Combat
Skills Covered by Slayer, About XP Rates) were being clipped below the
visible window — confirmed fixed via screenshot, fully visible now. Also
found and fixed the **All Accounts overview table**'s own column-width
overflow (its `LAST REFRESH` column ran off the right edge) — a different
table than the per-account skill table fixed in beta.18, same bug class;
narrowed columns and capped it to the same 10-row scroll behavior.

**5. Invisible-scroll bug (Launcher, plus 4 other places using the same
pattern).** Real root cause: every Canvas+Scrollbar implementation in this
app shows/hides the scrollbar based on whether content actually overflows
— correctly — but the mousewheel/trackpad handlers underneath that
scrollbar called `yview_scroll()` *unconditionally*, regardless of
whether scrolling was ever actually needed. `canvas.bbox('all')` can
report a scrollregion a few pixels taller than the visible canvas from
rounding alone, which was enough for a wheel scroll to nudge the view
even when there was visually "nothing to scroll to." Fixed by gating the
actual scroll action on a live `needs_scroll` flag, not just the
scrollbar's visibility — found and fixed identically in Launcher, Status,
History, and Monitor's new sidebar wrapper. Verified both that scrolling
is now a no-op when content fits, and that it still works correctly when
content genuinely overflows. Settings' own scroll wrapper already had an
equivalent guard via a different mechanism (gating the bind itself) and
needed no change.

**6. Settings — all 4 pages (General, Discord Alerts, Event Notifications,
Restarts & Updates).** The reported "right side clips out" had a real,
structural cause, not just tight padding: the two-column layout used
`pack(side='left', expand=True)` for both columns, and pack gives
whichever column is packed *first* its full natural width before the
second one gets anything — so if the left column's content was ever
wider than half the available space (it was: an unwrapped subtitle on
the Paint Reference card), the right column got starved down to whatever
was left over, compressing "Monitoring Intervals" and the Debug card well
below their own minimum width. Switched both two-column layouts (General
page's main columns, and the Discord page's webhook columns) from `pack`
to `grid` with equal `uniform`-weighted columns, which forces a fair
50/50 split regardless of which side's content is heavier — confirmed via
direct widget measurement that both columns now get equal, bounded width,
and the previously-compressed labels render at their full requested size.
Also fixed several more missing-`wraplength` instances (`_row_text`/
`_row_int`/`_row_bool`'s helper text, the debug-file path display) that
were silently overflowing under the canvas-width constraint. Sidebar
narrowed (−25%), font sizes reduced, page header and card padding
tightened throughout.

**7. Linux Monitor — "No WOM data" despite a real WOM cache existing.**
Traced to a real, concrete bug: `refresh_max_progress()`'s per-account
loop called `compute_account_summary()` with no exception handling at
all. One malformed or unexpectedly-shaped cache entry would silently
crash the *entire* background thread (daemon thread, uncaught exception),
meaning `_apply_max_progress()` never got called and the UI stayed stuck
on its initial "No WOM data yet" placeholder forever — indistinguishable
from there being no cache at all, even though the cache file itself was
read successfully moments earlier. Fixed with a per-account try/except
(one bad entry no longer blocks every other account) plus an outer
safety net (any other unexpected failure still results in a real UI
update, falling back to "no data," instead of the thread just vanishing).
Verified with a test that deliberately puts a malformed entry *before* a
good one in iteration order and confirms the good one still gets found.

**8. Linux History — expanding an account rebuilt it every time.**
`_toggle_account()` called the same full `_rebuild_accounts()` used for
filter changes on *every single expand/collapse click* — destroying and
rebuilding every account's card, including every other already-open
account's tree, just to toggle one chevron. Rewritten to operate directly
on the toggled account: collapsing just unpacks its body (the built tree
stays alive, merely hidden); expanding re-packs it if already built, or
builds it lazily exactly once if this is the first time that account has
ever been expanded. Verified: re-expanding reuses the literal same tree
widget object (no rebuild at all), and toggling one account never touches
any other account's card.

**9. DPI restore — re-investigated, not re-assumed fixed.** The beta18 fix
(thread-level `SetThreadDpiAwarenessContext`) addressed a real gap, but a
DPI *scaling* error (the reported symptom) is a multiplicative effect —
1.25x/1.5x — categorically different from the small additive pixel offset
that fix's own docstring acknowledged accepting. Fixed every remaining
untyped `SetThreadDpiAwarenessContext` call in the file (there were three:
`_get_window_bounds`, `_set_window_geometry_windows`, and
`_click_at_windows`) — `DPI_AWARENESS_CONTEXT` is a pointer-sized `HANDLE`
(8 bytes on 64-bit Windows), and without explicit `c_void_p`
argtypes/restype, ctypes' default 32-bit `c_int` guess can silently
misread the saved "old context" value. All three now match, with a
`finally` around each so the context is restored even if the call it
wraps raises.

Went through a real back-and-forth on the actual restore logic that's
worth recording. First pass divided the captured rect by
`get_window_dpi_scale()` *before every* `SetWindowPos` call, on the theory
that the target window (DreamBot's own process) might not be DPI-aware
and could be re-inflating an already-correct physical value. Caught and
reverted before shipping, for exactly the reasons that make this dangerous
as a default: if the thread-awareness fix is already working, pre-dividing
would make a correctly-restored client too small instead — `get_window_dpi_scale()`
is a DPI *reading*, not proof `SetWindowPos` is actually misbehaving — and
dividing x/y by a scale factor is a real correctness risk on multi-monitor
setups, where the coordinate origin for a non-primary monitor generally
isn't (0, 0), so a naive divide can move the window to a different screen
entirely. Replaced with the right shape of fix: restore using the
**original captured rect** first (no pre-emptive scaling of any kind),
wait ~400ms for the new window to settle, then re-query its actual
geometry the same way it was originally captured. Only if that measured
ratio clearly, consistently matches a recognized Windows DPI scale preset
on *both* axes independently (125%/150%/175%/200%, each within a tight
band) does a second, corrective `SetWindowPos` fire — and that correction
only ever touches width/height; **x/y are never divided**, for the same
multi-monitor-origin reason above. An unrecognized or single-axis-only
mismatch is logged but never auto-corrected, since guessing at a ratio
that doesn't match a real preset risks making things worse, not better.

Cannot independently verify this is the *complete* fix — there is no way
to exercise real Windows DPI virtualization from this Linux sandbox.
What's shipped: the safe primary path (original rect, corrected thread
typing) plus a measured, conservative fallback that only ever activates
on unambiguous evidence — not a guess applied up front. Comprehensive,
always-on diagnostics (`get_dpi_diagnostics()` — thread DPI awareness
context *by name*, process DPI awareness, window DPI, system DPI) are
logged at both capture and restore time, and every step of the
verify-then-maybe-correct sequence (requested rect, first actual rect,
detected ratio if any, corrected rect if a correction fired, final actual
rect) writes to the persistent debug log regardless of the debug toggle —
specifically so a real scaled-DPI Windows test now produces a complete,
readable trail instead of a guess. Did not set process-level DPI
awareness (the other option raised) — that carries real risk of affecting
Tk's own rendering in ways untestable from here, so it's deliberately
deferred pending what the new diagnostics show on real hardware. Did not
clamp to work area, per instruction — exact position is always restored,
never touched by the fallback correction either.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean;
`pyflakes` diffed against the pre-pass baseline — zero new warnings. 85+
checks across 14 test scripts (10 new this pass), covering: the Monitor
sidebar/scroll fixes, the invisible-scroll guard (both the no-op and the
genuinely-needed case), DPI diagnostics logic (Linux no-op path +
mocked-Windows scale-factor computation), the measured DPI fallback
correction (band detection, single-axis-mismatch rejection, unrecognized-
ratio rejection, x/y-untouched, and a full simulated round-trip), the WOM
cache exception resilience, the chart resize-redraw, and the History
toggle-caching behavior. Layout re-measured at the literal 960×680 minimum
across all 6
tabs and the Goals & Maxing sub-page under synthetic load. Every
structural finding (Monitor's `pack_propagate` gap, the Status badge
render bug, Settings' `pack` greedy-column bug) was confirmed via an
actual rendered screenshot, not measurement alone — per this pass's own
opening instruction.

**Known limitations:**
- DPI restore: see item 9 — diagnostics are comprehensive and ready, but
  the fix's completeness cannot be confirmed without a real scaled-DPI
  Windows test. Next step is reading what `get_dpi_diagnostics()` and the
  post-restore verification log actually show on real hardware.
- All UI fixes were verified visually under Xvfb (effectively a Linux
  render) and against the user-provided Windows screenshots/spec, but
  there's no substitute for a fresh screenshot pass on real Windows to
  catch anything Segoe UI's font metrics do differently from DejaVu
  Sans's.

**Files changed:** `p2p_monitor.py` (header chrome/nav height, version
bump to beta.19), `py/platform_ops.py` (DPI diagnostics, ctypes typing
fixes — all 3 instances — debug_log threading), `py/launcher.py` (DPI
debug logging at capture and restore, measured-fallback size correction),
`ui/monitor_tab.py` (sidebar restructure, scroll
guard, sizing), `ui/status_tab.py` (left column removed, badge render
fix, scroll guard), `ui/stats_tab.py` (chart resize-redraw, KPI sizing),
`ui/wom_goals.py` (toolbar consolidation, tile sizing, all-accounts table
columns), `ui/launcher_tab.py` (scroll guard), `ui/history_tab.py`
(toggle-account caching, scroll guard), `ui/settings_tab.py` (grid-based
column fix, sidebar/card sizing, wraplength fixes), `CHANGELOG.md`.
`update_manifest.txt`/`install.sh` unchanged — no new files added.

---

## v2.0.0-beta.18
### Fix pass: Windows DPI restore, Wine/Prayer cross-batch suppression, Goals & Maxing + app-wide 960×680 containment, History live-append

**1. Windows DPI / launcher restore resize bug.** Root cause: the *read* side of window geometry (`_get_window_bounds`, used to capture the DreamBot client's position before a relaunch) already ran under `SetThreadDpiAwarenessContext(-4)` like every other Win32 geometry call in this app — but the *write* side (`_set_window_geometry_windows`, which actually restores it via `SetWindowPos`) never did. Since the process itself is never declared DPI-aware anywhere, the OS silently DPI-virtualized the restore call, rescaling the position/size against the wrong DPI context on scaled displays. Fixed by wrapping the `SetWindowPos` call in the same thread-DPI-awareness pattern as the rest of the module. Position is always restored exactly as captured, never clamped or guessed, including when it was partially off-screen or under the taskbar — that's where the user had it. A new, separate guardrail rejects only the *size* (never the position) when the captured width/height falls below DreamBot's real, known-impossible-to-render-smaller floor (765×503) — in that case the window moves back to its exact spot but isn't resized, falling back to the client's current size instead of reapplying known-bad data.

**2. Wine of Zamorak death-suppression false positives.** The old check only looked at the 2 raw lines immediately before a death line for two exact markers. Widened to a small window on both sides of the death line, and added `Ooh nasty` / `Died during Prayer` as additional markers — but only as *weak* evidence: they never suppress on their own, only when a strong Wine marker (`STOP STEALING MY WINE`, `Interacting Wine of zamorak`) is also present in the same window, so an unrelated Prayer death elsewhere is never swallowed. Separately, and more importantly: traced a real production case (provided log excerpt) where the Wine lines and the death line landed in two different live-poll batches 5 seconds apart — exactly the watcher's poll interval — meaning no amount of window-widening inside the parser alone could ever see both sides. Fixed by carrying a small tail of recent raw lines forward across batch boundaries (live watcher and backfill both now pass this as `context_lines` into `parse_lines()`, a new optional, backward-compatible parameter), reusing the same rolling-buffer pattern already in place for manual-stop detection. `parse_lines()`'s default behavior is unchanged for every existing caller that doesn't pass it.

**3. Goals & Maxing table.** Skill table's `Treeview` row cap reduced from 26 to 10 — the actual reported bug (table content forcing the app window taller). Existing scrollbar already handles the rest.

**4. App-wide 960×680 containment.** Auditing every tab the same way Goals & Maxing was audited turned up real, separate overflow bugs already present before this pass, all fixed the same way — tightened sizing first, scrolling only where content is genuinely unbounded:
- **Monitor:** the event log's `Text` widget never set `height=`/`width=`, so it defaulted to Tk's built-in 24 lines × 80 characters — by far the largest contributor to both the tab's width and height overflow. Given an explicit 12-line height (still scrolls internally) and a sane width. Stat-strip and Highlights cards' padding/wraplength tightened to fit the remaining width budget. No content or layout changed, only spacing.
- **Status:** two independent bugs. The column-header row used fixed character-widths that summed past the available space even with zero accounts (a static, data-independent overflow). Separately and more seriously: confirmed by directly injecting 15 synthetic account rows that the account list — unlike History/Launcher/Settings — had never been given the Canvas+Scrollbar wrapper those other tabs use, so it grew the whole app window without bound as accounts were added (reqheight 1091px against a 586px budget in the test). Added the same wrapper, narrowed the header, and **caught a real regression while visually verifying the fix with a screenshot, not just width numbers**: narrowing the row's labels without capping their actual text length let a long task/activity name push Mute/Screenshot off the now-fixed-width canvas with no horizontal scroll to recover them. Fixed by capping displayed task/activity/account text to their column width (with an ellipsis), which also surfaced and fixed an unrelated pre-existing rendering issue where the status badge — built via `pack(in_=...)` to a separate wrapper frame — silently failed to draw at all once that wrapper was width-constrained; rewritten to parent the badge directly to its wrapper, which is also simply the more standard pattern.
- **Stats → Overview:** the always-present filter row (account/skill comboboxes, date-range pills, refresh button) was the actual overflow source, not the empty-data state as first suspected — narrowed to fit. Separately, the daily-levels-gained chart's `Canvas` also never set `width=`/`height=`, defaulting to Tk's built-in 394×276 — same root cause as Monitor's Text widget, same fix (explicit smaller initial size, still expands via `fill='both', expand=True` when the window is larger). No chart/donut rendering logic touched, per the original "Overview unchanged" guidance reinterpreted as containment-only.
- **Goals & Maxing (revisited):** confirmed by populating it with 23 synthetic skills that the page itself still overflowed width (skill table's column pixel-widths, an unwrapped helper-hint label, and the WOM-username row's unwrapped explanatory text) even after the row-count fix above — narrowed all three.
- **History, Launcher, Settings:** audited, already fit cleanly, left untouched.

All six tabs and the Goals & Maxing sub-page now measure within budget under Xvfb at the literal 960×680 minimum, including under synthetic load (15 Status accounts, 23 Goals & Maxing skills) — verified by both automated width/height checks and visual screenshot inspection (which is what caught the Status badge regression above; the numbers alone would not have).

**5. History tab: stop reloading from disk on every tab switch, add genuine live-append.** Traced `on_tab_shown()` to the real, currently-active bug: every click into History — even a redundant click while already on it — triggered a full disk re-read for every account plus a full destroy/rebuild of every account card. Now gated to the first show only; subsequent switches do nothing on their own. This made a real live-update path necessary (previously the tab simply didn't update live at all — `append_entry()`'s "debounced rebuild" was traced and confirmed to be dead code with zero call sites anywhere in the project before this pass), so:
- `App._on_event()` now forwards every live event to `HistoryTab.append_entry()` after its existing handling, using the current time (the exact parser timestamp isn't passed through this callback, and per scope this wasn't changed to add it) and matching the disk-write's value/activity shape exactly for `death` and `script_event` (both get remapped differently between the UI counter callback and the actual disk record in `watcher.py`) — `drop`'s exact sub-type isn't derivable here without the parser's internal data, so it gets a close approximation that self-corrects on the next real reload.
- `append_entry()` itself is now genuinely incremental: updates the in-memory cache, and if the account's card is already built and the event matches the active filters, inserts exactly one row at the correct sorted position and updates that account's count/last-event labels in place — never a full rebuild for a normal event. A bounded recent-event-key guard prevents the same event from ever being double-applied. A brand-new account (no card yet) still triggers one real rebuild — rare, and judged not worth the risk of hand-computing its sorted insertion position. An event that doesn't match the active type/search filter is still cached, just not visually inserted. Also fixed the Summary popup button, which previously captured its account's event list once at build time — now stale the moment a live append updates the cache without a full rebuild — to recompute fresh at click time instead.
- Date-filter changes, manual refresh, sort changes, and backfill completing all still go through a full reload/rebuild exactly as before — only the redundant tab-switch reload and the per-event rebuild were removed.

**6. Cleanup, post-review:**
- `_set_window_geometry_windows`'s `SetWindowPos` call now restores the thread's DPI-awareness context in a `finally` block, so it's restored even if `SetWindowPos` or anything around it raises — previously a failure there could leave the thread stuck in `PER_MONITOR_AWARE_V2` for every subsequent Win32 call on that thread, not just this one.
- Fixed a real, repeated bug across the entire auto-update flow: 8 separate `except ... as e:` blocks scheduled a `lambda` via `self.after(0, ...)` that referenced `e` — but Python 3 deletes an exception variable the moment its `except` block exits, so by the time the deferred lambda actually ran, `e` no longer existed in scope. Every one of these would have raised `NameError` instead of showing the intended error dialog, specifically in the failure path where the user most needs to see what went wrong. Fixed by capturing the needed message into a plain local variable before scheduling the callback. Confirmed with a standalone reproduction of the exact failure mode before and after.
- Fixed a genuine `NameError`-on-use bug in `py/discord.py`: the discord.py-auto-install path referenced `sys.executable` but the module never imported `sys`.
- General `pyflakes` pass across every touched file plus a few adjacent ones: removed several dead/unused imports and a few vestigial `global` declarations (the actual mutation in both cases already happens correctly in a nested function with its own `global`), one dead local variable, two genuinely-redundant `find_window_ids_by_name`/`get_window_geometry` re-imports in `py/screenshot.py` that pyflakes flagged as shadowing an already-dead module-level import, and 8 harmless f-strings with no actual placeholders. Pyflakes warning count across the whole project: 45 → 4, all 4 remaining already-documented intentional existence-check imports (`discord`, `PIL.ImageGrab`/`ImageChops`) predating this pass.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean; `pyflakes` diffed line-by-line against the pre-change baseline at every step — zero new warnings introduced by any fix, only line-number shifts from added code, with the project-wide warning count actually reduced (45 → 4) by the cleanup pass above. New standalone test scripts (stdlib-only, Tkinter simulated rather than imported per project convention, except where real Tk/Xvfb integration testing was the only way to catch what pure-logic tests couldn't — see the Status badge regression above): Wine/Prayer suppression (9 checks, including the exact cross-batch scenario from the reported log), launcher geometry guardrails (13 checks), History filter-matching and dedup-key logic (12 checks), a real-Xvfb History integration test exercising the actual `HistoryTab` class end-to-end (14 checks: incremental insert, dedup, new-account fallback, filtered-event caching), tab-switch reload gating (2 checks), `_on_event`'s History-forwarding remap logic (9 checks), and a mocked reproduction confirming the DPI-context `finally` fix actually restores context across a simulated `SetWindowPos` failure (2 checks). All 61 pass. Layout measured under Xvfb against the real `App` class (not a mock) at the literal 960×680 minimum, both at baseline and under synthetic load, plus visual screenshot inspection of every changed tab.

**Known limitations, left as-is on purpose:**
- A live-appended `drop` event's exact sub-type (pet/collection log/etc) isn't derivable from `App._on_event`'s callback signature — it gets a close approximation that's replaced with the exact disk value on the next real reload.
- History's "Last event" label has likely always shown the *oldest* event in the loaded window rather than the newest — `entries[0]` on a list that's chronologically oldest-first, not reverse-sorted. Pre-existing, not introduced by this pass, and out of scope for it — left alone on request.

**Files changed:** `py/platform_ops.py`, `py/launcher.py`, `py/reader.py`, `py/watcher.py`, `py/discord.py`, `py/error_rules.py`, `py/inferno_rules.py`, `py/paint.py`, `py/screenshot.py`, `ui/wom_goals.py`, `ui/monitor_tab.py`, `ui/status_tab.py`, `ui/stats_tab.py`, `ui/history_tab.py`, `p2p_monitor.py` (History live-append wiring, the deferred-exception fix, version bump to beta.18 — beta.17 had already shipped), `CHANGELOG.md`. `update_manifest.txt`/`install.sh` unchanged — no new files added.

---

## v2.0.0-beta.17
### Fix: WOM API requests rejected with HTTP 403 — missing User-Agent header

**The bug, exactly as reported:** "Refresh WOM" failing for every account with `WOM API error (HTTP 403)`, despite valid accounts and a correct request shape.

**Root cause:** `fetch_player()` never set a `User-Agent` header, so every request fell back to urllib's default (`Python-urllib/3.x`). Confirmed directly against WOM's own API documentation (`docs.wiseoldman.net`) — the intro page states plainly that a request with no identifiable User-Agent gets the client IP banned, which is exactly the 403 being hit. This was missed originally because the player-endpoints page (the one actually consulted while building `py/wom.py`) doesn't mention this requirement at all — it's stated only on the docs site's front page.

**Fix:** every request now sends a real, non-default User-Agent. Deliberately generic and unconnected to this project, any script, or DreamBot — this app runs independently on many separate end-user machines, and a shared, project-identifying value would make every installation collectively traceable as one entity to WOM, which isn't something to opt every user into.

**Refined: WOM requests now use the refreshed WOM username as the anonymous User-Agent instead of a shared label.** The first version of this fix used one fixed placeholder value for every request, from every account, on every installation — which still satisfies WOM's "must be identifiable" requirement technically, but groups all activity under one indistinguishable label rather than the more genuinely useful signal WOM actually asks for. Since the WOM username is already right there in the request URL, using that exact same value as the User-Agent (via a new `build_user_agent()` helper) gives a real per-account signal without exposing anything about this app, its repository, or DreamBot — and without grouping unrelated users together under one shared identity. Sanitized against header-injection (CR/LF and other control characters stripped) and length-capped; falls back to a generic placeholder only if a username is somehow empty, which `fetch_player()`'s existing early-return already prevents from being reachable in practice.

**Why none of the existing tests caught this:** every WOM test mocks `urllib.request.urlopen` directly, which is correct for testing response-handling logic but means none of them ever exercised what headers a *real* request would actually carry — this could only surface against the live API, which is exactly how it was found.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean, re-run after the refinement too. Tests rewritten to confirm the exact specified behavior precisely: a simple single-word username sends a User-Agent identical to that username and the unchanged URL; a username containing spaces sends a User-Agent with spaces preserved exactly (not URL-encoded, not underscored) alongside the correctly percent-encoded URL; CR/LF and other control characters are stripped so header injection is impossible; very long or blank usernames are handled safely. All 479 checks across all fourteen suites re-run clean, both in the working copy and on a fresh beta.16 checkout with just this patch applied.

**Files changed:** `py/wom.py` (the fix, then refined to use the per-username User-Agent), `p2p_monitor.py` (version bump only), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files.

---

## v2.0.0-beta.16
### WOM Goals & Maxing — Wise Old Man integration, Monitor summary cards, Highlight persistence fix

**New feature: Goals & Maxing**, a new Stats sub-page tracking time-to-99/time-to-max progress via the [Wise Old Man](https://wiseoldman.net) API. Lives entirely in a new `py/wom.py` (API client/cache/calculations) + `ui/wom_goals.py` (the UI) — kept separate from `ui/stats_tab.py` specifically because that file is already large and has its own delicate, well-documented lazy-build history (see below).

**API verified live before writing any code against it.** Confirmed `POST /players/{username}` does "update then fetch latest" in one request, exactly the behavior the spec wanted for manual refresh. Found one real naming mismatch worth flagging: WOM's skill metric is `runecrafting`, not `runecraft` — every other skill name matches the in-game name directly. Couldn't reach the live API from this sandbox to confirm an exact 404 error body, so `fetch_player()` is written defensively against HTTP status codes and malformed/missing data generically rather than assuming one exact error shape — handles player-not-found, opted-out/private players (WOM marks these via an `annotations: [{"type": "opt_out"}]` field on an otherwise-successful response, not an error), network failure, timeout, and malformed responses, each with its own clear message, none of them ever raising.

**Architecture, per spec's explicit separation:** config (`wom_username_map`, `wom_global_rate_overrides`, `wom_account_rate_overrides`) holds settings only; the cache (`~/.p2p_monitor/wom_cache.json`) holds fetched skill data only; nothing computed (time-to-99, time-to-max, closest-99) is ever persisted in either file — every estimate is computed fresh from cache + current config on every render, which is what makes "editing one rate immediately recalculates displayed estimates" trivially true rather than something that needs its own invalidation logic.

**Monitor tab:** the old duplicate "Active Accounts" highlight card (already shown in the sidebar) is replaced with **Last 99 Achieved**. A new **Max Progress** sidebar card sits below Active Accounts, reading only the on-disk WOM cache — confirmed by code path and by a dedicated test that it never calls the WOM API itself. Clicking the card jumps straight to Stats → Goals & Maxing, building Stats lazily if it hasn't been opened yet this session.

**Stats tab:** gained a 2-item sidebar — Overview (the existing content, byte-for-byte unchanged, default-shown) and Goals & Maxing (new). This was the riskiest part of this checkpoint: `ui/stats_tab.py`'s own comments document a Linux duplicate-section bug from when building canvas content into an unmapped frame could leave partial widgets behind. The restructure preserves the exact same `fill='both', expand=True` chain down to the existing chart, just with one more level of Frame nesting for the sidebar — validated by re-running the full existing Stats suite (chart, donut, filters, KPIs) unchanged, plus new checks confirming repeated section-switching and repeated tab-switching never duplicate widgets. Goals & Maxing itself is built lazily on first click, mirroring that same file's own established prewarm philosophy, not built eagerly just because Stats was opened.

**Highlight persistence fix:** Latest Task / Last Level Up / Last 99 Achieved / Last Error / Latest Drop now restore from history on a background thread shortly after app startup, instead of showing "None yet" after every restart. Found and removed something the brief didn't anticipate: `App._start()` was unconditionally wiping all five highlights every time Start was clicked — not just on a fresh launch — which directly fought the persistence goal, since stopping and restarting monitoring mid-session would lose them again. Highlights now persist across Start/Stop within a session too. A live event arriving while the background history scan is still running always wins over whatever the scan finds — verified directly with a deliberate race-ordering test.

**Last 99 Achieved logic:** prefers a real history levelup event (has an actual timestamp) over WOM cache data (which only knows a skill is *currently* at 99, never when it was reached) — implemented once in `py/wom.py` for a single account, with the Stats page's "system-wide" (all-accounts) version built as a thin orchestration layer on top rather than a second copy of the same logic.

**Pre-release review pass — four fixes, plus one real bug found while implementing them:**

1. **WOM username mapping UI added.** Goals & Maxing's single-account view now has a simple "WOM Username" field, defaulting to the account name, saving to `cfg['wom_username_map'][account]` — needed because a DreamBot account name and its actual WOM username don't always match. Saving never triggers a refresh itself; that stays a separate, explicit action.

2. **Last-99 history scan moved off the Tk main thread.** `ui/wom_goals.py` previously called `load_levelup_rows()` (a disk read) directly during render. Replaced with a one-time background scan at page construction, cached in memory for the page's lifetime; the page renders immediately with whatever's already available (WOM cache fallback), then re-renders once the scan completes via `app.after(0, ...)` if a real history match exists.

3. **Monitor's Last 99 / Max Progress now genuinely use `py.wom.determine_last_99()`.** Found and fixed a real bug while implementing this: **every adapter that built rows for `determine_last_99()` — in both the original `ui/wom_goals.py` and the new Monitor code — omitted the `'activity': '99'` field that function's own filter requires.** The result: `determine_last_99()` silently rejected every row and *always* fell through to the cache fallback, even when genuine, more-recent history existed — since beta.16's very first draft, not something introduced by this fix. Caught because two of my own earlier tests happened to seed cache and history with the *same* skill, so a coincidental cache-fallback match was indistinguishable from a real history match passing. Fixed at every call site, the function's docstring corrected to actually state the requirement, and every affected test rewritten to deliberately mismatch cache vs. history — a pass can now only mean history was truly consulted.

4. **Source column fixed.** `effective_rate()` now returns *where* a skill's rate actually came from — `'Account override'`, `'Global override'`, or `'Default'` — instead of the skill table always showing `'Default'` for every active skill regardless of whether it had been overridden.

**Edit XP Rates:** scope selector (global defaults or a specific account), per-skill rate column, mode/label always read-only (sourced from the static rate table — only the number itself is ever overridden). The spec's "Reset selected skill" button would have needed a row-selection mechanism this table doesn't have; implemented as a small per-row reset button instead — same practical outcome, notably simpler. Top-level "Reset Defaults" resets the currently-selected scope, with a confirmation prompt since it's destructive.

**Refresh WOM:** always backgrounded, guarded against overlapping clicks, a small delay between sequential requests when refreshing "All Accounts" (WOM's own docs ask integrators to space out requests rather than hammer them even within the rate limit). A failed refresh keeps the existing cached data and shows a non-fatal warning — verified directly that old skill data survives a failed refresh untouched.

**Caught by the tuple-padding constructor scan, fixed before it shipped:** one instance in the new Edit XP Rates dialog's button row. Re-scanned after: zero remaining instances anywhere in the repo.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` clean — re-run after the review fixes too. `pyflakes` on every touched/new file, content-diffed against the beta.15 baseline where applicable — zero new issues anywhere, both before and after the review pass. 139 checks across four dedicated suites: 29 for the WOM API client, 51 for the calculation logic (8 new, covering the Source-column fix's `rate_source` field through `effective_rate()` and `compute_skill_estimate()`), 52 for the live Goals & Maxing page (15 new — the username field defaulting/saving/clearing, the background scan returning immediately while still finding the right data, the two history-vs-cache assertions rewritten to deliberately mismatch and catch the exact bug class described above), 7 in a new dedicated suite for Monitor's Max Progress and Highlight-card cache-fallback behavior, every assertion there built the same deliberately-mismatched way. All 327 pre-existing checks across the other ten suites re-run clean — 466 total. Full real-`App` construction cycling through all 6 tabs plus Goals & Maxing confirmed no crashes, both before and after the review pass.

**A real bug caught by my own test while building this, fixed before it shipped (unrelated to the review-pass bug above):** my first draft of the "editing a rate immediately recalculates" test appeared to fail — investigation showed it was actually a test-sequencing artifact (an earlier mocked "refresh all accounts" step had legitimately overwritten the test account's cache with minimal data, correctly leaving it with no active skills), not a real bug in the recalculation logic itself; fixed by re-seeding before that specific check rather than changing any product code.

**Limitations, stated plainly:** no live Discord, real WOM API, or actual multi-day account progress was exercised — everything above is sandbox/mocked, by design, since this environment has no network path to the real WOM API. Account-specific XP rate overrides are implemented (not deferred to a future checkpoint), per the spec's preference. The Monitor Max Progress card's progress bar is a rough "skills at 99 / skills with any progress data" visual cue, not a precise XP-weighted percentage — stated as a deliberate simplification, not an oversight.

**Files changed:** `py/wom.py` (new, plus the review-pass fixes: `effective_rate()` returns source, `determine_last_99()` docstring corrected), `ui/wom_goals.py` (new, plus the review-pass fixes: WOM username field, backgrounded history scan, Source column, the `'activity'` adapter bug), `ui/monitor_tab.py` (Last 99 Achieved card, Max Progress card, plus the review-pass `determine_last_99()` fix), `ui/stats_tab.py` (sidebar restructure), `p2p_monitor.py` (WOM config keys, highlight persistence, the same `determine_last_99()` fix applied to the Highlight row's cache fallback, version held at beta.16 — not yet released), `update_manifest.txt` and `install.sh` (both new files registered), `CHANGELOG.md`.

---

## v2.0.0-beta.15
### Targeted bug-fix checkpoint — updater cleanup, Discord channel repair, Farming reason accuracy, Wine of Zamorak death suppression

**Bug fix 1 — Linux updater `.update_tmp_*` cleanup.** Found something more significant than the reported symptom implied: `shutil` was never imported anywhere in `_do_apply_update()`'s scope, so both the staging-dir backup step and the `finally:`-block cleanup were silently raising `NameError` on *every* Linux source update — caught by a bare `except Exception: pass`, so it failed invisibly with no log line at all. That's the real reason these folders weren't being deleted; it wasn't only a crash/interruption case. Fixed with a single module-level `import shutil`, which corrects both broken call sites in one line. Added, additively, a Linux-only startup sweep for any `.update_tmp_*` folders still left over from a genuinely crashed/force-killed run (the one scenario a working in-process `finally:` can never reach, since the process is gone before it runs) — path-safety checked (must resolve to a direct child of the install directory) before anything is ever deleted, with the actual file I/O on a background thread. Windows's differently-named, fixed `.update_tmp` folder (no trailing underscore) is untouched — the pattern doesn't match it, and that flow is gated to Linux only regardless. While building the sandbox test for this, found and fixed a second real bug in my own first draft: calling the existing `self._log()` repeatedly inside a tight background-thread loop (once per removed folder) caused inconsistent deletion in headless testing — rewritten to collect results and log a single summary line after all file I/O completes, which is both safer and matches the original intent of not spamming the log per folder.

**Bug fix 2 — Discord startup/channel repair.** Far more of this already existed than the brief assumed: `DiscordRouter._handle_post_error` already detects a 404 on an actual post, recreates the missing channel/webhook/thread via the existing, already-idempotent `bot_setup_discord()`/`_run_bot_setup()` (checks for an existing channel by name before ever creating one), and retries the original post — that reactive loop was already closing correctly. The real gap was narrower: nothing proactively checked at startup, so a channel deleted while the app was otherwise idle stayed broken until something happened to post to it. Added `verify_bot_channels()` (one cheap `GET /channels/{id}` per configured channel — far lighter than a full guild-channel-list pass, intentionally so it's cheap enough to run unconditionally) and a new `LogWatcher._verify_discord_channels_startup()`, run once per `start()` on its own background thread. If anything's missing, it reuses `_run_bot_setup()` for repair — no new channel-creation logic, no duplicate channels, no API calls at all beyond the cheap check when everything's already valid.

**Bug fix 3 — Farming issue #2 (wrong failure reason).** Traced the exact mechanism: the generic `lock_reason_patterns` scan (`error_rules.json`, shared by every locked task) only looks back 5 lines and stops at the first match. Hand-traced both example logs from the issue and confirmed the existing code would report "Spade" — the true seed shortage sits outside that 5-line window, or gets pre-empted by an earlier single-item tool line. Added a Farming-specific extraction pass (`_extract_farming_lock_reason`, only ever invoked when the locked task name is "farming" — every other locked task keeps its exact existing behavior, untouched) that looks back 20 lines within the same timestamp, and merges signals across affordability lines, bank-shortage lines, and `Many [...]` resource-check lines rather than stopping at the first category found — an early draft of this got that merge wrong and silently dropped a second genuinely-missing seed that only appeared via a different line format than the first; caught by my own test against the issue's second example log and fixed before it shipped. Falls back to tool/teleport lines only when no seed/consumable signal exists at all, and stays silent (matching existing behavior) for a genuinely normal completion with no shortage signals present.

**Bug fix 4 — Wine of Zamorak intentional death suppression.** Exact rule as specified: checks only the previous 2 raw log lines for either `STOP STEALING MY WINE` or `Interacting Wine of zamorak` (case-insensitive substring, no time window, no fuzzy matching). Suppression happens at the source — the death event is simply never appended to `reader.py`'s output — which is sufficient on its own to prevent every downstream effect (Discord ping, death counter, history entry, status/highlight update), since all of those are driven purely by the event's existence; confirmed no other code path independently re-scans raw lines for this same death line. Inferno's own death handling is unrelated (tracked via wave/KC state, not this generic death-line path) and untouched. The death-detection loop now `continue`s past a suppressed wine death instead of `break`ing, so a genuine death later in the same log batch still fires correctly — a small robustness improvement that falls out naturally from adding the check, not a separate change.

**Small addition — death pings now show the current task/activity.** Reused the exact pattern error pings already use (`state.last_task`/`state.last_activity`, with the same "Break" → omit special-case): death events are now enriched with the same `_task_ctx` field error events already carry, and `death_payload()` gained an optional `task_context` parameter that adds a "Task" embed field only when there's something meaningful to show — never shown for a genuinely idle/Break state, same as errors. Entirely independent of the Wine of Zamorak suppression above — a suppressed wine death never reaches this code at all, since the event is never created in the first place.

**Test structure:** the repo has no committed test suite — confirmed directly, no test files or test directory exist anywhere in it. No new test framework or directory added, per scope. All validation below is sandbox-only, run in my own environment, not part of this patch.

**Validated:** `python3 -m compileall -q p2p_monitor.py py ui` — clean. `pyflakes` on every touched file, each diffed by content (not just line numbers, since every file gained real lines) against the beta.14 baseline: `p2p_monitor.py` now shows exactly two fewer warnings (the two real `shutil` bugs just fixed), every other file identical, zero new issues anywhere. Bug 1: a dedicated sandbox test builds a fake install directory with stale temp folders, decoy files, and a Windows-style `.update_tmp` (no trailing underscore) to confirm only the real targets are removed — run 3 times for reliability after the self._log fix, consistently clean; separately confirmed the non-Linux platform path is a no-op. Bug 2: `verify_bot_channels()` tested directly against a mocked `bot_api` (valid/deleted/non-404-error/empty-ID cases); `_verify_discord_channels_startup()` tested end-to-end against a fake watcher across 6 scenarios (nothing missing, something missing triggers repair exactly once, repair failure logs clearly, no token configured, no channels configured, the verification call itself raising). Bug 3: `_extract_farming_lock_reason()` tested against both verbatim example logs from the GitHub issue (confirmed correctly reports the seeds, never Spade/Rake/teleport/cape), plus a normal-completion-stays-silent case, a tools-only fallback case, a same-timestamp-only boundary case, and a full `parse_lines()` pipeline run confirming a non-Farming lock (Woodcutting) is completely unaffected. Bug 4: tested against both verbatim examples from the task (wine death fully suppressed, PK death fires normally) through the full `parse_lines()` pipeline, plus the exact 2-line boundary, either-marker-alone, case-insensitivity, and a two-death-in-one-batch case confirming a later real death still fires after an earlier suppressed one. Death-ping task/activity addition: `death_payload()` tested directly for the field-omitted-when-empty and field-present-with-value cases, plus the exact `last_t`/`last_a`/Break-state computation logic verified to match the watcher.py dispatch code line for line. Full real-`App` construction cycling through all 6 tabs (re-run after every change in this checkpoint, most recently after the death-ping addition) confirmed no crashes. The full prior 8-suite/328-check regression sweep was re-run clean after this final addition too. No live Discord, DreamBot, or actual Linux-update validation was performed — everything above is sandbox/mocked.

**Files changed:** `p2p_monitor.py` (shutil import fix, new startup-cleanup method, version bump), `py/discord.py` (new `verify_bot_channels()`, `death_payload()` gained an optional `task_context` field), `py/watcher.py` (new `_verify_discord_channels_startup()` wired into `start()`; death dispatch now computes and passes through task/activity context the same way error dispatch already does), `py/reader.py` (new Farming-specific reason extraction, new wine-death suppression), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files were added.

---

## v2.0.0-beta.14
### Launcher tab brought into the v2.0 warm dark design + safer relaunch with window-position restore

**The backend was already far ahead of the UI going into this checkpoint.** `py/launcher.py` already had `relaunch_account()` (safe close → wait → relaunch, refuses on ambiguous window matches, never kills by generic process name), `smart_launch()` (relaunch-if-already-open, launch-if-closed), and `launch_all()`/`relaunch_all()` — the watcher already had callbacks wired to all four, presumably for a Discord command. The UI never called any of it; it only ever used the most basic `launch_account()`, in a loop, for everything. This checkpoint is mostly about catching the UI up to backend capability that already existed, plus the one genuinely new piece described below.

**UI redesign**, built against an explicit visual reference: column-header row with icons, sage avatar circles (account initial), muted dark buttons with colored icon+text (sage Launch, amber Edit, coral Delete), solid-green Add Account / Launch Selected, plain inline helper text with no boxed background. The click-to-select-row + "Launch Selected" multi-launch mechanism is preserved exactly as before — same behavior, just reimplemented over Frame rows instead of a `ttk.Treeview` (selection now shows as a thin accent strip on the row's left edge rather than Treeview's native row highlight, since a custom row layout needed its own indicator).

**The per-row Launch button uses `launch_account()` — never silently relaunches an already-open account.** An earlier draft of this checkpoint wired the button to `smart_launch()` (relaunch-if-already-open), which would have closed and reopened a running client just from clicking "Launch" with no confirmation — caught and corrected before release. Clicking Launch on an account that's already open now shows the exact same "already running" message as before, but as a choice instead of a single OK: **Relaunch** (explicit, user-confirmed — only this path ever calls `relaunch_account()`) or **Cancel** (does nothing). The button itself still always just says "Launch," matching the reference image. "Launch Selected" dispatches the same `launch_account()` per selected account, for consistency — if several selected accounts are already open, each shows its own Relaunch/Cancel choice independently. A small/subtle Open/Closed/Unknown status dot sits near each avatar, refreshed via `discover_account_process()` — always off the Tk main thread, and deliberately *not* on a continuous ticker the way Monitor/Status are, since there's no real event stream to hook a live refresh into here; it refreshes once at tab build, once whenever the tab is shown, and once after any launch/relaunch completes. Repeated/overlapping calls to this refresh (e.g. quickly reopening the tab a few times) are now guarded by `_status_refresh_in_flight`, so they can't stack into multiple concurrent background scans — not the old freeze-class bug, just unnecessary duplicate work; the guard always clears on completion, including the no-presets/error cases.

**Fixed: Launch Selected could show the same jar-path error once per selected account.** Jar-path validation is now a single shared helper (`_validate_jar_path()`); "Launch Selected" calls it exactly once before looping and aborts the whole batch with one dialog if it fails, instead of letting each account's own validation pop its own copy of the same error. Single-account Launch is unaffected — it still validates (and errors) exactly as before.

**Fixed: editing an account's name left stale state under the old name.** Delete already cleaned up `_selected`/`_status_cache`/`_in_flight` for the removed account; Edit didn't. Renaming an account via Edit now removes the old name from all three, and transfers it to the new name in `_selected` if it was selected (so "Launch Selected" can never end up targeting an account that no longer exists). Cached status is intentionally *not* transferred to the new name — a fresh detection is cheap and the old cached value was tied to the old window-title match, which may no longer be meaningful. Editing any other field is unaffected — selection and cached status are left untouched when the account name doesn't change.

**The one genuinely new piece: window-position restore on relaunch.** `platform_ops.get_window_geometry()` already existed cross-platform (used elsewhere for paint detection) — there was no matching "set/restore position" function anywhere. Added `platform_ops.set_window_geometry()` (Linux: `xdotool windowmove --sync` + `windowsize --sync`; Windows: `SetWindowPos`), and wired the capture/restore directly into `relaunch_account()`'s existing close→wait→relaunch flow:
- Right when an existing window is confirmed (before anything is touched), its geometry is captured — skipped cleanly with a logged reason if the window is minimized (minimized geometry isn't meaningful to restore) or if the capture call itself fails.
- The restore happens inside the *same* background-thread window-poll that already exists to confirm the new client's PID after relaunch (`_discover_and_cache`) — no second poll loop, no main-thread involvement at any point.
- A conservative sanity check (`_geometry_is_sane()`) rejects clearly-broken coordinates (off by a wide margin, zero/negative size) before ever attempting a restore — deliberately simple rather than precisely multi-monitor-aware; the goal is catching obviously-bad values, not perfectly validating against every real monitor layout. If a saved position fails this check, the restore is skipped and logged rather than guessing a "clamped" position.
- Every step — capture, the restore call itself — is wrapped to log a clear warning and continue rather than ever raising. A missed position restore is never worth failing the relaunch over.
- This lives entirely in `relaunch_account()`, so it applies uniformly to *every* caller — the Launcher tab's Launch button, "Launch Selected," and the existing Discord `/relaunch` command all get it automatically, with no per-caller wiring needed.

**Per-account in-flight guard:** a Launch button disables itself the instant it's clicked and only re-enables once that account's attempt resolves, so a double-click (or "Launch Selected" overlapping a just-clicked single Launch) can never spawn two operations for the same account at once.

**Preserved exactly, byte-for-byte:** `_PresetDialog`'s field set, `_get_preset()`'s data shape, and `_save()`'s validation (only colors/fonts changed) — `launcher_jar`/`launcher_presets` config keys and every existing preset's fields remain fully compatible with old configs. Delete still requires confirmation.

**Caught by the tuple-padding constructor scan, fixed before it shipped:** two instances this time (`tk.Frame(root, bg=app.BG2, pady=(12, 0))` and a sibling one right below it) — same class of bug as previous checkpoints, same fix (move the asymmetric padding to the `.pack()` call). Re-scanned after: zero remaining instances anywhere in the repo.

**Two real bugs caught by my own test suite while building this, both test-harness issues rather than product bugs — but worth being explicit about, given the history on this project of test setup mistakes masking as product bugs:** (1) a test that set `app.cfg['launcher_jar']` directly without also updating the live `_jar_var` the UI actually reads from blocked forever on a real modal `messagebox.showerror` dialog waiting for a click that never comes in headless testing — fixed by setting the UI variable directly, as a real user's Settings/Browse flow always does; (2) a test that cached a row's widget reference, then triggered an unrelated `_refresh_rows()` (which always fully rebuilds — by design, for the simplest possible guarantee against duplicate widgets) before using that stale reference — fixed by re-fetching it fresh. Neither reflects an actual issue in the shipped code.

**Validated:** `compileall`/`pyflakes` clean on all touched files, confirmed against the beta.8-era baseline by content. Tuple-padding scan: zero, after the two fixes above. 20 dedicated checks for the window-position capture/restore logic (happy path, minimized-window skip, capture failure skip, insane-geometry skip, restore-exception handling, fresh-launch path never touching geometry at all, ambiguous-match refusal unaffected by any of the new logic, plus direct `platform_ops.set_window_geometry()` dispatch checks) — all via mocked `platform_ops` calls, no real xdotool/DreamBot needed. 53 live Tk UI checks covering: status dots reflecting open/closed/unknown correctly including the ambiguous-match case; selection toggle and its visual indicator; the in-flight guard actually preventing a second concurrent launch; "Launch Selected" dispatching to every selected account; the already-running flow specifically — confirming `relaunch_account()` is never called from a plain Launch click, the Relaunch/Cancel dialog appears, Cancel closes it with no relaunch, and Relaunch (only) triggers one; the jar-path-validated-once fix — exactly one error dialog and zero launch attempts for a multi-account batch with a bad path; the edit-rename fix — old name removed from and new name added to `_selected`, old name removed from `_status_cache`/`_in_flight`, confirmed separately that editing a non-name field disturbs none of these; the status-refresh in-flight guard — set immediately on start, blocks overlapping calls, clears on completion including the no-presets case, and allows a fresh scan afterward; delete confirmation honoring both Yes and No; no duplicate widgets across repeated rebuilds or tab switches. Separately smoke-tested the Add/Edit dialog directly (data shape on save, pre-population from an existing preset, cancel leaving no result, missing-account-name warning). All 8 prior suites (303 checks) re-run clean. Full real-`App` construction cycling through all 6 tabs twice, including repeated Launcher visits, confirmed no duplicate widgets and no crashes.

**Files changed:** `ui/launcher_tab.py` (full rewrite, plus the four fixes above), `py/launcher.py` (geometry capture/restore wiring into the existing `relaunch_account()`), `py/platform_ops.py` (new `set_window_geometry()` + platform implementations), `p2p_monitor.py` (one new `on_tab_shown` call-wiring line for Launcher), `README.md` (one accurate note that window-position restore now applies to both Discord and Launcher-tab relaunches), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files were added. Version intentionally held at v2.0.0-beta.14 throughout this round of fixes — not yet released, so no bump.

---

## v2.0.0-beta.13
### Fix: the actual cause of the Monitor/app freeze — an exponential timer-doubling bug in StatusTab

**beta.12's fix was real but addressed the wrong problem.** It fixed a genuine main-thread-blocking issue in Monitor's account refresh, but that bug only happens after Start is pressed — it couldn't explain the freeze happening immediately on app launch, before Start is ever reachable, which is what was actually being reported. Diagnosed wrong twice in a row on this one before the real cause was identified.

**The actual bug:** `StatusTab.refresh_session_overview()` had a leftover `app.after(1000, self._tick_overview)` call — a duplicate of the scheduling that `_tick_overview()` itself already does correctly. Every tick produced *two* new scheduled ticks instead of one. Worse, `refresh_session_overview()` is also called directly by `App._start()`/`_stop()` (added in beta.11, specifically to make Status's overview update immediately rather than waiting for the next tick) — every one of those calls spawned its own independent doubling chain too. The result: 1, 2, 4, 8, 16, 32, 64, 128... pending timer callbacks, doubling every second, starting the instant `StatusTab` is constructed during app startup — completely independent of Monitor, Start, or any account data. Within seconds the entire Tk event queue is flooded and the single-threaded UI never gets a chance to process a real click again. Matches exactly what was reported: Monitor loads, the UI "refreshes" to a half-painted state as the queue backs up, and the app never recovers.

This existed since beta.11, introduced as a side effect of the exact fix described in that release's changelog (separating the immediate-refresh call from the ticker) — the separation itself was the right idea, but the old scheduling line wasn't fully removed from the method being separated.

**Fix:** `refresh_session_overview()` is now strictly display-only — it updates labels and nothing else, never schedules anything. Only `_tick_overview()` schedules the next tick, and only via a new guarded `_schedule_overview_tick()` that refuses to schedule a second tick while one is already pending (tracked via `_overview_tick_id`), so no future call site can ever recreate this class of bug by accident, even indirectly. Monitor's equivalent method was checked and confirmed clean — this was isolated to Status.

**Validated:** confirmed by direct experiment that the bug, when deliberately reintroduced, doesn't just slow the app down — it hangs hard enough to time out a 30-second automated test run. Added a dedicated test that counts real pending Tcl `after()` callbacks over 14 seconds of realistic activity (the ticker running, plus repeated direct `refresh_session_overview()` calls simulating Start/Stop) and asserts the count stays flat rather than growing — confirmed flat (3–6 pending callbacks throughout) on the fix, confirmed the same test run against the original buggy code never completes at all. All 247 existing checks across the other six suites re-run clean.

**Files changed:** `ui/status_tab.py` (the fix), `p2p_monitor.py` (version bump only), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files.

---

## v2.0.0-beta.12
### Fix: Monitor freeze on Start (Linux) — Active Accounts refresh was blocking the main thread

**The bug, exactly as reported:** opening Monitor and pressing Start made the whole app completely unresponsive. Root cause: beta.11 added a "Highlights" row and an Active Accounts summary to Monitor, refreshed via `MonitorTab.refresh_highlights()`. That method calls `watcher.get_account_rows()` — which does real filesystem work (checking every monitored account's log directory for rotation) — directly, with no background thread. `StatusTab.refresh()`/`push_refresh()` already wrap that exact same call in a background thread; `refresh_highlights()` didn't.

That wouldn't matter if it only ran at safe moments, but it's also the target of `App._debounced_refresh_tick()` — the same 2-second debounce that's existed for a while (collapses a burst of events into one refresh, rather than refreshing once per event) — and that tick fires on the main/Tk thread, same as every `self.after(...)` callback. So every time the debounce settled — and it settles almost immediately after the startup catchup scan, which can replay a burst of historical events the moment you press Start — `get_account_rows()` ran synchronously on the main thread, freezing the entire UI (Tkinter is single-threaded; nothing else can happen while one call is running) for however long that disk check took.

**Fix:** `refresh_highlights()` now follows the exact same shape as `StatusTab.refresh()`: a background thread does the actual `get_account_rows()` call, then hands the result back to a new `_apply_highlights_data()` method via `app.after(0, ...)` for the actual widget updates (Tkinter widgets aren't thread-safe, so that part has to stay on the main thread). Added the same in-flight guard Status already has (`_accounts_refresh_in_flight`), so a fast burst of events can't spawn a pile of overlapping background checks — only one fetch runs at a time; extra calls while one's in flight are just skipped, since the in-flight one will land soon anyway.

Nothing about *what* gets checked, *how often* the debounce fires, or any other behavior changed — this is purely moving one disk-reading line off the main thread, the same pattern already proven correct elsewhere in this same file.

**Validated:** `compileall`/`pyflakes` clean. Tuple-padding scan: zero. Added 6 new targeted regression checks directly proving the fix: a simulated slow disk read (300ms) confirmed `refresh_highlights()` itself still returns near-instantly; a heartbeat timer confirmed the main thread keeps servicing other `after()` callbacks throughout that slow read (this is exactly what was frozen before); confirmed the slow background fetch still correctly updates the UI once it completes; confirmed the in-flight guard allows only one background fetch at a time under a rapid 5-call burst, and that it correctly resets and allows a fresh fetch afterward. All 43 pre-existing Monitor checks plus all 207 checks across the other six suites re-run clean. Separately reproduced the exact real-world scenario end-to-end — real `App`, real `_start()`, a deliberately slow fake watcher, a 20-event burst, and a heartbeat ticking every 20ms — confirmed the heartbeat fired continuously throughout (149 times in 3 seconds, no stall) where before the fix it would have stopped dead for the duration of every slow disk check.

**Files changed:** `ui/monitor_tab.py` (the fix), `p2p_monitor.py` (version bump only), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files.

---

## v2.0.0-beta.11
### Monitor, Status, and History brought into the v2.0 warm dark design — full UI rebuild on all three, all existing behavior preserved

All three tabs were still on the very first, pre-redesign layout (plain monospace-everything, neon-tinted status colors, a single `ttk.Treeview` for Status and for History). This release brings them in line with Stats/Settings — card-based layout, sans-serif UI text (monospace kept only for the raw event log), the warm sage/amber/coral palette throughout.

**One real data-honesty finding before any of this got built:** the originally-discussed "Session Overview" card (Status / Uptime / Started / **Script** / **Profile** / **Runtime**) doesn't have real data behind 3 of those 5 fields anywhere in the app — there's no single global "current script" (the app monitors multiple accounts simultaneously, each running independently), no "profile" concept anywhere in config or per-account state, and the closest thing to a client "runtime version" is a window-title parser that only runs transiently every 6h for the update-checker and is never persisted. Rather than fabricate those three, the card ships as **Status / Uptime / Started / Events** — all four genuinely real, the last one a simple sum of the existing per-type counters. Identical card on both Monitor and Status, reading the same underlying `app._session_start_ts` / `app._counts` / `app._highlights`, kept in sync via one shared immediate-refresh call from `App._start()`/`_stop()` (not just the two tabs' independent 1-second tickers, which would otherwise leave a momentary mismatch between them).

**Part A — Monitor.** Full visual rebuild: Session Control card (Start/Stop — same `app._btn_start`/`_btn_stop`, same `App._start()`/`_stop()` wiring, untouched), the Session Overview card described above, an Active Accounts card, a 7-card stat strip (same `app._sv` counter dict, same keys, same `App._on_event()` wiring), a new Highlights row (Latest Task / Last Level Up / Last Error / Latest Drop / Active Accounts — fed from a new `app._highlights` dict populated directly inside the existing `_on_event()` callback, never fabricated, dropped entirely on Start), and the event log itself gets a new event-type filter dropdown and a debounced search box. Both work via Tk Text `elide` tags on whole-line category tags added in `App._log()` — the existing emoji-based classification and per-tag coloring there is completely untouched; one parallel tag was added alongside it, that's all. Hover-search and the category filter never trigger a log rebuild, just show/hide existing text ranges.

**Part B — Status.** The single `ttk.Treeview` is replaced with real Frame-based account-row cards — necessary to get actual button widgets, colored status badges, and avatar circles, none of which a Treeview cell can render. Safe perf-wise: account counts here are small (a handful of concurrently-monitored accounts), nothing like History's scale. `refresh()` / `push_refresh()` / `on_tab_shown()` keep their exact names and no-arg signatures (same three call sites in `p2p_monitor.py`/`status_tab.py` itself), same background-thread-then-`app.after(0,...)` pattern, same smart-diff update rule (full rebuild only when the account *set* changes; otherwise every row updates in place — this is what keeps "no duplicate widgets" true). Mute/Screenshot/double-click-for-history now bind directly to real per-row widgets instead of Treeview column-position click detection, which is strictly more robust, not a behavior change. Added the same Session Overview card as Monitor (mirrored, same source data) plus an Accounts Overview card and a 4-card stat strip (Active/On Break/Logged In/Muted).

**Part C — History.** Deliberately a hybrid, not a uniform rebuild: account headers (few — one per monitored account) become real Frame cards with avatar circles and genuine Summary/Runtime Stats buttons. Event rows *within* an account (potentially hundreds) stay a per-account `ttk.Treeview` — rebuilding those as individual widgets would risk the exact "large history freezes the UI" regression this checkpoint explicitly warned against. Each account's Treeview is now also genuinely lazy: a collapsed account costs nothing, its rows are only ever inserted once you expand it. `load()` / `append_entry()` / `focus_account()` / `on_tab_shown()` keep their exact names and signatures; `py/history.py` (file format, writing, parsing, dedupe, backfill, runtime-stats computation) is completely untouched. Added a search box (matches account name, task, or activity/details — a content match narrows that account's rows, a name match shows the whole account; an account with nothing matching the active filter(s) is hidden, not shown empty) and an event-type filter dropdown, both pure presentation-layer filters over the already-loaded in-memory cache — no new disk reads. Added a Severity classification (Error/Success/Info) derived from the existing `etype` field, shown as a colored-dot-plus-label in its own column — doesn't touch event semantics, just a read of data that was already there. The old fragile "detect which pseudo-button was clicked by x-position within a treeview cell" heuristic for Summary/Runtime Stats is gone by construction, replaced with real buttons. Single-click an account row to expand/collapse (was double-click); double-click an individual event row now opens a small full-detail popup — both match the agreed mockup behavior precisely. Date-range filtering (MM/DD/YY entry, 7-day max, auto-sync From→To) and column-width persistence (`hist_col_widths`) are preserved exactly, just retthemed.

**Caught by the tuple-padding constructor scan, twice — both fixed before they shipped:** `tk.Frame(card, bg=app.BG3, padx=12, pady=(0, 10))` in History's per-account body frame, and an equivalent one already covered last pass. Re-scanned after each fix: zero remaining instances.

**One thing fixed in passing, not by request:** while building Status's mirrored Session Overview card, found that it only ever updated via its own 1-second ticker with no immediate refresh on Start/Stop (unlike Monitor's, which I'd wired directly). Refactored `StatusTab._tick_overview()` to separate the actual display-update logic (`refresh_session_overview()`, matching Monitor's method name) from the reschedule, so `App._start()`/`_stop()` can call it directly for an instant update without spawning a second parallel ticker chain.

**Found two genuinely-dead pieces of code while writing this and removed them on the spot** (not a request, just cleanup of things this exact rewrite introduced and then immediately stopped using): an unused `TYPE_BADGE_COLOR`-style placeholder dict in `status_tab.py`, and a duplicate inline type→color mapping in `history_tab.py` that should have just called the `_type_color()` method already sitting right next to it.

**Validated:** `compileall`/`pyflakes` clean on all three touched UI files. Confirmed zero *new* warnings in `p2p_monitor.py` — diffed against the beta.8-era baseline by content (not just line numbers, since this pass added real lines): identical set. Tuple-padding constructor scan: zero instances after the two fixes above. Live Tk suites: 43 new Monitor checks (Start/Stop wiring, highlight tracking from real `_on_event` calls, category-filter elide behavior, debounced search hiding non-matching lines, Clear Log, uptime freezing correctly on Stop), 40 new Status checks (empty state, smart-diff in-place updates vs. full rebuild on account-set change, mute/screenshot click handlers, double-click→History, the 60s uptime ticker, the push_refresh in-flight guard), 41 new History checks (lazy tree population on expand only, severity classification, type-filter and search-filter correctness including the bug described below, sort/reverse-sort, `focus_account`, column-width persistence) — all seeded with real `py.history.append_history()` calls into a disposable sandbox history dir, not mocked. All four pre-existing suites (Stats ×2, tooltip, Settings — 117 checks) re-run clean against these changes. Full real-`App` construction (pystray stubbed — no GTK/tray backend in this sandbox, unrelated to this change) cycling through all 6 tabs twice confirmed no duplicate widgets and no crashes anywhere in the shell.

**A real bug caught by my own test suite, not shipped:** the History event-type filter (e.g. selecting "Error") correctly filtered each account's *events*, but only skipped showing an account entirely when the *search* box was what emptied it — selecting a type filter alone with no search active left every other account visible with an empty, pointless card. Fixed before this ever reached a zip.

**Files changed:** `ui/monitor_tab.py`, `ui/status_tab.py`, `ui/history_tab.py` (all three fully rewritten), `p2p_monitor.py` (version bump + the new session-start/highlights bookkeeping + one new shared debounce-refresh method — additive only, no parser/watcher/Discord logic touched), `CHANGELOG.md`. `README.md` not touched — nothing in this pass changed any documented behavior. `update_manifest.txt` unchanged — no new files were added.

---

## v2.0.0-beta.10
### Settings tab redesign (sidebar + 5 cached pages) + Stats chart x-axis label collision fix

### Revision pass (same version — not re-tagged, per "don't bump until shipped")

1. **Daily Levels chart hover tooltip** — restored, native to the Tk Canvas chart (not matplotlib). Hovering near a plotted day (8px hit radius, nearest-point selection for dense charts) shows a themed tooltip: formatted date, total levels, and a per-account breakdown line per account (sage/olive bullets, amber/gold total). Per-account breakdown is computed directly in `ui/stats_tab.py` from the same already-filtered rows used for the chart/KPIs — `py/stats.py`'s aggregation functions are untouched. Hover never triggers a chart redraw; only `chart_tooltip`-tagged canvas items are touched, and redraws are skipped entirely when the nearest point hasn't changed since the last `<Motion>` event, so dense data doesn't flicker.
2. **Re-verified the x-axis label collision fix** (added last pass) still holds after the tooltip change — same 9-check suite re-run clean, no regression.
3. **Settings: removed the always-visible scrollbar** that made every page feel like a scroll panel even when its content fit fine. The scroll container is now conditional: no scrollbar at normal window sizes, but one appears automatically (with mousewheel scrolling) if the window is shrunk small enough that content would actually be clipped, and disappears again once restored — a real measured-height comparison, not a guess.
4. **Discord Alerts: Bot Setup Instructions moved out of an inline collapsible into a "📖 Show Setup Guide" button** that opens the same instructions in a themed, scrollable popup window (`tk.Toplevel`, modal). This was the more decisive of the two options on the table — it removes the height/scroll pressure from the main Discord page entirely, not just partially, so that page (like the other four) never needs scrolling at a normal window size. Never runs bot setup itself — purely informational. The now-unused inline-disclosure helper was removed.
5. **Added "Open" buttons** next to the session debug file path and a newly-added Config file path row on General Settings → Debug, using the existing (previously unused anywhere) `open_path()` from `platform_ops.py`. A button disables itself with a clear "(not created yet)" label instead of silently no-op'ing if the target path doesn't exist (e.g. debug logging has never been turned on). No Refresh Remote Error Rules / Refresh Inferno Rules buttons were added — explicitly out of scope for this pass.
6. **Hide Paint Overlay grid made more compact** — 6 columns / 2 rows instead of 4 columns / 3 rows, smaller font on the checkbox labels. Same 11 checkboxes, same config keys, same default values, same behavior — purely a layout density change.
7. **README.md correction:** last pass's README changes (fixing stale `Settings → X` path references after the redesign) were real and intentional, but I'd failed to call them out in the delivery summary, which looked like an accidental inclusion. Nothing in *this* revision pass touches README further, so it's excluded from this patch zip.
8. **Tiny cleanup:** searched thoroughly (exact-match and near-duplicate line scans, plus a cross-file check) for the specific duplicates called out — a duplicate `lbl.configure(...)` in section-switching and a duplicate `total = h * 60 + m` in `save()`. Neither exists in the actual shipped code; both appear exactly once, where expected. Did remove one real piece of dead code this pass directly caused: the now-unused inline-disclosure helper (see item 4).

**Caught by the tuple-padding constructor scan again, fixed before it shipped:** the new Show Setup Guide modal's `tk.Frame(win, bg=app.BG2, padx=16, pady=(0, 12))` — same class of bug as last pass, same fix (move the asymmetric padding to `.pack()`). Re-scanned afterward: zero remaining instances.

**Validated (this pass):** `compileall`/`pyflakes` clean on both touched files (`ui/stats_tab.py`, `ui/settings_tab.py`) — same single pre-existing `import discord` pattern as before, no new warnings. Tuple-padding scan zero after the one fix above. Live Tk suites: 23 new tooltip checks (point metadata population, hover-shows/leave-hides, anti-flicker on small mouse movement, hit-radius boundary exactly at 8/9.5px, dense nearest-point selection, tooltip box clamped within canvas bounds, works across 7D/30D/ALL, survives an empty-filter chart without crashing); 39+9 existing Stats checks re-run clean; 46 Settings checks (up from 27) covering the modal (opens exactly once, never runs setup, themed, closes cleanly), conditional scrollbar (absent at normal size on all 4 non-Discord pages, appears when the window is shrunk small enough to overflow, disappears once restored), the new Open buttons (calls `open_path()`, disables cleanly for a nonexistent path), and the 6-column/2-row hide-paint grid (still saves/loads correctly).

One methodology note for the record: partway through writing tests for this pass I discovered three of my own test files still pointed at a stale snapshot directory from an earlier verification step (a leftover `sed` substitution I'd never reverted), so an earlier "re-run the regression suite" in this same session was actually validating old code, not these changes. Caught it, fixed the path in all four test files, and every suite mentioned above was re-run against the real current code afterward.

**Files changed this pass:** `ui/stats_tab.py` (tooltip only), `ui/settings_tab.py` (items 3–6 + the tuple-padding fix), `CHANGELOG.md`. `p2p_monitor.py` and `README.md` not touched this pass. `update_manifest.txt` unchanged — no new files.

---

**Part A — Stats: fixed a dense-data x-axis label collision on the Daily Levels chart.** On wide ALL-range datasets, the label-thinning logic always force-included the actual last day even when it landed only a few pixels from the previous natural tick (e.g. `06-09-26` / `06-18-26` visually merging together). Added a collision-aware selection pass: the first and last labels are always kept, and middle candidates are greedily dropped — right-to-left from the always-kept last label, then left-to-right from the always-kept first label — using each label's *actual measured pixel width* (via a cached `tkinter.font.Font`, not a guessed constant) as the required gap. Adapts automatically to font or date-format changes. No change to date values, aggregation, filtering, donut, Top Accounts, or KPI behavior — `ui/stats_tab.py` only, this one method's label loop.

**Part B — Settings: full redesign**, replacing the old single giant scrollable expand/collapse layout with a left-side section nav and 5 cached pages shown via `tkraise()` — same cached-frame pattern as the main tab shell and Stats. All 5 pages build once, eagerly, when the Settings tab is constructed (no lazy per-section build — Settings has no expensive disk/network work at build time, so saving and loading stay simple and always cover every setting regardless of which section is currently visible).

Final section structure (mockup-driven — supersedes an earlier, more granular 7-section draft):
- **General Settings** — DreamBot Logs Folder, Monitoring Intervals, **Manual Update Check** (the app's own self-update — renamed from the old, misleading "AUTO-UPDATE" label, since it prompts for confirmation and never installs silently), Debug (now also shows the session debug file path), Paint Reference.
- **Discord Alerts** — Bot Setup (with a "Show Setup Guide" button opening the instructions in a themed popup, kept out of the main page entirely), Webhooks (always visible as its own card).
- **Event Notifications** — Script Events, the per-event Notify/Screenshot/Ping grid, Notify-every-N-levels, and the hide-paint-overlay grid (unchanged from its previous location).
- **Daily Summary** — daily summary + all screenshot scheduling toggles (moved here from where they previously sat, ungrouped, lower in the old layout).
- **Restarts & Updates** — Auto Restart + **Update Awareness** (the DreamBot/P2P Master AI script-update checker — kept as its own clearly-separate group from General's Manual Update Check, since these are two unrelated systems that previously had confusingly similar names).

**Every existing config key, default, and behavior is preserved** — this was a UI/organization checkpoint, not a settings change. `save()`/`load_fields()` still iterate over the same `self._vars` dict exactly as before, so the app's existing `self._settings.save()` call (fired from Monitor's Start button) keeps working unchanged. The three `ui_section_*_open` config keys from the old collapsible layout are no longer wired to anything (there's nothing left to collapse in a sidebar), but are intentionally left in `DEFAULT_CFG` rather than removed, preserving compatibility with existing user configs.

**Found and fixed one unrelated pre-existing bug while relocating the code it lives in:** `_manual_bot_setup()`'s discord.py auto-install path used `sys.executable`, but `sys` was never imported anywhere in this file. A Linux source-install user without discord.py clicking "Run Bot Setup" would have silently failed the install step with a confusing `name 'sys' is not defined` message instead of actually installing the package (caught by a surrounding try/except, so it never crashed — it just quietly didn't work). Added the missing import. Also dropped a redundant duplicate `import subprocess` that existed inside the same method alongside the existing top-level one.

**Simplification from the original mockups, by agreement:** boolean settings use plain styled checkboxes throughout rather than custom toggle-switch or pill/chip widgets — Tkinter has no native toggle switch, so matching that pixel-for-pixel would mean a custom Canvas-drawn widget with its own cross-platform testing surface for no functional gain. Copy-to-clipboard buttons (Server ID, Mention ID, debug file path) and a password-reveal toggle (Bot Token) from the mockups were also skipped this checkpoint — plain fields, reorg only.

**Caught by the tuple-padding constructor scan, fixed before it ever shipped:** the new collapsible Bot Setup Instructions block had `tk.Frame(holder, bg=app.BG3, padx=16, pady=(8, 10))` — a tuple passed to a Tkinter widget *constructor's* `pady` (which only accepts a scalar), the exact class of bug that caused the beta.5 duplicate-Stats-section incident. Moved the asymmetric padding to the `.pack()` call instead, where it belongs. Re-scanned afterward: zero remaining instances anywhere in the repo.

**Validated:** `py_compile` clean across the whole repo. `pyflakes` on `ui/settings_tab.py` down to a single pre-existing-pattern warning (the intentional `import discord  # noqa: F401` existence-check) — the previous version's `subprocess`-unused, `subprocess`-redefinition, and `sys`-undefined warnings are all gone, the first two resolved naturally by the rewrite and the third by the explicit fix above; confirmed zero *new* warnings in `p2p_monitor.py` beyond the pre-existing beta.8-era set (diffed line-for-line). Tuple-padding constructor scan: zero instances (after the one fix above). Live Tk validation under Xvfb against the real `SettingsTab` class: all 5 sections build with no duplicate widgets across repeated nav switches; every `DEFAULT_CFG` key with a Settings UI control is bound to a variable; active/inactive nav highlighting confirmed; unsaved value changes survive switching sections away and back; `save()` confirmed to persist values from pages other than the currently-visible one (not just the active page) and still correctly clamps the update-check interval to its 1-minute–24-hour range; `load_fields()` round-trips cfg → UI correctly; the logs-root account-folder warning is still wired; Show Setup Guide opens the bot setup instructions correctly. Separately constructed the real `App` shell end-to-end (pystray stubbed — this sandbox has no GTK/tray backend, an environment limitation unrelated to this change) and confirmed `app._settings.save()` — the exact call Monitor's Start button makes — works correctly through the real wiring, and `show_tab('Settings')` raises the new tab cleanly. Stats regression suites (beta.9's 39-check suite + the new 9-check label-collision suite) re-run clean against the Part A change.

**Files changed:** `ui/settings_tab.py` (full rewrite), `ui/stats_tab.py` (label-collision fix only), `p2p_monitor.py` (version bump only), `CHANGELOG.md`. `update_manifest.txt` unchanged — no new files were added.

---

## v2.0.0-beta.9
### Stats tab: matplotlib removed entirely — native Tk Canvas charts on every platform, plus per-panel filter fixes and grouping/layout cleanup

Consolidated entry. Beta.9, beta.10, and beta.11 were all worked through this same Linux Stats crash (and the native-canvas rewrite that followed) but none of them were ever tagged or shipped, so this single beta.9 release covers the whole arc plus the cleanup pass requested on top of it: full matplotlib removal, top-8+Other grouping, dynamic donut sizing, per-panel filter semantics, nice y-axis ticks, chart padding, and MM-DD-YY axis dates.

**Background — how we got here:** the Stats chart/donut originally used matplotlib's `FigureCanvasTkAgg`. On at least one real Linux install, every chart/donut render crashed with `FT_Render_Glyph ... raster overflow`. Root-caused to `canvas.draw()` being called before Tk's geometry manager had given the canvas real pixel dimensions; deferring the draw until after data loads, plus unbinding matplotlib's own `<Map>`-triggered DPI rescaling, narrowed it further, but the underlying fragility remained. Rather than keep chasing matplotlib/FreeType internals, the chart and donut were rewritten with plain `tkinter.Canvas` primitives on Linux (`create_line`/`create_oval` for the line chart, `create_arc(style=PIESLICE)` + a solid center circle for the donut hole) — Tk's own text rendering never touches matplotlib's FreeType wrapper, so it's immune to this class of bug by construction. Windows kept the matplotlib path at that point.

**This release finishes the job — matplotlib is gone, not just bypassed on Linux:**
1. **Removed matplotlib entirely from `ui/stats_tab.py`.** The native Tk Canvas chart/donut is now the only rendering path, Linux and Windows alike — no more dual code paths, no `USE_NATIVE_CHART`/`MATPLOTLIB_AVAILABLE` flags, no `_theme_chart_axes()` (matplotlib-only), no "matplotlib not installed" fallback label.
2. **Removed the dependency everywhere it appeared:** `requirements-linux.txt`, `requirements-windows.txt`, and the `matplotlib.backends.backend_tkagg` hidden-import (plus its stale `import matplotlib` verification line) in `p2p_monitor.spec`. `python3-tk`/Tkinter requirements are untouched — still required, never were matplotlib's concern. `install.sh` needed no change — it already installs from `requirements-linux.txt` rather than referencing matplotlib directly. `update_manifest.txt` needed no change — it already tracks `requirements-linux.txt`; `requirements-windows.txt`/`p2p_monitor.spec` are Windows-build-only files and aren't part of the Linux source-updater's file set by existing design.
3. **Levels by Skill panel — top 8 instead of top 5,** remaining skills collapsed into a single trailing `Other (N skills)` entry (unchanged below 9 distinct skills — no `Other` bucket added when there's nothing to collapse). Percentages are based on the total levels shown in that panel.
4. **Donut sizing is now dynamic, not a fixed guess.** The donut frame is resized on every redraw to match the *real rendered height* of the skill-bar rows next to it (now up to 9 rows: top 8 + Other), so it stays visually aligned whether 1 row or 9 are showing, on either platform, regardless of font metrics/DPI. Safe fallback + single `after_idle` retry if Tk hasn't finished laying out the panel yet (e.g. the very first build, before a mainloop pass) — guarded against runaway rescheduling. Added a reentrancy guard around the donut redraw itself: `update_idletasks()` processes Tk's *entire* idle queue, not just the widget it's called on, so a reentrant resize-retry firing mid-redraw could otherwise nest indefinitely under rapid filter changes.
5. **Per-panel filter semantics, fixed to match how each panel is actually used:**
   - KPI cards + Daily Levels chart: obey Account + Skill + Date range (unchanged).
   - **Levels by Skill: now obeys Account + Date range but ignores Skill** — previously it obeyed all three filters, which meant selecting a skill collapsed this panel to a single, useless slice.
   - **Top Accounts: now obeys Skill + Date range but ignores Account** — previously it obeyed all three, meaning selecting an account collapsed this panel to a single row. Example: Skill = Agility, Date = 30D now ranks every account by Agility level-ups in the last 30 days, regardless of any Account filter selected.
6. **Chart polish:** y-axis ticks now round to a clean step (1/2/5/10/20/25/50/100/...) instead of dividing the max value into raw fractional chunks (previously e.g. max=23 produced ticks 0/5/9/14/18/23 — now 0/5/10/15/20/25). Right padding increased and the first/last x-axis labels now anchor to their own edge instead of centering, so the rightmost date label can no longer run off the canvas; the actual last day is now always included among the labeled ticks (previously the label-thinning step could skip past it). Whole-number-only chart still in place. Default date range remains ALL; Account/Skill combobox text visibility unaffected.
7. **Daily Levels Gained chart x-axis dates now display as `MM-DD-YY`** (e.g. `06-19-26`) instead of `YYYY-MM-DD`. Display-only — the underlying stored date strings (still `YYYY-MM-DD`, needed for correct sorting/filtering) are untouched; only the text drawn on the chart's x-axis changed.

**Scope respected:** Monitor, Status, History, Launcher, Settings, the log parser, Discord routing, and updater behavior were not touched. `ui/stats_tab.py` is the only behavioral change; `p2p_monitor.py` got only a version bump and matplotlib-wording cleanup in comments/docstrings (no logic change).

**Validated:** `py_compile` across all touched files; `pyflakes` clean on `ui/stats_tab.py` (pre-existing, unrelated warnings in `p2p_monitor.py` confirmed identical to beta.8 — not introduced here). Live Tk suite under Xvfb against the real `StatsTab` class with synthetic multi-account/multi-skill data: zero matplotlib imports/flags remaining; date presets (7D/30D/90D/1Y/ALL) and Refresh all complete cleanly with no duplicate widgets; top-8+Other grouping confirmed (9 rows, correct trailing count) alongside a ≤8-skill dataset correctly producing no `Other` bucket; donut frame height matches the live-measured skill-panel height and stays square; resize-retry guard confirmed to fall back safely and schedule exactly one retry, never more; Levels by Skill confirmed to ignore the Skill filter while still obeying Account; Top Accounts confirmed to ignore the Account filter while still obeying Skill (row counts cross-checked against `py.stats` aggregation directly); nice-tick-step values spot-checked (23→5, 47→10, 3→1, 180→50); `MM-DD-YY` formatting confirmed both on the helper directly and on actual rendered chart canvas text; no chart text found extending past the canvas's right edge; empty-state, build-error/Retry, and forced chart-render-failure fallback all still behave correctly; revisiting the tab (Stats → Monitor → Stats) confirmed instant with no rebuilt/duplicated root frame. Tuple-padding constructor scan across `p2p_monitor.py` and every `ui/*.py` file: zero instances.

**Files changed:** `ui/stats_tab.py`, `p2p_monitor.py` (version bump + comment cleanup only), `requirements-linux.txt`, `requirements-windows.txt`, `p2p_monitor.spec`, `CHANGELOG.md`.

---

## v2.0.0-beta.8
### Stats prewarm disabled on Linux; race guard + failure cleanup added

**Linux**: prewarm disabled entirely for now. Despite beta.7's prewarm being verified data-only (no widgets/matplotlib touched — confirmed via live test), Linux still showed duplicate Stats sections, pointing at a race not yet pinned down. Per explicit instruction: stability over first-click speed. `_prewarm_stats()` now returns immediately on Linux.

**Windows**: prewarm unchanged, still active.

**New defense-in-depth in `StatsTab` (both platforms):**
- `_building` lock in `_ensure_built()` — a second call while a build is in progress is now a no-op instead of racing.
- If `_build_real_content()` throws partway through, all partial widgets are destroyed and a placeholder is restored before the lock releases — no broken UI left behind, and a later call can retry cleanly.
- Verified live: rapid double `on_tab_shown()` calls produce exactly one filter row; a forced build failure leaves no widgets behind and a subsequent retry recovers correctly.

**Files changed:** `p2p_monitor.py` (version; Linux guard in `_prewarm_stats`), `ui/stats_tab.py` (`_building` lock + cleanup in `_ensure_built`).

---

## v2.0.0-beta.7
### Fix: Stats prewarm caused duplicate Stats sections on Linux; chart polish

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3. Scoped to `ui/stats_tab.py` and `p2p_monitor.py`'s prewarm wiring only.

**Fix: Linux showed two stacked Stats sections after prewarm**
- Root cause: beta.6's prewarm built the *real* Stats widgets — including constructing and immediately `draw()`-ing matplotlib canvases — inside the Stats tab's frame while a different tab was the one actually raised via `tkraise()`. On Windows this happened to work; on Linux, drawing into a frame that isn't yet mapped/realized (and therefore may not have real allocated screen dimensions yet) could fail partway through matplotlib's Agg/FreeType text rendering — consistent with the "FT_Render_Glyph raster overflow" warning. Because the failure happened *inside* `_build_real_content()`, `self._built` was never set to `True`, so the next real visit to the tab ran the entire build again on top of the broken partial one — two stacked copies of the whole tab, exactly as described.
- Fixed by making `StatsTab.prewarm()` **data-only**: it now does nothing but load and cache levelup rows on a background thread. It never creates a single Tkinter widget, never constructs a matplotlib `Figure`/`Canvas`, and never calls `_build_real_content()`. Building the actual UI is now reserved entirely for `on_tab_shown()` — the one moment the Stats frame is guaranteed to have real screen dimensions, because it's in the process of being `tkraise()`'d.
- `_ensure_built()` now consumes the prewarm cache when it exists (`self._prewarm_rows`), so a prewarmed manual open skips a redundant disk read instead of reloading from scratch — prewarm still pays off, it just never touches anything visual.
- A failed prewarm (verified directly by forcing `load_levelup_rows()` to raise) now leaves **zero** widgets behind and caches an empty list rather than leaving the tab in a broken state; a subsequent real open still works normally.
- This removes the entire class of risk by construction rather than chasing the exact FreeType trigger condition — there is nothing in the prewarm path anymore that can build or draw into an unrealized widget.

**Chart polish**
- Daily Levels Gained y-axis now uses `matplotlib.ticker.MaxNLocator(integer=True)` — whole-number ticks only (0, 1, 2, 3...), never fractional ticks like 0.5/1.5, since you can't gain half a level in a day. The Average Per Day KPI card is unaffected and still shows one decimal place.
- Default date range on first Stats load is now **ALL** (previously 30D) — the ALL pill is highlighted by default; 7D/30D/90D/1Y are still one click away.

**Re-verified, not just assumed:**
- Combobox visibility fix (beta.6) — still correct in every state.
- Chart and donut dark theming (beta.6) — still correct; figure, axes, and the underlying Tk canvas widget's own background all confirmed non-white for both charts.
- Skill donut + grouping (beta.6), including the exact boundary: 5 skills produces no "Other" row, 6 skills produces "Other (1 skills)".
- Dependency restart prompt (beta.5) — untouched, its own live test still passes unchanged.
- Repeated Stats → Monitor → Stats switching, Account/Skill filters, date-range buttons, and Refresh all still produce exactly one filter row with zero widget duplication.

**Validation:**
- `python -m compileall .` — clean.
- `pyflakes` on `ui/stats_tab.py` (zero) and `p2p_monitor.py` (still the 14 pre-existing baseline warnings, zero new).
- Tuple-padding constructor scan re-run across `p2p_monitor.py` and every `ui/*.py` — zero instances.
- Live Tk test (real Xvfb display): 22 new checks specifically targeting the data-only prewarm contract (zero widgets created, cache consumed correctly, failure leaves nothing behind) plus the y-axis/default-range/grouping-boundary fixes, plus the existing 40-check beta.6 suite re-run (updated to reflect the corrected two-phase prewarm flow) and the empty-state/beta.4-full-flow/beta.5-restart-dialog suites — all passing, 92 live checks total across 5 test files.

**Files changed:**
- `ui/stats_tab.py` — `prewarm()` rewritten to be data-only; `_ensure_built()` consumes the prewarm cache; integer y-axis ticks; default date preset changed to `ALL`
- `p2p_monitor.py` — version 2.0.0-beta.7; `_prewarm_stats()` docstring corrected to describe the data-only design
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.6
### Stats tab polish: combobox visibility, chart theming, skill donut, prewarm

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3. Scoped entirely to the Stats tab and the shared `ttk.Style` Combobox configuration (the only current consumer of `ttk.Combobox` in the app) — no other tab, parser, Discord, or updater behavior changed.

**Fix: Account/Skill combobox text was unreadable when not focused**
- Root cause: the `clam` ttk theme has its own built-in state-based color maps for `Combobox` that silently override a plain `configure()` call for certain states (notably `readonly` and `!focus`) — exactly the states the dropdowns sit in most of the time. `configure()` only sets the *default* style; per-state `.map()` entries are required to actually force a color to stick in a specific state.
- Added explicit `.map('TCombobox', ...)` entries covering `readonly`, `disabled`, `focus`, and `!focus` for `fieldbackground`, `foreground`, `selectbackground`, `selectforeground`, `background`, and `arrowcolor`.
- The dropdown's open popdown list is a plain Tk `Listbox`, not a ttk widget — it doesn't inherit `ttk.Style` at all. Added `self.option_add('*TCombobox*Listbox...')` entries so the opened-dropdown list is themed too, not just the closed field.
- Verified directly: looked up `ttk.Style().lookup('TCombobox', 'foreground', state)` against the real, unmodified `App._style()` method for every relevant state combination (default, readonly, focus, `!focus`, and specifically `readonly + !focus` — the exact combination that was broken) and confirmed foreground never resolves to the same color as the field background in any of them.

**Fix: Linux chart area rendered as a plain white rectangle**
- Root cause: the figure's facecolor was only set once at initial build, the underlying Tk `Canvas` widget's *own* background (separate from matplotlib's figure/axes facecolor — a distinct layer) was never touched at all, and `ax.clear()` (called on every redraw) resets most per-Axes styling back to matplotlib's light-theme defaults. Any gap between the widget's first paint and a fully-themed redraw could show through as Tk's default white canvas background — apparently more visible/reproducible on Linux than Windows.
- Added a shared `_theme_chart_axes(fig, ax, canvas_widget)` that forces figure patch, axes facecolor, tick colors, label colors, title color, spines, *and* the Tk canvas widget's own `bg` — called at initial build and again at the top of every redraw (after `ax.clear()`), not just once.
- Switched `canvas.draw_idle()` → `canvas.draw()` (forced, synchronous) so the chart never sits unpainted waiting for an idle slot, and added an explicit `canvas.draw()` immediately after initial construction so the canvas is never left showing an unrendered default background before the first real data load completes.
- Same fix applied to the new skill donut chart below, since it shares the identical underlying mechanism.

**Levels by Skill panel: donut chart + grouped "Other" bucket**
- Added a donut chart on the left side of the "Levels by Skill" card; horizontal skill bars stay on the right, now with a small color swatch per row matching the donut's wedge colors.
- New `group_top_n_with_other()` in `py/stats.py` (pure, unit-tested): keeps the top 5 skills individually and collapses the rest into one `Other (N skills)` entry. Applied identically to both the donut and the bars so they always agree with each other. Percentages are computed off the full filtered total, so the grouped view never loses or double-counts levels.
- No "View All Skills" button (intentionally not added, per spec).
- Colors: sage/olive (`ACC`) for the largest slice, then amber (`YEL`) → coral (`RED`) → lavender (`PUR`) → amber-orange (`ACC2`), cycling if ever needed; "Other" always gets a fixed, deliberately neutral muted tan (`#a89a78`) rather than a theme accent, so it reads as "everything else" rather than competing for attention.

**New: Stats prewarm for faster first open**
- The App now schedules a single `self.after(4000, self._prewarm_stats)` call at startup (no recurring timer) that quietly builds the Stats tab's real widgets and kicks off its initial history-aggregation load *before* the user ever clicks the tab — particularly aimed at Linux, where cold-building the matplotlib figures was noticeably slower than on Windows.
- `StatsTab` gained `_ensure_built()`, a single shared guarded-build path now used by both `on_tab_shown()` and the new `prewarm()` — exactly one build code path, not two copies that could drift apart or double-build.
- Building happens inside the Stats tab's existing frame in the shared `tkraise()` stack from Checkpoint 1, which isn't the topmost (visible) frame unless the user is already on Stats — so prewarm never switches tabs, steals focus, or causes visible flicker. If the user clicks into Stats before the 4s timer fires, normal loading proceeds exactly as before (the timer's later call becomes a no-op).
- If prewarm throws for any reason, it's caught and logged (`⚠ Stats prewarm failed (non-fatal)`) without affecting app startup.

**Preserved (re-verified with the live Tk test, not assumed):**
- Stats still builds exactly once; switching Stats → Monitor → Stats repeatedly still produces exactly one filter row and a stable widget count.
- Refresh, Account filter, Skill filter, and date-range buttons all still update KPIs/chart/panels correctly with zero widget duplication.
- The empty state still works correctly with zero levelup history.
- The beta.5 dependency-restart prompt (Restart Now/Later + persistent chrome notice) is untouched and still passes its own live test.

**Validation:**
- `python -m compileall .` — clean.
- `pyflakes` on `p2p_monitor.py` (still exactly the 14 pre-existing baseline warnings, zero new) and `ui/stats_tab.py` (zero).
- Tuple-padding constructor scan (the class of bug found in beta.5) re-run across `p2p_monitor.py` and every `ui/*.py` file — zero instances.
- Live Tk test (real Xvfb display, real `App._style()`, real `StatsTab`): 36 checks covering combobox states, chart/donut theming, skill grouping, prewarm-without-tab-switch, post-prewarm idempotency, and the full filter/refresh/revisit matrix — all passing. Plus the existing empty-state, beta.4 full-flow, and beta.5 restart-dialog live tests re-run unchanged and still passing.

**Files changed:**
- `ui/stats_tab.py` — chart theming overhaul (`_theme_chart_axes`), skill donut chart, `group_top_n_with_other` wiring, `_ensure_built()`/`prewarm()` refactor
- `py/stats.py` — new `group_top_n_with_other()`
- `p2p_monitor.py` — version 2.0.0-beta.6; fixed `TCombobox` style maps + popdown listbox option_add entries in `_style()`; new `_prewarm_stats()` wired via a single startup `self.after()` call
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.4
### Linux dependency-update support in the source updater

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3.

**New: Linux dependency detection in the in-app updater**
- New `requirements-linux.txt` — the single source of truth for Linux pip dependencies (`pystray`, `Pillow`, `psutil`, `tkcalendar`, `matplotlib`). `install.sh` now installs from this file (`pip3 install -r requirements-linux.txt`) instead of a separately-maintained hardcoded package list, so the two can no longer drift out of sync.
- After a successful Linux source update, `_do_apply_update()` now calls a new `_check_and_install_linux_deps()`: it reads the just-updated `requirements-linux.txt`, checks each entry against installed packages via `importlib.metadata`, and — only if something is actually missing — prompts with a Yes/No dialog listing exactly which package(s) before running `pip install` for those specific entries.
- If the user declines, the update still completes and offers to restart as normal; a log line with the manual `pip3 install ...` command is left for later.
- This **never** touches system/apt packages, never runs the full `install.sh`, and never installs anything without an explicit prompt. If nothing is missing (the common steady-state case), this is a silent no-op — no dialog, no extra noise.
- `discord.py` is intentionally **not** in `requirements-linux.txt` — it already has its own on-demand installer (existing code in `ui/settings_tab.py`, triggered only when "Run Bot Setup" is used), since most setups only need webhook notifications and don't need the extra dependency at all.
- This addresses a real gap confirmed by reading the updater code directly: the file-copy-based updater has no dependency-installation step of any kind, so updating via "Check for Update" without also installing matplotlib would previously leave the Stats chart unavailable (gracefully, thanks to beta.3's fallback message — not a crash) until the user manually ran `pip install matplotlib`. This checkpoint closes that gap going forward for any future new dependency, not just matplotlib.
- **Important nuance for the beta.3 → beta.4 transition specifically:** an update is *applied* by whichever updater code is currently running — so upgrading from beta.3 (which predates this feature) to beta.4 is carried out by beta.3's old updater, which has no idea this dependency check exists and will not run it. That one transition still needs `matplotlib` installed manually (`./install.sh` or `pip3 install matplotlib --break-system-packages`) after updating. To close that gap too, `_check_and_install_linux_deps()` now also runs once at startup (`_startup_dependency_check()`, Linux source installs only) — so after restarting into beta.4 (or any future version), the *next* startup catches anything an older updater couldn't, in addition to the existing post-update check. Both call the same detection/prompt logic; the startup path is silent on every run after the first one that actually needed to install something.
- Dependency detection is presence-only (is the package importable at all?), not minimum-version enforcement — it would catch "matplotlib not installed" but not "matplotlib installed but older than required". Acceptable for beta; worth tightening with real version-comparison before the stable v2.0.0 release.

**Polish: restart-required prompt after a dependency install**
- After `_check_and_install_linux_deps()` successfully pip-installs missing packages, it no longer just logs "installed" — it now shows a modal dialog ("Dependencies installed successfully. Please restart P2P Monitor for the new packages to load.") with **Restart Now** / **Later** buttons. The new package was just installed into the running interpreter's site-packages, but Python's import system won't pick it up mid-process — a restart is the only way it actually loads.
- **Restart Now** stops the watcher if running and re-execs the process (`os.execv`) — the exact same restart path the existing post-update "Restart now?" prompt already used. Extracted both into one shared `_restart_app()` helper so there's a single restart code path, not two copies of the same logic.
- **Later** keeps the app open and shows a persistent "⚠ Restart required to finish dependency update" notice in the window chrome (next to the Monitor/Stopped status) until the user actually restarts. Clicking the notice offers to restart (with its own confirmation — restarting is never automatic).
- This applies to both places `_check_and_install_linux_deps()` is called from — the post-update check and the startup check — so the prompt shows up correctly however the install was triggered.
- Never runs `install.sh` or `apt`, and the whole feature is gated to non-frozen Linux installs already (inherited from `_check_and_install_linux_deps()`'s existing guard) — Windows frozen builds never reach this code at all.

**Bugfix: Stats tab created a duplicate filter row every time the tab was revisited**
- Root cause, confirmed by actually running the real `StatsTab` against a real Tk display (via Xvfb) rather than guessing from reading code: `_build_kpis()` passed `pady=(0, 10)` — a tuple — directly to a `tk.Frame()` **constructor**. Tkinter widget constructors only accept a single padding value; tuples are only valid on `.pack()`/`.grid()` calls. This raised a `TclError` on the very first build, *after* `_build_filters()` had already successfully created the filter row but *before* `self._built = True` ever executed.
- Tkinter's event dispatcher swallows exceptions raised inside callbacks (button clicks, etc.) — it prints a traceback and keeps running rather than crashing the app. So the Stats tab looked superficially fine (no visible crash), but `self._built` was permanently stuck at `False`. Every subsequent visit to the tab re-ran the *entire* build from scratch: `_build_filters()` would succeed again (adding another filter row) and then crash again at the same spot, so KPI cards/chart/panels never appeared at all. This is an exact match for the reported symptom.
- Fixed both occurrences (the same mistake was also in `_build_panels()`) by moving the asymmetric padding to the `.pack()` call instead of the constructor.
- Then swept the **entire codebase** (`p2p_monitor.py` and every `ui/*.py` file) with a script that parses every `tk.Frame`/`tk.Label`/`tk.Button`/etc. constructor call — including ones spanning multiple lines — for a tuple `padx=`/`pady=` argument. Found and fixed two more instances of the exact same mistake in the brand-new restart-prompt dialog code added in this same release, before they ever shipped. Confirmed zero remaining instances anywhere in the repo.
- Verified the fix directly: built a real Tk root against a virtual display, ran the actual `StatsTab` class through 5+ simulated tab-revisit cycles, filter changes, date-range clicks, and Refresh clicks, and confirmed exactly one filter row and a stable total widget count throughout — not inferred from code review.
- No changes to `py/stats.py`'s aggregation logic, no changes to Monitor/Status/History/Launcher/Settings/parser/Discord behavior — this was purely a Tkinter constructor-argument bug in `ui/stats_tab.py`.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.4 (unchanged); new `_check_and_install_linux_deps()` method (Checkpoint linux-deps); new `_startup_dependency_check()`; new `_restart_app()`, `_show_restart_now_later_dialog()`, `_show_restart_required_notice()`, `_on_restart_notice_clicked()`, `_prompt_restart_after_dep_install()`; existing post-update restart closure simplified to use the shared `_restart_app()`; hidden restart-notice label added to the window chrome
- `ui/stats_tab.py` — fixed the two tuple-padding constructor bugs in `_build_kpis()` and `_build_panels()` that caused the duplicate filter row bug
- `requirements-linux.txt` — new
- `install.sh` — installs from `requirements-linux.txt` instead of a hardcoded inline list; copies `requirements-linux.txt` into the install dir
- `update_manifest.txt` — added `requirements-linux.txt` (critical — without this the updater would never refresh the file it depends on)
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.3
### Checkpoint 2 — real Stats tab + theme refinement

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3.

**Stats tab (real implementation, replaces the Checkpoint 1 placeholder)**
- New `py/stats.py`: pure, stdlib-only levelup aggregation (no Tkinter) — `load_levelup_rows`, `filter_rows`, `aggregate_daily_totals`, `aggregate_skill_totals`, `aggregate_account_totals`, `compute_kpis`, `daily_series_for_range`, `date_bounds_for_preset`. Kept separate from the UI so it's unit-testable without a display.
- Data source is existing levelup history records only — no history file format changes. `'Total Level'` milestone-broadcast rows are deliberately excluded from aggregation (they're not a real skill and would double-count alongside the per-skill levelup that normally accompanies them).
- Filters: Account (All Accounts + individual), Skill (All Skills + individual), date range pills (7D / 30D / 90D / 1Y / ALL).
- KPI cards: Total Levels, Average Per Day (divided by calendar days in the selected range, not just days with data), Best Day, Top Account.
- Main chart: Daily Levels Gained, zero-filled across the full date range so quiet days show as real zeros instead of gaps. Uses `matplotlib`/`FigureCanvasTkAgg` when available; falls back to a "matplotlib not installed" message instead of crashing if the backend can't load.
- Lower panels: Levels by Skill and Top Accounts, both ranked bar-lists.
- Empty state ("No level-up data found yet.") shown when there is no levelup history at all; a filtered-to-zero result (e.g. an empty date range) shows zeros/"no data for this filter" instead of the full empty state.
- Performance: disk reads (`load_levelup_rows`, which may touch many history files across accounts) always run on a background thread. Filter and date-range changes only re-filter/re-aggregate the already-loaded in-memory rows and redraw — no disk I/O on every filter click. The two lower panels rebuild only their own row widgets, not the rest of the tab.
- Lazy-built: the Stats tab's real widgets (and first data load) are built on the first time the tab is opened, not at app startup — consistent with the Checkpoint 1 cached-tab architecture.
- A live `levelup` event now marks the Stats tab dirty (`mark_dirty()`); the next time the tab is opened it reloads from disk instead of showing stale data.

**Theme refinement**
- `ACC` and `GREEN` desaturated further toward muted sage/olive/moss — beta.2's values (48%/52% saturation) still read as a fairly vivid "terminal green" against the warm dark background. New values are 30%/36% saturation.
  - `ACC`: `#4a8f5c` → `#7c9468`
  - `GREEN`: `#5cbf72` → `#8aac6e`
- All other tokens (`ACC2`, `RED`, `YEL`, `PUR`, `FG`, `FG2`, backgrounds) already matched the target direction as of beta.2 and are unchanged.
- Confirmed nav bar/window chrome are already sans-serif (`SANS`/`SANSB`/`BIG`) from Checkpoint 1 — no font change needed.
- Hardcoded cyan/neon literals remain in `ui/launcher_tab.py` and `ui/history_tab.py` (e.g. `#2e86c1`, `#00ff88`) — intentionally left untouched. Those tabs get their own redesign in Checkpoint 4, and patching scattered literals now would just be redone there; fixing them piecemeal ahead of that pass isn't worth the churn.
- Monitor tab layout is still unchanged — full visual redesign is Checkpoint 4.

**Install / update plumbing**
- Added `py/stats.py` to `update_manifest.txt` and `install.sh`.
- Added `matplotlib` to `requirements-windows.txt` and `install.sh`'s pip install line.
- Added `matplotlib.backends.backend_tkagg` to `p2p_monitor.spec` hiddenimports (PyInstaller's built-in matplotlib hook handles `mpl-data`/fonts automatically; the TkAgg backend specifically is loaded dynamically and is sometimes missed by static analysis, so it's listed explicitly to match this spec's existing defensive style for third-party packages).
- Verified via simulation: copying *only* the manifest-listed files into a clean directory (what the in-app updater does) produces a working install, including the new Stats module.

**Files changed:**
- `py/stats.py` — new
- `ui/stats_tab.py` — full rewrite (was a Checkpoint 1 placeholder)
- `p2p_monitor.py` — version 2.0.0-beta.3; `ACC`/`GREEN` color tokens; `_on_event` marks Stats tab dirty on live `levelup` events
- `update_manifest.txt` — added `py/stats.py`
- `install.sh` — added `py/stats.py` copy line; added `matplotlib` to pip install line
- `requirements-windows.txt` — added `matplotlib`
- `p2p_monitor.spec` — added `matplotlib.backends.backend_tkagg` to hiddenimports; added verification step to header comment
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.2
### Checkpoint 1 correction — warm theme tokens

**Internal beta — not the public 2.0.0 release.** Still on top of Checkpoint 1 (navigation/theme foundation); Stats tab work hasn't started yet. Stable public release remains v1.8.3.

**Theme correction**
- Beta.1's background tokens (`BG`/`BG2`/`BG3`/`BG4`) and `FG2` were still blue-dominant in every channel comparison (e.g. `BG2 #161a22` = R22/G26/**B34**) — a navy/slate canvas, not the warm charcoal/espresso/graphite that was intended. That's what kept the app reading as "terminal/cyan-ish" even though the accent color itself had already changed away from cyan.
- Corrected all five tokens to genuinely warm neutrals (R ≥ G > B in every shade):
  - `BG`: `#0f1115` → `#13110f`
  - `BG2`: `#161a22` → `#1a1714`
  - `BG3`: `#1d2130` → `#221e19`
  - `BG4`: `#252840` → `#2c2620`
  - `FG2`: `#78788a` → `#8c8478`
- Lightness progression across the four background tiers is preserved (same relative layering/contrast as beta.1), only the hue shifted from cool to warm.
- `ACC`, `ACC2`, `GREEN`, `RED`, `YEL`, `PUR`, `FG` were already correct (sage green, amber, coral, warm cream) and are unchanged.
- Confirmed nav bar and window chrome were already using the sans-serif font tokens (`SANS`/`SANSB`/`BIG`) from beta.1 — no font change was needed, this was purely a color-token fix.
- No layout, behavior, or tab content changed. Monitor tab redesign is still scheduled for Checkpoint 4.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.2; corrected `BG`/`BG2`/`BG3`/`BG4`/`FG2` color tokens
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.1
### Checkpoint 1 — UI foundation: custom navigation, warm theme palette, Stats tab slot

**Internal beta — not the public 2.0.0 release.**
Use "Include pre-release versions when checking for updates manually" to receive this update.
The stable public release remains v1.8.3 until the full v2.0.0 is ready.

**Navigation architecture**
- Replaced `ttk.Notebook` with a custom frame-based navigation bar using `tkraise()` — tab frames are built once, never destroyed on switch, and re-raised instantly. This eliminates the per-switch rebuild overhead that made Linux feel slightly less responsive than Windows.
- Added `app.show_tab(name)` method (string-based, index-independent) replacing all `_nb.select(N)` calls — forward-compatible with future tab additions.
- Tab order is now locked for v2: Monitor | Status | Stats | History | Launcher | Settings.
- Stats tab added as a placeholder frame (real content in Checkpoint 2).

**Theme: warm dark palette**
- Replaced the cyan-heavy palette with a warm dark scheme: sage/olive green primary accent, warm amber/gold for level highlights, muted coral/red for errors and stopped states, warm cream/off-white text.
- Primary accent `ACC`: `#00d4ff` (cyan) → `#4a8f5c` (sage green)
- `GREEN`: `#00ff88` (neon) → `#5cbf72` (muted)
- `RED`: `#ff4444` (bright) → `#d04848` (coral)
- `YEL`: `#ffd700` (gold) → `#c8a840` (amber)
- `FG`: `#e8eaf0` (cool white) → `#e4ddd4` (warm cream)
- All color tokens are centralized as class attributes on `App` — later checkpoints inherit them without hardcoding.
- Added sans-serif font constants (`SANS`, `SANSB`, `SANSL`, `SANSS`, `BIG`): Segoe UI on Windows, DejaVu Sans on Linux. Applied to window chrome and navigation bar. Monospace (`MONO`) is retained for the raw event log text area.
- Existing tab content (Monitor, Status, History, Launcher, Settings) automatically picks up the new color tokens; font swap inside those tabs happens in Checkpoint 4.

**Install / update plumbing**
- Added `ui/stats_tab.py` to `update_manifest.txt` and `install.sh` — without this, a fresh install or an in-app update would copy/apply `p2p_monitor.py` (which now imports `ui.stats_tab`) without the file it depends on, causing a crash on launch. `p2p_monitor.spec` (PyInstaller) needed no change — it statically discovers local `.py` imports automatically; only non-`.py` assets like `error_rules.json` need explicit `datas` entries.
- Fixed `install.sh`'s version-banner regex to also capture pre-release suffixes (e.g. `-beta.1`) instead of truncating at the first hyphen.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.1; warm dark color tokens; SANS font constants; custom nav bar; `show_tab()` method; removed `_nb`, `_on_tab_changed`, `_history_tab_frame`, `_status_tab_frame`; tray icon color updated
- `ui/stats_tab.py` — new (Checkpoint 1 placeholder)
- `ui/status_tab.py` — `app._nb.select(2)` → `app.show_tab('History')`
- `update_manifest.txt` — added `ui/stats_tab.py`
- `install.sh` — added `ui/stats_tab.py` copy line; fixed version-banner regex for pre-release suffixes
- `CHANGELOG.md` — this entry

---

## v1.8.3
### Bugfix/cleanup release: update-check timing, reset attribution, Monitor tab cleanup, debug.jsonl

**Fixes**
- Startup update-awareness check (`force=True`) now updates `_last_update_check_slot`, so the periodic check no longer re-runs immediately after startup
- `levelup_every` is now clamped to a minimum of 1 (`max(1, int(...))`, falls back to 5 on bad config values) — previously `levelup_every = 0` could divide by zero
- Script reset attribution (`Escaped ship -> Startup`, `Stuck walking -> Startup`) now uses the task/activity active at the reset line's position in the batch (via each parsed event's `_line_idx`), instead of whatever task the batch ends on — fixes occasional mislabeling when a reset and a new task land in the same read batch. A contained fallback also covers the case where no task context is known at all (e.g. the very first batch ever processed for an account, where `slice_tasks()` had already suppressed the `Task is X` announcement because of a nearby `> Locking` line) — in that case the nearest `Task is X` / `Activity is Y` is read directly from this batch's raw lines, before the reset line only. This fallback never parses or emits task events, never updates `state.last_task` / `state.last_activity`, and never touches `py/reader.py`, `slice_tasks()`, Slayer parsing, or Discord routing.
- `Script Stopped` now logs to the Monitor tab before the corresponding `Auto restart scheduled/skipped ...` line — previously the auto-restart message could appear first

**New: structured session debug log**
- New `~/.p2p_monitor/debug.jsonl`, truncated on monitor startup
- Important diagnostics (history dedupe, launcher/relaunch internals, Discord thread rate-limiting, daily summary internals) are now always written here, independent of the debug checkbox
- The debug checkbox now only controls whether a short human-readable line also mirrors live to the Monitor tab — Monitor tab behavior stays append-only, with no replay of old debug entries and no auto-sorting

**History dedupe diagnostics**
- When `_dedup_history_file()` removes duplicate rows, it now writes a `history_dedupe` entry to `debug.jsonl` with the cleanup timestamp, account, history file path, duplicate count, type counts, and the removed entries themselves (capped at 200, with `truncated: true` if more were removed)
- Dedupe behavior itself is unchanged — this is diagnostics only
- Covers both backfill-triggered dedupe and History tab loads

**Monitor tab cleanup**
- Moved internal/plumbing lines to debug-only: Discord thread membership rate-limiting, daily summary send/failure internals, and launcher internals (closing client, waiting before relaunch, relaunching, client PID confirmed)
- Monitor tab keeps real account/script/task/error/drop/death/level/update events

**New: script event ping toggle**
- New setting "Ping for script events" (`ping_script_event`, default `True`) under Event Notifications — when off, script start/stop/pause/resume Discord messages still post but without a mention ping. Task/error/drop/death/level ping settings are unaffected.

**Files changed:**
- `py/watcher.py` — update-check timing fix; `levelup_every` clamp; reset attribution via `_line_idx` plus a raw-line fallback (`_task_ctx_from_raw_lines()`) for batches with no prior task context; `_maybe_schedule_auto_restart()` returns `(status, message)` instead of logging directly; `_debug_entry()` helper; `debug.jsonl` reset on `start()`; daily summary and thread rate-limit lines moved to debug
- `py/history.py` — `_dedup_history_file()` writes `history_dedupe` diagnostics to `debug.jsonl`; accepts optional `account` param
- `py/launcher.py` — `_discover_and_cache()` and `relaunch_account()` move internal lines to `debug.jsonl`, mirroring to Monitor only when the debug checkbox is enabled
- `py/discord.py` — `post_script_event()` respects new `ping_script_event` config key
- `py/util.py` — new `write_debug_entry()` / `reset_debug_log()` helpers for `~/.p2p_monitor/debug.jsonl`
- `ui/settings_tab.py` — new "Ping for script events" checkbox
- `p2p_monitor.py` — version 1.8.3; `ping_script_event` added to `DEFAULT_CFG`
- `README.md` — corrected SDN/Worker wording
- `CHANGELOG.md` — softened v1.8.2 compile-metadata claim; removed references to test files not present in the repo

---

## v1.8.2
### DreamBot SDN as primary update source + configurable check interval

**Primary update source: DreamBot SDN API**
- Update awareness now checks `https://sdn.dreambot.org/scripts/all` as primary source
- Finds P2P Master AI by exact name match (`name == "P2P Master AI"`) with optional safety checks on `id == 1500` and `author == "Aeglen"`
- Extracts the latest script version from the SDN response; compile metadata may be logged for debug if present but is not used for update decisions
- Cloudflare Worker (`p2p-sdn-watch.p2pmonitor.workers.dev`) kept as silent fallback — used automatically if SDN is unreachable, returns bad JSON, or does not contain P2P Master AI
- Source and fallback details are debug-only — no user-facing alerts for source failures alone

**Version comparison: Decimal-aware**
- SDN returns versions as JSON numbers (e.g. `2.15` for 2.150, dropping trailing zero)
- New `_sdn_ver_tuple()` uses `Decimal` arithmetic to pad fractional parts to 3 digits: `2.15 → (2, 150)`, `2.149 → (2, 149)` — so `2.15` correctly compares as newer than `2.149`
- Local title versions and all comparisons now use the same Decimal-based path

**Configurable update check interval**
- New HH:MM interval control in Settings → Update Awareness: default 6h 0m, minimum 1m, maximum 24h 0m
- Replaces the previous fixed UTC slot schedule (00:20, 06:20, 12:20, 18:20)
- Startup check still fires immediately on monitor launch
- Periodic checks fire based on wall-clock elapsed time since last check
- `0h 0m` clamps to `0h 1m`; values over `24h 0m` clamp to `24h 0m`
- Config keys: `update_check_interval_hours`, `update_check_interval_minutes`

**Files changed:**
- `p2p_monitor.py` — version 1.8.2; `update_check_interval_hours` / `update_check_interval_minutes` added to DEFAULT_CFG
- `py/watcher.py` — `_SDN_URL`, `_WORKER_URL`, `_SDN_SCRIPT_NAME/ID/AUTHOR` constants; `_sdn_ver_tuple()` with Decimal comparison; `_ver_tuple()` delegates to `_sdn_ver_tuple`; `_fetch_from_sdn()` + `_fetch_from_worker()` replace `_fetch_latest_version()`; `_check_update_awareness()` uses elapsed-time interval; `_last_update_check_slot` initialised to `0.0`
- `ui/settings_tab.py` — HH:MM interval spinboxes; updated description text; clamping in `save()`
- Validation covered SDN parsing, fallback behavior, Decimal version comparison, interval logic, and clamping.

---

## v1.8.1
### Real Discord pings, independent screenshot controls, auto-relaunch on update, runtime stats

---

**Real Discord pings**
- Mentions in embed descriptions (`<@user>`) appeared visually but did not trigger Discord notifications; fixed by moving real pings to top-level message `content` with explicit `allowed_mentions: {"users": ["id"]}`
- `<@user>` removed from embed descriptions — no function there now that real pings go in message content
- New `normalize_mention_id()` helper: accepts raw ID (`123`), `<@123>`, or `<@!123>` — all normalise to `123`
- New `apply_ping(payload, mention_id, enabled)` helper: adds `content` and `allowed_mentions` to any payload dict
- **New Ping column** in the Event Notifications settings table (alongside Notify and Screenshot): Quests, Tasks, Chat, Errors, Drops, Deaths, Level Ups each have an independent Ping checkbox
- Script lifecycle events (Started, Stopped, Paused, Resumed) always ping when a mention ID is configured
- Update awareness alerts have a separate Ping toggle in the Update Awareness section (default on)
- New config keys: `ping_quest`, `ping_task`, `ping_chat`, `ping_error`, `ping_drops`, `ping_death`, `ping_levelup`, `ping_update`

**Event screenshots no longer depend on scheduled screenshots (bug fix)**
- Previously `screenshots_enabled` (labelled "Enable scheduled screenshots") acted as a global kill switch — if off, all screenshots stopped including per-event ones, on-demand `/ss`, and startup
- Fixed: `screenshots_enabled` now gates scheduled screenshots only; each screenshot type is independently controlled
- `ScreenshotService.enqueue()` now returns `True` if queued, `False` if refused; `LogWatcher._enqueue_screenshot()` propagates that bool
- `DiscordRouter.post_event()` and `post_drop()`: if screenshot enqueue returns `True`, return immediately; if `False`, debug-log and fall through to embed-only — prevents duplicate messages (embed-only + screenshot-with-embed)
- `ScreenshotService._worker()`: if `take_screenshot()` fails and the item has a payload+URL (event/drop screenshot), posts the embed without the image as fallback; scheduled screenshots with no payload remain debug-only
- Startup screenshots gated by `screenshot_on_startup`; event screenshots gated by `ss_event_*` only; on-demand always works

**Update awareness: auto-relaunch on update (default off)**
- New setting: **Auto-relaunch clients when script/client update is found**
- When enabled: affected accounts are relaunched automatically with a 5-second stagger; accounts without a launcher preset are listed for manual action in the Discord alert
- Warning shown in Settings UI and Discord message: can interrupt any current activity including Inferno/Jad
- When disabled: normal grouped update alert sent, recommends `/relaunch <account>` when ready
- Separate dedupe stores for alert suppression vs relaunch suppression — a new version triggers both independently; when auto-relaunch fires, alert keys are also saved so the same update does not later produce a normal "Update Available" alert
- New config key: `auto_relaunch_on_update`
- New state file: `~/.p2p_monitor/update_relaunch_state.json`

**Update alerts: grouped and pingable**
- One Discord message per check listing all accounts by update category (both / script / client)
- Pings once per check if `ping_update` is enabled — not once per account

**Grouped repeated task/lock failures**
- Rapid burst of related lock/error events (e.g. multiple farming patch failures at once) now groups into one Discord alert and one monitor line
- 3-second debounce window per account; duplicate details deduped; one ping if ping_error is enabled
- Standalone errors (non-lock) and errors separated by more than 3 seconds still send individually

**Lock failure wording**
- Generic lock fallback changed from `Quest abandoned: X` to `Task locked/skipped: X`
- Covers non-quest locks (Gold BF, Sailing, etc.); specific `error_rules.json` matches unchanged

**Runtime stats in History tab**
- Each account row in the History tab shows **📈 Runtime Stats** alongside **📊 Summary**
- Opens a popup showing: Total running time, Active play time, Break time, Break %
- Range filters: All time, Today, 7 days, 30 days
- Break time calculated from actual elapsed intervals — not from planned break length in the log
- `py/history.py`: new `compute_runtime_stats(account, since_ts, until_ts)` and `_fmt_secs()` functions

**Files changed:**
- `p2p_monitor.py` — version 1.8.1; new `ping_*` and `auto_relaunch_on_update` keys in DEFAULT_CFG
- `py/reader.py` — lock fallback wording
- `py/discord.py` — `_desc()` removes mention embed; `normalize_mention_id()` + `apply_ping()`; `post_event()` enqueue fallback; `post_drop()` enqueue fallback; `post_task()` applies `ping_task`; `post_script_event()` always pings when mention set; `combined_daily_summary_payload()` removes inline mention
- `py/watcher.py` — `handle_event()` wires `apply_ping` per event type; `_enqueue_screenshot()` returns bool; `_check_update_awareness()` ping + auto-relaunch + separate dedupe stores; `_build_relaunch_alert_payload()`; `_load/save_relaunch_state()`; `_maybe_burst_error()` + `_flush_error_burst()` for grouped failures; `trigger_screenshot()` restored; `_do_screenshot()` no longer gates on-demand behind `screenshots_enabled`; `AccountState` gets burst buffer fields
- `py/screenshot.py` — `enqueue()` returns bool, trigger-specific guards (scheduled/startup/event); capture-failure embed-only fallback in `_worker()`
- `py/history.py` — `compute_runtime_stats()`, `_fmt_secs()`, supporting helpers
- `ui/settings_tab.py` — Ping column in event table; `ping_update` and `auto_relaunch_on_update` settings with warning label
- `ui/history_tab.py` — Runtime Stats pseudo-button in account row; `_show_runtime_stats_popup()`
- `tests/test_v181.py` — 42 tests covering all items above

---

## v1.8.0
### Stable release — Update Awareness, Screenshot Reliability, PID-First Window Lookup

---

**Update awareness: grouped Discord alert**
- The update-awareness check now sends **one Discord message per check** instead of one per account/window
- The grouped embed lists all accounts that need updates, split into up to three fields (only included when non-empty), ordered: `Both script + DreamBot update needed` → `P2P Master AI script update needed` → `DreamBot client update needed`
- Each account appears as a bullet with specific version detail, e.g. `• Account: v2.141 → v2.143, DreamBot 4.1.67 shows NEW CLIENT AVAILABLE`
- Embed description: `Recommended: use /relaunch <account> to restart and load the latest version.`
- Account name, DreamBot client version, and script version are now all parsed from the window title (`DreamBot 4.1.67 - Account - P2P Master AI v2.141 - proxy`)
- Dedupe key is now per-account and includes account name, DreamBot version, local script version, latest version, and NEW CLIENT AVAILABLE flag — any of these changing allows a new alert

**Update awareness: schedule changed to UTC 6-hour slots**
- Previously: startup + daily at 2:00 PM PC local time
- Now: startup + every 6 hours at minute 20 UTC (00:20, 06:20, 12:20, 18:20), aligned shortly after the Cloudflare Worker cache refresh at :17
- Only one check fires per UTC slot even if the monitor is running across the minute boundary
- "No update found" and "no DreamBot windows found" log lines are now debug-only — main log only shows alerts and failures

**Screenshot logging: all screenshot-flow noise removed**
- All successful screenshot log messages are now fully silent in both normal and debug mode: no "Screenshot queued", no "Screenshot captured", no "Screenshot sent"
- All screenshot failure messages are now debug-only: "Screenshot failed", "Bot screenshot failed", "Gateway not ready — screenshot dropped", "No default webhook configured for screenshot", "Screenshot worker error", "Screenshot queue full"
- `ScreenshotService` now has an internal `_dbg()` helper that gates all screenshot messages behind the debug flag
- No change to screenshot capture, paint hide/show, Discord upload, or queue behaviour

**PID-first screenshot window lookup**
- `take_screenshot` now resolves the DreamBot window by PID first, falling back to title-based lookup only if needed
- **PID path:** reads cached PID from `launcher_state.json` via callback → `find_windows_for_pid(pid)` → accepts first visible window with `DreamBot` in title (account name not required — the PID is the ownership signal)
- **Title fallback:** existing `find_window_ids_by_name(account)` logic unchanged, still requires both `DreamBot` and account name in title to prevent wrong-window captures; on success, resolves and caches the window's PID for future screenshots
- New `find_windows_for_pid(pid)` platform helper in `platform_ops.py`: Linux via `xdotool search --pid`, Windows via `EnumWindows + GetWindowThreadProcessId`; filters visible + DreamBot title; never raises
- New public `get_account_pid(account)` / `set_account_pid(account, pid)` wrappers in `launcher.py` expose the existing `launcher_state.json` PID cache to the rest of the app
- `ScreenshotService` wired with `get_account_pid` and `set_account_pid` callbacks from watcher; `take_screenshot` receives them as optional keyword args
- **Startup PID cache population:** when `_startup_catchup` confirms an account is active, a short daemon thread calls `discover_account_process(account)` and saves the result — so PID-first lookup works on the very first screenshot of the session, even for clients not launched by the monitor

**Files changed:**
- `p2p_monitor.py` — version bump to 1.8.0; `inferno_rules.start_background_fetch()` wired at startup
- `py/watcher.py` — UTC slot scheduler; grouped alert; title parser extended with account + DB version; no-update log to debug; `_get_account_pid_cb` / `_set_account_pid_cb` / `_startup_cache_pid` methods; PID callbacks wired into `ScreenshotService`; startup PID daemon thread per active account
- `py/screenshot.py` — `take_screenshot` PID-first window resolution with title fallback and PID save-back; all screenshot log calls converted to `_dbg()`; new `_dbg()` helper on `ScreenshotService`; `find_windows_for_pid` and `get_pid_for_window` added to imports
- `py/launcher.py` — public `get_account_pid()` / `set_account_pid()` wrappers
- `py/platform_ops.py` — `find_windows_for_pid(pid)` with Linux (`xdotool`) and Windows (`EnumWindows`) implementations
- `ui/settings_tab.py` — update awareness label updated to reflect UTC 6-hour schedule
- `CHANGELOG.md`, `README.md` — updated for stable release
- `tests/test_update_awareness.py` — 27 tests: update awareness schedule, grouped alert, dedupe, screenshot silence/debug
- `tests/test_pid_screenshot.py` — 19 tests: launcher public API, `find_windows_for_pid`, `take_screenshot` PID-first/fallback/save-back, watcher PID callbacks

---

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
