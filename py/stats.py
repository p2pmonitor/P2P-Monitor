"""
py/stats.py — Level-up aggregation for the Stats tab.

Pure, stdlib-only aggregation logic. No Tkinter, no network, no writes — just
reads already-persisted history via py.history and aggregates it. Kept
separate from ui/stats_tab.py so it can be unit-tested without a display and
reused later (e.g. CSV export) without dragging in Tkinter.

Data source: history records where type == 'levelup'.
  value    = skill name (or the literal 'Total Level' for total-level
             milestone broadcasts — see TOTAL_LEVEL_LABEL below)
  activity = new level (string, expected numeric)
  time     = 'YYYY-MM-DD HH:MM:SS'
  account  = stored on the record itself by append_history()

'Total Level' rows are excluded from aggregation: they're a milestone
broadcast, not a real skill, and normally fire alongside a real per-skill
levelup event for the same moment — counting both would double-count and
would also corrupt the per-skill breakdown with a fake "skill" called
"Total Level".
"""
from datetime import datetime, timedelta

from py.history import load_history_accounts, load_history_for

TOTAL_LEVEL_LABEL = 'Total Level'

# Date range presets shown as buttons in the Stats tab. 'ALL' has no lower bound.
DATE_PRESETS = ('7D', '30D', '90D', '1Y', 'ALL')
_PRESET_DAYS = {'7D': 7, '30D': 30, '90D': 90, '1Y': 365}


def load_levelup_rows(accounts=None):
    """Load all levelup rows (excluding 'Total Level' milestone broadcasts)
    across the given account names, or all accounts with history if None.

    Returns a list of dicts: {account, skill, level, time, date}.
    Malformed/unparseable rows (non-numeric level, missing time) are skipped
    silently — this is a reporting feature, not a data-integrity gate, and
    skipping a handful of bad rows shouldn't break the whole tab.
    """
    if accounts is None:
        accounts = load_history_accounts()
    rows = []
    for acc in accounts:
        for rec in load_history_for(acc):
            if not isinstance(rec, dict) or rec.get('type') != 'levelup':
                continue
            skill = rec.get('value', '')
            if not skill or skill == TOTAL_LEVEL_LABEL:
                continue
            try:
                level = int(str(rec.get('activity', '')).strip())
            except (ValueError, TypeError):
                continue
            time_str = rec.get('time', '') or ''
            date_str = time_str.split(' ')[0] if time_str else ''
            if not date_str:
                continue
            rows.append({
                'account': rec.get('account') or acc,
                'skill':   skill,
                'level':   level,
                'time':    time_str,
                'date':    date_str,
            })
    return rows


