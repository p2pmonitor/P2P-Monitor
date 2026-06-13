"""
inferno.py — Stateful Inferno tracker for P2P Monitor v1.7.0

Tracks two separate concepts:
  1. Inferno Gear Check  — buffers resource failures, emits a single outcome
  2. Inferno Attempt     — tracks waves, caches ping, emits start/milestone/death/success

Soft patterns (regexes, milestone list, timeouts) are loaded from inferno_patterns.json
via py/inferno_rules.py (GitHub remote → cache → packaged JSON → emergency fallback).
Hard state logic lives here.

Usage (one InfernoTracker per AccountState):
    tracker = InfernoTracker(log_fn=self.log)
    ui_updates, discord_events = tracker.feed(lines)

    ui_updates     — list of (task, activity) tuples for status/monitor tab updates
    discord_events — list of event dicts in handle_event() schema (type='inferno')

Call tracker.reset() on script restart.
"""

import re
import time

from py.inferno_rules import get_patterns


# ── Strip helpers (mirrors reader.py — kept local to avoid circular import) ───

_STRIP_PREFIX_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\[[A-Z]+\]\s*>?\s*', re.IGNORECASE)
_LOG_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

def _strip(line):
    return _STRIP_PREFIX_RE.sub('', line)

def _ts(line):
    m = _LOG_TS_RE.match(line)
    return m.group(1) if m else None

def _fmt_duration(secs):
    if secs <= 0:
        return None
    h, rem = divmod(int(secs), 3600)
    m = rem // 60
    s = rem % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"

def _make_event(value, activity='', ts=None):
    from py.util import now_str
    return {
        'type':     'inferno',
        'value':    value,
        'activity': activity,
        'ts':       ts or now_str(),
    }


# ── InfernoTracker ─────────────────────────────────────────────────────────────

