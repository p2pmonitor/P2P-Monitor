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
import subprocess
import sys
from pathlib import Path


from typing import NamedTuple, Optional, Tuple


class WindowBounds(NamedTuple):
    """Geometry result from _get_window_bounds().
    All values in physical screen pixels, consistent coordinate space.
    dwm_rect and winrect_rect store (left, top, width, height) for BitBlt.
    """
    sx:          int
    sy:          int
    w:           int
    h:           int
    method:      str                              # 'dwm' or 'winrect'
    dwm_rect:    Optional[Tuple[int,int,int,int]] # (left, top, w, h) or None
    winrect_rect:Optional[Tuple[int,int,int,int]] # (left, top, w, h) or None


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


def find_window_ids_by_name_ex(name):
    """Like find_window_ids_by_name, but returns (wids, reason). reason is
    '' on success; otherwise says WHY nothing came back (no match vs
    timeout vs enumeration failure) — see v2.2.1 changelog."""
    try:
        if sys.platform.startswith('linux'):
            return _find_window_ids_linux_ex(name)
        elif sys.platform == 'win32':
            wids = _find_window_ids_windows(name)
            return (wids, '') if wids else ([], 'no visible window title matched (EnumWindows)')
        return _find_window_ids_linux_ex(name)
    except Exception as e:
        return [], f'window lookup failed: {e}'


def find_windows_for_pid_ex(pid):
    """Like find_windows_for_pid, but returns (wids, reason) — '' reason on
    success, description of the empty result otherwise (v2.2.1)."""
    if not pid:
        return [], 'no cached PID'
    try:
        if sys.platform.startswith('linux'):
            try:
                result = subprocess.run(
                    ['xdotool', 'search', '--pid', str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                return [], f'PID window search timed out (X busy) for PID {pid}'
            wids = [w for w in result.stdout.strip().split() if w]
            if not wids:
                return [], f'no windows owned by PID {pid}'
            matches = []
            for wid in wids:
                try:
                    title = get_window_title(wid) or ''
                    if 'dreambot' in title.lower():
                        matches.append(wid)
                except Exception:
                    pass
            if matches:
                return matches, ''
            return [], f'PID {pid} owns {len(wids)} window(s) but none titled DreamBot'
        else:
            wids = find_windows_for_pid(pid)
            return (wids, '') if wids else ([], f'no visible DreamBot window owned by PID {pid}')
    except Exception as e:
        return [], f'PID window lookup failed: {e}'


def _ci_regex_pattern(text):
    """Build a case-insensitive POSIX extended regex from a literal string.
    xdotool's --name pattern is a case-SENSITIVE regex with no -i flag, so
    each letter becomes a [xX] class and regex metacharacters are escaped —
    an account named 'MyAccount' matches a title showing 'myaccount',
    'MYACCOUNT', or any mix, and names containing regex specials can't
    break or over-match the pattern."""
    out = []
    for ch in text:
        if ch.isalpha():
            out.append(f'[{ch.lower()}{ch.upper()}]')
        elif ch in r'\^$.[]|()*+?{}':
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)


def _find_window_ids_linux(name):
    """Find window IDs whose title contains 'name' (case-insensitive).

    Ownership guard (parity with the Windows matcher): each candidate's
    actual title is read back via `xdotool getwindowname` and must also
    contain 'dreambot' (case-insensitive) — so a terminal or editor whose
    title merely mentions the account name can never be matched (and
    therefore never terminated). If the title read-back itself fails for a
    candidate, the candidate is dropped: ownership could not be verified.
    """
    return _find_window_ids_linux_ex(name)[0]


def _find_window_ids_linux_ex(name):
    """Reason-carrying variant: returns (wids, reason). reason is '' when
    results were found; otherwise a precise failure description so callers
    can distinguish a genuinely absent window from a timed-out/failed
    lookup — previously both collapsed into an empty list (v2.2.1)."""
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--name', _ci_regex_pattern(name)],
            capture_output=True, text=True, timeout=10
        )
        wids = result.stdout.strip().split()
        if not wids:
            return [], 'no window title matched'
        verified = []
        readback_failed = 0
        needle = (name or '').lower()
        for wid in wids:
            try:
                r = subprocess.run(['xdotool', 'getwindowname', wid],
                                   capture_output=True, text=True, timeout=10)
                title = (r.stdout or '').strip().lower()
            except subprocess.TimeoutExpired:
                readback_failed += 1
                continue
            except Exception:
                readback_failed += 1
                continue
            if 'dreambot' in title and needle in title:
                verified.append(wid)
        if verified:
            return verified, ''
        if readback_failed:
            return [], (f'{len(wids)} candidate(s) found but title read-back '
                        f'failed/timed out for {readback_failed} (X busy?)')
        return [], f'{len(wids)} candidate(s) found but none verified as DreamBot+account'
    except subprocess.TimeoutExpired:
        return [], 'window search timed out (X busy)'
    except FileNotFoundError:
        return [], 'xdotool not installed'
    except Exception as e:
        return [], f'window search failed: {e}'


def list_dreambot_window_titles():
    """Diagnostic helper: return [(window_id, title), ...] for every visible
    window whose title contains 'dreambot' (case-insensitive). Used only for
    debug logging when relaunch ownership validation refuses — shows exactly
    what the matcher could see at that moment. Never raises."""
    out = []
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype  = ctypes.c_int
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.restype  = ctypes.c_int
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype  = ctypes.c_bool

            titles = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _lp):
                try:
                    if user32.IsWindowVisible(hwnd):
                        n = user32.GetWindowTextLengthW(hwnd)
                        if n > 0:
                            buf = ctypes.create_unicode_buffer(n + 1)
                            user32.GetWindowTextW(hwnd, buf, n + 1)
                            t = buf.value or ''
                            if 'dreambot' in t.lower():
                                titles.append((str(int(hwnd)), t))
                except Exception:
                    pass
                return True
            user32.EnumWindows(_enum, 0)
            out = titles
        else:
            r = subprocess.run(
                ['xdotool', 'search', '--name', _ci_regex_pattern('dreambot')],
                capture_output=True, text=True, timeout=5)
            for wid in r.stdout.strip().split():
                try:
                    t = subprocess.run(['xdotool', 'getwindowname', wid],
                                       capture_output=True, text=True, timeout=5)
                    out.append((wid, (t.stdout or '').strip()))
                except Exception:
                    continue
    except Exception:
        pass
    return out


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
            if needle in title and 'dreambot' in title:
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

