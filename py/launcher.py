"""
py/launcher.py — Safe DreamBot account launcher backend for P2P Monitor v1.8.0-beta.2

Owns all high-level account launch logic:
  - Preset lookup and command building
  - Safe launch (fresh) and relaunch (close existing → wait → launch)
  - PID discovery and validation via window title (never by generic process name)
  - Runtime PID state cache at ~/.p2p_monitor/launcher_state.json
  - Structured LaunchResult for UI and Discord consumption

Safety contract:
  - NEVER kills by generic process name (no "java.exe", no "DreamBot" wildcard)
  - Only closes a process when window title can be matched to the requested account
  - Ambiguous matches (multiple windows, or saved PID but no window) → refuse with explanation
  - Saved PID state is always validated/re-discovered before use; it is a cache, not truth

Platform support:
  - Linux:   xdotool window lookup + getwindowpid
  - Windows: EnumWindows title match + GetWindowThreadProcessId
  Both paths live in py/platform_ops.py; this module only calls those helpers.
"""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from py.util import write_debug_entry
from pathlib import Path
from typing import Optional

# Lazy import — platform_ops is available whenever the app runs.
# We import at call time in helpers to avoid circular import issues during testing.

# ── Monitor-initiated relaunch suppress window ─────────────────────────────────
# When the monitor closes a DreamBot client (via relaunch_account), the resulting
# "Stopped P2P Master AI!" log line would normally trigger auto-restart.
# These helpers let the watcher skip that spurious re-trigger.

_relaunch_suppress: dict = {}          # account → suppress_until epoch timestamp
_relaunch_suppress_lock = threading.Lock()


def set_relaunch_suppress(account: str, duration_secs: float = 300.0) -> None:
    """
    Mark 'account' as having a monitor-initiated relaunch in progress.
    Auto-restart will be suppressed for 'duration_secs' seconds (default 5 minutes).
    Called by relaunch_account() immediately before terminate_process_tree().
    """
    with _relaunch_suppress_lock:
        _relaunch_suppress[account] = time.time() + duration_secs


def is_relaunch_suppressed(account: str) -> bool:
    """Return True if 'account' is within a monitor-initiated relaunch suppress window."""
    with _relaunch_suppress_lock:
        return time.time() < _relaunch_suppress.get(account, 0.0)


# ── State file ─────────────────────────────────────────────────────────────────

_STATE_FILE = Path.home() / '.p2p_monitor' / 'launcher_state.json'


def _load_pid_state() -> dict:
    """Load account→PID mapping from launcher_state.json. Returns {} on any error."""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_pid_state(state: dict) -> None:
    """Write account→PID mapping to launcher_state.json. Fails silently."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _set_pid(account: str, pid: Optional[int]) -> None:
    """Update a single account's PID entry in the state file."""
    state = _load_pid_state()
    if pid is None:
        state.pop(account, None)
    else:
        state[account] = pid
    _save_pid_state(state)


def _get_pid(account: str) -> Optional[int]:
    """Return the saved PID for an account, or None."""
    return _load_pid_state().get(account)


# ── Public PID cache API (used by watcher, screenshot callback) ────────────────

def get_account_pid(account: str) -> Optional[int]:
    """Return the cached PID for account, or None. Never raises."""
    try:
        return _get_pid(account)
    except Exception:
        return None


def set_account_pid(account: str, pid: Optional[int]) -> None:
    """Write (or clear) the cached PID for account. Never raises."""
    try:
        _set_pid(account, pid)
    except Exception:
        pass


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class LaunchResult:
    """
    Structured result returned by all launcher functions.

    Fields:
        ok       — True if the operation succeeded (launched or relaunched)
        account  — account name this result is for
        action   — one of: 'launched', 'relaunched', 'skipped', 'failed'
        message  — human-readable detail for UI/Discord display
        pid      — immediate Popen PID (not the confirmed client PID — that
                   is discovered asynchronously in a background thread)
        details  — optional dict of extra metadata
    """
    ok:      bool
    account: str
    action:  str   # 'launched' | 'relaunched' | 'skipped' | 'failed'
    message: str
    pid:     Optional[int] = None
    details: Optional[dict] = field(default=None)


# ── Preset helpers ─────────────────────────────────────────────────────────────

