"""
platform_ops.py — Platform abstraction layer for P2P Monitor

Owns all OS-specific operations. The rest of the codebase calls these
functions and never directly uses platform-specific tools.

Linux backend:  readlink /proc/*/fd/*, xdotool, xprop
Windows backend: psutil, ctypes Win32 APIs

Adding a new platform:
  1. Add an elif branch in each function
  2. Keep the interface identical — callers must not change
"""

import os
import time
import re
import subprocess
import sys
from pathlib import Path


# ── Open file detection ────────────────────────────────────────────────────────

# Cache for get_open_log_handles() — avoids expensive repeated scans.
# On Windows a full psutil scan is slow; caching limits it to once per TTL.
_HANDLE_CACHE_TTL  = 15   # seconds — worst-case delay detecting a closed client
_handle_cache_time = 0.0
_handle_cache_result = None

class HandleScanResult:
    """
    Result of a handle scan attempt.

    Attributes:
        paths    set[str]  — normalized absolute file paths currently open
        reliable bool      — True if the scan produced trustworthy results;
                             False means the scan could not inspect the system
                             reliably and offline conclusions must not be drawn
        reason   str       — debug-only explanation when reliable=False;
                             empty string when reliable=True
    """
    __slots__ = ('paths', 'reliable', 'reason')

    def __init__(self, paths, reliable, reason=''):
        self.paths    = paths
        self.reliable = reliable
        self.reason   = reason

    def __repr__(self):
        return (f'HandleScanResult(paths={len(self.paths)} entries, '
                f'reliable={self.reliable}, reason={self.reason!r})')


def get_open_log_handles():
    """
    Return a HandleScanResult describing currently open file handles.

    Contract:
      - Always returns HandleScanResult — never None, never raises
      - result.paths  is a set[str] of normalized absolute file paths
      - result.reliable signals whether the scan can be trusted for
        offline/stale-session decisions:
          True  = scan completed; empty paths means genuinely no handles
          False = scan could not inspect reliably; empty paths means unknown
      - Callers must check result.reliable before making offline decisions
      - All paths are normalized via normalize_path() at collection time

    Used by watcher for:
      - identifying which log file is actively being written
      - detecting when a DreamBot client has closed (stale session)
      - EOF-pinning closed sessions before saving offsets

    Returns: HandleScanResult
    """
    global _handle_cache_time, _handle_cache_result
    now = time.time()
    if _handle_cache_result is not None and (now - _handle_cache_time) < _HANDLE_CACHE_TTL:
        return _handle_cache_result
    try:
        if sys.platform.startswith('linux'):
            result = _get_open_log_handles_linux()
        elif sys.platform == 'win32':
            result = _get_open_log_handles_windows()
        else:
            result = _get_open_log_handles_linux()  # best effort on other Unix
    except Exception as e:
        result = HandleScanResult(set(), reliable=False,
                                  reason=f'unexpected error in handle scan: {e}')
    _handle_cache_time   = now
    _handle_cache_result = result
    return result


def _get_open_log_handles_linux():
    try:
        result = subprocess.run(
            'readlink /proc/*/fd/* 2>/dev/null',
            shell=True, capture_output=True, text=True, timeout=5
        )
        paths = {normalize_path(p) for p in result.stdout.splitlines() if p}
        # A successful subprocess run (even with empty output) is reliable —
        # empty output means no handles found, not that we failed to check.
        return HandleScanResult(paths, reliable=True)
    except Exception as e:
        # Subprocess failed — we could not inspect reliably
        return HandleScanResult(set(), reliable=False,
                                reason=f'readlink failed: {e}')


def _get_open_log_handles_windows():
    """
    Windows: returns reliable=False unconditionally.

    psutil open_files() on Windows queries NtQueryObject for every handle
    in the process. A running JVM (DreamBot) keeps hundreds of handles open,
    making this call block for minutes. Even filtering to java processes only
    did not help — the per-process open_files() call itself is the bottleneck.

    Returning reliable=False causes the watcher to:
      - use name-based log file selection (newest logfile-*.log)
      - skip forced offline transitions
      - skip EOF pinning on stop

    This is the correct tradeoff for Windows — fast and stable beats
    theoretically precise but practically blocking for minutes.
    """
    return HandleScanResult(set(), reliable=False,
                            reason='open_files() skipped on Windows — uses name-based fallback')


# ── Process detection ──────────────────────────────────────────────────────────