def capture_window_image(window_id, out_path, debug_log=None):
    """
    Capture a screenshot of the given window to out_path.

    Contract:
      - Returns (ok: bool, err: str)
      - Never raises — always returns (False, msg) on failure
      - out_path parent directory will be created if it does not exist

    Linux:   ImageMagick `import -window <wid>`
    Windows: BitBlt from screen DC using DWM-first geometry (DwmGetWindowAttribute
             → GetWindowRect fallback → PrintWindow last resort)
             Requires: Pillow (pip install pillow)
             Note: uses ctypes only — pywin32 is NOT required

    Returns: tuple[bool, str]
    """
    try:
        if sys.platform.startswith('linux'):
            return _capture_window_image_linux(window_id, out_path)
        elif sys.platform == 'win32':
            return _capture_window_image_windows(window_id, out_path, debug_log=debug_log)
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


def _get_window_bounds(hwnd, debug_log=None):
    """
    Get window geometry in physical screen coordinates.

    Tries DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS) first — gives
    the true visible rendered frame, unaffected by DPI virtualization or
    invisible DWM border pixels.

    Falls back to GetWindowRect if DWM fails.

    Both use GetDC(NULL) coordinate space (screen/virtual-desktop origin)
    so BitBlt can use sx/sy directly without coordinate conversion.

    Args:
        hwnd:      window handle (int)
        debug_log: optional callable(msg) for debug output

    Returns:
        WindowBounds or None if all geometry calls failed
    """
    try:
        import ctypes
        from ctypes import wintypes

        # Set DPI awareness and save old context for restore.
        # DPI_AWARENESS_CONTEXT is a HANDLE (pointer-sized, 8 bytes on
        # x64) — argtypes/restype must say so explicitly, or ctypes'
        # default guess (c_int, 32-bit) can silently misread the actual
        # old-context value being saved here, before this function ever
        # gets a chance to restore it correctly afterward.
        try:
            _u32 = ctypes.WinDLL('user32', use_last_error=True)
            _u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            _u32.SetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
            old_ctx = _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            old_ctx = None

        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        MAX_W, MAX_H = 7680, 4320  # 8K upper bound sanity check

        dwm_rect    = None
        winrect     = None
        chosen_rect = None
        method      = None

        try:
            user32  = ctypes.WinDLL('user32',  use_last_error=True)
            dwmapi  = ctypes.WinDLL('dwmapi',  use_last_error=True)

            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype  = wintypes.BOOL

            dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_uint]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT

            # ── Try DWM extended frame bounds ────────────────────────────────
            dwm_r = wintypes.RECT()
            hr = dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(dwm_r), ctypes.sizeof(dwm_r))
            if hr == 0:
                dw = dwm_r.right  - dwm_r.left
                dh = dwm_r.bottom - dwm_r.top
                if 0 < dw <= MAX_W and 0 < dh <= MAX_H:
                    dwm_rect    = (dwm_r.left, dwm_r.top, dw, dh)
                    chosen_rect = dwm_rect
                    method      = 'dwm'
                    if debug_log:
                        debug_log(f'_get_window_bounds hwnd={hwnd}: '
                                  f'DWM bounds=({dwm_r.left},{dwm_r.top},{dw},{dh})')
                else:
                    if debug_log:
                        debug_log(f'_get_window_bounds hwnd={hwnd}: '
                                  f'DWM returned invalid size {dw}x{dh}, skipping')
            else:
                if debug_log:
                    debug_log(f'_get_window_bounds hwnd={hwnd}: '
                              f'DwmGetWindowAttribute failed HRESULT=0x{hr & 0xFFFFFFFF:08X}')

            # ── Try GetWindowRect if DWM failed ───────────────────────────────
            if chosen_rect is None:
                wr = wintypes.RECT()
                ok = user32.GetWindowRect(hwnd, ctypes.byref(wr))
                if ok:
                    ww = wr.right  - wr.left
                    wh = wr.bottom - wr.top
                    if 0 < ww <= MAX_W and 0 < wh <= MAX_H:
                        winrect     = (wr.left, wr.top, ww, wh)
                        chosen_rect = winrect
                        method      = 'winrect'
                        if debug_log:
                            debug_log(f'_get_window_bounds hwnd={hwnd}: '
                                      f'GetWindowRect=({wr.left},{wr.top},{ww},{wh})')
                    else:
                        if debug_log:
                            debug_log(f'_get_window_bounds hwnd={hwnd}: '
                                      f'GetWindowRect invalid size {ww}x{wh}')
                else:
                    err = ctypes.get_last_error()
                    if debug_log:
                        debug_log(f'_get_window_bounds hwnd={hwnd}: '
                                  f'GetWindowRect failed error={err}')
        finally:
            # Must run even if the calls above raise — must never leave the
            # thread stuck in PER_MONITOR_AWARE_V2 for every later Win32
            # call on it, just because one geometry read failed.
            if old_ctx is not None:
                try:
                    _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))
                except Exception:
                    pass

        if chosen_rect is None:
            return None

        sx, sy, w, h = chosen_rect
        return WindowBounds(
            sx=sx, sy=sy, w=w, h=h,
            method=method,
            dwm_rect=dwm_rect,
            winrect_rect=winrect)

    except Exception as e:
        if debug_log:
            debug_log(f'_get_window_bounds hwnd={hwnd}: unexpected error: {e}')
        return None


