"""
reader.py — Pure log parsing for P2P Monitor v1.5.0
Zero side effects: accepts lines, returns typed event dicts.
All slice_* functions live here. parse_lines() is the single entry point
used by both the live watcher and backfill — eliminates the triple-pipeline bug.

Error rule DATA (ERROR_TRIGGERS, _LOCK_REASON_PATTERNS, _SILENT_LOCK_NAMES)
is loaded from py/error_rules.py which fetches from GitHub on startup and
falls back to cache then bundled defaults. parse_lines() calls
error_rules.get_rules() at parse time so updates take effect immediately
without restarting the parser.
"""

import re
from py.error_rules import get_rules

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

# Intentional Wine of Zamorak death suppression — exact rule: check ONLY
# the previous 2 raw log lines for either marker (substring match, case-
# insensitive); no time window, no fuzzy matching beyond these two exact
# phrases. See _is_suppressed_wine_death() below.
_WINE_DEATH_MARKERS = ('STOP STEALING MY WINE', 'Interacting Wine of zamorak')
SKILL_LVL_RE    = re.compile(r"Congratulations, you've just advanced your (.+?) level\. You are now level (\d+)", re.I)
SKILL_99_RE     = re.compile(r"Congratulations, you've reached the highest possible (.+?) level of 99", re.I)
TOTAL_LVL_RE    = re.compile(r"Congratulations, you've reached a total level of (\d+)", re.I)
SCRIPT_START_RE = re.compile(r'Starting P2P Master AI now!', re.I)
SCRIPT_STOP_RE  = re.compile(r'Stopped P2P Master AI!', re.I)
SCRIPT_PAUSE_RE = re.compile(r'Script P2P Master AI paused\.\.\.')
SCRIPT_RESUME_RE= re.compile(r'Script P2P Master AI resumed!')

# Regex for '> Locking X' lines — structural parser, stays local
_LOCKING_RE = re.compile(r'\]\s*>\s*Locking\s+(.+)', re.IGNORECASE)

# Regex for reset lines that make the subsequent 'impossible' ping redundant
_RESET_RE = re.compile(r'(Escaped ship|Stuck walking)\s*->\s*Startup', re.IGNORECASE)

# Farming patch skip detection — structural parser, stays local
_FARM_REMOVE_RE = re.compile(r'Removing\s+(\S+)\s+due to low expected inv space', re.IGNORECASE)
_FARM_BUCKET_RE = re.compile(r"If you don't like this, get a bottomless bucket", re.IGNORECASE)

# Quest missing items block anchors
_QUEST_ITEMS_START_RE = re.compile(r'If any of these items are needed, make sure you have them', re.IGNORECASE)
_QUEST_ITEMS_END_RE   = re.compile(r'It is up to the human to manually re-obtain', re.IGNORECASE)

# Farming-specific lock-reason patterns — see GitHub issue #2
# ("wrong message for failing farming?"). Kept separate from the generic
# lock_reason_patterns (error_rules.json) because those only scan back 5
# lines and take the first match, which on a real Farming lock often
# lands on an early tool/teleport line while the true seed/consumable
# shortage sits further back — see _extract_farming_lock_reason().
_FARMING_RESOURCE_FAIL_RE = re.compile(
    r'(?:\(virtual\)\s+)?Resource check failed:\s*(Many|\d+)\s*\[(.+?)\]', re.IGNORECASE)
_FARMING_AFFORD_RE = re.compile(r"Can't reasonably afford\s*\[([^\]]+?)\]", re.IGNORECASE)
_FARMING_BANK_HAVE_RE = re.compile(r'\(bank\)\s+Have\s+0/Many\s+(.+)', re.IGNORECASE)

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

