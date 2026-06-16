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
def get_last_seen(account):
    """Return the last log line seen by backfill for this account, or None."""
    offsets = load_offsets()
    return offsets.get(f'{account}__last_seen')


def set_last_seen(account, line):
    """Store the last log line seen by backfill for this account.
    Merges only the __last_seen key into the existing offsets file rather than
    doing a full load+save cycle — avoids unnecessary disk reads on every poll tick.
    """
    key = f'{account}__last_seen'
    try:
        data = {}
        if OFFSETS_FILE.exists():
            try:
                with open(OFFSETS_FILE, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
            except Exception:
                pass
        if data.get(key) == line:
            return  # already current — skip the write entirely
        data[key] = line
        OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OFFSETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception:
        pass  # best-effort — backfill will re-process on next startup if lost


def load_offsets(log_fn=None, debug=False):
    """Load {filename: byte_offset} from offsets.json. Returns empty dict if missing/corrupt."""
    try:
        if OFFSETS_FILE.exists():
            with open(OFFSETS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] load_offsets failed for {OFFSETS_FILE}: {e}')
    return {}

def save_offsets(offsets, log_fn=None, debug=False):
    """Flush {filename: byte_offset} to offsets.json. Called only on clean shutdown."""
    try:
        OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OFFSETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(offsets, f)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] save_offsets failed for {OFFSETS_FILE}: {e}')