def _get_window_rect_for_restore(hwnd, debug_log=None):
    """
    GetWindowRect ONLY — deliberately never falls back to or mixes in DWM
    extended frame bounds, unlike _get_window_bounds() above.

    This exists specifically for launcher window-position restore, and
    only for that. SetWindowPos operates on the same "outer window rect"
    coordinate space that GetWindowRect reads — not the DWM extended-
    frame-bounds space, which is a visually-tighter measurement (DWM
    bounds exclude the invisible resize-border padding that GetWindowRect
    includes). These are genuinely different rects, not just different
    units of the same rect — capturing one and restoring via an API that
    operates on the other is wrong at *any* DPI, 100% included; it just
    happens to look exactly like a DPI-scaling bug once the invisible
    border's size (which itself scales with DPI) becomes proportionally
    significant. Confirmed directly against real captured values on a
    125%-scaled Windows machine: GetWindowRect and DWM bounds did not
    just disagree by a small/constant margin — the ratio between them
    matched the DPI scale factor, which is what made this look like a
    pure DPI-awareness bug rather than a coordinate-space mismatch.

    Runs in the identical PER_MONITOR_AWARE_V2 thread context that
    _set_window_geometry_windows uses for the SetWindowPos call this
    capture feeds into — capture and restore must agree on awareness
    context, or the same virtualization mismatch reappears one level up.

    Returns (x, y, w, h) or None on failure. Screenshot/click paths are
    untouched by this function entirely — they keep using
    _get_window_bounds() (DWM-first), which is the *correct* choice for
    them: BitBlt and click-target math want the visually-true rendered
    bounds, not the wider raw window rect.
    """
    try:
        import ctypes
        from ctypes import wintypes

        try:
            _u32 = ctypes.WinDLL('user32', use_last_error=True)
            _u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            _u32.SetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
            old_ctx = _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            old_ctx = None

        MAX_W, MAX_H = 7680, 4320  # 8K upper bound sanity check
        result = None

        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype  = wintypes.BOOL

            wr = wintypes.RECT()
            ok = user32.GetWindowRect(hwnd, ctypes.byref(wr))
            if ok:
                w = wr.right - wr.left
                h = wr.bottom - wr.top
                if 0 < w <= MAX_W and 0 < h <= MAX_H:
                    result = (wr.left, wr.top, w, h)
                    if debug_log:
                        debug_log(f'_get_window_rect_for_restore hwnd={hwnd}: '
                                  f'GetWindowRect=({wr.left},{wr.top},{w},{h})')
                else:
                    if debug_log:
                        debug_log(f'_get_window_rect_for_restore hwnd={hwnd}: '
                                  f'GetWindowRect invalid size {w}x{h}')
            else:
                err = ctypes.get_last_error()
                if debug_log:
                    debug_log(f'_get_window_rect_for_restore hwnd={hwnd}: '
                              f'GetWindowRect failed error={err}')
        finally:
            if old_ctx is not None:
                try:
                    _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))
                except Exception:
                    pass

        return result

    except Exception as e:
        if debug_log:
            debug_log(f'_get_window_rect_for_restore hwnd={hwnd}: unexpected error: {e}')
        return None


