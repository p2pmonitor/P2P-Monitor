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


def get_last_seen_meta(account):
    """Structured backfill checkpoint written alongside last_seen:
    {'line': <marker text>, 'file_key': <filename-timestamp sort key>,
     'line_index': <absolute index of the marker line in that file>}.
    Only valid while its 'line' still equals the current last_seen marker —
    a live-loop marker update (which writes no meta) silently invalidates it,
    and resume falls back to a first-occurrence text scan."""
    offsets = load_offsets()
    meta = offsets.get(f'{account}__last_seen_meta')
    return meta if isinstance(meta, dict) else None


def set_last_seen(account, line, file_key=None, line_index=None):
    """Store the last log line seen by backfill for this account.
    Merges only the __last_seen key into the existing offsets file rather than
    doing a full load+save cycle — avoids unnecessary disk reads on every poll tick.

    When file_key and line_index are provided (backfill checkpoints), the
    structured __last_seen_meta checkpoint is written in the SAME merge/write
    — one disk write per checkpoint, not two. When they are omitted (live
    poll loop), any existing meta is left untouched on disk; it self-
    invalidates on read because its stored 'line' no longer matches the
    updated marker (see get_last_seen_meta)."""
    key = f'{account}__last_seen'
    meta_key = f'{account}__last_seen_meta'
    write_meta = file_key is not None and line_index is not None
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
        if data.get(key) == line and not write_meta:
            return  # already current — skip the write entirely
        data[key] = line
        if write_meta:
            data[meta_key] = {'line': line, 'file_key': file_key,
                              'line_index': int(line_index)}
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