def list_presets(cfg: dict) -> list:
    """Return the list of launcher preset dicts from config."""
    return cfg.get('launcher_presets', [])


def find_preset(cfg: dict, account: str) -> Optional[dict]:
    """
    Find a launcher preset by account name (case-insensitive).
    Returns the preset dict, or None if not found.
    """
    needle = account.strip().lower()
    for p in list_presets(cfg):
        if p.get('account', '').strip().lower() == needle:
            return p
    return None


# ── Command builder ────────────────────────────────────────────────────────────

def build_command(jar_path: str, preset: dict) -> list:
    """
    Build the java CLI command list from a launcher preset dict.

    Moved here from ui/launcher_tab.py so UI, launcher backend, and tests
    all share the same command construction logic.
    """
    cmd = []

    mem = preset.get('mem', '')
    if mem:
        try:
            m = int(mem)
            cmd += ['java', f'-Xmx{m}M', '-jar', jar_path]
        except ValueError:
            cmd += ['java', '-jar', jar_path]
    else:
        cmd += ['java', '-jar', jar_path]

    script = preset.get('script', 'P2P Master AI').strip()
    if script:
        cmd += ['-script', script]

    account = preset.get('account', '').strip()
    if account:
        cmd += ['-account', account]

    proxy = preset.get('proxy', '').strip()
    if proxy:
        cmd += ['-proxy', proxy]

    if preset.get('covert'):
        cmd.append('-covert')
    if preset.get('nofresh'):
        cmd.append('-nofresh')
    if preset.get('fresh'):
        cmd.append('-fresh')
    if preset.get('menu_manipulation'):
        cmd.append('-menuManipulation')
    if preset.get('no_click_walk'):
        cmd.append('-noClickWalk')

    world = preset.get('world', '').strip()
    if world:
        cmd += ['-world', world]

    custom = preset.get('custom', '').strip()
    if custom:
        try:
            cmd += shlex.split(custom)
        except ValueError:
            cmd.append(custom)

    params = preset.get('params', '').strip()
    if params:
        cmd += ['-params', params]

    return cmd


# ── Process discovery ──────────────────────────────────────────────────────────

def discover_account_process(account: str) -> Optional[dict]:
    """
    Locate a running DreamBot client window for 'account' and resolve its PID.

    Returns:
        dict with keys 'window_id' and 'pid' — exactly one window found
        None                                  — no window found

    Raises:
        ValueError — multiple windows matched (ambiguous; caller must refuse)

    Delegates to platform_ops.find_account_window_and_pid which raises ValueError
    on ambiguity. This module propagates that exception upward.
    """
    from py.platform_ops import find_account_window_and_pid
    return find_account_window_and_pid(account)  # None or dict; raises ValueError if ambiguous


def validate_account_pid(account: str, pid: int) -> bool:
    """
    Validate that 'pid' belongs to the DreamBot client for 'account'.

    Strict contract — returns True ONLY when:
        1. The PID is still alive, AND
        2. A DreamBot window for 'account' is found, AND
        3. The window-resolved PID equals 'pid'.

    Returns False in all other cases:
        - PID is dead
        - No matching DreamBot window found for 'account'
        - Multiple windows matched (ambiguous)
        - Window-resolved PID does not equal 'pid'

    "PID is alive" alone is not sufficient — it does not prove that the process
    belongs to this account. No command-line fallback is applied in beta.1.
    """
    from py.platform_ops import is_pid_running
    if not is_pid_running(pid):
        return False
    try:
        info = discover_account_process(account)
    except ValueError:
        return False  # multiple windows — ambiguous, refuse
    if not info:
        return False  # no matching window found; cannot confirm ownership
    return info.get('pid') == pid


