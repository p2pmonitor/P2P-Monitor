"""
watcher.py — Log watching engine for P2P Monitor v1.7.0
LogWatcher: discovers accounts, polls log files, drives backfill and live events.
Backfill and live monitor both call reader.parse_lines() — no more triple pipeline.
"""
import json
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
                        get_last_seen, set_last_seen,
                        load_history_for, load_offsets, save_offsets)
from py.paint        import do_force, do_force_skill, do_force_panel, PANEL_ACTIONS, AMOUNT_ACTIONS
from py.platform_ops import (get_open_log_handles, find_window_ids_by_name,
                             normalize_path, capture_window_image)

try:
    import psutil as _psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _psutil = None
    _PSUTIL_AVAILABLE = False
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
from py.inferno import InfernoTracker

LOG_PATTERN = "logfile-*.log"

_IS_BREAK_START_RE = re.compile(r'BREAK START', re.IGNORECASE)
_IS_BREAK_OVER_RE  = re.compile(r'break over\s*(-?\d+)', re.IGNORECASE)

def _is_break_start(line):
    """True if this log line signals the start of a break."""
    return bool(_IS_BREAK_START_RE.search(line))

def _is_break_over(stripped_lower):
    """True if stripped lowercase line is a real completed break — not 'Break over -> Startup'."""
    return bool(_IS_BREAK_OVER_RE.match(stripped_lower))


def _get_log_files(folder):
    p = Path(folder)
    files = list(p.glob("logfile-*.log")) + list(p.glob("logfile-*.log.*"))
    return sorted(files, key=lambda f: f.name)



