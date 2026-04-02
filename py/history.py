"""
history.py — History file I/O for P2P Monitor
All reads, writes, rotation, and migration live here.
stdlib only: json, pathlib, datetime, re.
"""

import json
import re
from datetime import datetime
from pathlib import Path

HISTORY_DIR  = Path.home() / ".p2p_monitor" / "history"
HISTORY_FILE = Path.home() / ".p2p_monitor" / "history.jsonl"  # legacy flat — migrated on first run
OFFSETS_FILE = Path.home() / ".p2p_monitor" / "offsets.json"   # live resume positions — flushed on clean shutdown only
HISTORY_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB

def _safe_name(account):
    return re.sub(r'[^\w\-. ]', '_', account).strip()

def account_history_dir(account):
    return HISTORY_DIR / _safe_name(account)

def history_file(account):
    return account_history_dir(account) / "history.jsonl"

# ── Resume offsets ─────────────────────────────────────────────────────────────
def load_offsets():
    """Load {filename: byte_offset} from offsets.json. Returns empty dict if missing/corrupt."""
    try:
        if OFFSETS_FILE.exists():
            with open(OFFSETS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}

def save_offsets(offsets):
    """Flush {filename: byte_offset} to offsets.json. Called only on clean shutdown."""
    try:
        OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OFFSETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(offsets, f)
    except Exception:
        pass

# ── Migration ──────────────────────────────────────────────────────────────────
def migrate_history():
    """Migrate legacy flat history files to per-account subfolders."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    try:
        old_root = Path.home() / ".p2p_monitor"
        for old_file in list(old_root.glob("history_*.jsonl")) + list(HISTORY_DIR.glob("history_*.jsonl")):
            try:
                stem = old_file.stem
                acc_safe = stem[len('history_'):] if stem.startswith('history_') else stem
                if not acc_safe:
                    continue
                acc_dir = HISTORY_DIR / acc_safe
                acc_dir.mkdir(parents=True, exist_ok=True)
                dest = acc_dir / "history.jsonl"
                if not dest.exists():
                    old_file.rename(dest)
                else:
                    with open(old_file, 'r', encoding='utf-8') as src, \
                         open(dest, 'a', encoding='utf-8') as dst:
                        dst.write(src.read())
                    old_file.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    if not HISTORY_FILE.exists():
        return
    try:
        rows = []
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        if not rows:
            HISTORY_FILE.rename(HISTORY_FILE.with_suffix('.jsonl.bak'))
            return
        grouped = {}
        for r in rows:
            acc = r.get('account', 'Unknown').strip() or 'Unknown'
            r['account'] = acc
            grouped.setdefault(acc, []).append(r)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        for acc, entries in grouped.items():
            dest = history_file(acc)
            with open(dest, 'a', encoding='utf-8') as f:
                for e in entries:
                    f.write(json.dumps(e) + '\n')
        HISTORY_FILE.rename(HISTORY_FILE.with_suffix('.jsonl.bak'))
    except Exception:
        pass

# ── Write ──────────────────────────────────────────────────────────────────────
def _rotate_if_needed(account):
    hf = history_file(account)
    try:
        if hf.exists() and hf.stat().st_size >= HISTORY_MAX_BYTES:
            ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            dated  = account_history_dir(account) / f"history_{ts_str}.jsonl"
            hf.rename(dated)
    except Exception:
        pass

def append_history(account, etype, value, activity='', timestamp=None):
    account_history_dir(account).mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(account)
    ts_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = {
        "time":     timestamp or ts_now,
        "account":  account,
        "type":     etype,
        "value":    value,
        "activity": activity,
    }
    try:
        with open(history_file(account), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass

def record_log_scanned(account, log_filename):
    """Record that a log file has been fully backfilled for this account.
    Only called for rotated (completed) files — active file resume uses offsets.json."""
    account_history_dir(account).mkdir(parents=True, exist_ok=True)
    rec = {'type': 'scan', 'file': log_filename}
    try:
        with open(history_file(account), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass

# ── Read ───────────────────────────────────────────────────────────────────────
def get_scanned_logs(account):
    """Return set of filenames already fully backfilled for this account.
    Reads all history files (active + rotated) so rotation doesn't re-trigger backfill.
    Resume offsets are now stored separately in offsets.json, not here."""
    acc_dir = account_history_dir(account)
    if not acc_dir.exists():
        return set()
    scanned = set()
    files = list(acc_dir.glob('history*.jsonl'))
    for hf in files:
        try:
            with open(hf, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                        if rec.get('type') == 'scan':
                            fname = rec.get('file', '')
                            if fname:
                                scanned.add(fname)
                    except Exception:
                        pass
        except Exception:
            pass
    return scanned

def load_history_tail(account, cutoff_ts):
    """Read only entries >= cutoff_ts. Uses backwards seek to find the cutoff position,
    then reads forward from there — avoids loading the entire file."""
    hf = history_file(account)
    if not hf.exists():
        return []
    rows = []
    try:
        with open(hf, 'rb') as f:
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []
            chunk_size = 32768
            pos        = file_size
            remainder  = b''
            cutoff_pos = 0   # byte position where we should start the forward read
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size) + remainder
                lines = chunk.split(b'\n')
                remainder = lines[0]
                for raw in reversed(lines[1:]):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        ts  = rec.get('time', '')
                        if ts and ts < cutoff_ts:
                            # Found the first entry before cutoff — start reading from here
                            cutoff_pos = pos
                            pos = 0
                            break
                    except Exception:
                        continue
            # Forward read from cutoff_pos to end
            f.seek(cutoff_pos)
            for line in f.read().decode('utf-8', errors='replace').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts  = rec.get('time', '')
                    if ts >= cutoff_ts:
                        rows.append(rec)
                except Exception:
                    pass
    except Exception:
        rows = load_history_for(account)
        rows = [r for r in rows if r.get('time', '') >= cutoff_ts]
    return rows

def load_history_for(account):
    """Load all history entries for a single account, including rotated files."""
    acc_dir = account_history_dir(account)
    if not acc_dir.exists():
        return []
    # Read active file + all rotated history_*.jsonl files, oldest first
    files = sorted(acc_dir.glob('history*.jsonl'), key=lambda f: f.stat().st_mtime)
    rows = []
    for hf in files:
        try:
            with open(hf, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    return rows

def load_history_accounts():
    """Return list of account names that have history subfolders."""
    accounts = []
    if not HISTORY_DIR.exists():
        return accounts
    for d in sorted(HISTORY_DIR.iterdir()):
        if d.is_dir() and (d / 'history.jsonl').exists():
            accounts.append(d.name)
    return accounts