# ── Migration ──────────────────────────────────────────────────────────────────
def migrate_history(log_fn=None, debug=False):
    """Migrate legacy flat history files to per-account subfolders."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] migrate_history mkdir failed: {e}')
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
            except Exception as e:
                if debug and log_fn:
                    log_fn(f'[DEBUG] migrate_history file move failed for {old_file}: {e}')
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] migrate_history glob failed: {e}')
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
                    except Exception as e:
                        if debug and log_fn:
                            log_fn(f'[DEBUG] migrate_history parse error: {e}')
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
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] migrate_history flat-file migration failed: {e}')

# ── Write ──────────────────────────────────────────────────────────────────────
def _rotate_if_needed(account, log_fn=None, debug=False):
    hf = history_file(account)
    try:
        if hf.exists() and hf.stat().st_size >= HISTORY_MAX_BYTES:
            ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            dated  = account_history_dir(account) / f"history_{ts_str}.jsonl"
            hf.rename(dated)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] _rotate_if_needed failed for {account}: {e}')

def append_history(account, etype, value, activity='', timestamp=None, log_fn=None, debug=False):
    try:
        account_history_dir(account).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] append_history mkdir failed for {account}: {e}')
        return
    _rotate_if_needed(account, log_fn=log_fn, debug=debug)
    ts_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = {
        "time":     timestamp or ts_now,
        "account":  account,
        "type":     etype,
        "value":    value,
        "activity": activity,
    }
    # Dedup — skip if identical to the last entry (same type, value, activity, time)
    try:
        hf = history_file(account)
        if hf.exists():
            with open(hf, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                if size > 0:
                    f.seek(max(0, size - 512))
                    last_line = f.read().decode('utf-8', errors='replace').strip().splitlines()[-1]
                    try:
                        last = json.loads(last_line)
                        if (last.get('type') == etype and
                                last.get('value') == value and
                                last.get('activity') == activity and
                                last.get('time') == (timestamp or ts_now)):
                            return  # exact duplicate — skip
                    except Exception:
                        pass
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] append_history dedup check failed for {account}: {e}')
    try:
        with open(history_file(account), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] append_history write failed for {account}: {e}')

def record_log_scanned(account, log_filename, log_fn=None, debug=False):
    """Record that a log file has been fully backfilled for this account."""
    try:
        account_history_dir(account).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] record_log_scanned mkdir failed for {account}: {e}')
        return
    rec = {'type': 'scan', 'file': log_filename}
    try:
        with open(history_file(account), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] record_log_scanned write failed for {account}/{log_filename}: {e}')

# ── Read ───────────────────────────────────────────────────────────────────────
def get_scanned_logs(account, log_fn=None, debug=False):
    """Return set of filenames already fully backfilled for this account."""
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
        except Exception as e:
            if debug and log_fn:
                log_fn(f'[DEBUG] get_scanned_logs read failed for {hf}: {e}')
    return scanned

def _dedup_history_file(hf, log_fn=None, debug=False):
    """
    Read a history JSONL file, remove duplicate entries (same time+type+value+activity),
    keeping the first occurrence. Rewrites the file in-place if duplicates are found.
    Returns (rows, dupes_removed) where rows is the deduplicated list.
    """
    if not hf.exists():
        return [], 0
    rows = []
    seen = set()
    dupes = 0
    try:
        with open(hf, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    rows.append(line)
                    continue
                if rec.get('type') == 'scan':
                    rows.append(rec)
                    continue
                key = (rec.get('time', ''), rec.get('type', ''),
                       rec.get('value', ''), rec.get('activity', ''))
                if key in seen:
                    dupes += 1
                else:
                    seen.add(key)
                    rows.append(rec)
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] _dedup_history_file read failed for {hf}: {e}')
        return [], 0

    if dupes > 0:
        try:
            with open(hf, 'w', encoding='utf-8') as f:
                for rec in rows:
                    if isinstance(rec, str):
                        f.write(rec + '\n')
                    else:
                        f.write(json.dumps(rec) + '\n')
        except Exception as e:
            if debug and log_fn:
                log_fn(f'[DEBUG] _dedup_history_file rewrite failed for {hf}: {e}')

    return [r for r in rows if isinstance(r, dict) and r.get('type') != 'scan'], dupes


def load_history_tail(account, cutoff_ts, log_fn=None, debug=False):
    """Read only entries >= cutoff_ts. Uses backwards seek to find the cutoff position."""
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
            cutoff_pos = 0
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
                            cutoff_pos = pos
                            pos = 0
                            break
                    except Exception:
                        continue
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
    except Exception as e:
        if debug and log_fn:
            log_fn(f'[DEBUG] load_history_tail failed for {account}, falling back to full load: {e}')
        rows = load_history_for(account, log_fn=log_fn, debug=debug)
        rows = [r for r in rows if r.get('time', '') >= cutoff_ts]
    return rows

def load_history_for(account, log_fn=None, debug=False):
    """Load all history entries for a single account, including rotated files.
    Deduplicates each file on load and rewrites if duplicates are found."""
    acc_dir = account_history_dir(account)
    if not acc_dir.exists():
        return []
    files = sorted(acc_dir.glob('history*.jsonl'), key=lambda f: f.stat().st_mtime)
    rows = []
    for hf in files:
        clean_rows, dupes = _dedup_history_file(hf, log_fn=log_fn, debug=debug)
        rows.extend(clean_rows)
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


# ── Runtime / break stats ──────────────────────────────────────────────────────

_SCRIPT_START_VALS  = {'start', 'script started', '▶️ script started'}
_SCRIPT_STOP_VALS   = {'stop', 'script stopped', '⏹️ script stopped'}
_SCRIPT_PAUSE_VALS  = {'pause', 'script paused', '⏸️ script paused'}
_SCRIPT_RESUME_VALS = {'resume', 'script resumed', '▶️ script resumed'}
_BREAK_VALS         = {'break'}


def _norm_script_val(rec) -> str:
    """Normalise a script_event or similar record value to a canonical verb."""
    v = (rec.get('value', '') or rec.get('activity', '') or '').strip().lower()
    if any(k in v for k in ('start',)):
        return 'start'
    if any(k in v for k in ('stop',)):
        return 'stop'
    if any(k in v for k in ('pause',)):
        return 'pause'
    if any(k in v for k in ('resume',)):
        return 'resume'
    return v


def _parse_ts(ts_str: str) -> 'float | None':
    """Parse a history timestamp string to epoch float. Returns None on failure."""
    if not ts_str:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(ts_str[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def compute_runtime_stats(account: str,
                           since_ts: 'float | None' = None,
                           until_ts: 'float | None' = None) -> dict:
    """
    Compute runtime stats from history.jsonl for a single account.

    Returns dict with:
      total_run_secs    — time between Script Started/Resumed and Script Paused/Stopped
      break_secs        — actual elapsed break intervals (task/activity == 'break')
      active_secs       — total_run_secs - break_secs
      break_pct         — break_secs / total_run_secs * 100 (0 if no run time)
      account           — account name
      range_from        — earliest timestamp seen (ISO str) or None
      range_to          — latest timestamp seen (ISO str) or None
    """
    rows = load_history_for(account)

    # Filter by date range
    if since_ts or until_ts:
        filtered = []
        for r in rows:
            ts = _parse_ts(r.get('time', ''))
            if ts is None:
                continue
            if since_ts and ts < since_ts:
                continue
            if until_ts and ts > until_ts:
                continue
            filtered.append(r)
        rows = filtered

    if not rows:
        return {'total_run_secs': 0, 'break_secs': 0, 'active_secs': 0,
                'break_pct': 0.0, 'account': account, 'range_from': None, 'range_to': None}

    # Dedupe: skip exact same (time, type, value, activity)
    seen_keys: set = set()
    deduped = []
    for r in rows:
        key = (r.get('time', ''), r.get('type', ''), r.get('value', ''), r.get('activity', ''))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(r)
    rows = deduped

    total_run_secs = 0.0
    break_secs     = 0.0
    run_start      = None   # timestamp of last Script Started/Resumed
    break_start    = None   # timestamp of current break interval
    on_break       = False

    range_from = None
    range_to   = None

    for r in rows:
        rtype = r.get('type', '')
        ts    = _parse_ts(r.get('time', ''))
        if ts is None:
            continue

        if range_from is None:
            range_from = r.get('time', '')
        range_to = r.get('time', '')

        # Script lifecycle events
        if rtype == 'script_event':
            verb = _norm_script_val(r)

            if verb in ('start', 'resume'):
                if on_break and break_start is not None:
                    # Break ended by script restart
                    break_secs += ts - break_start
                    break_start = None
                    on_break    = False
                run_start = ts

            elif verb in ('pause', 'stop'):
                if run_start is not None:
                    total_run_secs += ts - run_start
                    run_start = None
                if on_break and break_start is not None:
                    break_secs += ts - break_start
                    break_start = None
                    on_break    = False

        # Break events
        elif rtype == 'break':
            if run_start is not None and not on_break:
                on_break    = True
                break_start = ts

        # Any task that is not a break ends the break interval
        elif rtype == 'task' and on_break:
            if break_start is not None:
                break_secs += ts - break_start
            break_start = None
            on_break    = False

    # If still running (no stop/pause seen), we do not extrapolate — we only
    # count confirmed intervals so stats don't drift with live sessions.

    active_secs = max(0.0, total_run_secs - break_secs)
    break_pct   = (break_secs / total_run_secs * 100) if total_run_secs > 0 else 0.0

    return {
        'total_run_secs': total_run_secs,
        'break_secs':     break_secs,
        'active_secs':    active_secs,
        'break_pct':      break_pct,
        'account':        account,
        'range_from':     range_from,
        'range_to':       range_to,
    }


def _fmt_secs(secs: float) -> str:
    """Format seconds as 'Xh Ym' or 'Ym Zs' for display."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m = rem // 60
    s = rem % 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