def is_account_process_running(account, jar_path=''):
    """
    Return True if a DreamBot process for the given account appears to be running.

    Checks for a java process with:
      - 'java' in the process name
      - '-jar' in the command line
      - account name in the command line

    Optionally also matches jar_path if provided.

    Used by the launcher to prevent duplicate client launches.

    Returns: bool
    """
    if sys.platform.startswith('linux') or sys.platform == 'win32':
        return _is_account_process_running_psutil(account, jar_path)
    else:
        return _is_account_process_running_psutil(account, jar_path)


def _is_account_process_running_psutil(account, jar_path=''):
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline') or []
                cmdline_str = ' '.join(cmdline)
                name = (proc.info.get('name') or '').lower()
                if ('java' in name and
                        '-jar' in cmdline_str and
                        account in cmdline_str):
                    if jar_path and jar_path not in cmdline_str:
                        continue
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass  # psutil unavailable — cannot detect duplicate launch
    return False


def _is_account_process_running_pgrep(account):
    try:
        result = subprocess.run(
            ['pgrep', '-f', account],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# ── Folder / path opening ──────────────────────────────────────────────────────

def open_path(path):
    """
    Open a file or folder in the OS default file manager / application.

    Linux:   xdg-open
    Windows: os.startfile()

    Returns: None. Fails silently if unavailable.
    """
    if sys.platform.startswith('linux'):
        _open_path_linux(path)
    elif sys.platform == 'win32':
        _open_path_windows(path)
    else:
        _open_path_linux(path)  # best effort


def _open_path_linux(path):
    try:
        subprocess.Popen(['xdg-open', str(path)])
    except Exception:
        pass


def _open_path_windows(path):
    try:
        os.startfile(str(path))
    except Exception:
        pass


# ── Window detection ───────────────────────────────────────────────────────────

def find_window_ids_by_name(name):
    """
    Return a list of window IDs matching the given name string.

    Contract:
      - Always returns list[str] — never None, never raises
      - Returns empty list if no windows found or platform not supported
      - Window ID format is platform-specific (X11 wid on Linux, HWND on Windows)

    Used by watcher for:
      - locating DreamBot client windows for force panel screenshots

    Returns: list[str] of window IDs
    """
    try:
        if sys.platform.startswith('linux'):
            return _find_window_ids_linux(name)
        elif sys.platform == 'win32':
            return _find_window_ids_windows(name)
        else:
            return _find_window_ids_linux(name)
    except Exception:
        return []


def _find_window_ids_linux(name):
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--name', name.lower()],
            capture_output=True, text=True, timeout=5
        )
        wids = result.stdout.strip().split()
        return wids if wids else []
    except Exception:
        return []


def _find_window_ids_windows(name):
    """
    Windows window lookup via Win32 EnumWindows + title matching.
    Returns HWNDs as strings (matching Linux xdotool wid format).
    Requires: ctypes (stdlib)
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    needle = (name or '').lower()
    if not needle:
        return []

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype  = ctypes.c_bool
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype  = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype  = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype  = ctypes.c_bool

    matches = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or '').lower()
            if needle in title:
                matches.append(str(int(hwnd)))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_enum_proc, 0)
    except Exception:
        return []
    return matches


# ── Window capture ─────────────────────────────────────────────────────────────

def capture_window_image(window_id, out_path):
    """
    Capture a screenshot of the given window to out_path.

    Contract:
      - Returns (ok: bool, err: str)
      - Never raises — always returns (False, msg) on failure
      - out_path parent directory will be created if it does not exist

    Linux:   ImageMagick `import -window <wid>`
    Windows: PIL.ImageGrab + Win32 GetWindowRect via ctypes (stdlib)
             Requires: Pillow (pip install pillow)
             Note: uses ctypes only — pywin32 is NOT required

    Returns: tuple[bool, str]
    """
    try:
        if sys.platform.startswith('linux'):
            return _capture_window_image_linux(window_id, out_path)
        elif sys.platform == 'win32':
            return _capture_window_image_windows(window_id, out_path)
        return False, 'window capture unsupported on this platform'
    except Exception as e:
        return False, str(e)


def _capture_window_image_linux(window_id, out_path):
    try:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ['import', '-window', str(window_id), str(p)],
            capture_output=True, timeout=15
        )
        if p.exists() and p.stat().st_size > 0:
            return True, ''
        err = (result.stderr or b'').decode(errors='replace').strip()
        return False, err or f'capture failed (rc={result.returncode})'
    except FileNotFoundError:
        return False, 'imagemagick not found — run: sudo apt-get install imagemagick'
    except Exception as e:
        return False, str(e)


def _capture_window_image_windows(window_id, out_path):
    """
    Windows screenshot via PIL.ImageGrab + Win32 GetWindowRect.
    Both Pillow (for ImageGrab) and ctypes (stdlib) are used.
    pywin32 is NOT required — all Win32 calls go through ctypes.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return False, 'Pillow not installed — run: pip install pillow'
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False, 'ctypes unavailable'

    try:
        hwnd = int(str(window_id), 0)
    except Exception:
        return False, f'invalid window id: {window_id!r}'

    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.IsWindow.argtypes     = [wintypes.HWND]
        user32.IsWindow.restype      = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype  = wintypes.BOOL

        if not user32.IsWindow(hwnd):
            return False, f'window {hwnd} does not exist'

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False, 'GetWindowRect failed'

        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        img  = ImageGrab.grab(bbox=bbox)

        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(p))

        if p.exists() and p.stat().st_size > 0:
            return True, ''
        return False, 'captured image is empty'
    except Exception as e:
        return False, str(e)



