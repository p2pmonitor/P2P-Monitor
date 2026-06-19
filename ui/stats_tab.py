"""ui/stats_tab.py — Stats tab for P2P Monitor (Checkpoint 2: real implementation)

Levelup aggregation lives in py/stats.py (pure, testable, no Tkinter). This
file only handles widget construction, the matplotlib chart embed, and wiring
filter/date-range changes to py.stats's pure functions.

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
"""
import threading
import tkinter as tk
from tkinter import ttk

from py.stats import (
    load_levelup_rows, filter_rows, compute_kpis, distinct_skills,
    daily_series_for_range, aggregate_skill_totals, aggregate_account_totals,
    date_bounds_for_preset, DATE_PRESETS, group_top_n_with_other,
)

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.ticker import MaxNLocator
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False


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

        # Prewarm cache — populated by prewarm() (data-only, no widgets, no
        # matplotlib) and consumed once by _ensure_built() the first time the
        # real UI actually gets built, so a prewarmed open skips a redundant
        # disk read instead of re-loading from scratch.
        self._prewarm_rows    = None
        self._prewarm_loading = False

        # Lightweight placeholder — replaced by _build_real_content() on first show
        self._placeholder = tk.Frame(parent_frame, bg=app.BG2)
        self._placeholder.pack(fill='both', expand=True)
        tk.Label(self._placeholder, text="Loading Stats…", font=app.SANS,
                 bg=app.BG2, fg=app.FG2).pack(expand=True)

    # ── Public API (called by App) ───────────────────────────────────────────
    def on_tab_shown(self):
        if not self._ensure_built():
            if self._dirty:
                self._reload_from_disk()

    def prewarm(self):
        """
        Data-only warm-up: load + cache levelup rows from disk in a
        background thread. Deliberately does NOT touch Tkinter widgets in
        any way — no filter row, no KPI cards, no chart/donut canvases, no
        matplotlib Figure/Canvas construction, and never calls
        _build_real_content(). The widget-construction path is reserved for
        the moment the user actually opens the tab, when its frame is about
        to be tkraise()'d and is therefore guaranteed to have real, realized
        screen dimensions — building (and especially drawing matplotlib
        canvases) into a frame that isn't yet mapped/sized is exactly what
        caused the Linux duplicate-Stats-section bug: a partial build could
        fail partway through (observed as "FT_Render_Glyph raster overflow"
        from matplotlib's Agg/FreeType text renderer hitting a degenerate
        canvas size) before `self._built` was ever set, so the next real
        visit rebuilt the whole tab from scratch on top of the broken one.
        Making prewarm purely data-only removes that entire class of risk by
        construction — there is nothing here that can leave partial widgets
        behind, because nothing widget-related is created at all.

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
        Returns True the one time it actually builds, False on every call
        after that. The only place _build_real_content() is ever called —
        only reached from on_tab_shown(), i.e. only when the tab's frame is
        already being tkraise()'d and therefore has real screen dimensions.
        If prewarm() already cached rows, they're consumed here instead of
        re-reading disk — the whole point of prewarming."""
        if self._built:
            return False
        self._placeholder.destroy()
        self._build_real_content()
        self._built = True
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

    # ── Chart theming (shared by the main chart and the skill donut) ────────
    # ax.clear() resets most per-Axes styling back to matplotlib's light-theme
    # defaults, so this must be called both at initial build AND after every
    # clear() — not just once. This is also what fixes the white-chart-on-
    # Linux bug: the figure patch was previously only set once at build time,
    # and the underlying Tk canvas widget's OWN background (separate from
    # matplotlib's figure/axes facecolor) was never touched at all, so any
    # gap between the widget's first paint and the first real plot could show
    # through as Tk's default (white) canvas background.
    def _theme_chart_axes(self, fig, ax, canvas_widget):
        app = self.app
        fig.patch.set_facecolor(app.BG3)
        ax.set_facecolor(app.BG3)
        ax.tick_params(colors=app.FG2, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(app.BG4)
        ax.xaxis.label.set_color(app.FG2)
        ax.yaxis.label.set_color(app.FG2)
        ax.title.set_color(app.FG)
        canvas_widget.configure(bg=app.BG3, highlightthickness=0, bd=0)

    def _build_chart(self, parent):
        app = self.app
        frame = tk.Frame(parent, bg=app.BG3, padx=8, pady=8)
        frame.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        tk.Label(frame, text="Daily Levels Gained", font=app.SANSB,
                 bg=app.BG3, fg=app.FG).pack(anchor='w', padx=4, pady=(0, 4))

        if MATPLOTLIB_AVAILABLE:
            fig = Figure(figsize=(8, 3), dpi=100)
            ax = fig.add_subplot(111)
            canvas = FigureCanvasTkAgg(fig, master=frame)
            tk_widget = canvas.get_tk_widget()
            tk_widget.pack(fill='both', expand=True)
            self._fig, self._ax, self._canvas = fig, ax, canvas
            self._theme_chart_axes(fig, ax, tk_widget)
            canvas.draw()   # force an immediate real paint now, before any
                             # data exists — never leave the widget showing
                             # Tk's default (white) canvas background
        else:
            tk.Label(frame, text="matplotlib is not installed — chart unavailable.\n"
                                  "Run: pip install matplotlib",
                     font=app.SANS, bg=app.BG3, fg=app.FG2, justify='center').pack(expand=True, pady=30)
            self._fig = self._ax = self._canvas = None

    # Palette for the skill donut/bars — sage/olive first (the biggest slice),
    # then amber/coral/lavender/amber-orange for the rest, cycling if needed.
    # 'Other' always gets a deliberately neutral muted tan, never a theme accent.
    _DONUT_PALETTE_KEYS = ['ACC', 'YEL', 'RED', 'PUR', 'ACC2']
    _DONUT_OTHER_COLOR  = '#a89a78'   # muted warm tan

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

        donut_frame = tk.Frame(skill_body, bg=app.BG3, width=130, height=130)
        donut_frame.pack(side='left', fill='y')
        if MATPLOTLIB_AVAILABLE:
            donut_fig = Figure(figsize=(1.9, 1.9), dpi=100)
            donut_ax = donut_fig.add_subplot(111)
            donut_canvas = FigureCanvasTkAgg(donut_fig, master=donut_frame)
            donut_widget = donut_canvas.get_tk_widget()
            donut_widget.pack(fill='both', expand=True)
            self._donut_fig, self._donut_ax, self._donut_canvas = donut_fig, donut_ax, donut_canvas
            self._theme_chart_axes(donut_fig, donut_ax, donut_widget)
            donut_ax.axis('off')
            donut_canvas.draw()
        else:
            self._donut_fig = self._donut_ax = self._donut_canvas = None

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

        rows = filter_rows(self._all_rows, account=account, skill=skill,
                            date_from=date_from, date_to=date_to)

        kpis = compute_kpis(rows, date_from=date_from, date_to=date_to)
        self._update_kpis(kpis, date_from, date_to)

        series = daily_series_for_range(rows, date_from=date_from, date_to=date_to)
        self._redraw_chart(series)

        grouped_skills = group_top_n_with_other(aggregate_skill_totals(rows), n=5)
        self._redraw_skill_donut(grouped_skills)
        self._update_skill_bars(grouped_skills)

        self._update_account_panel(aggregate_account_totals(rows))

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
    def _redraw_chart(self, series):
        if not MATPLOTLIB_AVAILABLE or self._ax is None:
            return
        app = self.app
        ax = self._ax
        ax.clear()
        self._theme_chart_axes(self._fig, ax, self._canvas.get_tk_widget())

        if series:
            dates  = [d for d, _ in series]
            counts = [c for _, c in series]
            x = range(len(dates))
            ax.plot(x, counts, color=app.ACC, linewidth=2, marker='o', markersize=3)
            ax.fill_between(x, counts, color=app.ACC, alpha=0.10)
            n = len(dates)
            step = max(1, n // 8)
            tick_idx = list(range(0, n, step))
            ax.set_xticks(tick_idx)
            ax.set_xticklabels([dates[i] for i in tick_idx], rotation=30, ha='right', fontsize=8)
            ax.set_ylim(bottom=0)
            # Levels gained per day is a whole-number count — never show
            # fractional ticks like 0.5/1.5 (MaxNLocator with integer=True
            # also de-duplicates if matplotlib's autoscaling would otherwise
            # produce repeated/identical integer ticks on a very small range).
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        else:
            ax.text(0.5, 0.5, 'No data for this filter', ha='center', va='center',
                    color=app.FG2, transform=ax.transAxes)
            ax.set_xticks([])

        ax.grid(True, color=app.BG4, linewidth=0.6, alpha=0.6)
        self._fig.tight_layout()
        self._canvas.draw()   # force, not draw_idle() -- see _theme_chart_axes docstring

    # ── Lower panels — rebuild only their own rows, not the whole tab ───────
    def _redraw_skill_donut(self, grouped):
        if not MATPLOTLIB_AVAILABLE or self._donut_ax is None:
            return
        app = self.app
        ax = self._donut_ax
        ax.clear()
        self._theme_chart_axes(self._donut_fig, ax, self._donut_canvas.get_tk_widget())
        ax.axis('off')

        if not grouped:
            ax.text(0.5, 0.5, 'No data', ha='center', va='center',
                    color=app.FG2, transform=ax.transAxes, fontsize=9)
            self._donut_canvas.draw()
            return

        values = [count for _, count in grouped]
        colors = [self._color_for_skill_slot(i, name) for i, (name, _) in enumerate(grouped)]
        ax.pie(values, colors=colors, startangle=90,
               wedgeprops=dict(width=0.42, edgecolor=app.BG3, linewidth=1.5))
        total = sum(values)
        ax.text(0, 0.10, str(total), ha='center', va='center',
                color=app.FG, fontsize=15, fontweight='bold')
        ax.text(0, -0.14, 'TOTAL', ha='center', va='center', color=app.FG2, fontsize=7)
        self._donut_fig.tight_layout()
        self._donut_canvas.draw()

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
