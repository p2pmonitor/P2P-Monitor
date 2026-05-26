"""
error_rules.py — Remote error rule loader for P2P Monitor v1.5.0

Owns loading, validating, caching, and compiling error rule data.
reader.py imports compiled rule sets from here via get_rules().

Fallback priority:
  1. GitHub remote  — fetched on startup in background thread
  2. Local cache    — ~/.p2p_monitor/error_rules_cache.json (last valid remote download)
  3. Packaged JSON  — error_rules.json bundled with the app (repo root / _MEIPASS)
  4. Emergency      — tiny hardcoded fallback; monitor never crashes if all else fails

No network calls happen at import time.
"""

import json
import re
import sys
import threading
from pathlib import Path

# ── Remote URL ────────────────────────────────────────────────────────────────
REMOTE_URL    = "https://raw.githubusercontent.com/p2pmonitor/P2P-Monitor/main/error_rules.json"
CACHE_FILE    = Path.home() / ".p2p_monitor" / "error_rules_cache.json"
FETCH_TIMEOUT = 8  # seconds

# ── Required fields per rule type ─────────────────────────────────────────────
_TRIGGER_REQUIRED = {'key', 'pattern', 'threshold', 'window_sec', 'dedupe_sec', 'label'}
_LOCK_REQUIRED    = {'pattern', 'label'}

# ── Emergency fallback — used only if packaged JSON is missing/corrupt ────────
# Intentionally minimal: the monitor should never crash because of a missing
# error_rules.json, but we don't duplicate the full rule set here.
_EMERGENCY = {
    "version": 0,
    "error_triggers": [],
    "lock_reason_patterns": [],
    "silent_lock_names": [],
}

# ── In-memory compiled rule store ─────────────────────────────────────────────
_rules_lock = threading.Lock()
_compiled   = None   # None until first get_rules() call or fetch completes


# ── Packaged JSON path helper ─────────────────────────────────────────────────

def _packaged_json_path():
    """
    Return the Path to the packaged error_rules.json.
    Works in three environments:
      - PyInstaller frozen:  sys._MEIPASS / error_rules.json
      - Running from source: repo root (same dir as p2p_monitor.py / this file's parent)
      - Installed to ~/.p2p_monitor: ~/.p2p_monitor/error_rules.json
    Returns the first path that exists, or None.
    """
    candidates = []

    # PyInstaller frozen — files extracted to _MEIPASS temp dir
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / 'error_rules.json')

    # Source / installed — relative to this file's location (py/error_rules.py → ../)
    candidates.append(Path(__file__).parent.parent / 'error_rules.json')

    # Installed copy in config dir
    candidates.append(Path.home() / '.p2p_monitor' / 'error_rules.json')

    for path in candidates:
        if path.exists():
            return path
    return None


# ── Validation & compilation ──────────────────────────────────────────────────

def _parse_flags(flags_list):
    """Convert list of flag name strings to combined re flag int."""
    result = 0
    for f in (flags_list or []):
        flag = getattr(re, f, None)
        if flag is None:
            raise ValueError(f"Unknown regex flag: {f!r}")
        result |= flag
    return result


def _validate_and_compile(data):
    """
    Validate a parsed rules dict and compile all regex patterns.
    Returns compiled rules dict on success.
    Raises ValueError with a descriptive message on any validation failure.
    Never executes remote code — only compiles regex strings locally.
    Unknown top-level keys are ignored.
    """
    if not isinstance(data, dict):
        raise ValueError("Rules JSON must be a dict")

    # ── error_triggers ────────────────────────────────────────────────────────
    triggers_raw = data.get('error_triggers', [])
    if not isinstance(triggers_raw, list):
        raise ValueError("error_triggers must be a list")
    compiled_triggers = []
    for i, t in enumerate(triggers_raw):
        if not isinstance(t, dict):
            raise ValueError(f"error_triggers[{i}] must be a dict")
        missing = _TRIGGER_REQUIRED - t.keys()
        if missing:
            raise ValueError(f"error_triggers[{i}] missing fields: {missing}")
        flags = _parse_flags(t.get('flags', ['IGNORECASE']))
        try:
            pattern = re.compile(t['pattern'], flags)
        except re.error as e:
            raise ValueError(f"error_triggers[{i}] bad regex {t['pattern']!r}: {e}")
        compiled_triggers.append((
            t['key'],
            pattern,
            int(t['threshold']),
            int(t['window_sec']),
            int(t['dedupe_sec']),
            t['label'],
        ))

    # ── lock_reason_patterns ──────────────────────────────────────────────────
    lock_raw = data.get('lock_reason_patterns', [])
    if not isinstance(lock_raw, list):
        raise ValueError("lock_reason_patterns must be a list")
    compiled_lock = []
    for i, p in enumerate(lock_raw):
        if not isinstance(p, dict):
            raise ValueError(f"lock_reason_patterns[{i}] must be a dict")
        missing = _LOCK_REQUIRED - p.keys()
        if missing:
            raise ValueError(f"lock_reason_patterns[{i}] missing fields: {missing}")
        flags = _parse_flags(p.get('flags', ['IGNORECASE']))
        try:
            pattern = re.compile(p['pattern'], flags)
        except re.error as e:
            raise ValueError(f"lock_reason_patterns[{i}] bad regex {p['pattern']!r}: {e}")
        compiled_lock.append((pattern, p['label']))

    # ── silent_lock_names ─────────────────────────────────────────────────────
    silent_raw = data.get('silent_lock_names', [])
    if not isinstance(silent_raw, list):
        raise ValueError("silent_lock_names must be a list")
    for i, name in enumerate(silent_raw):
        if not isinstance(name, str):
            raise ValueError(f"silent_lock_names[{i}] must be a string")
    compiled_silent = {n.lower() for n in silent_raw}

    return {
        'version':              data.get('version', 0),
        'error_triggers':       compiled_triggers,
        'lock_reason_patterns': compiled_lock,
        'silent_lock_names':    compiled_silent,
    }