# ── Window control ────────────────────────────────────────────────────────────
#
# All window operations go through this layer.
# Linux backend uses xdotool / xprop via util.py helpers.
# Windows backend uses Win32 APIs via ctypes (stdlib).
#
# Interface contract:
#   - All functions accept wid as str (X11 wid on Linux, HWND as str on Windows)
#   - All functions return None or a value as documented — never raise
#   - click_at(x, y) performs the click only; callers own timing/sleep
#   - paint hide/show (paint_is_visible, crop comparison) is Linux-only for now;
#     on Windows those checks are skipped and capture proceeds unconditionally

def get_focused_window():
    """
    Return the window ID of the currently focused/foreground window.

    Returns: str wid, or None on failure
    """
    try:
        if sys.platform.startswith('linux'):
            return _get_focused_window_linux()
        elif sys.platform == 'win32':
            return _get_focused_window_windows()
        return None
    except Exception:
        return None


def _get_focused_window_linux():
    from py.util import xdotool, get_display_env
    wid = xdotool(['getactivewindow'], get_display_env())
    return wid if wid else None


def _get_focused_window_windows():
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return str(int(hwnd)) if hwnd else None
    except Exception:
        return None


def get_window_geometry(wid):
    """
    Return (x, y, width, height) for the given window.

    Returns: tuple[int,int,int,int], or None on failure
    """
    try:
        if sys.platform.startswith('linux'):
            return _get_window_geometry_linux(wid)
        elif sys.platform == 'win32':
            return _get_window_geometry_windows(wid)
        return None
    except Exception:
        return None


def _get_window_geometry_linux(wid):
    from py.util import xdotool, get_display_env
    try:
        out = xdotool(['getwindowgeometry', '--shell', str(wid)], get_display_env())
        d   = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        x, y = int(d.get('X', 0)), int(d.get('Y', 0))
        w, h = int(d.get('WIDTH', 0)), int(d.get('HEIGHT', 0))
        return (x, y, w, h) if w and h else None
    except Exception:
        return None