def date_bounds_for_preset(preset, today=None):
    """Return (date_from, date_to) as 'YYYY-MM-DD' strings (inclusive) for a
    preset, or (None, None) for 'ALL' (no lower bound — caller derives the
    actual span from the data itself)."""
    today = today or datetime.now().date()
    date_to = today.strftime('%Y-%m-%d')
    days = _PRESET_DAYS.get(preset)
    if days is None:
        return None, None
    date_from = (today - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    return date_from, date_to


def filter_rows(rows, account=None, skill=None, date_from=None, date_to=None):
    """Filter levelup rows by account, skill, and inclusive date range.
    A falsy account/skill (None, '', or an 'All ...' sentinel the caller
    already resolved to None) means no filter on that field."""
    out = rows
    if account:
        out = [r for r in out if r['account'] == account]
    if skill:
        out = [r for r in out if r['skill'] == skill]
    if date_from:
        out = [r for r in out if r['date'] >= date_from]
    if date_to:
        out = [r for r in out if r['date'] <= date_to]
    return out


def aggregate_daily_totals(rows):
    """Return a sorted list of (date_str, count) — one entry per day that has
    at least one levelup — for the main 'Daily Levels Gained' chart."""
    counts = {}
    for r in rows:
        counts[r['date']] = counts.get(r['date'], 0) + 1
    return sorted(counts.items())


def aggregate_skill_totals(rows):
    """Return (skill, count) pairs sorted descending by count — 'Levels by Skill' panel."""
    counts = {}
    for r in rows:
        counts[r['skill']] = counts.get(r['skill'], 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def aggregate_account_totals(rows):
    """Return (account, count) pairs sorted descending by count — 'Top Accounts' panel."""
    counts = {}
    for r in rows:
        counts[r['account']] = counts.get(r['account'], 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def compute_kpis(rows, date_from=None, date_to=None):
    """Compute the four Stats tab KPI values for an already-filtered row set.

    Average Per Day divides by the number of *calendar* days in the applied
    range (date_from..date_to inclusive), not just the days that happen to
    have data — a quiet stretch should pull the average down, matching how
    the KPI reads elsewhere ("vs previous N days"). When no explicit bounds
    are given (the 'ALL' preset), the span is derived from the actual data's
    earliest-to-latest date instead.

    Returns a dict: total_levels, avg_per_day, best_day (date, count),
    top_account (account, count). All zeroed/None when rows is empty —
    callers should render the dedicated empty state in that case instead.
    """
    if not rows:
        return {
            'total_levels': 0, 'avg_per_day': 0.0,
            'best_day': (None, 0), 'top_account': (None, 0),
        }
    daily = aggregate_daily_totals(rows)
    total = len(rows)

    if date_from and date_to:
        d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        d_to   = datetime.strptime(date_to,   '%Y-%m-%d').date()
    else:
        dates  = [datetime.strptime(d, '%Y-%m-%d').date() for d, _ in daily]
        d_from, d_to = min(dates), max(dates)
    span_days = max(1, (d_to - d_from).days + 1)

    best = max(daily, key=lambda kv: kv[1]) if daily else (None, 0)
    accts = aggregate_account_totals(rows)
    top_account = accts[0] if accts else (None, 0)

    return {
        'total_levels': total,
        'avg_per_day':  total / span_days,
        'best_day':     best,
        'top_account':  top_account,
    }


def daily_series_for_range(rows, date_from=None, date_to=None):
    """Return a complete, zero-filled, sorted list of (date_str, count) for
    every calendar day in [date_from, date_to] inclusive — used for the main
    chart so a quiet day shows as a real zero rather than a gap that makes
    the line jump between non-adjacent points. When no explicit bounds are
    given, the range is derived from the data's own earliest/latest date."""
    daily = dict(aggregate_daily_totals(rows))
    if not daily and not (date_from and date_to):
        return []
    if date_from and date_to:
        d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        d_to   = datetime.strptime(date_to,   '%Y-%m-%d').date()
    else:
        dates  = [datetime.strptime(d, '%Y-%m-%d').date() for d in daily]
        d_from, d_to = min(dates), max(dates)
    out = []
    d = d_from
    while d <= d_to:
        ds = d.strftime('%Y-%m-%d')
        out.append((ds, daily.get(ds, 0)))
        d += timedelta(days=1)
    return out


def distinct_skills(rows):
    """Sorted list of distinct skill names present in rows — for the skill filter dropdown."""
    return sorted({r['skill'] for r in rows})


def group_top_n_with_other(totals, n=5, other_label_fmt="Other ({count} skills)"):
    """Given (name, count) pairs already sorted descending by count, keep the
    top N individually and collapse everything else into one trailing
    ('Other (N skills)', summed_count) entry. Returns the input unchanged
    (as a new list) if there are N or fewer entries — no 'Other' bucket is
    added when there's nothing to collapse.

    Used by the Stats tab's Levels-by-Skill donut/bar panel so a long tail of
    rarely-trained skills doesn't clutter the chart. Generic over what the
    grouped items represent — the 'skills' wording in the default label is
    just this feature's only current caller; pass other_label_fmt to reuse
    it elsewhere.
    """
    if len(totals) <= n:
        return list(totals)
    top = list(totals[:n])
    rest = totals[n:]
    other_count = sum(c for _, c in rest)
    top.append((other_label_fmt.format(count=len(rest)), other_count))
    return top
