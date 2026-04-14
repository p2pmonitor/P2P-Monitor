"""
config.py — Config I/O for P2P Monitor
Owns CONFIG_FILE path, save_config(), and load_config().
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
