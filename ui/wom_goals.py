"""
ui/wom_goals.py — Goals & Maxing (WOM) page for the Stats tab.

Lazily constructed by ui/stats_tab.py's _ensure_goals_maxing_built() on
first click into this section — never built eagerly, mirroring that
file's own established prewarm/lazy-build philosophy.

Architecture (mirrors py/wom.py's own separation):
  - self._cache (loaded once, refreshed after each WOM refresh) holds
    FETCHED data only — never edited directly by this UI.
  - app.cfg holds SETTINGS only (WOM username mapping, rate overrides) —
    edited via the Edit XP Rates dialog, never holds fetched XP.
  - Every displayed estimate (time-to-99, time-to-max, closest-99) is
    computed fresh from cache + cfg via py.wom.compute_account_summary()
    each time this page (re)renders — never a stale frozen number, which
    is what makes "editing a rate immediately recalculates" trivially
    true.

Threading: the only network-touching operation here is "Refresh WOM",
which always runs on a background thread (py.wom.refresh_account_in_cache
does the actual HTTP call) guarded by self._refresh_in_flight against
overlapping clicks, with results handed back via app.after(0, ...).
Opening this page, switching the account selector, and editing rates are
all pure in-memory/cache operations — no network, no file I/O on the Tk
main thread either way.
"""
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from py.config import save_config
from py.history import load_history_accounts
import py.wom as wom


