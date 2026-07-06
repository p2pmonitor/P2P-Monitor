"""
py/wom.py — Wise Old Man (WOM) integration for P2P Monitor's Goals & Maxing
feature: API client, on-disk cache, and time-to-99/time-to-max calculations.

Architecture (deliberate separation, per spec):
  - Config (py/config.py's cfg dict) stores SETTINGS/CUSTOMIZATIONS ONLY:
    WOM username mapping, global rate overrides, per-account rate overrides.
    Never holds fetched XP data.
  - Cache (this module's WOM_CACHE_FILE, ~/.p2p_monitor/wom_cache.json)
    stores FETCHED DATA ONLY: per-account skill levels/XP, last refresh
    timestamp, last refresh error. Never holds settings/overrides.
  - Computed values (time-to-99 per skill, time-to-max, closest-99) are
    deliberately NOT persisted into either file — they're derived fresh,
    on demand, from cache + current config every time they're needed. This
    is what makes "editing one rate immediately recalculates displayed
    estimates" trivially true: there's no stale precomputed number to
    invalidate, because nothing computed is ever cached as final.

All network calls use stdlib urllib only — no new dependency. Every
fetch_player() failure mode (not found, opted-out/private, network error,
timeout, malformed response, missing skill data) is caught and returned as
a (None, message) pair rather than raised, so a UI caller never needs a
broad try/except around this module to stay safe.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

WOM_CACHE_FILE = Path.home() / ".p2p_monitor" / "wom_cache.json"
WOM_API_BASE   = "https://api.wiseoldman.net/v2"
LEVEL_99_XP    = 13_034_431

# WOM's metric key for a skill is almost always just the lowercase OSRS
# skill name — the one real exception is Runecraft, which WOM calls
# 'runecrafting'. Everything else is handled by the .title() fallback in
# skill_display_name() below; this map only needs to list exceptions.
_WOM_SKILL_NAME_OVERRIDES = {
    'runecrafting': 'Runecraft',
}
# All 24 trainable skills WOM tracks (excludes 'overall', which isn't a
# real skill — it's WOM's combined-XP pseudo-metric).
WOM_SKILLS = (
    'attack', 'defence', 'strength', 'hitpoints', 'ranged', 'prayer', 'magic',
    'cooking', 'woodcutting', 'fletching', 'fishing', 'firemaking', 'crafting',
    'smithing', 'mining', 'herblore', 'agility', 'thieving', 'slayer',
    'farming', 'runecrafting', 'hunter', 'construction', 'sailing',
)


def skill_display_name(wom_metric):
    """WOM's lowercase skill metric -> the display name used as a
    DEFAULT_WOM_RATES key (e.g. 'firemaking' -> 'Firemaking',
    'runecrafting' -> 'Runecraft')."""
    return _WOM_SKILL_NAME_OVERRIDES.get(wom_metric, wom_metric.title())


# ── Default XP/hr rate table — exact table from the spec ───────────────────────
DEFAULT_WOM_RATES = {
    "Attack": {"xp_hr": 0, "mode": "excluded", "label": "Covered by Slayer"},
    "Strength": {"xp_hr": 0, "mode": "excluded", "label": "Covered by Slayer"},
    "Defence": {"xp_hr": 0, "mode": "excluded", "label": "Covered by Slayer"},
    "Ranged": {"xp_hr": 0, "mode": "excluded", "label": "Covered by Slayer"},
    "Hitpoints": {"xp_hr": 0, "mode": "excluded", "label": "Passive combat XP / covered by Slayer"},

    "Magic": {"xp_hr": 60000, "mode": "active", "label": "Ice giants / highest fire spell"},
    "Prayer": {"xp_hr": 180000, "mode": "active", "label": "Dragon bones, Wilderness altar"},
    "Runecraft": {"xp_hr": 40000, "mode": "active", "label": "Guardians of the Rift"},
    "Construction": {"xp_hr": 80000, "mode": "active", "label": "Mahogany Homes, teak planks"},
    "Agility": {"xp_hr": 45000, "mode": "active", "label": "Course progression"},
    "Herblore": {"xp_hr": 280000, "mode": "active", "label": "Best available potions"},
    "Thieving": {"xp_hr": 85000, "mode": "active", "label": "Guards \u2192 gnomes \u2192 elves"},
    "Crafting": {"xp_hr": 180000, "mode": "active", "label": "Cutting rubies"},
    "Fletching": {"xp_hr": 160000, "mode": "active", "label": "Stringing longbows"},
    "Slayer": {"xp_hr": 15000, "mode": "active", "label": "General Slayer progression"},
    "Hunter": {"xp_hr": 55000, "mode": "passive", "label": "Birdhouses"},
    "Mining": {"xp_hr": 50000, "mode": "active", "label": "Motherlode Mine"},
    "Smithing": {"xp_hr": 170000, "mode": "active", "label": "Blast Furnace gold bars"},
    "Fishing": {"xp_hr": 55000, "mode": "active", "label": "Tempoross \u2192 barb fishing"},
    "Cooking": {"xp_hr": 180000, "mode": "active", "label": "Lobsters \u2192 monkfish \u2192 anglers"},
    "Firemaking": {"xp_hr": 250000, "mode": "active", "label": "Wintertodt"},
    "Woodcutting": {"xp_hr": 60000, "mode": "active", "label": "Willows \u2192 redwoods"},
    "Farming": {"xp_hr": 72000, "mode": "passive", "label": "Hourly snape grass + best herb runs"},
    "Sailing": {"xp_hr": 60000, "mode": "active", "label": "Mercenary Shipwrecks"},
}


# ── Cache I/O ────────────────────────────────────────────────────────────────────
def load_wom_cache():
    """Load ~/.p2p_monitor/wom_cache.json. Returns {'accounts': {}} if the
    file is missing, empty, or corrupt — never raises."""
    try:
        if WOM_CACHE_FILE.exists():
            with open(WOM_CACHE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('accounts'), dict):
                return data
    except Exception:
        pass
    return {'accounts': {}}


def save_wom_cache(cache, log_fn=None):
    """Write the cache dict to disk. Never raises — a failed cache write
    should never crash a refresh that otherwise succeeded."""
    try:
        WOM_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WOM_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        if log_fn:
            log_fn(f"\u26a0 Could not write WOM cache: {e}")


# ── WOM API client ───────────────────────────────────────────────────────────────
class WomResult:
    """Result of a fetch_player() call. Exactly one of (skills is not None)
    or (error is not None) is true — never both, never neither."""
    __slots__ = ('skills', 'total_level', 'opted_out', 'error')

    def __init__(self, skills=None, total_level=None, opted_out=False, error=None):
        self.skills = skills
        self.total_level = total_level
        self.opted_out = opted_out
        self.error = error


def _skills_from_player_details(details):
    """Extract {SkillDisplayName: {'level': int, 'experience': int}} from a
    WOM PlayerDetails response. Returns ({} , None) on a structurally
    sound-but-empty response, or (None, error_message) if the skills data
    is missing/malformed in a way we can't safely interpret."""
    try:
        skills_raw = details['latestSnapshot']['data']['skills']
    except (KeyError, TypeError):
        return None, "Malformed response from Wise Old Man (no skill data)"
    if not isinstance(skills_raw, dict):
        return None, "Malformed response from Wise Old Man (no skill data)"

    out = {}
    for metric, entry in skills_raw.items():
        if metric == 'overall' or metric not in WOM_SKILLS:
            continue
        if not isinstance(entry, dict):
            continue
        level = entry.get('level')
        xp = entry.get('experience')
        if not isinstance(level, (int, float)) or not isinstance(xp, (int, float)):
            continue  # missing skill data for this one skill — skip it, don't fail the whole fetch
        out[skill_display_name(metric)] = {'level': int(level), 'experience': int(xp)}
    return out, None


