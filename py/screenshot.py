"""
screenshot.py — Screenshot capture for P2P Monitor
All xdotool, window automation, and paint hide/show logic lives here.
"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

@contextmanager
def _nullctx():
    yield

# ── Priority constants ─────────────────────────────────────────────────────────
SS_PRIORITY_ONDEMAND  = 0   # manual button click or bot !ss/!b
SS_PRIORITY_DROPS     = 1   # drop events (most time-sensitive)
SS_PRIORITY_EVENT     = 2   # task, quest, chat, error, slayer events
SS_PRIORITY_SCHEDULED = 3   # scheduled interval screenshots

SCREENSHOT_DIR = Path.home() / '.p2p_monitor' / 'screenshots'

# ── Paint button geometry ──────────────────────────────────────────────────────
# PAINT_BTN_* constants imported from py.platform_ops (centralized source of truth)
PAINT_REF_FILE       = Path.home() / ".p2p_monitor" / "paint_visible_ref.png"
PAINT_REF_SCALE_FILE = Path.home() / ".p2p_monitor" / "paint_ref_scale.txt"
PAINT_DIFF_THRESH  = 0.15

# ── Linux-only paint detection helpers ──────────────────────────────────────────
# These functions only run on Linux (gated by supports_paint_detection()).
# They use X11/xdotool/ImageMagick and must not be called on Windows.

def _get_linux_env():
    """Return X11 display env for Linux-only operations."""
    from py.util import get_display_env
    return get_display_env()

from py.platform_ops import (find_window_ids_by_name, capture_window_image,
                             get_focused_window,
                             is_window_minimized, restore_window,
                             raise_and_focus_window, minimize_window,
                             click_at, supports_paint_detection,
                             get_window_dpi_scale, get_window_title,
                             find_windows_for_pid, get_pid_for_window,
                             PAINT_BTN_X_OFFSET, PAINT_BTN_Y_OFFSET,
                             PAINT_BTN_CROP_W, PAINT_BTN_CROP_H)

def _window_is_minimized(wid, env=None):
    """Delegates to platform_ops — env param kept for call-site compat."""
    return is_window_minimized(wid)

def get_focused_wid():
    """Delegates to platform_ops."""
    return get_focused_window()

def _get_paint_btn_coords(wid, env=None):
    """Return paint button position in DWM/capture-space coordinates.
    Used for crop, reference, and visibility detection — NOT for clicking.
    """
    from py.platform_ops import get_window_geometry
    geom = get_window_geometry(wid)
    if not geom:
        return None
    x, y, w, h = geom
    scale = get_window_dpi_scale(wid)
    off_x = round(PAINT_BTN_X_OFFSET * scale)
    off_y = round(PAINT_BTN_Y_OFFSET * scale)
    return (x + off_x, y + h - off_y)

# ── Paint state detection ──────────────────────────────────────────────────────
def _capture_btn_crop(btn_coords, env=None, wid=None):
    """Capture a crop of the paint button area. Returns path to temp file or None."""
    import sys

    bx, by = btn_coords
    scale  = get_window_dpi_scale(wid) if wid else 1.0
    crop_w = round(PAINT_BTN_CROP_W * scale)
    crop_h = round(PAINT_BTN_CROP_H * scale)
    x0 = bx - crop_w // 2
    y0 = by - crop_h // 2
    tmp = str(Path.home() / ".p2p_monitor" / "_paint_btn_cmp.png")
    if sys.platform.startswith('linux'):
        env = env or _get_linux_env()
        subprocess.run(
            ['import', '-window', 'root',
             '-crop', f'{crop_w}x{crop_h}+{x0}+{y0}', '+repage', tmp],
            capture_output=True, timeout=5, env=env)
        return tmp if Path(tmp).exists() else None
    else:
        if wid is None:
            return None
        try:
            from PIL import Image
            from py.platform_ops import capture_window_image, get_window_geometry
            tmp_full = str(Path.home() / ".p2p_monitor" / "_paint_full_cap.png")
            ok, err  = capture_window_image(wid, tmp_full)
            if not ok or not Path(tmp_full).exists():
                return None

            geom = get_window_geometry(wid)
            if not geom:
                try: Path(tmp_full).unlink(missing_ok=True)
                except Exception: pass
                return None
            win_x, win_y, win_w, win_h = geom
            rel_x0 = x0 - win_x
            rel_y0 = y0 - win_y

            img = Image.open(tmp_full)
            img_w, img_h = img.size

            # Hard bounds validation — never save a crop that falls outside the
            # captured image, which would produce garbage or the full window image
            bounds_ok = (
                rel_x0 >= 0 and
                rel_y0 >= 0 and
                rel_x0 + crop_w <= img_w and
                rel_y0 + crop_h <= img_h
            )
            if not bounds_ok:
                try: Path(tmp_full).unlink(missing_ok=True)
                except Exception: pass
                return None

            crop = img.crop((rel_x0, rel_y0,
                             rel_x0 + crop_w,
                             rel_y0 + crop_h))
            crop.save(tmp)
            try: Path(tmp_full).unlink(missing_ok=True)
            except Exception: pass
            return tmp if Path(tmp).exists() else None
        except Exception:
            return None

def _paint_is_visible(btn_coords, env=None, wid=None):
    """Return True if paint overlay is currently visible.
    Falls back to True (assume visible) on any failure so capture always proceeds.
    Linux: ImageMagick compare. Windows: Pillow ImageChops.difference.

    Auto-resnaps the reference if DPI scale has changed by more than 0.05
    (e.g. window moved to a different monitor or user changed scaling).
    Guard prevents repeated resnap attempts within a single call.
    """
    import sys
    if not PAINT_REF_FILE.exists():
        return True

    # ── DPI scale check — resnap reference if scale has changed ──────────────
    ref_resnap_attempted = False
    current_scale = get_window_dpi_scale(wid) if wid else 1.0
    try:
        if PAINT_REF_SCALE_FILE.exists():
            saved_scale = float(PAINT_REF_SCALE_FILE.read_text().strip())
            if abs(current_scale - saved_scale) > 0.05:
                # DPI changed — resnap reference using current scale
                if not ref_resnap_attempted:
                    ref_resnap_attempted = True
                    try:
                        _save_paint_reference(btn_coords, wid=wid)
                    except Exception:
                        pass  # resnap failed — continue with old reference
    except Exception:
        pass  # scale file unreadable — treat as mismatch handled by resize below

    crop = _capture_btn_crop(btn_coords, env, wid=wid)
    if not crop:
        return True  # capture failed — assume visible, fail safe
    try:
        if sys.platform.startswith('linux'):
            env = env or _get_linux_env()
            r = subprocess.run(
                ['compare', '-metric', 'RMSE', crop, str(PAINT_REF_FILE), os.devnull],
                capture_output=True, text=True, timeout=5, env=env)
            out = (r.stdout + r.stderr).strip()
            m   = re.search(r'\(([\d.]+)\)', out)
            if m:
                diff = float(m.group(1))
                return diff < PAINT_DIFF_THRESH
        else:
            # Windows: pure Pillow comparison — no ImageMagick needed
            from PIL import Image, ImageChops
            img_curr = Image.open(crop).convert('RGB')
            img_ref  = Image.open(str(PAINT_REF_FILE)).convert('RGB')
            if img_curr.size != img_ref.size:
                img_curr = img_curr.resize(img_ref.size, Image.LANCZOS)
            diff     = ImageChops.difference(img_curr, img_ref)
            pixels   = list(diff.getdata())
            avg_diff = sum(max(r,g,b) for r,g,b in pixels) / (len(pixels) * 255.0)
            return avg_diff < PAINT_DIFF_THRESH
    except Exception:
        pass  # comparison failed — assume visible, fail safe
    finally:
        try:
            Path(crop).unlink(missing_ok=True)
        except Exception:
            pass
    return True

def _save_paint_reference(btn_coords, env=None, log=None, debug=False, wid=None):
    crop = _capture_btn_crop(btn_coords, env, wid=wid)
    if not crop:
        return
    # Guard: only save if the crop file actually exists and has non-zero size
    crop_path = Path(crop)
    if not crop_path.exists() or crop_path.stat().st_size == 0:
        try: crop_path.unlink(missing_ok=True)
        except Exception: pass
        return
    try:
        shutil.move(crop, str(PAINT_REF_FILE))
        scale = get_window_dpi_scale(wid) if wid else 1.0
        try:
            PAINT_REF_SCALE_FILE.write_text(str(scale))
        except Exception:
            pass  # scale file is best-effort
    except Exception as e:
        if debug and log:
            log(f'[DEBUG] _save_paint_reference failed: {e}')

def _get_paint_click_coords(wid):
    """
    Compute paint button click position using ClientToScreen as anchor.

    DWM EXTENDED_FRAME_BOUNDS (used by get_window_geometry) includes the
    invisible drop shadow border — its origin and size do not match the
    actual clickable client area. Using DWM coords for clicking lands below
    the real window bottom.

    Uses ClientToScreen(hwnd, 0,0) to get the real client area origin on the
    desktop, then adds client-relative offsets. No DPI scaling applied —
    ClientToScreen returns logical desktop coordinates which is the same space
    SetCursorPos operates in.

    Only used for click injection. Crop/reference path keeps using DWM geometry.
    Returns (click_x, click_y) or None on failure.
    """
    import sys
    if sys.platform != 'win32':
        return None
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
        click_x = pt.x + PAINT_BTN_X_OFFSET
        click_y = pt.y + client_h - PAINT_BTN_Y_OFFSET
        if old_ctx is not None:
            try:
                user32.SetThreadDpiAwarenessContext(old_ctx)
            except Exception:
                pass
        return (click_x, click_y)
    except Exception:
        return None


def _click_paint_button(coords, env=None):
    """Click the paint toggle button. env param kept for call-site compat."""
    bx, by = coords
    click_at(bx, by)


class ScreenshotService:
    """Owns the screenshot queue, worker thread, and capture execution.
    Watcher decides *when* and *why* — this class decides *how*.

    Callbacks (passed at construction):
        get_cfg()              → current cfg dict (live reference)
        log(msg)               → watcher log function
        is_muted(account)      → bool
        wh_with_thread(key, account) → (url, thread_id)
    """

    HIDE_KEY_MAP = {
        'scheduled': 'ss_hide_paint_scheduled',
        'on-demand': 'ss_hide_paint_ondemand',
        'startup':   'ss_hide_paint_startup',
        'bot-ss':    'ss_hide_paint_botss',
        'drop':      'ss_hide_paint_drops',
        'task':      'ss_hide_paint_task',
        'quest':     'ss_hide_paint_quest',
        'chat':      'ss_hide_paint_chat',
        'error':     'ss_hide_paint_error',
        'death':     'ss_hide_paint_death',
        'levelup':   'ss_hide_paint_levelup',
    }

    def __init__(self, callbacks):
        self._cb       = callbacks
        self._queue    = queue.PriorityQueue(maxsize=50)
        self._seq      = 0
        self._seq_lock = threading.Lock()
        self._thread   = None
        self._stop     = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Exception:
                break
        if self._thread and self._thread.is_alive() and \
                self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def enqueue(self, priority, account, trigger,
                url=None, payload=None,
                bot_channel_id=None, bot_token=None, restore_wid=None) -> bool:
        """Single enqueue point for all screenshot requests.
        Returns True if queued, False if refused.

        Guard logic by trigger type:
          - 'scheduled': check screenshots_enabled (scheduled-only setting)
          - 'startup':   check screenshot_on_startup
          - all others:  only check is_muted; event/on-demand/bot screenshots
                         are gated by their own per-event settings at call site
        """
        cfg = self._cb['get_cfg']()
        if trigger == 'scheduled' and not cfg.get('screenshots_enabled', False):
            return False
        if trigger == 'startup' and not cfg.get('screenshot_on_startup', False):
            return False
        if self._cb['is_muted'](account):
            return False
        # Successful screenshot flow is intentionally silent — no log here.
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        try:
            self._queue.put_nowait((priority, seq, account, trigger, True,
                                    url, payload, bot_channel_id, bot_token, restore_wid))
            return True
        except queue.Full:
            self._dbg(f"  ⚠ [{account}] Screenshot queue full — request dropped ({trigger})")
            return False

    def _dbg(self, msg: str) -> None:
        """Log msg only when debug mode is enabled in config. Screenshot-only noise is always routed here."""
        try:
            cfg = self._cb['get_cfg']()
            if cfg.get('debug', False):
                self._cb['log'](f'[DEBUG] {msg}')
        except Exception:
            pass  # never let a logging helper crash the worker

    def prune(self):
        """Remove screenshot files older than 24 hours."""
        try:
            cutoff = time.time() - 86400
            for f in SCREENSHOT_DIR.glob("*.png"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                except Exception:
                    pass  # file already gone or locked — safe to ignore
        except Exception:
            pass  # screenshot dir unavailable — safe to ignore

    def _worker(self):
        """Process queued screenshot requests in priority order."""
        from py.discord import (post_discord, post_bot_image,  # local import avoids circular
                                screenshot_payload, _add_recovery_footer)
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
                _priority, _seq, account, trigger, _is_last, \
                    url_override, payload_override, bot_channel_id, bot_token, restore_wid = item
            except queue.Empty:
                continue
            try:
                cfg        = self._cb['get_cfg']()
                hide_key   = self.HIDE_KEY_MAP.get(trigger)
                hide_paint = bool(cfg.get(hide_key, False)) if hide_key else False
                if restore_wid is None:
                    restore_wid = get_focused_wid()
                window_lock = self._cb.get('window_lock')
                with window_lock if window_lock else _nullctx():
                    path, err = take_screenshot(
                        account,
                        restore_wid=restore_wid,
                        hide_paint=hide_paint,
                        get_pid_cb=self._cb.get('get_account_pid'),
                        set_pid_cb=self._cb.get('set_account_pid'),
                    )
                if not path:
                    self._dbg(f"  🚫 [{account}] Screenshot failed: {err}")
                    # Fallback: if this was an event screenshot with a payload and URL,
                    # post the embed without the image so the notification is not lost.
                    # Scheduled screenshots have no event payload — skip fallback for those.
                    if payload_override is not None and url_override:
                        self._dbg(f"  ↩ [{account}] Posting embed-only fallback for {trigger}")
                        try:
                            from py.discord import post_discord as _pd
                            _pd(url_override, payload_override)
                        except Exception as _fe:
                            self._dbg(f"  🚫 [{account}] Embed-only fallback failed: {_fe}")
                else:
                    try:
                        if bot_channel_id and bot_token:
                            bot_ready = self._cb.get('bot_ready')
                            if bot_ready and not bot_ready.is_set():
                                self._dbg(f"  ⏳ [{account}] Waiting for gateway before screenshot...")
                                bot_ready.wait(timeout=60)
                            if bot_ready and not bot_ready.is_set():
                                self._dbg(f"  ⚠ [{account}] Gateway not ready after 60s — screenshot dropped")
                            else:
                                ok, err = post_bot_image(bot_channel_id, bot_token, account, path)
                                if not ok:
                                    handler = self._cb.get('handle_post_error')
                                    if handler:
                                        def _retry_bot(new_url, _p=path, _a=account, _t=bot_token):
                                            # Bot image posts go to a channel ID, not a webhook URL;
                                            # after recovery the channel may be recreated with a new ID.
                                            # Re-read the channel ID from config for the retry.
                                            new_cfg = self._cb['get_cfg']()
                                            ch_id = new_cfg.get('bot_channel_ids', {}).get('monitor', '')
                                            if ch_id:
                                                return post_bot_image(ch_id, _t, _a, _p)
                                            return False, 'No monitor channel after recovery'
                                        ok = handler(err, None, account, _retry_bot)
                                    if not ok:
                                        self._dbg(f"  🚫 [{account}] Bot screenshot failed: {err}")
                        else:
                            if url_override:
                                url = url_override
                            else:
                                url, _ = self._cb['wh_with_thread']('default', account)
                                if not url:
                                    url = cfg.get('webhook_default', '').strip()
                            payload = payload_override if payload_override is not None else (
                                None if trigger == 'scheduled' else screenshot_payload(account, trigger))
                            if url:
                                ok, e = post_discord(url, payload, image_path=path)
                                if not ok:
                                    handler = self._cb.get('handle_post_error')
                                    if handler:
                                        def _retry_wh(new_url, _p=payload, _img=path):
                                            retry_p = _add_recovery_footer(_p,
                                                "⚠ Thread/channel was recreated") if _p else _p
                                            return post_discord(new_url, retry_p, image_path=_img)
                                        ok = handler(e, url, account, _retry_wh)
                                    if not ok:
                                        self._dbg(f"  🚫 [{account}] Screenshot failed: {e}")
                            else:
                                self._dbg(f"  ⚠ [{account}] No default webhook configured for screenshot")
                    finally:
                        try:
                            os.unlink(path)
                        except Exception:
                            pass
            except Exception as ex:
                self._dbg(f"  ⚠ Screenshot worker error: {ex}")
            finally:
                self._queue.task_done()




def snap_paint_reference(log=None):
    """
    Capture a fresh paint reference image from the first available DreamBot window.
    Focuses the window briefly, captures the paint button region, saves reference,
    then restores focus to the monitor.
    Returns (ok: bool, msg: str).
    """
    import sys
    from py.platform_ops import get_focused_window
    from py.platform_ops import raise_and_focus_window

    # Find any DreamBot window
    # Try common account patterns — find_window_ids_by_name with just "dreambot"
    import ctypes
    wids = []
    if sys.platform == 'win32':
        # On Windows enumerate all DreamBot windows directly
        try:
            from ctypes import wintypes
            user32 = ctypes.WinDLL('user32')
            results = []
            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _):
                try:
                    if not user32.IsWindowVisible(hwnd): return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length <= 0: return True
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if 'dreambot' in (buf.value or '').lower():
                        results.append(str(int(hwnd)))
                except Exception: pass
                return True
            user32.EnumWindows(_enum, 0)
            wids = results
        except Exception:
            pass
    else:
        from py.platform_ops import find_window_ids_by_name
        wids = find_window_ids_by_name('dreambot')

    if not wids:
        return False, 'No DreamBot window found — make sure DreamBot is running'

    wid = wids[0]
    restore_wid = get_focused_window()

    try:
        import time
        raise_and_focus_window(wid)
        time.sleep(0.4)  # wait for window to render

        btn_coords = _get_paint_btn_coords(wid)
        if not btn_coords:
            return False, 'Could not determine paint button position'

        _save_paint_reference(btn_coords, wid=wid, log=log)

        if PAINT_REF_FILE.exists() and PAINT_REF_FILE.stat().st_size > 0:
            if log: log('✅ Paint reference snapped successfully')
            return True, 'Paint reference saved'
        else:
            return False, 'Snap failed — reference file not created'
    finally:
        if restore_wid:
            try:
                raise_and_focus_window(restore_wid)
            except Exception:
                pass

def take_screenshot(account_name, restore_wid=None, hide_paint=False,
                    get_pid_cb=None, set_pid_cb=None):
    """
    Capture DreamBot window for account. Returns (path, err).
    restore_wid:  window to raise after capture (pass for batch sequences).
    get_pid_cb:   optional callable(account) → int|None — read cached PID
    set_pid_cb:   optional callable(account, pid) — write resolved PID to cache
    """
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        target_wid = None

        # ── 1. PID-first path ─────────────────────────────────────────────────
        # find_windows_for_pid already filters: visible + 'dreambot' in title.
        # Account name match not required — trusted PID is the ownership signal.
        pid = get_pid_cb(account_name) if get_pid_cb else None
        if pid:
            pid_wids = find_windows_for_pid(pid)
            if pid_wids:
                target_wid = pid_wids[0]

        # ── 2. Title fallback ─────────────────────────────────────────────────
        if target_wid is None:
            wids = find_window_ids_by_name(account_name)
            if not wids:
                return None, f"No window found for account: {account_name}"
            candidate = wids[0]
            # Hard guard: must contain both 'dreambot' AND account name
            confirmed_title = get_window_title(candidate)
            title_lower = confirmed_title.lower()
            if 'dreambot' not in title_lower or account_name.lower() not in title_lower:
                return None, (
                    f"HWND {candidate} title {confirmed_title!r} does not match "
                    f"expected DreamBot account {account_name!r} — capture aborted "
                    f"to prevent wrong-window screenshot"
                )
            target_wid = candidate
            # Save discovered PID so PID path is used on subsequent screenshots
            if set_pid_cb:
                try:
                    resolved_pid = get_pid_for_window(target_wid)
                    if resolved_pid:
                        set_pid_cb(account_name, resolved_pid)
                except Exception:
                    pass

        ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', account_name)
        out_path  = str(SCREENSHOT_DIR / f"{safe_name}_{ts}.png")

        if restore_wid is None:
            restore_wid = get_focused_window()

        target_was_minimized = _window_is_minimized(target_wid)

        try:
            if target_was_minimized:
                restore_window(target_wid)
                time.sleep(0.15)
            raise_and_focus_window(target_wid)
            # If we had to bring the window into focus, give it extra time to
            # fully render before reading the paint button state.
            time.sleep(0.6 if target_was_minimized else 0.3)

            btn_coords = _get_paint_btn_coords(target_wid) if supports_paint_detection() else None
            did_hide   = False
            if btn_coords and supports_paint_detection():
                # btn_coords   — DWM-based logical coords, used for crop/reference/visibility
                # click_coords — ClientToScreen-based coords, used for click injection only
                # Falls back to btn_coords on Linux (where client == DWM frame)
                click_coords = _get_paint_click_coords(target_wid) or btn_coords
                if not PAINT_REF_FILE.exists():
                    _save_paint_reference(btn_coords, wid=target_wid)
                paint_visible = _paint_is_visible(btn_coords, wid=target_wid)
                if hide_paint and paint_visible:
                    _click_paint_button(click_coords)
                    time.sleep(0.3)
                    did_hide = True
                elif not hide_paint and not paint_visible:
                    _click_paint_button(click_coords)
                    time.sleep(0.3)

            ok_cap, err_cap = capture_window_image(target_wid, out_path)

            if did_hide and btn_coords:
                click_coords = _get_paint_click_coords(target_wid) or btn_coords
                _click_paint_button(click_coords)
        finally:
            if target_was_minimized:
                try:
                    minimize_window(target_wid)
                    time.sleep(0.1)
                except Exception:
                    pass
            if restore_wid and restore_wid != target_wid:
                try:
                    raise_and_focus_window(restore_wid)
                except Exception:
                    pass

        if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return out_path, ''
        return None, f'Screenshot failed: {err_cap}'
    except FileNotFoundError as e:
        if supports_paint_detection():
            return None, f"Tool missing: {e} — run: sudo apt-get install xdotool imagemagick"
        return None, f"Tool missing: {e}"
    except Exception as e:
        return None, str(e)