class InfernoTracker:
    """
    One instance per AccountState. Call feed(lines) on every live poll batch.
    Returns (ui_updates, discord_events).

    ui_updates      list of (task, activity) — always emitted for status tab
    discord_events  list of event dicts       — only outcome/milestone events
    """

    def __init__(self, log_fn=None):
        self._log_fn = log_fn
        self._p      = None   # compiled patterns, built on first feed()

        # ── Gear-check state ──────────────────────────────────────────────────
        self.gc_active          = False
        self.gc_started_at      = 0.0
        self.gc_resource_buffer = []   # list of "qty [items]" strings
        self.gc_suspicious      = []   # catch-all suspicious lines during window

        # ── Attempt state ─────────────────────────────────────────────────────
        self.attempt_active     = False
        self.attempt_started_at = 0.0
        self.current_wave       = 0
        self.highest_wave       = 0
        self.sent_milestones    = set()

        # ── Ping cache (cleared when attempt ends) ────────────────────────────
        self.ping_ms            = None
        self.required_ping_ms   = None
        self.bad_ping_override  = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def reset(self):
        """Call on script restart to wipe all state."""
        self.__init__(log_fn=self._log_fn)

    def feed(self, lines):
        """
        Process a batch of raw log lines.
        Returns (ui_updates, discord_events).
        """
        if self._p is None:
            self._p = _CompiledPatterns(get_patterns())

        ui_updates     = []
        discord_events = []

        for line in lines:
            b  = _strip(line).strip()
            ts = _ts(line)
            self._feed_line(b, line, ts, ui_updates, discord_events)

        return ui_updates, discord_events

    # ── Private line processor ─────────────────────────────────────────────────

    def _feed_line(self, b, raw, ts, ui, disc):
        p = self._p

        # ── Gear-check window timeout ─────────────────────────────────────────
        if self.gc_active and (time.time() - self.gc_started_at) > p.gc_timeout:
            self._close_gc_window(ui, disc, ts, timed_out=True)

        # ── Ping cache (always, regardless of attempt/gc state) ───────────────
        m = p.ping_re.search(b)
        if m:
            self.ping_ms          = int(m.group(1))
            self.required_ping_ms = int(m.group(2))
            return

        if any(pat.search(b) for pat in p.bad_ping_re):
            self.bad_ping_override = True
            return

        # ── Requirements failure — unconditional, no window needed ────────────
        # Specific enough phrase that false positives are essentially impossible.
        # Clears any open gear-check state.
        if any(pat.search(b) for pat in p.req_failed_re):
            self._dbg('Requirements not met (unconditional)')
            disc.append(_make_event('Inferno requirements not met', 'gear_check', ts))
            ui.append(('Inferno', 'Requirements not met'))
            self._reset_gc()
            return

        # ── Gear-check window open ────────────────────────────────────────────
        if not self.gc_active and not self.attempt_active:
            if any(pat.search(b) for pat in p.gc_start_re):
                self.gc_active     = True
                self.gc_started_at = time.time()
                self._dbg('Gear-check window opened')
                ui.append(('Inferno', 'Gear Check'))
                return

        # ── Inside gear-check window ──────────────────────────────────────────
        if self.gc_active:
            # Gear check pass
            if any(pat.search(b) for pat in p.gc_pass_re):
                self._dbg('Gear check passed')
                disc.append(_make_event('Inferno gear check passed', 'gear_check', ts))
                ui.append(('Inferno', 'Gear check passed'))
                self._reset_gc()
                return

            # Resource check failure (no-colon format only — prevents Fishing/Questing FP)
            m = p.resource_re.search(b)
            if m:
                qty   = m.group(1)
                items = m.group(2)
                self.gc_resource_buffer.append(f"{qty} [{items}]")
                return

            # Window close triggers (without a pass)
            if any(pat.search(b) for pat in p.gc_reset_re):
                self._close_gc_window(ui, disc, ts, timed_out=False)
                # fall through — the line may also carry other state

            # Suspicious catch-all — lines with failure keywords not handled above
            elif p.suspicious_re and p.suspicious_re.search(b):
                self.gc_suspicious.append(b[:120])   # cap line length

        # ── Attempt active: wave / death / success ────────────────────────────
        if self.attempt_active:
            wave = self._parse_wave(b)
            if wave is not None:
                self._on_wave(wave, ts, ui, disc)
                return

            if any(pat.search(b) for pat in p.death_re):
                self._on_death(ts, ui, disc)
                return

            m = p.success_re.search(b)
            if m:
                self._on_success(m.group(1), ts, ui, disc)
                return

        # ── Wave 1 starts attempt (only when not already active) ──────────────
        if not self.attempt_active:
            wave = self._parse_wave(b)
            if wave == 1:
                self._start_attempt(ts, ui, disc)
                return

    # ── Gear-check helpers ─────────────────────────────────────────────────────

    def _close_gc_window(self, ui, disc, ts, timed_out=False):
        """Close gear-check window without a pass — emit failure if warranted."""
        if not self.gc_active:
            return

        if self.gc_resource_buffer:
            # Include capped resource detail in the user-facing message
            cap   = self._p.resource_detail_cap
            shown = self.gc_resource_buffer[:cap]
            detail = '; '.join(shown)
            if len(self.gc_resource_buffer) > cap:
                detail += f' (+{len(self.gc_resource_buffer) - cap} more)'
            msg = f'Inferno gear check failed: missing usable gear/supplies — {detail}'
            disc.append(_make_event(msg, 'gear_check', ts))
            ui.append(('Inferno', 'Gear check failed'))
            self._dbg(f'Gear check failed (resource): {detail}')

        elif self.gc_suspicious and not timed_out:
            cap    = 3
            detail = '; '.join(self.gc_suspicious[:cap])
            if len(self.gc_suspicious) > cap:
                detail += f' (+{len(self.gc_suspicious) - cap} more)'
            msg = f'Inferno gear check failed: unknown reason — {detail}'
            disc.append(_make_event(msg, 'gear_check', ts))
            ui.append(('Inferno', 'Gear check failed'))
            self._dbg(f'Gear check failed (unknown): {detail}')

        # Timed out or silent close (no resource/suspicious lines) — no event
        self._reset_gc()

    def _reset_gc(self):
        self.gc_active          = False
        self.gc_started_at      = 0.0
        self.gc_resource_buffer = []
        self.gc_suspicious      = []

    # ── Attempt helpers ────────────────────────────────────────────────────────

    def _parse_wave(self, b):
        p = self._p
        m = p.wave_game_re.search(b)
        if m:
            return int(m.group(1))
        m = p.wave_internal_re.search(b)
        if m:
            return int(m.group(1))
        return None

    def _start_attempt(self, ts, ui, disc):
        self.attempt_active     = True
        self.attempt_started_at = time.time()
        self.current_wave       = 1
        self.highest_wave       = 1
        self.sent_milestones    = set()

        msg = 'Inferno started — Wave 1'
        if self.ping_ms is not None:
            msg += f', ping {self.ping_ms}ms'
            if self.bad_ping_override:
                msg += ', high ping override used'

        self._dbg(msg)
        disc.append(_make_event(msg, 'attempt_start', ts))
        ui.append(('Inferno', 'Wave 1'))

    def _on_wave(self, wave, ts, ui, disc):
        # Deduplicate Wave 1 — fires twice in real logs (pre- and post-tick-engine)
        if wave == 1 and self.current_wave == 1:
            return

        self.current_wave = wave
        if wave > self.highest_wave:
            self.highest_wave = wave

        # Always update status tab for every wave
        ui.append(('Inferno', f'Wave {wave}'))

        # Discord only for milestone waves (Wave 1 already sent at start)
        if wave in self._p.milestones and wave not in self.sent_milestones:
            self.sent_milestones.add(wave)
            msg = f'Inferno update — reached Wave {wave}'
            self._dbg(msg)
            disc.append(_make_event(msg, 'wave', ts))

    def _on_death(self, ts, ui, disc):
        wave = self.highest_wave
        msg  = f'Inferno failed — died on Wave {wave}'
        dur  = _fmt_duration(time.time() - self.attempt_started_at)
        if dur:
            msg += f' after {dur}'
        if self.ping_ms is not None:
            msg += f', ping {self.ping_ms}ms'

        self._dbg(msg)
        disc.append(_make_event(msg, 'death', ts))
        ui.append(('Inferno', f'Died on Wave {wave}'))
        self._reset_attempt()

    def _on_success(self, kc, ts, ui, disc):
        msg = f'Inferno successful — completed Wave 69, TzKal-Zuk KC: {kc}'
        self._dbg(msg)
        disc.append(_make_event(msg, 'success', ts))
        ui.append(('Inferno', f'Completed / TzKal-Zuk KC {kc}'))
        self._reset_attempt()

    def _reset_attempt(self):
        self.attempt_active     = False
        self.attempt_started_at = 0.0
        self.current_wave       = 0
        self.highest_wave       = 0
        self.sent_milestones    = set()
        self.ping_ms            = None
        self.required_ping_ms   = None
        self.bad_ping_override  = False

    def _dbg(self, msg):
        if self._log_fn:
            self._log_fn(f'[inferno] {msg}')


