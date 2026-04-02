"""
reader.py — Pure log parsing for P2P Monitor
Zero side effects: accepts lines, returns typed event dicts.
All slice_* functions live here. parse_lines() is the single entry point
used by both the live watcher and backfill — eliminates the triple-pipeline bug.
"""

import re

# ── Regex / pattern constants ──────────────────────────────────────────────────
STRIP_PREFIX_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\[[A-Z]+\]\s*>?\s*', re.IGNORECASE)
STRIP_COLOR_RE  = re.compile(r'<col=[^>]*>(.*?)</col>', re.IGNORECASE)
LOG_TS_RE       = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

PET_PATTERNS = [
    re.compile(r"you have a funny feeling like you're being followed", re.IGNORECASE),
    re.compile(r"you feel something weird sneaking into your backpack", re.IGNORECASE),
    re.compile(r"you have a funny feeling like you would have been followed", re.IGNORECASE),
]

DEATH_RE        = re.compile(r'\[GAME\] Oh dear, you are dead!', re.I)
SKILL_LVL_RE    = re.compile(r"Congratulations, you've just advanced your (.+?) level\. You are now level (\d+)", re.I)
TOTAL_LVL_RE    = re.compile(r"Congratulations, you've reached a total level of (\d+)", re.I)
SCRIPT_START_RE = re.compile(r'Starting P2P Master AI now!', re.I)
SCRIPT_STOP_RE  = re.compile(r'Stopped P2P Master AI!', re.I)
SCRIPT_PAUSE_RE = re.compile(r'Script P2P Master AI paused\.\.\.')
SCRIPT_RESUME_RE= re.compile(r'Script P2P Master AI resumed!')

# Names that appear after '> Locking' that are normal task completions or
# internal categories — not errors. Bare locks with no reason line are only
# pinged if the name is NOT in this set (i.e. it's a named quest).
_SILENT_LOCK_NAMES = {
    'attack', 'strength', 'defence', 'range', 'ranged', 'prayer', 'magic',
    'runecrafting', 'construction', 'agility', 'herblore', 'thieving', 'crafting',
    'fletching', 'slayer', 'hunter', 'mining', 'smithing', 'fishing', 'cooking',
    'firemaking', 'woodcutting', 'farming', 'sailing', 'questing', 'birdhouses',
    'port tasks',
}

# Regex for '> Locking X' lines
_LOCKING_RE = re.compile(r'\]\s*>\s*Locking\s+(.+)', re.IGNORECASE)

# Regex for reset lines that make the subsequent 'impossible' ping redundant
_RESET_RE = re.compile(r'(Escaped ship|Stuck walking)\s*->\s*Startup', re.IGNORECASE)

# Reason patterns scanned 1-5 lines before a lock line (same timestamp window)
# Tuples of (regex, label_template) — {item} replaced with captured group 1 if present
_LOCK_REASON_PATTERNS = [
    # Real resource check failed — exclude (virtual) prefix
    (re.compile(r'^(?!.*\(virtual\))Resource check failed \[(.+?)\]', re.IGNORECASE),
     'Missing: {item}'),
    # No bank teleport to exit dungeon
    (re.compile(r"Don't have any bank tp", re.IGNORECASE),
     'No teleport to exit area'),
    # No teleport available
    (re.compile(r"Don't have tp there", re.IGNORECASE),
     'No teleport to reach area'),
    # Quest-specific failures
    (re.compile(r'You need a stake to continue', re.IGNORECASE),
     'Missing: stake (Vampyre Slayer)'),
    (re.compile(r'Missing items can and will cause quest failure', re.IGNORECASE),
     'Missing quest items'),
    # Farming inv space
    (re.compile(r"If you don't like this, get a bottomless bucket", re.IGNORECASE),
     'Low inventory space — get a bottomless bucket'),
    # Quest state loop — always co-occurs with > Locking on same timestamp
    (re.compile(r'Quest state repeated too many times', re.IGNORECASE),
     'Quest state loop — auto-skipped'),
]

# Farming patch skip detection
_FARM_REMOVE_RE = re.compile(r'Removing\s+(\S+)\s+due to low expected inv space', re.IGNORECASE)
_FARM_BUCKET_RE = re.compile(r"If you don't like this, get a bottomless bucket", re.IGNORECASE)

