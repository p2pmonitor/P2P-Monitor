"""ui/stats_tab.py — Stats tab for P2P Monitor (v2.0.0-beta.9: native Tk Canvas charts)

Levelup aggregation lives in py/stats.py (pure, testable, no Tkinter). This
file only handles widget construction, the Tk Canvas chart/donut drawing, and
wiring filter/date-range changes to py.stats's pure functions.

Charts are drawn with plain tkinter.Canvas primitives on every platform —
no matplotlib anywhere. matplotlib was used for the chart/donut through
beta.8, but a Linux-specific FreeType render crash (and the dependency
weight/build complexity it brought) led to a native-Canvas rewrite that's
now the only rendering path, Linux and Windows alike.

Lazy-built: __init__ only creates a lightweight placeholder; the real widgets
(filters, KPI cards, chart, panels) are built on the first on_tab_shown()
call, consistent with the cached-frame / lazy-build architecture introduced
in Checkpoint 1.

Caching / recalculation rules (per spec):
  - Disk reload (py.stats.load_levelup_rows — may touch many history files)
    always runs in a background thread, never on the UI thread.
  - Filter / date-range changes only re-filter + re-aggregate the already-
    loaded in-memory rows (cheap, synchronous) and redraw — no disk I/O.
  - mark_dirty() is called by the App when a live 'levelup' event arrives;
    the *next* on_tab_shown() triggers a fresh disk reload instead of a no-op.
  - The two lower panels (Levels by Skill / Top Accounts) rebuild only their
    own row widgets on update, not the rest of the tab.

Filter semantics (per spec, beta.9):
  - KPI cards + Daily Levels chart: obey Account + Skill + Date range.
  - Levels by Skill panel: obeys Account + Date range, ignores Skill (a
    skill filter would otherwise reduce this panel to a single slice).
  - Top Accounts panel: obeys Skill + Date range, ignores Account (an
    account filter would otherwise reduce this panel to a single row).
"""
import math
import threading
import tkinter as tk
from tkinter import ttk

from py.util import write_debug_entry

from py.stats import (
    load_levelup_rows, filter_rows, compute_kpis, distinct_skills,
    daily_series_for_range, aggregate_skill_totals, aggregate_account_totals,
    date_bounds_for_preset, DATE_PRESETS, group_top_n_with_other,
)


