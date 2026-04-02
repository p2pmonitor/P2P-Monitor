"""
watcher.py — Log watching engine for P2P Monitor
LogWatcher: discovers accounts, polls log files, drives backfill and live events.
Backfill and live monitor both call reader.parse_lines() — no more triple pipeline.
"""
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from py.config  import save_config
from py.util    import now_str
from py.reader  import parse_lines, parse_log_ts, strip_prefix, slice_last_task
from py.reader  import LOG_TS_RE
from py.history import (append_history, record_log_scanned, get_scanned_logs,
                        load_history_for, load_offsets, save_offsets)
from py.paint import do_force, do_force_skill, do_force_panel, PANEL_ACTIONS, AMOUNT_ACTIONS
from py.discord import (post_discord, bot_api, bot_setup_discord, bot_ensure_thread,
                        GatewayRunner, DiscordRouter, DROP_ICONS,
                        quest_started_payload, quest_payload,
                        slayer_task_payload, slayer_complete_payload, slayer_skipped_payload,
                        chat_payload, error_payload,
                        death_payload, levelup_payload,
                        combined_daily_summary_payload)
from py.screenshot import (get_focused_wid,
                            SS_PRIORITY_ONDEMAND,
                            SS_PRIORITY_EVENT, SS_PRIORITY_SCHEDULED,
                            ScreenshotService)

LOG_PATTERN = "logfile-*.log"

def _get_log_files(folder):
    p = Path(folder)
    files = list(p.glob("logfile-*.log")) + list(p.glob("logfile-*.log.*"))
    return sorted(files, key=lambda f: f.name)

def _fmt_duration(secs):
    """Format a duration in seconds as 'Xh YYm', or '—' if zero/negative."""
    if secs <= 0:
        return '—'
    h, rem = divmod(int(secs), 3600)
    return f"{h}h {rem // 60:02d}m"

# ── AccountState ───────────────────────────────────────────────────────────────
class AccountState:
    def __init__(self, name):
        self.name            = name
        self.last_task       = ''
        self.last_activity   = ''
        self.last_seen       = now_str()
        self.last_seen_ts    = time.time()
        self.err_history     = {}
        self.err_alerted     = {}
        self.last_screenshot_ts = 0
        self.on_break        = False
        self.session_start   = time.time()
        self.script_start_ts = None
        self.logged_in       = False
        self.total_break_secs    = 0
        self._break_start_ts     = None
        self.break_expected_end  = None   # time.time() + break_ms/1000 set on BREAK START
        self.last_log_mtime      = 0.0    # updated by _check_file; replaces xdotool window check
        self.notified_levels     = {}
        self.session_file_set    = set()  # tracks known session files; triggers uptime recalc when changed

    def should_alert(self, key, threshold, window_sec, dedupe_sec):
        now = time.time()
        if key not in self.err_history:
            self.err_history[key] = deque()
        q = self.err_history[key]
        q.append(now)
        if window_sec > 0:
            while q and now - q[0] > window_sec:
                q.popleft()
        if dedupe_sec > 0 and now - self.err_alerted.get(key, 0) < dedupe_sec:
            return False
        if len(q) >= threshold:
            self.err_alerted[key] = now
            if window_sec > 0:
                q.clear()
            return True
        return False