# Quest missing items block anchors
_QUEST_ITEMS_START_RE = re.compile(r'If any of these items are needed, make sure you have them', re.IGNORECASE)
_QUEST_ITEMS_END_RE   = re.compile(r'It is up to the human to manually re-obtain', re.IGNORECASE)

ERROR_TRIGGERS = [
    ('high_severity',  re.compile(r'High severity server response', re.I),                    1, 0,   600,  'Script force-stopped by server'),
    ('start_problem',  re.compile(r'This script had a problem starting', re.I),               1, 0,   600,  'Script failed to start'),
    ('stuckness',      re.compile(r'Stuckness detected, stopping script', re.I),              1, 0,   600,  'Stuckness detected — script stopped'),
    ('overcrowded',    re.compile(r'LOCATION OVERCROWDED - SKIPPING TASK', re.I),             1, 0,   300,  'Location overcrowded — task skipped'),
    ('pathing_fail',   re.compile(r'Pathing failed -> lockout', re.I),                        1, 0,   600,  'Pathing lockout'),
    ('login_fail',     re.compile(r'Failed to login, waiting before trying again', re.I),     1, 0,   600,  'Login failure'),
    ('hop_fail',       re.compile(r'Failed to hop worlds!', re.I),                            2, 300, 600,  'Repeated world hop failures'),
    ('ge_fail',        re.compile(r"(GE failed to add item|I couldn't buy any of the items)", re.I), 1, 0, 600, 'Grand Exchange failure'),
    ('live_prices',    re.compile(r'There was a problem parsing the LivePrices data', re.I),  1, 0,   3600, 'LivePrices data error'),
    ('script_crash',   re.compile(r'(This script threw an error|Exception has occurred while running)', re.I), 1, 0, 600, 'Script exception/crash'),
    ('impossible',     re.compile(r'>>> Impossible to do anything in (.+?)!', re.I),          1, 0,   600,  'Impossible to do anything'),
    ('construction',   re.compile(r'Failed to clear floor!', re.I),                          1, 0,   600,  'Construction error — failed to clear floor'),
]

# ── String helpers ─────────────────────────────────────────────────────────────
def strip_prefix(line):
    return STRIP_PREFIX_RE.sub('', line)

def strip_color(text):
    return STRIP_COLOR_RE.sub(r'\1', text)

def parse_log_ts(lines):
    """Return timestamp from last timestamped line in a batch."""
    for line in reversed(lines):
        m = LOG_TS_RE.match(line)
        if m:
            return m.group(1)
    return None

# ── Individual slice functions ─────────────────────────────────────────────────

def _extract_quest_name(line):
    """Clean a log line and extract the quest name after the last colon."""
    clean = strip_color(strip_prefix(line))
    idx   = clean.rfind(':')
    return (clean[idx+1:].strip() if idx >= 0 else clean.strip())

def slice_quests(lines):
    """Returns list of ('complete', quest_name)."""
    results = []
    for line in lines:
        if 'completed a quest' in line.lower():
            name = _extract_quest_name(line)
            if name:
                results.append(('complete', name))
    return results

def slice_quests_started(lines):
    """Returns list of quest_name strings."""
    results = []
    for line in lines:
        if "you've started a new quest" in line.lower():
            name = _extract_quest_name(line)
            if name:
                results.append(name)
    return results

def slice_drops(lines):
    """Returns list of (item, [types]) with types combined per item."""
    raw = []
    arr = list(lines)
    for idx, line in enumerate(arr):
        clean = strip_color(strip_prefix(line)).strip()
        m = re.search(r'New item added to your collection log:\s*(.+)', clean, re.IGNORECASE)
        if m:
            raw.append(('collection', m.group(1).strip()))
            continue
        m = re.search(r'Untradeable drop:\s*(.+)', clean, re.IGNORECASE)
        if m:
            raw.append(('untradeable', m.group(1).strip()))
            continue
        m = re.search(r'Valuable drop:\s*(.+)', clean, re.IGNORECASE)
        if m:
            raw.append(('valuable', m.group(1).strip()))
            continue
        for pat in PET_PATTERNS:
            if pat.search(clean):
                # Try to find the pet name from a nearby collection log line
                # (same timestamp, within 3 lines either side)
                pet_name = 'Pet'
                line_ts = LOG_TS_RE.match(line)
                ts_str  = line_ts.group(1) if line_ts else None
                for k in range(max(0, idx - 3), min(len(arr), idx + 4)):
                    if k == idx:
                        continue
                    nb_ts = LOG_TS_RE.match(arr[k])
                    if nb_ts and ts_str and nb_ts.group(1) != ts_str:
                        continue
                    nb = strip_color(strip_prefix(arr[k])).strip()
                    pm = re.search(r'New item added to your collection log:\s*(.+)', nb, re.IGNORECASE)
                    if pm:
                        pet_name = pm.group(1).strip()
                        break
                raw.append(('pet', pet_name))
                break
    grouped = {}
    order = []
    for dtype, item in raw:
        if item not in grouped:
            grouped[item] = []
            order.append(item)
        if dtype not in grouped[item]:
            grouped[item].append(dtype)
    return [(item, grouped[item]) for item in order]