def _capture_window_image_windows(window_id, out_path, debug_log=None):
    """
    Windows screenshot using BitBlt from screen DC.

    Fallback chain:
      1. DWM extended frame bounds + GetDC(NULL) + BitBlt  (primary)
      2. GetWindowRect bounds     + GetDC(NULL) + BitBlt  (secondary)
      3. PrintWindow                                        (last resort)

    Each method keeps coordinate space consistent:
      GetDC(NULL) uses screen/virtual-desktop coordinates → use bounds.sx/sy directly.
      PrintWindow renders into its own DC → uses 0,0 coordinates.

    debug_log: optional callable(msg) — only passed when Debug Mode is enabled.
    """
    try:
        from PIL import Image
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
        gdi32  = ctypes.WinDLL('gdi32',  use_last_error=True)

        # ── Set up argtypes/restype for all GDI calls ─────────────────────────
        # Critical on 64-bit Windows: handles are pointer-sized (8 bytes).
        # Without restype, ctypes defaults to c_int (4 bytes) and truncates.
        user32.IsWindow.argtypes      = [wintypes.HWND]
        user32.IsWindow.restype       = wintypes.BOOL
        user32.IsIconic.argtypes      = [wintypes.HWND]
        user32.IsIconic.restype       = wintypes.BOOL
        user32.ShowWindow.argtypes    = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype     = wintypes.BOOL
        user32.GetDC.argtypes         = [wintypes.HWND]
        user32.GetDC.restype          = wintypes.HDC
        user32.ReleaseDC.argtypes     = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype      = ctypes.c_int
        user32.PrintWindow.argtypes   = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype    = wintypes.BOOL

        gdi32.CreateCompatibleDC.argtypes    = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype     = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype  = wintypes.HBITMAP
        gdi32.SelectObject.argtypes           = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype            = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
            wintypes.HDC, ctypes.c_int, ctypes.c_int,
            ctypes.c_ulong]
        gdi32.BitBlt.restype  = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HBITMAP,
            ctypes.c_uint, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        gdi32.GetDIBits.restype  = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype  = wintypes.BOOL
        gdi32.DeleteDC.argtypes     = [wintypes.HDC]
        gdi32.DeleteDC.restype      = wintypes.BOOL

        if not user32.IsWindow(hwnd):
            return False, f'window {hwnd} does not exist'

        # Restore if minimized
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            import time as _t; _t.sleep(0.3)

        # ── Get window bounds ─────────────────────────────────────────────────
        bounds = _get_window_bounds(hwnd, debug_log=debug_log)
        if bounds is None:
            return False, 'could not determine window geometry'

        if debug_log:
            debug_log(f'capture hwnd={hwnd} method={bounds.method} '
                      f'sx={bounds.sx} sy={bounds.sy} w={bounds.w} h={bounds.h}')

        def _do_bitblt(sx, sy, w, h, label):
            """BitBlt from screen DC at screen coordinates sx,sy."""
            hdc_screen = user32.GetDC(None)
            hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp       = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
            old_obj    = gdi32.SelectObject(hdc_mem, hbmp)
            try:
                SRCCOPY = 0x00CC0020
                ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h,
                                  hdc_screen, sx, sy, SRCCOPY)
                if not ok:
                    err = ctypes.get_last_error()
                    if debug_log:
                        debug_log(f'BitBlt({label}) failed error={err}')
                    return None

                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ('biSize',          ctypes.c_uint32),
                        ('biWidth',         ctypes.c_int32),
                        ('biHeight',        ctypes.c_int32),
                        ('biPlanes',        ctypes.c_uint16),
                        ('biBitCount',      ctypes.c_uint16),
                        ('biCompression',   ctypes.c_uint32),
                        ('biSizeImage',     ctypes.c_uint32),
                        ('biXPelsPerMeter', ctypes.c_int32),
                        ('biYPelsPerMeter', ctypes.c_int32),
                        ('biClrUsed',       ctypes.c_uint32),
                        ('biClrImportant',  ctypes.c_uint32),
                    ]

                bmi           = BITMAPINFOHEADER()
                bmi.biSize    = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.biWidth   =  w
                bmi.biHeight  = -h
                bmi.biPlanes  = 1
                bmi.biBitCount    = 32
                bmi.biCompression = 0

                buf = ctypes.create_string_buffer(w * h * 4)
                gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf,
                                ctypes.byref(bmi), 0)
                return Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
            finally:
                gdi32.SelectObject(hdc_mem, old_obj)
                user32.ReleaseDC(None, hdc_screen)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)

        # ── Primary: BitBlt with chosen bounds ────────────────────────────────
        img = _do_bitblt(bounds.sx, bounds.sy, bounds.w, bounds.h, bounds.method)

        # ── Secondary: retry with alternate rect if primary failed ────────────
        if img is None and bounds.method == 'dwm' and bounds.winrect_rect:
            if debug_log:
                debug_log('BitBlt DWM failed — retrying with GetWindowRect')
            sx, sy, w, h = bounds.winrect_rect
            img = _do_bitblt(sx, sy, w, h, 'winrect-fallback')

        # ── Last resort: PrintWindow ──────────────────────────────────────────
        if img is None:
            if debug_log:
                debug_log('BitBlt failed — trying PrintWindow fallback')
            w, h = bounds.w, bounds.h
            hdc_screen = user32.GetDC(None)
            hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp       = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
            old_obj    = gdi32.SelectObject(hdc_mem, hbmp)
            try:
                user32.PrintWindow(hwnd, hdc_mem, 3)
                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ('biSize',ctypes.c_uint32),('biWidth',ctypes.c_int32),
                        ('biHeight',ctypes.c_int32),('biPlanes',ctypes.c_uint16),
                        ('biBitCount',ctypes.c_uint16),('biCompression',ctypes.c_uint32),
                        ('biSizeImage',ctypes.c_uint32),('biXPelsPerMeter',ctypes.c_int32),
                        ('biYPelsPerMeter',ctypes.c_int32),('biClrUsed',ctypes.c_uint32),
                        ('biClrImportant',ctypes.c_uint32),]
                bmi = BITMAPINFOHEADER()
                bmi.biSize=ctypes.sizeof(BITMAPINFOHEADER)
                bmi.biWidth=w; bmi.biHeight=-h
                bmi.biPlanes=1; bmi.biBitCount=32; bmi.biCompression=0
                buf = ctypes.create_string_buffer(w * h * 4)
                gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
                img = Image.frombuffer('RGBA',(w,h),buf,'raw','BGRA',0,1).convert('RGB')
                if debug_log:
                    debug_log('PrintWindow fallback succeeded')
            finally:
                gdi32.SelectObject(hdc_mem, old_obj)
                user32.ReleaseDC(None, hdc_screen)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)

        if img is None:
            return False, 'all capture methods failed'

        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(p))

        if p.exists() and p.stat().st_size > 0:
            return True, ''
        return False, 'captured image is empty'

    except Exception as e:
        return False, str(e)


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


def get_window_geometry(wid, debug_log=None):
    """
    Return (x, y, width, height) for the given window.

    Returns: tuple[int,int,int,int], or None on failure
    """
    try:
        if sys.platform.startswith('linux'):
            return _get_window_geometry_linux(wid)
        elif sys.platform == 'win32':
            return _get_window_geometry_windows(wid, debug_log=debug_log)
        return None
    except Exception:
        return None


