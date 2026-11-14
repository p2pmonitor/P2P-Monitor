"""
paint.py — Detuks client window automation for P2P Monitor
Owns hide/show paint and all click-based interactions.
All coordinates are offsets from the bottom-left of the BotClient window.
"""

import sys
import time

from py.platform_ops import (find_window_ids_by_name, get_focused_window,
                             get_window_geometry, raise_and_focus_window, click_at,
                             is_window_minimized, restore_window,
                             get_window_dpi_scale)

# ── Click offsets from the bottom-left of the Detuks BotClient window ────────
# Measured on Detuks v1.3.17 window captures (import -window) at two window
# sizes: the P2P Master AI panel is pinned to the bottom-left corner and does
# not scale with the window, so fixed (x from left, y from bottom) offsets
# are valid at any window size. Values are button/cell centres.
#   Button row (y=17): wrench 32 · Hide/Show 94 · Stats 156 · Logs 218 ·
#                      -10m 280 · +10m 342 · Skip 404 · Pause 481
#   Skill grid: columns x=172/235/298/361/424/487, rows y=147/115/83/51
CLICK_OFFSETS = {
    # Time adjustments
    '-10m':         (280,  17),
    '+10m':         (342,  17),
    # Panel toggles (open → screenshot → close)
    'Stats':        (156,  17),
    'Logs':         (218,  17),
    # Single-click actions
    'Skip':         (404,  17),
    'Pause':        (481,  17),   # toggle — run again to resume
    'Quest':        (108,  50),   # quest-points box in the info panel
    # Skill grid — row 1
    'Attack':       (172, 147),
    'Mining':       (298, 147),
    'Prayer':       (361, 147),
    'Crafting':     (424, 147),
    'Firemaking':   (487, 147),
    # row 2
    'Strength':     (172, 115),
    'Agility':      (235, 115),
    'Smithing':     (298, 115),
    'Magic':        (361, 115),
    'Fletching':    (424, 115),
    'Woodcutting':  (487, 115),
    # row 3
    'Defence':      (172,  83),
    'Herblore':     (235,  83),
    'Fishing':      (298,  83),
    'Runecrafting': (361,  83),
    'Slayer':       (424,  83),
    'Farming':      (487,  83),
    # row 4
    'Range':        (172,  51),
    'Thieving':     (235,  51),
    'Cooking':      (298,  51),
    'Construction': (361,  51),
    'Hunter':       (424,  51),
    'Sailing':      (487,  51),
}

# Actions that are panel toggles (click open → screenshot → click close)
PANEL_ACTIONS = {'Stats', 'Logs'}

# Actions that need an amount parameter (clicked N times)
AMOUNT_ACTIONS = {'-10m', '+10m'}


def _get_client_click_pos(wid, offset_x, offset_y):
    """
    Compute absolute desktop click position for a client-relative offset.

    On Windows, uses ClientToScreen(hwnd, 0,0) as the anchor so clicks land
    in the actual client area — not the DWM extended frame which includes the
    invisible drop shadow and produces y-coords below the real window bottom.
    No DPI scaling applied: ClientToScreen returns logical desktop coords which
    is the same space SetCursorPos operates in.

    On Linux, falls back to DWM geometry (get_window_geometry) with DPI scaling
    since client area == frame on X11 and scaling works correctly there.

    Returns (click_x, click_y).
    """
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            hwnd   = int(str(wid), 0)
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            old_ctx = None
            try:
                old_ctx = user32.SetThreadDpiAwarenessContext(-4)
            except Exception:
                pass
            # Client area size
            cr = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(cr))
            client_h = cr.bottom
            # Client origin on desktop
            pt = wintypes.POINT(0, 0)
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            click_x = pt.x + offset_x
            click_y = pt.y + client_h - offset_y
            if old_ctx is not None:
                try:
                    user32.SetThreadDpiAwarenessContext(old_ctx)
                except Exception:
                    pass
            return click_x, click_y
        except Exception:
            pass  # fall through to DWM path on any failure

    # Linux / fallback: DWM geometry + DPI-scaled offsets
    geom = get_window_geometry(wid)
    if not geom:
        return None, None
    x, y, w, h = geom
    scale   = get_window_dpi_scale(wid)
    click_x = x + round(offset_x * scale)
    click_y = y + h - round(offset_y * scale)
    return click_x, click_y