def slice_slayer_tasks(lines):
    """Returns list of (monster, count). Deduped within the batch, but allows
    re-assignment of the same monster if a cancellation line appears between them."""
    seen_since_cancel = set()
    tasks = []
    arr   = list(lines)
    for line in arr:
        low = line.lower()
        # A cancellation resets the dedup set — next assignment is always fresh
        if 'your task has been cancelled' in low:
            seen_since_cancel = set()
            continue
        m = re.search(r'Slayer\s*->\s*(\d+)\s+(.+)', strip_prefix(line).strip(), re.IGNORECASE)
        if m:
            count   = int(m.group(1))
            monster = m.group(2).strip()
            key = monster.lower()
            if key not in seen_since_cancel:
                seen_since_cancel.add(key)
                tasks.append((monster, count))
    return tasks

def slice_slayer_complete(lines):
    """Returns list of (monster, tasks_done, points_earned, total_points)."""
    results = []
    arr = list(lines)
    for i, line in enumerate(arr):
        if 'you have completed your task' not in line.lower():
            continue
        tasks_done = points_earned = total_points = None
        monster = None
        block = arr[max(0, i-30):min(len(arr), i+60)]
        for ln in block:
            mc = re.search(
                r'You have completed your task.*?killed\s+[\d,]+\s+(.+?)(?:\.|<|$)',
                strip_color(strip_prefix(ln)), re.IGNORECASE)
            if mc:
                monster = mc.group(1).strip()
                break
        if not monster:
            for j in range(i-1, max(0, i-100), -1):
                ms = re.search(r'Slayer\s*->\s*\d+\s+(.+)',
                               strip_prefix(arr[j]).strip(), re.IGNORECASE)
                if ms:
                    monster = ms.group(1).strip()
                    break
        for ln in block:
            clean = strip_color(strip_prefix(ln))
            m = re.search(
                r"You.ve completed\s+([\d,]+)\s+tasks.*?received\s+([\d,]+)\s+points.*?total of\s+([\d,]+)",
                clean, re.IGNORECASE)
            if m:
                tasks_done    = int(m.group(1).replace(',', ''))
                points_earned = int(m.group(2).replace(',', ''))
                total_points  = int(m.group(3).replace(',', ''))
                break
            m2 = re.search(r"You.ve completed\s+([\d,]+)\s+tasks", clean, re.IGNORECASE)
            if m2 and tasks_done is None:
                tasks_done = int(m2.group(1).replace(',', ''))
        results.append((monster, tasks_done, points_earned, total_points))
    return results

def slice_slayer_skipped(lines):
    """
    Returns list of (monster, reason).
    BUG FIX: scans FORWARD from the Slayer -> line so
    'not doable with this style' (which always appears first) wins over
    'failed for the reasons above' (which appears later and was previously
    being picked up by the old backwards scan, causing the wrong reason
    on Discord).
    """
    results = []
    arr = list(lines)
    for i, line in enumerate(arr):
        if 'your task has been cancelled' not in line.lower():
            continue
        monster = None
        reason  = 'Not doable'

        # Find the Slayer -> line that precedes this cancellation
        slayer_idx = None
        for j in range(i-1, -1, -1):
            if 'getting new task' in arr[j].lower():
                break
            ms = re.search(r'Slayer\s*->\s*\d+\s+(.+)',
                           strip_prefix(arr[j]).strip(), re.IGNORECASE)
            if ms:
                monster    = ms.group(1).strip()
                slayer_idx = j
                break

        if slayer_idx is None:
            if monster:
                results.append((monster, reason))
            continue

        # Scan FORWARD from slayer line to cancellation — first definitive reason wins
        for j in range(slayer_idx + 1, i + 1):
            ln = arr[j].lower()
            if 'not doable with this style' in ln:
                reason = 'Not doable with current style'
                break
            if 'missing requirements' in ln or 'disabled by the user' in ln:
                reason = 'Missing requirements or disabled'
                break
            if 'failed for the reasons above' in ln:
                reason = 'Missing requirements or disabled'
                break

        if monster:
            results.append((monster, reason))
    return results