def get_window_geometry_for_restore(wid, debug_log=None):
    """
    Return (x, y, width, height) for the given window, in the specific
    coordinate space that set_window_geometry()/SetWindowPos expects.

    Use this ONLY for launcher window-position-restore capture (the
    rect saved before closing a client, to be reapplied to the relaunched
    one). Do NOT use this for screenshot cropping or paint-button click
    targeting — those want get_window_geometry()'s DWM-first behavior,
    which gives the visually-true rendered bounds rather than the wider
    raw window rect this function deliberately returns instead.

    On Windows this calls GetWindowRect directly, never DWM extended
    frame bounds — those are a different, visually-tighter rect (DWM
    bounds exclude the invisible resize-border padding GetWindowRect
    includes), and SetWindowPos operates in GetWindowRect's space, not
    DWM's. Capturing via DWM bounds and restoring via SetWindowPos was a
    real coordinate-space mismatch that looked exactly like a DPI-scaling
    bug — confirmed directly against real captured values on a 125%-
    scaled Windows machine, where the ratio between the two measurements
    matched the DPI scale factor almost exactly.

    On Linux there's no DWM-vs-window-rect distinction to begin with
    (xdotool's getwindowgeometry is the only geometry source), so this is
    just an alias for get_window_geometry() there.

    Returns: tuple[int,int,int,int], or None on failure
    """
    try:
        if sys.platform.startswith('linux'):
            return _get_window_geometry_linux(wid)
        elif sys.platform == 'win32':
            try:
                hwnd = int(str(wid), 0)
            except Exception:
                return None
            return _get_window_rect_for_restore(hwnd, debug_log=debug_log)
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


def _get_window_geometry_windows(wid, debug_log=None):
    """Uses _get_window_bounds for DWM-first geometry.
    Same coordinate space as capture_window_image — paint/force clicks align correctly.
    """
    try:
        hwnd   = int(str(wid), 0)
        bounds = _get_window_bounds(hwnd, debug_log=debug_log)
        if bounds is None:
            return None
        return (bounds.sx, bounds.sy, bounds.w, bounds.h)
    except Exception:
        return None


def set_window_geometry(wid, x, y, w, h, resize=True, debug_log=None):
    """
    Move/resize the given window to (x, y, w, h).

    Used by launcher.py's relaunch window-position-restore — best-effort
    only. Callers should already have sanity-checked the geometry; this
    function does not validate bounds itself, it just applies whatever
    it's given.

    resize: when False, only moves the window — the size is left as-is.
    Used when the caller has a position it trusts but a w/h it doesn't.

    debug_log: optional callable(msg) — Windows only, see
    _set_window_geometry_windows's docstring for exactly what gets logged.

    Returns: bool — True if the underlying command/API call ran without
    raising. This does NOT independently re-verify the window actually
    moved (window managers can refuse/clamp); callers that care should
    treat this as "we asked," not "we confirmed."
    """
    try:
        if sys.platform.startswith('linux'):
            return _set_window_geometry_linux(wid, x, y, w, h, resize=resize)
        elif sys.platform == 'win32':
            return _set_window_geometry_windows(wid, x, y, w, h, resize=resize, debug_log=debug_log)
        return False
    except Exception:
        return False


def _set_window_geometry_linux(wid, x, y, w, h, resize=True):
    from py.util import xdotool, get_display_env
    env = get_display_env()
    try:
        xdotool(['windowmove', '--sync', str(wid), str(int(x)), str(int(y))], env, timeout=3)
        if resize:
            xdotool(['windowsize', '--sync', str(wid), str(int(w)), str(int(h))], env, timeout=3)
        return True
    except Exception:
        return False