# ── File helpers ──────────────────────────────────────────────────────────────

def _load_json_file(path):
    """Load and return parsed JSON from a Path, or None on any failure."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    """Write raw rules dict to cache file. Fails silently."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def get_rules():
    """
    Return the current compiled rule set.
    On first call, loads packaged JSON/emergency fallback.
    Never overwrites rules if the background fetch completed first.
    """
    global _compiled

    with _rules_lock:
        if _compiled is not None:
            return _compiled

    initial = _load_initial()

    with _rules_lock:
        if _compiled is None:
            _compiled = initial
        return _compiled


def _load_initial(log_fn=None, debug=False):
    """
    Load initial rules synchronously at first get_rules() call.
    Tries packaged JSON first, then emergency fallback.
    Does not touch the cache or network.
    """
    def _dbg(msg):
        if debug and log_fn:
            log_fn(msg)

    pkg_path = _packaged_json_path()
    if pkg_path:
        data = _load_json_file(pkg_path)
        if data is not None:
            try:
                compiled = _validate_and_compile(data)
                _dbg(f'[ERROR_RULES] Initial load from packaged JSON ({pkg_path})')
                return compiled
            except ValueError:
                pass

    # Emergency fallback
    _dbg('[ERROR_RULES] Packaged JSON unavailable — using emergency fallback (no error triggers)')
    return _validate_and_compile(_EMERGENCY)


def fetch_and_apply_rules(log_fn=None, debug=False):
    """
    Fetch remote rules from GitHub, validate, compile, and replace in-memory rules.
    Falls back to: cache → packaged JSON → emergency fallback.
    Called once at app startup in a background thread.
    Never blocks the UI. Never raises.
    """
    global _compiled

    def _log(msg):
        if log_fn:
            log_fn(msg)

    def _dbg(msg):
        if debug and log_fn:
            log_fn(msg)

    def _apply(compiled, raw=None, source=''):
        global _compiled
        with _rules_lock:
            _compiled = compiled
        if raw is not None:
            _save_cache(raw)
        _dbg(f'[ERROR_RULES] {source} (version {compiled["version"]}, '
             f'{len(compiled["error_triggers"])} triggers)')

    # ── 1. Try remote ─────────────────────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(
            REMOTE_URL,
            headers={'User-Agent': 'P2PMonitor-ErrorRules/1.0'},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        compiled = _validate_and_compile(raw)
        _apply(compiled, raw=raw, source='Loaded from remote')
        return
    except Exception as e:
        _dbg(f'[ERROR_RULES] Remote fetch failed: {e}')

    # ── 2. Try cache ──────────────────────────────────────────────────────────
    cached = _load_json_file(CACHE_FILE)
    if cached is not None:
        try:
            compiled = _validate_and_compile(cached)
            _apply(compiled, source='Loaded from cache')
            return
        except ValueError as e:
            _dbg(f'[ERROR_RULES] Cache invalid: {e}')

    # ── 3. Try packaged JSON ──────────────────────────────────────────────────
    pkg_path = _packaged_json_path()
    if pkg_path:
        data = _load_json_file(pkg_path)
        if data is not None:
            try:
                compiled = _validate_and_compile(data)
                _apply(compiled, source=f'Loaded from packaged JSON ({pkg_path.name})')
                return
            except ValueError as e:
                _dbg(f'[ERROR_RULES] Packaged JSON invalid: {e}')

    # ── 4. Emergency fallback ─────────────────────────────────────────────────
    _log('[ERROR_RULES] All sources failed — using emergency fallback (no error triggers). '
         'Error detection is disabled until next restart with a valid error_rules.json.')
    compiled = _validate_and_compile(_EMERGENCY)
    _apply(compiled, source='Emergency fallback')


def start_background_fetch(log_fn=None, debug=False):
    """
    Spawn a daemon thread to fetch and apply remote rules.
    Returns immediately — rules update in-memory when the fetch completes.
    Safe to call from App.__init__ after config is loaded.
    """
    threading.Thread(
        target=fetch_and_apply_rules,
        kwargs={'log_fn': log_fn, 'debug': debug},
        daemon=True,
        name='error-rules-fetch',
    ).start()
