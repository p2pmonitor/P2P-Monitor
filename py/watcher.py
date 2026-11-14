"""
watcher.py — Log watching engine for P2P Monitor v3.1.0
LogWatcher: discovers accounts, polls log files, drives backfill and live events.
Backfill and live monitor both call reader.parse_lines() — no more triple pipeline.

v3.1.0 (Detuks client): every account the monitor launches has its own Java
home, so it writes its own log (<home>/.detuksosrs/logs/client.log, rotated to
client_YYYY-MM-DD.N.log). The watcher reads one log folder per launcher
preset — the folder IS the account, like the DreamBot era. The client's normal
shared log (accounts started outside the monitor) is not read: its lines carry
no account name.
"""
import json
import os
import re
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from py.config  import (save_config, CLIENT_LOG_ACTIVE, CLIENT_LOG_ROTATED_RE,
                        is_client_log_name, folder_has_client_logs)
from py.util    import now_str, write_debug_entry, reset_debug_log
from py.reader  import parse_lines, parse_log_ts, strip_prefix, slice_last_task
from py.reader  import (LOG_TS_RE, is_ban_line, SCRIPT_START_RE, SCRIPT_STOP_RE,
                        SCRIPT_LOADED_RE, SCRIPT_GUI_WAIT_RE,
                        LAG_RE, COUNT_RE, COINS_RE, COUNT_MILESTONE_EVERY)
from py.history import (append_history,
                        get_last_seen, set_last_seen,
                        get_last_seen_meta,
                        load_history_for, load_offsets, save_offsets)
from py.paint        import do_force, do_force_skill, do_force_panel
from py.platform_ops import (get_open_log_handles, find_window_ids_by_name,
                             normalize_path, capture_window_image)

