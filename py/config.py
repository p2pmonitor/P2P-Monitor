"""
config.py — Config I/O for P2P Monitor
Owns CONFIG_FILE path, save_config(), and load_config().
stdlib only — no imports from other py/ modules.
"""

import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".p2p_monitor" / "config.json"


def save_config(cfg):
    """Write cfg dict to ~/.p2p_monitor/config.json."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


def load_config(defaults):
    """Load config from disk, merging with defaults. Returns merged dict."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return dict(defaults)