def _get_active_log_file(folder):
    """
    Return the active logfile-*.log Path in folder — the one actually being
    written to by a running DreamBot process.

    Strategy:
      1. Uses get_open_log_handles() from platform_ops — Linux: readlink /proc/*/fd/*
      2. If exactly one .log (no suffix) has an open handle — return it.
      3. If multiple have open handles (duplicate client) — return the one
         with the most recent mtime.
      4. If none have open handles or scan is unreliable (Windows) — fall back
         to most recently modified .log file. This correctly identifies the file
         DreamBot is actively writing to regardless of filename order, and
         naturally ignores rotated .log.1 files which won't be touched after rotation.
    """
    log_files = [f for f in _get_log_files(folder)
                 if re.match(r'logfile-\d+\.log$', f.name)]
    if not log_files:
        return None
    try:
        scan = get_open_log_handles()
        if not scan.reliable:
            # Cannot trust handle scan — fall through to name-based fallback
            raise RuntimeError(f'handle scan unreliable: {scan.reason}')
        active_candidates = [f for f in log_files if normalize_path(f) in scan.paths]
        if len(active_candidates) == 1:
            return active_candidates[0]
        elif len(active_candidates) > 1:
            # Duplicate client — pick most recently modified
            return max(active_candidates, key=lambda f: f.stat().st_mtime)
        # No open handles — fall through to name-based fallback
    except Exception:
        pass
    # Fallback: most recently modified .log file — the one DreamBot is
    # actively writing to. More reliable than newest-by-filename on Windows
    # where handle scanning is disabled, and correctly ignores rotated
    # .log.1 files which won't have been touched since rotation.
    try:
        return max(log_files, key=lambda f: f.stat().st_mtime)
    except Exception:
        return log_files[-1]

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
        self.err_history     = {}
        self.err_alerted     = {}
        self.last_screenshot_ts = 0
        self.on_break        = False
        self.session_start   = time.time()
        self.script_start_ts = None
        self.logged_in       = False
        self.total_break_secs    = 0
        self._break_start_ts     = None
        self._break_length_ms    = None   # parsed from "Break length N" log line; ms int or None
        self.script_running      = False  # True once script start confirmed; False on stop
        self.notified_levels     = {}
        self.session_file_set    = set()  # tracks known session files; triggers uptime recalc when changed
        self._startup_done       = False  # guards _startup_catchup to run only once per state
        self.inferno             = InfernoTracker()  # stateful Inferno gear-check and attempt tracker
        # Auto-restart state
        self._recent_lines       = deque(maxlen=30)  # rolling buffer; manual-stop signature check
        self._pending_restart_timer: 'threading.Timer | None' = None
        self._pre_stop_break_snap = None  # break snapshot captured before script-stop clears it

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
        self._last_update_check_date = None    # dedupe: date string of last update-awareness check

    def _dbg(self, msg):
        """Log msg only when debug mode is enabled in config."""
        if self.cfg and self.cfg.get('debug', False):
            self.log(f'[DEBUG] {msg}')

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
                'on_launch':      self._on_launch_cb,
                'on_launch_all':  self._on_launch_all_cb,
                'on_relaunch':     self._on_relaunch_cb,
                'on_relaunch_all': self._on_relaunch_all_cb,
                'is_running':    lambda: self._running,
                'get_cfg':       lambda: self.cfg,
            })
            runner.bot_ready = self._bot_ready  # share the same event
            self._bot_thread = threading.Thread(target=runner.run, daemon=True)
            self._bot_thread.start()

    def stop(self):
        self._running = False
        # Cancel any pending auto-restart timers before tearing down
        with self._accounts_lock:
            for state in self._accounts.values():
                t = getattr(state, '_pending_restart_timer', None)
                if t is not None:
                    t.cancel()
                    state._pending_restart_timer = None
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
            # Before saving offsets, EOF-pin any active log files whose DreamBot
            # client has already closed. This prevents backfill from re-reading
            # those lines on next startup and duplicating history entries.
            scan = get_open_log_handles()
            # EOF-pin only when scan is reliable — skip if we can't trust
            # the result, to avoid pinning files that are actually still open
            if scan.reliable:
                for d in self._get_log_dirs():
                    log_files = _get_log_files(d)
                    active = next((f for f in reversed(log_files)
                                   if re.match(r'logfile-\d+\.log$', f.name)), None)
                    if not active:
                        continue
                    fstr = str(active)
                    if normalize_path(fstr) not in scan.paths:
                        # DreamBot already closed — pin to EOF
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
        # Check for new rotated log files per account and recalculate uptime/breaks if changed
        try:
            for d in self._get_log_dirs():
                log_files   = _get_log_files(d)
                # Use name-based selection here — fast, no proc scan.
                # _get_active_log_file is only needed at startup and on explicit refresh.
                active = next((f for f in reversed(log_files)
                               if re.match(r'logfile-\d+\.log$', f.name)), None)
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
                    # Log rotation detected — re-run catchup to pick up new active file.
                    # Run in a daemon thread so get_account_rows isn't blocked by a
                    # potentially slow full-file scan on large logs.
                    state.session_file_set = session_files
                    active_str = str(active)
                    threading.Thread(
                        target=self._startup_catchup,
                        args=(active_str,),
                        kwargs={'is_rotation': True},
                        daemon=True,
                    ).start()
        except Exception as e:
            self._dbg(f'get_account_rows rotation check failed: {e}')

        rows = []
        with self._accounts_lock:
            snapshot = list(self._accounts.items())
        for name, s in sorted(snapshot):
            if not s.script_running:
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
            break_str = _fmt_duration(break_secs) if (s.script_running or s.on_break or s._break_start_ts) else '—'
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
                         'break_time': _fmt_duration(break_secs)})
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
            if state.on_break or state._break_start_ts:
                continue  # account is on break
            if not state.script_running:
                continue  # account is offline
            if time.time() - state.last_screenshot_ts >= threshold:
                state.last_screenshot_ts = time.time()
                due.append(name)
        if not due:
            return
        for i, name in enumerate(due):
            if is_startup and not self.cfg.get('screenshot_on_startup', False):
                continue
            trigger = 'startup' if is_startup else 'scheduled'
            self._enqueue_screenshot(SS_PRIORITY_SCHEDULED, name, trigger)

    # ── DreamBot / P2P Master AI update awareness ──────────────────────────────

    _UPDATE_CHECK_URL  = 'https://p2p-sdn-watch.p2pmonitor.workers.dev/p2p-master-ai/latest'
    _UPDATE_STATE_FILE = Path.home() / '.p2p_monitor' / 'update_check_state.json'

    @staticmethod
    def _ver_tuple(v: str) -> tuple:
        """Convert '2.143' or 'v2.143' to (2, 143) for numeric comparison."""
        try:
            return tuple(int(x) for x in v.lstrip('v').strip().split('.'))
        except Exception:
            return (0,)

    @staticmethod
    def _parse_dreambot_title(title: str) -> 'dict | None':
        """
        Parse a DreamBot window title.
        Example: 'DreamBot 4.1.67 - AccountName - P2P Master AI v2.141 - proxy (NEW CLIENT AVAILABLE)'
        Returns: {'script_version': '2.141', 'new_client': bool} or None if not a DreamBot title.
        """
        import re as _re
        if 'dreambot' not in title.lower():
            return None
        m = _re.search(r'P2P Master AI v(\d+\.\d+)', title, _re.IGNORECASE)
        if not m:
            return None
        return {
            'script_version': m.group(1),
            'new_client':     '(NEW CLIENT AVAILABLE)' in title.upper(),
        }

    def _fetch_latest_version(self) -> 'str | None':
        """Fetch latest_version from Cloudflare Worker. Returns version string or None."""
        import urllib.request, json as _json
        try:
            req = urllib.request.Request(
                self._UPDATE_CHECK_URL,
                headers={'User-Agent': 'P2PMonitor/update-check'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
            ver = data.get('latest_version', '').strip()
            return ver if ver else None
        except Exception as exc:
            self._dbg(f'[update_check] fetch failed: {exc}')
            return None

    def _load_update_state(self) -> set:
        try:
            if self._UPDATE_STATE_FILE.exists():
                return set(json.loads(self._UPDATE_STATE_FILE.read_text()))
        except Exception:
            pass
        return set()

    def _save_update_state(self, alerted: set) -> None:
        try:
            self._UPDATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._UPDATE_STATE_FILE.write_text(json.dumps(sorted(alerted)))
        except Exception:
            pass

    def _check_update_awareness(self, force: bool = False) -> None:
        """
        Run the update-awareness check.
        - force=True: run unconditionally (startup call).
        - force=False: only run once per day at 14:00 PC local time.
        """
        if not self.cfg.get('update_check_enabled', True):
            return
        if not force:
            now   = datetime.now()
            today = now.strftime('%Y-%m-%d')
            if self._last_update_check_date == today:
                return
            if now.hour != 14:
                return
            self._last_update_check_date = today

        # Read all DreamBot window titles using existing platform helpers
        try:
            from py.platform_ops import find_window_ids_by_name, get_window_title
            wids   = find_window_ids_by_name('P2P Master AI')
            titles = [get_window_title(w) for w in wids]
        except Exception as exc:
            self._dbg(f'[update_check] window read failed: {exc}')
            return

        parsed = [self._parse_dreambot_title(t) for t in titles]
        parsed = [p for p in parsed if p]

        if not parsed:
            self.log('ℹ Update awareness: no DreamBot windows found with P2P Master AI — no alert.')
            # Still allow NEW CLIENT AVAILABLE alerts if we somehow got titles
            return

        latest_ver = self._fetch_latest_version()  # None if unreachable

        alerted = self._load_update_state()
        new_alerts = []

        for info in parsed:
            local_ver  = info['script_version']
            new_client = info['new_client']
            key = f"{local_ver}|{latest_ver or 'unknown'}|{int(new_client)}"

            if key in alerted:
                continue  # already sent this alert combination

            script_outdated = (
                latest_ver is not None
                and self._ver_tuple(local_ver) < self._ver_tuple(latest_ver)
            )

            if not script_outdated and not new_client:
                if latest_ver is None:
                    self.log(f'⚠ Update awareness: local P2P Master AI v{local_ver}, latest SDN unavailable, DreamBot client update={new_client} — no script alert.')
                else:
                    self.log(f'✅ Update awareness: local P2P Master AI v{local_ver}, latest SDN v{latest_ver}, DreamBot client update={new_client} — current, no alert.')
                continue

            new_alerts.append((key, local_ver, latest_ver, new_client, script_outdated))

        if not new_alerts:
            return

        # Dedupe: only the unique combinations not yet sent
        url = self._router.resolve_url(None, 'default')
        if not url:
            self._dbg('[update_check] No default webhook — cannot send update alert.')
            return

        from py.discord import post_discord
        for key, local_ver, latest_ver, new_client, script_outdated in new_alerts:
            payload = self._build_update_alert_payload(local_ver, latest_ver,
                                                        new_client, script_outdated)
            ok, err = post_discord(url, payload)
            if ok:
                alerted.add(key)
                self.log(f'🔔 Update alert sent: script={local_ver} latest={latest_ver} '
                         f'new_client={new_client}')
            else:
                self.log(f'⚠ Update alert failed: {err}')

        self._save_update_state(alerted)

    def _build_update_alert_payload(self, local_ver: str, latest_ver: 'str | None',
                                    new_client: bool, script_outdated: bool) -> dict:
        """Build the Discord embed for an update alert."""
        from py.discord import _embed
        fields = []
        lines  = []

        if script_outdated and latest_ver:
            lines.append(f'**P2P Master AI** update available.')
            fields.append({'name': 'Local script',  'value': f'v{local_ver}',  'inline': True})
            fields.append({'name': 'Latest script', 'value': f'v{latest_ver}', 'inline': True})
            lines.append('Use `/relaunch <account>` to restart and load the latest script.')

        if new_client:
            lines.append('**DreamBot client** update available (`NEW CLIENT AVAILABLE` in title).')
            lines.append('Relaunch DreamBot to apply the client update.')

        title = '🔔 Update Available'
        desc  = '\n'.join(lines)
        color = 0xffaa00  # amber

        return _embed(title, desc, fields, color)

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
        self.log(f"📊 Sending daily summary — {len(rows)} account(s): {acct_names} [{window_str}]")
        ok, err = post_discord(url, combined_daily_summary_payload(self._router.mention(), rows, window_str))
        if not ok:
            self.log(f"  🚫 Daily summary failed: {err}")

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
                    folder = os.path.basename(d)
                    self._dbg(f'handle scan unreliable ({scan.reason}) — '
                              f'skipping offline check for {folder}')
                self._unreliable_scan_logged = True
            self.on_status()
            return
        for d in self._get_log_dirs():
            folder = os.path.basename(d)
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
        Returns True if DreamBot is actively writing to the folder.
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
    def _run(self):
        dirs = self._get_log_dirs()
        if not dirs:
            # Idle wait — check every 5 seconds for up to 10 minutes
            self.log("⏳ Waiting for active sessions...")
            deadline = time.time() + 600  # 10 minutes
            while self._running and time.time() < deadline:
                time.sleep(5)
                dirs = self._get_log_dirs()
                if dirs:
                    break
            if not dirs:
                self.log("⚠ No active sessions found after 10 minutes — stopping monitor.")
                self._running = False
                return
            self.log(f"✅ Sessions found — starting monitor.")
        for d in dirs:
            log_files = _get_log_files(d)
            active = _get_active_log_file(d)
            for f in log_files:
                with self._offsets_lock:
                    if active and normalize_path(f) == normalize_path(active):
                        pass  # leave active file for _startup_catchup / poll loop
                    else:
                        self._offsets[str(f)] = f.stat().st_size  # rotated — skip
        account_names = [os.path.basename(d) for d in dirs]
        self.log(f"▶ Monitoring {len(dirs)} account(s): {', '.join(account_names)}")
        for d in dirs:
            log_files = _get_log_files(d)
            active = _get_active_log_file(d)
            folder_active = self._is_folder_active(d)
            if active and folder_active:
                self._startup_catchup(str(active))
                # Pin active file to EOF so poll loop only sees new content from here
                try:
                    with self._offsets_lock:
                        self._offsets[str(active)] = active.stat().st_size
                except Exception as e:
                    self._dbg(f'Could not pin offset for {active.name}: {e}')
            else:
                # Stale log — create blank offline state, skip catchup entirely
                folder_name = os.path.basename(d)
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

        interval        = int(self.cfg.get('check_interval', 5))
        ss_min          = int(self.cfg.get('screenshot_minutes', 60))
        last_periodic   = time.time()
        self.on_status()  # populate status tab immediately after startup catchup

        # Startup update-awareness check — runs once in background, non-blocking
        if self.cfg.get('update_check_enabled', True):
            threading.Thread(target=self._check_update_awareness,
                             kwargs={'force': True}, daemon=True).start()

        while self._running:
            current_dirs = self._get_log_dirs()
            for d in current_dirs:
                active = _get_active_log_file(d)
                if not active:
                    continue
                self._check_file(str(active))
            now = time.time()
            if now - last_periodic >= 60:
                last_periodic = now
                self._check_screenshots(ss_min)
                self._check_daily_summary()
                self._check_update_awareness()
                self._prune_screenshots()
                self._prune_dedupe()
                self.on_status()
            time.sleep(interval)

    # ── Startup catchup ────────────────────────────────────────────────────────
    def _startup_catchup(self, active_path, is_rotation=False):
        """
        Called once per account at startup with the active log file path.
        Reads the current session (active file + any .log.1/.log.2 rotated
        parts of the same session) to set:
          script_start_ts  - timestamp of first 'Connecting to server' in oldest
                             session file (= DreamBot client session start time)
          total_break_secs - sum of ALL 'Break over N' ms across all session files
          logged_in / on_break / script_running - reconstructed by scanning ALL
                             session files oldest→newest on cold start; active file
                             only on rotation (preserves in-memory state)
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
                except Exception as e:
                    self._dbg(f'Could not parse rotation index for {n}: {e}')
                    return -999

            # Newest first for scanning; we'll reverse where needed
            session_files_newest_first = sorted(session_files, key=_rot_key, reverse=True)
            session_files_oldest_first = list(reversed(session_files_newest_first))

            # ── Read active file lines (reused for state scan + task scan) ────
            try:
                with open(active_path, 'r', encoding='utf-8', errors='replace') as f:
                    active_lines = [l.rstrip('\n') for l in f]
            except Exception as e:
                self.log(f"⚠ [{folder}] Could not read active log: {e}")
                active_lines = []

            # ── Debug: show what we're working with ───────────────────────────
            self._dbg(
                f'[{folder}] _startup_catchup: '
                f'{"rotation" if is_rotation else "cold start"} | '
                f'active={active_name} | '
                f'session files (oldest→newest): '
                f'{[f.name for f in session_files_oldest_first]}'
            )

            # ── Login / break state scan ──────────────────────────────────────
            # Forward scan is the correct algorithm — tracks the last unmatched
            # BREAK START giving the true current break state regardless of how
            # many completed breaks appear in the files.
            #
            # Cold start: reset all state then walk ALL session files oldest→newest
            # so that startup/login lines in rotated siblings are not missed.
            # Active file lines are already in active_lines — reuse them for the
            # active file pass to avoid reading it twice.
            #
            # Rotation: preserve last known good in-memory state — only override
            # if the new active file contains explicit state-changing lines.
            break_start_log_ts = None
            if not is_rotation:
                state.on_break       = False
                state.logged_in      = False
                state.script_running = False

                def _state_lines_for(sf):
                    """Return lines for sf, reusing active_lines to avoid double read."""
                    if sf.name == active_name:
                        return active_lines
                    try:
                        with open(str(sf), 'r', encoding='utf-8', errors='replace') as fh:
                            return [l.rstrip('\n') for l in fh]
                    except Exception as e:
                        self.log(f"⚠ [{folder}] Could not read {sf.name} for state scan: {e}")
                        return []

                for sf in session_files_oldest_first:
                    for line in _state_lines_for(sf):
                        b = strip_prefix(line).strip()
                        if _is_break_start(line):
                            state.on_break  = True
                            state.logged_in = False
                            m = LOG_TS_RE.match(line)
                            if m:
                                try:
                                    break_start_log_ts = datetime.strptime(
                                        m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                                except Exception as e:
                                    self._dbg(f'Break start timestamp parse failed: {e}')
                                    break_start_log_ts = None
                        elif _is_break_over(b.lower()):
                            state.on_break       = False
                            state.logged_in      = True
                            state.script_running = True
                            break_start_log_ts   = None
                        elif 'you have successfully been logged in' in b.lower():
                            state.on_break      = False
                            state.logged_in     = True
                            break_start_log_ts  = None
                        elif 'starting p2p master ai now' in b.lower() or \
                             'script set to running' in b.lower() or \
                             'awaiting login' in b.lower():
                            state.script_running = True
                            state.logged_in      = False
                        elif 'solvers all finished' in b.lower():
                            state.script_running = True
                            state.logged_in      = True
                            state.on_break       = False
                        elif 'NEW TASK' in b.upper() and state.script_running and not state.on_break:
                            # Script is processing tasks — definitively in game
                            # Don't override on_break — break state takes priority
                            state.logged_in = True
                        elif 'stopped p2p master ai' in b.lower():
                            state.script_running  = False
                            state.logged_in       = False
                            state.on_break        = False
                            state._break_start_ts = None
            else:
                # Rotation: only scan the new active file for state changes
                for line in active_lines:
                    b = strip_prefix(line).strip()
                    if _is_break_start(line):
                        state.on_break  = True
                        state.logged_in = False
                        m = LOG_TS_RE.match(line)
                        if m:
                            try:
                                break_start_log_ts = datetime.strptime(
                                    m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                            except Exception as e:
                                self._dbg(f'Break start timestamp parse failed: {e}')
                                break_start_log_ts = None
                    elif _is_break_over(b.lower()):
                        state.on_break       = False
                        state.logged_in      = True
                        state.script_running = True
                        break_start_log_ts   = None
                    elif 'you have successfully been logged in' in b.lower():
                        state.on_break      = False
                        state.logged_in     = True
                        break_start_log_ts  = None
                    elif 'starting p2p master ai now' in b.lower() or \
                         'script set to running' in b.lower() or \
                         'awaiting login' in b.lower():
                        state.script_running = True
                        state.logged_in      = False
                    elif 'solvers all finished' in b.lower():
                        state.script_running = True
                        state.logged_in      = True
                        state.on_break       = False
                    elif 'NEW TASK' in b.upper() and state.script_running and not state.on_break:
                        state.logged_in = True
                    elif 'stopped p2p master ai' in b.lower():
                        state.script_running  = False
                        state.logged_in       = False
                        state.on_break        = False
                        state._break_start_ts = None

            # Debug: show reconstructed state
            self._dbg(
                f'[{folder}] reconstructed state: '
                f'script_running={state.script_running} '
                f'logged_in={state.logged_in} '
                f'on_break={state.on_break} '
                f'script_start_ts={state.script_start_ts} '
                f'total_break_secs={state.total_break_secs:.1f}'
            )

            # If we started mid-break, find the break length for expected_end calculation
            if state.on_break:
                from py.util import parse_break_length_ms
                last_break_idx = None
                for i, line in enumerate(active_lines):
                    if _is_break_start(line):
                        last_break_idx = i
                if last_break_idx is not None:
                    break_length_ms = parse_break_length_ms(active_lines, last_break_idx + 1, max_search=3)
                    if break_length_ms is not None:
                        state._break_length_ms = break_length_ms  # persist for break-end scheduling

            # ── Uptime + break time: scan ALL session files ───────────────────
            # Walk oldest-first to find client start time and sum all completed breaks.
            # Uses timestamp math (BREAK START → Break over N) — ignores logged ms value
            # since DreamBot logs -100 for manually skipped breaks.
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
                            except Exception as e:
                                self._dbg(f'Break scan timestamp parse failed: {e}')
                        if _is_break_start(line) and ts:
                            pending_break_start = ts
                        elif _is_break_over(b.lower()) and ts and pending_break_start:
                            duration_ms = (ts - pending_break_start) * 1000
                            if duration_ms > 0:
                                total_break_ms += duration_ms
                            pending_break_start = None
                        if client_start_ts is None and 'connecting to server' in b.lower() and ts:
                            client_start_ts = ts

            if client_start_ts:
                state.script_start_ts = client_start_ts

            # Completed break total — set unconditionally from session scan.
            state.total_break_secs = total_break_ms / 1000.0

            # Seed _break_start_ts from log timestamp once — do not overwrite if
            # already set so the timer keeps accumulating across _startup_catchup calls.
            if state.on_break and not state._break_start_ts:
                if break_start_log_ts and break_start_log_ts <= time.time():
                    state._break_start_ts = break_start_log_ts
                else:
                    # Fallback: use now — better than leaving _break_start_ts unset
                    state._break_start_ts = time.time()

            state._startup_done = True

            # ── Current task: use shared slice_last_task from reader.py ────────
            # On rotation: preserve existing task — new log starts empty so searching
            # for a task would always fail and log a misleading warning.
            # On cold start: always search for task.
            if not is_rotation:
                last_task, last_activity = slice_last_task(active_lines)
                if last_task or last_activity:
                    state.last_task     = last_task
                    state.last_activity = last_activity
                    display = last_task or last_activity or '?'
                    self.log(f"📋 [{folder}] Startup task: {display}" +
                             (f" / {last_activity}" if last_activity and last_task else ''))
                elif state.on_break:
                    # Mid-break restart: slice_last_task returns empty because
                    # the last NEW TASK block is followed by BREAK START.
                    # Show the break with its length so the status tab is useful.
                    from py.util import format_break_duration
                    state.last_task = 'Break'
                    if 'break_length_ms' in dir() and break_length_ms:
                        state.last_activity = format_break_duration(break_length_ms)
                    else:
                        state.last_activity = ''
                    self.log(f"📋 [{folder}] Startup task: Break" +
                             (f" / {state.last_activity}" if state.last_activity else ''))
                else:
                    if state.script_running:
                        self.log(f"⚠ [{folder}] No task found in active log")

        except Exception as e:
            self.log(f"⚠ Startup scan error [{e.__class__.__name__}]: {e}")

    # ── Backfill ───────────────────────────────────────────────────────────────
    @staticmethod
    def _base_log_name(fname):
        """Strip rotation suffix from log filename.
        logfile-X.log.1 -> logfile-X.log
        logfile-X.log   -> logfile-X.log
        """
        import re as _re
        return _re.sub(r'\.\d+$', '', fname)

    @staticmethod
    def _log_file_sort_key(f):
        """Sort key for log files — extracts unix timestamp from filename.
        logfile-1777318301710.log.1 → 1777318301710
        Sorts correctly regardless of rotation suffix.
        """
        import re as _re
        m = _re.search(r'logfile-(\d+)', str(f))
        return int(m.group(1)) if m else 0

    def _backfill_history(self, folder):
        """
        Scan all log files for this account and write missing history entries.

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
        account = os.path.basename(folder)
        try:
            last_seen = get_last_seen(account)
            log_files = _get_log_files(folder)
            if not log_files:
                return

            # Sort all files by unix timestamp embedded in filename — correct
            # chronological order regardless of .log/.log.1/.log.2 suffix
            log_files = sorted(log_files, key=self._log_file_sort_key)

            total_entries  = 0
            new_last_seen  = None
            found_last     = (last_seen is None)  # if no marker, process everything

            bf_last_task     = ''
            bf_last_activity = ''
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

                # Find last_seen line in this file if not yet found
                if not found_last and last_seen:
                    for i, line in enumerate(lines):
                        if line == last_seen:
                            found_last = True
                            lines = lines[i+1:]  # process only lines after marker
                            break
                    else:
                        # Marker not in this file — skip entire file
                        continue

                if not lines:
                    continue

                # Process lines in chunks
                CHUNK = 500
                chunk = []
                entries_this_file = 0

                def _process_chunk(chunk):
                    nonlocal entries_this_file, bf_last_task, bf_last_activity, new_last_seen
                    if not chunk:
                        return
                    try:
                        events = parse_lines(chunk)
                    except Exception as pe:
                        self.log(f'  ⚠ [{account}] Backfill parse error: {pe}')
                        return

                    new_task_lines = {i for i, l in enumerate(chunk) if 'NEW TASK' in l.upper()}

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

                for line in lines:
                    chunk.append(line)
                    if len(chunk) >= CHUNK:
                        _process_chunk(chunk)
                        chunk = []
                if chunk:
                    _process_chunk(chunk)

                # Always update last_seen to the final line of this file,
                # regardless of whether it produced parseable events
                if lines:
                    new_last_seen = lines[-1]

                total_entries += entries_this_file

            if total_entries:
                # Dedup after backfill — cleans any duplicates from previous sessions
                try:
                    from py.history import _dedup_history_file, history_file
                    hf = history_file(account)
                    _, dupes = _dedup_history_file(hf, log_fn=self.log)
                    if dupes:
                        self.log(f'🧹 [{account}] Cleaned {dupes} duplicate history entries')
                except Exception as e:
                    self._dbg(f'History dedup failed [{account}]: {e}')

            # Update last-seen marker
            if new_last_seen:
                set_last_seen(account, new_last_seen)

            if self._on_backfill_done:
                self._on_backfill_done()

        except Exception as e:
            self.log(f'  ⚠ Backfill error [{account}]: {e}')

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
                    # Record base name as scanned so backfill skips .log.1
                    # — all content up to this offset was already seen live
                    try:
                        account = os.path.basename(os.path.dirname(path))
                        record_log_scanned(account, self._base_log_name(os.path.basename(path)))
                    except Exception:
                        pass
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
            self._process_lines(new_lines, folder)
            # Update last-seen marker so backfill knows where to resume.
            # Only write to disk when the value actually changes — avoids I/O every 5s.
            if new_lines:
                try:
                    account   = os.path.basename(os.path.dirname(path))
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
    #     _task_ctx         str         — task context string for error embed enrichment
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
                    is_99     = ev.get('_is_99', False)
                    url = self._router.resolve_url(account, 'levelup')
                    if url:
                        self._router.post_event(account, 'levelup',
                            levelup_payload(mention, account, value, level,
                                            total_level=total_lvl, is_99=is_99), url=url)
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

    def _maybe_schedule_auto_restart(self, folder: str, lines: list,
                                     state: 'AccountState') -> str:
        """
        Called from _process_lines after a 'script_event / stop' fires for 'folder'.
        Returns a status string used to annotate the Script Stopped Discord embed:
          'disabled'                                       — feature off (silent in monitor)
          'skipped — <reason>'                             — gate blocked it
          'scheduled in Nm' / 'scheduled at H:MM AM/PM'   — timer armed

        Gate checks (in order):
          1. auto_restart_enabled in cfg
          2. suppress window — monitor-initiated relaunch in progress
          3. manual-stop signature in recent log lines
          4. game update window gate (if auto_restart_game_update_window_only)
          5. preset still exists for this account
        """
        account = folder  # folder == account name throughout watcher

        # ── Gate 1: feature enabled ────────────────────────────────────────────
        if not self.cfg.get('auto_restart_enabled', False):
            return 'disabled'  # silent in monitor tab; shown in Discord embed only

        # ── Gate 2: suppress window (monitor-initiated relaunch) ──────────────
        try:
            from py.launcher import is_relaunch_suppressed
            if is_relaunch_suppressed(account):
                status = 'skipped — monitor-initiated relaunch in progress'
                self.log(f'🔄 [{account}] Auto restart {status}.')
                return status
        except Exception:
            pass

        # ── Gate 3: manual-stop signature ─────────────────────────────────────
        sig = 'user initiated script stop via control bar.'
        recent_snapshot = list(state._recent_lines)
        all_recent = list(lines) + recent_snapshot
        if any(sig in ln.lower() for ln in all_recent):
            status = 'skipped — manual stop detected'
            self.log(f'🔄 [{account}] Auto restart {status}.')
            return status

        # ── Gate 4: game update window ─────────────────────────────────────────
        if self.cfg.get('auto_restart_game_update_window_only', True):
            if not self._in_game_update_window():
                status = 'skipped — outside game update window (Tue/Wed 1–4 AM PT)'
                self.log(f'🔄 [{account}] Auto restart {status}.')
                return status

        # ── Gate 5: preset exists ──────────────────────────────────────────────
        from py.launcher import find_preset
        if not find_preset(self.cfg, account):
            status = 'skipped — no launcher preset found'
            self.log(f'🔄 [{account}] Auto restart {status}.')
            return status

        # ── Compute delay ──────────────────────────────────────────────────────
        delay_secs, delay_desc = self._compute_restart_delay(account, state)

        # ── Cancel any already-pending timer for this account ─────────────────
        old_timer = state._pending_restart_timer
        if old_timer is not None:
            old_timer.cancel()
            state._pending_restart_timer = None

        # ── Schedule ───────────────────────────────────────────────────────────
        status = f'scheduled {delay_desc}'
        self.log(f'⏰ [{account}] Auto restart {status} after Script Stopped.')

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
        return status

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

        # Update login/break state from this batch
        for idx, line in enumerate(lines):
            b = strip_prefix(line).strip()
            state._recent_lines.append(line)          # rolling buffer for manual-stop detection
            if _is_break_start(line):
                state.on_break  = True
                state.logged_in = False
                if not state._break_start_ts:
                    state._break_start_ts = time.time()
                from py.util import parse_break_length_ms
                bl_ms = parse_break_length_ms(lines, idx + 1, max_search=3)
                if bl_ms is not None:
                    state._break_length_ms = bl_ms    # persist for break-end scheduling
            elif _is_break_over(b.lower()):
                # Real completed break — not 'Break over -> Startup'
                state.on_break = False
                state.logged_in = True
                state.script_running = True
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts  = None
                state._break_length_ms = None   # break consumed; clear for next cycle
            elif 'interacting (widget) logout' in b.lower():
                state.logged_in = False
                # Do NOT set _break_start_ts here — logout is not a break
            elif 'you have successfully been logged in' in b.lower():
                state.logged_in = True
                state.on_break  = False
                if state._break_start_ts:
                    state.total_break_secs += time.time() - state._break_start_ts
                    state._break_start_ts = None
            elif 'starting p2p master ai now' in b.lower():
                state.script_running = True
                state.logged_in      = False
                # Only set script_start_ts if not already set from DreamBot client start
                # (Connecting to server in _startup_catchup). This preserves session uptime
                # across script restarts — uptime tracks DreamBot session, not script runs.
                if not state.script_start_ts:
                    m = LOG_TS_RE.match(line)
                    if m:
                        try:
                            state.script_start_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                        except Exception as e:
                            self._dbg(f'script_start_ts parse failed: {e}')
            elif 'script set to running' in b.lower() or 'awaiting login' in b.lower():
                state.script_running = True
                state.logged_in      = False
            elif 'solvers all finished' in b.lower():
                state.script_running = True
                state.logged_in      = True
                state.on_break       = False
            elif 'NEW TASK' in b.upper() and state.script_running and not state.on_break:
                # Script is processing tasks — definitively in game
                # Don't touch on_break — break state takes priority
                state.logged_in = True
            elif 'stopped p2p master ai' in b.lower():
                # Capture break context before clearing — used by _compute_restart_delay
                # if 'Respect breaks on relaunch' is enabled.
                state._pre_stop_break_snap = {
                    'on_break':       state.on_break,
                    'break_start_ts': state._break_start_ts,
                    'break_length_ms': state._break_length_ms,
                }
                state.script_running  = False
                state.logged_in       = False
                state.on_break        = False
                state._break_start_ts = None
                state.inferno.reset()

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
                is_99 = ev.get('_is_99', False)
                notify_every = int(self.cfg.get('levelup_every', 5))
                last_notified = state.notified_levels.get(skill, 0)
                if is_99:
                    should_notify = True  # Level 99 ALWAYS notifies
                else:
                    should_notify = (level // notify_every > last_notified // notify_every
                                     or last_notified == 0) if level else True
                state.notified_levels[skill] = level
                if not should_notify or self._is_muted(folder):
                    continue
                prefix = "🎆" if is_99 else "🎉"
                self.log(f"{prefix} [{folder}] Level up: {skill} → {level}")

            elif etype == 'script_event':
                # ── Auto-restart logic: runs regardless of notification/mute settings ──
                ar_status = None
                if value == 'stop':
                    try:
                        ar_status = self._maybe_schedule_auto_restart(folder, lines, state)
                    except Exception as _ar_exc:
                        self._dbg(f'[auto_restart] scheduling error for {folder}: {_ar_exc}')
                elif value == 'start':
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
            self.log(f"🤖 Rate limited checking membership for thread {thread_id} — skipping this pass")
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