def _extract_farming_lock_reason(arr, lock_idx, lock_ts_str):
    """
    Dedicated Farming-specific failure-reason extraction — see GitHub
    issue #2 ("wrong message for failing farming?"). The generic
    lock_reason_patterns scan (error_rules.json, applied to every locked
    task) only looks back 5 lines and stops at the first pattern match.
    On a real Farming lock that often lands on an early tool/teleport
    "Resource check failed: 1 [...]" line, while the true blocker — a
    missing seed reported as "Resource check failed: Many [...]" several
    lines further back — falls outside that 5-line window entirely, or
    would have been checked later and never reached.

    Looks back up to 20 lines (comfortably covers every example in the
    issue, which had at most 8 resource-check lines before the lock)
    within the same timestamp, collects every matching line, then prefers
    stronger consumable/seed signals over generic tool/teleport noise:
        1. Can't reasonably afford [...]      (explicit affordability failure)
        2. (bank) Have 0/Many <item>            (explicit bank-shortage line)
        3. (virtual) Resource check failed: Many [...]
        4. Resource check failed: Many [...]    (any count, real or virtual)
        5. Resource check failed: 1 [...]       (tools/teleports — only used
           if nothing above matched anything at all)
    Item names are deduplicated (case-insensitive), preserving first-seen
    order and casing. Returns '' if nothing matched, so the caller falls
    back to the existing generic logic's behavior unchanged.

    Only ever called when the locked task name is 'farming' — every other
    locked task keeps the exact existing behavior, untouched.
    """
    afford_items, bank_items, virtual_many_items, many_items, single_items = [], [], [], [], []
    seen_afford, seen_bank, seen_v_many, seen_many, seen_single = set(), set(), set(), set(), set()

    def _split_items(raw):
        # "Construct. cape, Construct. cape(t), Teleport to house" -> list
        return [x.strip() for x in raw.split(',') if x.strip()]

    for j in range(max(0, lock_idx - 20), lock_idx):
        raw = arr[j]
        ts = LOG_TS_RE.match(raw)
        if ts and lock_ts_str and ts.group(1) != lock_ts_str:
            continue
        body = strip_prefix(raw).strip()

        m = _FARMING_AFFORD_RE.search(body)
        if m:
            # "Snapdragon seed false true" -> strip trailing true/false flags
            item = re.sub(r'\s+(?:true|false)\b.*$', '', m.group(1).strip(), flags=re.IGNORECASE).strip()
            key = item.lower()
            if item and key not in seen_afford:
                seen_afford.add(key)
                afford_items.append(item)
            continue

        m = _FARMING_BANK_HAVE_RE.search(body)
        if m:
            item = m.group(1).strip()
            key = item.lower()
            if item and key not in seen_bank:
                seen_bank.add(key)
                bank_items.append(item)
            continue

        m = _FARMING_RESOURCE_FAIL_RE.search(body)
        if m:
            is_many = m.group(1).lower() == 'many'
            is_virtual = body.lower().startswith('(virtual)')
            for item in _split_items(m.group(2)):
                key = item.lower()
                if is_many and is_virtual:
                    if key not in seen_v_many:
                        seen_v_many.add(key)
                        virtual_many_items.append(item)
                elif is_many:
                    if key not in seen_many:
                        seen_many.add(key)
                        many_items.append(item)
                else:
                    if key not in seen_single:
                        seen_single.add(key)
                        single_items.append(item)

    def _merge(*lists):
        """Combine item lists in priority order, deduplicating
        case-insensitively while preserving first-seen casing — used so a
        higher-priority signal (e.g. affordability) doesn't silently drop
        a genuinely different missing item that only showed up in a
        lower-priority signal (e.g. a second seed reported only via a
        'Resource check failed: Many [...]' line, not its own bank-have
        or affordability line)."""
        out, seen = [], set()
        for lst in lists:
            for item in lst:
                key = item.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(item)
        return out

    if afford_items:
        combined = _merge(afford_items, bank_items, virtual_many_items, many_items)
        return f"Farming locked: could not afford {', '.join(combined)}"
    combined = _merge(bank_items, virtual_many_items, many_items)
    if combined:
        return f"Farming locked: missing {', '.join(combined)}"
    if single_items:
        return f"Farming locked: missing {', '.join(single_items)}"
    return ''