class WomGoalsPage:
    def __init__(self, app, parent_frame):
        self.app = app
        self.frame = parent_frame
        self._cache = wom.load_wom_cache()
        self._refresh_in_flight = False
        self._selected_account = 'All Accounts'
        # Last-99 history data — populated once by a background scan (see
        # _kick_off_last99_scan), never read synchronously from disk during
        # render. Empty until the scan completes, at which point
        # determine_last_99() naturally falls back to WOM cache for
        # anything not yet found — see _last_99_rows_for().
        self._last99_history_rows = {}
        self._last99_scan_done = False
        self._build()
        self._rerender()
        self._kick_off_last99_scan()

    # ── Known accounts ──────────────────────────────────────────────────────────
    def _known_accounts(self):
        """Every account this app knows about from any source — history,
        Launcher presets, or existing WOM cache — deduplicated and
        sorted. Deliberately not limited to accounts with WOM data
        already, so a user never has to pre-configure anything before
        opening this page (per spec)."""
        names = set()
        try:
            names.update(load_history_accounts())
        except Exception:
            pass
        for p in self.app.cfg.get('launcher_presets', []):
            acc = p.get('account', '').strip()
            if acc:
                names.add(acc)
        names.update(self._cache.get('accounts', {}).keys())
        return sorted(names)

    def _wom_username_for(self, account):
        """Account name is the default WOM username (per spec) unless an
        explicit mapping override exists in config."""
        return (self.app.cfg.get('wom_username_map') or {}).get(account, account)

    # ── Build (scaffold only — content is rebuilt per-render in _rerender) ─────
    def _build(self):
        app = self.app
        root = tk.Frame(self.frame, bg=app.BG2, padx=16, pady=16)
        root.pack(fill='both', expand=True)

        toolbar = tk.Frame(root, bg=app.BG2)
        toolbar.pack(fill='x', pady=(0, 10))

        title_block = tk.Frame(toolbar, bg=app.BG2)
        title_block.pack(side='left')
        self._header_lbl = tk.Label(title_block, text="GOALS & MAXING", font=(app.SANS[0], 11, 'bold'),
                                     bg=app.BG2, fg=app.FG)
        self._header_lbl.pack(side='left')
        self._subtitle_lbl = tk.Label(title_block, text="  ·  All accounts", font=app.SANSS,
                                       bg=app.BG2, fg=app.FG2)
        self._subtitle_lbl.pack(side='left', pady=(2, 0))

        self._account_var = tk.StringVar(value='All Accounts')
        self._account_combo = ttk.Combobox(toolbar, textvariable=self._account_var, state='readonly',
                                            font=app.SANSS, width=9)
        self._account_combo.pack(side='left', padx=(10, 0), ipady=1)
        self._account_combo.bind('<<ComboboxSelected>>', lambda e: self._on_account_changed())

        btn_row = tk.Frame(toolbar, bg=app.BG2)
        btn_row.pack(side='right')
        self._refresh_btn = tk.Button(btn_row, text='🔄  Refresh', font=app.SANSS,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=5, pady=3, cursor='hand2',
            command=self._on_refresh_click)
        self._refresh_btn.pack(side='left', padx=(0, 3))
        tk.Button(btn_row, text='✏  Edit XP Rates', font=app.SANSS, bg=app.BG4, fg=app.ACC2,
                  relief='flat', padx=5, pady=3, cursor='hand2',
                  command=self._open_edit_rates_dialog).pack(side='left', padx=(0, 3))
        tk.Button(btn_row, text='↺  Reset Defaults', font=app.SANSS, bg=app.BG4, fg=app.FG2,
                  relief='flat', padx=5, pady=3, cursor='hand2',
                  command=self._on_reset_defaults_click).pack(side='left')

        self._username_row_area = tk.Frame(root, bg=app.BG2)
        self._username_row_area.pack(fill='x', pady=(0, 10))

        self._cards_row = tk.Frame(root, bg=app.BG2)
        self._cards_row.pack(fill='x', pady=(0, 12))

        self._content_area = tk.Frame(root, bg=app.BG2)
        self._content_area.pack(fill='both', expand=True)

        self._footer_area = tk.Frame(root, bg=app.BG2)
        self._footer_area.pack(fill='x', pady=(12, 0))

        self._refresh_account_dropdown_values()

    def _refresh_account_dropdown_values(self):
        accounts = self._known_accounts()
        self._account_combo.configure(values=['All Accounts'] + accounts)
        if self._account_var.get() not in (['All Accounts'] + accounts):
            self._account_var.set('All Accounts')
            self._selected_account = 'All Accounts'

    def _on_account_changed(self):
        self._selected_account = self._account_var.get()
        self._rerender()

    # ── Data helpers ─────────────────────────────────────────────────────────────
    def _kick_off_last99_scan(self):
        """
        One-time background scan of every known account's level-99 history
        events — the only place py.stats.load_levelup_rows() (which reads
        history files from disk) is ever called by this page. Runs once
        per page instance: this page is itself only ever lazily built
        once (ui/stats_tab.py never rebuilds it while the app is running),
        so a single scan at construction time is sufficient — a fresh app
        launch naturally re-scans when the page is built again.

        Never touches the Tk main thread for the actual file reads; only
        the final result + re-render happens via app.after(0, ...).
        """
        app = self.app
        accounts = self._known_accounts()

        def _do():
            from py.stats import load_levelup_rows
            from py.history import _parse_ts
            result = {}
            for account in accounts:
                try:
                    rows = load_levelup_rows(accounts=[account])
                except Exception:
                    rows = []
                result[account] = [
                    {'value': r['skill'], 'activity': '99', '_ts_epoch': _parse_ts(r['time'])}
                    for r in rows if r['level'] == 99
                ]
            app.after(0, lambda: self._on_last99_scan_done(result))
        threading.Thread(target=_do, daemon=True).start()

    def _on_last99_scan_done(self, result):
        """Main-thread callback once the background scan above completes.
        Re-renders so any account whose true last-99 came from history
        (rather than the WOM-cache fallback shown until now) immediately
        reflects the more authoritative answer."""
        self._last99_history_rows = result
        self._last99_scan_done = True
        self._rerender()

    def _last_99_rows_for(self, account):
        """Pure in-memory lookup — no I/O. Returns whatever the background
        scan has found for this account so far; empty before the scan
        completes, in which case determine_last_99() falls back to WOM
        cache data (labeled 'from WOM cache' in the UI) until the real
        history-based answer arrives and triggers a re-render."""
        return self._last99_history_rows.get(account, [])

    def _account_skills(self, account):
        return (self._cache.get('accounts', {}).get(account, {}) or {}).get('skills') or {}

    def _account_entry(self, account):
        return self._cache.get('accounts', {}).get(account, {}) or {}

    def _last_99_for_account(self, account):
        """Single account's 'Last 99 Achieved' — history preferred over
        cache, per spec. Returns {'skill':, 'ts':, 'source':} or None."""
        return wom.determine_last_99(self._last_99_rows_for(account), self._account_skills(account))

    def _last_99_overall(self):
        """System-wide 'Last 99 Achieved' across every known account —
        runs the single-account determination per account, then picks
        the most recent result. Cache-sourced results (ts=None, since the
        cache only knows a skill IS at 99, not when) are treated as older
        than any history-sourced result for cross-account comparison."""
        best = None
        for account in self._known_accounts():
            result = self._last_99_for_account(account)
            if not result:
                continue
            result_ts = result['ts'] if result['ts'] is not None else -1
            best_ts = (best['ts'] if best and best['ts'] is not None else -1)
            if best is None or result_ts > best_ts:
                best = dict(result, account=account)
        return best

    def _all_account_summaries(self):
        """compute_account_summary() for every known account that has
        cached skill data. Accounts with no cache data yet are simply
        excluded here — they still appear in the account table with
        dashes, handled separately in _render_all_accounts_view()."""
        out = {}
        for account in self._known_accounts():
            skills = self._account_skills(account)
            if not skills:
                continue
            out[account] = wom.compute_account_summary(account, self.app.cfg, skills)
        return out

    # ── Render dispatch ──────────────────────────────────────────────────────────
    def _rerender(self):
        self._refresh_account_dropdown_values()
        for w in self._username_row_area.winfo_children():
            w.destroy()
        for w in self._cards_row.winfo_children():
            w.destroy()
        for w in self._content_area.winfo_children():
            w.destroy()
        for w in self._footer_area.winfo_children():
            w.destroy()

        if self._selected_account == 'All Accounts':
            self._subtitle_lbl.configure(text="  ·  All accounts")
            self._render_all_accounts_view()
        else:
            self._subtitle_lbl.configure(text=f"  ·  Account: {self._selected_account}")
            self._render_wom_username_row(self._selected_account)
            self._render_single_account_view(self._selected_account)

    # ── Shared small helpers ─────────────────────────────────────────────────────
    def _summary_card(self, parent, icon, title, icon_color=None):
        app = self.app
        cell = tk.Frame(parent, bg=app.BG3, padx=9, pady=6)
        cell.pack(side='left', fill='both', expand=True, padx=(0, 8))
        top = tk.Frame(cell, bg=app.BG3)
        top.pack(fill='x', anchor='w')
        tk.Label(top, text=icon, font=(app.SANS[0], 11), bg=app.BG3,
                 fg=icon_color or app.ACC).pack(side='left', padx=(0, 4))
        tk.Label(top, text=title, font=app.SANSS, bg=app.BG3, fg=app.FG2).pack(side='left')
        val_lbl = tk.Label(cell, text="—", font=(app.SANS[0], 12), bg=app.BG3,
                            fg=app.FG, anchor='w', justify='left', wraplength=180)
        val_lbl.pack(fill='x', anchor='w', pady=(2, 0))
        sub_lbl = tk.Label(cell, text="", font=app.SANSS, bg=app.BG3, fg=app.FG2, anchor='w')
        sub_lbl.pack(fill='x', anchor='w')
        return val_lbl, sub_lbl

    def _ago(self, ts):
        if ts is None:
            return ''
        secs = max(0, int(time.time() - ts))
        if secs < 60:
            return f"{secs}s ago"
        mins, secs = divmod(secs, 60)
        if mins < 60:
            return f"{mins}m ago"
        hours, mins = divmod(mins, 60)
        if hours < 24:
            return f"{hours}h {mins}m ago"
        days, hours = divmod(hours, 24)
        return f"{days}d ago"

    def _last_refresh_overall(self):
        """Most recent last_refresh_ts across every cached account, plus
        a clock-time string for the 'Today, HH:MM:SS' style display."""
        accounts = self._cache.get('accounts', {})
        ts_values = [e.get('last_refresh_ts') for e in accounts.values() if e.get('last_refresh_ts')]
        if not ts_values:
            return None, ''
        ts = max(ts_values)
        from datetime import datetime as _dt
        dt = _dt.fromtimestamp(ts)
        today = _dt.now().date() == dt.date()
        prefix = "Today" if today else dt.strftime('%b %d')
        return ts, f"{prefix}, {dt.strftime('%H:%M:%S')}"

    # ── All Accounts view ────────────────────────────────────────────────────────
    def _render_all_accounts_view(self):
        app = self.app
        summaries = self._all_account_summaries()

        # Closest to max
        closest_max = None
        for acc, s in summaries.items():
            ttm = s.get('time_to_max_hours')
            if ttm and ttm > 0 and (closest_max is None or ttm < closest_max[1]):
                closest_max = (acc, ttm, s.get('total_level'))
        val, sub = self._summary_card(self._cards_row, '🏆', 'CLOSEST TO MAX', app.YEL)
        if closest_max:
            acc, ttm, total_level = closest_max
            val.configure(text=acc)
            sub.configure(text=f"{wom.format_hours_compact(ttm)} left"
                                + (f"  •  Total level: {total_level}" if total_level else ""))
        else:
            val.configure(text="No data yet")
            sub.configure(text="")

        # Closest 99 (across all accounts)
        closest_99 = None
        for acc, s in summaries.items():
            c99 = s.get('closest_99')
            if c99 and (closest_99 is None or c99['hours_to_99'] < closest_99[1]['hours_to_99']):
                closest_99 = (acc, c99)
        val, sub = self._summary_card(self._cards_row, '⭐', 'CLOSEST 99', app.GREEN)
        if closest_99:
            acc, est = closest_99
            val.configure(text=est['skill'])
            sub.configure(text=f"{wom.format_hours_compact(est['hours_to_99'])} left  •  {acc}")
        else:
            val.configure(text="No data yet")
            sub.configure(text="")

        # Last 99 achieved (system-wide)
        last99 = self._last_99_overall()
        val, sub = self._summary_card(self._cards_row, '📈', 'LAST 99 ACHIEVED', app.ACC2)
        if last99:
            val.configure(text=last99['skill'])
            age = self._ago(last99['ts']) if last99['ts'] else 'from WOM cache'
            sub.configure(text=f"{last99['account']}  •  {age}")
        else:
            val.configure(text="None yet")
            sub.configure(text="")

        # Last WOM refresh
        ts, ts_str = self._last_refresh_overall()
        val, sub = self._summary_card(self._cards_row, '🔄', 'LAST WOM REFRESH', app.ACC)
        if ts:
            val.configure(text=self._ago(ts))
            sub.configure(text=ts_str)
        else:
            val.configure(text="Never")
            sub.configure(text="")

        self._render_account_table(summaries)

    def _render_account_table(self, summaries):
        app = self.app
        accounts = self._known_accounts()
        if not accounts:
            empty = tk.Frame(self._content_area, bg=app.BG2)
            empty.pack(fill='both', expand=True)
            tk.Label(empty, text="No WOM data yet", font=app.SANSL, bg=app.BG2, fg=app.FG
                     ).pack(pady=(40, 4))
            tk.Label(empty, text="Click Refresh WOM to fetch account stats", font=app.SANS,
                     bg=app.BG2, fg=app.FG2).pack()
            return

        card = tk.Frame(self._content_area, bg=app.BG3, padx=2, pady=2)
        card.pack(fill='both', expand=True)
        cols = ('account', 'total_level', 'last99', 'time_to_max', 'closest99', 'last_refresh')
        tree = ttk.Treeview(card, columns=cols, show='headings', height=min(max(len(accounts), 3), 10))
        headers = [('account', 'ACCOUNT', 115), ('total_level', 'TOTAL LEVEL', 85),
                   ('last99', 'LAST 99', 100), ('time_to_max', 'TIME TO MAX', 95),
                   ('closest99', 'CLOSEST 99', 115), ('last_refresh', 'LAST REFRESH', 90)]
        for col, label, width in headers:
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor='w')
        scr = ttk.Scrollbar(card, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scr.set)
        scr.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)

        for account in accounts:
            s = summaries.get(account)
            entry = self._account_entry(account)
            if not s:
                tree.insert('', 'end', values=(account, '—', '—', '—', '—',
                                                self._ago(entry.get('last_refresh_ts')) or 'Never'))
                continue
            last99 = self._last_99_for_account(account)
            last99_text = f"{last99['skill']}" if last99 else '—'
            ttm_text = wom.format_hours_compact(s.get('time_to_max_hours'))
            c99 = s.get('closest_99')
            c99_text = f"{c99['skill']} ({c99['level']})" if c99 else '—'
            tree.insert('', 'end', values=(
                account, s.get('total_level') or '—', last99_text, ttm_text, c99_text,
                self._ago(entry.get('last_refresh_ts')) or 'Never'))

    # ── Single Account view ──────────────────────────────────────────────────────
    def _render_wom_username_row(self, account):
        """Simple WOM username override for this account — needed when the
        DreamBot account name differs from the actual WOM username.
        Defaults to the account name. Saving only updates config; it
        deliberately does NOT trigger a refresh itself — Refresh WOM
        stays a separate, explicit action either way."""
        app = self.app
        row = tk.Frame(self._username_row_area, bg=app.BG3, padx=8, pady=5)
        row.pack(fill='x')
        tk.Label(row, text='WOM Username:', font=app.SANSS, bg=app.BG3, fg=app.FG2
                 ).pack(side='left', padx=(0, 6))
        var = tk.StringVar(value=self._wom_username_for(account))
        entry = tk.Entry(row, textvariable=var, font=app.SANSS, bg=app.BG4, fg=app.FG,
                          insertbackground=app.ACC, relief='flat', width=16)
        entry.pack(side='left', ipady=2, padx=(0, 6))
        note_lbl = tk.Label(row, text='', font=app.SANSS, bg=app.BG3, fg=app.GREEN)
        note_lbl.pack(side='left', padx=(0, 6))

        def _save():
            new_username = var.get().strip() or account
            var.set(new_username)
            mapping = self.app.cfg.setdefault('wom_username_map', {})
            if new_username == account:
                mapping.pop(account, None)  # matches the default — no override needed
            else:
                mapping[account] = new_username
            save_config(self.app.cfg)
            note_lbl.configure(text='Saved ✓')
            self.app.after(1500, lambda: note_lbl.configure(text='') if note_lbl.winfo_exists() else None)

        tk.Button(row, text='Save', font=app.SANSS, bg=app.BG4, fg=app.ACC, relief='flat',
                  padx=8, pady=3, cursor='hand2', command=_save).pack(side='left')
        tk.Label(row, text="Used when the DreamBot account name differs from the WOM username.",
                 font=app.SANSS, bg=app.BG3, fg=app.FG2, wraplength=280, justify='left'
                 ).pack(side='left', padx=(12, 0))

    def _render_single_account_view(self, account):
        app = self.app
        skills = self._account_skills(account)
        entry = self._account_entry(account)

        if not skills:
            val, sub = self._summary_card(self._cards_row, '⏱', 'TIME TO MAX', app.ACC)
            val.configure(text="—")
            val, sub = self._summary_card(self._cards_row, '⭐', 'CLOSEST 99', app.GREEN)
            val.configure(text="—")
            val, sub = self._summary_card(self._cards_row, '📈', 'LAST 99 ACHIEVED', app.ACC2)
            val.configure(text="—")
            ts = entry.get('last_refresh_ts')
            val, sub = self._summary_card(self._cards_row, '🔄', 'LAST WOM REFRESH', app.ACC)
            val.configure(text=self._ago(ts) if ts else "Never")

            empty = tk.Frame(self._content_area, bg=app.BG2)
            empty.pack(fill='both', expand=True)
            tk.Label(empty, text="No WOM data yet", font=app.SANSL, bg=app.BG2, fg=app.FG
                     ).pack(pady=(40, 4))
            tk.Label(empty, text="Click Refresh WOM to fetch account stats", font=app.SANS,
                     bg=app.BG2, fg=app.FG2).pack()
            err = entry.get('last_refresh_error')
            if err:
                tk.Label(empty, text=f"Last attempt failed: {err}", font=app.SANSS,
                         bg=app.BG2, fg=app.RED).pack(pady=(8, 0))
            return

        summary = wom.compute_account_summary(account, app.cfg, skills)

        val, sub = self._summary_card(self._cards_row, '⏱', 'TIME TO MAX', app.ACC)
        ttm = summary.get('time_to_max_hours')
        val.configure(text=wom.format_hours_compact(ttm))
        sub.configure(text=wom.format_days(ttm))

        val, sub = self._summary_card(self._cards_row, '⭐', 'CLOSEST 99', app.GREEN)
        c99 = summary.get('closest_99')
        if c99:
            val.configure(text=c99['skill'])
            sub.configure(text=wom.format_hours_precise(c99['hours_to_99']))
        else:
            val.configure(text="—")

        val, sub = self._summary_card(self._cards_row, '📈', 'LAST 99 ACHIEVED', app.ACC2)
        last99 = self._last_99_for_account(account)
        if last99:
            val.configure(text=last99['skill'])
            sub.configure(text=self._ago(last99['ts']) if last99['ts'] else 'from WOM cache')
        else:
            val.configure(text="None yet")

        val, sub = self._summary_card(self._cards_row, '🔄', 'LAST WOM REFRESH', app.ACC)
        ts = entry.get('last_refresh_ts')
        if ts:
            val.configure(text=self._ago(ts))
            from datetime import datetime as _dt
            dt = _dt.fromtimestamp(ts)
            today = _dt.now().date() == dt.date()
            sub.configure(text=f"{'Today' if today else dt.strftime('%b %d')}, {dt.strftime('%H:%M:%S')}")
        else:
            val.configure(text="Never")

        self._render_skill_table(account, summary)
        self._render_single_account_footer(summary)

    def _render_skill_table(self, account, summary):
        app = self.app
        card = tk.Frame(self._content_area, bg=app.BG3, padx=2, pady=2)
        card.pack(fill='both', expand=True)
        cols = ('skill', 'level', 'xp', 'xp_left', 'rate', 'time_to_99', 'source')
        per_skill = summary['per_skill']
        tree = ttk.Treeview(card, columns=cols, show='headings',
                             height=min(max(len(per_skill), 3), 10))
        headers = [('skill', 'Skill', 100), ('level', 'Level', 50), ('xp', 'Current XP', 85),
                   ('xp_left', 'XP left', 80), ('rate', 'Rate/hr', 70),
                   ('time_to_99', 'Time to 99', 85), ('source', 'Source', 85)]
        for col, label, width in headers:
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor='w')
        scr = ttk.Scrollbar(card, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scr.set)
        scr.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)

        tree.tag_configure('achieved', foreground=app.ACC2)
        tree.tag_configure('excluded', foreground=app.FG2)
        tree.tag_configure('no_rate', foreground=app.RED)

        # Sort: active skills by shortest time-to-99 first (clearest single
        # ordering per spec — "pick the clearest option and keep it
        # consistent"), achieved skills next, excluded skills grouped at
        # the very bottom regardless of anything else.
        active = sorted((e for e in per_skill if e['status'] == 'active'),
                         key=lambda e: e['hours_to_99'])
        no_rate = [e for e in per_skill if e['status'] == 'no_rate']
        achieved = [e for e in per_skill if e['status'] == 'achieved']
        excluded = [e for e in per_skill if e['status'] == 'excluded']
        ordered = active + no_rate + achieved + excluded

        for e in ordered:
            if e['status'] == 'achieved':
                vals = (e['skill'], e['level'], f"{e['experience']:,}", '—', '—', '—', 'Achieved')
                tag = 'achieved'
            elif e['status'] == 'excluded':
                vals = (e['skill'], e['level'], f"{e['experience']:,}", '—', '—', '—', e['label'])
                tag = 'excluded'
            elif e['status'] == 'no_rate':
                vals = (e['skill'], e['level'], f"{e['experience']:,}", f"{e['xp_left']:,}",
                        '—', '—', 'No rate')
                tag = 'no_rate'
            else:
                vals = (e['skill'], e['level'], f"{e['experience']:,}", f"{e['xp_left']:,}",
                        f"{e['xp_hr']:,}", wom.format_hours_precise(e['hours_to_99']),
                        e['rate_source'])
                tag = ''
            tree.insert('', 'end', values=vals, tags=(tag,) if tag else ())

    def _render_single_account_footer(self, summary):
        app = self.app
        ttm = summary.get('time_to_max_hours')
        val, _ = self._summary_card(self._footer_area, '⏱', 'ESTIMATED TIME TO MAX', app.ACC)
        val.configure(text=f"{wom.format_hours_compact(ttm)}  {wom.format_days(ttm)}".strip())

        val, _ = self._summary_card(self._footer_area, '⚔', 'COMBAT SKILLS COVERED BY SLAYER', app.RED)
        excluded_names = [e['skill'] for e in summary['per_skill'] if e['status'] == 'excluded']
        val.configure(text=", ".join(excluded_names) if excluded_names else "—",
                       font=app.SANS)

        val, _ = self._summary_card(self._footer_area, 'ℹ', 'ABOUT XP RATES', app.FG2)
        val.configure(text='XP rates are default, editable estimates.\nAdjust in "Edit XP Rates" to refine calculations.',
                      font=app.SANSS)

    # ── Refresh WOM ──────────────────────────────────────────────────────────────
    def _on_refresh_click(self):
        if self._refresh_in_flight:
            return
        accounts = self._known_accounts() if self._selected_account == 'All Accounts' \
            else [self._selected_account]
        if not accounts:
            messagebox.showinfo('Goals & Maxing', 'No accounts to refresh yet.')
            return
        self._set_refresh_in_flight(True)

        def _do():
            cache = wom.load_wom_cache()
            errors = []
            for account in accounts:
                wom_username = self._wom_username_for(account)
                result = wom.refresh_account_in_cache(account, wom_username, cache)
                if result.error:
                    errors.append((account, result.error))
                time.sleep(0.4)  # be a good API citizen, especially for "All Accounts"
            wom.save_wom_cache(cache)
            self.app.after(0, lambda: self._on_refresh_done(cache, errors))
        threading.Thread(target=_do, daemon=True).start()

    def _set_refresh_in_flight(self, flag):
        self._refresh_in_flight = flag
        self._refresh_btn.configure(state='disabled' if flag else 'normal',
                                     text='⏳  Refreshing…' if flag else '🔄  Refresh WOM')

    def _on_refresh_done(self, cache, errors):
        self._set_refresh_in_flight(False)
        self._cache = cache
        if errors:
            lines = '\n'.join(f"• {acc}: {err}" for acc, err in errors[:8])
            more = f"\n...and {len(errors) - 8} more" if len(errors) > 8 else ''
            messagebox.showwarning('WOM Refresh',
                f"Some accounts could not be refreshed:\n\n{lines}{more}\n\n"
                f"Existing cached data was kept.")
        self._rerender()
        # Monitor's Max Progress card reads the same cache file — let it
        # pick up the fresh data immediately rather than waiting for its
        # own next unrelated refresh.
        if getattr(self.app, '_monitor_tab', None):
            self.app._monitor_tab.refresh_max_progress()

    # ── Edit XP Rates / Reset Defaults ──────────────────────────────────────────
    def _open_edit_rates_dialog(self):
        initial = self._selected_account if self._selected_account != 'All Accounts' else None
        _EditRatesDialog(self.app, self, initial_scope=initial)

    def _on_reset_defaults_click(self):
        """Top-level Reset Defaults button — resets overrides for the
        CURRENT scope (the account selected in the main dropdown, or
        every global+account override if 'All Accounts' is selected).
        Confirmed first, since this is destructive."""
        scope = self._selected_account
        if scope == 'All Accounts':
            msg = "Reset ALL XP rate overrides — both global defaults and every per-account override?"
        else:
            msg = f"Reset all XP rate overrides for \"{scope}\" back to defaults?"
        if not messagebox.askyesno('Reset Defaults', msg):
            return
        cfg = self.app.cfg
        if scope == 'All Accounts':
            cfg['wom_global_rate_overrides'] = {}
            cfg['wom_account_rate_overrides'] = {}
        else:
            (cfg.get('wom_account_rate_overrides') or {}).pop(scope, None)
        save_config(cfg)
        self._rerender()