# ── LogWatcher ─────────────────────────────────────────────────────────────────
class LogWatcher:
    # Maps event type → config key that gates it; applies to both live and backfill.
    # Caller checks this BEFORE calling handle_event() — dispatcher never filters.
    _CFG_GUARD = {
        'quest_started':   'monitor_quests',
        'quest':           'monitor_quests',
        'task':            'monitor_tasks',
        'slayer_task':     'monitor_tasks',
        'slayer_complete': 'monitor_tasks',
        'slayer_skip':     'monitor_tasks',
        'chat':            'monitor_chat',
        'drop':            'monitor_drops',
        'death':           'monitor_deaths',
        'levelup':         'monitor_levelups',
        'error':           'monitor_errors',
    }

    def __init__(self, log_cb, event_cb, status_cb, backfill_cb=None):
        self.log       = log_cb
        self.on_event  = event_cb
        self.on_status = status_cb
        self._on_backfill_done = backfill_cb  # called after each account backfill completes
        self._running  = False
        self._thread   = None
        self._bot_thread = None
        self._backfill_threads = []
        self._backfill_lock   = threading.Lock()
        self._offsets  = load_offsets()   # {filepath: byte_offset} — persisted on clean shutdown
        self._accounts = {}
        self._accounts_lock = threading.Lock()
        self._offsets_lock  = threading.Lock()
        self._window_lock   = threading.Lock()  # serializes all window focus/click/screenshot ops
        self._ss_svc   = None    # ScreenshotService — created in start()
        self._router   = None    # DiscordRouter — created in start()
        self.cfg = {}
        self._cached_dirs     = []
        self._dirs_last_check = 0
        self._last_summary_date = None

    # ── Dir discovery ──────────────────────────────────────────────────────────
    def _get_log_dirs(self):
        now = time.time()
        if now - self._dirs_last_check < 30 and self._cached_dirs:
            return self._cached_dirs
        root = self.cfg.get('logs_root', '').strip()
        if not root or not os.path.isdir(root):
            self._cached_dirs = []
            self._dirs_last_check = now
            return []
        dirs = []
        root_path = Path(root)
        if _get_log_files(root_path):
            dirs.append(str(root_path))
        else:
            for sub in sorted(root_path.iterdir(), key=lambda x: x.name):
                if sub.is_dir() and _get_log_files(sub):
                    dirs.append(str(sub))
        self._cached_dirs = dirs
        self._dirs_last_check = now
        return dirs

    # ── Start / stop ───────────────────────────────────────────────────────────
    def start(self, cfg):
        if self._running:
            return
        self.cfg      = cfg
        self._running = True
        self._router  = DiscordRouter({
            'get_cfg':            lambda: self.cfg,
            'log':                self.log,
            'is_muted':           self._is_muted,
            'enqueue_screenshot': self._enqueue_screenshot,
        })
        self._bot_ready = threading.Event()
        self._ss_svc  = ScreenshotService({
            'get_cfg':        lambda: self.cfg,
            'log':            self.log,
            'is_muted':       self._is_muted,
            'wh_with_thread': self._router.wh_with_thread,
            'window_lock':    self._window_lock,
            'bot_ready':      self._bot_ready,
        })
        self._ss_svc.start()
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if cfg.get('bot_token'):
            runner = GatewayRunner(cfg, {
                'log':           self.log,
                'get_rows':      self.get_account_rows,
                'get_accounts':  lambda: list(self._accounts.keys()),
                'on_screenshot': self._bot_screenshot_to_channel,
                'on_force':       lambda account, adjustment, amount: do_force(
                                     account, adjustment, amount,
                                     log=self.log, window_lock=self._window_lock),
                'on_force_skill': lambda account, action: do_force_skill(
                                     account, action,
                                     log=self.log, window_lock=self._window_lock),
                'on_force_panel': self._bot_force_panel,
                'is_running':    lambda: self._running,
                'get_cfg':       lambda: self.cfg,
            })
            runner.bot_ready = self._bot_ready  # share the same event
            self._bot_thread = threading.Thread(target=runner.run, daemon=True)
            self._bot_thread.start()

    def stop(self):
        self._running = False
        if self._ss_svc:
            self._ss_svc.stop()
        with self._backfill_lock:
            threads_snapshot = list(self._backfill_threads)
            self._backfill_threads.clear()
        for t in threads_snapshot:
            t.join(timeout=10)
        if hasattr(self, '_thread') and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if hasattr(self, '_bot_thread') and self._bot_thread and self._bot_thread.is_alive() and self._bot_thread is not threading.current_thread():
            self._bot_thread.join(timeout=5)
        with self._offsets_lock:
            save_offsets(dict(self._offsets))

    # ── Account row export ─────────────────────────────────────────────────────
    def get_account_rows(self):
        # Check for new rotated log files per account and recalculate uptime/breaks if changed
        try:
            for d in self._get_log_dirs():
                log_files   = _get_log_files(d)
                active      = next((f for f in reversed(log_files) if re.match(r'logfile-\d+\.log$', f.name)), None)
                if not active:
                    continue
                active_name   = active.name
                session_files = frozenset(
                    f.name for f in log_files
                    if f.name == active_name or f.name.startswith(active_name + '.'))
                folder = os.path.basename(d)
                with self._accounts_lock:
                    state = self._accounts.get(folder)
                if state and session_files != state.session_file_set:
                    state.session_file_set = session_files
                    self._startup_catchup(str(active))
        except Exception:
            pass

        rows = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, s in sorted(snapshot):
            window_open = self._is_window_open(s)
            if not window_open:
                status = '🔴 Offline'
            elif s.on_break or s._break_start_ts or (s.last_task or '').lower() == 'break':
                status = '🟡 On Break'
            elif not s.logged_in:
                status = '🟡 Logged Out'
            else:
                status = '🟢 Logged In'
            start_ts    = s.script_start_ts or s.session_start
            uptime_secs = time.time() - start_ts
            uptime_str  = _fmt_duration(uptime_secs) if window_open else '—'
            break_secs  = s.total_break_secs
            if s._break_start_ts:
                break_secs += time.time() - s._break_start_ts
            break_str = _fmt_duration(break_secs)
            rows.append({'account': name, 'task': s.last_task or '—',
                         'activity': s.last_activity or '—', 'status': status,
                         'uptime': uptime_str, 'break_time': break_str,
                         'muted': name in self.cfg.get('muted_accounts', [])})
        return rows

    def _is_muted(self, account):
        return account in self.cfg.get('muted_accounts', [])

    def toggle_mute(self, account):
        muted = list(self.cfg.get('muted_accounts', []))
        if account in muted:
            muted.remove(account)
            self.log(f"🔊 [{account}] Unmuted")
        else:
            muted.append(account)
            self.log(f"🔇 [{account}] Muted — Discord posts and screenshots suppressed")
        self.cfg['muted_accounts'] = muted
        self._save_cfg()

    def _save_cfg(self):
        save_config(self.cfg)

    def trigger_screenshot(self, account):
        self._do_screenshot(account, 'on-demand')

    def _do_screenshot(self, account, trigger='scheduled'):
        if not self.cfg.get('screenshots_enabled'):
            self.log("⚠ Screenshots not enabled in Settings")
            return
        if self._is_muted(account):
            return
        priority = SS_PRIORITY_ONDEMAND if trigger == 'on-demand' else SS_PRIORITY_EVENT
        self._enqueue_screenshot(priority, account, trigger)

    def _enqueue_screenshot(self, priority, account, trigger,
                             url=None, payload=None,
                             bot_channel_id=None, bot_token=None, restore_wid=None):
        """Delegate to ScreenshotService — guards (enabled, muted) enforced there."""
        self._ss_svc.enqueue(priority, account, trigger,
                             url=url, payload=payload,
                             bot_channel_id=bot_channel_id, bot_token=bot_token,
                             restore_wid=restore_wid)

    # ── Periodic checks ────────────────────────────────────────────────────────
    def _prune_screenshots(self):
        self._ss_svc.prune()

    def _check_screenshots(self, ss_min):
        if not self.cfg.get('screenshots_enabled'):
            return
        is_startup = not getattr(self, '_screenshots_started', False)
        self._screenshots_started = True
        threshold = ss_min * 60
        due = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, state in snapshot:
            if state.on_break:
                continue
            if time.time() - state.last_screenshot_ts >= threshold:
                state.last_screenshot_ts = time.time()
                due.append(name)
        if not due:
            return
        for i, name in enumerate(due):
            trigger = 'startup' if is_startup else 'scheduled'
            self._enqueue_screenshot(SS_PRIORITY_SCHEDULED, name, trigger)

    def _check_daily_summary(self):
        if not self.cfg.get('summary_enabled'):
            return
        summary_time = self.cfg.get('summary_time', '22:00').strip()
        try:
            sh, sm = [int(x) for x in summary_time.split(':')]
        except Exception:
            return
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if self._last_summary_date == today:
            return
        if now.hour > sh or (now.hour == sh and now.minute >= sm):
            self._last_summary_date = today
            self._send_daily_summaries()

    def _send_daily_summaries(self):
        if not self._accounts:
            return
        url = self._router.resolve_url(None, 'default')
        if not url:
            self.log("⚠ Daily summary: no default webhook configured")
            return
        summary_time = self.cfg.get('summary_time', '22:00').strip()
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        window_lo  = today + ' 00:00:00'
        window_hi  = today + ' ' + summary_time + ':00'
        window_str = f"Period: {today} 00:00 → {summary_time}"
        rows = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, s in sorted(snapshot):
            all_entries = load_history_for(name)
            day_entries = [r for r in all_entries if window_lo <= r.get('time','') <= window_hi]
            counts = {}
            for r in day_entries:
                t = r.get('type','')
                if t and t != 'scan':
                    counts[t] = counts.get(t, 0) + 1
            start_ts    = s.script_start_ts or s.session_start
            uptime_str  = _fmt_duration(time.time() - start_ts)
            break_secs  = s.total_break_secs + (time.time() - s._break_start_ts if s._break_start_ts else 0)
            break_str   = _fmt_duration(break_secs) if break_secs > 0 else "0h 00m"
            rows.append({'account': name, 'quests': counts.get('quest_completed',0),
                         'tasks': counts.get('task',0), 'chats': counts.get('chat',0),
                         'errors': counts.get('error',0), 'drops': counts.get('drop',0),
                         'deaths': counts.get('death',0), 'levels': counts.get('levelup',0),
                         'uptime': uptime_str, 'break_str': break_str})
        acct_names = ', '.join(r['account'] for r in rows)
        self.log(f"📊 Sending daily summary — {len(rows)} account(s): {acct_names} [{window_str}]")
        ok, err = post_discord(url, combined_daily_summary_payload(self._router.mention(), rows, window_str))
        if not ok:
            self.log(f"  🚫 Daily summary failed: {err}")

    def _is_window_open(self, state):
        """Derive client-running state from log activity instead of xdotool.
        - On break within expected window → treat as open (script is alive, just paused)
        - On break past expected_end + 10min → treat as crashed
        - Otherwise → open if log was written to in the last 5 minutes
        """
        now = time.time()
        if state.on_break:
            if state.break_expected_end is None:
                # No expected end recorded — give generous 8h benefit of the doubt
                return True
            if now <= state.break_expected_end + 600:   # +10 minutes grace
                return True
            return False   # break ran out — likely crashed
        return (now - state.last_log_mtime) < 300       # 5 min stale = offline

    def _prune_dedupe(self):
        with self._offsets_lock:
            dead = [p for p in list(self._offsets) if not os.path.exists(p)]
            for p in dead:
                del self._offsets[p]

    # ── Main run loop ──────────────────────────────────────────────────────────
    def _run(self):
        dirs = self._get_log_dirs()
        if not dirs:
            self.log("⚠ No valid log directories configured")
            return
        for d in dirs:
            log_files = _get_log_files(d)
            active = next((f for f in reversed(log_files) if re.match(r'logfile-\d+\.log$', f.name)), None)
            for f in log_files:
                with self._offsets_lock:
                    if active and str(f) == str(active):
                        pass  # leave active file for _startup_catchup / poll loop
                    else:
                        self._offsets[str(f)] = f.stat().st_size  # rotated — skip
        account_names = [os.path.basename(d) for d in dirs]
        self.log(f"▶ Monitoring {len(dirs)} account(s): {', '.join(account_names)}")
        for d in dirs:
            log_files = _get_log_files(d)
            active = next((f for f in reversed(log_files) if re.match(r'logfile-\d+\.log$', f.name)), None)
            if active:
                self._startup_catchup(str(active))
                # Pin active file to EOF so poll loop only sees new content from here
                try:
                    with self._offsets_lock:
                        self._offsets[str(active)] = active.stat().st_size
                except Exception:
                    pass
            t = threading.Thread(target=self._backfill_history, args=(d,), daemon=True)
            t.start()
            with self._backfill_lock:
                self._backfill_threads = [x for x in self._backfill_threads if x.is_alive()]
                self._backfill_threads.append(t)

        interval        = int(self.cfg.get('check_interval', 5))
        ss_min          = int(self.cfg.get('screenshot_minutes', 60))
        last_periodic   = time.time()
        self.on_status()  # populate status tab immediately after startup catchup

        while self._running:
            current_dirs = self._get_log_dirs()
            for d in current_dirs:
                log_files = _get_log_files(d)
                active    = next((f for f in reversed(log_files) if re.match(r'logfile-\d+\.log$', f.name)), None)
                if not active:
                    continue
                self._check_file(str(active))
            now = time.time()
            if now - last_periodic >= 60:
                last_periodic = now
                self._check_screenshots(ss_min)
                self._check_daily_summary()
                self._prune_screenshots()
                self._prune_dedupe()
                self.on_status()
            time.sleep(interval)

    # ── Startup catchup ────────────────────────────────────────────────────────
    def _startup_catchup(self, active_path):
        """
        Called once per account at startup with the active log file path.
        Reads the current session (active file + any .log.1/.log.2 rotated
        parts of the same session) to set:
          script_start_ts  - timestamp of first 'Connecting to server' in oldest
                             session file (= DreamBot client session start time)
          total_break_secs - sum of ALL 'Break over N' ms across all session files
          logged_in / on_break - from the most recent state line in active file
          last_task / last_activity - from the last NEW TASK block in active file

        Session grouping: logfile-X.log is active; logfile-X.log.1, .log.2 etc.
        are rotated parts of the same DreamBot client session (same base name,
        higher N = older). Other logfile-Y files are different sessions.
        """
        try:
            active_path = str(active_path)
            folder = os.path.basename(os.path.dirname(active_path)) or \
                     os.path.splitext(os.path.basename(active_path))[0]
            state = self._get_account(folder, skip_backfill=True)
            # Use actual file mtime so stale accounts don't appear online for 5min
            try:
                state.last_log_mtime = Path(active_path).stat().st_mtime
            except Exception:
                state.last_log_mtime = 0.0

            # ── Identify all files in this session ────────────────────────────
            active_name = os.path.basename(active_path)  # e.g. logfile-X.log
            log_dir     = os.path.dirname(active_path)
            all_files   = _get_log_files(log_dir)        # sorted by name

            # Session files = active + any logfile-X.log.N (same base)
            session_files = [f for f in all_files
                             if f.name == active_name or
                             f.name.startswith(active_name + '.')]

            # Update session_file_set so get_account_rows doesn't re-trigger this
            state.session_file_set = frozenset(f.name for f in session_files)

            def _rot_key(f):
                """Sort key: .log = 0 (newest), .log.1 = -1, .log.2 = -2, ..."""
                n = f.name
                if n == active_name:
                    return 0
                try:
                    return -int(n.rsplit('.', 1)[1])
                except Exception:
                    return -999

            # Newest first for scanning; we'll reverse where needed
            session_files_newest_first = sorted(session_files, key=_rot_key, reverse=True)

            # ── Read active file lines (task/login scan) ───────────────────────
            try:
                with open(active_path, 'r', encoding='utf-8', errors='replace') as f:
                    active_lines = [l.rstrip('\n') for l in f]
            except Exception as e:
                self.log(f"⚠ [{folder}] Could not read active log: {e}")
                active_lines = []

            # ── Login / break state: scan active file backwards ───────────────
            break_start_log_ts = None
            break_length_ms    = None
            for line in reversed(active_lines):
                b = strip_prefix(line).strip().lower()
                if 'you have successfully been logged in' in b:
                    state.logged_in = True;  state.on_break = False;  break
                elif re.match(r'break over\s*(-?\d+)', b):
                    # Real completed break (Break over N ms) — not 'Break over -> Startup'
                    state.logged_in = True;  state.on_break = False;  break
                elif 'break start' in line.upper():
                    state.logged_in = False; state.on_break = True
                    m = LOG_TS_RE.match(line)
                    if m:
                        try:
                            break_start_log_ts = datetime.strptime(
                                m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                        except Exception:
                            pass
                    break

            # If we started mid-break, find the break length for expected_end calculation
            if state.on_break:
                from py.util import parse_break_length_ms
                last_break_idx = None
                for i, line in enumerate(active_lines):
                    if 'BREAK START' in line.upper():
                        last_break_idx = i
                if last_break_idx is not None:
                    break_length_ms = parse_break_length_ms(active_lines, last_break_idx + 1, max_search=3)
                if break_start_log_ts and break_length_ms:
                    state.break_expected_end = break_start_log_ts + break_length_ms / 1000.0

            # ── Uptime + break time: scan ALL session files ───────────────────
            # Walk oldest-first to find client start time and sum all completed breaks.
            # Uses timestamp math (BREAK START → Break over N) — ignores logged ms value
            # since DreamBot logs -100 for manually skipped breaks.
            session_files_oldest_first = list(reversed(session_files_newest_first))
            total_break_ms  = 0
            client_start_ts = None
            pending_break_start = None
            for sf in session_files_oldest_first:
                try:
                    sf_fh = open(str(sf), 'r', encoding='utf-8', errors='replace')
                except Exception as e:
                    self.log(f"⚠ [{folder}] Could not read session file {sf.name}: {e}")
                    continue
                with sf_fh:
                    for line in sf_fh:
                        line = line.rstrip('\n')
                        b    = strip_prefix(line).strip()
                        m    = LOG_TS_RE.match(line)
                        ts   = None
                        if m:
                            try:
                                ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                            except Exception:
                                pass
                        if 'BREAK START' in line.upper() and ts:
                            pending_break_start = ts
                        elif re.match(r'break over\s*(-?\d+)', b.lower()) and ts and pending_break_start:
                            duration_ms = (ts - pending_break_start) * 1000
                            if duration_ms > 0:
                                total_break_ms += duration_ms
                            pending_break_start = None
                        if client_start_ts is None and 'connecting to server' in b.lower() and ts:
                            client_start_ts = ts

            if client_start_ts:
                state.script_start_ts = client_start_ts

            # Set completed breaks FIRST, then add current in-progress elapsed.
            # Previously already_elapsed was added before this line and got overwritten.
            state.total_break_secs = total_break_ms / 1000.0
            if state.on_break and break_start_log_ts:
                already_elapsed = time.time() - break_start_log_ts
                state.total_break_secs += max(0.0, already_elapsed)
                state._break_start_ts = time.time()

            # ── Current task: use shared slice_last_task from reader.py ────────
            last_task, last_activity = slice_last_task(active_lines)

            if last_task or last_activity:
                state.last_task     = last_task
                state.last_activity = last_activity
                display = last_task or last_activity or '?'
                self.log(f"📋 [{folder}] Startup task: {display}" +
                         (f" / {last_activity}" if last_activity and last_task else ''))
            else:
                self.log(f"⚠ [{folder}] No task found in active log")

        except Exception as e:
            self.log(f"⚠ Startup scan error [{e.__class__.__name__}]: {e}")

    # ── Backfill ───────────────────────────────────────────────────────────────
    def _backfill_history(self, folder):
        """
        Scan all unscanned log files for this account and write history entries.
        Uses parse_lines() — no Discord, no screenshots, no state updates.
        Error events are skipped: only events that alerted live belong in history.
        Marks completed (rotated) files as scanned via record_log_scanned.
        Resume offsets for the active file come from offsets.json (self._offsets).
        """
        account = os.path.basename(folder)
        try:
            scanned  = get_scanned_logs(account)
            # Load resume offsets directly from disk — self._offsets has already been
            # set to EOF by _run before this thread started, so we can't use it here.
            # If history was cleared (scanned is empty), ignore stored offsets entirely
            # so we backfill from the beginning rather than resuming near EOF.
            resume_offsets = load_offsets() if scanned else {}
            log_files = _get_log_files(folder)
            if not log_files:
                return

            total_entries = 0
            files_scanned = 0
            active_fname  = log_files[-1].name  # newest = active

            for f in log_files:
                fname = f.name
                fstr  = str(f)
                is_active = (fname == active_fname)

                # For the active file: resume from offsets.json if available,
                # otherwise set to EOF so live monitor only sees new content.
                # For rotated files: always set to EOF (they won't grow).
                with self._offsets_lock:
                    stored_offset = resume_offsets.get(fstr)
                try:
                    eof = f.stat().st_size
                    with self._offsets_lock:
                        if is_active and stored_offset is not None:
                            self._offsets[fstr] = stored_offset
                        # rotated files already set to EOF by _run, leave them
                except Exception:
                    pass

                # Skip already-scanned rotated files only.
                # Always process the active file — it may have grown, and on upgrade
                # from older versions it may already have a stale scan record.
                if not is_active and fname in scanned:
                    continue

                try:
                    with open(fstr, 'r', encoding='utf-8', errors='replace') as fh:
                        if is_active and stored_offset:
                            fh.seek(stored_offset)

                        CHUNK = 500
                        entries_this_file = 0
                        bf_last_task     = ''
                        bf_last_activity = ''
                        chunk = []

                        def _process_chunk(chunk, bf_error_seen):
                            nonlocal entries_this_file, bf_last_task, bf_last_activity
                            if not chunk:
                                return
                            try:
                                events = parse_lines(chunk)
                            except Exception as pe:
                                self.log(f"  ⚠ [{account}] Backfill parse error in {fname}: {pe}")
                                return

                            # Reset error dedup on each NEW TASK boundary in this chunk
                            new_task_lines = {i for i, l in enumerate(chunk) if 'NEW TASK' in l.upper()}

                            # Stuck walking / Escaped ship detection — scan before events update task state
                            for stuck_idx, line in enumerate(chunk):
                                _reset_type = None
                                if 'Stuck walking -> Startup' in line:
                                    _reset_type = 'Stuck walking → Startup'
                                elif 'Escaped ship -> Startup' in line:
                                    _reset_type = 'Escaped ship → Startup'
                                if not _reset_type:
                                    continue
                                ctx_task = bf_last_task
                                ctx_act  = bf_last_activity
                                for prev in reversed(chunk[:stuck_idx]):
                                    pb = strip_prefix(prev).strip()
                                    if re.match(r'^Actually task is\s+', pb, re.IGNORECASE):
                                        ctx_task = re.sub(r'^Actually task is\s*', '', pb, flags=re.IGNORECASE).strip()
                                        if ctx_act:
                                            break
                                    elif re.match(r'^Task is\b', pb, re.IGNORECASE) and not ctx_task:
                                        ctx_task = re.sub(r'^Task is\s*', '', pb, flags=re.IGNORECASE).strip()
                                        if ctx_act:
                                            break
                                    elif re.match(r'^Activity is\s+', pb, re.IGNORECASE) and not ctx_act:
                                        ctx_act = re.sub(r'^Activity is\s*', '', pb, flags=re.IGNORECASE).strip()
                                        if ctx_task:
                                            break
                                task_ctx = f"{ctx_task} — {ctx_act}" if ctx_act else ctx_task
                                label    = task_ctx or f"Script reset ({_reset_type})"
                                reason   = f"Script reset: {_reset_type}"
                                ts_line  = next((LOG_TS_RE.match(l).group(1) for l in reversed(chunk[:stuck_idx+1]) if LOG_TS_RE.match(l)), '')
                                reset_ev = {
                                    'type': 'error', 'value': label, 'activity': reason, 'ts': ts_line,
                                    '_raw': (f'reset_{ts_line}', 1, 0, 600, reason),
                                    '_detail': reason, '_task_ctx': task_ctx,
                                }
                                if self.handle_event(reset_ev, account, source='backfill'):
                                    entries_this_file += 1

                            for ev in events:
                                etype    = ev.get('type', '')
                                value    = ev.get('value', '')
                                activity = ev.get('activity', '')
                                ts       = ev.get('ts', '')
                                line_idx = ev.get('_line_idx', -1)

                                # Config guard — same as live path
                                bf_guard = self._CFG_GUARD.get(etype)
                                if bf_guard and not self.cfg.get(bf_guard, True):
                                    continue

                                # Reset error dedup when we cross a NEW TASK boundary
                                if any(t <= line_idx for t in new_task_lines):
                                    last_nt = max((t for t in new_task_lines if t <= line_idx), default=-1)
                                    bf_error_seen.clear()
                                    new_task_lines = {t for t in new_task_lines if t > last_nt}

                                # ── State mutations (backfill task tracking) ──
                                if etype == 'task':
                                    bf_last_task     = value
                                    bf_last_activity = activity
                                elif etype == 'slayer_task':
                                    bf_last_task     = 'Slayer'
                                    bf_last_activity = activity
                                elif etype == 'slayer_complete':
                                    td, pe, tp = ev.get('_slayer_complete', (None, None, None))
                                    pts = f"+{pe:,} pts (total: {tp:,})" if pe else "no points yet"
                                    ev  = dict(ev, activity=pts)

                                # ── Error: enrich + backfill dedup ────────────
                                elif etype == 'error':
                                    raw = ev.get('_raw')
                                    if not raw:
                                        continue
                                    err_key = raw[0]
                                    if err_key in bf_error_seen:
                                        continue
                                    bf_error_seen.add(err_key)
                                    _, _, _, _, detail = raw
                                    lock_name    = ev.get('_lock_name', '')
                                    is_farm_skip = ev.get('_is_farm_skip', False)
                                    last_t = bf_last_task     or ''
                                    last_a = bf_last_activity or ''
                                    if last_t.lower() in ('break', ''):
                                        last_t = ''
                                        last_a = ''
                                    if lock_name:
                                        enriched_value = lock_name
                                    elif is_farm_skip or last_t:
                                        enriched_value = f"{last_t} — {last_a}" if last_a else last_t or value
                                    else:
                                        enriched_value = value
                                    task_ctx = f"{last_t} — {last_a}" if last_a else last_t
                                    ev = dict(ev, value=enriched_value,
                                              _detail=detail, _task_ctx=task_ctx)

                                # ── Fan out through unified dispatcher ────────
                                # source='backfill' → history only, no Discord/UI
                                if self.handle_event(ev, account, source='backfill'):
                                    entries_this_file += 1

                        bf_error_seen = set()  # dedup errors per task block across chunks
                        for raw_line in fh:
                            if not self._running:
                                return
                            chunk.append(raw_line.rstrip('\n'))
                            if len(chunk) >= CHUNK:
                                _process_chunk(chunk, bf_error_seen)
                                chunk = []
                        _process_chunk(chunk, bf_error_seen)  # flush remainder

                except Exception as e:
                    self.log(f"  ⚠ [{account}] Backfill read error {fname}: {e}")
                    continue

                # Only record completed rotated files as scanned.
                # Active file resume is handled by offsets.json, not history.
                if not is_active:
                    record_log_scanned(account, fname)
                total_entries  += entries_this_file
                files_scanned  += 1

            if files_scanned:
                if self._on_backfill_done:
                    self._on_backfill_done()

        except Exception as e:
            self.log(f"  ⚠ Backfill error [{account}]: {e}")

    # ── File polling ───────────────────────────────────────────────────────────
    def _check_file(self, path):
        try:
            size = os.path.getsize(path)
            with self._offsets_lock:
                offset = self._offsets.get(path, 0)
                if size < offset:
                    # File shrank — rotation detected. Migrate old offset to .log.1
                    # so backfill knows where it was already processed up to.
                    rotated_path = path + '.1'
                    self._offsets[rotated_path] = offset
                    self._offsets[path] = 0
                    offset = 0
                    self.log(f"🔄 Log rotated: {os.path.basename(path)}")
            if size <= offset:
                return
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(offset)
                new_text = f.read()
                new_offset = f.tell()
            with self._offsets_lock:
                self._offsets[path] = new_offset
            if not new_text.strip():
                return
            new_lines = new_text.splitlines()
            folder    = os.path.basename(os.path.dirname(path)) or os.path.splitext(os.path.basename(path))[0]
            # Update last_log_mtime so window_open can be derived without xdotool
            with self._accounts_lock:
                s = self._accounts.get(folder)
            if s is not None:
                s.last_log_mtime = time.time()
            self._process_lines(new_lines, folder)
        except Exception as e:
            self.log(f"⚠ Error reading {os.path.basename(path)}: {e}")

    def _get_account(self, folder, skip_backfill=False):
        with self._accounts_lock:
            is_new = folder not in self._accounts
            if is_new:
                self._accounts[folder] = AccountState(folder)
            s = self._accounts[folder]
        s.last_seen    = datetime.now().strftime('%m/%d/%y %H:%M')
        s.last_seen_ts = time.time()
        if is_new:
            threading.Thread(target=self._ensure_threads_for_account, args=(folder,), daemon=True).start()
            if not skip_backfill:
                t = threading.Thread(target=self._backfill_history, args=(folder,), daemon=True)
                t.start()
                with self._backfill_lock:
                    self._backfill_threads = [x for x in self._backfill_threads if x.is_alive()]
                    self._backfill_threads.append(t)
        return s

    # ── Unified event dispatcher ───────────────────────────────────────────────
    def handle_event(self, ev, account, *, source):
        """
        Fan a single normalized event out to all output legs independently.

        Caller is responsible for ALL filtering, dedupe, threshold checks,
        and state mutations before calling this method.
        handle_event() only persists and dispatches — it never decides whether
        an event should exist.

        source='live'     → history + UI callback + Discord
        source='backfill' → history only
        """
        etype    = ev.get('type', '')
        value    = ev.get('value', '')
        activity = ev.get('activity', '')
        ts       = ev.get('ts', '') or now_str()

        persist_history = True
        emit_ui         = (source == 'live')
        emit_discord    = (source == 'live')

        # ── Leg 1: History ────────────────────────────────────────────
        if persist_history:
            try:
                hist_etype = 'quest_completed' if etype == 'quest' else etype
                if etype == 'script_event':
                    append_history(account, 'script_event', activity, '', timestamp=ts)
                elif etype == 'drop':
                    dtype = (ev.get('_drop_types') or [activity])[0] if activity else 'drop'
                    append_history(account, 'drop', value, dtype, timestamp=ts)
                else:
                    append_history(account, hist_etype, value, activity, timestamp=ts)
            except Exception as e:
                self.log(f"⚠ [{account}] history write failed for {etype}: {e}")

        # ── Leg 2: UI callback ────────────────────────────────────────
        if emit_ui:
            try:
                ui_etype = 'quest_completed' if etype == 'quest' else etype
                ui_v     = 'died' if etype == 'death' else value
                ui_a     = ''     if etype == 'death' else activity
                self.on_event(ui_etype, account, ui_v, ui_a)
            except Exception as e:
                self.log(f"⚠ [{account}] on_event failed for {etype}: {e}")

        # ── Leg 3: Discord dispatch ───────────────────────────────────
        if emit_discord:
            try:
                mention = self._router.mention()
                if etype == 'quest_started':
                    self._router.post_event(account, 'quest',
                        quest_started_payload(mention, account, value))
                elif etype == 'quest':
                    self._router.post_event(account, 'quest',
                        quest_payload(mention, account, value))
                elif etype == 'task':
                    if 'slayer' not in (value or '').lower():
                        self._router.post_task(account, value, activity)
                elif etype == 'slayer_task':
                    self._router.post_event(account, 'task',
                        slayer_task_payload(mention, account, value, activity))
                elif etype == 'slayer_complete':
                    td, pe, tp = ev.get('_slayer_complete', (None, None, None))
                    self._router.post_event(account, 'task',
                        slayer_complete_payload(mention, account, value, td, pe, tp))
                elif etype == 'slayer_skip':
                    self._router.post_event(account, 'task',
                        slayer_skipped_payload(mention, account, value, activity))
                elif etype == 'chat':
                    self._router.post_event(account, 'chat',
                        chat_payload(mention, account, value, activity))
                elif etype == 'drop':
                    drop_types = ev.get('_drop_types', [activity])
                    self._router.post_drop(account, drop_types, value)
                elif etype == 'error':
                    detail   = ev.get('_detail', activity)
                    task_ctx = ev.get('_task_ctx', '')
                    self._router.post_event(account, 'error',
                        error_payload(mention, account, value, detail, task_ctx))
                elif etype == 'death':
                    url = self._router.resolve_url(account, 'death')
                    if url:
                        self._router.post_event(account, 'death',
                            death_payload(mention, account), url=url)
                elif etype == 'levelup':
                    level     = int(activity) if activity.isdigit() else 0
                    total_lvl = ev.get('_total_level')
                    url = self._router.resolve_url(account, 'levelup')
                    if url:
                        self._router.post_event(account, 'levelup',
                            levelup_payload(mention, account, value, level,
                                            total_level=total_lvl), url=url)
                elif etype == 'script_event':
                    self._router.post_script_event(account, value)
            except Exception as e:
                self.log(f"⚠ [{account}] discord dispatch failed for {etype}: {e}")

        return True

    # ── Process lines (live) ───────────────────────────────────────────────────
    def _process_lines(self, lines, folder):
        state  = self._get_account(folder)
        events = []

        # Update login/break state from this batch
        for idx, line in enumerate(lines):
            b = strip_prefix(line).strip()
            if 'BREAK START' in line.upper():
                state.on_break = True
                state.logged_in = False
                state._break_start_ts = time.time()
                from py.util import parse_break_length_ms
                bl_ms = parse_break_length_ms(lines, idx + 1, max_search=3)
                if bl_ms is not None:
                    state.break_expected_end = time.time() + bl_ms / 1000.0
            elif re.match(r'break over\s*(-?\d+)', b.lower()):
                # Real completed break (Break over N ms) — not 'Break over -> Startup'
                state.on_break = False
                state.logged_in = True
                state.break_expected_end = None
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts = None
            elif 'interacting (widget) logout' in b.lower():
                state.logged_in = False
                if state._break_start_ts is None:
                    state._break_start_ts = time.time()
            elif 'you have successfully been logged in' in b.lower():
                state.logged_in = True
                state.on_break  = False
                state.break_expected_end = None
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts = None
            elif 'starting p2p master ai now' in b.lower():
                m = LOG_TS_RE.match(line)
                if m:
                    try:
                        state.script_start_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                    except Exception:
                        pass

        # Parse all events through the unified reader pipeline.
        parsed = parse_lines(lines)

        for ev in parsed:
            etype    = ev['type']
            value    = ev.get('value', '')
            activity = ev.get('activity', '')
            ts       = ev.get('ts', '') or parse_log_ts(lines) or now_str()
            ev       = dict(ev, ts=ts)

            # ── Filtering: should this event be emitted at all? ───────
            guard_key = self._CFG_GUARD.get(etype)
            if guard_key and not self.cfg.get(guard_key, True):
                continue

            # ── State mutations + per-type filtering ──────────────────
            if etype == 'task':
                state.last_task     = value
                state.last_activity = activity
                display = value or activity or '?'
                self.log(f"📋 [{folder}] Task: {display}" + (f" / {activity}" if activity and value else ""))

            elif etype == 'slayer_task':
                state.last_task     = 'Slayer'
                state.last_activity = f"{activity} {value}"
                self.log(f"🗡️ [{folder}] New Slayer task: {activity} {value}")

            elif etype == 'slayer_complete':
                td, pe, tp = ev.get('_slayer_complete', (None, None, None))
                pts = f"+{pe:,} pts (total: {tp:,})" if pe else "no points yet"
                ev  = dict(ev, activity=pts)
                activity = pts
                self.log(f"✅ [{folder}] Slayer complete: {value} — {pts}")

            elif etype == 'slayer_skip':
                # Fallback to last known monster if cancel fired in a different poll
                if not value and state.last_activity:
                    parts = state.last_activity.split(' ', 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        value = parts[1]
                        ev    = dict(ev, value=value)
                self.log(f"⏭️ [{folder}] Slayer skipped: {value} — {activity}")

            elif etype == 'quest_started':
                self.log(f"📜 [{folder}] Quest started: {value}")

            elif etype == 'quest':
                self.log(f"🏆 [{folder}] Quest completed: {value}")

            elif etype == 'chat':
                self.log(f"💬 [{folder}] Chat: {value[:60]}")

            elif etype == 'drop':
                drop_types = ev.get('_drop_types', [activity])
                icons = ' '.join(DROP_ICONS.get(t, '🎁') for t in drop_types)
                self.log(f"{icons} [{folder}] Drop ({activity}): {value}")

            elif etype == 'error':
                raw = ev.get('_raw')
                if not raw:
                    continue
                raw_key, threshold, window_sec, dedupe_sec, detail = raw
                # Dedup check — caller's responsibility
                if not state.should_alert(raw_key, threshold, window_sec, dedupe_sec):
                    continue
                # Payload enrichment
                lock_name    = ev.get('_lock_name', '')
                is_farm_skip = ev.get('_is_farm_skip', False)
                last_t = state.last_task     or ''
                last_a = state.last_activity or ''
                if last_t.lower() in ('break', ''):
                    last_t = ''
                    last_a = ''
                if lock_name:
                    enriched_value = lock_name
                elif is_farm_skip or last_t:
                    enriched_value = f"{last_t} — {last_a}" if last_a else last_t or value
                else:
                    enriched_value = value
                task_ctx = f"{last_t} — {last_a}" if last_a else last_t
                ev = dict(ev, value=enriched_value, _detail=detail, _task_ctx=task_ctx)
                self.log(f"❌ [{folder}] {enriched_value}: {activity or detail}")

            elif etype == 'death':
                self.log(f"💀 [{folder}] Character died!")
                ev = dict(ev, value='Oh dear, you are dead!', activity='')

            elif etype == 'levelup':
                skill = value
                level = int(activity) if activity.isdigit() else 0
                notify_every = int(self.cfg.get('levelup_every', 5))
                last_notified = state.notified_levels.get(skill, 0)
                should_notify = (level // notify_every > last_notified // notify_every
                                 or last_notified == 0) if level else True
                state.notified_levels[skill] = level
                if not should_notify or self._is_muted(folder):
                    continue
                self.log(f"🎉 [{folder}] Level up: {skill} → {level}")

            elif etype == 'script_event':
                cfg_key_map = {
                    'start':  'monitor_script_start',  'stop':   'monitor_script_stop',
                    'pause':  'monitor_script_pause',   'resume': 'monitor_script_resume',
                }
                if not self.cfg.get(cfg_key_map.get(value, ''), True):
                    continue
                self.log(f"🖥️ [{folder}] {activity}")
                if self._is_muted(folder):
                    continue

            # ── Dispatch — caller has already decided this fires ──────
            self.handle_event(ev, folder, source='live')
            events.append(ev)

        # Standalone activity updates (no Discord, status only)
        for line in lines:
            b = strip_prefix(line).strip()
            if 'need a new slayer task' in b.lower() or 'getting new task' in b.lower():
                state.last_task     = 'Slayer'
                state.last_activity = 'Fetching task...'
            if b.lower().startswith('activity is ') and 'NEW TASK' not in ''.join(lines):
                act = re.sub(r'^Activity is\s*', '', b, flags=re.IGNORECASE).strip()
                if act and act != state.last_activity:
                    state.last_activity = act

            # ── Script reset mid-task ──────────────────────────────────
            # "Stuck walking -> Startup" / "Escaped ship -> Startup" means
            # the script gave up mid-task and reset. Fire an error immediately
            # (single occurrence) with the task that was abandoned.
            _reset_trigger = None
            if 'Stuck walking -> Startup' in line:
                _reset_trigger = ('Stuck walking → Startup', 'Script got stuck and teleported home to reset')
            elif 'Escaped ship -> Startup' in line:
                _reset_trigger = ('Escaped ship → Startup', 'Script escaped ship and teleported home to reset')
            if _reset_trigger and self.cfg.get('monitor_errors', True):
                last_t = state.last_task     or ''
                last_a = state.last_activity or ''
                if last_t.lower() in ('break', ''):
                    last_t = ''
                    last_a = ''
                task_display = f"{last_t} — {last_a}" if last_a else last_t
                task_ctx     = task_display
                label        = task_display or f"Script reset ({_reset_trigger[0]})"
                reason       = f"Script reset: {_reset_trigger[0]}"
                ts_line      = next((LOG_TS_RE.match(l).group(1) for l in reversed(lines) if LOG_TS_RE.match(l)), now_str())
                self.log(f"❌ [{folder}] {label}: {reason}")
                reset_ev = {
                    'type': 'error', 'value': label, 'activity': reason, 'ts': ts_line,
                    '_raw': (f'reset_{ts_line}', 1, 0, 600, _reset_trigger[1]),
                    '_detail': _reset_trigger[1], '_task_ctx': task_ctx,
                }
                self.handle_event(reset_ev, folder, source='live')

        return events

    # ── Bot wiring ─────────────────────────────────────────────────────────────
    def _run_bot_setup(self, log_fn=None):
        token     = self.cfg.get('bot_token', '').strip()
        server_id = self.cfg.get('bot_server_id', '').strip()
        if not token or not server_id:
            return False, "Bot token and Server ID are required"
        try:
            result = bot_setup_discord(token, server_id, log_fn=log_fn or self.log)
            self.cfg.update(result)
            self._save_cfg()
            self.log("🤖 Bot setup complete — channels and webhooks ready")
            for acc in list(self._accounts.keys()):
                self._ensure_threads_for_account(acc)
            return True, "OK"
        except Exception as e:
            self.log(f"🤖 Bot setup failed: {e}")
            return False, str(e)

    def _ensure_threads_for_account(self, account):
        token = self.cfg.get('bot_token', '').strip()
        if not token or not self.cfg.get('bot_setup_done'):
            return
        channel_ids = self.cfg.get('bot_channel_ids', {})
        if not channel_ids:
            return
        thread_ids  = self.cfg.get('bot_thread_ids', {})
        acct_threads = thread_ids.get(account, {})
        changed = False
        for ch_name, ch_id in channel_ids.items():
            if ch_name in acct_threads:
                continue
            tid = bot_ensure_thread(token, ch_id, account, log_fn=self.log)
            if tid:
                acct_threads[ch_name] = tid
                changed = True
        if changed:
            thread_ids[account] = acct_threads
            self.cfg['bot_thread_ids'] = thread_ids
            self._save_cfg()
            self.log(f"🤖 Threads ready for account: {account}")
            for tid in acct_threads.values():
                self._bot_add_user_to_thread(tid, token)

    # ── Bot screenshot helpers (called by GatewayRunner via callbacks) ──────────
    def _bot_screenshot_to_channel(self, account, channel_id, token):
        # Capture the currently focused window NOW (before DreamBot steals focus)
        # so the worker can restore it after the screenshot completes.
        restore_wid = get_focused_wid()
        self._enqueue_screenshot(SS_PRIORITY_ONDEMAND, account, 'bot-ss',
                                 bot_channel_id=channel_id, bot_token=token,
                                 restore_wid=restore_wid)


    def _bot_force_panel(self, account, action, channel_id, token):
        """
        Open a Stats/Loot panel, take a full-window screenshot using the
        already-focused window (inside the lock), post to the account's monitor
        thread, then close the panel.
        Runs in a daemon thread (called by GatewayRunner).
        """
        import subprocess
        from datetime import datetime
        from pathlib  import Path
        from py.screenshot import SCREENSHOT_DIR
        from py.discord    import post_bot_image
        from py.util       import get_display_env

        captured = {}

        def _do_capture():
            env = get_display_env()
            try:
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                # Find window — same technique as paint.py
                r = subprocess.run(
                    ['xdotool', 'search', '--name', account.lower()],
                    capture_output=True, text=True, timeout=5, env=env)
                wids = r.stdout.strip().split()
                if not wids:
                    self.log(f"  ⚠ [{account}] {action} panel: no window found for capture")
                    return
                wid      = wids[0]
                ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe     = re.sub(r'[^a-zA-Z0-9_-]', '_', account)
                out_path = str(SCREENSHOT_DIR / f"{safe}_{action}_{ts}.png")
                result   = subprocess.run(
                    ['import', '-window', wid, out_path],
                    capture_output=True, timeout=15, env=env)
                if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
                    captured['path'] = out_path
                else:
                    err = result.stderr.decode(errors='replace').strip()
                    self.log(f"  ⚠ [{account}] {action} panel capture failed: {err}")
            except Exception as e:
                self.log(f"  ⚠ [{account}] {action} panel capture error: {e}")

        do_force_panel(account, action,
                       screenshot_cb=_do_capture,
                       log=self.log,
                       window_lock=self._window_lock)

        path = captured.get('path')
        if not path:
            self.log(f"  ⚠ [{account}] {action} panel screenshot failed — nothing to post")
            return

        ok, err = post_bot_image(channel_id, token, account, path)
        if not ok:
            self.log(f"  ⚠ [{account}] {action} panel post failed: {err}")
        else:
            self.log(f"✅ [{account}] {action} panel posted to thread")
            try:
                os.remove(path)
            except Exception:
                pass

        user_id = self.cfg.get('mention_id', '').strip()
        if not user_id or not thread_id:
            return
        _, err = bot_api(token, 'PUT',
                         f'/channels/{thread_id}/thread-members/{user_id}')
        if err:
            self.log(f"🤖 Could not add user to thread {thread_id}: {err}")
        else:
            self.log(f"🤖 Added user to thread {thread_id}")