# ── Window helpers ─────────────────────────────────────────────────────────────
def _find_window(account):
    """Find DreamBot window ID for account. Returns wid string or None."""
    wids = find_window_ids_by_name(account)
    return wids[0] if wids else None

def _click(x, y, env=None):
    """Move mouse to absolute screen coords and click. env param kept for compat."""
    click_at(x, y)

# ── Force: single skill/action click ──────────────────────────────────────────
def do_force_skill(account, action, log=None, window_lock=None):
    """
    Click a skill or action button once for the account.
    Focuses the DreamBot window, clicks once, then restores focus.
    window_lock: shared threading.Lock() to serialize with screenshot ops.
    """
    offsets = CLICK_OFFSETS.get(action)
    if not offsets:
        if log:
            log(f"  ⚠ [{account}] Unknown action: {action}")
        return
    offset_x, offset_y = offsets
    wid = _find_window(account)
    if not wid:
        if log:
            log(f"  ⚠ [{account}] No window found")
        return

    if log:
        log(f"🎯 [{account}] Forcing {action}")

    from contextlib import nullcontext
    ctx = window_lock if window_lock else nullcontext()
    with ctx:
        restore_wid = get_focused_window()
        try:
            was_minimized = is_window_minimized(wid)
            if was_minimized:
                restore_window(wid)
                time.sleep(0.5)
            raise_and_focus_window(wid)
            time.sleep(0.4 if was_minimized else 0.3)

            geom = get_window_geometry(wid)
            if not geom:
                if log:
                    log(f"  ⚠ [{account}] Could not get window geometry")
                return
            click_x, click_y = _get_client_click_pos(wid, offset_x, offset_y)
            if click_x is None:
                if log:
                    log(f"  ⚠ [{account}] Could not compute click position")
                return
            _click(click_x, click_y)
        finally:
            if restore_wid and restore_wid != wid:
                raise_and_focus_window(restore_wid)

    if log:
        log(f"✅ [{account}] {action} forced")


# ── Force: panel toggle (open → screenshot → close) ───────────────────────────
def do_force_panel(account, action, screenshot_cb, log=None, window_lock=None):
    """
    Open a panel (Stats/Logs) by clicking once, take a full-window screenshot,
    then immediately click again to close it.
    screenshot_cb: callable() — caller handles capture + post while panel is open.
    window_lock: shared threading.Lock() to serialize with screenshot ops.
    """
    offsets = CLICK_OFFSETS.get(action)
    if not offsets:
        if log:
            log(f"  ⚠ [{account}] Unknown panel action: {action}")
        return
    offset_x, offset_y = offsets
    wid = _find_window(account)
    if not wid:
        if log:
            log(f"  ⚠ [{account}] No window found")
        return

    if log:
        log(f"📊 [{account}] Opening {action} panel for screenshot")

    from contextlib import nullcontext
    ctx = window_lock if window_lock else nullcontext()
    with ctx:
        restore_wid = get_focused_window()
        try:
            # Restore if minimized, then focus — do this BEFORE getting geometry
            # so GetWindowRect/GetClientRect return valid dimensions
            was_minimized = is_window_minimized(wid)
            if was_minimized:
                restore_window(wid)
                time.sleep(0.5)
            raise_and_focus_window(wid)
            time.sleep(0.4 if was_minimized else 0.3)

            # Get geometry after window is visible and focused
            geom = get_window_geometry(wid)
            if not geom:
                if log:
                    log(f"  ⚠ [{account}] Could not get window geometry")
                return

            click_x, click_y = _get_client_click_pos(wid, offset_x, offset_y)
            if click_x is None:
                if log:
                    log(f"  ⚠ [{account}] Could not compute click position")
                return

            # Open panel
            _click(click_x, click_y)
            time.sleep(0.2)

            # Screenshot while panel is open (full window) — runs inside the lock
            if screenshot_cb:
                screenshot_cb()

            # Close panel
            _click(click_x, click_y)
        finally:
            if restore_wid and restore_wid != wid:
                raise_and_focus_window(restore_wid)