def _discover_with_timeout(account: str, timeout: int = 30, poll: int = 2) -> Optional[dict]:
    """
    Poll for the real DreamBot client window PID, with a timeout.
    Returns the dict from discover_account_process, or None if not found in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = discover_account_process(account)
            if result and result.get('pid'):
                return result
        except ValueError:
            pass  # ambiguous — keep polling
        time.sleep(poll)
    return None


# ── Low-level launch ───────────────────────────────────────────────────────────

def _popen(cmd: list) -> int:
    """
    Spawn a DreamBot launcher subprocess. Returns the immediate Popen PID.
    Suppresses console windows on Windows.
    """
    kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


def _validate_jar(cfg: dict) -> Optional[str]:
    """
    Return the jar path from config if it exists, else None.
    Used for early validation before spawning anything.
    """
    jar = cfg.get('launcher_jar', '').strip()
    if not jar or not os.path.isfile(jar):
        return None
    return jar


# ── Background PID discovery and cache ────────────────────────────────────────

# ── Window-position restore (relaunch only — see relaunch_account) ─────────────

# DreamBot's own client cannot render smaller than this — confirmed real
# floor, not a guess. A captured size below this is known-impossible data
# (e.g. a bad read mid-minimize), not a legitimately small real window —
# see _size_is_plausible().
DREAMBOT_MIN_W = 765
DREAMBOT_MIN_H = 503


def _geometry_is_sane(geom) -> bool:
    """
    Conservative sanity check for a captured (x, y, w, h) tuple before ever
    trying to restore it. Deliberately simple rather than precisely
    multi-monitor-aware (e.g. exact virtual-desktop bounds) — the goal is
    only to catch clearly-broken values (a minimized window, a stale/bogus
    read, etc.), not to perfectly validate against every real monitor
    layout. When in doubt this returns False, and the caller's contract
    is to skip the restore and log why rather than guess a "clamped"
    position — see relaunch_account's docstring.
    """
    if not geom or len(geom) != 4:
        return False
    x, y, w, h = geom
    if w <= 0 or h <= 0 or w > 10000 or h > 10000:
        return False
    if x < -500 or y < -500 or x > 10000 or y > 10000:
        return False
    return True


def _size_is_plausible(geom) -> bool:
    """
    True if the captured size could plausibly be a real DreamBot window.
    DreamBot cannot render below DREAMBOT_MIN_W x DREAMBOT_MIN_H, so
    anything smaller is known-bad data, not a legitimately small window —
    e.g. a geometry read taken mid-minimize/mid-animation. This is checked
    separately from _geometry_is_sane(): a geometry can pass the generic
    sanity check (not wildly out of range) while still being below this
    real, known floor. Only gates whether to trust the *size* — the
    *position* from the same read is restored regardless (the user's
    window was exactly there; we don't second-guess where they put it).
    """
    if not geom or len(geom) != 4:
        return False
    _, _, w, h = geom
    return w >= DREAMBOT_MIN_W and h >= DREAMBOT_MIN_H


def _discover_and_cache(account: str, immediate_pid: int, log_fn=None, cfg: dict = None,
                        restore_geometry: Optional[tuple] = None) -> None:
    """
    Run in a daemon thread after Popen. Polls for the real DreamBot client PID
    via window title, caches it in launcher_state.json.

    If the window is not found within 30 seconds, caches the immediate Popen PID
    as a best-effort fallback, and logs a warning.

    restore_geometry: optional (x, y, w, h) captured from the previous
    client window before it was closed (relaunch_account only — fresh
    launches via launch_account have nothing to restore from, so they
    never pass this). If the new window is found in time and the geometry
    passes _geometry_is_sane(), best-effort move it back into place.
    Resize is applied too unless the captured size fails
    _size_is_plausible() (below DreamBot's real minimum render size) —
    in that case the position is still restored exactly as captured
    (never clamped or guessed), just without reapplying a size known to
    be bad. Any failure here is logged and swallowed — a missed position
    restore is never worth crashing the launch over.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    result = _discover_with_timeout(account, timeout=30, poll=2)
    if result:
        real_pid = result['pid']
        _set_pid(account, real_pid)
        write_debug_entry('launcher', {'account': account,
                           'msg': f'Client PID confirmed: {real_pid}'})
        if cfg and cfg.get('debug', False):
            _log(f'✅ [{account}] Client PID confirmed: {real_pid}')

        if restore_geometry is not None:
            new_wid = result.get('window_id')
            if not new_wid:
                _log(f'⚠️ [{account}] New window found but no window_id to restore position to — skipping.')
            elif not _geometry_is_sane(restore_geometry):
                _log(f'⚠️ [{account}] Saved window position looked invalid '
                     f'({restore_geometry}) — skipping restore rather than guessing.')
            else:
                resize_ok = _size_is_plausible(restore_geometry)
                try:
                    from py.platform_ops import set_window_geometry
                    x, y, w, h = restore_geometry
                    ok = set_window_geometry(new_wid, x, y, w, h, resize=resize_ok)
                    if ok:
                        action = 'Restored window position' if resize_ok else \
                                 'Restored window position only — captured size was below DreamBot\'s minimum, not reapplied'
                        write_debug_entry('launcher', {'account': account,
                                           'msg': f'{action} {restore_geometry}'})
                        if cfg and cfg.get('debug', False):
                            _log(f'↩️ [{account}] {action}.')
                        if not resize_ok:
                            _log(f'⚠️ [{account}] Captured size {restore_geometry[2]}x{restore_geometry[3]} '
                                 f'is below DreamBot\'s minimum ({DREAMBOT_MIN_W}x{DREAMBOT_MIN_H}) — '
                                 f'position restored, size left as-is.')
                    else:
                        _log(f'⚠️ [{account}] Could not restore window position — '
                             f'client is open at its default position instead.')
                except Exception as exc:
                    _log(f'⚠️ [{account}] Window position restore failed: {exc} — '
                         f'client is open at its default position instead.')
    else:
        _set_pid(account, immediate_pid)
        _log(f'⚠️ [{account}] Client window not found within 30s — caching launcher PID {immediate_pid}.')
        if restore_geometry is not None:
            _log(f'⚠️ [{account}] Window position restore skipped — new client window never confirmed.')


# ── Public launcher API ────────────────────────────────────────────────────────

def launch_account(cfg: dict, account: str, log_fn=None) -> LaunchResult:
    """
    Fresh launch for 'account'.

    - Fails with action='skipped' if the account appears already running.
      (Preserves the UI and Discord safety behaviour: report it, do not close anything.)
    - Spawns the process and starts background PID discovery.
    - Returns immediately — PID confirmation happens in a daemon thread.

    Use relaunch_account() to explicitly close an existing client first.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    preset = find_preset(cfg, account)
    if not preset:
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'No launcher preset found for "{account}".')

    jar = _validate_jar(cfg)
    if not jar:
        jar_path = cfg.get('launcher_jar', '').strip()
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'Launcher.jar not found at: {jar_path!r}')

    # Safety: refuse if already running
    try:
        existing = discover_account_process(account)
    except ValueError as exc:
        return LaunchResult(ok=False, account=account, action='skipped',
                            message=f'Multiple windows matched "{account}" — {exc}. '
                                    f'Close duplicates and retry.')
    if existing:
        return LaunchResult(ok=False, account=account, action='skipped',
                            message=f'"{account}" is already running '
                                    f'(PID {existing.get("pid")}). '
                                    f'Close the existing client before launching again.')

    cmd = build_command(jar, preset)
    _log(f'🚀 [{account}] Launching: {" ".join(shlex.quote(c) for c in cmd)}')
    try:
        immediate_pid = _popen(cmd)
    except Exception as exc:
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'Launch failed: {exc}')

    _log(f'✅ [{account}] Launcher process started (PID {immediate_pid}). '
         f'Waiting for client window...')

    threading.Thread(
        target=_discover_and_cache,
        args=(account, immediate_pid, log_fn, cfg),
        daemon=True,
    ).start()

    return LaunchResult(ok=True, account=account, action='launched',
                        message=f'Launched {account}. Client PID will be confirmed within 30s.',
                        pid=immediate_pid)


def relaunch_account(cfg: dict, account: str, log_fn=None,
                     safe_delay_seconds: int = 10) -> LaunchResult:
    """
    Safely close the existing DreamBot client for 'account', wait, then relaunch.

    Validation chain (in order):
        1. Window title match → PID  (most trusted: proves ownership)
        2. Saved PID in state file   (only if no window match; refused as ambiguous)

    If no client is found running at all, falls back to a fresh launch.
    Never kills by generic process name.

    Window-position restore: if an existing window is found, its geometry
    (x, y, w, h) is captured via platform_ops.get_window_geometry() before
    closing anything. After the relaunch, once the new client window is
    confirmed (same background poll that already discovers/caches its
    PID — see _discover_and_cache), that geometry is best-effort restored
    via platform_ops.set_window_geometry(). Any failure at any step here
    (capture, close, relaunch, re-discovery, or restore) is logged and
    swallowed — losing the window's on-screen position is never worth
    failing the relaunch over.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    def _dbg_log(msg, category='launcher', extra=None):
        """Always write to debug.jsonl; mirror to Monitor only if debug checkbox on."""
        payload = {'account': account, 'msg': msg}
        if extra:
            payload.update(extra)
        write_debug_entry(category, payload)
        if cfg and cfg.get('debug', False):
            _log(msg)

    preset = find_preset(cfg, account)
    if not preset:
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'No launcher preset found for "{account}".')

    jar = _validate_jar(cfg)
    if not jar:
        jar_path = cfg.get('launcher_jar', '').strip()
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'Launcher.jar not found at: {jar_path!r}')

    # ── Step 1: discover running client ───────────────────────────────────────
    target_pid = None
    discovery_method = None
    captured_geometry = None  # (x, y, w, h) of the existing window, if found — see docstring

    try:
        window_result = discover_account_process(account)
    except ValueError as exc:
        return LaunchResult(ok=False, account=account, action='skipped',
                            message=f'Multiple windows matched "{account}" — {exc}. '
                                    f'Close duplicates and retry.')

    if window_result:
        pid = window_result.get('pid')
        if not pid:
            # Window is visible but we cannot resolve its PID — do not guess.
            return LaunchResult(
                ok=False, account=account, action='skipped',
                message=(
                    f'DreamBot window found for "{account}", but PID could not be '
                    f'resolved — refusing to relaunch safely. '
                    f'Close the client manually and retry.'
                ),
            )
        target_pid = pid
        discovery_method = 'window'

        # Best-effort geometry capture — never blocks or fails the relaunch.
        # A minimized window, multi-monitor edge case, or any platform_ops
        # failure just means captured_geometry stays None, and the restore
        # step later is skipped with a clear log line — see _discover_and_cache.
        try:
            existing_wid = window_result.get('window_id')
            if existing_wid:
                from py.platform_ops import get_window_geometry, is_window_minimized
                if is_window_minimized(existing_wid):
                    _dbg_log(f'ℹ️ [{account}] Existing window is minimized — '
                             f'skipping position capture (nothing meaningful to restore).')
                else:
                    captured_geometry = get_window_geometry(existing_wid)
                    if captured_geometry:
                        _dbg_log(f'📐 [{account}] Captured window position {captured_geometry} '
                                 f'before closing.', extra={'geometry': captured_geometry})
                    else:
                        _dbg_log(f'⚠️ [{account}] Could not read window geometry before closing — '
                                 f'position restore will be skipped after relaunch.')
        except Exception as exc:
            _dbg_log(f'⚠️ [{account}] Geometry capture failed: {exc} — '
                     f'position restore will be skipped after relaunch.')
    else:
        # Fallback: saved PID
        from py.platform_ops import is_pid_running
        saved_pid = _get_pid(account)
        if saved_pid and is_pid_running(saved_pid):
            # Ownership unconfirmed — window didn't match. Refuse.
            return LaunchResult(ok=False, account=account, action='skipped',
                                message=(
                                    f'A process with saved PID {saved_pid} is running, '
                                    f'but no DreamBot window matched "{account}". '
                                    f'Ownership is ambiguous — refusing to close. '
                                    f'Close the client manually and retry.'
                                ))

    # ── Step 2: if nothing running, fresh launch ───────────────────────────────
    if target_pid is None:
        _log(f'⚠️ [{account}] No running client found — launching fresh.')
        cmd = build_command(jar, preset)
        _log(f'🚀 [{account}] Launching: {" ".join(shlex.quote(c) for c in cmd)}')
        try:
            immediate_pid = _popen(cmd)
        except Exception as exc:
            return LaunchResult(ok=False, account=account, action='failed',
                                message=f'Launch failed: {exc}')
        threading.Thread(
            target=_discover_and_cache,
            args=(account, immediate_pid, log_fn, cfg),
            daemon=True,
        ).start()
        return LaunchResult(ok=True, account=account, action='launched',
                            message=f'No running client found — launched {account} fresh.',
                            pid=immediate_pid)

    # ── Step 3: safe close ─────────────────────────────────────────────────────
    _dbg_log(f'🔴 [{account}] Closing client (PID {target_pid}, via {discovery_method})...',
             extra={'target_pid': target_pid, 'discovery_method': discovery_method})
    _set_pid(account, None)  # clear stale state before close
    # Mark suppress window BEFORE terminating — watcher detects "Stopped P2P" shortly
    # after this and must not trigger an auto-restart loop.
    set_relaunch_suppress(account, duration_secs=300.0)
    try:
        from py.platform_ops import terminate_process_tree
        terminate_process_tree(target_pid, timeout=10)
    except Exception as exc:
        _log(f'⚠️ [{account}] terminate raised: {exc} — continuing with relaunch.')

    _dbg_log(f'⏳ [{account}] Waiting {safe_delay_seconds}s before relaunch...',
             extra={'safe_delay_seconds': safe_delay_seconds})
    time.sleep(safe_delay_seconds)

    # ── Step 4: relaunch ──────────────────────────────────────────────────────
    cmd = build_command(jar, preset)
    _dbg_log(f'🚀 [{account}] Relaunching: {" ".join(shlex.quote(c) for c in cmd)}')
    try:
        immediate_pid = _popen(cmd)
    except Exception as exc:
        return LaunchResult(ok=False, account=account, action='failed',
                            message=f'Relaunch failed after closing client: {exc}')

    threading.Thread(
        target=_discover_and_cache,
        args=(account, immediate_pid, log_fn, cfg),
        kwargs={'restore_geometry': captured_geometry},
        daemon=True,
    ).start()

    return LaunchResult(ok=True, account=account, action='relaunched',
                        message=f'Restarted {account} after closing existing client.',
                        pid=immediate_pid)