# ── Compiled pattern cache ─────────────────────────────────────────────────────

class _CompiledPatterns:
    """Pre-compile all regexes from the validated pattern dict."""

    def __init__(self, p):
        def _c(pattern):
            return re.compile(pattern, re.IGNORECASE)

        def _cl(patterns):
            return [_c(pat) for pat in patterns]

        self.gc_start_re   = _cl(p.get('gear_check_start_patterns', []))
        self.gc_pass_re    = _cl(p.get('gear_check_pass_patterns', []))
        self.req_failed_re = _cl(p.get('requirements_failed_patterns', []))
        self.gc_reset_re   = _cl(p.get('gear_check_reset_patterns', []))
        self.bad_ping_re   = _cl(p.get('bad_ping_override_patterns', []))
        self.death_re      = _cl(p.get('death_patterns', []))

        self.resource_re      = _c(p.get('resource_check_failed_pattern',
                                         r'Resource check failed\s+(\S+)\s+\[(.+?)\]'))
        self.ping_re          = _c(p.get('ping_pattern',
                                         r'Ping is\s+(\d+)\s+needs to be\s+(\d+)\s+or less for Inferno'))
        self.wave_game_re     = _c(p.get('wave_game_pattern',    r'\[GAME\].*?Wave:\s*(\d+)'))
        self.wave_internal_re = _c(p.get('wave_internal_pattern', r'\bWAVE\s+(\d+)\b'))
        self.success_re       = _c(p.get('success_pattern',
                                         r'(?i)Your TzKal-Zuk kill count is:\s*(?:<[^>]+>)*(\d+)'))

        sus = p.get('suspicious_failure_pattern')
        self.suspicious_re = _c(sus) if sus else None

        self.milestones          = set(p.get('milestone_waves', [7,15,24,31,41,48,56,63,67,68,69]))
        self.gc_timeout          = p.get('gear_check_window_timeout_sec', 300)
        self.resource_detail_cap = p.get('resource_detail_cap', 6)
