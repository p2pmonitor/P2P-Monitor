"""
py/relaunch.py — Central relaunch coordinator (RelaunchManager).

All monitor-initiated relaunch requests (Discord /relaunch, /relaunch all)
flow through one manager so every source follows the same rules:

  • Respect Break (auto_restart_respect_breaks) is honored BEFORE a running
    client is ever closed — not just before launching. A /relaunch against a
    running, not-on-break account is queued; the client is closed at the
    account's next break start and relaunched at that break's end. An account
    already on break is closed immediately and relaunched at break end.
  • Respect Break OFF → immediate close + relaunch, no break checks, no queue.
  • Startup confirmation: a relaunch attempt only counts as successful when
    the watcher detects the account's Script Started line — a spawned process
    is never treated as success.
  • Retry/backoff: unconfirmed attempts retry at 5, 10, 20, 30, then 60-minute
    (cap) intervals until the script starts, the account is removed, or the
    user intervenes (a detected Script Started from ANY source clears the
    pending state).
  • Sequential worker: only one launch/confirmation attempt runs at a time.
    Accounts waiting out a retry delay or a break window do not occupy the
    worker.
  • Pending state is persisted to ~/.p2p_monitor/pending_relaunches.json so a
    monitor restart does not forget that an account still needs to relaunch.

Process safety follows the launcher's existing rules: ownership is always
validated via window title before anything is terminated; nothing is ever
killed by generic process name; saved PID state is cache, not truth.
"""
import json
import queue
import threading
import time
from pathlib import Path

PENDING_FILE = Path.home() / '.p2p_monitor' / 'pending_relaunches.json'

# Retry delays (minutes) after an unconfirmed relaunch attempt; the last
# entry repeats (cap) for every further attempt.
RETRY_DELAYS_MIN = [5, 10, 20, 30, 60]

# How long to wait for the Script Started confirmation after an attempt.
CONFIRM_TIMEOUT_SECS = 240          # 4 minutes

# If a break starts but its length line never parses, wait at most this long
# before closing anyway and falling back to a random restart delay.
BREAK_LENGTH_WAIT_SECS = 90

# Phases (informational; drives log wording and resume behavior)
P_QUEUED_BREAK  = 'queued_break'     # waiting for the account's next break start
P_WAIT_BREAK_END = 'waiting_break_end'  # client closed; timer armed for break end
P_WAIT_RETRY    = 'waiting_retry'    # last attempt unconfirmed; backoff timer armed
P_QUEUED_NOW    = 'queued_now'       # in the worker queue for an immediate attempt
P_LAUNCHING     = 'launching'        # worker is actively attempting / confirming


# On resume without a usable saved resume time, wait this long before
# deciding what to do — watcher startup catch-up needs a moment to
# reconstruct each account's live break state from the logs first.
RESUME_DECISION_DELAY_SECS = 30