def build_user_agent(username: str) -> str:
    """Build a safe anonymous WOM User-Agent from the WOM username.

    Use the same username we are refreshing. This avoids grouping all users
    under one shared fake User-Agent while not exposing app/repo details.
    """
    value = str(username or "").replace("\r", " ").replace("\n", " ").strip()

    # Remove other control characters while preserving normal OSRS spaces.
    value = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)

    # Keep it small and header-safe.
    value = value[:80].strip()

    return value or "OSRS Player"


def fetch_player(username, timeout=10):
    """
    POST /players/{username} — updates the player on WOM then returns their
    latest snapshot in the same call (the "update then fetch" behavior the
    spec asks for, in a single request).

    Sends the WOM username being requested as the User-Agent header too
    (see build_user_agent()) — WOM asks for a User-Agent that lets abusive
    traffic be identified, and the username is already the one piece of
    per-request identifying info this app has that isn't tied to the app/
    repo/DreamBot itself, and isn't shared across every installation.

    Returns a WomResult. Every failure mode is caught here:
      - HTTP 404                  -> error="Player not found on Wise Old Man"
      - other HTTP error          -> error="WOM API error (HTTP {code})"
      - network/timeout           -> error="Network error contacting Wise Old Man"
      - malformed/non-JSON body   -> error="Malformed response from Wise Old Man"
      - opted-out/private player  -> opted_out=True (not an error — a real,
                                      distinct state; the caller decides how
                                      to present it)
    Never raises.
    """
    username = (username or '').strip()
    if not username:
        return WomResult(error="No WOM username configured for this account")

    url = f"{WOM_API_BASE}/players/{urllib.request.quote(username)}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": build_user_agent(username),
    }
    req = urllib.request.Request(url, method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return WomResult(error="Player not found on Wise Old Man")
        return WomResult(error=f"WOM API error (HTTP {e.code})")
    except urllib.error.URLError:
        return WomResult(error="Network error contacting Wise Old Man")
    except Exception as e:
        return WomResult(error=f"Network error contacting Wise Old Man: {e}")

    try:
        details = json.loads(body)
    except Exception:
        return WomResult(error="Malformed response from Wise Old Man")
    if not isinstance(details, dict):
        return WomResult(error="Malformed response from Wise Old Man")

    annotations = details.get('annotations') or []
    opted_out = any(isinstance(a, dict) and a.get('type') == 'opt_out' for a in annotations)
    if opted_out:
        return WomResult(opted_out=True,
                          error="This player has opted out of Wise Old Man tracking")

    skills, err = _skills_from_player_details(details)
    if err:
        return WomResult(error=err)

    total_level = sum(s['level'] for s in skills.values()) if skills else 0
    return WomResult(skills=skills, total_level=total_level)


# ── Rate resolution (config overrides) ──────────────────────────────────────────
def effective_rate(skill, account, cfg):
    """
    Resolve the XP/hr rate actually in use for `skill` for `account`,
    applying the override order: per-account override > global override >
    baked-in DEFAULT_WOM_RATES default. mode/label always come from the
    static table — only the rate number itself is ever overridden (per
    spec: editing a rate never changes a skill's excluded/active
    classification).

    Returns (xp_hr, mode, label, source), where source is one of
    'Account override', 'Global override', or 'Default' — so callers can
    show exactly where a skill's rate actually came from, instead of
    every active skill looking identical regardless of whether it's been
    overridden.
    """
    default = DEFAULT_WOM_RATES.get(skill)
    if not default:
        return (None, 'unknown', '', 'Default')
    mode, label = default['mode'], default['label']
    account_overrides = (cfg.get('wom_account_rate_overrides') or {}).get(account) or {}
    global_overrides = cfg.get('wom_global_rate_overrides') or {}
    if skill in account_overrides:
        return (account_overrides[skill], mode, label, 'Account override')
    if skill in global_overrides:
        return (global_overrides[skill], mode, label, 'Global override')
    return (default['xp_hr'], mode, label, 'Default')


# ── Per-skill / per-account computed estimates (never persisted) ───────────────
def compute_skill_estimate(skill, level, xp, account, cfg):
    """
    One skill's progress toward 99 for one account, using whatever rate is
    *currently* configured (global/account override or default) — always
    computed fresh, never read back from a cache of old numbers. This is
    what makes editing a rate immediately reflect in displayed estimates:
    there's nothing stale to invalidate.

    status is one of: 'achieved', 'excluded', 'no_rate', 'active'.
    """
    xp_hr, mode, label, rate_source = effective_rate(skill, account, cfg)
    xp_left = max(0, LEVEL_99_XP - xp)
    if xp >= LEVEL_99_XP:
        status, hours = 'achieved', 0.0
    elif mode == 'excluded':
        status, hours = 'excluded', None
    elif not xp_hr or xp_hr <= 0:
        status, hours = 'no_rate', None
    else:
        status, hours = 'active', xp_left / xp_hr
    return {
        'skill': skill, 'level': level, 'experience': xp, 'xp_left': xp_left,
        'xp_hr': xp_hr, 'mode': mode, 'label': label, 'status': status,
        'hours_to_99': hours, 'rate_source': rate_source,
    }


def compute_account_summary(account, cfg, skills):
    """
    skills: {SkillDisplayName: {'level': int, 'experience': int}}, as
    stored in the cache for this account. Returns per-skill estimates plus
    the account-level rollups: time_to_max_hours (sum of every 'active'
    skill's hours_to_99 — excluded/achieved/no_rate skills never
    contribute), closest_99 (the active skill nearest to 99, or None),
    missing_rate_skills (tracked separately, never silently dropped, per
    spec), and total_level.
    """
    per_skill = []
    for skill in DEFAULT_WOM_RATES:
        s = skills.get(skill)
        if not s:
            continue
        per_skill.append(compute_skill_estimate(skill, s['level'], s['experience'], account, cfg))

    time_to_max = 0.0
    missing_rate_skills = []
    closest = None
    for est in per_skill:
        if est['status'] == 'active':
            time_to_max += est['hours_to_99']
            if closest is None or est['hours_to_99'] < closest['hours_to_99']:
                closest = est
        elif est['status'] == 'no_rate':
            missing_rate_skills.append(est['skill'])

    return {
        'account': account,
        'per_skill': per_skill,
        'time_to_max_hours': time_to_max if any(e['status'] == 'active' for e in per_skill) else None,
        'closest_99': closest,
        'missing_rate_skills': missing_rate_skills,
        'total_level': sum(e['level'] for e in per_skill) if per_skill else None,
    }


# ── Last-99 determination (history preferred over cache) ───────────────────────
def determine_last_99(history_levelup_rows, cached_skills):
    """
    Prefer a history levelup event with activity=='99' (has a real
    timestamp + is a genuine recorded event) over WOM cache data (which
    only shows a skill is *currently* at 99, with no record of when it was
    reached). If multiple history rows show a 99, the most recent wins.

    history_levelup_rows: list of dicts, each row MUST include
    'activity': '99' (this function only ever considers rows where that's
    true — it does not re-filter by level itself) plus 'value' (the skill
    name) and '_ts_epoch' (float unix timestamp). Callers are expected to
    pre-filter to level-99 rows themselves and set 'activity' accordingly;
    forgetting 'activity' here means every row is silently rejected and
    this function always falls through to the cache fallback below, even
    when real history exists — exactly the bug this comment is here to
    prevent from recurring.

    Skips any row/cached-skill named 'Combat' or 'Combat Level' entirely —
    Combat is a derived/composite level (computed from Attack/Strength/
    Defence/Hitpoints/Ranged/Magic/Prayer), not a real trainable skill, so
    it should never be reported as a "Last 99 Achieved" in its own right.

    Returns {'skill':, 'ts': float|None, 'source': 'history'|'cache'} or
    None if there's no real (non-Combat) 99 anywhere for this account.
    """
    best = None
    for r in (history_levelup_rows or []):
        if r.get('activity') != '99':
            continue
        skill = r.get('value', '')
        if str(skill or '').strip().lower() in ('combat', 'combat level'):
            continue
        ts = r.get('_ts_epoch')
        if best is None or (ts or 0) > (best['ts'] or 0):
            best = {'skill': skill, 'ts': ts, 'source': 'history'}
    if best:
        return best
    for skill, s in (cached_skills or {}).items():
        if str(skill or '').strip().lower() in ('combat', 'combat level'):
            continue
        if s.get('experience', 0) >= LEVEL_99_XP:
            return {'skill': skill, 'ts': None, 'source': 'cache'}
    return None


# ── Display formatting ──────────────────────────────────────────────────────────
def format_hours_compact(hours):
    """'<1h' / '~42h' / '~3,284h' — used in summary cards."""
    if hours is None:
        return '\u2014'
    if hours < 1:
        return '<1h'
    return f"~{round(hours):,}h"


def format_hours_precise(hours):
    """'<1h' / '~41h 54m' — used in the per-skill table's Time to 99 column."""
    if hours is None:
        return '\u2014'
    if hours < 1:
        return '<1h'
    total_minutes = round(hours * 60)
    h, m = divmod(total_minutes, 60)
    return f"~{h}h {m}m" if m else f"~{h}h"


def format_days(hours):
    """'(136.8 days)' secondary display under a compact hour total."""
    if hours is None:
        return ''
    return f"({hours / 24:.1f} days)"


# ── Refresh orchestration ────────────────────────────────────────────────────────
def refresh_account_in_cache(account, wom_username, cache, timeout=10):
    """
    Fetches fresh data for one account via fetch_player() and updates the
    cache dict IN PLACE for that account. Does not write to disk itself —
    callers refreshing multiple accounts should call save_wom_cache() once
    after the whole batch, to avoid redundant writes. On failure, any
    previously-cached skill data is deliberately left untouched (spec:
    "If refresh fails... keep old cache") — only last_refresh_error and
    last_refresh_ts change.

    Returns the WomResult, so the caller can show a non-fatal message on
    failure without needing to re-derive it from the cache.
    """
    result = fetch_player(wom_username, timeout=timeout)
    now = time.time()
    accounts = cache.setdefault('accounts', {})
    entry = accounts.setdefault(account, {})
    entry['wom_username'] = wom_username
    entry['last_refresh_ts'] = now
    if result.error:
        entry['last_refresh_error'] = result.error
    else:
        entry['last_refresh_error'] = None
        entry['skills'] = result.skills
        entry['total_level'] = result.total_level
    return result