class StatsTab:
    ALL_ACCOUNTS = 'All Accounts'
    ALL_SKILLS   = 'All Skills'

    def __init__(self, app, parent_frame):
        self.app   = app
        self._frame = parent_frame
        self._built = False
        self._dirty = True     # first show always loads
        self._loading = False
        self._all_rows = []    # full unfiltered levelup dataset, in memory
        self._date_preset = 'ALL'

        # Prewarm cache — populated by prewarm() (data-only, no widgets)
        # and consumed once by _ensure_built() the first time the
        # real UI actually gets built, so a prewarmed open skips a redundant
        # disk read instead of re-loading from scratch.
        self._prewarm_rows    = None
        self._prewarm_loading = False
        self._building        = False  # build-lock guard against races
        self._donut_resize_pending = False  # guards the single after_idle retry in _size_donut_to_panel
        self._donut_redraw_in_progress = False  # reentrancy guard — see _redraw_skill_donut

        # Lightweight placeholder — replaced by _build_real_content() on first show
        self._placeholder = None
        self._show_loading_placeholder()

    def _show_loading_placeholder(self):
        self._placeholder = tk.Frame(self._frame, bg=self.app.BG2)
        self._placeholder.pack(fill='both', expand=True)
        tk.Label(self._placeholder, text="Loading Stats…", font=self.app.SANS,
                 bg=self.app.BG2, fg=self.app.FG2).pack(expand=True)

    def _show_build_error(self, err_msg):
        """Visible, recoverable error state for when _build_real_content()
        itself fails (a genuine widget/layout construction error). Stats
        must never sit on 'Loading Stats...' forever with no way forward."""
        app = self.app
        self._placeholder = tk.Frame(self._frame, bg=app.BG2)
        self._placeholder.pack(fill='both', expand=True)
        tk.Frame(self._placeholder, bg=app.BG2).pack(expand=True)
        tk.Label(self._placeholder, text="Stats failed to load", font=app.SANSL,
                 bg=app.BG2, fg=app.RED).pack()
        tk.Label(self._placeholder, text=err_msg, font=app.SANSS, bg=app.BG2,
                 fg=app.FG2, wraplength=500, justify='center').pack(pady=(6, 12))
        tk.Button(self._placeholder, text="↻  Retry", font=app.SANSB,
                  bg=app.ACC, fg=app.BG, relief='flat', padx=14, pady=6,
                  cursor='hand2', command=self._retry_build).pack()
        tk.Frame(self._placeholder, bg=app.BG2).pack(expand=True)

    def _retry_build(self):
        self._placeholder.destroy()
        self._show_loading_placeholder()
        self.on_tab_shown()

    # ── Public API (called by App) ───────────────────────────────────────────
    def on_tab_shown(self):
        if self._built:
            if self._dirty:
                self._reload_from_disk()
            return
        self._ensure_built()

    def prewarm(self):
        """
        Data-only warm-up: load + cache levelup rows from disk in a
        background thread. Deliberately does NOT touch Tkinter widgets in
        any way — no filter row, no KPI cards, no chart/donut canvases,
        and never calls _build_real_content(). The widget-construction path
        is reserved for the moment the user actually opens the tab, when its
        frame is about to be tkraise()'d and is therefore guaranteed to have
        real, realized screen dimensions — building (and drawing) canvases
        into a frame that isn't yet mapped/sized is exactly what caused the
        Linux duplicate-Stats-section bug back when the chart used
        matplotlib: a partial build could fail partway through before
        `self._built` was ever set, so the next real visit rebuilt the whole
        tab from scratch on top of the broken one. Making prewarm purely
        data-only removes that entire class of risk by construction — there
        is nothing here that can leave partial widgets behind, because
        nothing widget-related is created at all.

        Safe to call any number of times: a no-op if the tab is already
        built (the user got there first), if data is already cached, or if
        a prewarm load is already in flight.
        """
        if self._built or self._prewarm_loading or self._prewarm_rows is not None:
            return

        self._prewarm_loading = True

        def _worker():
            try:
                rows = load_levelup_rows()
            except Exception:
                rows = []
            self.app.after(0, lambda: self._on_prewarm_loaded(rows))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_prewarm_loaded(self, rows):
        """Main-thread callback for prewarm()'s background load. Only ever
        touches plain data — no widgets exist to update yet (and may never
        exist, if the user never opens Stats this session)."""
        self._prewarm_loading = False
        self._prewarm_rows = rows

    def _ensure_built(self) -> bool:
        """Build the real Stats content + load its data, exactly once.
        Returns True the one time it actually builds, False if guarded out
        or if the build failed. Guarded by self._building so two near-
        simultaneous calls (e.g. a fast double-click) can't both start
        building. If the build throws partway through (a genuine widget/
        layout construction error — chart/donut *rendering* failures are
        caught separately inside _redraw_chart()/_redraw_skill_donut() and
        never reach here), the partial widgets are destroyed and a visible,
        recoverable error state with a Retry button is shown instead of
        silently restoring 'Loading Stats...' forever."""
        if self._built or self._building:
            return False
        self._building = True
        try:
            self._placeholder.destroy()
            self._build_real_content()
        except Exception as e:
            for w in self._frame.winfo_children():
                w.destroy()
            self._building = False
            try:
                write_debug_entry('stats_build_error', {'error': str(e)})
            except Exception:
                pass
            self._show_build_error(str(e))
            return False
        self._built = True
        self._building = False
        if self._prewarm_rows is not None:
            cached = self._prewarm_rows
            self._prewarm_rows = None   # consumed — don't hold a stale copy
            self._on_data_loaded(cached)
        else:
            self._reload_from_disk()
        return True

    def mark_dirty(self):
        """Called by the App when a live 'levelup' event arrives. Cheap —
        just sets a flag; the actual reload is deferred to the next time
        this tab is actually shown."""
        self._dirty = True

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build_real_content(self):
        app = self.app
        root = tk.Frame(self._frame, bg=app.BG2)
        root.pack(fill='both', expand=True)
        self._root = root

        self._content = tk.Frame(root, bg=app.BG2)
        self._empty   = tk.Frame(root, bg=app.BG2)
        self._content.pack(fill='both', expand=True)
        # self._empty is packed/unpacked on demand, not both at once

        self._build_empty_state(self._empty)
        self._build_filters(self._content)
        self._build_kpis(self._content)
        self._build_chart(self._content)
        self._build_panels(self._content)

    def _build_empty_state(self, f):
        app = self.app
        tk.Frame(f, bg=app.BG2).pack(expand=True)
        tk.Label(f, text="▦", font=(app.SANS[0], 40), bg=app.BG2, fg=app.FG2).pack(pady=(0, 10))
        tk.Label(f, text="No level-up data found yet.", font=app.SANSL,
                 bg=app.BG2, fg=app.FG).pack()
        tk.Label(f, text="Level-up stats will appear here once the monitor records some.",
                 font=app.SANS, bg=app.BG2, fg=app.FG2).pack(pady=(6, 0))
        tk.Frame(f, bg=app.BG2).pack(expand=True)

    def _build_filters(self, parent):
        app = self.app
        row = tk.Frame(parent, bg=app.BG2, padx=12, pady=10)
        row.pack(fill='x')

        tk.Label(row, text="Account", font=app.SANS, bg=app.BG2, fg=app.FG2).pack(side='left', padx=(0, 6))
        self._account_var = tk.StringVar(value=self.ALL_ACCOUNTS)
        self._account_cb = ttk.Combobox(row, textvariable=self._account_var, state='readonly',
                                         font=app.SANS, width=18, values=[self.ALL_ACCOUNTS])
        self._account_cb.pack(side='left', padx=(0, 16))
        self._account_cb.bind('<<ComboboxSelected>>', lambda e: self._on_filter_changed())

        tk.Label(row, text="Skill", font=app.SANS, bg=app.BG2, fg=app.FG2).pack(side='left', padx=(0, 6))
        self._skill_var = tk.StringVar(value=self.ALL_SKILLS)
        self._skill_cb = ttk.Combobox(row, textvariable=self._skill_var, state='readonly',
                                       font=app.SANS, width=16, values=[self.ALL_SKILLS])
        self._skill_cb.pack(side='left', padx=(0, 16))
        self._skill_cb.bind('<<ComboboxSelected>>', lambda e: self._on_filter_changed())

        # Date-range pill buttons
        self._date_btns = {}
        pills = tk.Frame(row, bg=app.BG2)
        pills.pack(side='left')
        for preset in DATE_PRESETS:
            b = tk.Button(pills, text=preset, font=app.SANSB, relief='flat',
                          padx=10, pady=4, cursor='hand2',
                          command=lambda p=preset: self._set_date_preset(p))
            b.pack(side='left', padx=2)
            self._date_btns[preset] = b
        self._refresh_preset_buttons()

        self._refresh_btn = tk.Button(row, text="↻  Refresh", font=app.SANSB,
                                       bg=app.BG3, fg=app.FG2, relief='flat',
                                       padx=12, pady=4, cursor='hand2',
                                       command=self._on_refresh_click)
        self._refresh_btn.pack(side='right')

    def _build_kpis(self, parent):
        app = self.app
        row = tk.Frame(parent, bg=app.BG2, padx=12)
        row.pack(fill='x', pady=(0, 10))

        self._kpi_vars = {}
        cards = [
            ('total_levels', 'TOTAL LEVELS',    app.ACC),
            ('avg_per_day',  'AVERAGE PER DAY', app.YEL),
            ('best_day',     'BEST DAY',        app.ACC2),
            ('top_account',  'TOP ACCOUNT',     app.GREEN),
        ]
        for key, title, color in cards:
            card = tk.Frame(row, bg=app.BG3, padx=14, pady=10)
            card.pack(side='left', fill='x', expand=True, padx=(0, 8))
            tk.Label(card, text=title, font=app.SANSS, bg=app.BG3, fg=app.FG2).pack(anchor='w')
            val_var = tk.StringVar(value='—')
            tk.Label(card, textvariable=val_var, font=(app.SANS[0], 18, 'bold'),
                     bg=app.BG3, fg=color).pack(anchor='w')
            sub_var = tk.StringVar(value='')
            tk.Label(card, textvariable=sub_var, font=app.SANSS, bg=app.BG3, fg=app.FG2).pack(anchor='w')
            self._kpi_vars[key] = (val_var, sub_var)

    def _build_chart(self, parent):
        app = self.app
        frame = tk.Frame(parent, bg=app.BG3, padx=8, pady=8)
        frame.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        tk.Label(frame, text="Daily Levels Gained", font=app.SANSB,
                 bg=app.BG3, fg=app.FG).pack(anchor='w', padx=4, pady=(0, 4))
        self._chart_frame = frame

        canvas = tk.Canvas(frame, bg=app.BG3, highlightthickness=0, bd=0)
        canvas.pack(fill='both', expand=True)
        self._chart_canvas = canvas

    # Palette for the skill donut/bars — sage/olive first (the biggest slice),
    # then amber/coral/lavender/amber-orange for the rest, cycling if needed.
    # 'Other' always gets a deliberately neutral muted tan, never a theme accent.
    _DONUT_PALETTE_KEYS = ['ACC', 'YEL', 'RED', 'PUR', 'ACC2']
    _DONUT_OTHER_COLOR  = '#a89a78'   # muted warm tan

    # Dynamic donut sizing (see _size_donut_to_panel): the donut frame is
    # resized on every redraw to match the real rendered height of the
    # skill-bar rows next to it, so it stays aligned whether 1 or 9 rows
    # (top-8 + Other) are showing. _DONUT_FALLBACK_SIZE is used only when
    # there's no data, or when Tk hasn't finished laying out the panel yet
    # (e.g. the very first build, before a mainloop pass) — in the latter
    # case a single after_idle retry corrects the size once real geometry
    # is available, guarded by _donut_resize_pending so it can't loop.
    _DONUT_FALLBACK_SIZE  = 130
    _DONUT_MIN_VALID_SIZE = 40


    def _color_for_skill_slot(self, index, name):
        app = self.app
        if name.startswith('Other'):
            return self._DONUT_OTHER_COLOR
        key = self._DONUT_PALETTE_KEYS[index % len(self._DONUT_PALETTE_KEYS)]
        return getattr(app, key)

    def _build_panels(self, parent):
        app = self.app
        row = tk.Frame(parent, bg=app.BG2, padx=12)
        row.pack(fill='both', expand=True, pady=(0, 12))

        left = tk.Frame(row, bg=app.BG3, padx=12, pady=10)
        left.pack(side='left', fill='both', expand=True, padx=(0, 6))
        tk.Label(left, text="Levels by Skill", font=app.SANSB, bg=app.BG3, fg=app.FG).pack(anchor='w')

        skill_body = tk.Frame(left, bg=app.BG3)
        skill_body.pack(fill='both', expand=True, pady=(8, 0))

        donut_frame = tk.Frame(skill_body, bg=app.BG3,
                                width=self._DONUT_FALLBACK_SIZE,
                                height=self._DONUT_FALLBACK_SIZE)
        # pack_propagate(False) + no fill='y': the frame's size is driven
        # entirely by _size_donut_to_panel() (called from _redraw_skill_donut),
        # which measures the real height of the skill-bar rows and resizes
        # this frame to match — not by Tk's default child-driven sizing or
        # by stretching to fill the row's height.
        donut_frame.pack_propagate(False)
        donut_frame.pack(side='left')
        self._donut_frame = donut_frame
        donut_canvas = tk.Canvas(donut_frame, bg=app.BG3, highlightthickness=0, bd=0)
        donut_canvas.pack(fill='both', expand=True)
        self._donut_canvas = donut_canvas

        self._skill_panel_inner = tk.Frame(skill_body, bg=app.BG3)
        self._skill_panel_inner.pack(side='left', fill='both', expand=True, padx=(12, 0))

        right = tk.Frame(row, bg=app.BG3, padx=12, pady=10)
        right.pack(side='left', fill='both', expand=True, padx=(6, 0))
        tk.Label(right, text="Top Accounts", font=app.SANSB, bg=app.BG3, fg=app.FG).pack(anchor='w')
        self._account_panel_inner = tk.Frame(right, bg=app.BG3)
        self._account_panel_inner.pack(fill='both', expand=True, pady=(8, 0))

    # ── Data loading (background thread — disk I/O only) ────────────────────
    def _reload_from_disk(self):
        if self._loading:
            return
        self._loading = True
        self._dirty = False
        if hasattr(self, '_refresh_btn'):
            self._refresh_btn.configure(state='disabled', text='Loading…')

        def _worker():
            try:
                rows = load_levelup_rows()
            except Exception:
                rows = []
            self.app.after(0, lambda: self._on_data_loaded(rows))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_data_loaded(self, rows):
        self._all_rows = rows
        self._loading = False
        if hasattr(self, '_refresh_btn'):
            self._refresh_btn.configure(state='normal', text='↻  Refresh')

        if not rows:
            self._content.pack_forget()
            self._empty.pack(fill='both', expand=True)
            return
        self._empty.pack_forget()
        self._content.pack(fill='both', expand=True)

        accounts = sorted({r['account'] for r in rows})
        self._account_cb.configure(values=[self.ALL_ACCOUNTS] + accounts)
        skills = distinct_skills(rows)
        self._skill_cb.configure(values=[self.ALL_SKILLS] + skills)

        self._apply_filters()

    # ── Filter / date-range handling (in-memory only — no disk I/O) ─────────
    def _on_filter_changed(self):
        self._apply_filters()

    def _set_date_preset(self, preset):
        self._date_preset = preset
        self._refresh_preset_buttons()
        self._apply_filters()

    def _refresh_preset_buttons(self):
        app = self.app
        for preset, btn in self._date_btns.items():
            if preset == self._date_preset:
                btn.configure(bg=app.ACC, fg=app.BG)
            else:
                btn.configure(bg=app.BG3, fg=app.FG2)

    def _on_refresh_click(self):
        self._reload_from_disk()

    def _apply_filters(self):
        if not self._all_rows:
            return
        account = self._account_var.get()
        skill   = self._skill_var.get()
        account = None if account in ('', self.ALL_ACCOUNTS) else account
        skill   = None if skill in ('', self.ALL_SKILLS) else skill
        date_from, date_to = date_bounds_for_preset(self._date_preset)

        # KPI cards + Daily Levels chart: obey every filter.
        rows = filter_rows(self._all_rows, account=account, skill=skill,
                            date_from=date_from, date_to=date_to)
        kpis = compute_kpis(rows, date_from=date_from, date_to=date_to)
        self._update_kpis(kpis, date_from, date_to)

        series = daily_series_for_range(rows, date_from=date_from, date_to=date_to)
        self._redraw_chart(series)

        # Levels by Skill: obeys Account + Date range, ignores Skill — a
        # skill filter would otherwise collapse this panel to one slice.
        rows_for_skills = filter_rows(self._all_rows, account=account, skill=None,
                                       date_from=date_from, date_to=date_to)
        grouped_skills = group_top_n_with_other(aggregate_skill_totals(rows_for_skills), n=8)
        # Skill bars first — the donut measures their rendered height and
        # resizes itself to match (see _size_donut_to_panel).
        self._update_skill_bars(grouped_skills)
        self._redraw_skill_donut(grouped_skills)

        # Top Accounts: obeys Skill + Date range, ignores Account — an
        # account filter would otherwise collapse this panel to one row.
        rows_for_accounts = filter_rows(self._all_rows, account=None, skill=skill,
                                         date_from=date_from, date_to=date_to)
        self._update_account_panel(aggregate_account_totals(rows_for_accounts))

    # ── KPI cards ─────────────────────────────────────────────────────────────
    def _update_kpis(self, kpis, date_from, date_to):
        range_label = 'All time' if not date_from else f"Last {self._date_preset}"

        val, sub = self._kpi_vars['total_levels']
        val.set(str(kpis['total_levels']))
        sub.set(range_label)

        val, sub = self._kpi_vars['avg_per_day']
        val.set(f"{kpis['avg_per_day']:.1f}")
        sub.set(range_label)

        val, sub = self._kpi_vars['best_day']
        date_str, count = kpis['best_day']
        val.set(str(count) if date_str else '—')
        sub.set(date_str or 'No data')

        val, sub = self._kpi_vars['top_account']
        acct, count = kpis['top_account']
        val.set(acct or '—')
        sub.set(f"{count} levels" if acct else 'No data')

    # ── Chart ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _nice_tick_step(max_val, target_steps=5):
        """Pick a clean whole-number step (1/2/5/10/20/25/50/100/...) for
        y-axis gridlines instead of dividing max_val into raw fractional
        chunks (e.g. max=23 -> 0/5/9/14/18/23). Mirrors the intent of
        matplotlib's MaxNLocator without the dependency."""
        if max_val <= 0:
            return 1
        raw_step = max_val / target_steps
        magnitude = 10 ** math.floor(math.log10(raw_step)) if raw_step >= 1 else 1
        for mult in (1, 2, 5, 10):
            step = mult * magnitude
            if step >= raw_step:
                return max(1, int(round(step)))
        return max(1, int(round(magnitude * 10)))

    @staticmethod
    def _format_date_for_axis(date_str):
        """Display-only reformat for the Daily Levels chart's x-axis labels:
        stored rows use 'YYYY-MM-DD' (needed for correct string sorting/
        filtering elsewhere) — this renders that as 'MM-DD-YY' purely for
        the chart. Falls back to the raw string for anything that doesn't
        actually look like 'YYYY-MM-DD'."""
        try:
            y, m, d = date_str.split('-')
            if len(y) == 4 and len(m) == 2 and len(d) == 2:
                return f"{m}-{d}-{y[2:]}"
        except Exception:
            pass
        return date_str

    def _redraw_chart(self, series):
        """Pure-Tkinter Canvas line chart — used on every platform so chart
        rendering never depends on matplotlib at all. Whole-number y-axis
        ticks only, since fractional levels-per-day are meaningless."""
        app = self.app
        c = self._chart_canvas
        try:
            c.delete('all')
            c.update_idletasks()
            w, h = c.winfo_width(), c.winfo_height()
            if w < 10 or h < 10:
                w, h = 700, 260
            # pad_r is generous enough that the rightmost x-axis label
            # (anchored to its own right edge, not centered) never runs
            # off the canvas.
            pad_l, pad_r, pad_t, pad_b = 42, 32, 12, 28
            plot_w = max(w - pad_l - pad_r, 10)
            plot_h = max(h - pad_t - pad_b, 10)

            if not series:
                c.create_text(w / 2, h / 2, text='No data for this filter',
                              fill=app.FG2, font=app.SANS)
                return

            dates  = [d for d, _ in series]
            counts = [v for _, v in series]
            n = len(series)
            data_max = max(max(counts), 1)

            # Clean gridlines: round the axis top up to the next whole
            # multiple of a "nice" step (0/10/20/30/... instead of
            # 0/5/9/14/18/23), so the chart never plots flush to the top.
            step = self._nice_tick_step(data_max)
            y_top = step
            while y_top < data_max:
                y_top += step
            tick_vals = list(range(0, y_top + 1, step))

            def xy(i, val):
                x = pad_l + (i / max(n - 1, 1)) * plot_w
                y = pad_t + plot_h - (val / y_top) * plot_h
                return x, y

            for val in tick_vals:
                _, y = xy(0, val)
                c.create_line(pad_l, y, pad_l + plot_w, y, fill=app.BG4)
                c.create_text(pad_l - 6, y, text=str(val), fill=app.FG2,
                              font=app.SANSS, anchor='e')

            label_step = max(1, n // 6)
            tick_idx = list(range(0, n, label_step))
            if tick_idx[-1] != n - 1:
                tick_idx.append(n - 1)  # always show the actual last day
            for i in tick_idx:
                x, _ = xy(i, 0)
                # The first/last labels anchor to their own edge instead of
                # centering, so they can't run off either side of the canvas.
                anchor = 'nw' if i == 0 else ('ne' if i == n - 1 else 'n')
                c.create_text(x, h - pad_b + 12, text=self._format_date_for_axis(dates[i]),
                              fill=app.FG2, font=app.SANSS, anchor=anchor)

            points = [xy(i, v) for i, v in enumerate(counts)]
            if len(points) > 1:
                c.create_line(*[coord for pt in points for coord in pt],
                              fill=app.ACC, width=2)
            for x, y in points:
                c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=app.ACC, outline=app.ACC)
        except Exception as e:
            self._show_chart_error(str(e))

    def _show_chart_error(self, err_msg):
        """Non-fatal fallback when chart *rendering* itself fails. Hides the
        canvas and shows a clean message instead; the rest of the Stats tab
        (KPIs, donut, panels) is unaffected."""
        try:
            write_debug_entry('stats_chart_error', {'chart': 'main', 'error': err_msg})
        except Exception:
            pass
        self._chart_canvas.pack_forget()
        if getattr(self, '_chart_error_lbl', None) is None:
            self._chart_error_lbl = tk.Label(self._chart_frame, text="Chart unavailable on this system",
                                              font=self.app.SANS, bg=self.app.BG3, fg=self.app.FG2)
        self._chart_error_lbl.pack(expand=True, pady=30)

    # ── Lower panels — rebuild only their own rows, not the whole tab ───────
    def _size_donut_to_panel(self, grouped):
        """Resize the donut frame to match the real rendered height of the
        skill-bar rows next to it (self._skill_panel_inner), so the donut
        stays visually aligned whether 1 row or all 9 (top-8 + Other) are
        showing — no hardcoded guess. Must be called *after*
        _update_skill_bars() has already populated those rows for this
        redraw, or there's nothing real to measure yet.

        Safe fallback: if there's no data, or if Tk hasn't finished laying
        out the panel yet (measured height reads as 0 / unrealistically
        small — e.g. the very first build, before a mainloop pass has run),
        use _DONUT_FALLBACK_SIZE for now. In the layout-not-ready case only
        (real data, just not measurable yet), schedule a single after_idle
        retry to correct the size once real geometry is available — guarded
        by _donut_resize_pending so a still-small measurement on retry can't
        reschedule itself forever.
        """
        if not grouped:
            self._donut_frame.configure(width=self._DONUT_FALLBACK_SIZE,
                                         height=self._DONUT_FALLBACK_SIZE)
            return self._DONUT_FALLBACK_SIZE

        self._skill_panel_inner.update_idletasks()
        measured = self._skill_panel_inner.winfo_height()

        if measured >= self._DONUT_MIN_VALID_SIZE:
            self._donut_resize_pending = False
            size = measured
        else:
            size = self._DONUT_FALLBACK_SIZE
            if not self._donut_resize_pending:
                self._donut_resize_pending = True
                self.app.after_idle(lambda: self._on_donut_resize_retry(grouped))

        self._donut_frame.configure(width=size, height=size)
        return size

    def _on_donut_resize_retry(self, grouped):
        """One-shot retry for _size_donut_to_panel(): re-runs the donut
        redraw now that a mainloop pass has hopefully given the skill panel
        real dimensions. Clears the pending flag itself via the normal
        _size_donut_to_panel() success path, or leaves it for one further
        natural filter-change attempt if it's still not ready."""
        self._donut_resize_pending = False
        self._redraw_skill_donut(grouped)

    def _redraw_skill_donut(self, grouped):
        """Pure-Tkinter Canvas donut, drawn at the size _size_donut_to_panel()
        computes. Uses create_arc(style=PIESLICE) for wedges and a solid
        center circle (matching the card background) to punch the donut
        hole, since Tk has no native ring/annulus primitive.

        Reentrancy guard: update_idletasks() (called here and in
        _size_donut_to_panel) processes Tk's *entire* idle queue, not just
        this widget — which can include another already-queued
        _on_donut_resize_retry callback. Without a guard, that callback
        firing reentrantly mid-redraw could nest indefinitely under rapid
        filter changes and blow the Python call stack. The guard makes a
        reentrant call into this method a harmless no-op instead — the
        outer call (already in progress) finishes the redraw anyway.
        """
        if self._donut_redraw_in_progress:
            return
        self._donut_redraw_in_progress = True
        try:
            app = self.app
            self._size_donut_to_panel(grouped)
            c = self._donut_canvas
            try:
                c.delete('all')
                c.update_idletasks()
                w, h = c.winfo_width(), c.winfo_height()
                if w < 10 or h < 10:
                    w = h = self._DONUT_FALLBACK_SIZE
                cx, cy = w / 2, h / 2
                r = min(w, h) / 2 - 4

                if not grouped:
                    c.create_text(cx, cy, text='No data', fill=app.FG2, font=app.SANSS)
                    return

                total = sum(v for _, v in grouped) or 1
                start = 90.0
                for i, (name, val) in enumerate(grouped):
                    extent = -360.0 * (val / total)
                    color = self._color_for_skill_slot(i, name)
                    c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=extent,
                                 fill=color, outline=app.BG3, width=2, style=tk.PIESLICE)
                    start += extent

                hole_r = r * 0.55
                c.create_oval(cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r,
                              fill=app.BG3, outline=app.BG3)
                c.create_text(cx, cy - 7, text=str(total), fill=app.FG,
                              font=(app.SANS[0], 15, 'bold'))
                c.create_text(cx, cy + 9, text='TOTAL', fill=app.FG2, font=app.SANSS)
            except Exception as e:
                self._show_donut_error(str(e))
        finally:
            self._donut_redraw_in_progress = False

    def _show_donut_error(self, err_msg):
        """Non-fatal fallback when the donut *rendering* itself fails.
        Hides/disables only the donut area — the skill bars (built
        separately in _update_skill_bars) still render normally."""
        try:
            write_debug_entry('stats_chart_error', {'chart': 'donut', 'error': err_msg})
        except Exception:
            pass
        self._donut_canvas.pack_forget()

    def _update_skill_bars(self, grouped):
        app = self.app
        for w in self._skill_panel_inner.winfo_children():
            w.destroy()
        if not grouped:
            tk.Label(self._skill_panel_inner, text="No data", font=app.SANS,
                     bg=app.BG3, fg=app.FG2).pack(anchor='w')
            return
        max_count = max(c for _, c in grouped)
        total = sum(c for _, c in grouped)
        for i, (name, count) in enumerate(grouped):
            color = self._color_for_skill_slot(i, name)
            row = tk.Frame(self._skill_panel_inner, bg=app.BG3)
            row.pack(fill='x', pady=2)
            pct = (count / total * 100) if total else 0
            tk.Frame(row, bg=color, width=10, height=10).pack(side='left', padx=(0, 6))
            tk.Label(row, text=name, font=app.SANS, bg=app.BG3, fg=app.FG,
                     width=14, anchor='w').pack(side='left')
            bar_bg = tk.Frame(row, bg=app.BG4, height=10)
            bar_bg.pack(side='left', fill='x', expand=True, padx=6)
            bar_w_ratio = count / max_count if max_count else 0
            bar_fg = tk.Frame(bar_bg, bg=color, height=10)
            bar_fg.place(relx=0, rely=0, relwidth=bar_w_ratio, relheight=1)
            tk.Label(row, text=f"{count} ({pct:.0f}%)", font=app.SANSS,
                     bg=app.BG3, fg=app.FG2, width=12, anchor='e').pack(side='left')

    def _update_account_panel(self, account_totals):
        app = self.app
        for w in self._account_panel_inner.winfo_children():
            w.destroy()
        if not account_totals:
            tk.Label(self._account_panel_inner, text="No data", font=app.SANS,
                     bg=app.BG3, fg=app.FG2).pack(anchor='w')
            return
        max_count = max(c for _, c in account_totals)
        total = sum(c for _, c in account_totals)
        for i, (acct, count) in enumerate(account_totals[:10], start=1):
            row = tk.Frame(self._account_panel_inner, bg=app.BG3)
            row.pack(fill='x', pady=2)
            pct = (count / total * 100) if total else 0
            tk.Label(row, text=str(i), font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     width=2, anchor='w').pack(side='left')
            tk.Label(row, text=acct, font=app.SANS, bg=app.BG3, fg=app.FG,
                     width=14, anchor='w').pack(side='left')
            bar_bg = tk.Frame(row, bg=app.BG4, height=10)
            bar_bg.pack(side='left', fill='x', expand=True, padx=6)
            bar_w_ratio = count / max_count if max_count else 0
            bar_fg = tk.Frame(bar_bg, bg=app.GREEN, height=10)
            bar_fg.place(relx=0, rely=0, relwidth=bar_w_ratio, relheight=1)
            tk.Label(row, text=f"{count} ({pct:.0f}%)", font=app.SANSS,
                     bg=app.BG3, fg=app.FG2, width=12, anchor='e').pack(side='left')
