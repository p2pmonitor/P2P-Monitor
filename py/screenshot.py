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

SCREENSHOT_DIR = Path('/tmp/screenshots')

# ── Paint button geometry ──────────────────────────────────────────────────────
PAINT_BTN_X_OFFSET = 100
PAINT_BTN_Y_OFFSET = 50
PAINT_BTN_CROP_W   = 60
PAINT_BTN_CROP_H   = 20
PAINT_REF_FILE     = Path.home() / ".p2p_monitor" / "paint_visible_ref.png"
PAINT_DIFF_THRESH  = 0.15

# ── xdotool helpers (from shared util) ─────────────────────────────────────────
from py.util import xdotool as _xdotool, get_display_env, get_window_geom as _query_window_geom

def _window_is_minimized(wid, env):
    try:
        r = subprocess.run(['xprop', '-id', wid, 'WM_STATE'],
                           capture_output=True, text=True, timeout=3, env=env)
        return 'Iconic' in r.stdout
    except Exception:
        return False

def get_focused_wid():
    wid = _xdotool(['getactivewindow'], get_display_env())
    return wid if wid else None

def _get_paint_btn_coords(wid, env):
    geom = _query_window_geom(wid, env)
    if not geom:
        return None
    x, y, w, h = geom
    return (x + PAINT_BTN_X_OFFSET, y + h - PAINT_BTN_Y_OFFSET)

# ── Paint state detection ──────────────────────────────────────────────────────
def _capture_btn_crop(btn_coords, env):
    bx, by = btn_coords
    x0 = bx - PAINT_BTN_CROP_W // 2
    y0 = by - PAINT_BTN_CROP_H // 2
    tmp = str(Path.home() / ".p2p_monitor" / "_paint_btn_cmp.png")
    subprocess.run(
        ['import', '-window', 'root',
         '-crop', f'{PAINT_BTN_CROP_W}x{PAINT_BTN_CROP_H}+{x0}+{y0}', '+repage', tmp],
        capture_output=True, timeout=5, env=env)
    return tmp if Path(tmp).exists() else None

def _paint_is_visible(btn_coords, env):
    if not PAINT_REF_FILE.exists():
        return True
    crop = _capture_btn_crop(btn_coords, env)
    if not crop:
        return True
    try:
        r = subprocess.run(
            ['compare', '-metric', 'RMSE', crop, str(PAINT_REF_FILE), '/dev/null'],
            capture_output=True, text=True, timeout=5, env=env)
        out = (r.stdout + r.stderr).strip()
        m   = re.search(r'\(([\d.]+)\)', out)
        if m:
            diff = float(m.group(1))
            return diff < PAINT_DIFF_THRESH
    except Exception:
        pass
    finally:
        try:
            Path(crop).unlink(missing_ok=True)
        except Exception:
            pass
    return True

def _save_paint_reference(btn_coords, env):
    crop = _capture_btn_crop(btn_coords, env)
    if crop:
        try:
            shutil.move(crop, str(PAINT_REF_FILE))
        except Exception:
            pass

def _click_paint_button(coords, env):
    bx, by = coords
    _xdotool(['mousemove', '--', str(bx), str(by)], env, timeout=2)
    time.sleep(0.05)
    _xdotool(['click', '--clearmodifiers', '1'], env, timeout=2)

# ── Screenshot service ────────────────────────────────────────────────────────
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
        # Drain queue so worker can exit cleanly
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
                    pass
        except Exception:
            pass

    def _worker(self):
        """Process queued screenshot requests in priority order."""
        from py.discord import post_discord, post_bot_image, screenshot_payload  # local import avoids circular
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
                # Fixed 10-field tuple — see enqueue()
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
    env = get_display_env()
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        r = subprocess.run(['xdotool', 'search', '--name', account_name.lower()],
                           capture_output=True, text=True, timeout=5, env=env)
        wids = r.stdout.strip().split()
        if not wids:
            return None, f"No window found for account: {account_name}"
        target_wid = wids[0]

        ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', account_name)
        out_path  = str(SCREENSHOT_DIR / f"{safe_name}_{ts}.png")

        if restore_wid is None:
            restore_wid = _xdotool(['getactivewindow'], env) or None

        target_was_minimized = _window_is_minimized(target_wid, env)

        try:
            if target_was_minimized:
                _xdotool(['windowmap', target_wid], env)
                time.sleep(0.15)
            _xdotool(['windowraise',  target_wid], env)
            _xdotool(['windowfocus',  '--sync', target_wid], env)
            # If we had to bring the window into focus, give it extra time to
            # fully render before reading the paint button state.
            time.sleep(0.6 if target_was_minimized else 0.3)

            btn_coords = _get_paint_btn_coords(target_wid, env)
            did_hide   = False
            if btn_coords:
                if not PAINT_REF_FILE.exists():
                    _save_paint_reference(btn_coords, env)
                paint_visible = _paint_is_visible(btn_coords, env)
                if hide_paint and paint_visible:
                    _click_paint_button(btn_coords, env)
                    time.sleep(0.2)
                    # Verify the click had the intended effect — self-correct if not
                    if _paint_is_visible(btn_coords, env):
                        _click_paint_button(btn_coords, env)
                        time.sleep(0.2)
                    did_hide = True
                elif not hide_paint and not paint_visible:
                    _click_paint_button(btn_coords, env)
                    time.sleep(0.2)
                    # Verify paint is now visible — self-correct if still hidden
                    if not _paint_is_visible(btn_coords, env):
                        _click_paint_button(btn_coords, env)
                        time.sleep(0.2)

            result = subprocess.run(
                ['import', '-window', target_wid, out_path],
                capture_output=True, timeout=15, env=env)

            if did_hide and btn_coords:
                _click_paint_button(btn_coords, env)
        finally:
            if target_was_minimized:
                try:
                    _xdotool(['windowminimize', target_wid], env)
                    time.sleep(0.1)
                except Exception:
                    pass
            if restore_wid and restore_wid != target_wid:
                try:
                    _xdotool(['windowraise', restore_wid], env)
                    _xdotool(['windowfocus', '--sync', restore_wid], env)
                except Exception:
                    pass

        if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return out_path, ''
        err = result.stderr.decode(errors='replace').strip()
        return None, f"Screenshot failed (rc={result.returncode}): {err}"
    except FileNotFoundError as e:
        return None, f"Tool missing: {e} — run: sudo apt-get install xdotool imagemagick"
    except Exception as e:
        return None, str(e)