def _get_window_geometry_windows(wid):
    try:
        import ctypes
        from ctypes import wintypes
        hwnd  = int(str(wid), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype  = wintypes.BOOL
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        return (rect.left, rect.top, w, h) if w and h else None
    except Exception:
        return None


def is_window_minimized(wid):
    """
    Return True if the window is currently minimized/iconic.

    Returns: bool (False on failure — assume not minimized)
    """
    try:
        if sys.platform.startswith('linux'):
            return _is_window_minimized_linux(wid)
        elif sys.platform == 'win32':
            return _is_window_minimized_windows(wid)
        return False
    except Exception:
        return False


def _is_window_minimized_linux(wid):
    try:
        r = subprocess.run(
            ['xprop', '-id', str(wid), 'WM_STATE'],
            capture_output=True, text=True, timeout=3
        )
        return 'Iconic' in r.stdout
    except Exception:
        return False


def _is_window_minimized_windows(wid):
    try:
        import ctypes
        from ctypes import wintypes
        hwnd   = int(str(wid), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype  = wintypes.BOOL
        return bool(user32.IsIconic(hwnd))
    except Exception:
        return False


def restore_window(wid):
    """
    Restore (un-minimize) a window.

    Returns: None
    """
    try:
        if sys.platform.startswith('linux'):
            _restore_window_linux(wid)
        elif sys.platform == 'win32':
            _restore_window_windows(wid)
    except Exception:
        pass


def _restore_window_linux(wid):
    from py.util import xdotool, get_display_env
    xdotool(['windowmap', str(wid)], get_display_env())


def _restore_window_windows(wid):
    try:
        import ctypes
        from ctypes import wintypes
        SW_RESTORE = 9
        hwnd   = int(str(wid), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype  = wintypes.BOOL
        user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass


def raise_and_focus_window(wid):
    """
    Bring window to foreground and give it input focus.

    On Windows, SetForegroundWindow can be flaky due to focus-stealing
    protection. The implementation uses AttachThreadInput as a workaround
    when needed. Callers should not need to know this detail.

    Returns: None
    """
    try:
        if sys.platform.startswith('linux'):
            _raise_and_focus_window_linux(wid)
        elif sys.platform == 'win32':
            _raise_and_focus_window_windows(wid)
    except Exception:
        pass


def _raise_and_focus_window_linux(wid):
    from py.util import xdotool, get_display_env
    env = get_display_env()
    xdotool(['windowraise', str(wid)], env)
    xdotool(['windowfocus', '--sync', str(wid)], env)


def _raise_and_focus_window_windows(wid):
    try:
        import ctypes
        from ctypes import wintypes
        hwnd   = int(str(wid), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype  = wintypes.BOOL
        user32.BringWindowToTop.argtypes    = [wintypes.HWND]
        user32.BringWindowToTop.restype     = wintypes.BOOL
        # AttachThreadInput trick — improves reliability on Windows
        # when the calling thread does not own the foreground lock
        cur_thread    = ctypes.windll.kernel32.GetCurrentThreadId()
        target_thread = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if cur_thread != target_thread:
            ctypes.windll.user32.AttachThreadInput(cur_thread, target_thread, True)
            attached = True
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                ctypes.windll.user32.AttachThreadInput(cur_thread, target_thread, False)
    except Exception:
        pass


def minimize_window(wid):
    """
    Minimize a window.

    Returns: None
    """
    try:
        if sys.platform.startswith('linux'):
            _minimize_window_linux(wid)
        elif sys.platform == 'win32':
            _minimize_window_windows(wid)
    except Exception:
        pass


def _minimize_window_linux(wid):
    from py.util import xdotool, get_display_env
    xdotool(['windowminimize', str(wid)], get_display_env())


def _minimize_window_windows(wid):
    try:
        import ctypes
        from ctypes import wintypes
        SW_MINIMIZE = 6
        hwnd   = int(str(wid), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype  = wintypes.BOOL
        user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception:
        pass


def click_at(x, y):
    """
    Move the mouse cursor to absolute screen coordinates (x, y) and click.

    Callers own all timing — this function performs the click only,
    with no added sleeps.

    Returns: None
    """
    try:
        if sys.platform.startswith('linux'):
            _click_at_linux(x, y)
        elif sys.platform == 'win32':
            _click_at_windows(x, y)
    except Exception:
        pass


def _click_at_linux(x, y):
    from py.util import xdotool, get_display_env
    env = get_display_env()
    xdotool(['mousemove', '--', str(x), str(y)], env, timeout=2)
    xdotool(['click', '--clearmodifiers', '1'], env, timeout=2)


def _click_at_windows(x, y):
    try:
        import ctypes
        # MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN | LEFTUP
        MOUSEEVENTF_MOVE      = 0x0001
        MOUSEEVENTF_ABSOLUTE  = 0x8000
        MOUSEEVENTF_LEFTDOWN  = 0x0002
        MOUSEEVENTF_LEFTUP    = 0x0004
        # Normalize to 0–65535 range for MOUSEEVENTF_ABSOLUTE
        sm_cx = ctypes.windll.user32.GetSystemMetrics(0)  # screen width
        sm_cy = ctypes.windll.user32.GetSystemMetrics(1)  # screen height
        ax = int(x * 65535 / max(sm_cx, 1))
        ay = int(y * 65535 / max(sm_cy, 1))
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, ax, ay, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)
    except Exception:
        pass


def supports_paint_detection():
    """
    Return True if paint hide/show detection is supported on this platform.

    Linux:   ImageMagick compare + xdotool crop
    Windows: Pillow ImageGrab + ImageChops.difference (requires Pillow)

    Returns: bool
    """
    if sys.platform.startswith('linux'):
        return True
    if sys.platform == 'win32':
        try:
            from PIL import ImageGrab, ImageChops  # noqa: F401
            return True
        except ImportError:
            return False
    return False


# ── Path utilities ─────────────────────────────────────────────────────────────

def normalize_path(path):
    """
    Normalize a file path for cross-platform string comparison.
    Handles slash direction and case sensitivity differences between platforms.

    Returns: str
    """
    return os.path.normcase(os.path.normpath(str(path)))