class RelaunchManager:
    """Coordinates queued relaunches. Owned and started by LogWatcher."""

    def __init__(self, watcher):
        self._w        = watcher              # LogWatcher — cfg/log/router/state access
        self._lock     = threading.RLock()
        self._pending  = {}                   # account -> {'phase', 'attempts', 'requested_ts'}
        self._timers   = {}                   # account -> threading.Timer
        self._confirm  = {}                   # account -> threading.Event
        self._queue    = queue.Queue()
        self._running  = False
        self._worker   = None

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        self._running = True
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._resume_persisted()

    def stop(self):
        self._running = False
        with self._lock:
            for t in self._timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self._timers.clear()
            self._save_pending()
        try:
            self._queue.put_nowait(None)      # wake worker so it can exit
        except Exception:
            pass

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _cfg(self):
        return self._w.cfg

    def _log(self, msg):
        self._w.log(msg)

    def _notify(self, account, text):
        """Log to the Monitor tab and best-effort post to the account's
        Discord channel/thread. Never raises."""
        self._log(text)
        try:
            from py.discord import post_discord
            url, _ = self._w._router.wh_with_thread('default', account)
            if url:
                post_discord(url, {'content': text})
        except Exception:
            pass

    def _respect_breaks(self):
        return bool(self._cfg().get('auto_restart_respect_breaks', True))

    def _state_for(self, account):
        """Live AccountState for a monitored account, or None."""
        try:
            with self._w._accounts_lock:
                return self._w._accounts.get(account)
        except Exception:
            return None

    def _set_phase(self, account, phase, attempts=None, resume_at=None):
        with self._lock:
            entry = self._pending.setdefault(
                account, {'phase': phase, 'attempts': 0, 'requested_ts': time.time()})
            entry['phase'] = phase
            if attempts is not None:
                entry['attempts'] = attempts
            # resume_at: absolute epoch ts of when the armed timer will fire
            # (break end or next retry) — persisted so a monitor restart can
            # re-arm the remaining delay instead of launching immediately.
            if resume_at is not None:
                entry['resume_at'] = resume_at
            elif phase in (P_QUEUED_NOW, P_LAUNCHING, P_QUEUED_BREAK):
                entry.pop('resume_at', None)
            self._save_pending()

    def _clear(self, account, reason=''):
        with self._lock:
            existed = account in self._pending
            self._pending.pop(account, None)
            t = self._timers.pop(account, None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
            self._save_pending()
        if existed and reason:
            self._log(f'🔄 [{account}] Pending relaunch cleared — {reason}')

    def pending_phase(self, account):
        with self._lock:
            e = self._pending.get(account)
            return e['phase'] if e else None

    def _arm_timer(self, account, delay_secs, fn):
        with self._lock:
            old = self._timers.pop(account, None)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
            t = threading.Timer(max(1.0, delay_secs), fn)
            t.daemon = True
            t.start()
            self._timers[account] = t

    # ── Persistence ─────────────────────────────────────────────────────────
    def _save_pending(self):
        """Best-effort write; caller holds self._lock."""
        try:
            PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
            snap = {a: {k: v for k, v in e.items()
                        if k in ('phase', 'attempts', 'requested_ts', 'resume_at')}
                    for a, e in self._pending.items()}
            with open(PENDING_FILE, 'w') as f:
                json.dump(snap, f, indent=2)
        except Exception:
            pass

    def _resume_persisted(self):
        """On watcher start: re-adopt any pending relaunches from the previous
        session.

        Timing state is honored, not discarded:
          • waiting_break_end / waiting_retry with a future resume_at →
            re-arm a timer for exactly the remaining delay.
          • resume_at already passed → attempt promptly (via a short decision
            delay, see below, in case the account is actually mid-break).
          • no resume_at at all → wait RESUME_DECISION_DELAY_SECS so startup
            catch-up can reconstruct on_break from the logs, THEN route the
            request through the normal request_relaunch() logic — this stops
            a restart-during-break from relaunching mid-break just because
            the manager came up before the break state did."""
        try:
            if not PENDING_FILE.exists():
                return
            with open(PENDING_FILE) as f:
                saved = json.load(f)
            if not isinstance(saved, dict) or not saved:
                return
        except Exception:
            return

        from py.launcher import find_preset
        now = time.time()
        for account, entry in saved.items():
            if not isinstance(entry, dict):
                continue
            if not find_preset(self._cfg(), account):
                continue    # preset removed since last session — drop silently
            attempts  = int(entry.get('attempts', 0) or 0)
            phase     = entry.get('phase') or P_QUEUED_NOW
            resume_at = entry.get('resume_at')
            with self._lock:
                self._pending[account] = {'phase': phase,
                                          'attempts': attempts,
                                          'requested_ts': entry.get('requested_ts', now)}

            if resume_at and resume_at > now and phase in (P_WAIT_BREAK_END,
                                                           P_WAIT_RETRY):
                remaining = resume_at - now
                self._set_phase(account, phase, attempts=attempts,
                                resume_at=resume_at)
                fire = (self._on_break_end_timer if phase == P_WAIT_BREAK_END
                        else self._on_retry_timer)
                self._arm_timer(account, remaining, lambda a=account, f=fire: f(a))
                self._log(f'🔄 [{account}] Resumed pending relaunch — '
                          f'{round(remaining / 60)}m remaining on its '
                          f'{"break window" if phase == P_WAIT_BREAK_END else "retry delay"}.')
            else:
                # Timing unknown or already elapsed — decide after startup
                # catch-up has had a chance to rebuild live break state.
                self._log(f'🔄 [{account}] Resumed pending relaunch from previous '
                          f'session — deciding in {RESUME_DECISION_DELAY_SECS}s '
                          f'(waiting for startup state).')
                self._arm_timer(account, RESUME_DECISION_DELAY_SECS,
                                lambda a=account, n=attempts: self._resume_decide(a, n))
        with self._lock:
            self._save_pending()

    def _resume_decide(self, account, attempts):
        """Deferred resume decision: route through the normal request logic
        (which now evaluates live break state correctly), preserving the
        attempt count from the previous session."""
        if not self._running:
            return
        if self.pending_phase(account) is None:
            return   # cleared meanwhile (e.g. Script Started detected)
        try:
            result = self.request_relaunch(account)
            with self._lock:
                if account in self._pending:
                    self._pending[account]['attempts'] = attempts
                    self._save_pending()
            self._log(f'🔄 [{account}] Resume decision: {result.message}')
        except Exception as exc:
            self._log(f'❌ [{account}] Resume decision failed: {exc}')

    # ── Auto-restart entry point ─────────────────────────────────────────────
    def request_auto_restart_launch(self, account):
        """Final launch step for the watcher's auto-restart path. All
        auto-restart gating (manual-stop detection, game-update window,
        suppress window, respect-break delay) has already happened by the
        time this is called — this just runs the actual attempt through the
        manager so it gets Script Started confirmation, retry/backoff,
        persisted pending state, geometry restore, and the
        one-active-launch-at-a-time worker."""
        with self._lock:
            existing = self._pending.get(account)
            if existing and existing.get('phase') == P_LAUNCHING:
                self._log(f'🔄 [{account}] Auto restart: a relaunch attempt is '
                          f'already in progress — not queuing another.')
                return
            self._pending[account] = {'phase': P_QUEUED_NOW, 'attempts': 0,
                                      'requested_ts': time.time()}
            self._save_pending()
        self._enqueue(account)

    # ── Public API — request entry points ───────────────────────────────────
    def request_relaunch(self, account_arg):
        """Handle a single /relaunch. Returns a LaunchResult-compatible object
        immediately; the actual close/launch/confirm work happens on the
        manager's worker thread and timers."""
        from py.launcher import LaunchResult, find_preset, discover_account_process

        # Resolve to a preset account (exact, then case-insensitive substring)
        account = self._resolve_account(account_arg)
        if not account:
            return LaunchResult(ok=False, account=account_arg, action='failed',
                                message=f'No launcher preset found for "{account_arg}".')
        if not find_preset(self._cfg(), account):
            return LaunchResult(ok=False, account=account, action='failed',
                                message=f'No launcher preset found for "{account}".')

        try:
            running = discover_account_process(account)
        except ValueError as exc:
            return LaunchResult(ok=False, account=account, action='skipped',
                                message=f'Multiple windows matched "{account}" — {exc}. '
                                        f'Close duplicates and retry.')

        with self._lock:
            phase = self.pending_phase(account)
            if phase in (P_LAUNCHING,):
                return LaunchResult(ok=True, account=account, action='queued',
                                    message=f'{account}: a relaunch attempt is already '
                                            f'in progress.')
            # A fresh request supersedes an existing queue entry — reset attempts.
            self._pending[account] = {'phase': P_QUEUED_NOW, 'attempts': 0,
                                      'requested_ts': time.time()}
            self._save_pending()

        # ── Respect Break ON: break state is evaluated BEFORE the running
        # check — an account on break relaunches at break end whether its
        # client is open or already closed (a closed client mid-break must
        # not launch immediately, that would violate the break).
        state = self._state_for(account)
        on_break_now = bool(state and state.on_break)
        snap = getattr(state, '_pre_stop_break_snap', None) if state else None
        snap_active = False
        snap_end_ts = None
        if snap and snap.get('on_break') and snap.get('break_start_ts') \
                and snap.get('break_length_ms'):
            snap_end_ts = (snap['break_start_ts']
                           + (snap['break_length_ms'] / 1000.0))
            snap_active = snap_end_ts > time.time()

        if self._respect_breaks() and (on_break_now or snap_active):
            if running:
                # Close now (validated), relaunch at break end.
                self._set_phase(account, P_WAIT_BREAK_END)
                threading.Thread(target=self._close_and_schedule_break_end,
                                 args=(account,), daemon=True).start()
                return LaunchResult(ok=True, account=account, action='queued',
                                    message=f'{account} is on break — closing now; '
                                            f'relaunch is scheduled for the break end.')
            # Client already closed mid-break: nothing to close — just wait
            # out the remainder of the break, then launch.
            self._set_phase(account, P_WAIT_BREAK_END)
            end_ts = None
            if state is not None and state._break_start_ts and state._break_length_ms:
                end_ts = state._break_start_ts + (state._break_length_ms / 1000.0)
            if end_ts is None or end_ts <= time.time():
                end_ts = snap_end_ts
            self._schedule_break_end_launch(account, end_ts)
            return LaunchResult(ok=True, account=account, action='queued',
                                message=f'{account} is on break (client closed) — '
                                        f'relaunch is scheduled for the break end.')

        # ── Respect Break OFF, or nothing running → immediate attempt ───────
        if not self._respect_breaks() or not running:
            self._set_phase(account, P_QUEUED_NOW)
            self._enqueue(account)
            verb = 'Relaunching' if running else 'Launching'
            return LaunchResult(ok=True, account=account, action='queued',
                                message=f'{verb} {account} now — success will be '
                                        f'confirmed when Script Started is detected.')

        # ── Respect Break ON with a running, not-on-break client ────────────
        self._set_phase(account, P_QUEUED_BREAK)
        return LaunchResult(ok=True, account=account, action='queued',
                            message=f'Relaunch queued for {account} — Respect Break '
                                    f'is enabled, so it will relaunch during the '
                                    f'next break window.')

    def request_relaunch_all(self):
        """Handle /relaunch all. Each account gets its own independent pending
        state; immediate attempts feed the sequential worker one at a time."""
        from py.launcher import LaunchResult, list_presets
        presets = list_presets(self._cfg())
        if not presets:
            return [LaunchResult(ok=False, account='(all)', action='failed',
                                 message='No launcher presets configured.')]
        results = []
        for preset in presets:
            account = (preset.get('account') or '').strip()
            if not account:
                continue
            results.append(self.request_relaunch(account))
        return results

    def _resolve_account(self, account_arg):
        from py.launcher import list_presets
        arg = (account_arg or '').strip()
        if not arg:
            return None
        names = [(p.get('account') or '').strip()
                 for p in list_presets(self._cfg()) if p.get('account')]
        for n in names:
            if n.lower() == arg.lower():
                return n
        matches = [n for n in names if arg.lower() in n.lower()]
        return matches[0] if len(matches) == 1 else (arg if arg in names else
                                                     (matches[0] if matches else None))

    # ── Watcher hooks ────────────────────────────────────────────────────────
    def on_script_started(self, account):
        """Watcher detected Script Started for this account. Confirms an
        in-flight attempt and clears ANY pending relaunch — a running script
        is the success condition regardless of who started it."""
        evt = self._confirm.get(account)
        if evt is not None:
            evt.set()
        phase = self.pending_phase(account)
        if phase and phase != P_LAUNCHING:
            # Started outside our own attempt (manual start, auto-restart…)
            self._clear(account, reason='Script Started detected.')

    def on_break_started(self, account):
        """Watcher detected a break start. If this account has a relaunch
        queued for the next break window, close it now and schedule the
        relaunch at break end."""
        if self.pending_phase(account) != P_QUEUED_BREAK:
            return
        self._set_phase(account, P_WAIT_BREAK_END)
        threading.Thread(target=self._close_and_schedule_break_end,
                         args=(account,), daemon=True).start()

    # ── Break-window close + schedule ────────────────────────────────────────
    def _schedule_break_end_launch(self, account, break_end_ts):
        """Arm the launch timer for a break's end (or the random-delay
        fallback when the break window is unknown), persisting the absolute
        resume time so a monitor restart re-arms the remainder."""
        delay = (break_end_ts - time.time()) if break_end_ts else None
        if delay is None or delay <= 0:
            # Length never parsed / break already over — fall back to the
            # configured random restart delay, same as auto-restart does.
            import random
            try:
                lo = max(0, int(self._cfg().get('auto_restart_min_minutes', 1)))
                hi = max(lo, int(self._cfg().get('auto_restart_max_minutes', 30)))
            except (ValueError, TypeError):
                lo, hi = 1, 30
            delay = max(10.0, random.randint(lo, hi) * 60.0)
            desc = f'in {round(delay / 60)}m (break window unknown)'
        else:
            import datetime as _dt
            end_dt = _dt.datetime.fromtimestamp(break_end_ts)
            desc = f'at {end_dt.strftime("%I:%M %p").lstrip("0") or "12:00 AM"} (break end)'

        self._set_phase(account, P_WAIT_BREAK_END, resume_at=time.time() + delay)
        self._notify(account, f'🔄 Relaunch queued for {account} — '
                              f'relaunching {desc}.')
        self._arm_timer(account, delay, lambda: self._on_break_end_timer(account))

    def _close_and_schedule_break_end(self, account):
        """Close the running client (validated), then arm a timer for the
        break's end. Runs on its own thread — never on the watcher loop."""
        state = self._state_for(account)

        # Wait briefly for the break length to parse (it usually arrives on
        # the line right after BREAK START).
        deadline = time.time() + BREAK_LENGTH_WAIT_SECS
        while (state is not None and state.on_break
               and state._break_length_ms is None and time.time() < deadline
               and self._running):
            time.sleep(2)

        # Shutdown guard: if the monitor was stopped while waiting above,
        # do NOT proceed to close the user's client.
        if not self._running:
            return
        if self.pending_phase(account) != P_WAIT_BREAK_END:
            return  # cleared/superseded while waiting (e.g. script started)

        # Snapshot break window BEFORE closing — closing stops the log.
        break_end_ts = None
        if state is not None:
            start_ts = state._break_start_ts
            length_ms = state._break_length_ms
            snap = getattr(state, '_pre_stop_break_snap', None)
            if (start_ts is None or length_ms is None) and snap:
                start_ts = start_ts or snap.get('break_start_ts')
                length_ms = length_ms or snap.get('break_length_ms')
            if start_ts and length_ms and length_ms > 0:
                break_end_ts = start_ts + (length_ms / 1000.0)

        ok, msg = self._close_client(account)
        if not ok and msg:
            self._log(f'⚠️ [{account}] {msg}')

        self._schedule_break_end_launch(account, break_end_ts)

    def _on_break_end_timer(self, account):
        if not self._running:
            return
        if self.pending_phase(account) != P_WAIT_BREAK_END:
            return
        self._set_phase(account, P_QUEUED_NOW)
        self._enqueue(account)

    # ── Worker — one attempt at a time ───────────────────────────────────────
    def _enqueue(self, account):
        self._queue.put(account)

    def _worker_loop(self):
        while self._running:
            try:
                account = self._queue.get(timeout=2)
            except queue.Empty:
                continue
            if account is None:
                continue
            if not self._running:
                break
            if self.pending_phase(account) is None:
                continue    # cleared while queued (e.g. script started manually)
            try:
                self._attempt(account)
            except Exception as exc:
                self._log(f'❌ [{account}] Relaunch attempt raised: {exc}')
                self._schedule_retry(account)

    def _attempt(self, account):
        """Close-if-running (validated), launch, then wait for Script Started.
        Runs on the worker thread — exactly one attempt is active at a time."""
        from py.launcher import (find_preset, discover_account_process,
                                 launch_account)
        self._set_phase(account, P_LAUNCHING)

        if not find_preset(self._cfg(), account):
            self._clear(account, reason='launcher preset removed.')
            return

        # Close a running client first (retry attempts may find a stuck client).
        try:
            running = discover_account_process(account)
        except ValueError as exc:
            self._notify(account, f'⚠️ Relaunch skipped for {account} — multiple '
                                  f'windows matched ({exc}). Close duplicates; '
                                  f'will retry.')
            self._schedule_retry(account)
            return

        # ── Duplicate-launch guard ───────────────────────────────────────────
        # If no window matched but the saved PID is still alive, the client
        # may well be running with discovery failing transiently (observed in
        # the field: a valid client + valid saved PID, and the title search
        # momentarily returned nothing). Launching now could open a duplicate
        # client for the same account, and closing by PID alone would be
        # killing blind. Do neither: dump what the matcher could see and
        # retry on the normal backoff — a transient discovery failure heals
        # itself by the next attempt.
        if not running:
            saved_pid = None
            try:
                from py.launcher import get_account_pid
                from py.platform_ops import is_pid_running
                saved_pid = get_account_pid(account)
                pid_alive = bool(saved_pid) and is_pid_running(saved_pid)
            except Exception:
                pid_alive = False
            if pid_alive:
                try:
                    from py.platform_ops import list_dreambot_window_titles
                    from py.util import write_debug_entry
                    write_debug_entry('relaunch', {
                        'account': account,
                        'msg': 'Ownership/discovery ambiguity — saved PID alive, '
                               'no window matched. Refusing to close or launch.',
                        'saved_pid': saved_pid,
                        'visible_dreambot_windows': list_dreambot_window_titles(),
                    })
                except Exception:
                    pass
                self._schedule_retry(
                    account,
                    reason=(f'Could not verify existing DreamBot window for '
                            f'{account}, but saved PID {saved_pid} is still '
                            f'alive — not closing, not launching a duplicate.'))
                return

        if running:
            ok, msg = self._close_client(account)
            if not ok:
                self._notify(account, f'⚠️ Relaunch for {account} could not close '
                                      f'the running client safely: {msg} — will retry.')
                self._schedule_retry(account)
                return
            time.sleep(10)   # same safe close→launch gap relaunch_account uses

        # Arm the confirmation event BEFORE launching.
        evt = threading.Event()
        self._confirm[account] = evt

        result = launch_account(self._cfg(), account, log_fn=self._w.log)
        if not result.ok:
            self._confirm.pop(account, None)
            self._notify(account, f'❌ Relaunch failed for {account} — '
                                  f'{result.message}')
            self._schedule_retry(account)
            return

        try:
            timeout = max(60, int(self._cfg().get('relaunch_confirm_timeout_secs',
                                                  CONFIRM_TIMEOUT_SECS)))
        except (ValueError, TypeError):
            timeout = CONFIRM_TIMEOUT_SECS
        confirmed = evt.wait(timeout)
        self._confirm.pop(account, None)

        if confirmed:
            self._clear(account)
            self._notify(account, f'✅ Relaunch successful for {account} — '
                                  f'Script Started detected.')
        else:
            self._schedule_retry(account, launched_but_unconfirmed=True)

    def _schedule_retry(self, account, launched_but_unconfirmed=False, reason=None):
        with self._lock:
            entry = self._pending.get(account)
            if entry is None:
                return
            entry['attempts'] += 1
            attempts = entry['attempts']
            entry['phase'] = P_WAIT_RETRY
            self._save_pending()
        delay_min = RETRY_DELAYS_MIN[min(attempts - 1, len(RETRY_DELAYS_MIN) - 1)]
        delay_secs = delay_min * 60.0
        self._set_phase(account, P_WAIT_RETRY, resume_at=time.time() + delay_secs)
        if reason:
            self._notify(account, f'⚠️ {reason} Retrying in {delay_min} minutes.')
        elif launched_but_unconfirmed:
            self._notify(account, f'❌ Relaunch failed for {account} — no Script '
                                  f'Started detected within the timeout. '
                                  f'Retrying in {delay_min} minutes.')
        else:
            self._log(f'⏰ [{account}] Relaunch retry scheduled in {delay_min}m '
                      f'(attempt {attempts}).')
        self._arm_timer(account, delay_secs,
                        lambda: self._on_retry_timer(account))

    def _on_retry_timer(self, account):
        if not self._running:
            return
        if self.pending_phase(account) != P_WAIT_RETRY:
            return
        self._set_phase(account, P_QUEUED_NOW)
        self._enqueue(account)

    # ── Safe close (validated; captures + persists geometry) ────────────────
    def _close_client(self, account):
        """Validated close of the account's client. Returns (ok, message).
        Never kills blind — window-title ownership is required. Captures and
        persists window geometry before terminating."""
        from py.launcher import (discover_account_process, set_relaunch_suppress,
                                 _set_pid, save_account_geometry)
        try:
            window_result = discover_account_process(account)
        except ValueError as exc:
            return False, f'multiple windows matched: {exc}'
        if not window_result:
            return True, 'no running client found'
        pid = window_result.get('pid')
        if not pid:
            return False, ('window found but PID could not be resolved — '
                           'refusing to close')

        # Geometry capture + persist — best-effort, never blocks the close.
        try:
            wid = window_result.get('window_id')
            if wid:
                from py.platform_ops import (get_window_geometry_for_restore,
                                             is_window_minimized)
                if not is_window_minimized(wid):
                    geom = get_window_geometry_for_restore(wid)
                    if geom:
                        save_account_geometry(account, geom)
        except Exception:
            pass

        _set_pid(account, None)
        set_relaunch_suppress(account, duration_secs=300.0)
        try:
            from py.platform_ops import terminate_process_tree
            terminate_process_tree(pid, timeout=10)
        except Exception as exc:
            return True, f'terminate raised: {exc} — continuing'
        return True, ''