def smart_launch(cfg: dict, account: str, log_fn=None) -> LaunchResult:
    """
    Smart dispatch: if the account is already running → relaunch_account;
    if not → launch_account.

    Not used by Discord /launch or launch_all (both use launch_account so they
    skip accounts that are already open). Retained for direct caller use where
    relaunch-on-open is explicitly desired.
    """
    try:
        existing = discover_account_process(account)
    except ValueError as exc:
        return LaunchResult(ok=False, account=account, action='skipped',
                            message=f'Multiple windows matched "{account}" — {exc}. '
                                    f'Close duplicates and retry.')

    if existing:
        return relaunch_account(cfg, account, log_fn=log_fn)
    return launch_account(cfg, account, log_fn=log_fn)


def launch_all(cfg: dict, log_fn=None) -> list:
    """
    Launch every account that has a launcher preset, skipping any already running.

    - Staggers launches: 5 seconds between each account.
    - If one account fails or is already open, continues with the rest.
    - Returns list[LaunchResult].

    Uses launch_account (not smart_launch) — accounts that are already open
    are reported as 'skipped', not force-restarted.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    presets = list_presets(cfg)
    if not presets:
        return [LaunchResult(ok=False, account='(all)', action='failed',
                             message='No launcher presets configured.')]

    results = []
    for i, preset in enumerate(presets):
        account = preset.get('account', '').strip()
        if not account:
            continue
        if i > 0:
            _log('⏳ [launch_all] Stagger: waiting 5s before next account...')
            time.sleep(5)
        _log(f'🚀 [launch_all] Processing: {account}')
        results.append(launch_account(cfg, account, log_fn=log_fn))

    return results


def relaunch_all(cfg: dict, log_fn=None) -> list:
    """
    Relaunch (close-if-open then launch) every account that has a launcher preset.
    Uses smart_launch — running accounts are closed and relaunched; closed accounts
    are launched fresh. This is the destructive counterpart to launch_all.

    - Staggers launches: 5 seconds between each account.
    - Continues on individual failures.
    - Returns list[LaunchResult].
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    presets = list_presets(cfg)
    if not presets:
        return [LaunchResult(ok=False, account='(all)', action='failed',
                             message='No launcher presets configured.')]

    results = []
    for i, preset in enumerate(presets):
        account = preset.get('account', '').strip()
        if not account:
            continue
        if i > 0:
            _log('⏳ [relaunch_all] Stagger: waiting 5s before next account...')
            time.sleep(5)
        _log(f'🔄 [relaunch_all] Processing: {account}')
        results.append(smart_launch(cfg, account, log_fn=log_fn))

    return results