# ── Force: time adjustment (click N times) ────────────────────────────────────
def do_force(account, adjustment, amount, log=None, window_lock=None):
    """
    Click the given adjustment button `amount` times for the account.
    Focuses the DreamBot window once, clicks N times, then restores focus.
    window_lock: shared threading.Lock() to serialize with screenshot ops.
    """
    offsets = CLICK_OFFSETS.get(adjustment)
    if not offsets:
        if log:
            log(f"  ⚠ [{account}] Unknown adjustment: {adjustment}")
        return
    offset_x, offset_y = offsets
    wid = _find_window(account)
    if not wid:
        if log:
            log(f"  ⚠ [{account}] No window found")
        return

    if log:
        log(f"⏱ [{account}] Clicking {adjustment} × {amount}")

    from contextlib import nullcontext
    ctx = window_lock if window_lock else nullcontext()
    with ctx:
        restore_wid = get_focused_window()
        try:
            was_minimized = is_window_minimized(wid)
            if was_minimized:
                restore_window(wid)
                time.sleep(0.5)
            raise_and_focus_window(wid)
            time.sleep(0.4 if was_minimized else 0.3)

            geom = get_window_geometry(wid)
            if not geom:
                if log:
                    log(f"  ⚠ [{account}] Could not get window geometry")
                return
            click_x, click_y = _get_client_click_pos(wid, offset_x, offset_y)
            if click_x is None:
                if log:
                    log(f"  ⚠ [{account}] Could not compute click position")
                return

            for i in range(amount):
                _click(click_x, click_y)
                if i < amount - 1:
                    time.sleep(0.15)
        finally:
            if restore_wid and restore_wid != wid:
                raise_and_focus_window(restore_wid)

    if log:
        log(f"✅ [{account}] {adjustment} × {amount} done")


# ── Script GUI: click Start (v3.0.0) ──────────────────────────────────────────
# On Detuks, --scriptArgs only picks the profile; P2P Master AI then opens its
# GUI ("P2P Master AI <version>") and waits for Start (Enter does nothing).
# The window can open anywhere and at any size, so the button is found by
# colour, not by a fixed offset: Start is the largest solid block of the
# dark-teal fill below in the GUI capture. The GUI window is only looked up by
# the PID of the account's own BotClient window, so another account's GUI can
# never be clicked.
START_BTN_RGB      = (0, 65, 71)
START_BTN_TOL      = 14
START_BTN_MIN_FRAC = 0.02     # block must cover ≥2% of the GUI window


