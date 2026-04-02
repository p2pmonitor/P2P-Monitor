"""
util.py — Shared utilities for P2P Monitor
Formatting helpers, xdotool wrappers, and break-length parsing
used across py/ and ui/ layers. stdlib only.
"""

import os
import re
import subprocess
from datetime import datetime


def now_str():
    """Return current time as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def fmt_ts(ts_str):
    """Format a log timestamp string to 'MM/DD/YY HH:MM'."""
    try:
        dt = datetime.strptime(ts_str[:16], '%Y-%m-%d %H:%M')
        return dt.strftime('%m/%d/%y %H:%M')
    except Exception:
        return ts_str


# ── xdotool helpers (shared by screenshot.py and paint.py) ─────────────────────

def get_display_env():
    """Return os.environ with DISPLAY set for X11 operations."""
    return {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':0')}


def xdotool(args, env=None, timeout=3):
    """Run an xdotool command. Returns stdout string or '' on failure."""
    if env is None:
        env = get_display_env()
    try:
        r = subprocess.run(['xdotool'] + args, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.stdout.strip()
    except Exception:
        return ''


def get_window_geom(wid, env=None):
    """Returns (x, y, w, h) for a window ID, or None on failure."""
    if env is None:
        env = get_display_env()
    try:
        out = xdotool(['getwindowgeometry', '--shell', wid], env)
        d   = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        geom = (int(d.get('X', 0)), int(d.get('Y', 0)),
                int(d.get('WIDTH', 0)), int(d.get('HEIGHT', 0)))
        return geom if geom[2] and geom[3] else None
    except Exception:
        return None


# ── Break length parser (shared by reader.py and watcher.py) ───────────────────

_BREAK_LENGTH_RE = re.compile(r'[Bb]reak\s+length\s+(\d+)')


def parse_break_length_ms(lines, start_idx=0, max_search=25):
    """Search lines for 'Break length N' (milliseconds). Returns int or None.
    Searches from start_idx forward up to max_search lines."""
    end = min(len(lines), start_idx + max_search)
    for i in range(start_idx, end):
        line = lines[i] if isinstance(lines[i], str) else ''
        m = _BREAK_LENGTH_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def format_break_duration(ms):
    """Format break duration in milliseconds as 'Xh Ym Zs' string."""
    h, rem = divmod(ms, 3600000)
    mn, s  = divmod(rem, 60000)
    s //= 1000
    parts = ([f"{h}h"] if h else []) + ([f"{mn}m"] if mn or h else []) + [f"{s}s"]
    return ' '.join(parts)