def slice_tasks(lines):
    """
    Returns list of (task_name, activity).

    Anchors on 'Task is' lines directly — no NEW TASK dependency.
    This fixes cross-chunk split (e.g. world hop between NEW TASK and Task is)
    and the locking detection bug (strip_prefix was eating the leading '>').

    Rules:
      - 'Actually task is X' overrides a preceding 'Task is X'
      - 'Task is X - Y' splits into task=X, activity=Y (except Questing)
      - 'Activity is Y' on a following line sets activity if not already set
      - '> Locking' before any Step 0 = suppress that task
      - Slayer -> tasks deferred to slice_slayer_tasks
      - BREAK START emits ('Break', 'Length: Xh Ym Zs')
    """
    result = []
    arr    = list(lines)
    n      = len(arr)

    # BREAK START
    from py.util import parse_break_length_ms, format_break_duration
    for i, line in enumerate(arr):
        if 'BREAK START' in line.upper():
            activity = ''
            ms = parse_break_length_ms(arr, max(0, i - 25), max_search=51)
            if ms is not None:
                activity = "Length: " + format_break_duration(ms)
            result.append(("Break", activity))

    # Task scanning — anchor on 'Task is' and 'Actually task is'
    i = 0
    while i < n:
        b   = strip_prefix(arr[i]).strip()
        raw = arr[i]

        # 'Actually task is X' — highest priority override
        # Suppressed if '> Locking' appears within the same timestamp + 15-line window
        if re.match(r'^Actually task is\s+', b, re.IGNORECASE):
            task_name = re.sub(r'^Actually task is\s*', '', b, flags=re.IGNORECASE).strip()
            activity  = ''
            locked    = False

            # Determine the timestamp of this line (used as the same-second boundary)
            ts_match  = LOG_TS_RE.match(arr[i])
            this_ts   = ts_match.group(1) if ts_match else None

            for j in range(i + 1, min(n, i + 15)):
                raw_j  = arr[j]
                nb     = strip_prefix(raw_j).strip()
                # Stop scanning if we've moved past the same timestamp
                ts_j   = LOG_TS_RE.match(raw_j)
                if ts_j and this_ts and ts_j.group(1) != this_ts:
                    break
                if re.search(r'\]\s*>\s*Locking\b', raw_j, re.IGNORECASE):
                    locked = True
                    break
                if re.match(r'^Activity is\s+', nb, re.IGNORECASE) and not activity:
                    activity = re.sub(r'^Activity is\s*', '', nb, flags=re.IGNORECASE).strip()
                if re.match(r'^Task is\b', nb, re.IGNORECASE):
                    break

            if not locked and task_name and 'slayer' not in task_name.lower():
                result.append((task_name, activity))
            i += 1
            continue

        # 'Task is X' — skip doable/NOT doable variants
        if (re.match(r'^Task is\b', b, re.IGNORECASE)
                and not re.match(r'^Task is(?:\s+NOT)?\s+doable', b, re.IGNORECASE)):

            task_name = re.sub(r'^Task is\s*', '', b, flags=re.IGNORECASE).strip()

            # Look ahead up to 10 lines for locking, activity, step 0, slayer
            locked    = False
            activity  = ''
            step_seen = False
            has_slayer = False
            for j in range(i + 1, min(n, i + 11)):
                nb    = strip_prefix(arr[j]).strip()
                raw_j = arr[j]
                if re.match(r'.+\bStep\s+0\b', nb, re.IGNORECASE):
                    step_seen = True
                # Check raw line — strip_prefix eats the leading '>' in '> Locking'
                if not step_seen and re.search(r'\]\s*>\s*Locking\b', raw_j, re.IGNORECASE):
                    locked = True
                    break
                if re.match(r'^Activity is\s+', nb, re.IGNORECASE) and not activity:
                    activity = re.sub(r'^Activity is\s*', '', nb, flags=re.IGNORECASE).strip()
                if 'Slayer ->' in raw_j:
                    has_slayer = True

            if locked or has_slayer:
                i += 1
                continue

            # 'Task is X - Y' split (except Questing which uses ' - ' in quest names)
            if ' - ' in task_name and task_name.lower() != 'questing':
                parts     = task_name.split(' - ', 1)
                task_name = parts[0].strip()
                if not activity:
                    activity = parts[1].strip()

            if re.match(r'^(?:NOT\s+)?doable with this style$', task_name, re.IGNORECASE):
                i += 1
                continue

            if task_name or activity:
                result.append((task_name.strip(), activity.strip()))

        i += 1
    return result