def find_start_button(img, target=START_BTN_RGB, tol=START_BTN_TOL,
                      min_frac=START_BTN_MIN_FRAC):
    """Largest connected block of the Start colour in a PIL image.
    Returns (cx, cy, (x0, y0, x1, y1), area) in image pixels, or None."""
    im = img.convert('RGB')
    W, H = im.size
    if not W or not H:
        return None
    data = im.tobytes()
    tr, tg, tb = target
    ok = bytearray(W * H)
    for i in range(W * H):
        r, g, b = data[3 * i], data[3 * i + 1], data[3 * i + 2]
        if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
            ok[i] = 1
    best = None
    for start in range(W * H):
        if ok[start] != 1:
            continue
        ok[start] = 2
        stack = [start]
        n = 0
        x0 = x1 = start % W
        y0 = y1 = start // W
        while stack:
            p = stack.pop()
            n += 1
            px, py = p % W, p // W
            if px < x0: x0 = px
            if px > x1: x1 = px
            if py < y0: y0 = py
            if py > y1: y1 = py
            if px + 1 < W and ok[p + 1] == 1:
                ok[p + 1] = 2; stack.append(p + 1)
            if px > 0 and ok[p - 1] == 1:
                ok[p - 1] = 2; stack.append(p - 1)
            if py + 1 < H and ok[p + W] == 1:
                ok[p + W] = 2; stack.append(p + W)
            if py > 0 and ok[p - W] == 1:
                ok[p - W] = 2; stack.append(p - W)
        if best is None or n > best[3]:
            best = ((x0 + x1) // 2, (y0 + y1) // 2, (x0, y0, x1, y1), n)
    if best is None or best[3] < min_frac * W * H:
        return None
    return best


def find_start_guis(account):
    """Script GUI windows to click Start on for this account (v3.1.0).
    Each visible "P2P Master AI x.y" window is matched to the BotClient window
    of the same process:
      1. GUIs on the account's own client (title ends in "- <account>") —
         used if there are any;
      2. otherwise GUIs on clients that have not logged in yet (title without
         an account) — a freshly loaded script waiting for Start (it logs in
         only after Start), all of them;
      never a GUI on a client logged in as another account — except when
      exactly one client is open and no client shows this account's name
      (shared-log fallback label), as in v3.0.0.
    Returns (window_ids, reason_if_none)."""
    from py.platform_ops import (list_script_gui_windows, client_titles_for_pid,
                                 parse_client_title, title_matches_account)
    from py.platform_ops import list_client_window_titles
    guis = list_script_gui_windows()
    if not guis:
        if not list_client_window_titles():
            return [], 'no BotClient window found'
        return [], 'script GUI window not found (already started or not open yet)'
    own, prelogin, other = [], [], 0
    for wid, pid in guis:
        titles = client_titles_for_pid(pid) if pid else []
        if any(title_matches_account(t, account) for t in titles):
            own.append(wid)
        elif titles and all((parse_client_title(t) or (None, 'x'))[1] == '' for t in titles):
            prelogin.append(wid)
        else:
            other += 1
    if own:
        return own, ''
    if prelogin:
        return prelogin, ''
    # Shared-log fallback (account name not shown in any title, e.g. the
    # neutral "Shared log" label): with exactly ONE client open, its GUI is
    # the only candidate — same rule as v3.0.0.
    clients = list_client_window_titles()
    if guis and len(clients) == 1 and not any(
            title_matches_account(t, account) for _w, t in clients):
        return [wid for wid, _pid in guis], ''
    return [], (f'no script GUI for this account ({other} open on other accounts\' clients)'
                if other else 'script GUI window not found (already started or not open yet)')


def click_script_start(account, log=None, window_lock=None):
    """Find the account's P2P Master AI GUI and click Start.
    Returns (ok, reason). Never raises."""
    targets, why = find_start_guis(account)
    if not targets:
        return False, why
    results = [_click_start_on(gwid, account, log, window_lock) for gwid in targets]
    ok = [r for r in results if r[0]]
    if ok:
        return True, ''
    return False, results[-1][1]


def _click_start_on(gwid, account, log=None, window_lock=None):
    """Locate Start in one GUI window by colour and click it. (ok, reason)."""
    import os
    import tempfile
    from contextlib import nullcontext
    from py.platform_ops import capture_window_image
    try:
        from PIL import Image
    except ImportError:
        return False, 'Pillow not installed'

    ctx = window_lock if window_lock else nullcontext()
    tmp = os.path.join(tempfile.gettempdir(), f'p2p_gui_{os.getpid()}.png')
    with ctx:
        restore_wid = get_focused_window()
        try:
            if is_window_minimized(gwid):
                restore_window(gwid)
                time.sleep(0.5)
            raise_and_focus_window(gwid)
            time.sleep(0.4)
            ok, err = capture_window_image(gwid, tmp)
            if not ok:
                return False, f'GUI capture failed: {err}'
            with Image.open(tmp) as im:
                im.load()
                iw, ih = im.size
                hit = find_start_button(im)
            if not hit:
                return False, 'Start button not found in the GUI capture'
            geom = get_window_geometry(gwid)
            if not geom:
                return False, 'could not read GUI window geometry'
            gx, gy, gw, gh = geom
            sx, sy = (gw / iw if iw else 1.0), (gh / ih if ih else 1.0)
            cx, cy = hit[0], hit[1]
            x, y = int(round(gx + cx * sx)), int(round(gy + cy * sy))
            if log:
                log(f'[{account}] Start button at image ({cx},{cy}) of {iw}x{ih} '
                    f'→ screen ({x},{y}); GUI geom {geom}')
            _click(x, y)
            time.sleep(0.2)
            return True, ''
        except Exception as e:
            return False, f'auto-click error: {e}'
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if restore_wid and restore_wid != gwid:
                try:
                    raise_and_focus_window(restore_wid)
                except Exception:
                    pass