def _set_window_geometry_windows(wid, x, y, w, h, resize=True, debug_log=None):
    """Same coordinate space as _get_window_geometry_windows (DWM extended
    frame bounds), applied directly to SetWindowPos. SetWindowPos itself
    operates on the raw window rect, not the DWM-adjusted one, so the
    restored position can be off by the (small, invisible) DWM shadow
    inset — acceptable for "put the window roughly back where it was,"
    the same tolerance capture_window_image already accepts for paint
    detection on this same geometry source.

    DPI awareness: this thread must be PER_MONITOR_AWARE_V2 for the
    duration of the SetWindowPos call, exactly like the read side
    (_get_window_bounds) and every other Win32 geometry call in this
    module (_click_at_windows, etc). Without it, the OS DPI-virtualizes
    the call — coordinates captured in physical-pixel space (DWM-aware)
    get silently rescaled against the process's actual (unaware) DPI
    context, which is what caused the client to resize incorrectly on
    restore on scaled displays. The process itself is never declared
    DPI-aware anywhere in this app, so this per-thread context is the
    only thing that made the read side correct — the write side needs
    the identical treatment.

    resize: when False, only moves the window (SWP_NOSIZE) — used when
    the caller has decided the captured w/h isn't trustworthy enough to
    reapply, but the position still is.

    debug_log: optional callable(msg) — if given, logs the thread DPI
    context immediately before and after the SetWindowPos call (not just
    trusting that the set succeeded — GetThreadDpiAwarenessContext is
    queried fresh both times), and the exact rect requested. Combined
    with a fresh capture of the window immediately after (the caller's
    job, since this function only knows what it asked for, not what it
    actually got), this is what lets a real before/after comparison be
    made on a real scaled-DPI Windows machine.
    """
    try:
        import ctypes
        from ctypes import wintypes

        old_ctx = None
        u32 = None
        try:
            u32 = ctypes.WinDLL('user32', use_last_error=True)
            # DPI_AWARENESS_CONTEXT is pointer-sized (HANDLE) — explicit
            # argtypes/restype required, or ctypes' default 32-bit c_int
            # guess can silently misread the value on 64-bit Windows.
            u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
            old_ctx = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if debug_log:
                try:
                    from py.platform_ops import get_dpi_diagnostics
                    diag = get_dpi_diagnostics(wid)
                    debug_log(f'_set_window_geometry_windows wid={wid}: '
                              f'requesting rect=({x},{y},{w},{h}) resize={resize} '
                              f'pre-restore dpi_diag={diag}')
                except Exception as diag_e:
                    debug_log(f'_set_window_geometry_windows wid={wid}: diag failed: {diag_e}')
        except Exception:
            pass

        try:
            hwnd = int(str(wid), 0)
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = wintypes.BOOL
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_NOSIZE = 0x0001
            flags = SWP_NOZORDER | SWP_NOACTIVATE | (SWP_NOSIZE if not resize else 0)
            ok = user32.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), flags)
            if debug_log:
                err = ctypes.get_last_error() if not ok else 0
                debug_log(f'_set_window_geometry_windows wid={wid}: '
                          f'SetWindowPos returned ok={bool(ok)} last_error={err}')
        finally:
            # Must run even if SetWindowPos (or anything above) raises —
            # leaving the thread stuck in PER_MONITOR_AWARE_V2 would affect
            # every later Win32 call made on this same thread, not just
            # this one.
            if old_ctx is not None and u32 is not None:
                try:
                    u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))
                    if debug_log:
                        try:
                            from py.platform_ops import get_dpi_diagnostics
                            debug_log(f'_set_window_geometry_windows wid={wid}: '
                                      f'post-restore dpi_diag={get_dpi_diagnostics(wid)}')
                        except Exception:
                            pass
                except Exception:
                    pass

        return bool(ok)
    except Exception:
        return False


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
    """
    Click at absolute screen coordinates on Windows using mouse_event.

    Java AWT/Swing does not process WM_LBUTTONDOWN/UP sent via PostMessage —
    Java intercepts raw input at a lower level. We must use mouse_event to
    simulate real hardware input that Java's event system will receive.

    Uses MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK for correct coordinate
    normalization across multiple monitors. Virtual desktop bounds are derived
    from EnumDisplayMonitors which returns physical pixel rects for every monitor,
    avoiding the GetSystemMetrics logical-pixel ambiguity on scaled displays.
    Falls back to GetSystemMetrics if EnumDisplayMonitors fails or returns nothing.
    """
    try:
        import ctypes
        from ctypes import wintypes

        old_ctx = None
        u32 = None
        try:
            u32 = ctypes.WinDLL('user32', use_last_error=True)
            # DPI_AWARENESS_CONTEXT is a HANDLE (pointer-sized, 8 bytes on
            # x64) — argtypes/restype must say so explicitly, or ctypes'
            # default guess (c_int, 32-bit) can silently misread the
            # actual old-context value being saved here. Same fix as
            # _get_window_bounds/_set_window_geometry_windows — this was
            # the last remaining untyped call of this specific function.
            u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
            old_ctx = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

        try:
            MOUSEEVENTF_MOVE        = 0x0001
            MOUSEEVENTF_LEFTDOWN    = 0x0002
            MOUSEEVENTF_LEFTUP      = 0x0004
            MOUSEEVENTF_ABSOLUTE    = 0x8000
            MOUSEEVENTF_VIRTUALDESK = 0x4000

            # ── Virtual desktop bounds via EnumDisplayMonitors (physical pixels) ──
            monitors = []
            vx = vy = vw = vh = None

            try:
                MonitorEnumProc = ctypes.WINFUNCTYPE(
                    ctypes.c_bool,
                    ctypes.c_ulong,
                    ctypes.c_ulong,
                    ctypes.POINTER(wintypes.RECT),
                    ctypes.c_double
                )

                def _monitor_cb(_hmon, _hdc, lprect, data):
                    r = lprect.contents
                    monitors.append((r.left, r.top, r.right, r.bottom))
                    return True

                cb = MonitorEnumProc(_monitor_cb)
                ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)

                if monitors:
                    vx = min(m[0] for m in monitors)
                    vy = min(m[1] for m in monitors)
                    vw = max(m[2] for m in monitors) - vx
                    vh = max(m[3] for m in monitors) - vy
            except Exception:
                pass

            # ── Fallback: GetSystemMetrics ────────────────────────────────────────
            if vx is None or vw is None or vw <= 0:
                vx = ctypes.windll.user32.GetSystemMetrics(76)
                vy = ctypes.windll.user32.GetSystemMetrics(77)
                vw = ctypes.windll.user32.GetSystemMetrics(78)
                vh = ctypes.windll.user32.GetSystemMetrics(79)
                if vw <= 0:
                    vw = ctypes.windll.user32.GetSystemMetrics(0)
                if vh <= 0:
                    vh = ctypes.windll.user32.GetSystemMetrics(1)

            # ── Normalize and inject ──────────────────────────────────────────────
            ax = int((x - vx) * 65535 / max(vw, 1))
            ay = int((y - vy) * 65535 / max(vh, 1))

            flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
            ctypes.windll.user32.SetCursorPos(x, y)
            ctypes.windll.user32.mouse_event(flags, ax, ay, 0, 0)
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                ax, ay, 0, 0)
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                ax, ay, 0, 0)
        finally:
            # Must run even if anything above raises — must never leave the
            # thread stuck in PER_MONITOR_AWARE_V2 for every later Win32
            # call on it just because one click attempt failed partway
            # through.
            if old_ctx is not None and u32 is not None:
                try:
                    u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))
                except Exception:
                    pass

    except Exception:
        pass