class _EditRatesDialog:
    """
    Edit XP Rates popup. Scope selector: "Global defaults" or a specific
    account. Saving writes only to config (wom_global_rate_overrides /
    wom_account_rate_overrides) — never touches the cache. mode/label are
    always read-only display, sourced from the static DEFAULT_WOM_RATES
    table (excluded skills' rate field is disabled — their rate is fixed
    at 0 by design, editing it would be misleading since it never
    contributes to time-to-max regardless).

    "Reset selected skill" from the spec is implemented as a small
    per-row reset button rather than a separate row-selection mechanism —
    same practical outcome (clear one skill's override), notably simpler.
    """

    def __init__(self, app, parent_page, initial_scope=None):
        self.app = app
        self.parent_page = parent_page
        self.window = tk.Toplevel(app, bg=app.BG2)
        self.window.title('Edit XP Rates')
        self.window.resizable(False, False)
        self.window.transient(app)
        self.window.grab_set()
        self._scope_var = tk.StringVar(value=initial_scope or 'Global defaults')
        self._rate_entries = {}  # skill -> (StringVar, Entry widget)
        self._build()
        self._load_scope()

    def _current_scope_overrides(self):
        cfg = self.app.cfg
        scope = self._scope_var.get()
        if scope == 'Global defaults':
            return cfg.setdefault('wom_global_rate_overrides', {})
        return cfg.setdefault('wom_account_rate_overrides', {}).setdefault(scope, {})

    def _build(self):
        app = self.app
        w = self.window

        top = tk.Frame(w, bg=app.BG2, padx=14, pady=10)
        top.pack(fill='x')
        tk.Label(top, text='Edit XP Rates', font=app.SANSB, bg=app.BG2, fg=app.ACC).pack(side='left')

        scope_row = tk.Frame(w, bg=app.BG2, padx=14)
        scope_row.pack(fill='x', pady=(0, 8))
        tk.Label(scope_row, text='Scope:', font=app.SANS, bg=app.BG2, fg=app.FG2).pack(side='left')
        accounts = self.parent_page._known_accounts()
        combo = ttk.Combobox(scope_row, textvariable=self._scope_var, state='readonly',
                              values=['Global defaults'] + accounts, font=app.SANS, width=22)
        combo.pack(side='left', padx=(8, 0))
        combo.bind('<<ComboboxSelected>>', lambda e: self._load_scope())

        table_frame = tk.Frame(w, bg=app.BG3)
        table_frame.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        header = tk.Frame(table_frame, bg=app.BG4, padx=8, pady=6)
        header.pack(fill='x')
        for text, width in (('Skill', 13), ('Default', 9), ('Override', 11), ('Mode / label', 28), ('', 4)):
            tk.Label(header, text=text, font=app.SANSS, bg=app.BG4, fg=app.FG2,
                     width=width, anchor='w').pack(side='left')

        scroll_outer = tk.Frame(table_frame, bg=app.BG3)
        scroll_outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(scroll_outer, bg=app.BG3, highlightthickness=0, height=340, width=560)
        canvas.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(scroll_outer, orient='vertical', command=canvas.yview)
        sb.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=sb.set)
        inner = tk.Frame(canvas, bg=app.BG3)
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win_id, width=e.width))

        for skill, default in wom.DEFAULT_WOM_RATES.items():
            row = tk.Frame(inner, bg=app.BG3, padx=8, pady=3)
            row.pack(fill='x')
            tk.Label(row, text=skill, font=app.SANS, bg=app.BG3, fg=app.FG,
                     width=13, anchor='w').pack(side='left')
            tk.Label(row, text=f"{default['xp_hr']:,}", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     width=9, anchor='w').pack(side='left')
            var = tk.StringVar()
            entry = tk.Entry(row, textvariable=var, font=app.SANS, bg=app.BG4, fg=app.FG,
                              insertbackground=app.ACC, relief='flat', width=11,
                              state='disabled' if default['mode'] == 'excluded' else 'normal')
            entry.pack(side='left', ipady=2, padx=(0, 8))
            tk.Label(row, text=f"{default['mode']} — {default['label']}", font=app.SANSS,
                     bg=app.BG3, fg=app.FG2, width=28, anchor='w').pack(side='left')
            reset_btn = tk.Button(row, text='↺', font=app.SANSS, bg=app.BG4, fg=app.FG2,
                relief='flat', padx=6, cursor='hand2',
                command=lambda s=skill: self._reset_one_skill(s))
            if default['mode'] != 'excluded':
                reset_btn.pack(side='left')
            self._rate_entries[skill] = (var, entry)

        btn_row = tk.Frame(w, bg=app.BG2, padx=14)
        btn_row.pack(fill='x', pady=(0, 12))
        tk.Button(btn_row, text='Save', font=app.SANSB, bg=app.GREEN, fg=app.BG, relief='flat',
                   padx=14, pady=6, cursor='hand2', command=self._on_save).pack(side='left', padx=(0, 8))
        tk.Button(btn_row, text='Reset All Defaults (this scope)', font=app.SANSB,
                   bg=app.BG3, fg=app.FG2, relief='flat', padx=14, pady=6, cursor='hand2',
                   command=self._reset_all_this_scope).pack(side='left', padx=(0, 8))
        tk.Button(btn_row, text='Cancel', font=app.SANSB, bg=app.BG3, fg=app.FG2,
                   relief='flat', padx=14, pady=6, cursor='hand2',
                   command=self.window.destroy).pack(side='left')

    def _load_scope(self):
        overrides = self._current_scope_overrides()
        for skill, (var, _entry) in self._rate_entries.items():
            val = overrides.get(skill)
            var.set(f"{val:,}" if val is not None else '')

    def _reset_one_skill(self, skill):
        var, _entry = self._rate_entries[skill]
        var.set('')

    def _reset_all_this_scope(self):
        for var, _entry in self._rate_entries.values():
            var.set('')

    def _on_save(self):
        overrides = self._current_scope_overrides()
        new_values = {}
        for skill, (var, _entry) in self._rate_entries.items():
            text = var.get().strip().replace(',', '')
            if not text:
                continue
            try:
                num = float(text)
            except ValueError:
                messagebox.showwarning('Edit XP Rates', f'"{var.get()}" is not a valid number for {skill}.')
                return
            if num < 0:
                messagebox.showwarning('Edit XP Rates', f'{skill}\'s rate cannot be negative.')
                return
            new_values[skill] = num
        overrides.clear()
        overrides.update(new_values)
        save_config(self.app.cfg)
        self.window.destroy()
        self.parent_page._rerender()