# ── Read ───────────────────────────────────────────────────────────────────────
def _dedup_history_file(hf, log_fn=None, debug=False, account=None):
    """
    Read a history JSONL file, remove duplicate entries (same time+type+value+activity),
    keeping the first occurrence. Rewrites the file in-place if duplicates are found.
    Returns (rows, dupes_removed) where rows is the deduplicated list.

    When duplicates are removed, a structured diagnostic entry is always written to
    debug.jsonl (category 'history_dedupe'), regardless of the debug checkbox — this
    does not change dedupe behavior, it only records what was removed. If debug=True
    and log_fn is given, a short summary line is also mirrored to the Monitor tab.
    """
    if not hf.exists():
        return [], 0
    rows    = []
    removed = []
    seen  = set()
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
                    removed.append(rec)
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

        # ── Diagnostics: always written, independent of the debug checkbox ──────
        try:
            from py.util import write_debug_entry, now_str
            acct = account or hf.parent.name
            type_counts = {}
            for r in removed:
                t = r.get('type', '')
                type_counts[t] = type_counts.get(t, 0) + 1
            MAX_LOGGED = 200
            truncated  = len(removed) > MAX_LOGGED
            dup_entries = [
                {'time': r.get('time', ''), 'type': r.get('type', ''),
                 'value': r.get('value', ''), 'activity': r.get('activity', '')}
                for r in removed[:MAX_LOGGED]
            ]
            payload = {
                'cleanup_ts':     now_str(),
                'account':        acct,
                'history_file':   str(hf),
                'dupes_removed':  dupes,
                'type_counts':    type_counts,
                'duplicates':     dup_entries,
            }
            if truncated:
                payload['truncated'] = True
            write_debug_entry('history_dedupe', payload)

            if debug and log_fn:
                log_fn(f"[DEBUG] history_dedupe: {acct} removed {dupes} duplicates; "
                       f"type_counts={type_counts}")
        except Exception:
            pass  # diagnostics must never affect dedupe behavior

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
        clean_rows, dupes = _dedup_history_file(hf, log_fn=log_fn, debug=debug, account=account)
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

    Walks ALL rows chronologically regardless of date range so that carry-in
    state (script was already running, already in break) is correctly applied
    to the selected range.  Intervals that straddle range boundaries are
    clipped rather than dropped.

    Returns dict with:
      total_run_secs  — elapsed time when script was running (in range)
      break_secs      — elapsed time in break (in range, subset of run time)
      active_secs     — total_run_secs - break_secs
      break_pct       — break_secs / total_run_secs * 100 (0 if no run time)
      account         — account name
      range_from      — since_ts as ISO string, or earliest event if no since_ts
      range_to        — until_ts as ISO string, or latest event if no until_ts
    """
    rows = load_history_for(account)
    if not rows:
        return {'total_run_secs': 0, 'break_secs': 0, 'active_secs': 0,
                'break_pct': 0.0, 'account': account, 'range_from': None, 'range_to': None}

    # Sort all rows chronologically — history files can have out-of-order rows
    # from rotated files or backfill.
    rows.sort(key=lambda r: r.get('time', ''))

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

    # Effective range
    range_start = since_ts
    range_end   = until_ts or _parse_ts(rows[-1].get('time', ''))

    total_run_secs = 0.0
    break_secs     = 0.0

    # Carry-in state — updated as we walk every row before the range
    run_start   = None   # float: when the current running interval started
    break_start = None   # float: when the current break started
    on_break    = False

    def _add_run(interval_start, interval_end):
        """Accumulate a running interval, clipped to [range_start, range_end]."""
        nonlocal total_run_secs
        lo = max(interval_start, range_start) if range_start else interval_start
        hi = min(interval_end,   range_end)   if range_end   else interval_end
        if hi > lo:
            total_run_secs += hi - lo

    def _add_break(interval_start, interval_end):
        """Accumulate a break interval, clipped to [range_start, range_end]."""
        nonlocal break_secs
        lo = max(interval_start, range_start) if range_start else interval_start
        hi = min(interval_end,   range_end)   if range_end   else interval_end
        if hi > lo:
            break_secs += hi - lo

    for r in rows:
        rtype = r.get('type', '')
        rval  = (r.get('value', '') or '').strip()
        ts    = _parse_ts(r.get('time', ''))
        if ts is None:
            continue

        # Skip rows past range_end — state is fully accumulated
        if range_end and ts > range_end:
            break

        # ── Script lifecycle ──────────────────────────────────────────────────
        if rtype == 'script_event':
            verb = _norm_script_val(r)

            if verb in ('start', 'resume'):
                # Close any open break interval
                if on_break and break_start is not None:
                    _add_break(break_start, ts)
                    break_start = None
                    on_break    = False
                # If already running (e.g. script restarted without a stop),
                # close the current running interval before starting a new one.
                if run_start is not None:
                    _add_run(run_start, ts)
                run_start = ts

            elif verb in ('pause', 'stop'):
                # Close running interval
                if run_start is not None:
                    _add_run(run_start, ts)
                    run_start = None
                # Close break interval
                if on_break and break_start is not None:
                    _add_break(break_start, ts)
                    break_start = None
                    on_break    = False

        # ── Break task (type="task", value="Break") ────────────────────────
        # History stores breaks as type="task", value="Break".
        # Do NOT use the planned "Length:" activity — use actual elapsed time.
        elif rtype == 'task' and rval.lower() == 'break':
            if run_start is not None and not on_break:
                on_break    = True
                break_start = ts

        # ── Any non-Break task ends the break interval ─────────────────────
        elif rtype == 'task' and rval.lower() != 'break':
            if on_break and break_start is not None:
                _add_break(break_start, ts)
                break_start = None
                on_break    = False

    # ── Close open intervals at range_end ────────────────────────────────────
    if range_end:
        if run_start is not None:
            _add_run(run_start, range_end)
        if on_break and break_start is not None:
            _add_break(break_start, range_end)

    active_secs = max(0.0, total_run_secs - break_secs)
    break_pct   = (break_secs / total_run_secs * 100) if total_run_secs > 0 else 0.0

    # Human-readable range labels
    def _iso(ts_f):
        from datetime import datetime as _dt
        return _dt.fromtimestamp(ts_f).strftime('%Y-%m-%d %H:%M:%S') if ts_f else None

    rf = _iso(range_start) if range_start else (rows[0].get('time') if rows else None)
    rt = _iso(range_end)   if range_end   else (rows[-1].get('time') if rows else None)

    return {
        'total_run_secs': total_run_secs,
        'break_secs':     break_secs,
        'active_secs':    active_secs,
        'break_pct':      break_pct,
        'account':        account,
        'range_from':     rf,
        'range_to':       rt,
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