def get_dpi_diagnostics(window_id=None):
    """
    Snapshot of everything relevant to the "DreamBot client restored at the
    wrong DPI scale" investigation, for direct before/after comparison on a
    real scaled-DPI Windows machine — this function makes no assumptions
    about which value is "correct," it just reports what the OS actually
    says at this exact moment, including the *current thread's* DPI
    awareness context (the actual current state, not just whatever the
    last SetThreadDpiAwarenessContext call claimed to set — if that call's
    effect was ever silently lost for any reason, this is what would catch
    it). Linux: returns platform='linux' with everything else None, since
    none of this concept applies there.

    Returns a dict — never raises. Always JSON-serializable (for
    write_debug_entry).
    """
    if not sys.platform.startswith('win32') and sys.platform != 'win32':
        return {'platform': 'linux'}
    out = {'platform': 'win32', 'window_id': str(window_id) if window_id else None}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL('user32', use_last_error=True)

        # DPI_AWARENESS_CONTEXT is a HANDLE (pointer-sized — 8 bytes on
        # x64), not a plain int. Both of these specifically need
        # c_void_p, not the ctypes-guessed default of c_int (32-bit) —
        # this exact mismatch is the leading suspect for why the beta18
        # fix may not have fully worked: a truncated/misread context
        # value here would make every later DPI-awareness check or
        # restore unreliable in ways that wouldn't necessarily raise.
        user32.GetThreadDpiAwarenessContext.argtypes = []
        user32.GetThreadDpiAwarenessContext.restype  = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype  = ctypes.c_int
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.AreDpiAwarenessContextsEqual.restype  = wintypes.BOOL

        ctx = user32.GetThreadDpiAwarenessContext()
        out['thread_dpi_context_raw'] = ctx

        # Named DPI_AWARENESS_CONTEXT sentinel values, for identifying
        # which one `ctx` actually is via AreDpiAwarenessContextsEqual
        # (the context values themselves are opaque/version-dependent
        # pseudo-handles — comparing by equality is the documented way,
        # not comparing the raw integer to -1/-2/-3/-4/-5 directly).
        named = {
            'DPI_AWARENESS_CONTEXT_UNAWARE': -1,
            'DPI_AWARENESS_CONTEXT_SYSTEM_AWARE': -2,
            'DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE': -3,
            'DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2': -4,
            'DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED': -5,
        }
        out['thread_dpi_context_name'] = 'UNKNOWN'
        for name, val in named.items():
            try:
                if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(val)):
                    out['thread_dpi_context_name'] = name
                    break
            except Exception:
                pass

        try:
            out['thread_dpi_awareness_enum'] = user32.GetAwarenessFromDpiAwarenessContext(ctx)
        except Exception:
            out['thread_dpi_awareness_enum'] = None

        # Process-level awareness (set once at startup, if ever — this app
        # currently never calls SetProcessDpiAwareness/Context anywhere,
        # which this diagnostic should make directly visible).
        try:
            shcore = ctypes.WinDLL('shcore', use_last_error=True)
            shcore.GetProcessDpiAwareness.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
            shcore.GetProcessDpiAwareness.restype  = ctypes.c_long
            val = ctypes.c_int()
            hr = shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
            out['process_dpi_awareness'] = val.value if hr == 0 else None
        except Exception:
            out['process_dpi_awareness'] = None

        if window_id:
            try:
                hwnd = int(str(window_id), 0)
                user32.GetDpiForWindow.argtypes = [wintypes.HWND]
                user32.GetDpiForWindow.restype  = ctypes.c_uint
                dpi = user32.GetDpiForWindow(hwnd)
                out['window_dpi'] = dpi
                out['window_scale_factor'] = round(dpi / 96.0, 4) if dpi else None
            except Exception as e:
                out['window_dpi_error'] = str(e)

        # System-wide DPI (GetDpiForSystem), for comparison against the
        # per-window value above — they only differ on real multi-monitor
        # setups with mixed scaling.
        try:
            user32.GetDpiForSystem.argtypes = []
            user32.GetDpiForSystem.restype  = ctypes.c_uint
            out['system_dpi'] = user32.GetDpiForSystem()
        except Exception:
            out['system_dpi'] = None

        return out
    except Exception as e:
        out['error'] = str(e)
        return out


def get_window_dpi_scale(window_id):
    """
    Return the DPI scale factor for the monitor the window is on.

    Windows: uses GetDpiForWindow — returns actual DPI for the window's monitor,
             handles multi-monitor setups where each monitor has different scaling.
             96 DPI = 1.0 (100%), 120 DPI = 1.25 (125%), 144 DPI = 1.5 (150%) etc.
    Linux:   always returns 1.0 — X11 logical pixels match physical for our purposes.

    Used to scale hardcoded DreamBot UI offsets (button positions relative to
    window edge) which were measured at 100% DPI. Do NOT use this to scale window
    bounds — those are already physical pixels from DWM/GetWindowRect.

    Returns: float scale factor, always >= 1.0, defaults to 1.0 on any error.
    """
    if not sys.platform.startswith('win32') and sys.platform != 'win32':
        return 1.0
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(str(window_id), 0)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype  = ctypes.c_uint
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi <= 0:
            return 1.0
        return dpi / 96.0
    except Exception:
        return 1.0


# ── DreamBot paint UI offsets (measured at 100% DPI) ───────────────────────────
# These are scaled at point of use by get_window_dpi_scale().
# Centralized here so paint.py and screenshot.py share one source of truth.
PAINT_BTN_X_OFFSET = 100   # pixels from window left edge to Hide button
PAINT_BTN_Y_OFFSET = 50    # pixels from window bottom edge to Hide button
PAINT_BTN_CROP_W   = 60    # crop width for paint button visibility check
PAINT_BTN_CROP_H   = 20    # crop height for paint button visibility check


def get_window_title(window_id):
    """Return the window title string for the given window_id, or '' on failure.
    Used to verify a matched HWND actually belongs to the expected process before capture.
    Linux: xdotool getwindowname. Windows: GetWindowTextW.
    """
    try:
        if sys.platform.startswith('linux'):
            r = subprocess.run(
                ['xdotool', 'getwindowname', str(window_id)],
                capture_output=True, text=True, timeout=3)
            return r.stdout.strip() if r.returncode == 0 else ''
        elif sys.platform == 'win32':
            import ctypes
            hwnd   = int(str(window_id), 0)
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ''
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or ''
    except Exception:
        pass
    return ''


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


# ── Process / PID helpers (used by py/launcher.py) ────────────────────────────

