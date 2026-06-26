"""
ui/monitor_tab.py — Monitor tab for P2P Monitor (v2.0.0-beta.11 redesign)

Warm dark card-based layout matching Stats/Settings. Preserves exactly:
- app._btn_start / app._btn_stop (Start/Stop wiring untouched — same
  command callbacks, same App._start()/_stop() logic)
- app._sv (the 7 counter StringVars — same keys: quest/task/chat/error/
  drop/death/levelup — same App._on_event() wiring)
- app._log_text (the Text widget; App._log()'s emoji-based classification
  and per-tag coloring is untouched — one new whole-line category tag was
  added there purely for this tab's event-type filter, see App._log())
- self._clear_log()

Adds, purely additively:
- A Session Overview card: Status / Uptime / Started / Events. (The
  original mockup also showed Script/Profile/Runtime fields, but nothing
  in the app tracks a single global script/profile, and the closest thing
  to a "runtime version" is a per-window field never persisted anywhere —
  rather than fabricate those three, they're dropped for now.)
- An Active Accounts card (sidebar) and a matching Highlights-row card —
  both read from the same App._highlights dict and watcher.get_account_rows(),
  refreshed only on the existing event-driven debounce (App._debounced_refresh_tick),
  never polled on tab-switch or on a network-touching timer.
- An event-type filter dropdown + search box for the log, implemented via
  Tk Text elide tags (hide/show line ranges) — no rebuild of the log
  content, no change to how lines get classified or colored.
"""
import tkinter as tk
import threading
import time
from datetime import datetime
from tkinter import ttk

# Minimum genuine overflow (in px) before a scroll container shows its
# scrollbar — without this, even a few pixels of rounding/measurement
# noise (which happens routinely across different font metrics, e.g.
# Windows vs Linux) triggers a scrollbar that barely moves and serves
# no purpose. Only real, meaningful overflow should ever scroll.
_SCROLL_TOLERANCE_PX = 16