from py.discord import (post_discord, bot_api, bot_setup_discord, bot_ensure_thread,
                        verify_bot_channels,
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
from py.inferno import InfernoTracker


_IS_BREAK_START_RE = re.compile(r'BREAK START', re.IGNORECASE)
_IS_BREAK_OVER_RE  = re.compile(r'break over\s*(-?\d+)', re.IGNORECASE)

def _is_break_start(line):
    """True if this log line signals the start of a break."""
    return bool(_IS_BREAK_START_RE.search(line))

def _is_break_over(stripped_lower):
    """True if stripped lowercase line is a real completed break — not 'Break over -> Startup'."""
    return bool(_IS_BREAK_OVER_RE.match(stripped_lower))


# ── Session-state markers (prefix-stripped body) ──────────────────────────────
# Detuks strings first, DreamBot strings kept as harmless alternates.
def _is_script_loaded(b):
    """Profile loaded, GUI about to open — the script is NOT running yet."""
    return bool(SCRIPT_LOADED_RE.search(b))

def _is_script_start(b):
    """The script actually starts (Start pressed). DreamBot strings kept."""
    low = b.lower()
    return bool(SCRIPT_START_RE.search(b)) or 'script set to running' in low or 'awaiting login' in low

def _is_gui_wait(b):
    return bool(SCRIPT_GUI_WAIT_RE.search(b))

def _is_script_stop(b):
    return bool(SCRIPT_STOP_RE.search(b))

# v3.1.1 — who stopped the script, read from the line itself. In every Detuks
# log so far: the script stopping itself logs "Finished onExit" on its own
# worker thread (after a "…; stopping" reason), a stop by hand (plugin switch
# or closing the client) logs it on the UI thread (AWT-EventQueue-*), and a
# script update reload logs it on the UI thread right after the client's
# 'Stopping external plugin' — the client then loads the new version itself.
_LINE_THREAD_LOGGER_RE = re.compile(r'^\S+ \S+ \[([^\]]*)\]\s+\w+\s+(\S+)\s+-\s')
_PLUGIN_LOAD_RE = re.compile(r'loading external plugin "p2p"', re.I)


def _thread_and_logger(line):
    m = _LINE_THREAD_LOGGER_RE.match(line)
    return (m.group(1), m.group(2)) if m else ('', '')


def _logger_family(logger):
    """Package root of a logger ('com.d.a.a.Main' -> 'com.d.a'): every line
    the script writes shares it, the client's own lines do not."""
    parts = logger.split('.')
    return '.'.join(parts[:3]) if len(parts) >= 3 else logger


def _stop_kind(line, prior_lines):
    """'reload' | 'manual' | 'script' for a "Finished onExit" line."""
    if any('stopping external plugin' in p.lower() for p in prior_lines[-5:]):
        return 'reload'
    thread, _lg = _thread_and_logger(line)
    if thread.startswith('AWT-EventQueue'):
        return 'manual'
    return 'script'


def _is_plugin_load(b):
    return bool(_PLUGIN_LOAD_RE.search(b))


def _is_login(b):
    """Explicit 'now in game' lines. Detuks has no 'successfully logged in'
    line; it logs 'Relogged on world N' (script relog after hop/logout) and the
    client break handler's 'Logged back in, break cycle complete'. NEW TASK is
    handled separately (it also implies in game)."""
    low = b.lower()
    return ('you have successfully been logged in' in low
            or low.startswith('relogged on world')
            or 'logged back in, break cycle complete' in low)

def _is_logout(b):
    low = b.lower()
    return 'interacting (widget) logout' in low or low == 'logging out'


def _log_file_order(f):
    """Chronological sort key: rotated parts by (date, N), then the active
    client.log last. N keeps counting up across days, so (date, N) is a total
    order. Unknown names sort first."""
    name = getattr(f, 'name', None) or os.path.basename(str(f))
    if name == CLIENT_LOG_ACTIVE:
        return (2, '', 0)
    m = CLIENT_LOG_ROTATED_RE.match(name)
    if m:
        return (1, m.group(1), int(m.group(2)))
    return (0, name, 0)


def _get_log_files(folder):
    """All Detuks client log files in folder, oldest → newest."""
    try:
        files = [f for f in Path(folder).iterdir()
                 if f.is_file() and is_client_log_name(f.name)]
    except Exception:
        return []
    return sorted(files, key=_log_file_order)


def _get_active_log_file(folder):
    """The file the client is writing to — always client.log in the Detuks
    layout (rotated parts are never written again). None if absent."""
    p = Path(folder) / CLIENT_LOG_ACTIVE
    return p if p.is_file() else None


def _offset_since(path, since_epoch):
    """Byte offset of the first line stamped at/after since_epoch (log
    timestamps are local time), or the file size if there is none. Used when
    a per-account log appears mid-run (v3.1.0): lines written since the
    monitor started are processed live, older ones only go to history."""
    try:
        with open(path, 'rb') as fh:
            pos = 0
            for raw in fh:
                m = LOG_TS_RE.match(raw[:19].decode('ascii', 'replace'))
                if m:
                    try:
                        t = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
                    except Exception:
                        t = None
                    if t is not None and t >= since_epoch:
                        return pos
                pos += len(raw)
            return pos
    except Exception:
        try:
            return os.path.getsize(path)
        except Exception:
            return 0


def _newest_rotated_file(folder):
    rotated = [f for f in _get_log_files(folder) if f.name != CLIENT_LOG_ACTIVE]
    return rotated[-1] if rotated else None

def _fmt_duration(secs, zero='—'):
    """Format a duration in seconds as 'Xh YYm', or `zero` if zero/negative."""
    if secs <= 0:
        return zero
    h, rem = divmod(int(secs), 3600)
    return f"{h}h {rem // 60:02d}m"

# ── AccountState ───────────────────────────────────────────────────────────────
class AccountState:
    def __init__(self, name):
        self.name            = name
        self.last_task       = ''
        self.last_activity   = ''
        self.err_history     = {}
        self.err_alerted     = {}
        self.last_screenshot_ts = 0
        self.on_break        = False
        self.session_start   = time.time()
        self.script_start_ts = None
        self.logged_in       = False
        self.banned          = False  # ban detected in log; cleared on successful login (v2.2.0)
        self.total_break_secs    = 0
        self._break_start_ts     = None
        self._break_length_ms    = None   # parsed from "Break length N" log line; ms int or None
        self.script_running      = False  # True once script start confirmed; False on stop
        self.notified_levels     = {}
        self._startup_done       = False  # guards _startup_catchup to run only once per state
        self.inferno             = InfernoTracker()  # stateful Inferno gear-check and attempt tracker
        # ── Error burst debounce ──────────────────────────────────────────────
        # Groups rapid repeated lock/error events per account into one Discord alert.
        self._err_burst_events   = []   # list of ev dicts buffered this burst
        self._err_burst_timer    = None # threading.Timer handle
        self._err_burst_task_ctx = ''  # task context when burst started
        # Auto-restart state
        self._recent_lines       = deque(maxlen=30)  # rolling buffer; manual-stop signature check
        self._pending_restart_timer: 'threading.Timer | None' = None
        self._pre_stop_break_snap = None  # break snapshot captured before script-stop clears it
        # v3.0.0 Detuks extras (live only)
        self._lag_quiet_until    = 0.0    # epoch: ignore LAG DETECTED until (login/start warm-up)
        self.last_gp             = None   # last "Up to N coins available" value logged
        self.count_milestones    = {}     # count name -> last milestone notified
        # Script GUI / Start (v3.0.0): profile loaded, GUI open, Start not pressed
        self.awaiting_start      = False
        self._gui_wait_since     = None   # epoch the GUI wait was seen (live)
        self._autostart_timer    = None   # threading.Timer for the next auto-click step
        self._autostart_attempts = 0
        self._gui_alerted        = False  # "needs a click" alert already sent this wait
        # v3.1.1: stop kind, silence watchdog, script-never-started check
        self._last_stop_kind      = ''    # 'script' | 'manual' | 'reload' (last stop line)
        self._script_logger       = ''    # logger family of the script's lines ('com.d.a')
        self._last_script_line_ts = 0.0   # epoch (wall clock) the last script line was read live
        self._silence_alerted     = False # silence alert sent for the current silence
        self._plugin_load_timer   = None  # threading.Timer: plugin loaded, waiting for the script

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

    def __init__(self, log_cb, event_cb, status_cb, backfill_cb=None,
                 on_launch_cb=None, on_launch_all_cb=None,
                 on_relaunch_cb=None, on_relaunch_all_cb=None):
        self.log       = log_cb
        self.on_event  = event_cb
        self.on_status = status_cb
        self._on_backfill_done  = backfill_cb
        self._on_launch_cb      = on_launch_cb
        self._on_launch_all_cb  = on_launch_all_cb
        self._on_relaunch_cb     = on_relaunch_cb      # passed through to GatewayRunner
        self._on_relaunch_all_cb = on_relaunch_all_cb  # passed through to GatewayRunner
        self.relaunch_mgr        = None                # created in start()
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
        self._last_seen_cache  = {}    # account -> last line written; avoids disk I/O every 5s
        self._threads_verified = set() # accounts whose Discord thread membership confirmed this session
        self._threads_recovery_attempted = set()  # (account, ch_name) pairs already recovered this session — prevents loops
        self._threads_ensuring = set()            # accounts currently inside _ensure_threads_for_account
        self._threads_ensure_lock = threading.Lock()  # guards _threads_ensuring
        self._ss_svc   = None    # ScreenshotService — created in start()
        self._router   = None    # DiscordRouter — created in start()
        self.cfg = {}
        self._cached_dirs     = []
        self._dirs_last_check = 0
        self._last_summary_date = None
        self._ss_skip_log_ts = {}      # account -> last "due but gated" debug log time (10-min throttle)
        self._last_heartbeat = 0.0     # epoch float: last 24h heartbeat log
        self._loop_started   = 0.0     # epoch float: when the _run loop started

    def _dbg(self, msg):
        """Log msg only when debug mode is enabled in config."""
        if self.cfg and self.cfg.get('debug', False):
            self.log(f'[DEBUG] {msg}')

    def _debug_entry(self, category, payload=None, mirror_msg=None):
        """Always write a structured entry to debug.jsonl, regardless of the debug
        checkbox. If mirror_msg is given, also mirror a short readable line to the
        Monitor tab — but only when the debug checkbox is enabled (via _dbg)."""
        write_debug_entry(category, payload)
        if mirror_msg is not None:
            self._dbg(mirror_msg)

    # ── Dir discovery ──────────────────────────────────────────────────────────
    def _home_log_dirs(self):
        """{account: logs dir} — one per launcher preset (its Detuks home)."""
        try:
            from py.launcher import account_home_logs
            return account_home_logs(self.cfg)
        except Exception as e:
            self._dbg(f'home log dirs unavailable: {e}')
            return {}

    def _get_log_dirs(self):
        """Log folders that exist and hold a client log (an account's home
        gets one the first time it is launched)."""
        now = time.time()
        if now - self._dirs_last_check < 30 and self._cached_dirs:
            return self._cached_dirs
        home_map = self._home_log_dirs()
        self._dir_accounts = {normalize_path(d): a for a, d in home_map.items()}
        dirs = [d for _a, d in sorted(home_map.items())
                if os.path.isdir(d) and folder_has_client_logs(Path(d))]
        self._cached_dirs = dirs
        self._dirs_last_check = now
        return dirs

    # ── Log folder → account name ──────────────────────────────────────────────
    def _acct_for_dir(self, d):
        """Account a log folder belongs to: the launcher preset whose home it
        is. Unknown folders fall back to the folder name."""
        d = str(d)
        try:
            acct = getattr(self, '_dir_accounts', {}).get(normalize_path(d))
            if acct:
                return acct
            if not getattr(self, '_dir_accounts', None):
                self._get_log_dirs()
                acct = self._dir_accounts.get(normalize_path(d))
                if acct:
                    return acct
        except Exception:
            pass
        return os.path.basename(os.path.normpath(d))

    def _acct_for_path(self, path):
        return self._acct_for_dir(os.path.dirname(str(path)))

    # ── Start / stop ───────────────────────────────────────────────────────────
    def start(self, cfg):
        if self._running:
            return
        # Reset the structured session debug log for this run. Best-effort —
        # never blocks startup if it fails.
        reset_debug_log()
        self.cfg      = cfg
        self._running = True
        self._router  = DiscordRouter({
            'get_cfg':            lambda: self.cfg,
            'log':                self.log,
            'is_muted':           self._is_muted,
            'enqueue_screenshot': self._enqueue_screenshot,
            'invalidate_threads': lambda acct: self._threads_verified.discard(acct),
            'ensure_threads':     self._ensure_threads_for_account,
            'run_bot_setup':      lambda: self._run_bot_setup(),
            'save_cfg':           self._save_cfg,
        })
        self._bot_ready = threading.Event()
        self._ss_svc  = ScreenshotService({
            'get_cfg':          lambda: self.cfg,
            'log':              self.log,
            'is_muted':         self._is_muted,
            'wh_with_thread':   self._router.wh_with_thread,
            'window_lock':      self._window_lock,
            'bot_ready':        self._bot_ready,
            'handle_post_error': self._router._handle_post_error,
            'get_account_pid':  self._get_account_pid_cb,
            'set_account_pid':  self._set_account_pid_cb,
        })
        self._ss_svc.start()
        from py.relaunch import RelaunchManager
        self.relaunch_mgr = RelaunchManager(self)
        self.relaunch_mgr.start()
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
                'on_launch':      self._on_launch_cb,
                'on_launch_all':  self._on_launch_all_cb,
                'on_relaunch':     self._on_relaunch_cb,
                'on_relaunch_all': self._on_relaunch_all_cb,
                'is_running':    lambda: self._running,
                'get_cfg':       lambda: self.cfg,
                'save_cfg':      self._save_cfg,
            })
            runner.bot_ready = self._bot_ready  # share the same event
            self._bot_thread = threading.Thread(target=runner.run, daemon=True)
            self._bot_thread.start()
            threading.Thread(target=self._verify_discord_channels_startup, daemon=True).start()

    def stop(self):
        self._running = False
        mgr = getattr(self, 'relaunch_mgr', None)
        if mgr is not None:
            try:
                mgr.stop()   # persists pending relaunch state to disk
            except Exception:
                pass
        # Cancel any pending auto-restart timers before tearing down
        with self._accounts_lock:
            for state in self._accounts.values():
                t = getattr(state, '_pending_restart_timer', None)
                if t is not None:
                    t.cancel()
                    state._pending_restart_timer = None
                self._cancel_autostart(state)
                self._cancel_script_load_check(state)
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
            # Before saving offsets, EOF-pin any active log file that no client
            # process has open anymore. This prevents backfill from re-reading
            # those lines on next startup and duplicating history entries.
            scan = get_open_log_handles()
            # EOF-pin only when scan is reliable — skip if we can't trust
            # the result, to avoid pinning files that are actually still open
            if scan.reliable:
                for d in self._get_log_dirs():
                    active = _get_active_log_file(d)
                    if not active:
                        continue
                    fstr = str(active)
                    if normalize_path(fstr) not in scan.paths:
                        # No client has it open — pin to EOF
                        try:
                            self._offsets[fstr] = active.stat().st_size
                        except Exception:
                            pass
            else:
                self._dbg(f'stop(): handle scan unreliable ({scan.reason}) — skipping EOF pin')
            # Merge with existing disk contents to preserve __last_seen keys
            # written by set_last_seen() which go directly to disk, not self._offsets
            try:
                disk = load_offsets()
                disk.update(dict(self._offsets))
                save_offsets(disk)
            except Exception:
                save_offsets(dict(self._offsets))

    # ── Account row export ─────────────────────────────────────────────────────
    def get_account_rows(self):
        # v3.0.0: no per-session file switching any more — the client always
        # writes client.log, and rotation is handled inline by _check_file().
        rows = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, s in sorted(snapshot):
            if s.banned:
                status = '🔨 Banned'
            elif s.awaiting_start:
                status = '🟡 Waiting for Start'
            elif not s.script_running:
                status = '🔴 Offline'
            elif s.on_break or s._break_start_ts:
                status = '🟡 On Break'
            elif not s.logged_in:
                status = '🟡 Starting...'
            else:
                status = '🟢 Logged In'
            start_ts    = s.script_start_ts or s.session_start
            uptime_secs = time.time() - start_ts
            show_uptime = s.script_running or s.on_break or bool(s._break_start_ts)
            uptime_str  = _fmt_duration(uptime_secs) if show_uptime else '—'
            break_secs = s.total_break_secs
            if s._break_start_ts:
                break_secs += time.time() - s._break_start_ts
            break_str = (_fmt_duration(break_secs, zero='0h 00m')
                         if (s.script_running or s.on_break or s._break_start_ts) else '—')
            rows.append({'account': name, 'task': s.last_task or '—',
                         'activity': s.last_activity or '—', 'status': status,
                         'uptime': uptime_str, 'break_time': break_str,
                         'muted': name in self.cfg.get('muted_accounts', [])})
        return rows

    def get_uptime_rows(self):
        """
        Lightweight uptime/break tick — pure math, no I/O, no threads.
        Returns list of {account, uptime, break_time} for the minute ticker.
        """
        rows = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, s in sorted(snapshot):
            show_uptime = s.script_running or s.on_break or bool(s._break_start_ts)
            if show_uptime:
                start_ts    = s.script_start_ts or s.session_start
                uptime_str  = _fmt_duration(time.time() - start_ts)
            else:
                uptime_str  = '—'
            break_secs = s.total_break_secs
            if s._break_start_ts:
                break_secs += time.time() - s._break_start_ts
            rows.append({'account': name,
                         'uptime': uptime_str,
                         'break_time': (_fmt_duration(break_secs, zero='0h 00m')
                                        if show_uptime else '—')})
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

    def _get_account_pid_cb(self, account: str):
        """Callback: return cached launcher PID for account, or None."""
        try:
            from py.launcher import get_account_pid
            return get_account_pid(account)
        except Exception:
            return None

    def _set_account_pid_cb(self, account: str, pid) -> None:
        """Callback: save (or clear) launcher PID for account. Never raises."""
        try:
            from py.launcher import set_account_pid
            set_account_pid(account, pid)
        except Exception:
            pass

    def _startup_cache_pid(self, account: str) -> None:
        """
        Background daemon: discover the client window for account and cache
        its PID via the launcher state file. Called once per active account at
        startup so PID-first screenshot lookup works from the first screenshot.
        Fails completely silently — never affects startup or monitor behaviour.
        """
        try:
            from py.launcher import discover_account_process, set_account_pid
            result = discover_account_process(account)
            if result and result.get('pid'):
                set_account_pid(account, result['pid'])
                self._dbg(f'[pid_cache] [{account}] startup PID cached: {result["pid"]}')
            else:
                self._dbg(f'[pid_cache] [{account}] startup PID discovery: no result')
        except ValueError:
            # Multiple windows matched — ambiguous, do not cache
            self._dbg(f'[pid_cache] [{account}] startup PID discovery: ambiguous windows')
        except Exception as e:
            self._dbg(f'[pid_cache] [{account}] startup PID discovery failed: {e}')

    def _flush_error_burst(self, folder: str, state) -> None:
        """
        Flush the error burst buffer for an account.
        Called after debounce window expires (or immediately for standalone errors).
        Posts one grouped Discord alert for all buffered events; one monitor line.
        """
        evs = list(state._err_burst_events)
        state._err_burst_events  = []
        state._err_burst_timer   = None
        state._err_burst_task_ctx = ''
        if not evs:
            return

        # Single event — dispatch normally
        if len(evs) == 1:
            ev = evs[0]
            self.log(f"⚠ [{folder}] {ev.get('value', '')} — {ev.get('activity', '')}")
            self.handle_event(ev, folder, source='live')
            return

        # Multiple events — build grouped alert
        mention   = self._router.mention()
        task_ctx  = evs[0].get('_task_ctx', '') or state.last_task or ''
        details   = []
        seen      = set()
        for ev in evs:
            detail = ev.get('activity', '') or ev.get('value', '')
            if detail and detail not in seen:
                seen.add(detail)
                details.append(detail)

        # One monitor log line
        self.log(f"⚠ [{folder}] {len(evs)} related errors grouped — {task_ctx or 'multiple failures'}")

        # Persist each event to history
        for ev in evs:
            try:
                from py.history import append_history
                append_history(folder, 'error', ev.get('value', ''), ev.get('activity', ''),
                               timestamp=ev.get('ts'), log_fn=self.log,
                               debug=self.cfg.get('debug', False))
            except Exception:
                pass

        # One Discord embed
        from py.discord import apply_ping, _embed, post_discord
        fields = [
            {'name': 'Account',       'value': folder,          'inline': True},
            {'name': 'Task/Activity', 'value': task_ctx or '—', 'inline': True},
            {'name': 'Failures',      'value': '\n'.join(f'• {d}' for d in details[:20]), 'inline': False},
        ]
        title   = f"❌ Task Failure — {task_ctx}" if task_ctx else "❌ Task Failures"
        payload = _embed(title, f"**Account:** {folder}", fields, 0xff4444)
        apply_ping(payload, mention, self.cfg.get('ping_error', True))

        url = self._router.resolve_url(folder, 'error')
        if url and not self._is_muted(folder):
            ok, err = post_discord(url, payload)
            if not ok:
                self.log(f"  🚫 [{folder}] Grouped error Discord failed: {err}")

        # UI callback once
        try:
            self.on_event('error', folder, details[0] if details else 'Multiple errors', task_ctx)
        except Exception:
            pass

    def _maybe_burst_error(self, ev: dict, folder: str, state) -> bool:
        """
        Add an error event to the burst buffer. Returns True if buffered (caller skips normal dispatch).
        Only bursts 'lock' style errors — standalone errors go through normally.
        """
        # Only burst lock/skip events — identified by '_lock_name' key
        if '_lock_name' not in ev:
            return False

        # Cancel existing timer, add to buffer
        if state._err_burst_timer is not None:
            state._err_burst_timer.cancel()

        state._err_burst_events.append(ev)
        if not state._err_burst_task_ctx:
            state._err_burst_task_ctx = ev.get('_task_ctx', '') or state.last_task or ''

        # Schedule flush after 3-second debounce window
        t = threading.Timer(3.0, self._flush_error_burst, args=(folder, state))
        t.daemon = True
        t.start()
        state._err_burst_timer = t
        return True

    def trigger_screenshot(self, account):
        """Public API: on-demand screenshot (called by bot /ss command, monitor tab button)."""
        self._do_screenshot(account, 'on-demand')

    def _do_screenshot(self, account, trigger='scheduled'):
        if trigger == 'scheduled' and not self.cfg.get('screenshots_enabled'):
            self.log("⚠ Scheduled screenshots not enabled in Settings")
            return
        if self._is_muted(account):
            return
        priority = SS_PRIORITY_ONDEMAND if trigger == 'on-demand' else SS_PRIORITY_EVENT
        self._enqueue_screenshot(priority, account, trigger)

    def _enqueue_screenshot(self, priority, account, trigger,
                             url=None, payload=None,
                             bot_channel_id=None, bot_token=None, restore_wid=None):
        """Delegate to ScreenshotService — guards (enabled, muted) enforced there.
        Returns True if queued, False if refused (queue full, trigger-gated, muted)."""
        return self._ss_svc.enqueue(priority, account, trigger,
                                    url=url, payload=payload,
                                    bot_channel_id=bot_channel_id, bot_token=bot_token,
                                    restore_wid=restore_wid)

    # ── Periodic checks ────────────────────────────────────────────────────────
    def _prune_screenshots(self):
        self._ss_svc.prune()

    def _check_screenshots(self, ss_min):
        if not self.cfg.get('screenshots_enabled'):
            if not getattr(self, '_ss_disabled_logged', False):
                self._dbg('[ss] Scheduled screenshots disabled in Settings — skipping checks')
                self._ss_disabled_logged = True
            return
        self._ss_disabled_logged = False
        is_startup = not getattr(self, '_screenshots_started', False)
        self._screenshots_started = True
        threshold = ss_min * 60
        due = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, state in snapshot:
            if time.time() - state.last_screenshot_ts < threshold:
                continue  # not due yet — normal, no log
            # Account is due — if a gate blocks it, say why in debug mode.
            # These skips were previously silent, which made "screenshots
            # stopped, no errors anywhere" undiagnosable from the logs.
            # Throttled to one line per account per 10 min to avoid noise.
            skip = None
            if state.on_break or state._break_start_ts:
                skip = 'on break'
            elif not state.script_running:
                skip = 'offline'
            elif not state.logged_in:
                # Break-end race (v2.2.1): 'Break over' clears on_break while
                # the client is still logging in for 20s-minutes; firing now
                # bounces off a client whose title can't be matched. Stay due;
                # this check re-runs every 60s and fires right after login.
                skip = 'not logged in yet'
            elif self._is_muted(name):
                skip = 'muted'
            if skip:
                _log_ts = self._ss_skip_log_ts.get(name, 0)
                if time.time() - _log_ts >= 600:
                    self._ss_skip_log_ts[name] = time.time()
                    self._dbg(f'[ss] [{name}] screenshot due but account is {skip} — skipped')
                continue
            self._ss_skip_log_ts.pop(name, None)
            state.last_screenshot_ts = time.time()
            due.append(name)
        if not due:
            return
        for i, name in enumerate(due):
            if is_startup and not self.cfg.get('screenshot_on_startup', False):
                self._dbg(f'[ss] [{name}] startup screenshot disabled in Settings — skipped')
                continue
            trigger = 'startup' if is_startup else 'scheduled'
            if not self._enqueue_screenshot(SS_PRIORITY_SCHEDULED, name, trigger):
                self._dbg(f'[ss] [{name}] {trigger} screenshot enqueue refused')

    # ── Daily summary ──────────────────────────────────────────────────────────

    def _check_daily_summary(self):
        if not self.cfg.get('summary_enabled'):
            return
        summary_time = self.cfg.get('summary_time', '22:00').strip()
        try:
            sh, sm = [int(x) for x in summary_time.split(':')]
        except Exception as e:
            self._dbg(f'Daily summary skipped — bad summary_time value "{summary_time}": {e}')
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
        self._debug_entry('daily_summary',
            {'accounts': [r['account'] for r in rows], 'window': window_str},
            mirror_msg=f"📊 Sending daily summary — {len(rows)} account(s): {acct_names} [{window_str}]")
        ok, err = post_discord(url, combined_daily_summary_payload(self._router.mention(), rows, window_str))
        if not ok:
            self._debug_entry('daily_summary',
                {'accounts': [r['account'] for r in rows], 'window': window_str, 'error': str(err)},
                mirror_msg=f"  🚫 Daily summary failed: {err}")

    def _prune_dedupe(self):
        with self._offsets_lock:
            dead = [p for p in list(self._offsets) if not os.path.exists(p)]
            for p in dead:
                del self._offsets[p]

    def check_active_sessions(self):
        """
        Re-check open file handles for each tracked account. If a folder has no
        active file handle, flip script_running=False and clear break state.
        Called by the status tab on open and on manual refresh.
        Calls get_open_log_handles() once and reuses the result for all accounts.
        """
        scan = get_open_log_handles()
        if not scan.reliable:
            # Cannot trust handle scan — skip all offline transitions.
            # Only log once per session to avoid spamming the monitor tab.
            if not getattr(self, '_unreliable_scan_logged', False):
                for d in self._get_log_dirs():
                    folder = self._acct_for_dir(d)
                    self._dbg(f'handle scan unreliable ({scan.reason}) — '
                              f'skipping offline check for {folder}')
                self._unreliable_scan_logged = True
            self.on_status()
            return
        for d in self._get_log_dirs():
            folder = self._acct_for_dir(d)
            with self._accounts_lock:
                state = self._accounts.get(folder)
            if state is None:
                continue
            norm_folder = normalize_path(str(d))
            is_active = any(p.startswith(norm_folder + os.sep) or p == norm_folder
                            for p in scan.paths)
            if not is_active:
                if state.script_running or state.on_break or state._break_start_ts:
                    self.log(f'[proc] [{folder}] No active file handle — marking Offline')
                    state.script_running  = False
                    state.logged_in       = False
                    state.on_break        = False
                    state._break_start_ts = None
                    state.break_time      = 0
        self.on_status()

    def _is_folder_active(self, folder):
        """
        Check if any process has an open file handle in the given folder.
        Uses get_open_log_handles() from platform_ops (Linux: readlink /proc/*/fd/*)
        Returns True if a client process is actively writing to the folder.
        Returns False if no process has the folder open (stale/dead session).
        """
        try:
            scan = get_open_log_handles()
            if not scan.reliable:
                if not getattr(self, '_unreliable_scan_logged', False):
                    self._dbg(f'handle scan unreliable ({scan.reason}) — '
                              f'skipping offline check for {folder}')
                    self._unreliable_scan_logged = True
                return True  # Fail open — cannot confirm stale
            norm_folder = normalize_path(str(folder))
            return any(p.startswith(norm_folder + os.sep) or p == norm_folder
                       for p in scan.paths)
        except Exception as e:
            self._dbg(f'proc fd check failed for {folder}: {e}')
            return True  # Fail open — assume active if unavailable

    # ── Main run loop ──────────────────────────────────────────────────────────
    def _attach_new_dir(self, d):
        """A log folder that appeared after startup (v3.1.0: an account's
        Detuks home gets its .detuksosrs/logs on first launch). Reconstruct
        state from the whole log, process lines written since the monitor
        started as live events (Script Started, tasks…), backfill the rest."""
        acct = self._acct_for_dir(d)
        active = _get_active_log_file(d)
        for f in _get_log_files(d):
            if active and normalize_path(f) == normalize_path(active):
                continue
            try:
                with self._offsets_lock:
                    self._offsets[str(f)] = f.stat().st_size
            except Exception:
                pass
        if active and self._is_folder_active(d):
            try:
                self._startup_catchup(str(active))
            except Exception as e:
                self._dbg(f'[{acct}] catch-up of new log failed: {e}')
            with self._offsets_lock:
                self._offsets[str(active)] = _offset_since(str(active), self._run_started_wall)
        else:
            if active:
                try:
                    with self._offsets_lock:
                        self._offsets[str(active)] = active.stat().st_size
                except Exception:
                    pass
            with self._accounts_lock:
                if acct not in self._accounts:
                    self._accounts[acct] = AccountState(acct)
                self._accounts[acct]._startup_done = True
        self.log(f"▶ [{acct}] Monitoring its own log: {d}")
        t = threading.Thread(target=self._backfill_history, args=(d,), daemon=True)
        t.start()
        with self._backfill_lock:
            self._backfill_threads = [x for x in self._backfill_threads if x.is_alive()]
            self._backfill_threads.append(t)
        try:
            self.on_status()
        except Exception:
            pass

    def _run(self):
        self._run_started_wall = time.time()
        dirs = self._get_log_dirs()
        if not dirs:
            # Homes get their log on first launch; the poll loop attaches them.
            self.log("⏳ Waiting for accounts to be launched (Launcher tab or /launch) — "
                     "each launched account is picked up automatically.")
        for d in dirs:
            log_files = _get_log_files(d)
            active = _get_active_log_file(d)
            for f in log_files:
                with self._offsets_lock:
                    if active and normalize_path(f) == normalize_path(active):
                        pass  # leave active file for _startup_catchup / poll loop
                    else:
                        self._offsets[str(f)] = f.stat().st_size  # rotated — skip
        account_names = [self._acct_for_dir(d) for d in dirs]
        if dirs:
            self.log(f"▶ Monitoring {len(dirs)} account(s): {', '.join(account_names)}")
        self._known_dirs = {normalize_path(d) for d in dirs}
        for d in dirs:
            log_files = _get_log_files(d)
            active = _get_active_log_file(d)
            folder_active = self._is_folder_active(d)
            if active and folder_active:
                self._startup_catchup(str(active))
                # Background: discover and cache PID for this account so PID-first
                # screenshot lookup works immediately, even for clients not launched
                # by the monitor this session.
                _acct = self._acct_for_dir(d)
                threading.Thread(
                    target=self._startup_cache_pid,
                    args=(_acct,),
                    daemon=True,
                    name=f'pid-cache-{_acct}',
                ).start()
                # Pin active file to EOF so poll loop only sees new content from here
                try:
                    with self._offsets_lock:
                        self._offsets[str(active)] = active.stat().st_size
                except Exception as e:
                    self._dbg(f'Could not pin offset for {active.name}: {e}')
            else:
                # Stale log — create blank offline state, skip catchup entirely.
                # Pin client.log to EOF here too (v3.0.0): the Detuks log is ONE
                # long-lived shared file, so leaving the offset at 0/stale would
                # make the poll loop replay old lines as live events (Discord
                # spam). History for that span is the backfill's job.
                if active:
                    try:
                        with self._offsets_lock:
                            self._offsets[str(active)] = active.stat().st_size
                    except Exception as e:
                        self._dbg(f'Could not pin offset for {active.name}: {e}')
                folder_name = self._acct_for_dir(d)
                with self._accounts_lock:
                    if folder_name not in self._accounts:
                        self._accounts[folder_name] = AccountState(folder_name)
                    # Mark startup done so get_account_rows never re-triggers
                    # _startup_catchup on this offline account
                    self._accounts[folder_name]._startup_done = True
                self.log(f"📋 [{folder_name}] No active session — showing Offline")
            t = threading.Thread(target=self._backfill_history, args=(d,), daemon=True)
            t.start()
            with self._backfill_lock:
                self._backfill_threads = [x for x in self._backfill_threads if x.is_alive()]
                self._backfill_threads.append(t)

        # Accounts whose script GUI was already open and waiting at startup
        # get the same auto-click / alert handling as a live GUI wait.
        # Silence watchdog starts counting now for accounts already running
        # (their last log line may predate the monitor start).
        with self._accounts_lock:
            for _st in self._accounts.values():
                if _st.script_running and not _st._last_script_line_ts:
                    _st._last_script_line_ts = time.time()
        for _acct in sorted(getattr(self, '_startup_gui_waiting', set())):
            with self._accounts_lock:
                _st = self._accounts.get(_acct)
            if _st is not None and _st.awaiting_start:
                _st._gui_wait_since = time.time()
                self.log(f"⏸ [{_acct}] P2P Master AI is waiting at its GUI for Start")
                self._schedule_autostart(_acct, _st)

        interval        = int(self.cfg.get('check_interval', 5))
        ss_min          = int(self.cfg.get('screenshot_minutes', 60))
        last_periodic   = time.time()
        self.on_status()  # populate status tab immediately after startup catchup

        self._last_heartbeat = time.time()
        self._loop_started   = time.time()
        while self._running:
            # The entire loop body is guarded: one bad cycle must never kill
            # this thread. It runs event processing AND all periodic checks —
            # if it dies, events and scheduled screenshots stop silently while
            # the UI, gateway bot, and screenshot worker (separate threads)
            # keep working, which masks the failure entirely.
            try:
                current_dirs = self._get_log_dirs()
                for d in current_dirs:
                    if normalize_path(d) not in self._known_dirs:
                        self._known_dirs.add(normalize_path(d))
                        self._attach_new_dir(d)
                    active = _get_active_log_file(d)
                    if not active:
                        continue
                    self._check_file(str(active))
                now = time.time()
                if now - last_periodic >= 60:
                    last_periodic = now
                    # Each periodic check individually guarded so one failure
                    # can't starve the others (or kill the loop).
                    for _p_name, _p_fn in (
                            ('screenshot check',  lambda: self._check_screenshots(ss_min)),
                            ('daily summary',     self._check_daily_summary),
                            ('silence watchdog',  self._check_silence),
                            ('screenshot prune',  self._prune_screenshots),
                            ('dedupe prune',      self._prune_dedupe),
                            ('status refresh',    self.on_status),
                    ):
                        try:
                            _p_fn()
                        except Exception:
                            tb = traceback.format_exc()
                            self.log(f"⚠ Periodic {_p_name} failed — monitor continues")
                            self._dbg(f"[periodic] {_p_name} traceback:\n{tb}")
                # Heartbeat — one line per 24h so a dead loop is diagnosable at
                # a glance (last heartbeat older than a day = loop stopped then).
                if now - self._last_heartbeat >= 86400:
                    self._last_heartbeat = now
                    with self._accounts_lock:
                        _n_accounts = len(self._accounts)
                    self.log(f"💓 Monitor loop alive — {_n_accounts} account(s), "
                             f"up {_fmt_duration(now - self._loop_started)}")
                # Persist running client window positions every ~2 minutes so a
                # delayed relaunch / monitor restart can restore them (item:
                # window geometry forgotten after delayed relaunch). Runs on its
                # own daemon thread — window enumeration never blocks this loop.
                if now - getattr(self, '_last_geom_sweep', 0) >= 120:
                    self._last_geom_sweep = now
                    accounts_snapshot = list(self._accounts.keys())
                    if accounts_snapshot:
                        from py.launcher import persist_running_geometries
                        threading.Thread(
                            target=persist_running_geometries,
                            args=(self.cfg, accounts_snapshot), daemon=True).start()
            except Exception:
                tb = traceback.format_exc()
                self.log("⚠ Monitor cycle failed — monitor continues")
                self._dbg(f"[loop] cycle traceback:\n{tb}")
            time.sleep(interval)

    # ── Startup catchup ────────────────────────────────────────────────────────
    def _startup_catchup(self, active_path, is_rotation=False):
        """
        Called once per account at startup with the active log path (client.log).
        Reconstructs live state from the log in ONE forward pass:
          logged_in / on_break / script_running / banned — last state markers
          script_start_ts  - start of the CURRENT script run: the 'Start
                             button pressed' (or first NEW TASK) after the last
                             'started with profile' load line.
          awaiting_start   - loaded, GUI open, Start not pressed yet.
                             v3.0.0: uptime = time since that script start (the
                             shared client.log has no per-client session file).
          total_break_secs - completed breaks (BREAK START → Break over N,
                             timestamp math) within that run only
          last_task / last_activity - from the last NEW TASK block

        Files scanned: client.log, plus rotated parts (client_YYYY-MM-DD.N.log,
        newest first) until the beginning of the current run is included — a
        script load, Start press or stop line. The client rotates its log at
        midnight, so a run started yesterday (or days ago) has its start and
        earlier breaks in rotated parts (v3.1.1 — previously only one part
        was added, and only when client.log had no task at all, so uptime and
        break time were lost on any monitor start after midnight). At most
        _CATCHUP_MAX_ROTATED parts are read.

        is_rotation is accepted for call compatibility and ignored (v3.0.0:
        rotation is handled inline in _check_file).
        """
        try:
            active_path = str(active_path)
            log_dir     = os.path.dirname(active_path)
            folder      = self._acct_for_dir(log_dir)
            state       = self._get_account(folder, skip_backfill=True)

            def _read(path):
                try:
                    with open(str(path), 'r', encoding='utf-8', errors='replace') as fh:
                        return [l.rstrip('\n') for l in fh]
                except Exception as e:
                    self.log(f"⚠ [{folder}] Could not read {os.path.basename(str(path))}: {e}")
                    return []

            def _has_run_anchor(lines_):
                for l in lines_:
                    b_ = strip_prefix(l).strip()
                    if _is_script_loaded(b_) or _is_script_start(b_) or _is_script_stop(b_):
                        return True
                return False

            active_lines = _read(active_path)
            scan_lines   = active_lines
            used_files   = [os.path.basename(active_path)]
            if not _has_run_anchor(active_lines):
                rotated = [f for f in _get_log_files(log_dir) if f.name != CLIENT_LOG_ACTIVE]
                for prev in reversed(rotated[-self._CATCHUP_MAX_ROTATED:]):
                    prev_lines = _read(prev)
                    scan_lines = prev_lines + scan_lines
                    used_files.insert(0, prev.name)
                    if _has_run_anchor(prev_lines):
                        break

            self._dbg(f'[{folder}] _startup_catchup: files (oldest→newest): {used_files}')

            def _ts_of(line):
                m = LOG_TS_RE.match(line)
                if not m:
                    return None
                try:
                    return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                except Exception:
                    return None

            state.on_break       = False
            state.logged_in      = False
            state.script_running = False
            state.banned         = False
            state.awaiting_start = False
            gui_waiting         = False   # GUI wait seen since the last LOADED line
            gui_wait_ts         = None
            run_start_ts        = None
            break_start_log_ts  = None
            total_break_secs    = 0.0
            break_length_ms     = None

            from py.util import parse_break_length_ms
            for idx, line in enumerate(scan_lines):
                b = strip_prefix(line).strip()
                if _is_break_start(line):
                    state.on_break  = True
                    state.logged_in = False
                    break_start_log_ts = _ts_of(line)
                    break_length_ms = parse_break_length_ms(scan_lines, idx + 1, max_search=3)
                elif _is_break_over(b.lower()):
                    ts = _ts_of(line)
                    if ts and break_start_log_ts and ts > break_start_log_ts:
                        total_break_secs += ts - break_start_log_ts
                    state.on_break       = False
                    state.logged_in      = True
                    state.script_running = True
                    break_start_log_ts   = None
                    break_length_ms      = None
                elif _is_login(b):
                    state.on_break     = False
                    state.logged_in    = True
                    state.banned       = False
                    break_start_log_ts = None
                elif _is_script_loaded(b):
                    _lg_c = _thread_and_logger(line)[1]
                    if _lg_c:
                        state._script_logger = _logger_family(_lg_c)
                    # Profile loaded; GUI opens and waits for Start (v3.0.0).
                    # A fresh load ends any previous run.
                    state.script_running = False
                    state.awaiting_start = True
                    gui_waiting = False
                elif _is_gui_wait(b):
                    if state.awaiting_start:
                        gui_waiting = True
                        gui_wait_ts = _ts_of(line)
                elif _is_script_start(b) or ('NEW TASK' in b.upper() and state.awaiting_start):
                    if not state.script_running:
                        # A new run begins — uptime and break totals restart.
                        run_start_ts     = _ts_of(line)
                        total_break_secs = 0.0
                    state.script_running = True
                    state.awaiting_start = False
                    gui_waiting          = False
                    if 'NEW TASK' in b.upper() and not state.on_break:
                        state.logged_in = True
                elif 'solvers all finished' in b.lower():
                    state.script_running = True
                    state.logged_in      = True
                    state.on_break       = False
                elif 'NEW TASK' in b.upper() and state.script_running and not state.on_break:
                    # Script is processing tasks — definitively in game
                    state.logged_in = True
                elif _is_logout(b) and not state.on_break:
                    state.logged_in = False
                elif _is_script_stop(b):
                    state.script_running = False
                    state.awaiting_start = False
                    gui_waiting          = False
                    state.logged_in      = False
                    state.on_break       = False
                    break_start_log_ts   = None
                    run_start_ts         = None
                elif is_ban_line(b):
                    state.banned = True

            state.script_start_ts  = run_start_ts if state.script_running else None
            # GUI open and waiting at monitor start → same handling as live
            # (auto-click Start / "needs a click" alert) once startup completes.
            self._startup_gui_waiting = getattr(self, '_startup_gui_waiting', set())
            if state.awaiting_start and gui_waiting:
                if gui_wait_ts and time.time() - gui_wait_ts > self._GUI_WAIT_STALE_SEC:
                    # Hours-old GUI wait with nothing after it: the client was
                    # closed at the GUI. Don't show it as waiting or alert.
                    state.awaiting_start = False
                else:
                    self._startup_gui_waiting.add(folder)
            state.total_break_secs = total_break_secs
            state._break_start_ts  = None
            if state.on_break:
                state._break_length_ms = break_length_ms
                if break_start_log_ts and break_start_log_ts <= time.time():
                    state._break_start_ts = break_start_log_ts
                else:
                    state._break_start_ts = time.time()

            self._dbg(
                f'[{folder}] reconstructed state: '
                f'script_running={state.script_running} '
                f'logged_in={state.logged_in} '
                f'on_break={state.on_break} '
                f'script_start_ts={state.script_start_ts} '
                f'total_break_secs={state.total_break_secs:.1f}'
            )
            state._startup_done = True

            # ── Current task (shared slice_last_task from reader.py) ──────────
            last_task, last_activity = slice_last_task(scan_lines)
            if last_task or last_activity:
                state.last_task     = last_task
                state.last_activity = last_activity
                display = last_task or last_activity or '?'
                self.log(f"📋 [{folder}] Startup task: {display}" +
                         (f" / {last_activity}" if last_activity and last_task else ''))
            elif state.on_break:
                # Mid-break restart: the last NEW TASK block is followed by
                # BREAK START, so slice_last_task returns empty — show the break.
                from py.util import format_break_duration
                state.last_task     = 'Break'
                state.last_activity = format_break_duration(break_length_ms) if break_length_ms else ''
                self.log(f"📋 [{folder}] Startup task: Break" +
                         (f" / {state.last_activity}" if state.last_activity else ''))
            elif state.script_running:
                self.log(f"⚠ [{folder}] No task found in active log")

        except Exception as e:
            self.log(f"⚠ Startup scan error [{e.__class__.__name__}]: {e}")

    # ── Backfill ───────────────────────────────────────────────────────────────
    @staticmethod
    def _log_file_sort_key(f):
        """Stable file identity for the backfill resume checkpoint — the file
        NAME (client.log or client_YYYY-MM-DD.N.log). Chronological ordering
        uses _log_file_order(). A checkpoint naming client.log that no longer
        matches after a rotation is rejected by the line-equality check and
        falls back to the first-occurrence scan (bounded replay, never skip)."""
        return getattr(f, 'name', None) or os.path.basename(str(f))

    def _backfill_history(self, folder):
        """
        Scan all log files for this account and write missing history entries.

        Each folder is one account's own Detuks home log (v3.1.0), so all of
        its files belong to that account.

        Uses a last-seen-line approach instead of byte offsets or scanned-file sets:
        - All log files sorted chronologically by unix timestamp in filename
        - Scan forward through all lines until we find the last line seen in a
          previous session, then process everything after it
        - No rotation suffix tracking, no scanned sets, no base name stripping
        - Works correctly with .log, .log.1, .log.2 etc. — treated as one stream

        On first run (no last-seen line): processes all files, dedup cleans history.
        Subsequent runs: skips to last-seen line quickly, processes only new content.
        Never fires Discord or screenshots — append_history only.
        """
        account = self._acct_for_dir(folder)
        # ── Single-flight guard ─────────────────────────────────────────────
        # Only one backfill per account per session — insurance against any
        # concurrent spawn path double-appending the same unmarked span.
        with self._backfill_lock:
            if not hasattr(self, '_backfill_active'):
                self._backfill_active = set()
            if account in self._backfill_active:
                self._dbg(f'[backfill] {account}: already running — skipping duplicate spawn')
                return
            self._backfill_active.add(account)
        try:
            last_seen = get_last_seen(account)
            log_files = _get_log_files(folder)
            if not log_files:
                return

            # ── Idempotent-append preload ───────────────────────────────────
            # Existing history keys (time,type,value,activity): any event we
            # are about to append that already exists is skipped up front, so
            # a replayed span (stale marker, interrupted previous backfill…)
            # is a no-op instead of a duplicate. _dedup_history_file remains
            # as a rare repair net, not the normal startup path.
            existing_keys = set()
            try:
                from py.history import history_file as _hist_file
                _hf = _hist_file(account)
                if _hf.exists():
                    with open(_hf, 'r', encoding='utf-8', errors='replace') as fh:
                        for _line in fh:
                            try:
                                _e = json.loads(_line)
                                existing_keys.add((_e.get('time', ''), _e.get('type', ''),
                                                   _e.get('value', ''), _e.get('activity', '')))
                            except Exception:
                                continue
            except Exception as e:
                self._dbg(f'[backfill] {account}: could not preload history keys: {e}')

            # Chronological: rotated parts by (date, N), then client.log
            log_files = sorted(log_files, key=_log_file_order)
            # v3.1.0: the marker may come from a different log than this one
            # (account moved from the shared log to its own Detuks home, or the
            # marker's file was rotated away). If it is in none of the files,
            # resume can never match — treat it as a first run instead of
            # skipping everything (the idempotent preload absorbs overlap).
            if last_seen is not None:
                _present = False
                for _f in log_files:
                    try:
                        with open(str(_f), 'r', encoding='utf-8', errors='replace') as _fh:
                            if any(_ln.rstrip('\n\r') == last_seen for _ln in _fh):
                                _present = True
                                break
                    except Exception:
                        continue
                if not _present:
                    self._dbg(f'[backfill] {account}: resume marker not in any log file '
                              f'— processing as first run')
                    last_seen = None

            total_entries  = 0
            skipped_existing = 0
            new_last_seen  = None
            found_last     = (last_seen is None)  # if no marker, process everything
            # Diagnostics for debug.jsonl (category 'backfill')
            diag = {'account': account,
                    'marker_present': last_seen is not None,
                    'marker_found': last_seen is None,   # trivially "found" when absent
                    'marker_file': None,
                    'first_processed_file': None,
                    'first_processed_line': None,
                    'appended_per_file': {},
                    'resume_mode': None,
                    'files_skipped_before_marker': 0}

            bf_last_task     = ''
            bf_last_activity = ''
            # Run state across chunks/files so backfilled history only gets a
            # 'Script Started' for runs that really began (v3.0.0).
            bf_running  = False
            bf_awaiting = False
            # One InfernoTracker per account for the full backfill — state must
            # persist across chunks so gear-check windows and attempt waves that
            # span chunk boundaries are handled correctly.
            bf_inferno = InfernoTracker()

            for f in log_files:
                fstr = str(f)
                try:
                    with open(fstr, 'r', encoding='utf-8', errors='replace') as fh:
                        lines = fh.readlines()
                except Exception as e:
                    self._dbg(f'Backfill read error {f.name}: {e}')
                    continue

                # Strip newlines
                lines = [l.rstrip('\n\r') for l in lines]

                # ── Resume-point matching ───────────────────────────────────
                # Direction of safety: with idempotent appends in place, a
                # REPLAY is a harmless no-op (existing keys are skipped), but
                # a SKIP silently loses events forever. So resume must never
                # land later than the true checkpoint:
                #   1. Fast path — a structured checkpoint (file identity +
                #      absolute line index) written with every marker persist.
                #      Valid only if its stored line still matches both the
                #      current marker AND the line at that index (a live-loop
                #      marker update, or any file surprise, invalidates it).
                #   2. Fallback — FIRST-occurrence text scan. Client logs
                #      contain thousands of exact duplicate lines, so a
                #      later duplicate of a mid-file checkpoint line must
                #      never be trusted: first occurrence can only resume
                #      at-or-before the true position (bounded replay,
                #      absorbed by the idempotent preload), never after it.
                if not found_last and last_seen:
                    resume_idx = None
                    meta = get_last_seen_meta(account)
                    if (meta and meta.get('line') == last_seen
                            and meta.get('file_key') == self._log_file_sort_key(f)):
                        mi = meta.get('line_index', -1)
                        if isinstance(mi, int) and 0 <= mi < len(lines) \
                                and lines[mi] == last_seen:
                            resume_idx = mi
                            diag['resume_mode'] = 'checkpoint'
                    if resume_idx is None:
                        for i, line in enumerate(lines):
                            if line == last_seen:
                                resume_idx = i
                                diag['resume_mode'] = 'first_occurrence'
                                break
                    if resume_idx is None:
                        # Marker not in this file — skip entire file
                        diag['files_skipped_before_marker'] += 1
                        continue
                    found_last = True
                    diag['marker_found'] = True
                    diag['marker_file'] = f.name
                    base_idx = resume_idx + 1        # absolute index of lines[0] below
                    lines = lines[resume_idx + 1:]   # process only lines after marker
                else:
                    base_idx = 0

                if not lines:
                    continue
                if diag['first_processed_file'] is None:
                    diag['first_processed_file'] = f.name
                    diag['first_processed_line'] = lines[0][:160]

                # Process lines in chunks
                CHUNK = 500
                chunk = []
                entries_this_file = 0
                # Tail of recent raw lines from the previous chunk *within
                # this file*, carried forward purely for the Wine of
                # Zamorak death-suppression check's cross-batch backward
                # lookback (same purpose as _process_lines's
                # _prior_context in the live path) — a Wine interaction
                # and its death line could in principle straddle a
                # 500-line chunk boundary in an old log just as they can
                # straddle a live poll boundary. Deliberately reset for
                # each new file (not carried across file boundaries) —
                # separate log files are separate sessions, with no real
                # continuity to preserve.
                bf_recent_tail = []

                def _process_chunk(chunk):
                    nonlocal entries_this_file, bf_last_task, bf_last_activity, \
                             bf_recent_tail, skipped_existing, bf_running, bf_awaiting
                    if not chunk:
                        return
                    _genuine = set()
                    for _i, _ln in enumerate(chunk):
                        _b = strip_prefix(_ln).strip()
                        if _is_script_stop(_b):
                            bf_running = bf_awaiting = False
                        elif _is_script_loaded(_b):
                            bf_running, bf_awaiting = False, True
                        elif _is_script_start(_b) or ('NEW TASK' in _b.upper() and bf_awaiting):
                            if not bf_running:
                                _genuine.add(_i)
                            bf_running, bf_awaiting = True, False
                    try:
                        events = parse_lines(chunk, context_lines=bf_recent_tail)
                        events = self._filter_start_events(events, _genuine, chunk)
                    except Exception as pe:
                        self.log(f'  ⚠ [{account}] Backfill parse error: {pe}')
                        return
                    finally:
                        # Always refresh the tail, even on a parse error for this
                        # chunk — the next chunk's lookback should still reflect
                        # what was actually in the log, not stale earlier context.
                        bf_recent_tail = chunk[-30:]

                    for idx, ev in enumerate(events):
                        if not ev or ev.get('type') == 'error':
                            continue
                        etype = ev.get('type', '')
                        v1    = ev.get('value', '')
                        v2    = ev.get('activity', '')

                        if etype == 'task':
                            bf_last_task     = v1
                            bf_last_activity = v2
                        elif etype in ('slayer_task', 'quest_started', 'quest_completed',
                                       'drop', 'death', 'levelup', 'chat'):
                            if not v2 and bf_last_activity:
                                ev['activity'] = bf_last_activity

                        key = (ev.get('ts', ''), ev.get('type', ''),
                               ev.get('value', ''), ev.get('activity', ''))
                        if key in existing_keys:
                            skipped_existing += 1
                            continue
                        existing_keys.add(key)
                        append_history(account,
                                       ev.get('type', ''),
                                       ev.get('value', ''),
                                       ev.get('activity', ''),
                                       timestamp=ev.get('ts'))
                        entries_this_file += 1

                    # (last_seen marker updated at file level after all chunks)

                    # ── Inferno backfill ──────────────────────────────────────
                    # Feed same chunk through the Inferno tracker (state persists
                    # across chunks via bf_inferno declared in outer scope).
                    # Only write history — no Discord, no on_event, no status update.
                    try:
                        _bf_ui, bf_inferno_disc = bf_inferno.feed(chunk)
                        for inf_ev in bf_inferno_disc:
                            key = (inf_ev.get('ts', ''), 'task', 'Inferno',
                                   inf_ev.get('value', ''))
                            if key in existing_keys:
                                skipped_existing += 1
                                continue
                            existing_keys.add(key)
                            append_history(
                                account,
                                'task',
                                'Inferno',
                                inf_ev.get('value', ''),
                                timestamp=inf_ev.get('ts'),
                            )
                            entries_this_file += 1
                    except Exception as ie:
                        self._dbg(f'Inferno backfill error [{account}]: {ie}')

                chunks_done = 0
                lines_consumed = 0        # count of `lines` entries fed to chunks
                file_key = self._log_file_sort_key(f)
                for line in lines:
                    chunk.append(line)
                    lines_consumed += 1
                    if len(chunk) >= CHUNK:
                        _process_chunk(chunk)
                        chunks_done += 1
                        # Persist the marker every ~2,500 lines so a monitor
                        # restart mid-backfill resumes here instead of
                        # replaying the whole span from the old marker (the
                        # observed cause of the duplicate history entries —
                        # previously the marker was only written once, after
                        # the entire backfill finished). The structured meta
                        # (file identity + absolute line index) makes the
                        # resume exact even though this mid-file line's text
                        # may repeat later in the file.
                        if chunks_done % 5 == 0:
                            set_last_seen(account, chunk[-1], file_key=file_key,
                                          line_index=base_idx + lines_consumed - 1)
                            self._last_seen_cache[account] = chunk[-1]
                        chunk = []
                if chunk:
                    _process_chunk(chunk)

                # Always update last_seen to the final line of this file,
                # regardless of whether it produced parseable events —
                # persisted NOW (per file), not just at the very end.
                if lines:
                    new_last_seen = lines[-1]
                    set_last_seen(account, new_last_seen, file_key=file_key,
                                  line_index=base_idx + len(lines) - 1)
                    self._last_seen_cache[account] = new_last_seen

                total_entries += entries_this_file
                if entries_this_file:
                    diag['appended_per_file'][f.name] = entries_this_file

            dupes = 0
            if total_entries:
                # Dedup after backfill — now a rare repair net (appends are
                # idempotent), not the normal startup path.
                try:
                    from py.history import _dedup_history_file, history_file
                    hf = history_file(account)
                    _, dupes = _dedup_history_file(hf, log_fn=self.log,
                                                    debug=self.cfg.get('debug', False),
                                                    account=account)
                    if dupes:
                        self.log(f'🧹 [{account}] Cleaned {dupes} duplicate history entries')
                except Exception as e:
                    self._dbg(f'History dedup failed [{account}]: {e}')

            # Update last-seen marker (already persisted incrementally per
            # file/chunk above — this is a final safety write)
            if new_last_seen:
                set_last_seen(account, new_last_seen)
                self._last_seen_cache[account] = new_last_seen

            # Structured diagnostics — one entry per backfill run, so the next
            # duplicate report is answerable from debug.jsonl alone.
            try:
                from py.util import write_debug_entry
                diag.update({'entries_appended': total_entries,
                             'skipped_existing': skipped_existing,
                             'dupes_removed': dupes})
                write_debug_entry('backfill', diag)
            except Exception:
                pass

            if self._on_backfill_done:
                self._on_backfill_done()

        except Exception as e:
            self.log(f'  ⚠ Backfill error [{account}]: {e}')
        finally:
            with self._backfill_lock:
                try:
                    self._backfill_active.discard(account)
                except Exception:
                    pass

    # ── File polling ───────────────────────────────────────────────────────────
    def _check_file(self, path):
        try:
            size = os.path.getsize(path)
            rotated_tail = []
            with self._offsets_lock:
                offset = self._offsets.get(path, 0)
            if size < offset:
                # client.log shrank — the client rotated it to
                # client_YYYY-MM-DD.N.log and started a fresh client.log.
                # Lines written between our last poll and the rotation now sit
                # at the end of the newest rotated part: read that tail first so
                # they are not lost, then continue from the start of client.log.
                prev = _newest_rotated_file(os.path.dirname(path))
                if prev is not None:
                    try:
                        if prev.stat().st_size >= offset:
                            with open(str(prev), 'r', encoding='utf-8', errors='replace') as f:
                                f.seek(offset)
                                rotated_tail = f.read().splitlines()
                            with self._offsets_lock:
                                self._offsets[str(prev)] = prev.stat().st_size
                    except Exception as e:
                        self._dbg(f'rotation tail read failed for {prev.name}: {e}')
                with self._offsets_lock:
                    self._offsets[path] = 0
                offset = 0
                self.log(f"🔄 Log rotated: {os.path.basename(path)}"
                         + (f" (+{len(rotated_tail)} line(s) recovered from {prev.name})"
                            if rotated_tail and prev is not None else ''))
            new_lines = []
            if size > offset:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(offset)
                    new_text = f.read()
                    new_offset = f.tell()
                with self._offsets_lock:
                    self._offsets[path] = new_offset
                new_lines = new_text.splitlines()
            new_lines = rotated_tail + new_lines
            if not any(l.strip() for l in new_lines):
                return
            account = self._acct_for_path(path)
            self._process_lines(new_lines, account)
            # Update last-seen marker so backfill knows where to resume.
            # Only write to disk when the value actually changes — avoids I/O every 5s.
            try:
                last_line = new_lines[-1]
                if last_line != self._last_seen_cache.get(account):
                    set_last_seen(account, last_line)
                    self._last_seen_cache[account] = last_line
            except Exception:
                pass
        except Exception as e:
            self.log(f"⚠ Error reading {os.path.basename(path)}: {e}")

    def _get_account(self, folder, skip_backfill=False):
        with self._accounts_lock:
            is_new = folder not in self._accounts
            if is_new:
                self._accounts[folder] = AccountState(folder)
            s = self._accounts[folder]
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
    #
    # Event dict contract — keys emitted by reader.parse_lines() and consumed here:
    #
    #   ALWAYS PRESENT:
    #     type       str   — event type: task, slayer_task, slayer_complete, slayer_skip,
    #                        quest, quest_started, drop, death, levelup, script_event,
    #                        chat, error
    #     value      str   — primary value (task name, monster, item, skill, label…)
    #     activity   str   — secondary value (count, level, reason, drop type…)
    #     ts         str   — ISO timestamp from log line (or now_str() fallback)
    #
    #   OPTIONAL — set by specific event types:
    #     _drop_types       list[str]   — drop categories e.g. ['pet', 'collection']
    #                                     (drop events only)
    #     _slayer_complete  tuple       — (tasks_done, points_earned, total_points)
    #                                     (slayer_complete events only)
    #     _total_level      int         — total level reached (levelup/total events only)
    #     _raw              tuple       — (key, threshold, window_sec, dedupe_sec, detail)
    #                                     (error events only — used for threshold/dedup)
    #     _detail           str         — human-readable error detail (error events only)
    #     _task_ctx         str         — task context string for error/death embed enrichment
    #     _lock_name        str         — locked task/quest name (lock error events only)
    #     _is_farm_skip     bool        — True for farming patch skip errors
    #     _line_idx         int         — source line index in the parsed batch
    #
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
                    append_history(account, 'script_event', activity, '', timestamp=ts,
                                        log_fn=self.log, debug=self.cfg.get('debug', False))
                elif etype == 'drop':
                    dtype = (ev.get('_drop_types') or [activity])[0] if activity else 'drop'
                    append_history(account, 'drop', value, dtype, timestamp=ts,
                                        log_fn=self.log, debug=self.cfg.get('debug', False))
                else:
                    append_history(account, hist_etype, value, activity, timestamp=ts,
                                        log_fn=self.log, debug=self.cfg.get('debug', False))
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
                from py.discord import apply_ping

                def _ping(payload, key):
                    """Apply ping if cfg key is enabled and mention is set."""
                    enabled = self.cfg.get(f'ping_{key}', False)
                    return apply_ping(payload, mention, enabled)

                if etype == 'quest_started':
                    self._router.post_event(account, 'quest',
                        _ping(quest_started_payload(mention, account, value), 'quest'))
                elif etype == 'quest':
                    self._router.post_event(account, 'quest',
                        _ping(quest_payload(mention, account, value), 'quest'))
                elif etype == 'task':
                    if 'slayer' not in (value or '').lower():
                        self._router.post_task(account, value, activity,
                                               location=ev.get('_location', ''),
                                               planned_min=ev.get('_planned_min'))
                elif etype == 'slayer_task':
                    self._router.post_event(account, 'task',
                        _ping(slayer_task_payload(mention, account, value, activity), 'task'))
                elif etype == 'slayer_complete':
                    td, pe, tp = ev.get('_slayer_complete', (None, None, None))
                    self._router.post_event(account, 'task',
                        _ping(slayer_complete_payload(mention, account, value, td, pe, tp), 'task'))
                elif etype == 'slayer_skip':
                    self._router.post_event(account, 'task',
                        _ping(slayer_skipped_payload(mention, account, value, activity), 'task'))
                elif etype == 'chat':
                    self._router.post_event(account, 'chat',
                        _ping(chat_payload(mention, account, value, activity), 'chat'))
                elif etype == 'drop':
                    drop_types = ev.get('_drop_types', [activity])
                    self._router.post_drop(account, drop_types, value)
                elif etype == 'error':
                    detail   = ev.get('_detail', activity)
                    task_ctx = ev.get('_task_ctx', '')
                    self._router.post_event(account, 'error',
                        _ping(error_payload(mention, account, value, detail, task_ctx), 'error'))
                elif etype == 'death':
                    url = self._router.resolve_url(account, 'death')
                    if url:
                        task_ctx = ev.get('_task_ctx', '')
                        self._router.post_event(account, 'death',
                            _ping(death_payload(mention, account, task_context=task_ctx), 'death'), url=url)
                elif etype == 'levelup':
                    if ev.get('_suppress_discord'):
                        pass  # below "Skip level notifications below N" — Discord only
                    else:
                        level     = int(activity) if activity.isdigit() else 0
                        total_lvl = ev.get('_total_level')
                        is_99     = ev.get('_is_99', False)
                        url = self._router.resolve_url(account, 'levelup')
                        if url:
                            self._router.post_event(account, 'levelup',
                                _ping(levelup_payload(mention, account, value, level,
                                                total_level=total_lvl, is_99=is_99), 'levelup'), url=url)
                elif etype == 'script_event':
                    ar_detail = ''
                    if value == 'stop':
                        ar_status = ev.get('_ar_status', '')
                        if ar_status:
                            ar_detail = f'Auto restart: {ar_status}'
                    elif value == 'start' and ev.get('_ar_cancelled'):
                        ar_detail = 'Auto restart cancelled — script started before scheduled restart.'
                    self._router.post_script_event(account, value, detail=ar_detail)
            except Exception as e:
                self.log(f"⚠ [{account}] discord dispatch failed for {etype}: {e}")

        return True

    # ── Auto-restart helpers ───────────────────────────────────────────────────

    @staticmethod
    def _in_game_update_window() -> bool:
        """
        Return True when within the hardcoded game update window:
            Tuesday   1:00 AM – 4:00 AM  America/Los_Angeles
            Wednesday 1:00 AM – 4:00 AM  America/Los_Angeles

        Primary path: stdlib zoneinfo (Python 3.9+).
        Fallback:     manual UTC-offset using US DST rules — for Windows frozen
                      builds where IANA tzdata is not bundled.
        Returns False on any unexpected error (fail-safe, do not auto-restart).
        """
        import datetime as _dt

        # ── Primary: zoneinfo with IANA data ──────────────────────────────────
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo('America/Los_Angeles'))
            return now.weekday() in (1, 2) and 1 <= now.hour < 4
        except Exception:
            pass  # ZoneInfoNotFoundError on Windows without tzdata, or other error

        # ── Fallback: manual US Pacific offset from UTC ────────────────────────
        # US DST rules (since Energy Policy Act 2007):
        #   Spring forward: 2nd Sunday in March  at 2 AM PST  → UTC 10:00
        #   Fall back:      1st Sunday in November at 2 AM PDT → UTC 09:00
        try:
            utc_now = _dt.datetime.utcnow()
            year = utc_now.year

            def _nth_sunday(month: int, nth: int) -> _dt.datetime:
                d = _dt.datetime(year, month, 1)
                d += _dt.timedelta(days=(6 - d.weekday()) % 7)  # first Sunday
                return d + _dt.timedelta(weeks=nth - 1)

            dst_start = _nth_sunday(3, 2).replace(hour=10)   # 2AM PST = 10:00 UTC
            dst_end   = _nth_sunday(11, 1).replace(hour=9)   # 2AM PDT = 09:00 UTC
            offset    = -7 if dst_start <= utc_now < dst_end else -8
            la_now    = utc_now + _dt.timedelta(hours=offset)
            return la_now.weekday() in (1, 2) and 1 <= la_now.hour < 4
        except Exception:
            return False  # fail safe

    # ── Script GUI / Start (v3.0.0) ─────────────────────────────────────────
    # On Detuks, --scriptArgs only selects the profile: after loading, the
    # script opens its GUI and waits for Start. When the log shows the GUI
    # wait, the monitor (if enabled) clicks Start itself after a short grace
    # period (so a human click wins), verifies via "Start button pressed",
    # retries once, then alerts the Errors channel. With auto-click off it
    # alerts after _GUI_WAIT_ALERT_SEC instead.
    _AUTOSTART_GRACE_SEC    = 10
    _AUTOSTART_VERIFY_SEC   = 15
    _AUTOSTART_MAX_ATTEMPTS = 2
    _GUI_WAIT_ALERT_SEC     = 300
    _GUI_WAIT_STALE_SEC     = 6 * 3600
    # v3.1.1: a running script logs at least every ~50 s (19 h of real logs);
    # 10 min of silence means the client froze or crashed. Alert only.
    _SILENCE_ALERT_SEC      = 600
    # v3.1.1: the script starts within seconds of its plugin loading; on some
    # Windows setups the client's --script is never applied. Alert only.
    _SCRIPT_LOAD_TIMEOUT_SEC = 180
    # v3.1.1: rotated log parts read back at startup to find the run start.
    _CATCHUP_MAX_ROTATED     = 10

    def _cancel_script_load_check(self, state):
        t = getattr(state, '_plugin_load_timer', None)
        if t is not None:
            t.cancel()
            state._plugin_load_timer = None

    def _arm_script_load_check(self, folder, state):
        """Plugin loaded: if P2P Master AI hasn't started within the timeout,
        alert (Errors) and stop any queued relaunch from retrying."""
        self._cancel_script_load_check(state)

        def _fire():
            state._plugin_load_timer = None
            if not self._running:
                return
            # A new client is up without the script: any earlier run is over
            # (a relaunched client leaves no stop line) — Offline, and no
            # silence alert on top of this one.
            state.script_running = False
            state.awaiting_start = False
            mins = self._SCRIPT_LOAD_TIMEOUT_SEC // 60
            detail = (f'The client opened and loaded P2P Master AI, but the script did not '
                      f'start within {mins} minutes (a known Detuks issue on some Windows '
                      f'setups). Turn the script on in the client\'s plugin list. '
                      f'Not relaunching automatically.')
            self.log(f'❌ [{folder}] Client opened but P2P Master AI didn\'t start '
                     f'within {mins} min — turn it on in the client\'s plugin list.')
            if self.relaunch_mgr is not None:
                try:
                    self.relaunch_mgr.on_script_never_started(folder)
                except Exception as e:
                    self._dbg(f'[relaunch] on_script_never_started failed for {folder}: {e}')
            if self.cfg.get('monitor_errors', True):
                try:
                    self.handle_event({'type': 'error',
                                       'value': "⚠ Client opened but P2P Master AI didn't start",
                                       'activity': detail, 'ts': now_str(),
                                       '_detail': detail, '_task_ctx': ''},
                                      folder, source='live')
                except Exception as e:
                    self._dbg(f'[{folder}] script-load alert failed: {e}')

        t = threading.Timer(self._SCRIPT_LOAD_TIMEOUT_SEC, _fire)
        t.daemon = True
        state._plugin_load_timer = t
        t.start()

    def _check_silence(self):
        """Periodic (v3.1.1): alert once when a running account's script has
        written nothing for _SILENCE_ALERT_SEC — the client froze or crashed."""
        now = time.time()
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, st in snapshot:
            if (not st.script_running or st.on_break or st._break_start_ts
                    or st.awaiting_start or st.banned or st._silence_alerted
                    or st._plugin_load_timer is not None):
                continue
            last = st._last_script_line_ts
            if not last or now - last < self._SILENCE_ALERT_SEC:
                continue
            st._silence_alerted = True
            mins = int((now - last) // 60)
            try:
                from py.platform_ops import find_account_window_and_pid
                info = find_account_window_and_pid(name)
                win = ('the client window is still open — it may be frozen' if info
                       else 'no client window found — it may have crashed or been closed')
            except Exception:
                win = 'could not check the client window'
            detail = (f'P2P Master AI has written nothing to the log for {mins} minutes; '
                      f'{win}. Use /relaunch to restart it.')
            self.log(f'❌ [{name}] Script silent for {mins} min — {win}.')
            if self.cfg.get('monitor_errors', True):
                try:
                    self.handle_event({'type': 'error',
                                       'value': '⚠ Script silent — client frozen or crashed?',
                                       'activity': detail, 'ts': now_str(),
                                       '_detail': detail, '_task_ctx': ''},
                                      name, source='live')
                except Exception as e:
                    self._dbg(f'[{name}] silence alert failed: {e}')

    @staticmethod
    def _filter_start_events(events, genuine_starts, lines):
        """Keep only 'Script Started' events that really began a run (a
        Start pressed while already running — GUI reopened mid-run — is
        dropped), and synthesize one for a run that began via NEW TASK
        without a Start line (auto-start)."""
        out = [e for e in events
               if not (e.get('type') == 'script_event' and e.get('value') == 'start'
                       and e.get('_line_idx') not in genuine_starts)]
        have = {e.get('_line_idx') for e in out
                if e.get('type') == 'script_event' and e.get('value') == 'start'}
        for idx in sorted(set(genuine_starts) - have):
            m = LOG_TS_RE.match(lines[idx]) if 0 <= idx < len(lines) else None
            out.append({'type': 'script_event', 'value': 'start', 'activity': '▶️ Script Started',
                        'ts': m.group(1) if m else now_str(), '_line_idx': idx})
        out.sort(key=lambda e: (e.get('ts', ''), e.get('_line_idx', 0)))
        return out

    def _cancel_autostart(self, state):
        t = getattr(state, '_autostart_timer', None)
        if t is not None:
            t.cancel()
            state._autostart_timer = None

    def _schedule_autostart(self, folder, state, delay=None):
        self._cancel_autostart(state)
        if self.cfg.get('auto_click_start', True):
            d  = self._AUTOSTART_GRACE_SEC if delay is None else delay
            fn = lambda: self._autostart_step(folder, state)
        else:
            d  = self._GUI_WAIT_ALERT_SEC
            fn = lambda: self._gui_wait_alert(folder, state, 'auto-click Start is off')
        t = threading.Timer(d, fn)
        t.daemon = True
        state._autostart_timer = t
        t.start()

    def _autostart_step(self, folder, state):
        state._autostart_timer = None
        if not self._running or not state.awaiting_start:
            return
        if state._autostart_attempts >= self._AUTOSTART_MAX_ATTEMPTS:
            why = getattr(state, '_autostart_reason', '')
            if why == 'no BotClient window found':
                # The client is gone (closed at the GUI) — nothing to click.
                state.awaiting_start = False
                self.log(f"⚪ [{folder}] Script GUI was waiting but no client window is open — "
                         f"assuming the client was closed")
                return
            self._gui_wait_alert(folder, state, why
                                 or 'Start was clicked but the script did not start')
            return
        state._autostart_attempts += 1
        try:
            from py.paint import click_script_start
            ok, why = click_script_start(folder, log=self._dbg, window_lock=self._window_lock)
        except Exception as e:
            ok, why = False, f'auto-click error: {e}'
        state._autostart_reason = '' if ok else why
        if ok:
            self.log(f"🖱️ [{folder}] Clicked Start on the P2P Master AI GUI "
                     f"(attempt {state._autostart_attempts}/{self._AUTOSTART_MAX_ATTEMPTS})")
        else:
            self.log(f"⚠ [{folder}] Auto-click Start failed: {why}")
        # Next step verifies (start line cancels this timer) / retries / alerts.
        self._schedule_autostart(folder, state, delay=self._AUTOSTART_VERIFY_SEC)

    def _gui_wait_alert(self, folder, state, reason):
        state._autostart_timer = None
        if state._gui_alerted or not state.awaiting_start or not self._running:
            return
        state._gui_alerted = True
        detail = f'P2P Master AI is waiting at its GUI — click Start. ({reason})'
        self.log(f"❌ [{folder}] Waiting for Start — needs a click: {reason}")
        if self.cfg.get('monitor_errors', True):
            try:
                self.handle_event({'type': 'error', 'value': '⏸ Waiting for Start — needs a click',
                                   'activity': detail, 'ts': now_str(),
                                   '_detail': detail, '_task_ctx': ''},
                                  folder, source='live')
            except Exception as e:
                self._dbg(f'[{folder}] waiting-for-start alert failed: {e}')

    # Severe-lag alert window. LAG DETECTED is the script's own tick-gap
    # measurement: the median is ~1s; spikes right after a client start /
    # login / break are normal loading (40s+), and gaps of minutes/hours are
    # time spent logged out — not lag. Only a 10s–5min freeze while logged
    # in, running, and past a 3-minute warm-up is reported, at most once per
    # 30 minutes.
    _LAG_ALERT_MIN_MS = 10_000
    _LAG_ALERT_MAX_MS = 300_000
    _LAG_DEDUPE_SEC   = 1800

    def _process_info_lines(self, lines, folder, state):
        """Severe lag alerts, count milestones and the GE coins line."""
        from py.reader import strip_color
        for line in lines:
            b = strip_prefix(line).strip()

            m = LAG_RE.match(b)
            if m:
                ms = int(m.group(1))
                if (self._LAG_ALERT_MIN_MS <= ms <= self._LAG_ALERT_MAX_MS
                        and state.script_running and state.logged_in
                        and not state.on_break
                        and time.time() >= state._lag_quiet_until
                        and self.cfg.get('monitor_errors', True)
                        and state.should_alert('severe_lag', 1, 0, self._LAG_DEDUPE_SEC)):
                    secs   = ms / 1000.0
                    detail = f'Client froze for {secs:.0f}s (LAG DETECTED {ms}ms)'
                    last_t = state.last_task or ''
                    last_a = state.last_activity or ''
                    if last_t.lower() == 'break':
                        last_t = last_a = ''
                    task_ctx = f"{last_t} — {last_a}" if last_a else last_t
                    tsm = LOG_TS_RE.match(line)
                    self.log(f"❌ [{folder}] Severe lag spike: {secs:.0f}s")
                    self.handle_event({'type': 'error', 'value': 'Severe lag spike',
                                       'activity': detail, 'ts': tsm.group(1) if tsm else now_str(),
                                       '_detail': detail, '_task_ctx': task_ctx},
                                      folder, source='live')
                continue

            m = COUNT_RE.match(strip_color(b))
            if m:
                name = m.group(1).strip()
                try:
                    n = int(m.group(2).replace(',', ''))
                except ValueError:
                    continue
                key = name.lower()
                if ('tzkal' in key or not n or n % COUNT_MILESTONE_EVERY
                        or state.count_milestones.get(key) == n):
                    continue
                state.count_milestones[key] = n
                if not self.cfg.get('monitor_levelups', True):
                    continue
                self.log(f"🎉 [{folder}] 🏅 {name[:1].upper() + name[1:]} count: {n:,}")
                try:
                    from py.discord import count_milestone_payload, apply_ping
                    url = self._router.resolve_url(folder, 'levelup')
                    if url:
                        mention = self._router.mention()
                        payload = apply_ping(count_milestone_payload(mention, folder, name, n),
                                             mention, self.cfg.get('ping_levelup', False))
                        self._router.post_event(folder, 'levelup', payload, url=url)
                except Exception as e:
                    self._dbg(f'[{folder}] count milestone post failed: {e}')
                continue

            m = COINS_RE.match(b)
            if m:
                try:
                    gp = int(m.group(1).replace(',', ''))
                except ValueError:
                    continue
                if gp != state.last_gp:
                    state.last_gp = gp
                    self.log(f"🪙 [{folder}] Coins available (GE check): {gp:,}")

    def _maybe_schedule_auto_restart(self, folder: str, lines: list,
                                     state: 'AccountState') -> tuple:
        """
        Called from _process_lines after a 'script_event / stop' fires for 'folder'.
        Returns (status, message):
          status  — short status string used to annotate the Script Stopped Discord embed:
                      'disabled'                                       — feature off (silent in monitor)
                      'skipped — <reason>'                             — gate blocked it
                      'scheduled in Nm' / 'scheduled at H:MM AM/PM'   — timer armed
          message — Monitor tab line to log for this status, or None for 'disabled'
                      (silent). The caller logs this *after* the Script Stopped line
                      so visible ordering is correct; scheduling itself still happens
                      here, synchronously, regardless of when the caller logs it.

        Gate checks (in order):
          1. auto_restart_enabled in cfg
          2. suppress window — monitor-initiated relaunch in progress
          3. who stopped it — only a stop the script made itself (v3.1.1)
          4. game update window gate (if auto_restart_game_update_window_only)
          5. preset still exists for this account
        """
        account = folder  # folder == account name throughout watcher

        # ── Gate 1: feature enabled ────────────────────────────────────────────
        if not self.cfg.get('auto_restart_enabled', False):
            return 'disabled', None  # silent in monitor tab; shown in Discord embed only

        # ── Gate 1b: account banned — never auto-restart a banned account ─────
        if state.banned:
            status = 'skipped — account is banned'
            return status, f'🔄 [{account}] Auto restart {status}.'

        # ── Gate 2: suppress window (monitor-initiated relaunch) ──────────────
        try:
            from py.launcher import is_relaunch_suppressed
            if is_relaunch_suppressed(account):
                status = 'skipped — monitor-initiated relaunch in progress'
                return status, f'🔄 [{account}] Auto restart {status}.'
        except Exception:
            pass

        # ── Gate 3: who stopped it (v3.1.1) ───────────────────────────────────
        # Only a stop the script made itself (login timeout, error…) is
        # restarted. A stop by hand is left alone, and a script update reload
        # needs nothing — the client loads the new version itself.
        kind = getattr(state, '_last_stop_kind', '')
        if kind == 'manual':
            status = 'skipped — stopped by hand'
            return status, f'🔄 [{account}] Auto restart {status}.'
        if kind == 'reload':
            status = 'skipped — script update (the client reloads it)'
            return status, f'🔄 [{account}] Auto restart {status}.'

        # ── Gate 4: game update window ─────────────────────────────────────────
        if self.cfg.get('auto_restart_game_update_window_only', True):
            if not self._in_game_update_window():
                status = 'skipped — outside game update window (Tue/Wed 1–4 AM PT)'
                return status, f'🔄 [{account}] Auto restart {status}.'

        # ── Gate 5: preset exists ──────────────────────────────────────────────
        from py.launcher import find_preset
        if not find_preset(self.cfg, account):
            status = 'skipped — no launcher preset found'
            return status, f'🔄 [{account}] Auto restart {status}.'

        # ── Compute delay ──────────────────────────────────────────────────────
        delay_secs, delay_desc = self._compute_restart_delay(account, state)

        # ── Cancel any already-pending timer for this account ─────────────────
        old_timer = state._pending_restart_timer
        if old_timer is not None:
            old_timer.cancel()
            state._pending_restart_timer = None

        # ── Schedule ───────────────────────────────────────────────────────────
        status  = f'scheduled {delay_desc}'
        message = f'⏰ [{account}] Auto restart {status} after Script Stopped.'

        def _do_auto_restart():
            # Gate: watcher stopped during delay
            if not self._running:
                return
            # Gate: timer was cancelled or superseded (Script Started, newer stop, stop())
            # t is assigned in enclosing scope just below; safe to reference at call time.
            if state._pending_restart_timer is not t:
                return
            state._pending_restart_timer = None  # clear before running
            # Gate: preset removed during delay
            from py.launcher import find_preset as _fp, relaunch_account, is_relaunch_suppressed
            if not _fp(self.cfg, account):
                self.log(f'🔄 [{account}] Auto restart cancelled — preset removed during delay.')
                return
            # Gate: suppress window (e.g. /launch ran during delay)
            if is_relaunch_suppressed(account):
                self.log(f'🔄 [{account}] Auto restart cancelled — '
                         f'monitor-initiated relaunch started during delay.')
                return
            self.log(f'🔄 [{account}] Auto restart launching now...')
            # Normal path: route the actual attempt through the RelaunchManager
            # so auto-restarts get Script Started confirmation, retry/backoff,
            # persisted pending state, geometry restore, and the one-active-
            # launch-at-a-time worker. All auto-restart gates above are
            # unchanged — the manager only handles the launch itself.
            mgr = getattr(self, 'relaunch_mgr', None)
            if mgr is not None:
                try:
                    mgr.request_auto_restart_launch(account)
                    return
                except Exception as _mgr_exc:
                    self.log(f'⚠ [{account}] RelaunchManager unavailable '
                             f'({_mgr_exc}) — falling back to direct relaunch.')
            try:
                result = relaunch_account(self.cfg, account, log_fn=self.log)
                if not result.ok:
                    self.log(f'❌ [{account}] Auto restart failed: {result.message}')
            except Exception as exc:
                self.log(f'❌ [{account}] Auto restart exception: {exc}')

        t = threading.Timer(delay_secs, _do_auto_restart)
        t.daemon = True
        t.start()
        state._pending_restart_timer = t
        return status, message

    def _compute_restart_delay(self, account: str,
                               state: 'AccountState') -> 'tuple[float, str]':
        """
        Return (delay_seconds, human_description) for the scheduled restart.

        Priority:
          1. Respect breaks (if enabled): use break end from the pre-stop snapshot
             captured before script-stop cleared break state.
          2. Random delay between auto_restart_min_minutes and auto_restart_max_minutes.

        min == max is valid and results in exactly that many minutes.
        """
        import random, datetime as _dt

        # ── Validated min/max from config ──────────────────────────────────────
        try:
            lo = max(0, int(self.cfg.get('auto_restart_min_minutes', 1)))
        except (ValueError, TypeError):
            lo = 1
        try:
            hi = int(self.cfg.get('auto_restart_max_minutes', 30))
        except (ValueError, TypeError):
            hi = 30
        if hi < lo:
            hi = lo   # clamp; randint(n, n) == n, which is correct

        # ── Break-end path — uses pre-stop snapshot ────────────────────────────
        if self.cfg.get('auto_restart_respect_breaks', True):
            try:
                snap = state._pre_stop_break_snap or {}
                if (snap.get('on_break')
                        and snap.get('break_start_ts') is not None
                        and snap.get('break_length_ms') is not None
                        and snap['break_length_ms'] > 0):
                    break_end_ts = snap['break_start_ts'] + (snap['break_length_ms'] / 1000.0)
                    delay = break_end_ts - time.time()
                    if delay > 0:
                        end_dt   = _dt.datetime.fromtimestamp(break_end_ts)
                        time_str = end_dt.strftime('%I:%M %p').lstrip('0') or '12:00 AM'
                        desc = f'at {time_str} (break end)'
                        return delay, desc
                    # Break already past end — fall through to random delay
                    self._dbg(f'[auto_restart] Break end already passed for {account} — '
                              f'using random delay instead.')
            except Exception as exc:
                self._dbg(f'[auto_restart] Break-end calc failed for {account}: {exc} — '
                          f'using random delay.')

        # ── Random delay ───────────────────────────────────────────────────────
        mins = random.randint(lo, hi)
        if mins == 0:
            return 10.0, 'in 10 seconds'   # safety floor: Windows needs time to fully close
        desc = f'in {mins}m'
        return mins * 60.0, desc

    # ── Process lines (live) ───────────────────────────────────────────────────
    def _process_lines(self, lines, folder):
        # TODO: _process_lines handles state mutation, suppression, event filtering,
        # and dispatch prep in one pass. Split when adding a new event family or
        # significant new stateful parsing rule.
        state  = self._get_account(folder)
        events = []

        # Snapshot recent lines from *before* this batch, for the Wine of
        # Zamorak death-suppression check's cross-batch backward lookback
        # (see py.reader.parse_lines's context_lines param). Must be taken
        # before the loop below appends this batch's own lines into
        # state._recent_lines, or this batch's lines would double up in
        # both `lines` and the "prior" context passed alongside it.
        _prior_context = list(state._recent_lines)

        # Update login/break state from this batch
        genuine_starts = set()   # line idx of starts that really began a run (v3.0.0)
        genuine_stops  = set()   # line idx of stops that really ended a run (v3.1.1)
        for idx, line in enumerate(lines):
            b = strip_prefix(line).strip()
            state._recent_lines.append(line)          # rolling buffer for manual-stop detection
            # Silence watchdog (v3.1.1): only the script's own lines count —
            # the client keeps logging while a hung script is silent.
            _thr, _lg = _thread_and_logger(line)
            if _lg and (not state._script_logger or _lg.startswith(state._script_logger)):
                state._last_script_line_ts = time.time()
                if state._silence_alerted:
                    state._silence_alerted = False
                    self.log(f'▶ [{folder}] Script is logging again.')
            if _is_break_start(line):
                state.on_break  = True
                state.logged_in = False
                if not state._break_start_ts:
                    state._break_start_ts = time.time()
                from py.util import parse_break_length_ms
                bl_ms = parse_break_length_ms(lines, idx + 1, max_search=3)
                if bl_ms is not None:
                    state._break_length_ms = bl_ms    # persist for break-end scheduling
                # A relaunch queued behind Respect Break executes at this
                # account's own break window — close now, relaunch at break end.
                if self.relaunch_mgr is not None:
                    try:
                        self.relaunch_mgr.on_break_started(folder)
                    except Exception as _rm_exc:
                        self._dbg(f'[relaunch] on_break_started failed for {folder}: {_rm_exc}')
            elif _is_break_over(b.lower()):
                # Real completed break — not 'Break over -> Startup'
                state.on_break = False
                state.logged_in = True
                state.script_running = True
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts  = None
                state._break_length_ms = None   # break consumed; clear for next cycle
                state._lag_quiet_until = time.time() + 180
            elif is_ban_line(b):
                if not state.banned:
                    state.banned = True
                    self.log(f'🔨 [{folder}] Ban detected — status set to Banned '
                             f'(clears on next successful login).')
                    try:
                        self.handle_event({'type': 'ban', 'value': 'Account banned',
                                           'activity': '', 'ts': now_str()},
                                          folder, source='live')
                    except Exception as _be:
                        self._dbg(f'[ban] handle_event failed for {folder}: {_be}')
                    try:
                        from py.discord import ban_payload
                        self._router.post_event(folder, 'default',
                                                ban_payload(self._router.mention(), folder))
                    except Exception as _be:
                        self.log(f'⚠ [{folder}] Ban ping failed: {_be}')
            elif _is_logout(b) and not state.on_break:
                state.logged_in = False
                # Do NOT set _break_start_ts here — logout is not a break
            elif _is_login(b):
                state.logged_in = True
                state.on_break  = False
                state.banned    = False  # successful login clears ban (2-day temps, v2.2.0)
                state._lag_quiet_until = time.time() + 180
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts = None
            elif _is_plugin_load(b):
                # Client loaded the P2P plugin — the script itself should
                # start within seconds (v3.1.1 check, see _arm_script_load_check).
                self._arm_script_load_check(folder, state)
            elif _is_script_loaded(b):
                # Profile loaded — the script's GUI opens and waits for Start.
                # Not a start (v3.0.0): status → Waiting for Start. A fresh
                # load always means any previous run is over (a client that
                # crashed leaves no "Finished onExit" line).
                self._cancel_script_load_check(state)
                if _lg:
                    state._script_logger = _logger_family(_lg)
                state.script_running = False
                state.awaiting_start = True
                state._gui_wait_since = None
                state._gui_alerted = False
                state._autostart_attempts = 0
                self._cancel_autostart(state)
            elif _is_gui_wait(b):
                if state.awaiting_start and state._gui_wait_since is None:
                    state._gui_wait_since = time.time()
                    self._schedule_autostart(folder, state)
            elif _is_script_start(b) or ('NEW TASK' in b.upper() and state.awaiting_start):
                if not state.script_running:
                    # A new script run begins (v3.0.0: uptime = current run —
                    # the shared client.log has no per-client session start).
                    genuine_starts.add(idx)
                    state.total_break_secs = 0.0
                    m = LOG_TS_RE.match(line)
                    try:
                        state.script_start_ts = (datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                                                 .timestamp() if m else time.time())
                    except Exception as e:
                        state.script_start_ts = time.time()
                        self._dbg(f'script_start_ts parse failed: {e}')
                    state._lag_quiet_until = time.time() + 180
                state.script_running = True
                state.awaiting_start = False
                state._gui_wait_since = None
                self._cancel_autostart(state)
                if 'NEW TASK' in b.upper() and not state.on_break:
                    state.logged_in = True
            elif 'solvers all finished' in b.lower():
                state.script_running = True
                state.logged_in      = True
                state.on_break       = False
            elif 'NEW TASK' in b.upper() and state.script_running and not state.on_break:
                # Script is processing tasks — definitively in game
                # Don't touch on_break — break state takes priority
                state.logged_in = True
            elif _is_script_stop(b):
                # v3.1.1: who stopped it decides auto restart (Gate 3), and
                # only the first stop of a run is reported (the script can
                # log "Finished onExit" several times in a row).
                state._last_stop_kind = _stop_kind(line, list(state._recent_lines)[:-1])
                if state.script_running or state.awaiting_start:
                    genuine_stops.add(idx)
                # Capture break context before clearing — used by _compute_restart_delay
                # if 'Respect breaks on relaunch' is enabled.
                state._pre_stop_break_snap = {
                    'on_break':       state.on_break,
                    'break_start_ts': state._break_start_ts,
                    'break_length_ms': state._break_length_ms,
                }
                state.script_running  = False
                state.awaiting_start  = False
                state._gui_wait_since = None
                self._cancel_autostart(state)
                state.logged_in       = False
                state.on_break        = False
                state._break_start_ts = None
                state.inferno.reset()

        # Parse all events through the unified reader pipeline.
        parsed = parse_lines(lines, context_lines=_prior_context)
        parsed = self._filter_start_events(parsed, genuine_starts, lines)
        parsed = [e for e in parsed
                  if not (e.get('type') == 'script_event' and e.get('value') == 'stop'
                          and e.get('_line_idx') not in genuine_stops)]

        # Snapshot the task/activity context as it stood *before* this batch —
        # used by reset attribution below so a reset line is matched against the
        # task active at that point in the log, not whatever this batch ends on.
        _pre_batch_task     = state.last_task
        _pre_batch_activity = state.last_activity

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
                extra = ''
                if ev.get('_location'):
                    extra += f" @ {ev['_location']}"
                if ev.get('_planned_min'):
                    extra += f" (~{ev['_planned_min']}m)"
                self.log(f"📋 [{folder}] {display}" + (f" / {activity}" if activity and value else "") + extra)

            elif etype == 'slayer_task':
                state.last_task     = 'Slayer'
                state.last_activity = f"{activity} {value}"
                self.log(f"🗡️ [{folder}] New Task: {activity} {value}")

            elif etype == 'slayer_complete':
                td, pe, tp = ev.get('_slayer_complete', (None, None, None))
                pts = f"+{pe:,} pts (total: {tp:,})" if pe else "no points yet"
                ev  = dict(ev, activity=pts)
                activity = pts
                self.log(f"✅ [{folder}] Task complete: {value} — {pts}")

            elif etype == 'slayer_skip':
                # Fallback to last known monster if cancel fired in a different poll
                if not value and state.last_activity:
                    parts = state.last_activity.split(' ', 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        value = parts[1]
                        ev    = dict(ev, value=value)
                self.log(f"⏭️ [{folder}] Skipped: {value} — {activity}")

            elif etype == 'quest_started':
                self.log(f"📜 [{folder}] Started: {value}")

            elif etype == 'quest':
                self.log(f"🏆 [{folder}] Completed: {value}")

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

                # Burst-group rapid lock failures; standalone errors dispatch immediately
                if self._maybe_burst_error(ev, folder, state):
                    continue  # buffered — will flush after debounce window

                self.log(f"❌ [{folder}] {enriched_value}: {activity or detail}")

            elif etype == 'death':
                self.log(f"💀 [{folder}] Character died!")
                last_t = state.last_task     or ''
                last_a = state.last_activity or ''
                if last_t.lower() in ('break', ''):
                    last_t = ''
                    last_a = ''
                task_ctx = f"{last_t} — {last_a}" if last_a else last_t
                ev = dict(ev, value='Oh dear, you are dead!', activity='', _task_ctx=task_ctx)

            elif etype == 'levelup':
                skill = value
                level = int(activity) if activity.isdigit() else 0
                is_99 = ev.get('_is_99', False)
                try:
                    notify_every = max(1, int(self.cfg.get('levelup_every', 5)))
                except (ValueError, TypeError):
                    notify_every = 5
                last_notified = state.notified_levels.get(skill, 0)
                if is_99:
                    should_notify = True  # Level 99 ALWAYS notifies
                else:
                    should_notify = (level // notify_every > last_notified // notify_every
                                     or last_notified == 0) if level else True
                state.notified_levels[skill] = level
                if not should_notify or self._is_muted(folder):
                    continue
                # "Skip level notifications below N" — Discord-only suppression.
                # The level-up is still logged to the Event Log and recorded in
                # History below; only the Discord relay is skipped. Level 99 and
                # Total Level milestones are never suppressed. An unparsed level
                # (level == 0) preserves existing behavior rather than
                # incorrectly suppressing.
                try:
                    skip_below = max(1, int(self.cfg.get('levelup_skip_below', 1)))
                except (ValueError, TypeError):
                    skip_below = 1
                if (skip_below > 1 and level and level < skip_below
                        and not is_99 and skill != 'Total Level'):
                    ev = dict(ev, _suppress_discord=True)
                prefix = "🎆" if is_99 else "🎉"
                self.log(f"{prefix} [{folder}] {skill} → {level}")

            elif etype == 'script_event':
                # ── Auto-restart logic: runs regardless of notification/mute settings ──
                ar_status  = None
                ar_message = None
                if value == 'stop':
                    try:
                        ar_status, ar_message = self._maybe_schedule_auto_restart(folder, lines, state)
                    except Exception as _ar_exc:
                        self._dbg(f'[auto_restart] scheduling error for {folder}: {_ar_exc}')
                elif value == 'start':
                    # Confirm any in-flight queued relaunch — Script Started is
                    # the manager's success condition (never process spawn).
                    if self.relaunch_mgr is not None:
                        try:
                            self.relaunch_mgr.on_script_started(folder)
                        except Exception as _rm_exc:
                            self._dbg(f'[relaunch] on_script_started failed for {folder}: {_rm_exc}')
                    # Cancel pending restart if script started before timer fired
                    if state._pending_restart_timer is not None:
                        state._pending_restart_timer.cancel()
                        state._pending_restart_timer = None
                        self.log(f'🔄 [{folder}] Auto restart cancelled — '
                                 f'Script Started detected before scheduled restart.')
                        ev = dict(ev, _ar_cancelled=True)

                # Annotate ev for Discord embed BEFORE continue guards
                if ar_status is not None:
                    ev = dict(ev, _ar_status=ar_status)

                cfg_key_map = {
                    'start':  'monitor_script_start',  'stop':   'monitor_script_stop',
                    'pause':  'monitor_script_pause',   'resume': 'monitor_script_resume',
                }
                if not self.cfg.get(cfg_key_map.get(value, ''), True):
                    # Script Stopped line itself is suppressed by config, but the
                    # auto-restart message (if any) still logs — same as before.
                    if ar_message:
                        self.log(ar_message)
                    continue
                self.log(f"🖥️ [{folder}] {activity}")
                # Log Script Stopped before the auto-restart message — visible
                # ordering should match the order these things actually happened.
                if ar_message:
                    self.log(ar_message)
                if self._is_muted(folder):
                    continue

            # ── Dispatch — caller has already decided this fires ──────
            self.handle_event(ev, folder, source='live')
            events.append(ev)

        # Resolve the task/activity context "as of" a given line position in this
        # batch, using the last task/slayer_task parsed event at or before that
        # line — falls back to the pre-batch context if none exists in-batch.
        # Used by reset attribution below so a reset line is matched against the
        # task active when it occurred, not whatever task this batch ends on.
        def _task_ctx_as_of(line_pos):
            best = None
            for pe in parsed:
                idx = pe.get('_line_idx')
                if idx is None or idx > line_pos:
                    continue
                if pe['type'] == 'task':
                    best = (pe.get('value', ''), pe.get('activity', ''))
                elif pe['type'] == 'slayer_task':
                    best = ('Slayer', f"{pe.get('activity', '')} {pe.get('value', '')}".strip())
            if best is not None:
                return best
            return (_pre_batch_task, _pre_batch_activity)

        # Last-resort fallback for reset attribution ONLY: scan the raw lines
        # of this same batch, before the reset line, for the nearest
        # "Task is X" (and an optional following "Activity is Y" before the
        # reset line). This covers the case where _task_ctx_as_of() above
        # finds nothing — e.g. the very first batch ever processed for an
        # account (no pre-batch state) where slice_tasks() has already
        # suppressed the "Task is X" announcement itself because a
        # "> Locking" line follows it within the same batch. Purely a label
        # lookup for the reset error message — it does not parse or emit any
        # task event, and does not touch state.last_task / state.last_activity
        # or any other current/global task state. Lines at or after the reset
        # line (later "NEW TASK" / "Task is Slayer" / "Slayer -> ...") are
        # never considered.
        def _task_ctx_from_raw_lines(line_pos):
            task_name = ''
            task_idx  = None
            for i in range(line_pos - 1, -1, -1):
                b = strip_prefix(lines[i]).strip()
                if re.match(r'^Task is(?:\s+NOT)?\s+doable', b, re.IGNORECASE):
                    continue
                m = re.match(r'^Task is\s+(.+)$', b, re.IGNORECASE)
                if m:
                    task_name = m.group(1).strip()
                    task_idx  = i
                    break
            if task_idx is None:
                return '', ''
            activity = ''
            for j in range(task_idx + 1, line_pos):
                b = strip_prefix(lines[j]).strip()
                m = re.match(r'^Activity is\s+(.+)$', b, re.IGNORECASE)
                if m:
                    activity = m.group(1).strip()
                    break
            return task_name, activity

        # Standalone activity updates (no Discord, status only)
        for _line_pos, line in enumerate(lines):
            b = strip_prefix(line).strip()
            if 'need a new slayer task' in b.lower() or 'getting new task' in b.lower():
                # Only set Fetching task... if no slayer_task event already fired
                # in this same chunk — if it did, the task is already known
                if not any(e['type'] == 'slayer_task' for e in events):
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
                last_t, last_a = _task_ctx_as_of(_line_pos)
                last_t = last_t or ''
                last_a = last_a or ''
                if last_t.lower() in ('break', ''):
                    last_t = ''
                    last_a = ''
                if not last_t:
                    # Nothing usable from parsed events or pre-batch state —
                    # fall back to scanning this batch's raw lines directly.
                    last_t, last_a = _task_ctx_from_raw_lines(_line_pos)
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

        # ── Detuks informational lines (v3.0.0, live only — never backfilled) ──
        self._process_info_lines(lines, folder, state)

        # ── Inferno tracker ────────────────────────────────────────────────────
        # Feed every live line batch through the stateful Inferno tracker.
        # ui_updates  → update last_task/last_activity for status/monitor tab display
        # disc_events → emit as 'inferno' type events (routed to Tasks channel)
        inferno_ui, inferno_disc = state.inferno.feed(lines)

        for (task, activity) in inferno_ui:
            state.last_task     = task
            state.last_activity = activity

        for ev in inferno_disc:
            ts_ev    = ev.get('ts', now_str())
            activity = ev.get('activity', '')   # sub-type: gear_check, attempt_start, wave, death, success
            msg      = ev.get('value', '')

            # Log to monitor tab
            icon = '🌋'
            self.log(f"{icon} [{folder}] {msg}")

            # Fire UI callback so the Monitor tab TASKS counter increments
            # (same as handle_event does for etype=='task')
            try:
                self.on_event('task', folder, 'Inferno', msg)
            except Exception as e:
                self.log(f"⚠ [{folder}] inferno on_event failed: {e}")

            # Route to Tasks channel via post_task
            if not self._is_muted(folder):
                self._router.post_task(folder, 'Inferno', msg)

            # Persist to history as a task event so it appears in History tab
            try:
                from py.history import append_history
                append_history(folder, 'task', 'Inferno', msg, timestamp=ts_ev,
                               log_fn=self.log, debug=self.cfg.get('debug', False))
            except Exception as e:
                self.log(f"⚠ [{folder}] inferno history write failed: {e}")

            events.append(ev)

        # Push status tab update if any events fired or state changed
        if events or inferno_ui:
            self.on_status()
        return events

    # ── Bot wiring ─────────────────────────────────────────────────────────────
    def _verify_discord_channels_startup(self):
        """
        Proactive startup check (runs once per LogWatcher.start() call, on
        its own background thread — see the call site in start()). Confirms
        every configured Discord channel still exists via one cheap GET per
        channel; if any are missing, reuses the existing, already-idempotent
        _run_bot_setup() to repair them (recreates only what's actually
        missing — never duplicates a channel that's still valid).

        This complements, rather than replaces, the existing reactive 404
        recovery in DiscordRouter._handle_post_error (which fires from an
        actual failed post). That reactive path already correctly recovers
        and retries — this just covers the case where a channel was deleted
        while the app was otherwise idle, so it's already fixed by the time
        anything needs to post to it, instead of only fixing itself on the
        next post attempt.

        Never touches the Tk main thread, never blocks watcher startup —
        start() only spawns this thread and returns immediately.
        """
        token     = self.cfg.get('bot_token', '').strip()
        server_id = self.cfg.get('bot_server_id', '').strip()
        channel_ids = self.cfg.get('bot_channel_ids', {})
        if not token or not server_id or not channel_ids:
            return  # nothing configured yet, or bot setup was never completed

        try:
            missing = verify_bot_channels(token, channel_ids, log_fn=self.log)
        except Exception as e:
            self.log(f"🔧 Startup Discord channel check failed: {e}")
            return

        if not missing:
            return  # everything still exists — no API calls beyond the cheap check above

        self.log(f"🔧 {len(missing)} configured Discord channel(s) missing — "
                 f"running bot setup to repair...")
        ok, msg = self._run_bot_setup(log_fn=self.log)
        if not ok:
            self.log(f"🔧 Discord channel repair failed: {msg} — "
                     f"fix manually via Settings > Discord > Run Bot Setup")

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
            # Bot setup recreates channels/webhooks — clear per-session thread state
            # so _ensure_threads_for_account runs fresh for every account and
            # doesn't skip due to a stale _threads_verified entry.
            self._threads_verified.clear()
            self._threads_recovery_attempted.clear()
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
        if account in self._threads_verified:
            return  # already verified this session

        # Prevent concurrent ensure passes for the same account
        with self._threads_ensure_lock:
            if account in self._threads_ensuring:
                self._dbg(f'[{account}] _ensure_threads already in progress — skipping concurrent call')
                return
            self._threads_ensuring.add(account)

        try:
            channel_ids  = self.cfg.get('bot_channel_ids', {})
            if not channel_ids:
                return
            thread_ids   = self.cfg.get('bot_thread_ids', {})
            acct_threads = thread_ids.get(account, {})
            newly_created = set()   # ch_names created in this pass — get deferred PUT
            stale_channels = set()  # ch_names whose saved thread ID returned 10003

            # ── Phase 1: membership-check pre-existing saved thread IDs ──────
            # Collect stale channels; do not spawn recovery threads from here.
            for ch_name, tid in list(acct_threads.items()):
                if not self._running:
                    return
                result = self._bot_add_user_to_thread(account, ch_name, tid, token)
                if result == 'deleted':
                    stale_channels.add(ch_name)

            # ── Phase 2: remove stale IDs found in phase 1 ───────────────────
            if stale_channels:
                for ch_name in stale_channels:
                    guard_key = (account, ch_name)
                    if guard_key in self._threads_recovery_attempted:
                        self.log(
                            f"🔧 [{account}] Recovery already attempted for #{ch_name} this session "
                            f"— skipping to avoid duplicate threads / rate limit loop"
                        )
                        continue
                    self._threads_recovery_attempted.add(guard_key)
                    tid = acct_threads.pop(ch_name, None)
                    if tid:
                        self.log(f"🔧 [{account}] Stale thread {tid} (#{ch_name}) removed — recreating")

            # ── Phase 3: create any missing or just-removed threads ───────────
            for ch_name, ch_id in channel_ids.items():
                if not self._running:
                    return
                if ch_name in acct_threads:
                    continue  # already have a valid thread ID
                tid = bot_ensure_thread(token, ch_id, account, log_fn=self.log)
                if tid:
                    acct_threads[ch_name] = tid
                    newly_created.add(ch_name)
                else:
                    self.log(f"🤖 [{account}] Could not create thread for #{ch_name} — will retry next session")

            # ── Phase 4: save config if anything changed ──────────────────────
            if stale_channels or newly_created:
                thread_ids[account] = acct_threads
                self.cfg['bot_thread_ids'] = thread_ids
                self._save_cfg()
                self.log(f"🤖 Threads ready for account: {account}")

            # ── Phase 5: deferred PUT for newly created threads ───────────────
            # Direct PUT only — no recovery path — a 404 here means Discord
            # hasn't propagated the thread yet, not that it was deleted.
            # If PUT returns 429, parse retry_after, sleep, retry once.
            # Small delay between each PUT to avoid rate limiting.
            if newly_created and self._running:
                newly_snapshot = {ch: acct_threads[ch] for ch in newly_created if ch in acct_threads}
                user_id = self.cfg.get('mention_id', '').strip()
                def _deferred_add(acc=account, snap=newly_snapshot, tok=token, uid=user_id):
                    import time as _time, re as _re
                    _time.sleep(5)
                    if not self._running or not uid:
                        return
                    for ch, tid in snap.items():
                        if not self._running:
                            return
                        _, err = bot_api(tok, 'PUT',
                                         f'/channels/{tid}/thread-members/{uid}')
                        if err and '429' in err:
                            m = _re.search(r'"retry_after"\s*:\s*([\d.]+)', err)
                            wait = float(m.group(1)) + 1.0 if m else 5.0
                            self.log(f"🤖 [{acc}] Rate limited adding user to #{ch} — retrying after {wait:.1f}s")
                            deadline = _time.time() + wait
                            while self._running and _time.time() < deadline:
                                _time.sleep(0.1)
                            if not self._running:
                                return

                            _, err = bot_api(tok, 'PUT',
                                             f'/channels/{tid}/thread-members/{uid}')
                        if err:
                            self.log(f"🤖 [{acc}] Could not add user to new thread #{ch}: {err}")
                        else:
                            self.log(f"🤖 [{acc}] Added user to new thread #{ch}")
                        _time.sleep(0.75)  # small buffer between PUTs to avoid rate limiting
                threading.Thread(target=_deferred_add, daemon=True).start()

            # ── Phase 6: mark verified only if all channels are covered ───────
            all_present = all(ch in acct_threads for ch in channel_ids)
            if all_present:
                self._threads_verified.add(account)
            else:
                self._dbg(f'[{account}] Not all threads present — will retry on next startup')

        finally:
            with self._threads_ensure_lock:
                self._threads_ensuring.discard(account)

    def _bot_add_user_to_thread(self, account, ch_name, thread_id, token):
        """Best-effort: add configured mention user to a Discord thread.
        Checks membership first to avoid redundant PUTs.
        Handles 429 rate limits with a single retry after retry_after delay.

        Returns a status string:
          'ok'           — user was already a member or was added successfully
          'rate_limited' — Discord rate limited the request; skip for now
          'deleted'      — Discord returned 10003 (thread/channel gone)
          'error'        — any other failure

        GET /channels/{thread_id}/thread-members/{user_id} returns plain HTTP 404
        when the user is simply not a member — that is normal and proceeds to PUT.
        Only Discord error code 10003 means the thread itself is gone.

        Recovery is NOT done here. Callers collect 'deleted' results and handle
        stale-thread removal and recreation inline in _ensure_threads_for_account.
        """
        import re as _re, time as _time
        user_id = self.cfg.get('mention_id', '').strip()
        if not user_id or not thread_id:
            return 'error'

        def _is_deleted_thread(e):
            """True only when the thread/channel itself is gone (Discord code 10003).
            Plain HTTP 404 from the membership GET just means user is not a member."""
            if not e:
                return False
            if '429' in e:
                return False
            return '10003' in e

        def _is_429(e):
            return bool(e and '429' in e)

        # Check if user is already a member.
        # Plain HTTP 404 = user not in thread = normal, proceed to PUT.
        # 10003 = thread itself is deleted = return 'deleted' to caller.
        data, err = bot_api(token, 'GET',
                            f'/channels/{thread_id}/thread-members/{user_id}')
        if data is not None:
            return 'ok'  # already a member

        if _is_429(err):
            self._debug_entry('discord_thread', {'thread_id': thread_id, 'reason': 'rate_limited'},
                mirror_msg=f"🤖 Rate limited checking membership for thread {thread_id} — skipping this pass")
            return 'rate_limited'

        if _is_deleted_thread(err):
            return 'deleted'

        # Plain HTTP 404 or absent data — user is not a member, attempt to add
        def _put():
            return bot_api(token, 'PUT',
                           f'/channels/{thread_id}/thread-members/{user_id}')

        _, err = _put()
        if _is_429(err):
            m = _re.search(r'"retry_after"\s*:\s*([\d.]+)', err)
            wait = float(m.group(1)) if m else 5.0
            self._dbg(f'Rate limited adding user to thread {thread_id} — retrying after {wait:.1f}s')
            _time.sleep(wait)
            _, err = _put()
        if err:
            if _is_deleted_thread(err):
                return 'deleted'
            elif _is_429(err):
                self.log(f"🤖 Rate limited adding user to thread {thread_id} — will retry next session")
                return 'rate_limited'
            else:
                self.log(f"🤖 Could not add user to thread {thread_id}: {err}")
                return 'error'
        else:
            self.log(f"🤖 Added user to thread {thread_id}")
            return 'ok'

    # ── Bot screenshot helpers (called by GatewayRunner via callbacks) ──────────
    def _bot_screenshot_to_channel(self, account, channel_id, token):
        # Capture the currently focused window NOW (before the client steals focus)
        # so the worker can restore it after the screenshot completes.
        restore_wid = get_focused_wid()
        self._enqueue_screenshot(SS_PRIORITY_ONDEMAND, account, 'bot-ss',
                                 bot_channel_id=channel_id, bot_token=token,
                                 restore_wid=restore_wid)


    def _bot_force_panel(self, account, action, channel_id, token):
        """
        Open a Stats/Logs panel, take a full-window screenshot using the
        already-focused window (inside the lock), post to the account's monitor
        thread, then close the panel.
        Runs in a daemon thread (called by GatewayRunner).
        """
        from datetime import datetime
        from pathlib  import Path
        from py.screenshot import SCREENSHOT_DIR
        from py.discord    import post_bot_image

        captured = {}

        def _do_capture():
            try:
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                # Find window — same technique as paint.py
                wids = find_window_ids_by_name(account)
                if not wids:
                    self.log(f"  ⚠ [{account}] {action} panel: no window found for capture")
                    return
                wid      = wids[0]
                ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe     = re.sub(r'[^a-zA-Z0-9_-]', '_', account)
                out_path = str(SCREENSHOT_DIR / f"{safe}_{action}_{ts}.png")
                ok_cap, err_cap = capture_window_image(wid, out_path)
                if ok_cap and Path(out_path).exists() and Path(out_path).stat().st_size > 0:
                    captured['path'] = out_path
                else:
                    self.log(f"  ⚠ [{account}] {action} panel capture failed: {err_cap}")
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
            # Only add user to thread membership when we have a confirmed thread ID.
            # channel_id may be a plain channel fallback if no thread ID was saved;
            # _bot_add_user_to_thread must not receive a channel ID.
            tid = self.cfg.get('bot_thread_ids', {}).get(account, {}).get('monitor')
            if tid:
                self._bot_add_user_to_thread(account, 'monitor', tid, token)


