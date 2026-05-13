"""
config.py — Config I/O for P2P Monitor v1.4.0
Owns CONFIG_FILE path, save_config(), load_config(), and sanitize_config().
stdlib only — no imports from other py/ modules.
"""

import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".p2p_monitor" / "config.json"


def save_config(cfg, log_fn=None, debug=False):
    """Write cfg dict to ~/.p2p_monitor/config.json."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] save_config failed for {CONFIG_FILE}: {e}')


def load_config(defaults, log_fn=None, debug=False):
    """Load config from disk, merging with defaults. Returns merged dict."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] load_config mkdir failed: {e}')
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return {**defaults, **json.load(f)}
        except Exception as e:
            if debug and log_fn:
                log_fn(f'[DEBUG] load_config failed for {CONFIG_FILE}, using defaults: {e}')
    return dict(defaults)


# ── Config sanitization ───────────────────────────────────────────────────────

# Channel names that bot setup creates — used to validate bot_channel_ids,
# bot_webhook_urls, and bot_thread_ids sub-dicts.
_VALID_CHANNEL_NAMES = frozenset([
    'monitor', 'tasks', 'quests', 'chat', 'errors', 'drops', 'deaths', 'levelup',
])


def sanitize_config(cfg, defaults, logs_root=None, log_fn=None, debug=False):
    """
    Clean up config in-place:
      1. Remove keys not present in defaults (stale / user-added junk).
      2. Validate types — coerce where safe, remove where not.
      3. Validate bot_channel_ids / bot_webhook_urls entries against known channel names.
      4. Validate bot_thread_ids structure: {account: {ch_name: id}}.
      5. Prune bot_thread_ids for accounts whose log folders no longer exist
         (only when logs_root is set and the directory exists).
    Returns the number of corrections made.
    """
    def _log(msg):
        if debug and log_fn:
            log_fn(msg)

    corrections = 0

    # ── 1. Remove unknown top-level keys ──────────────────────────────────────
    unknown_keys = [k for k in list(cfg.keys()) if k not in defaults]
    for k in unknown_keys:
        del cfg[k]
        corrections += 1
        _log(f'[CONFIG] Removed unknown key: {k}')

    # ── 2. Type validation ────────────────────────────────────────────────────
    for key, default_val in defaults.items():
        if key not in cfg:
            continue
        val = cfg[key]
        expected_type = type(default_val)

        # dict fields — validated deeper in steps 3-4
        if expected_type is dict:
            if not isinstance(val, dict):
                _log(f'[CONFIG] Reset {key}: expected dict, got {type(val).__name__}')
                cfg[key] = dict(default_val)
                corrections += 1
            continue

        # list fields (muted_accounts, launcher_presets, etc.)
        if expected_type is list:
            if not isinstance(val, list):
                _log(f'[CONFIG] Reset {key}: expected list, got {type(val).__name__}')
                cfg[key] = list(default_val)
                corrections += 1
            continue

        # bool fields
        if expected_type is bool:
            if not isinstance(val, bool):
                # Coerce int 0/1 silently; anything else reset
                if isinstance(val, int) and val in (0, 1):
                    cfg[key] = bool(val)
                    corrections += 1
                else:
                    _log(f'[CONFIG] Reset {key}: expected bool, got {type(val).__name__}')
                    cfg[key] = default_val
                    corrections += 1
            continue

        # int fields
        if expected_type is int:
            if isinstance(val, bool):
                # bool is a subclass of int in Python — treat as wrong type
                cfg[key] = default_val
                corrections += 1
                _log(f'[CONFIG] Reset {key}: expected int, got bool')
            elif not isinstance(val, int):
                try:
                    cfg[key] = int(val)
                    corrections += 1
                except (ValueError, TypeError):
                    _log(f'[CONFIG] Reset {key}: could not coerce {val!r} to int')
                    cfg[key] = default_val
                    corrections += 1
            continue

        # str fields
        if expected_type is str:
            if not isinstance(val, str):
                cfg[key] = str(val) if val is not None else default_val
                corrections += 1
            continue

    # ── 3. Validate bot_channel_ids and bot_webhook_urls ──────────────────────
    for dict_key in ('bot_channel_ids', 'bot_webhook_urls'):
        d = cfg.get(dict_key, {})
        if not isinstance(d, dict):
            cfg[dict_key] = {}
            corrections += 1
            continue
        bad_names = [k for k in d if k not in _VALID_CHANNEL_NAMES]
        for k in bad_names:
            del d[k]
            corrections += 1
            _log(f'[CONFIG] Removed invalid channel name {k!r} from {dict_key}')
        # Validate values are non-empty strings
        bad_vals = [k for k, v in d.items() if not isinstance(v, str) or not v.strip()]
        for k in bad_vals:
            del d[k]
            corrections += 1
            _log(f'[CONFIG] Removed empty/invalid entry {k!r} from {dict_key}')

    # ── 4. Validate bot_thread_ids structure ──────────────────────────────────
    thread_ids = cfg.get('bot_thread_ids', {})
    if not isinstance(thread_ids, dict):
        cfg['bot_thread_ids'] = {}
        corrections += 1
    else:
        bad_accounts = []
        for account, threads in list(thread_ids.items()):
            if not isinstance(threads, dict):
                bad_accounts.append(account)
                continue
            # Remove thread entries with invalid channel names
            bad_ch = [k for k in threads if k not in _VALID_CHANNEL_NAMES]
            for k in bad_ch:
                del threads[k]
                corrections += 1
                _log(f'[CONFIG] Removed invalid thread channel {k!r} for account {account!r}')
            # Remove thread entries with non-string or empty IDs
            bad_ids = [k for k, v in threads.items()
                       if not isinstance(v, (str, int)) or not str(v).strip()]
            for k in bad_ids:
                del threads[k]
                corrections += 1
                _log(f'[CONFIG] Removed empty thread ID for {account!r}/{k!r}')
            # Normalize int IDs to strings
            for k, v in threads.items():
                if isinstance(v, int):
                    threads[k] = str(v)
        for account in bad_accounts:
            del thread_ids[account]
            corrections += 1
            _log(f'[CONFIG] Removed malformed thread entry for account {account!r}')

    # ── 5. Prune thread IDs for nonexistent account folders ───────────────────
    if logs_root:
        logs_path = Path(logs_root)
        if logs_path.is_dir():
            existing_folders = {f.name for f in logs_path.iterdir() if f.is_dir()}
            stale_accounts = [a for a in thread_ids if a not in existing_folders]
            for account in stale_accounts:
                del thread_ids[account]
                corrections += 1
                _log(f'[CONFIG] Pruned threads for missing account folder: {account!r}')

    if corrections:
        _log(f'[CONFIG] Sanitization complete — {corrections} correction(s)')

    return corrections
