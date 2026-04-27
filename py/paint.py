"""
paint.py — DreamBot window automation for P2P Monitor
Owns hide/show paint and all click-based interactions.
All coordinates are offsets from the bottom-left of the DreamBot window.
"""

import time

from py.platform_ops import (find_window_ids_by_name, get_focused_window,
                             get_window_geometry, raise_and_focus_window, click_at,
                             is_window_minimized, restore_window)

# ── Paint button (hide/show toggle) ───────────────────────────────────────────
PAINT_BTN_X_OFFSET = 100   # right from window left edge
PAINT_BTN_Y_OFFSET = 50    # up from window bottom edge

# ── Click offsets from bottom-left of the DreamBot window ────────────────────
# Time adjustments
CLICK_OFFSETS = {
    '-10m':         (345,  48),
    '+10m':         (412,  47),
    # Panel toggles (Stats / Loot)
    'Stats':        (162,  48),
    'Loot':         (219,  50),
    # Skill / action forces
    'Skip':         (475,  48),
    'Quest':        (104,  80),
    'Attack':       (168, 176),
    'Strength':     (171, 146),
    'Defence':      (170, 114),
    'Range':        (174,  80),
    'Agility':      (231, 147),
    'Herblore':     (234, 116),
    'Thieving':     (232,  85),
    'Mining':       (294, 178),
    'Smithing':     (293, 145),
    'Fishing':      (296, 113),
    'Cooking':      (295,  84),
    'Prayer':       (355, 177),
    'Magic':        (358, 145),
    'Runecrafting': (359, 113),
    'Construction': (359,  82),
    'Crafting':     (421, 177),
    'Fletching':    (423, 145),
    'Slayer':       (419, 120),
    'Hunter':       (421,  84),
    'Firemaking':   (485, 177),
    'Woodcutting':  (487, 146),
    'Farming':      (484, 115),
    'Sailing':      (483,  83),
}

# Actions that are panel toggles (click open → screenshot → click close)
PANEL_ACTIONS = {'Stats', 'Loot'}

# Actions that need an amount parameter (clicked N times)
AMOUNT_ACTIONS = {'-10m', '+10m'}

# ── Window helpers ─────────────────────────────────────────────────────────────
def _find_window(account):
    """Find DreamBot window ID for account. Returns wid string or None."""
    wids = find_window_ids_by_name(account)
    return wids[0] if wids else None

def _click(x, y, env=None):
    """Move mouse to absolute screen coords and click. env param kept for compat."""
    click_at(x, y)

def click_at_offset(account, offset_x, offset_y):
    """
    Find the DreamBot window for account, focus it, click at the given offset
    from the bottom-left corner, then restore the previously focused window.
    Returns (True, '') on success or (False, error_msg) on failure.
    """
    wid = _find_window(account)
    if not wid:
        return False, f"No window found for account: {account}"
    geom = get_window_geometry(wid)
    if not geom:
        return False, f"Could not get window geometry for: {account}"

    # Remember what was focused before
    restore_wid = get_focused_window()

    try:
        raise_and_focus_window(wid)
        time.sleep(0.3)

        x, y, w, h = geom
        click_x = x + offset_x
        click_y = y + h - offset_y
        _click(click_x, click_y)
    finally:
        if restore_wid and restore_wid != wid:
            raise_and_focus_window(restore_wid)

    return True, ''

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
            x, y, w, h = geom
            click_x = x + offset_x
            click_y = y + h - offset_y
            _click(click_x, click_y)
        finally:
            if restore_wid and restore_wid != wid:
                raise_and_focus_window(restore_wid)

    if log:
        log(f"✅ [{account}] {action} forced")


# ── Force: panel toggle (open → screenshot → close) ───────────────────────────
def do_force_panel(account, action, screenshot_cb, log=None, window_lock=None):
    """
    Open a panel (Stats/Loot) by clicking once, take a full-window screenshot,
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

            x, y, w, h = geom
            click_x = x + offset_x
            click_y = y + h - offset_y

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
            x, y, w, h = geom
            click_x = x + offset_x
            click_y = y + h - offset_y

            for i in range(amount):
                _click(click_x, click_y)
                if i < amount - 1:
                    time.sleep(0.15)
        finally:
            if restore_wid and restore_wid != wid:
                raise_and_focus_window(restore_wid)

    if log:
        log(f"✅ [{account}] {adjustment} × {amount} done")
