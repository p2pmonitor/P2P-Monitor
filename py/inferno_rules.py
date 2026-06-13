"""
inferno_rules.py — Remote Inferno pattern loader for P2P Monitor v1.7.0

Owns loading, validating, caching, and making available the Inferno pattern config.
py/inferno.py imports patterns from here via get_patterns().

Fallback priority:
  1. GitHub remote  — fetched on startup in background thread
  2. Local cache    — ~/.p2p_monitor/inferno_patterns_cache.json
  3. Packaged JSON  — inferno_patterns.json bundled with the app (repo root / _MEIPASS)
  4. Emergency      — built-in minimal pattern dict; monitor never crashes if all else fails

No network calls happen at import time.
"""

import json
import re
import sys
import threading
from pathlib import Path

# ── Remote URL ────────────────────────────────────────────────────────────────
REMOTE_URL    = "https://raw.githubusercontent.com/p2pmonitor/P2P-Monitor/main/inferno_patterns.json"
CACHE_FILE    = Path.home() / ".p2p_monitor" / "inferno_patterns_cache.json"
FETCH_TIMEOUT = 8  # seconds

# ── Emergency fallback ────────────────────────────────────────────────────────
# Intentionally minimal: all patterns are valid regex; milestone list is complete.
# The monitor should never crash because inferno_patterns.json is missing.
_EMERGENCY = {
    "version": 0,
    "gear_check_start_patterns":  [
        "You have the stats and quests needed for Inferno",
        "Possible gear clear for Infernal Cape",
    ],
    "gear_check_pass_patterns":   [
        "You have the gear needed for Inferno",
        "All checks passed",
    ],
    "requirements_failed_patterns": ["Inferno requirements not met"],
    "resource_check_failed_pattern": r"Resource check failed\s+(\S+)\s+\[(.+?)\]",
    "suspicious_failure_pattern": r"(?i)(inferno.*not met|requirements not met|resource check failed|can't|cannot|unable|failed|missing|not enough|could not)",
    "ping_pattern":               r"Ping is\s+(\d+)\s+needs to be\s+(\d+)\s+or less for Inferno",
    "bad_ping_override_patterns": ["ALLOW BAD PING"],
    "wave_game_pattern":          r"\[GAME\].*?Wave:\s*(\d+)",
    "wave_internal_pattern":      r"\bWAVE\s+(\d+)\b",
    "death_patterns":             [r"\[GAME\] You have been defeated!", "Inferno death detected!"],
    "success_pattern":            r"(?i)Your TzKal-Zuk kill count is:\s*(?:<[^>]+>)*(\d+)",
    "gear_check_reset_patterns":  ["Resetting via Startup", "NEW TASK", "Stopped P2P Master AI"],
    "milestone_waves":            [7, 15, 24, 31, 41, 48, 56, 63, 67, 68, 69],
    "gear_check_window_timeout_sec": 300,
    "resource_detail_cap": 6,
}

# ── In-memory pattern store ───────────────────────────────────────────────────
_patterns_lock = threading.Lock()
_patterns      = None   # None until first get_patterns() call or fetch completes


# ── Packaged JSON path helper ─────────────────────────────────────────────────

def _packaged_json_path():
    """
    Return the Path to the packaged inferno_patterns.json.
    Works in three environments:
      - PyInstaller frozen:  sys._MEIPASS / inferno_patterns.json
      - Running from source: repo root (same dir as p2p_monitor.py / this file's parent)
      - Installed to ~/.p2p_monitor: ~/.p2p_monitor/inferno_patterns.json
    Returns the first path that exists, or None.
    """
    candidates = []

    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / 'inferno_patterns.json')

    # Source / installed — py/inferno_rules.py → ../inferno_patterns.json
    candidates.append(Path(__file__).parent.parent / 'inferno_patterns.json')

    candidates.append(Path.home() / '.p2p_monitor' / 'inferno_patterns.json')

    for path in candidates:
        if path.exists():
            return path
    return None


# ── Validation ────────────────────────────────────────────────────────────────

_REQUIRED_KEYS = {
    'gear_check_start_patterns', 'gear_check_pass_patterns',
    'requirements_failed_patterns', 'resource_check_failed_pattern',
    'ping_pattern', 'bad_ping_override_patterns',
    'wave_game_pattern', 'wave_internal_pattern',
    'death_patterns', 'success_pattern',
    'gear_check_reset_patterns', 'milestone_waves',
}


