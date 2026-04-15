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
PAINT_BTN_X_OFFSET = 100
PAINT_BTN_Y_OFFSET = 50
PAINT_BTN_CROP_W   = 60
PAINT_BTN_CROP_H   = 20
PAINT_REF_FILE     = Path.home() / ".p2p_monitor" / "paint_visible_ref.png"
PAINT_DIFF_THRESH  = 0.15

# ── Linux-only paint detection helpers ──────────────────────────────────────────
# These functions only run on Linux (gated by supports_paint_detection()).
# They use X11/xdotool/ImageMagick and must not be called on Windows.

def _get_linux_env():
    """Return X11 display env for Linux-only operations."""
    from py.util import get_display_env
    return get_display_env()

from py.platform_ops import (find_window_ids_by_name, capture_window_image,
                             get_focused_window, get_window_geometry,
                             is_window_minimized, restore_window,
                             raise_and_focus_window, minimize_window,
                             click_at, supports_paint_detection)

def _window_is_minimized(wid, env=None):
    """Delegates to platform_ops — env param kept for call-site compat."""
    return is_window_minimized(wid)

def get_focused_wid():
    """Delegates to platform_ops."""
    return get_focused_window()

def _get_paint_btn_coords(wid, env=None):
    import sys
    if sys.platform.startswith('linux'):
        env  = env or _get_linux_env()
        geom = _query_window_geom(wid, env)
    else:
        from py.platform_ops import get_window_geometry
        geom = get_window_geometry(wid)
    if not geom:
        return None
    x, y, w, h = geom
    return (x + PAINT_BTN_X_OFFSET, y + h - PAINT_BTN_Y_OFFSET)

# ── Paint state detection ──────────────────────────────────────────────────────
def _capture_btn_crop(btn_coords, env=None):
    """Capture a crop of the paint button area. Returns path to temp file or None."""
    import sys
    bx, by = btn_coords
    x0 = bx - PAINT_BTN_CROP_W // 2
    y0 = by - PAINT_BTN_CROP_H // 2
    tmp = str(Path.home() / ".p2p_monitor" / "_paint_btn_cmp.png")
    if sys.platform.startswith('linux'):
        env = env or _get_linux_env()
        subprocess.run(
            ['import', '-window', 'root',
             '-crop', f'{PAINT_BTN_CROP_W}x{PAINT_BTN_CROP_H}+{x0}+{y0}', '+repage', tmp],
            capture_output=True, timeout=5, env=env)
        return tmp if Path(tmp).exists() else None
    else:
        # Windows: use Pillow ImageGrab for screen region capture
        try:
            from PIL import ImageGrab
            bbox = (x0, y0, x0 + PAINT_BTN_CROP_W, y0 + PAINT_BTN_CROP_H)
            img  = ImageGrab.grab(bbox=bbox)
            img.save(tmp)
            return tmp if Path(tmp).exists() else None
        except Exception:
            return None

def _paint_is_visible(btn_coords, env=None):
    """Return True if paint overlay is currently visible.
    Falls back to True (assume visible) on any failure so capture always proceeds.
    Linux: ImageMagick compare. Windows: Pillow ImageChops.difference.
    """
    import sys
    if not PAINT_REF_FILE.exists():
        return True
    crop = _capture_btn_crop(btn_coords, env)
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
            diff = ImageChops.difference(img_curr, img_ref)
            pixels   = list(diff.getdata())
            # Normalise to 0.0–1.0 range matching PAINT_DIFF_THRESH
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

def _save_paint_reference(btn_coords, env=None, log=None, debug=False):
    crop = _capture_btn_crop(btn_coords, env)
    if crop:
        try:
            shutil.move(crop, str(PAINT_REF_FILE))
        except Exception as e:
            if debug and log:
                log(f'[DEBUG] _save_paint_reference failed: {e}')

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
                bot_channel_id=None, bot_token=None, restore_wid=None):
        """Single enqueue point for all screenshot requests.
        Guards: screenshots_enabled and is_muted are checked here so no
        caller can bypass them regardless of which code path they take.
        """
        cfg = self._cb['get_cfg']()
        if not cfg.get('screenshots_enabled'):
            return
        if self._cb['is_muted'](account):
            return
        self._cb['log'](f"📸 [{account}] Screenshot queued ({trigger})...")
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        try:
            self._queue.put_nowait((priority, seq, account, trigger, True,
                                    url, payload, bot_channel_id, bot_token, restore_wid))
        except queue.Full:
            self._cb['log'](f"  ⚠ [{account}] Screenshot queue full — request dropped ({trigger})")

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
        from py.discord import post_discord, post_bot_image, screenshot_payload  # local import avoids circular
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
                    path, err = take_screenshot(account, restore_wid=restore_wid, hide_paint=hide_paint)
                if not path:
                    self._cb['log'](f"  🚫 [{account}] Screenshot failed: {err}")
                else:
                    try:
                        if bot_channel_id and bot_token:
                            bot_ready = self._cb.get('bot_ready')
                            if bot_ready and not bot_ready.is_set():
                                self._cb['log'](f"  ⏳ [{account}] Waiting for gateway before screenshot...")
                                bot_ready.wait(timeout=60)
                            if bot_ready and not bot_ready.is_set():
                                self._cb['log'](f"  ⚠ [{account}] Gateway not ready after 60s — screenshot dropped")
                            else:
                                ok, err = post_bot_image(bot_channel_id, bot_token, account, path)
                                if not ok:
                                    self._cb['log'](f"  🚫 [{account}] Bot screenshot failed: {err}")
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
                                    self._cb['log'](f"  🚫 [{account}] Screenshot failed: {e}")
                            else:
                                self._cb['log'](f"  ⚠ [{account}] No default webhook configured")
                    finally:
                        try:
                            os.unlink(path)
                        except Exception:
                            pass
            except Exception as ex:
                self._cb['log'](f"  ⚠ Screenshot worker error: {ex}")
            finally:
                self._queue.task_done()


def take_screenshot(account_name, restore_wid=None, hide_paint=False):
    """
    Capture DreamBot window for account. Returns (path, err).
    restore_wid: window to raise after capture (pass for batch sequences).
    """
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        wids = find_window_ids_by_name(account_name)
        if not wids:
            return None, f"No window found for account: {account_name}"
        target_wid = wids[0]

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
                if not PAINT_REF_FILE.exists():
                    _save_paint_reference(btn_coords)
                paint_visible = _paint_is_visible(btn_coords)
                if hide_paint and paint_visible:
                    _click_paint_button(btn_coords)
                    time.sleep(0.2)
                    # Verify the click had the intended effect — self-correct if not
                    if _paint_is_visible(btn_coords):
                        _click_paint_button(btn_coords)
                        time.sleep(0.2)
                    did_hide = True
                elif not hide_paint and not paint_visible:
                    _click_paint_button(btn_coords)
                    time.sleep(0.2)
                    # Verify paint is now visible — self-correct if still hidden
                    if not _paint_is_visible(btn_coords):
                        _click_paint_button(btn_coords)
                        time.sleep(0.2)

            ok_cap, err_cap = capture_window_image(target_wid, out_path)

            if did_hide and btn_coords:
                _click_paint_button(btn_coords)
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