def get_pid_for_window(window_id) -> 'Optional[int]':
    """
    Resolve a window ID (str or int) to the PID of the owning process.

    Linux:   xdotool getwindowpid <wid>
    Windows: GetWindowThreadProcessId via ctypes

    Returns int PID on success, None on failure.
    """
    try:
        if sys.platform.startswith('linux'):
            return _get_pid_for_window_linux(window_id)
        elif sys.platform == 'win32':
            return _get_pid_for_window_windows(window_id)
        else:
            return _get_pid_for_window_linux(window_id)
    except Exception:
        return None


def _get_pid_for_window_linux(window_id) -> 'Optional[int]':
    try:
        result = subprocess.run(
            ['xdotool', 'getwindowpid', str(window_id)],
            capture_output=True, text=True, timeout=10,
        )
        pid_str = result.stdout.strip()
        if pid_str and pid_str.isdigit():
            return int(pid_str)
        return None
    except Exception:
        return None


def _get_pid_for_window_windows(window_id) -> 'Optional[int]':
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(int(window_id), ctypes.byref(pid))
        return pid.value if pid.value else None
    except Exception:
        return None


def is_pid_running(pid: int) -> bool:
    """
    Return True if a process with the given PID is currently alive.

    Prefers psutil (most reliable, handles zombies correctly).
    Falls back to:
      Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION
      Linux/macOS: os.kill(pid, 0)
    """
    try:
        import psutil
        try:
            p = psutil.Process(pid)
            return p.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    except ImportError:
        pass

    if sys.platform == 'win32':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True   # process exists; we lack permission to signal it
        except Exception:
            return False


def find_windows_for_pid(pid: int) -> list:
    """
    Return visible window IDs owned by the given PID whose title contains 'DreamBot'.

    Linux:   xdotool search --pid <pid>, then filter by title via get_window_title()
    Windows: EnumWindows + GetWindowThreadProcessId, filter visible + title

    Returns list[str] of window IDs. Never raises. Empty list on any failure.
    """
    if not pid:
        return []
    try:
        if sys.platform.startswith('linux'):
            return _find_windows_for_pid_linux(pid)
        elif sys.platform == 'win32':
            return _find_windows_for_pid_windows(pid)
        else:
            return _find_windows_for_pid_linux(pid)
    except Exception:
        return []


def _find_windows_for_pid_linux(pid: int) -> list:
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--pid', str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        wids = [w for w in result.stdout.strip().split() if w]
        if not wids:
            return []
        # Filter: visible + title contains 'dreambot'
        matches = []
        for wid in wids:
            try:
                title = get_window_title(wid) or ''
                if 'dreambot' in title.lower():
                    matches.append(wid)
            except Exception:
                pass
        return matches
    except Exception:
        return []


def _find_windows_for_pid_windows(pid: int) -> list:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        ]
        user32.EnumWindows.restype           = ctypes.c_bool
        user32.IsWindowVisible.argtypes      = [wintypes.HWND]
        user32.IsWindowVisible.restype       = ctypes.c_bool
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype  = ctypes.c_int
        user32.GetWindowTextW.argtypes       = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype        = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        matches = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum_proc(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if win_pid.value != pid:
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = (buf.value or '').lower()
                if 'dreambot' in title:
                    matches.append(str(int(hwnd)))
            except Exception:
                pass
            return True

        user32.EnumWindows(_enum_proc, 0)
        return matches
    except Exception:
        return []


def terminate_process_tree(pid: int, timeout: int = 10) -> None:
    """
    Terminate a process and all its children by PID.

    Prefers psutil (handles full process tree).
    Falls back to:
      Windows: taskkill /PID <pid> /T /F
      Linux:   SIGTERM → wait → SIGKILL

    Caller is responsible for validating ownership before calling.
    This function never validates — it just kills.
    """
    try:
        import psutil
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            # Send SIGTERM to everyone first
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            try:
                parent.terminate()
            except psutil.NoSuchProcess:
                pass
            # Wait; force-kill survivors
            gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)
            for p in alive:
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
            return
        except psutil.NoSuchProcess:
            return  # already gone
    except ImportError:
        pass

    # psutil not available — platform fallback
    if sys.platform == 'win32':
        try:
            subprocess.run(
                ['taskkill', '/PID', str(pid), '/T', '/F'],
                capture_output=True, timeout=timeout,
            )
        except Exception:
            pass
    else:
        import signal as _signal
        try:
            os.kill(pid, _signal.SIGTERM)
            time.sleep(2)
            if is_pid_running(pid):
                os.kill(pid, _signal.SIGKILL)
        except Exception:
            pass


def find_account_window_and_pid(account: str) -> 'Optional[dict]':
    """
    Find the DreamBot client window for 'account' and resolve its PID.

    Returns:
        dict with keys 'window_id' (str) and 'pid' (int)  — exactly one match
        None                                               — no window found

    Raises:
        ValueError  — multiple windows matched; caller must refuse and explain

    Validation:
        Uses find_window_ids_by_name() (existing, tested, cross-platform).
        PID is resolved via get_pid_for_window().
        Only returns a result when exactly one window matches.
    """
    try:
        wids = find_window_ids_by_name(account)
    except Exception:
        return None

    if not wids:
        return None

    if len(wids) > 1:
        raise ValueError(
            f'{len(wids)} DreamBot windows matched "{account}" — ownership is ambiguous'
        )

    wid = wids[0]
    pid = get_pid_for_window(wid)
    if not pid:
        # Window found but can't get PID — still return window info, pid=None
        return {'window_id': wid, 'pid': None}

    return {'window_id': wid, 'pid': pid}


# ── Path utilities ─────────────────────────────────────────────────────────────

def normalize_path(path):
    """
    Normalize a file path for cross-platform string comparison.
    Handles slash direction and case sensitivity differences between platforms.

    Returns: str
    """
    return os.path.normcase(os.path.normpath(str(path)))