def _validate(data):
    """
    Validate a parsed inferno patterns dict. Compiles all regex strings to catch
    malformed patterns early. Returns the validated dict on success.
    Raises ValueError with a descriptive message on failure.
    """
    if not isinstance(data, dict):
        raise ValueError("Inferno patterns JSON must be a dict")

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"Inferno patterns JSON missing required keys: {missing}")

    # Validate all regex fields
    _single_patterns = [
        'resource_check_failed_pattern', 'ping_pattern',
        'wave_game_pattern', 'wave_internal_pattern', 'success_pattern',
    ]
    for key in _single_patterns:
        val = data.get(key)
        if not isinstance(val, str):
            raise ValueError(f"{key} must be a string")
        try:
            re.compile(val, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"{key} bad regex {val!r}: {e}")

    _list_patterns = [
        'gear_check_start_patterns', 'gear_check_pass_patterns',
        'requirements_failed_patterns', 'bad_ping_override_patterns',
        'death_patterns', 'gear_check_reset_patterns',
    ]
    for key in _list_patterns:
        val = data.get(key)
        if not isinstance(val, list):
            raise ValueError(f"{key} must be a list")
        for i, pat in enumerate(val):
            if not isinstance(pat, str):
                raise ValueError(f"{key}[{i}] must be a string")
            try:
                re.compile(pat, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"{key}[{i}] bad regex {pat!r}: {e}")

    # Validate optional suspicious_failure_pattern if present
    sus = data.get('suspicious_failure_pattern')
    if sus is not None:
        if not isinstance(sus, str):
            raise ValueError("suspicious_failure_pattern must be a string")
        try:
            re.compile(sus, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"suspicious_failure_pattern bad regex {sus!r}: {e}")

    milestones = data.get('milestone_waves')
    if not isinstance(milestones, list) or not all(isinstance(m, int) for m in milestones):
        raise ValueError("milestone_waves must be a list of integers")

    return data


# ── File helpers ──────────────────────────────────────────────────────────────

def _load_json_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def get_patterns():
    """
    Return the current validated pattern dict.
    On first call, loads packaged JSON or emergency fallback synchronously.
    Never overwrites patterns if the background fetch already completed.
    """
    global _patterns

    with _patterns_lock:
        if _patterns is not None:
            return _patterns

    initial = _load_initial()

    with _patterns_lock:
        if _patterns is None:
            _patterns = initial
        return _patterns


def _load_initial(log_fn=None, debug=False):
    """Load synchronously from packaged JSON or emergency fallback. No network."""
    def _dbg(msg):
        if debug and log_fn:
            log_fn(msg)

    pkg_path = _packaged_json_path()
    if pkg_path:
        data = _load_json_file(pkg_path)
        if data is not None:
            try:
                validated = _validate(data)
                _dbg(f'[INFERNO_RULES] Initial load from packaged JSON ({pkg_path})')
                return validated
            except ValueError as e:
                _dbg(f'[INFERNO_RULES] Packaged JSON invalid: {e}')

    _dbg('[INFERNO_RULES] Packaged JSON unavailable — using emergency fallback')
    return _validate(_EMERGENCY)


def fetch_and_apply_patterns(log_fn=None, debug=False):
    """
    Fetch remote patterns from GitHub, validate, and replace in-memory patterns.
    Falls back to: cache → packaged JSON → emergency fallback.
    Called once at app startup in a background thread. Never blocks the UI.
    Never raises.
    """
    global _patterns

    def _log(msg):
        if log_fn:
            log_fn(msg)

    def _dbg(msg):
        if debug and log_fn:
            log_fn(msg)

    def _apply(validated, raw=None, source=''):
        global _patterns
        with _patterns_lock:
            _patterns = validated
        if raw is not None:
            _save_cache(raw)
        _dbg(f'[INFERNO_RULES] {source} (version {validated.get("version", 0)}, '
             f'{len(validated.get("milestone_waves", []))} milestones)')

    # ── 1. Try remote ─────────────────────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(
            REMOTE_URL,
            headers={'User-Agent': 'P2PMonitor-InfernoRules/1.0'},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        validated = _validate(raw)
        _apply(validated, raw=raw, source='Loaded from remote')
        return
    except Exception as e:
        _dbg(f'[INFERNO_RULES] Remote fetch failed: {e}')

    # ── 2. Try cache ──────────────────────────────────────────────────────────
    cached = _load_json_file(CACHE_FILE)
    if cached is not None:
        try:
            validated = _validate(cached)
            _apply(validated, source='Loaded from cache')
            return
        except ValueError as e:
            _dbg(f'[INFERNO_RULES] Cache invalid: {e}')

    # ── 3. Try packaged JSON ──────────────────────────────────────────────────
    pkg_path = _packaged_json_path()
    if pkg_path:
        data = _load_json_file(pkg_path)
        if data is not None:
            try:
                validated = _validate(data)
                _apply(validated, source=f'Loaded from packaged JSON ({pkg_path.name})')
                return
            except ValueError as e:
                _dbg(f'[INFERNO_RULES] Packaged JSON invalid: {e}')

    # ── 4. Emergency fallback ─────────────────────────────────────────────────
    _log('[INFERNO_RULES] All sources failed — using emergency fallback.')
    validated = _validate(_EMERGENCY)
    _apply(validated, source='Emergency fallback')


def start_background_fetch(log_fn=None, debug=False):
    """
    Spawn a daemon thread to fetch and apply remote Inferno patterns.
    Returns immediately — patterns update in-memory when the fetch completes.
    Safe to call from App.__init__ after config is loaded.
    """
    threading.Thread(
        target=fetch_and_apply_patterns,
        kwargs={'log_fn': log_fn, 'debug': debug},
        daemon=True,
        name='inferno-rules-fetch',
    ).start()