def _is_suppressed_wine_death(arr, death_idx):
    """
    Intentional-death suppression for the Wine of Zamorak task. Exact
    rule, deliberately narrow: check ONLY the previous 2 raw log lines
    (not a time window, not any other context size) for either marker as
    a plain case-insensitive substring — no fuzzy matching beyond these
    two exact phrases:
        - 'STOP STEALING MY WINE'
        - 'Interacting Wine of zamorak'
    If either is present in either of those 2 lines, this death should be
    treated as if it never happened: returning True here means the caller
    never appends a 'death' event for it at all, which is sufficient to
    suppress every downstream effect (Discord ping, death counter,
    history entry, status/highlight update) — all of those are driven
    purely by the event's existence in the events list this module
    produces; nothing else in the watcher independently re-scans raw
    lines for this same death line.

    Does not affect Inferno's own, unrelated death handling (Inferno
    tracks death via wave/KC state in py/inferno.py, not this generic
    DEATH_RE-based path at all).
    """
    for j in range(max(0, death_idx - 2), death_idx):
        line = arr[j]
        if any(marker.lower() in line.lower() for marker in _WINE_DEATH_MARKERS):
            return True
    return False

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
    _consumed_by_pet = set()  # line indices consumed as pet name source

    # First pass: mark collection log lines consumed by a pet event
    for idx, line in enumerate(arr):
        clean = strip_color(strip_prefix(line)).strip()
        for pat in PET_PATTERNS:
            if pat.search(clean):
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
                        _consumed_by_pet.add(k)
                        break
                break

    for idx, line in enumerate(arr):
        clean = strip_color(strip_prefix(line)).strip()
        m = re.search(r'New item added to your collection log:\s*(.+)', clean, re.IGNORECASE)
        if m:
            if idx in _consumed_by_pet:
                continue  # already consumed as pet name
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
    re-assignment of the same monster if a new task is fetched between them."""
    seen_since_cancel = set()
    tasks = []
    arr   = list(lines)
    for line in arr:
        low = line.lower()
        # Reset dedup whenever the script fetches a new task — covers cancellations
        # and legitimate re-assignments of the same monster
        if ('your task has been cancelled' in low or
                'need a new slayer task' in low or
                'getting new task' in low):
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
    Also detects tasks cancelled immediately as unsupported (no Slayer -> line)
    by looking for 'This slayer task is not supported' before the cancel.
    """
    # Known unsupported task IDs
    _UNSUPPORTED_TASK_IDS = {
        126: 'Spiritual creatures',
    }

    results = []
    arr = list(lines)
    for i, line in enumerate(arr):
        if 'your task has been cancelled' not in line.lower():
            continue
        monster = None
        reason  = 'Not doable'

        # Check if this cancel was preceded by 'This slayer task is not supported'
        # within a short window — means script cancelled immediately, no Slayer -> line
        not_supported = False
        task_id = None
        for j in range(max(0, i - 50), i):
            lb = arr[j].lower()
            if 'this slayer task is not supported' in lb:
                not_supported = True
            # Look for 'Task id X' to identify the task
            m_id = re.search(r'task id\s+(\d+)', lb)
            if m_id:
                task_id = int(m_id.group(1))

        if not_supported:
            if task_id is not None:
                name = _UNSUPPORTED_TASK_IDS.get(task_id, f'Unknown task (ID {task_id})')
                reason = f'Not supported by script'
                results.append((name, reason))
            else:
                results.append(('Unsupported task', 'Not supported by script'))
            continue

        # Find the Slayer -> line that precedes this cancellation
        # Search up to 2000 lines back — task can be assigned far before cancel
        slayer_idx = None
        for j in range(i-1, max(-1, i-2000), -1):
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
            # Store the actual log line as search hint so parse_lines can find
            # the correct line index and timestamp via _find_ts — not 'Break'
            # which would never match anything in arr and fall back to len(arr).
            result.append(("Break", activity, line))

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

            # Suppress if 'Actually task is' appears within the same timestamp block
            # (same second or within 15 lines) — Actually task is takes full priority
            ts_match_ti = LOG_TS_RE.match(arr[i])
            this_ts_ti  = ts_match_ti.group(1) if ts_match_ti else None
            overridden  = False
            for j in range(i + 1, min(n, i + 15)):
                raw_j = arr[j]
                ts_j  = LOG_TS_RE.match(raw_j)
                if ts_j and this_ts_ti and ts_j.group(1) != this_ts_ti:
                    break
                nb_j = strip_prefix(raw_j).strip()
                if re.match(r'^Actually task is\s+', nb_j, re.IGNORECASE):
                    overridden = True
                    break
            if overridden:
                i += 1
                continue

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

            # If no explicit Task is line but a Slayer -> line exists in this block,
            # infer task=Slayer (handles new slayer task assigned outside a full NEW TASK block)
            if not resolved and slayer_val:
                resolved  = 'Slayer'
                task_val  = 'Slayer'

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
                        return ('', '')  # Break follows — no real task
                # Last NEW TASK block had no task — scan backwards through
                # previous NEW TASK blocks to find the last one that resolved
                for i in range(new_task_idx - 1, -1, -1):
                    if 'NEW TASK' not in arr[i].upper():
                        continue
                    prev_task_idx = i
                    prev_task_val = ''; prev_actually_val = ''; prev_activity_val = ''; prev_slayer_val = ''
                    prev_locked = False; prev_step_seen = False
                    for j in range(prev_task_idx, min(n, prev_task_idx + 60)):
                        b2 = strip_prefix(arr[j]).strip()
                        if re.match(r'.+\bStep\s+0\b', b2, re.IGNORECASE):
                            prev_step_seen = True
                        if not prev_step_seen and re.search(r'\]\s*>\s*Locking\b', arr[j], re.IGNORECASE):
                            prev_locked = True
                        m2 = re.match(r'^Actually task is\s+(.+)', b2, re.IGNORECASE)
                        if m2: prev_actually_val = m2.group(1).strip(); continue
                        m2 = re.match(r'^Task is\s+(.+)', b2, re.IGNORECASE)
                        if m2:
                            cand = m2.group(1).strip()
                            if not re.match(r'^(doable|not doable)\b', cand, re.IGNORECASE):
                                prev_task_val = cand
                            continue
                        m2 = re.match(r'^Activity is\s+(.+)', b2, re.IGNORECASE)
                        if m2: prev_activity_val = m2.group(1).strip(); continue
                        ms2 = re.search(r'Slayer\s*->\s*(\d+)\s+(.+)', b2, re.IGNORECASE)
                        if ms2: prev_slayer_val = f"{ms2.group(1)} {ms2.group(2).strip()}"
                    if prev_locked:
                        continue
                    prev_resolved = prev_actually_val or prev_task_val
                    if not prev_resolved and prev_slayer_val:
                        prev_resolved = 'Slayer'
                    if prev_resolved:
                        task = prev_resolved
                        activity = prev_activity_val
                        if ' - ' in task and task.lower() != 'questing':
                            parts = task.split(' - ', 1)
                            task = parts[0].strip()
                            if not activity: activity = parts[1].strip()
                        if task.lower() == 'slayer':
                            # Use most recent Slayer -> from the full file
                            for k in range(n - 1, -1, -1):
                                ms3 = re.search(r'Slayer\s*->\s*(\d+)\s+(.+)',
                                                strip_prefix(arr[k]).strip(), re.IGNORECASE)
                                if ms3:
                                    activity = f"{ms3.group(1)} {ms3.group(2).strip()}"
                                    break
                        return (task, activity)

    # Fallback
    # Don't return 'Break' as last task — it's not a real task name
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

    # Tasks (non-slayer) — tuples are (name, activity) or (name, activity, search_hint)
    # BREAK START entries carry the raw log line as search_hint so _find_ts resolves
    # the correct index; regular tasks use task_name as the search string.
    for tup in slice_tasks(arr):
        task_name, activity = tup[0], tup[1]
        search_hint = tup[2] if len(tup) > 2 else task_name
        events.append(_ev('task', task_name, activity, search_hint))

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

    _rules = get_rules()
    for (key, pattern, threshold, window_sec, dedupe_sec, label) in _rules['error_triggers']:
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
        is_silent   = locked_name.lower() in _rules['silent_lock_names']

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
            for pat, label_tpl in _rules['lock_reason_patterns']:
                rm = pat.search(prev_b)
                if rm:
                    item = rm.group(1).strip() if rm.lastindex and rm.lastindex >= 1 else ''
                    reason = label_tpl.replace('{item}', item) if item else label_tpl
                    break
            if reason:
                break

        # Farming-specific override — see GitHub issue #2. The generic scan
        # above can land on an early tool/teleport line while a true seed
        # shortage sits further back; this dedicated pass looks further
        # back and prioritizes seed/consumable signals over tool/teleport
        # noise. Only ever applied to Farming — every other locked task
        # keeps the exact reason computed above, untouched.
        if locked_name.lower() == 'farming':
            farming_reason = _extract_farming_lock_reason(arr, i, lock_ts_str)
            if farming_reason:
                reason = farming_reason

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
            reason_detail = f"Task locked/skipped: {locked_name}"

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
            if _is_suppressed_wine_death(arr, i):
                continue  # intentional Wine of Zamorak death — fully ignored, see issue
            events.append({'type': 'death', 'value': 'Oh dear, you are dead!', 'activity': '',
                           'ts': _ts_for_line(i), '_line_idx': i})
            break

    # Level ups
    for i, line in enumerate(arr):
        clean = strip_color(strip_prefix(line)).strip()
        # Check level 99 message FIRST — different format from normal levelups
        m99 = SKILL_99_RE.search(clean)
        if m99:
            skill = m99.group(1).strip()
            events.append({'type': 'levelup', 'value': skill, 'activity': '99',
                           'ts': _ts_for_line(i), '_line_idx': i, '_is_99': True})
            continue
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

    # Sort by timestamp then line index so same-second events reflect log order
    events.sort(key=lambda e: (e.get('ts', ''), e.get('_line_idx', len(arr))))

    return events