class MonitorTab:
    def __init__(self, app, parent_frame):
        self.app = app
        self._last_account_rows = []
        self._log_line_count_at_last_search = 0
        self._search_debounce_id = None
        self._accounts_refresh_in_flight = False  # guards against overlapping background fetches
        self._build(parent_frame)
        self.refresh_session_overview()
        self.refresh_highlights()
        self.refresh_max_progress()
        self._tick()

    def on_tab_shown(self):
        """Called when the Monitor tab is selected. Active Accounts/
        Highlights/Max Progress otherwise only ever refresh reactively —
        Active Accounts/Highlights debounced after a live event (via
        App._debounced_refresh_tick), and Max Progress only at Monitor's
        own construction plus whenever Goals & Maxing's "Refresh WOM"
        button is clicked (it calls refresh_max_progress() directly) —
        there was no path that re-pulled the WOM cache just from switching
        into Monitor. During a quiet period with no new events and no
        manual WOM refresh, both cards could keep showing whatever was
        true at construction time indefinitely (Max Progress in
        particular: a WOM cache that gets populated by something other
        than this exact app instance's own startup moment — e.g. a cache
        file that already existed from an earlier session — would only
        ever show up after that explicit Refresh WOM click), even though
        Status, which queries the same underlying watcher state on demand
        via its own on_tab_shown(), would show the truth immediately. This
        makes Monitor do the same on-demand refresh Status already does."""
        if self.app.cfg.get('debug', False):
            self.app._log('🔍 Monitor.on_tab_shown() fired — refreshing highlights + max progress')
        self.refresh_highlights()
        self.refresh_max_progress()

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self, f):
        app = self.app
        root = tk.Frame(f, bg=app.BG2, padx=16, pady=16)
        root.pack(fill='both', expand=True)

        left_outer = tk.Frame(root, bg=app.BG2, width=200)
        left_outer.pack(side='left', fill='y', padx=(0, 12))
        left_outer.pack_propagate(False)

        # Canvas+Scrollbar wrap, same pattern as Status/History/Launcher/
        # Settings — the four stacked cards below (Session Control, the
        # status card, Active Accounts, Max Progress) used to live directly
        # in a plain pack_propagate(False) frame with no explicit height,
        # which meant real populated content (a real account, real WOM
        # cache data) could silently overflow the visible area with zero
        # signal anywhere in the widget tree — that's what was clipping
        # Max Progress. Compaction (below) should mean this scrollbar
        # never actually needs to appear in practice; this is the backstop
        # for whatever compaction doesn't fully cover (e.g. Windows' own
        # font metrics, which aren't the same as what's measurable here).
        canvas = tk.Canvas(left_outer, bg=app.BG2, highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(left_outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        left = tk.Frame(canvas, bg=app.BG2)
        win = canvas.create_window((0, 0), window=left, anchor='nw')

        needs_scroll = False

        def _sync_scroll(_e=None):
            nonlocal needs_scroll
            canvas.configure(scrollregion=canvas.bbox('all'))
            content_h = left.winfo_reqheight()
            visible_h = canvas.winfo_height()
            needs_scroll = (content_h - visible_h) > _SCROLL_TOLERANCE_PX and visible_h > 1
            if needs_scroll and not sb.winfo_ismapped():
                sb.pack(side='right', fill='y')
            elif not needs_scroll and sb.winfo_ismapped():
                sb.pack_forget()
                canvas.yview_moveto(0)
        left.bind('<Configure>', _sync_scroll)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfig(win, width=e.width), _sync_scroll()))

        def _on_enter(_):
            canvas.bind_all('<MouseWheel>', lambda e: needs_scroll and canvas.yview_scroll(-1 * (e.delta // 120), 'units'))
            canvas.bind_all('<Button-4>',   lambda e: needs_scroll and canvas.yview_scroll(-1, 'units'))
            canvas.bind_all('<Button-5>',   lambda e: needs_scroll and canvas.yview_scroll(1,  'units'))
        def _on_leave(_):
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')
        canvas.bind('<Enter>', _on_enter)
        canvas.bind('<Leave>', _on_leave)

        right = tk.Frame(root, bg=app.BG2)
        right.pack(side='left', fill='both', expand=True)

        self._build_session_control(left)
        self._build_session_overview(left)
        self._build_active_accounts_card(left)
        self._build_max_progress_card(left)

        self._build_stat_strip(right)
        self._build_highlights(right)
        self._build_event_log(right)

    def _card(self, parent, title, icon=None):
        app = self.app
        card = tk.Frame(parent, bg=app.BG3, padx=8, pady=6)
        card.pack(fill='x', pady=(0, 6))
        if title:
            hdr = tk.Frame(card, bg=app.BG3)
            hdr.pack(fill='x', anchor='w', pady=(0, 4))
            if icon:
                tk.Label(hdr, text=icon, font=(app.SANS[0], 12), bg=app.BG3, fg=app.ACC
                         ).pack(side='left', padx=(0, 6))
            tk.Label(hdr, text=title, font=app.SANSS, bg=app.BG3, fg=app.FG2
                     ).pack(side='left')
        return card

    # ── Session Control ──────────────────────────────────────────────────────────
    def _build_session_control(self, parent):
        app = self.app
        card = self._card(parent, "SESSION CONTROL", '▶')
        app._btn_start = tk.Button(card, text="▶  START", font=app.SANSB,
            bg=app.GREEN, fg=app.BG, relief='flat', padx=14, pady=5,
            cursor='hand2', command=app._start)
        app._btn_start.pack(fill='x', pady=(0, 3))
        app._btn_stop = tk.Button(card, text="■  STOP", font=app.SANSB,
            bg=app.BG3, fg=app.FG2, relief='flat', padx=14, pady=5,
            cursor='hand2', command=app._stop, state='disabled')
        app._btn_stop.pack(fill='x')

    # ── Session Overview ─────────────────────────────────────────────────────────
    def _build_session_overview(self, parent):
        app = self.app
        card = self._card(parent, None)
        self._so_status_lbl = tk.Label(card, text="● STOPPED", font=app.SANSB,
                                        bg=app.BG3, fg=app.RED)
        self._so_status_lbl.pack(anchor='w', pady=(0, 3))

        self._so_uptime_lbl   = self._so_row(card, "Uptime", "—")
        self._so_started_lbl  = self._so_row(card, "Started", "—")
        self._so_events_lbl   = self._so_row(card, "Events", "0")

    def _so_row(self, parent, label, value):
        app = self.app
        row = tk.Frame(parent, bg=app.BG3)
        row.pack(fill='x', pady=1)
        tk.Label(row, text=label, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                 width=9, anchor='w').pack(side='left')
        val_lbl = tk.Label(row, text=value, font=app.SANS, bg=app.BG3, fg=app.FG, anchor='w')
        val_lbl.pack(side='left', fill='x', expand=True)
        return val_lbl

    def refresh_session_overview(self):
        """Pure local state/math — no I/O. Called on Start/Stop and once a
        second from _tick() while running."""
        app = self.app
        running = str(app._btn_stop.cget('state')) == 'normal'
        self._so_status_lbl.configure(
            text="● RUNNING" if running else "● STOPPED",
            fg=app.GREEN if running else app.RED)

        if app._session_start_ts:
            started_dt = datetime.fromtimestamp(app._session_start_ts)
            today = datetime.now().date() == started_dt.date()
            prefix = "Today" if today else started_dt.strftime('%b %d')
            self._so_started_lbl.configure(text=f"{prefix}, {started_dt.strftime('%H:%M')}")
            if running:
                elapsed = int(time.time() - app._session_start_ts)
                h, rem = divmod(elapsed, 3600)
                m, s = divmod(rem, 60)
                self._so_uptime_lbl.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
        else:
            self._so_started_lbl.configure(text="—")
            self._so_uptime_lbl.configure(text="—")

        self._so_events_lbl.configure(text=str(sum(app._counts.values())))

    # ── Active Accounts (sidebar) ────────────────────────────────────────────────
    def _build_active_accounts_card(self, parent):
        app = self.app
        card = self._card(parent, "ACTIVE ACCOUNTS", '👥')
        self._aa_count_lbl = tk.Label(card, text="—", font=(app.SANS[0], 16, 'bold'),
                                       bg=app.BG3, fg=app.FG)
        self._aa_count_lbl.pack(anchor='w')
        self._aa_dots = tk.Frame(card, bg=app.BG3)
        self._aa_dots.pack(anchor='w', pady=(2, 3))
        self._aa_sub_lbl = tk.Label(card, text="No accounts yet", font=app.SANSS,
                                     bg=app.BG3, fg=app.FG2)
        self._aa_sub_lbl.pack(anchor='w')

    def _render_active_accounts(self, rows):
        app = self.app
        total = len(rows)
        active = sum(1 for r in rows if '🟢' in r['status'] or '🟡' in r['status'])
        self._aa_count_lbl.configure(text=f"{active}/{total}" if total else "—")
        for w in self._aa_dots.winfo_children():
            w.destroy()
        for r in rows:
            ok = '🟢' in r['status'] or '🟡' in r['status']
            tk.Label(self._aa_dots, text='●', font=app.SANS,
                     bg=app.BG3, fg=(app.GREEN if ok else app.RED)).pack(side='left', padx=1)
        if total == 0:
            self._aa_sub_lbl.configure(text="No accounts yet", fg=app.FG2)
        elif active == total:
            self._aa_sub_lbl.configure(text="✓ All accounts operational", fg=app.GREEN)
        else:
            self._aa_sub_lbl.configure(text=f"⚠ {total - active} need attention", fg=app.YEL)

    # ── Max Progress (sidebar) ──────────────────────────────────────────────────
    def _build_max_progress_card(self, parent):
        app = self.app
        card = self._card(parent, "MAX PROGRESS", '🏆')
        tk.Label(card, text="Closest to max", font=app.SANSS, bg=app.BG3, fg=app.FG2
                 ).pack(anchor='w')
        self._mp_account_lbl = tk.Label(card, text="No WOM data yet", font=(app.SANS[0], 12, 'bold'),
                                         bg=app.BG3, fg=app.FG, cursor='hand2', anchor='w',
                                         wraplength=172, justify='left')
        self._mp_account_lbl.pack(anchor='w', pady=(1, 2), fill='x')
        self._mp_99_lbl = tk.Label(card, text="", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                                    anchor='w', wraplength=172, justify='left')
        self._mp_99_lbl.pack(anchor='w', fill='x')
        self._mp_time_lbl = tk.Label(card, text="", font=app.SANSS, bg=app.BG3, fg=app.ACC,
                                      anchor='w', wraplength=172, justify='left')
        self._mp_time_lbl.pack(anchor='w', pady=(0, 2), fill='x')
        self._mp_bar_bg = tk.Frame(card, bg=app.BG4, height=5)
        self._mp_bar_bg.pack(fill='x', pady=(0, 3))
        self._mp_bar_fill = tk.Frame(self._mp_bar_bg, bg=app.GREEN, height=5, width=0)
        self._mp_bar_fill.place(x=0, y=0)
        self._mp_note_lbl = tk.Label(card, text="No WOM data yet", font=app.SANSS,
                                      bg=app.BG3, fg=app.FG2, anchor='w')
        self._mp_note_lbl.pack(anchor='w', fill='x')

        # Clicking the card switches to Stats -> Goals & Maxing, per spec.
        for w in (card, self._mp_account_lbl):
            w.configure(cursor='hand2')
            w.bind('<Button-1>', lambda e: self._open_goals_maxing())

    def _open_goals_maxing(self):
        app = self.app
        app.show_tab('Stats')
        if getattr(app, '_stats_tab', None) and hasattr(app._stats_tab, 'show_goals_maxing'):
            app.after(50, app._stats_tab.show_goals_maxing)

    def refresh_max_progress(self):
        """
        Reads ONLY the on-disk WOM cache — never calls the WOM API. The
        actual file read + computation runs on a background thread (cheap
        in practice, but file I/O has no business on the Tk main thread
        regardless). "Closest to max" = the cached account with the
        lowest nonzero computed time-to-max; an account with no usable
        rate data anywhere just isn't a candidate.

        For the winning account's "Last 99", uses
        py.wom.determine_last_99() the same way Goals & Maxing does:
        a real history levelup event wins if one exists, falling back to
        WOM cache data (labeled "from WOM cache", since cache only knows
        a skill IS at 99, never when) — never the bare "any cached skill
        at/above the 99 XP threshold" check this used before. The history
        read for just this one account happens inside this same
        already-backgrounded thread, so it adds no new main-thread risk.
        """
        app = self.app
        def _do():
            best, last99 = None, None
            try:
                from py.wom import load_wom_cache, compute_account_summary, determine_last_99, WOM_CACHE_FILE
                try:
                    cache = load_wom_cache()
                except Exception:
                    cache = {'accounts': {}}
                accounts_in_cache = cache.get('accounts', {})
                if app.cfg.get('debug', False):
                    self.app._log(f'🔍 Max Progress: WOM_CACHE_FILE={WOM_CACHE_FILE} '
                                   f'exists={WOM_CACHE_FILE.exists()} '
                                   f'accounts_in_cache={list(accounts_in_cache.keys())}')
                for account, entry in accounts_in_cache.items():
                    skills = entry.get('skills') or {}
                    if not skills:
                        if app.cfg.get('debug', False):
                            self.app._log(f'🔍 Max Progress: {account} has no skills in cache, skipping')
                        continue
                    try:
                        summary = compute_account_summary(account, app.cfg, skills)
                    except Exception as e:
                        # One malformed/legacy-shaped cache entry must never
                        # take down every other account's computation — this
                        # loop previously had no try/except here at all, so a
                        # single bad entry would crash this whole background
                        # thread silently (daemon thread, uncaught exception
                        # just kills it), leaving the UI stuck on its initial
                        # "No WOM data yet" state forever even though the
                        # cache file itself was read successfully. That's
                        # indistinguishable from "no cache" in the UI, which
                        # is the reported symptom.
                        if app.cfg.get('debug', False):
                            self.app._log(f'⚠ Max Progress: skipping {account}, '
                                           f'compute_account_summary failed: {e}')
                        continue
                    ttm = summary.get('time_to_max_hours')
                    if not ttm or ttm <= 0:
                        if app.cfg.get('debug', False):
                            self.app._log(f'🔍 Max Progress: {account} has no usable '
                                           f'time_to_max_hours ({ttm}), not a candidate')
                        continue
                    if best is None or ttm < best['time_to_max_hours']:
                        best = summary

                if app.cfg.get('debug', False):
                    self.app._log(f'🔍 Max Progress: decided best='
                                   f'{best.get("account") if best else None}')

                if best:
                    account = best['account']
                    try:
                        from py.stats import load_levelup_rows
                        from py.history import _parse_ts
                        rows99 = [{'value': r['skill'], 'activity': '99', '_ts_epoch': _parse_ts(r['time'])}
                                  for r in load_levelup_rows(accounts=[account]) if r['level'] == 99]
                    except Exception:
                        rows99 = []
                    acc_skills = cache.get('accounts', {}).get(account, {}).get('skills') or {}
                    last99 = determine_last_99(rows99, acc_skills)
            except Exception as e:
                # Outermost safety net: whatever happened, the UI must still
                # get an update (falling back to "no data") rather than the
                # thread just vanishing and the card staying stuck forever.
                if app.cfg.get('debug', False):
                    self.app._log(f'⚠ Max Progress: refresh failed unexpectedly: {e}')
                best, last99 = None, None

            app.after(0, lambda: self._apply_max_progress(best, last99))
        threading.Thread(target=_do, daemon=True).start()

    def _apply_max_progress(self, best, last99=None):
        app = self.app
        if not best:
            self._mp_account_lbl.configure(text="No WOM data yet", fg=app.FG2)
            self._mp_99_lbl.configure(text="")
            self._mp_time_lbl.configure(text="")
            self._mp_bar_fill.place_configure(width=0)
            self._mp_note_lbl.configure(text="Open Stats → Goals & Maxing")
            return
        from py.wom import format_hours_compact
        self._mp_account_lbl.configure(text=best['account'], fg=app.ACC)
        if last99:
            source_text = self._ago(last99['ts']) if last99['ts'] else 'from WOM cache'
            self._mp_99_lbl.configure(text=f"Last 99: {last99['skill']}  •  {source_text}")
        else:
            self._mp_99_lbl.configure(text="Last 99: —")
        ttm = best['time_to_max_hours']
        self._mp_time_lbl.configure(text=f"Time left: {format_hours_compact(ttm)}")
        # Progress bar: rough share of total levels already at 99, just a
        # quick visual cue, not a precise XP-weighted progress metric.
        achieved = sum(1 for e in best['per_skill'] if e['status'] == 'achieved')
        eligible = sum(1 for e in best['per_skill'] if e['status'] in ('achieved', 'active'))
        frac = (achieved / eligible) if eligible else 0
        try:
            bar_w = self._mp_bar_bg.winfo_width() or 200
        except Exception:
            bar_w = 200
        self._mp_bar_fill.place_configure(width=max(2, int(bar_w * frac)))
        self._mp_note_lbl.configure(text="WOM cached")

    # ── Stat strip ────────────────────────────────────────────────────────────────
    def _build_stat_strip(self, parent):
        app = self.app
        strip = tk.Frame(parent, bg=app.BG2)
        strip.pack(fill='x', pady=(0, 12))
        app._sv = {}
        specs = [
            ("QUESTS",  "quest",   '🚩', app.PUR),
            ("TASKS",   "task",    '📋', app.ACC),
            ("CHATS",   "chat",    '💬', app.YEL),
            ("ERRORS",  "error",   '⚠',  app.RED),
            ("DROPS",   "drop",    '💎', app.GREEN),
            ("DEATHS",  "death",   '💀', app.RED),
            ("LEVELS",  "levelup", '📈', app.ACC2),
        ]
        for label, key, icon, color in specs:
            cell = tk.Frame(strip, bg=app.BG3, padx=3, pady=6)
            cell.pack(side='left', fill='x', expand=True, padx=(0, 3))
            top = tk.Frame(cell, bg=app.BG3)
            top.pack(fill='x', anchor='w')
            tk.Label(top, text=icon, font=(app.SANS[0], 11), bg=app.BG3, fg=color
                     ).pack(side='left', padx=(0, 4))
            tk.Label(top, text=label, font=app.SANSS, bg=app.BG3, fg=app.FG2
                     ).pack(side='left')
            var = tk.StringVar(value='0')
            tk.Label(cell, textvariable=var, font=(app.SANS[0], 15, 'bold'),
                     bg=app.BG3, fg=color).pack(anchor='w', pady=(2, 0))
            app._sv[key] = var

    # ── Highlights row ───────────────────────────────────────────────────────────
    def _build_highlights(self, parent):
        app = self.app
        wrap = tk.Frame(parent, bg=app.BG2)
        wrap.pack(fill='x', pady=(0, 12))
        tk.Label(wrap, text="HIGHLIGHTS", font=app.SANSS, bg=app.BG2, fg=app.FG2
                 ).pack(anchor='w', pady=(0, 6))
        strip = tk.Frame(wrap, bg=app.BG2)
        strip.pack(fill='x')

        self._hl_widgets = {}
        specs = [
            ('task',     "LATEST TASK",     '⭐', app.ACC),
            ('levelup',  "LAST LEVEL UP",   '📈', app.ACC2),
            ('error',    "LAST ERROR",      '⚠',  app.RED),
            ('drop',     "LATEST DROP",     '💎', app.GREEN),
        ]
        for key, label, icon, color in specs:
            cell = tk.Frame(strip, bg=app.BG3, padx=3, pady=6)
            cell.pack(side='left', fill='both', expand=True, padx=(0, 1))
            top = tk.Frame(cell, bg=app.BG3)
            top.pack(fill='x', anchor='w')
            tk.Label(top, text=icon, font=(app.SANS[0], 11), bg=app.BG3, fg=color
                     ).pack(side='left', padx=(0, 4))
            tk.Label(top, text=label, font=app.SANSS, bg=app.BG3, fg=app.FG2
                     ).pack(side='left')
            account_lbl = tk.Label(cell, text="", font=app.SANSS, bg=app.BG3, fg=color,
                                    anchor='w', justify='left', wraplength=110)
            account_lbl.pack(fill='x', anchor='w', pady=(2, 0))
            val_lbl = tk.Label(cell, text="None yet", font=app.SANSS, bg=app.BG3, fg=app.FG,
                                anchor='w', justify='left', wraplength=110)
            val_lbl.pack(fill='x', anchor='w')
            sub_lbl = tk.Label(cell, text="", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                                anchor='w', wraplength=110, justify='left')
            sub_lbl.pack(fill='x', anchor='w')
            self._hl_widgets[key] = (account_lbl, val_lbl, sub_lbl)

        # Last 99 Achieved — replaces the old duplicate Active Accounts card
        # (that data is already shown in the Active Accounts sidebar card;
        # showing it twice was redundant). Same tile-name/account/info/time
        # rhythm as the other four highlight cards.
        cell = tk.Frame(strip, bg=app.BG3, padx=3, pady=6)
        cell.pack(side='left', fill='both', expand=True)
        top = tk.Frame(cell, bg=app.BG3)
        top.pack(fill='x', anchor='w')
        tk.Label(top, text='🏆', font=(app.SANS[0], 11), bg=app.BG3, fg=app.YEL
                 ).pack(side='left', padx=(0, 4))
        tk.Label(top, text="LAST 99 ACHIEVED", font=app.SANSS, bg=app.BG3, fg=app.FG2
                 ).pack(side='left')
        account_lbl = tk.Label(cell, text="", font=app.SANSS, bg=app.BG3, fg=app.YEL,
                                anchor='w', justify='left', wraplength=110)
        account_lbl.pack(fill='x', anchor='w', pady=(2, 0))
        val_lbl = tk.Label(cell, text="None yet", font=app.SANSS, bg=app.BG3, fg=app.FG,
                            anchor='w', justify='left', wraplength=110)
        val_lbl.pack(fill='x', anchor='w')
        sub_lbl = tk.Label(cell, text="", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                            anchor='w', wraplength=110, justify='left')
        sub_lbl.pack(fill='x', anchor='w')
        self._hl_widgets['last99'] = (account_lbl, val_lbl, sub_lbl)

    def refresh_highlights(self):
        """app._highlights is a pure dict read (no I/O) — but
        watcher.get_account_rows() does real filesystem work (checking
        every monitored account's log directory for rotation), so that
        part always runs on a background thread, never on the caller's
        thread. This matters because the caller is sometimes
        App._debounced_refresh_tick(), which runs on the main/Tk thread —
        calling get_account_rows() directly from there would freeze the
        whole app for however long that disk check takes. _do() fetches
        the rows off-thread, then hands them to _apply_highlights_data()
        via app.after(0, ...) for the actual widget updates (Tkinter
        widgets aren't thread-safe, so that part must run on the main
        thread). Guarded by _accounts_refresh_in_flight — same pattern as
        StatusTab.refresh()/push_refresh() — so a fast burst of events
        (e.g. the startup catchup scan) can't pile up overlapping
        background checks."""
        app = self.app
        if not app.watcher:
            self._apply_highlights_data([])
            return
        if self._accounts_refresh_in_flight:
            return
        self._accounts_refresh_in_flight = True

        def _do():
            try:
                rows = app.watcher.get_account_rows()
                app.after(0, lambda: self._apply_highlights_data(rows))
            except Exception:
                app.after(0, lambda: self._apply_highlights_data([]))
            finally:
                self._accounts_refresh_in_flight = False
        threading.Thread(target=_do, daemon=True).start()

    def _apply_highlights_data(self, rows):
        """Main-thread-only: applies already-fetched account rows to the
        Active Accounts sidebar card. Never does I/O itself — rows are
        always handed in by refresh_highlights()'s background thread."""
        self._last_account_rows = rows
        self._render_active_accounts(rows)
        self._render_highlight_values()

    def _render_highlight_values(self):
        for key, (account_lbl, val_lbl, sub_lbl) in self._hl_widgets.items():
            h = self.app._highlights.get(key)
            if not h:
                account_lbl.configure(text="")
                val_lbl.configure(text="None yet")
                sub_lbl.configure(text="")
                continue

            account_lbl.configure(text=h.get('account') or "Unknown account")

            if key == 'task':
                val_lbl.configure(text=f"{h['value']} — {h['activity']}" if h['activity'] else h['value'])
            elif key == 'levelup':
                val_lbl.configure(text=f"{h['value']} → {h['activity']}")
            elif key == 'error':
                val_lbl.configure(text=h['value'][:48] or 'Error')
            elif key == 'drop':
                val_lbl.configure(text=h['value'])
            elif key == 'last99':
                val_lbl.configure(text=f"{h['value']} → 99")

            if key == 'last99':
                sub_lbl.configure(text=self._ago(h['ts']) if h['ts'] is not None else 'from WOM cache')
            else:
                sub_lbl.configure(text=self._ago(h['ts']))

    @staticmethod
    def _ago(ts):
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

    def _tick(self):
        """Lightweight 1-second ticker — pure local math (uptime, relative
        'ago' times) plus a debounced search re-scan only while the search
        box actually has text. No I/O, no watcher calls, unaffected by tab
        switching since it's a passive self.after() chain."""
        self.refresh_session_overview()
        self._render_highlight_values()
        if self._search_var.get().strip():
            self._maybe_rescan_search()
        self.app.after(1000, self._tick)

    # ── Event Log ─────────────────────────────────────────────────────────────────
    FILTER_OPTIONS = [
        ("All Events", None),
        ("System",     'system'),
        ("Quests",     'quest'),
        ("Tasks",      'task'),
        ("Chats",      'chat'),
        ("Errors",     'error'),
        ("Drops",      'drop'),
        ("Deaths",     'death'),
        ("Levels",     'levelup'),
        ("Other",      'other'),
    ]
    ALL_CATEGORIES = [c for _, c in FILTER_OPTIONS if c]

    def _build_event_log(self, parent):
        app = self.app
        card = tk.Frame(parent, bg=app.BG3, padx=14, pady=12)
        card.pack(fill='both', expand=True)

        hdr = tk.Frame(card, bg=app.BG3)
        hdr.pack(fill='x', pady=(0, 8))
        tk.Label(hdr, text="📜", font=(app.SANS[0], 12), bg=app.BG3, fg=app.ACC
                 ).pack(side='left', padx=(0, 6))
        tk.Label(hdr, text="EVENT LOG", font=app.SANSB, bg=app.BG3, fg=app.FG
                 ).pack(side='left')

        self._filter_var = tk.StringVar(value="All Events")
        filter_cb = ttk.Combobox(hdr, textvariable=self._filter_var, state='readonly',
                                  font=app.SANSS, width=10,
                                  values=[lbl for lbl, _ in self.FILTER_OPTIONS])
        filter_cb.pack(side='right')
        filter_cb.bind('<<ComboboxSelected>>', lambda e: self._apply_category_filter())

        self._search_var = tk.StringVar(value="")
        search_entry = tk.Entry(hdr, textvariable=self._search_var, font=app.SANSS,
                                 bg=app.BG4, fg=app.FG, relief='flat', insertbackground=app.ACC,
                                 width=14)
        search_entry.pack(side='right', padx=(0, 8), ipady=3)
        self._search_placeholder(search_entry)
        self._search_var.trace_add('write', lambda *_: self._debounce_search())

        tk.Button(hdr, text="🗑 Clear Log", font=app.SANSS, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=8, pady=3, cursor='hand2',
            command=self._clear_log).pack(side='right', padx=(0, 8))

        lf = tk.Frame(card, bg=app.BG)
        lf.pack(fill='both', expand=True)
        app._log_text = tk.Text(lf, bg=app.BG, fg=app.FG, font=app.MONO, relief='flat',
            wrap='word', state='disabled', insertbackground=app.ACC, height=12, width=40,
            selectbackground=app.BG3, padx=12, pady=8, spacing1=2)
        scr = ttk.Scrollbar(lf, command=app._log_text.yview)
        scr.pack(side='right', fill='y')
        app._log_text.pack(fill='both', expand=True)
        app._log_text.configure(yscrollcommand=scr.set)
        for tag, col in [
            ('info',            app.FG2),
            ('ts',              app.FG2),
            ('warn',            app.ACC2),
            ('ok',              app.GREEN),
            ('quest',           app.PUR),
            ('task',            app.ACC),
            ('chat',            app.YEL),
            ('error',           app.RED),
            ('drop',            app.GREEN),
            ('death',           app.RED),
            ('levelup',         app.ACC2),
            ('slayer_complete', app.PUR),
            ('slayer_skip',     app.RED),
            ('script_event',    app.FG2),
        ]:
            app._log_text.tag_configure(tag, foreground=col)
        # Whole-line category tags used by the event-type filter (elide-based
        # show/hide). Configured once here; App._log() just adds membership.
        for cat in self.ALL_CATEGORIES:
            app._log_text.tag_configure(f'cat_{cat}', elide=False)
        app._log_text.tag_configure('search_hidden', elide=False)

    def _search_placeholder(self, entry):
        app = self.app
        entry.configure(fg=app.FG2)
        entry.insert(0, "Search log...")
        def _on_focus_in(_e):
            if entry.get() == "Search log...":
                entry.delete(0, 'end')
                entry.configure(fg=app.FG)
        def _on_focus_out(_e):
            if not entry.get():
                entry.configure(fg=app.FG2)
                entry.insert(0, "Search log...")
        entry.bind('<FocusIn>', _on_focus_in)
        entry.bind('<FocusOut>', _on_focus_out)

    def _clear_log(self):
        t = self.app._log_text
        t.configure(state='normal')
        t.delete('1.0', 'end')
        t.configure(state='disabled')
        self._log_line_count_at_last_search = 0

    # ── Filtering (Tk Text elide — never rebuilds log content) ──────────────────
    def _apply_category_filter(self):
        t = self.app._log_text
        label = self._filter_var.get()
        selected = dict(self.FILTER_OPTIONS).get(label)
        for cat in self.ALL_CATEGORIES:
            t.tag_configure(f'cat_{cat}', elide=(selected is not None and cat != selected))

    def _debounce_search(self):
        if self._search_debounce_id:
            self.app.after_cancel(self._search_debounce_id)
        self._search_debounce_id = self.app.after(250, self._apply_search_filter)

    def _apply_search_filter(self):
        query = self._search_var.get().strip()
        if query == "Search log...":
            return
        t = self.app._log_text
        t.tag_remove('search_hidden', '1.0', 'end')
        if not query:
            self._log_line_count_at_last_search = int(t.index('end-1c').split('.')[0])
            return
        q = query.lower()
        last_line = int(t.index('end-1c').split('.')[0])
        for i in range(1, last_line + 1):
            line_text = t.get(f'{i}.0', f'{i}.end')
            if line_text and q not in line_text.lower():
                t.tag_add('search_hidden', f'{i}.0', f'{i+1}.0')
        self._log_line_count_at_last_search = last_line

    def _maybe_rescan_search(self):
        """Only re-runs the (cheap, ≤2000-line) search scan if new lines
        have actually arrived since the last scan — avoids rescanning every
        second while idle with a search active."""
        t = self.app._log_text
        last_line = int(t.index('end-1c').split('.')[0])
        if last_line != self._log_line_count_at_last_search:
            self._apply_search_filter()