def slice_last_task(lines):
    """
    Return the most recent (task_name, activity) from the log lines,
    using the same parsing rules as slice_tasks().
    Scans backwards for the last NEW TASK block, then forward to resolve.
    For Slayer tasks, also scans the full file backwards for the most recent
    'Slayer -> N Monster' line to handle rerolls outside the 60-line window.
    Falls back to last BREAK START if no task found.
    Returns ('', '') if nothing found.
    """
    from py.util import parse_break_length_ms, format_break_duration
    arr = list(lines)
    n   = len(arr)

    # Find last NEW TASK line scanning backwards
    new_task_idx = None
    for i in range(n - 1, -1, -1):
        if 'NEW TASK' in arr[i].upper():
            new_task_idx = i
            break

    if new_task_idx is not None:
        task_val     = ''
        actually_val = ''
        activity_val = ''
        slayer_val   = ''
        locked       = False
        step_seen    = False

        for j in range(new_task_idx, min(n, new_task_idx + 60)):
            b = strip_prefix(arr[j]).strip()
            raw_j = arr[j]

            if re.match(r'.+\bStep\s+0\b', b, re.IGNORECASE):
                step_seen = True
            if not step_seen and re.search(r'\]\s*>\s*Locking\b', raw_j, re.IGNORECASE):
                locked = True

            m = re.match(r'^Actually task is\s+(.+)', b, re.IGNORECASE)
            if m:
                actually_val = m.group(1).strip()
                continue
            m = re.match(r'^Task is\s+(.+)', b, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if not re.match(r'^(doable|not doable)\b', cand, re.IGNORECASE):
                    task_val = cand
                continue
            m = re.match(r'^Activity is\s+(.+)', b, re.IGNORECASE)
            if m:
                activity_val = m.group(1).strip()
                continue
            ms = re.search(r'Slayer\s*->\s*(\d+)\s+(.+)', b, re.IGNORECASE)
            if ms:
                slayer_val = f"{ms.group(1)} {ms.group(2).strip()}"

        if not locked:
            resolved = actually_val or task_val
            if resolved:
                task = resolved
                activity = activity_val
                if ' - ' in task and task.lower() != 'questing':
                    parts = task.split(' - ', 1)
                    task = parts[0].strip()
                    if not activity:
                        activity = parts[1].strip()
                if task.lower() == 'slayer':
                    # Scan full file backwards for most recent Slayer -> line
                    # handles rerolls that appear far outside the 60-line window
                    for k in range(n - 1, new_task_idx - 1, -1):
                        ms2 = re.search(r'Slayer\s*->\s*(\d+)\s+(.+)',
                                        strip_prefix(arr[k]).strip(), re.IGNORECASE)
                        if ms2:
                            slayer_val = f"{ms2.group(1)} {ms2.group(2).strip()}"
                            break
                    if slayer_val:
                        activity = slayer_val
                return (task, activity)
            else:
                for j in range(new_task_idx, min(n, new_task_idx + 60)):
                    if 'BREAK START' in arr[j].upper():
                        return ('Break', '')

    # Fallback: check for BREAK START at end of log
    for i in range(n - 1, -1, -1):
        if 'BREAK START' in arr[i].upper():
            bl_ms = parse_break_length_ms(arr, max(0, i - 25), max_search=51)
            activity = "Length: " + format_break_duration(bl_ms) if bl_ms else ''
            return ('Break', activity)

    return ('', '')

def slice_chat_segments(lines):
    """Returns list of (chat_text, response_text)."""
    segments, current = [], []
    for line in lines:
        upper = line.upper()
        if 'CHAT' in upper and not current:
            current.append(line)
        elif ('SLOWLY TYPING RESPONSE' in upper or 'BAD RESPONSE' in upper) and current:
            current.append(line)
            segments.append(list(current))
            current = []
        elif current:
            current.append(line)
    results = []
    for seg in segments:
        chat_lines = [l for l in seg if 'CHAT' in l.upper() and 'SLOWLY TYPING' not in l.upper()]
        resp_lines = [l for l in seg if 'SLOWLY TYPING RESPONSE' in l.upper() or 'BAD RESPONSE' in l.upper()]
        chat_text = strip_prefix(chat_lines[0]).strip() if chat_lines else ''
        resp_text = re.sub(r'^(SLOWLY TYPING RESPONSE|BAD RESPONSE):\s*', '',
                           strip_prefix(resp_lines[-1]).strip() if resp_lines else '',
                           flags=re.IGNORECASE)
        if chat_text:
            results.append((chat_text, resp_text))
    return results

# ── parse_lines — unified entry point ─────────────────────────────────────────
# Used by both live watcher (_process_lines) and backfill (_backfill_history).
# Returns a list of event dicts. No side effects.
#
# Event dict keys:
#   type     — event type string
#   value    — primary value (quest name, monster, item, skill, label…)
#   activity — secondary value (count, level, drop type, reason…)
#   ts       — ISO timestamp from the log line (or '' if not found)
#   _raw     — for error events: (key, threshold, window_sec, dedupe_sec, detail)
#   _drop_types — for drop events: list of type strings
#   _slayer_complete — for slayer_complete: (tasks_done, points_earned, total_points)

def parse_lines(lines):
    """
    Parse a batch of log lines into a list of typed event dicts.
    No side effects. Used by both live watcher and backfill.
    """
    events   = []
    arr      = list(lines)

    # Build a per-line timestamp index — each entry is the most recent timestamp
    # at or before that line. Used to assign accurate per-event timestamps.
    _line_ts = []
    _last_ts = ''
    for line in arr:
        m = LOG_TS_RE.match(line)
        if m:
            _last_ts = m.group(1)
        _line_ts.append(_last_ts)

    def _ts(line_list):
        """Return timestamp from last timestamped line in line_list."""
        for line in reversed(line_list):
            m = LOG_TS_RE.match(line)
            if m:
                return m.group(1)
        return _last_ts or ''

    def _find_ts(search_str):
        """
        Find the timestamp and line index for the line in arr that contains search_str.
        Falls back to (_ts(arr), len(arr)) if not found.
        """
        if not search_str:
            return _ts(arr), len(arr)
        for i, line in enumerate(arr):
            if search_str in line:
                return _line_ts[i] or _ts(arr), i
        return _ts(arr), len(arr)

    def _ev(type_, value, activity, search_str, **extra):
        ts, idx = _find_ts(search_str)
        return {'type': type_, 'value': value, 'activity': activity, 'ts': ts, '_line_idx': idx, **extra}

    # Quests started
    for name in slice_quests_started(arr):
        events.append(_ev('quest_started', name, '', name))

    # Quests completed
    for _, quest in slice_quests(arr):
        events.append(_ev('quest', quest, '', quest))

    # Tasks (non-slayer)
    for task_name, activity in slice_tasks(arr):
        events.append(_ev('task', task_name, activity, task_name))

    # Slayer new task
    for monster, count in slice_slayer_tasks(arr):
        events.append(_ev('slayer_task', monster, str(count), monster))

    # Slayer complete
    for monster, tasks_done, points_earned, total_points in slice_slayer_complete(arr):
        label = monster or 'Unknown'
        pts   = f"+{points_earned:,} pts (total: {total_points:,})" if points_earned else "no points yet"
        events.append(_ev('slayer_complete', label, pts, monster,
                          _slayer_complete=(tasks_done, points_earned, total_points)))

    # Slayer skipped
    for monster, reason in slice_slayer_skipped(arr):
        events.append(_ev('slayer_skip', monster, reason, monster))

    # Chat
    for chat_text, resp_text in slice_chat_segments(arr):
        events.append(_ev('chat', chat_text, resp_text, chat_text))

    # Drops
    for item, drop_types in slice_drops(arr):
        label = ' + '.join(t.title() for t in drop_types)
        events.append(_ev('drop', item, label, item, _drop_types=drop_types))

    # Errors — returned as raw tuples so caller can apply threshold/dedupe logic
    # Build a set of timestamps that had a reset line — impossible pings on the
    # same timestamp are redundant (the reset error already covers it).
    _reset_ts = set()
    for line in arr:
        if _RESET_RE.search(line):
            m = LOG_TS_RE.match(line)
            if m:
                _reset_ts.add(m.group(1))

    for (key, pattern, threshold, window_sec, dedupe_sec, label) in ERROR_TRIGGERS:
        matches = [(i, l) for i, l in enumerate(arr) if pattern.search(l)]
        if not matches:
            continue
        if key == 'impossible':
            for mi, m_line in matches:
                m = pattern.search(m_line)
                if m:
                    skill = m.group(1).strip()
                    if skill.lower() == 'hunter':
                        continue
                    # Suppress if a reset fired on the same timestamp
                    line_ts = LOG_TS_RE.match(m_line)
                    if line_ts and line_ts.group(1) in _reset_ts:
                        continue
                    detail = strip_prefix(m_line).strip()
                    ts, _ = _find_ts(detail)
                    events.append({
                        'type': 'error', 'value': label, 'activity': skill, 'ts': ts,
                        '_line_idx': mi,
                        '_raw': (f'impossible_{skill}', threshold, window_sec, dedupe_sec, detail),
                    })
            continue
        last_i, last_line = matches[-1]
        detail = strip_prefix(last_line).strip()
        ts, _ = _find_ts(detail)
        events.append({
            'type': 'error', 'value': label, 'activity': detail, 'ts': ts,
            '_line_idx': last_i,
            '_raw': (key, threshold, window_sec, dedupe_sec, detail),
        })

    def _ts_for_line(i):
        """Return the most recent timestamp at or before line index i in arr."""
        return _line_ts[i] if i < len(_line_ts) and _line_ts[i] else _ts(arr)

    # ── Unified lock detection ─────────────────────────────────────────────────
    # Scans every '> Locking X' line. For each:
    #   1. Look back up to 5 lines (same timestamp) for a known reason pattern.
    #   2. Also collect quest missing items block if present nearby.
    #   3. If reason found → ping regardless of whether X is a skill or quest.
    #   4. If no reason found → only ping if X is a named quest (not a skill).
    # Also collects quest missing items block for additional detail.
    for i, line in enumerate(arr):
        m = _LOCKING_RE.search(line)
        if not m:
            continue
        locked_name = m.group(1).strip()
        lock_ts     = LOG_TS_RE.match(line)
        lock_ts_str = lock_ts.group(1) if lock_ts else None
        is_silent   = locked_name.lower() in _SILENT_LOCK_NAMES

        # Scan back up to 5 lines within the same timestamp for a reason
        # Also scan forward a few lines at the same timestamp (e.g. quest state loop
        # appears on same line as the lock)
        reason = ''
        scan_range = list(range(max(0, i - 5), i)) + list(range(i, min(len(arr), i + 3)))
        for j in scan_range:
            if j == i and not reason:
                # Check the lock line itself and lines just after it too
                pass
            prev_raw = arr[j]
            prev_b   = strip_prefix(prev_raw).strip()
            prev_ts  = LOG_TS_RE.match(prev_raw)
            if prev_ts and lock_ts_str and prev_ts.group(1) != lock_ts_str:
                continue
            for pat, label_tpl in _LOCK_REASON_PATTERNS:
                rm = pat.search(prev_b)
                if rm:
                    item = rm.group(1).strip() if rm.lastindex and rm.lastindex >= 1 else ''
                    reason = label_tpl.replace('{item}', item) if item else label_tpl
                    break
            if reason:
                break

        # Collect quest missing items block if present nearby (up to 15 lines back)
        quest_items = []
        in_items_block = False
        for j in range(max(0, i - 15), i):
            nb = strip_prefix(arr[j]).strip()
            if _QUEST_ITEMS_START_RE.search(nb):
                in_items_block = True
                continue
            if in_items_block:
                if _QUEST_ITEMS_END_RE.search(nb):
                    break
                # Item lines are plain names (no brackets, no [INFO] noise)
                if nb and not nb.startswith('>>>') and '[' not in nb:
                    quest_items.append(nb)

        # Decide whether to fire
        # Only fire if a NEW TASK follows within 25 lines — confirms the task
        # was actually abandoned. If no NEW TASK follows, the script continued
        # running the task despite the lock line (false positive).
        new_task_follows = any(
            'NEW TASK' in arr[k].upper()
            for k in range(i + 1, min(len(arr), i + 26))
        )
        if not new_task_follows:
            continue  # lock didn't lead to task change — not a real abandonment

        if not reason and is_silent:
            continue  # normal completion, no reason, no ping

        # Build reason detail
        if quest_items:
            reason_detail = f"{reason + ' — ' if reason else ''}needs: {', '.join(quest_items)}"
        elif reason:
            reason_detail = reason
        else:
            reason_detail = f"Quest abandoned: {locked_name}"

        dedupe = f'lock_{locked_name.lower().replace(" ", "_")}'
        events.append({
            'type': 'error',
            'value': locked_name,       # task/quest name — watcher enriches with last_task/activity
            'activity': reason_detail,  # reason for failure
            'ts': _ts_for_line(i), '_line_idx': i,
            '_raw': (dedupe, 1, 0, 600, reason_detail),
            '_lock_name': locked_name,  # used by watcher to build Task — Activity display
        })

    # ── Farming patch skip detection ───────────────────────────────────────────
    # Collect all 'Removing X due to low expected inv space' lines that share a
    # timestamp and fire a single ping listing all removed patches.
    _farm_skip_seen = set()
    for i, line in enumerate(arr):
        if not _FARM_BUCKET_RE.search(line):
            continue
        line_ts_m = LOG_TS_RE.match(line)
        if not line_ts_m:
            continue
        ts_str = line_ts_m.group(1)
        if ts_str in _farm_skip_seen:
            continue
        _farm_skip_seen.add(ts_str)
        # Collect all removed patches at this timestamp
        removed = []
        for j, other in enumerate(arr):
            other_ts = LOG_TS_RE.match(other)
            if other_ts and other_ts.group(1) == ts_str:
                rm = _FARM_REMOVE_RE.search(strip_prefix(other).strip())
                if rm:
                    removed.append(rm.group(1).strip())
        if removed:
            reason_detail = f"Patches skipped (no bottomless bucket): {', '.join(removed)}"
            events.append({
                'type': 'error',
                'value': 'Farming patches skipped',  # watcher replaces with last_task — last_activity
                'activity': reason_detail,
                'ts': _ts_for_line(i), '_line_idx': i,
                '_raw': (f'farm_skip_{ts_str}', 1, 0, 3600, reason_detail),
                '_is_farm_skip': True,
            })

    # Deaths
    for i, line in enumerate(arr):
        if DEATH_RE.search(line):
            events.append({'type': 'death', 'value': 'Oh dear, you are dead!', 'activity': '',
                           'ts': _ts_for_line(i), '_line_idx': i})
            break

    # Level ups
    for i, line in enumerate(arr):
        clean = strip_color(strip_prefix(line)).strip()
        m = SKILL_LVL_RE.search(clean)
        if m:
            skill = m.group(1).strip()
            level = int(m.group(2))
            events.append({'type': 'levelup', 'value': skill, 'activity': str(level),
                           'ts': _ts_for_line(i), '_line_idx': i})
            continue
        m2 = TOTAL_LVL_RE.search(clean)
        if m2:
            total = int(m2.group(1))
            events.append({'type': 'levelup', 'value': 'Total Level', 'activity': str(total),
                           'ts': _ts_for_line(i), '_line_idx': i, '_total_level': total})

    # Script lifecycle
    _SCRIPT_EVENTS = [
        (SCRIPT_START_RE,  'start',  '▶️ Script Started'),
        (SCRIPT_STOP_RE,   'stop',   '⏹️ Script Stopped'),
        (SCRIPT_PAUSE_RE,  'pause',  '⏸️ Script Paused'),
        (SCRIPT_RESUME_RE, 'resume', '▶️ Script Resumed'),
    ]
    for pattern, ev_key, label in _SCRIPT_EVENTS:
        for i, line in enumerate(arr):
            if pattern.search(line):
                events.append({'type': 'script_event', 'value': ev_key, 'activity': label,
                               'ts': _ts_for_line(i), '_line_idx': i})
                break

    # Sort by timestamp then line index so same-second events reflect log order
    events.sort(key=lambda e: (e.get('ts', ''), e.get('_line_idx', len(arr))))

    return events
