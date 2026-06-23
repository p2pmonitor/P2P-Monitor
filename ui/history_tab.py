"""
ui/history_tab.py — History tab for P2P Monitor (v2.0.0-beta.11 redesign)

Hybrid architecture, deliberately: account headers (few — one per
monitored account) become real Frame-based cards with avatar circles and
genuine Summary/Runtime Stats buttons, matching the mockup. Event rows
*within* each account (potentially hundreds) stay a per-account
ttk.Treeview — rebuilding those as individual Tkinter widgets would risk
exactly the "large history freezes the UI" regression this checkpoint
explicitly warns against. Treeview tag-coloring approximates "badges" as
colored text, not literal rounded pills — a deliberate, documented
simplification of the mockup, same call made for Settings/Monitor/Status.

Preserves exactly (same public names, same signatures, same callers in
p2p_monitor.py / ui/status_tab.py):
  - load(force_full=False)   — reload cache from disk, rebuild display
  - append_entry(account, entry) — live append + debounced rebuild
  - focus_account(account)   — collapse all, expand+scroll to one account
  - on_tab_shown()           — reload on tab switch

Untouched: py/history.py (file format, writing, parsing, dedupe, backfill,
runtime-stats computation) and the date-range filter's logic/validation
(MM/DD/YY parsing, 7-day max, auto-sync From->To) — only its colors/fonts
changed. Column-width persistence (hist_col_widths) is preserved per
account-table column, same config key.

Adds, purely as a presentation-layer filter over the already-loaded
in-memory cache (no new disk reads): a search box (matches account name,
task, or activity/details) and an event-type filter dropdown, plus a
per-event Severity classification derived from the existing `etype` field
(Error/Success/Info) — none of this changes event semantics or what gets
written to disk.
"""
import tkinter as tk
from collections import deque
from datetime import datetime, timedelta
from tkinter import ttk

from py.platform_ops import open_path as _open_path
from py.history import (load_history_accounts, load_history_for,
                         load_history_tail, HISTORY_DIR)
from py.config  import save_config
from py.util    import fmt_ts


class HistoryTab:
    TYPE_FILTER_OPTIONS = [
        ("All Event Types", None),
        ("Task",          'task'),
        ("Quest",         'quest_completed'),
        ("Chat",          'chat'),
        ("Error",         'error'),
        ("Drop",          'drop'),
        ("Death",         'death'),
        ("Level Up",      'levelup'),
        ("Script Event",  'script_event'),
        ("Slayer Task",   'slayer_task'),
        ("Slayer Complete", 'slayer_complete'),
        ("Break",         'break'),
    ]

    def __init__(self, app, parent_frame):
        self.app              = app
        self._filter_date      = None   # None or (ds_from, ds_to)
        self._cache            = {}     # account -> list of entry dicts
        self._open_accounts    = set()
        self._account_widgets  = {}     # account -> dict of widget refs
        self._sort_col         = 'time'
        self._sort_rev         = False
        self._debounce_id      = None
        self._initial_load     = True
        self._search_debounce_id = None
        # Bounded recency guard against double-appending the exact same
        # live event twice (see append_entry) — small and cheap; this tab
        # never needs to remember more than a couple hundred recent keys
        # to make duplicates effectively impossible in practice.
        self._recent_event_keys = deque(maxlen=200)
        self._build(parent_frame)

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self, f):
        app = self.app
        root = tk.Frame(f, bg=app.BG2, padx=16, pady=16)
        root.pack(fill='both', expand=True)

        hdr = tk.Frame(root, bg=app.BG2)
        hdr.pack(fill='x', pady=(0, 4))
        tk.Label(hdr, text="Event History", font=(app.SANS[0], 18, 'bold'),
                 bg=app.BG2, fg=app.FG).pack(anchor='w')
        tk.Label(hdr, text="Recent account activity and archived events",
                 font=app.SANS, bg=app.BG2, fg=app.FG2).pack(anchor='w')

        toolbar = tk.Frame(root, bg=app.BG2, pady=10)
        toolbar.pack(fill='x')
        tk.Button(toolbar, text="⤓ Expand All", font=app.SANSS, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self._expand_all).pack(side='left', padx=(0, 6))
        tk.Button(toolbar, text="⤒ Collapse All", font=app.SANSS, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self._collapse_all).pack(side='left', padx=(0, 6))
        tk.Button(toolbar, text="📂 Open History Folder", font=app.SANSS,
            bg=app.BG4, fg=app.FG2, relief='flat', padx=8, pady=4, cursor='hand2',
            command=lambda: _open_path(HISTORY_DIR)).pack(side='left', padx=(0, 6))

        self._date_btn = tk.Button(toolbar, text="📅 Filter Date", font=app.SANSS,
            bg=app.BG4, fg=app.FG2, relief='flat', padx=8, pady=4, cursor='hand2',
            command=self._toggle_date_picker)
        self._date_btn.pack(side='right')
        self._date_lbl = tk.Label(toolbar, text="", font=app.SANSS, bg=app.BG2, fg=app.YEL)
        self._date_lbl.pack(side='right', padx=(0, 8))

        self._type_filter_var = tk.StringVar(value="All Event Types")
        type_cb = ttk.Combobox(toolbar, textvariable=self._type_filter_var, state='readonly',
                                font=app.SANSS, width=14,
                                values=[lbl for lbl, _ in self.TYPE_FILTER_OPTIONS])
        type_cb.pack(side='right', padx=(0, 8))
        type_cb.bind('<<ComboboxSelected>>', lambda e: self._apply_filters())

        self._search_var = tk.StringVar(value="")
        search_entry = tk.Entry(toolbar, textvariable=self._search_var, font=app.SANSS,
                                 bg=app.BG4, fg=app.FG, relief='flat', insertbackground=app.ACC,
                                 width=24)
        search_entry.pack(side='right', padx=(0, 8), ipady=3)
        self._search_placeholder(search_entry)
        self._search_var.trace_add('write', lambda *_: self._debounce_search())

        outer = tk.Frame(root, bg=app.BG2)
        outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(outer, bg=app.BG2, highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        self._scroll_canvas = canvas
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        self._scrollbar = sb
        self._accounts_frame = tk.Frame(canvas, bg=app.BG2)
        win = canvas.create_window((0, 0), window=self._accounts_frame, anchor='nw')

        def _sync_scroll(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
            content_h = self._accounts_frame.winfo_reqheight()
            visible_h = canvas.winfo_height()
            needs_scroll = content_h > visible_h > 1
            if needs_scroll and not sb.winfo_ismapped():
                sb.pack(side='right', fill='y')
            elif not needs_scroll and sb.winfo_ismapped():
                sb.pack_forget()
                canvas.yview_moveto(0)
        self._accounts_frame.bind('<Configure>', _sync_scroll)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfig(win, width=e.width), _sync_scroll()))

        def _on_enter(_):
            canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-1 * (e.delta // 120), 'units'))
            canvas.bind_all('<Button-4>',   lambda e: canvas.yview_scroll(-1, 'units'))
            canvas.bind_all('<Button-5>',   lambda e: canvas.yview_scroll(1,  'units'))
        def _on_leave(_):
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')
        canvas.bind('<Enter>', _on_enter)
        canvas.bind('<Leave>', _on_leave)

        self._empty_lbl = tk.Label(self._accounts_frame,
            text="No history yet — events will appear here once the monitor starts logging.",
            font=app.SANS, bg=app.BG2, fg=app.FG2)

        footer = tk.Frame(root, bg=app.BG2, pady=6)
        footer.pack(fill='x', side='bottom')
        tk.Label(footer,
            text="ⓘ  Click an account row to expand/collapse  •  Double-click an event for full details",
            font=app.SANSS, bg=app.BG2, fg=app.FG2).pack()

    def _search_placeholder(self, entry):
        app = self.app
        entry.configure(fg=app.FG2)
        entry.insert(0, "Search accounts, tasks, activities...")
        def _on_focus_in(_e):
            if entry.get() == "Search accounts, tasks, activities...":
                entry.delete(0, 'end')
                entry.configure(fg=app.FG)
        def _on_focus_out(_e):
            if not entry.get():
                entry.configure(fg=app.FG2)
                entry.insert(0, "Search accounts, tasks, activities...")
        entry.bind('<FocusIn>', _on_focus_in)
        entry.bind('<FocusOut>', _on_focus_out)

    def _debounce_search(self):
        if self._search_debounce_id:
            self.app.after_cancel(self._search_debounce_id)
        self._search_debounce_id = self.app.after(250, self._apply_filters)

    # ── Public API (called by App / Status tab) — unchanged signatures ─────────
    def load(self, force_full=False):
        """Reload cache from disk and rebuild the display."""
        self._debounce_id = None
        if self._filter_date:
            ds_from, ds_to = self._filter_date
            lo = ds_from + ' 00:00:00'
            hi = ds_to   + ' 23:59:59'
            for acc in load_history_accounts():
                self._cache[acc] = [r for r in load_history_for(acc)
                                    if lo <= r.get('time', '') <= hi]
        else:
            cutoff = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            for acc in load_history_accounts():
                self._cache[acc] = load_history_tail(acc, cutoff)
        self._rebuild_accounts()
        if self._initial_load:
            self._initial_load = False

    def append_entry(self, account, entry):
        """Incrementally apply one live event to the in-memory cache and,
        where possible, the on-screen display — without a full
        destroy/rebuild of every account card. See the class docstring
        for the full contract; summary:
          - always cache (subject to the same 24h trim load() uses)
          - skip entirely if this exact event was already applied (dedup
            guard — see _recent_event_keys)
          - if a date filter is active, cache only — a live event is
            always "now", which by definition isn't inside an explicit
            past date range someone is currently viewing
          - if this is a brand-new account (no card yet), do one real
            rebuild — rare (once per account, ever), and inserting a new
            card at the correct sorted position isn't worth the risk of
            getting pack-ordering wrong by hand
          - if the account has a card but the event doesn't match the
            active type/search filter, cache only
          - otherwise: update that account's count/last-event labels in
            place, and if its tree is currently built (account expanded),
            insert exactly one new row at the correct sorted position
        Never calls _rebuild_accounts() for the normal case this exists
        for. A full rebuild remains the fallback for the cases that
        genuinely need one (see on_tab_shown/load/_apply_filters/_on_sort
        and the explicit rebuild calls elsewhere in this file).
        """
        key = (entry.get('time', ''), account, entry.get('type', ''),
               entry.get('value', ''), entry.get('activity', ''))
        if key in self._recent_event_keys:
            return
        self._recent_event_keys.append(key)

        is_new_account = account not in self._cache
        if is_new_account:
            self._cache[account] = []
        self._cache[account].append(entry)
        cutoff = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        self._cache[account] = [r for r in self._cache[account] if r.get('time', '') >= cutoff]

        if self._filter_date:
            return  # viewing an explicit past range — cache only, see docstring

        if is_new_account:
            self._rebuild_accounts()
            return

        widgets = self._account_widgets.get(account)
        if widgets is None:
            return  # account exists but its card is filtered out of view right now

        if not self._entry_matches_filters(entry, account):
            return  # cached, but doesn't belong in the current filtered view

        filtered = self._filtered_entries_for(account)
        period_lbl = self._period_label()
        widgets['count_lbl'].configure(text=f"{len(filtered)} events ({period_lbl})")
        if filtered:
            widgets['last_ts_lbl'].configure(text=fmt_ts(filtered[0]['time']))

        if widgets.get('tree') is not None:
            self._insert_row_sorted(widgets['tree'], entry)

    def focus_account(self, account):
        """Collapse all accounts, expand+scroll to the target — called from
        Status tab's double-click-account-name action."""
        self._open_accounts = set()
        for acc in self._cache:
            if account.lower() in acc.lower():
                self._open_accounts.add(acc)
        self._rebuild_accounts()
        w = self._account_widgets.get(account) or next(
            (v for k, v in self._account_widgets.items() if account.lower() in k.lower()), None)
        if w:
            self.app.after(50, lambda: self._scroll_to(w['header']))

    def _scroll_to(self, widget):
        try:
            self._scroll_canvas.update_idletasks()
            y = widget.winfo_y()
            total = max(self._accounts_frame.winfo_height(), 1)
            self._scroll_canvas.yview_moveto(y / total)
        except Exception:
            pass

    def on_tab_shown(self):
        """Called when the History tab is selected. Used to unconditionally
        reload from disk every time, which meant every click into History —
        even a redundant click while already on it — did a full disk read
        for every account plus a full destroy/rebuild of every account card.
        Now: only the very first show does that. After that, the in-memory
        cache is kept current via append_entry() as live events arrive (see
        App._on_event's History forwarding), and a full reload only happens
        for the cases that genuinely need one: an explicit date-filter
        change, manual refresh, or backfill completing — see load()'s other
        callers in p2p_monitor.py. Switching away and back must never blank
        or rebuild the tab on its own.
        """
        if self._initial_load:
            self.load()

    # ── Filtering helpers (pure in-memory — no disk reads) ──────────────────────
    def _get_search_text(self):
        s = self._search_var.get().strip().lower()
        return '' if s == "search accounts, tasks, activities..." else s

    @staticmethod
    def _row_matches_search(r, search):
        return (search in str(r.get('value', '')).lower()
                or search in str(r.get('activity', '')).lower()
                or search in str(r.get('type', '')).lower())

    def _apply_filters(self):
        self._rebuild_accounts()

    def _severity(self, etype):
        if etype in ('error', 'death'):
            return 'Error'
        if etype in ('levelup', 'quest_completed', 'drop', 'slayer_complete'):
            return 'Success'
        return 'Info'

    def _type_color(self, etype):
        app = self.app
        return {
            'task':            app.ACC,
            'quest_completed': app.PUR,
            'chat':            app.YEL,
            'error':           app.RED,
            'drop':            app.GREEN,
            'death':           app.RED,
            'levelup':         app.ACC2,
            'script_event':    app.FG2,
            'slayer_task':     app.PUR,
            'slayer_complete': app.GREEN,
            'slayer_skip':     app.RED,
            'break':           app.FG2,
        }.get(etype, app.FG2)

    # ── Filter helpers (shared by full rebuild and incremental append) ─────────
    def _active_type_filter(self):
        type_label = self._type_filter_var.get()
        return dict(self.TYPE_FILTER_OPTIONS).get(type_label)

    def _filtered_entries_for(self, acc):
        """Apply the currently-active type/search filters to one account's
        cached entries. Used by both _rebuild_accounts (all accounts) and
        the incremental append path (one account, on a live event) so the
        two never drift apart on what counts as 'matching'."""
        search = self._get_search_text()
        type_filter = self._active_type_filter()
        entries = [r for r in self._cache.get(acc, []) if r.get('type') != 'scan']
        if type_filter:
            entries = [r for r in entries if r.get('type') == type_filter]
        account_name_matches = bool(search) and search in acc.lower()
        if search and not account_name_matches:
            entries = [r for r in entries if self._row_matches_search(r, search)]
        return entries

    def _entry_matches_filters(self, entry, acc):
        """True if a single new entry would currently be visible for this
        account under the active type/search filters — i.e. whether a live
        append should be inserted into the Treeview, or just cached. An
        account-name search match makes every entry for that account count
        (matches _filtered_entries_for's account_name_matches behavior)."""
        if entry.get('type') == 'scan':
            return False
        type_filter = self._active_type_filter()
        if type_filter and entry.get('type') != type_filter:
            return False
        search = self._get_search_text()
        if search and not (search in acc.lower()) and not self._row_matches_search(entry, search):
            return False
        return True

    # ── Rebuild ──────────────────────────────────────────────────────────────────
    def _rebuild_accounts(self):
        """Full rebuild of every account header card. Reserved for cases
        that genuinely need one — first load, a filter/sort change, manual
        refresh, backfill completing, or a brand-new account appearing —
        never for a normal live event on an account that already has a
        card; see append_entry() for that path. Event ROWS within an
        account are only populated into that account's Treeview if it's
        actually expanded — a collapsed account with thousands of events
        costs nothing."""
        self._debounce_id = None
        for w in self._accounts_frame.winfo_children():
            if w is not self._empty_lbl:
                w.destroy()
        self._account_widgets = {}

        search = self._get_search_text()
        type_filter = self._active_type_filter()
        any_filter_active = bool(search) or bool(type_filter)

        visible = []
        for acc in sorted(self._cache.keys()):
            entries = self._filtered_entries_for(acc)
            account_name_matches = bool(search) and search in acc.lower()
            if any_filter_active and not entries and not account_name_matches:
                continue  # this account has nothing matching the active filter(s)
            visible.append((acc, entries))

        self._empty_lbl.pack_forget()
        if not visible:
            self._empty_lbl.pack(pady=30)
            return

        for acc, entries in visible:
            self._account_widgets[acc] = self._build_account_card(acc, entries)

    def _build_account_card(self, acc, entries):
        app = self.app
        card = tk.Frame(self._accounts_frame, bg=app.BG3)
        card.pack(fill='x', pady=(0, 8))

        is_open = acc in self._open_accounts
        period_lbl = self._period_label()
        count_lbl = f"{len(entries)} events ({period_lbl})"
        last_event_ts = fmt_ts(entries[0]['time']) if entries else '—'

        header = tk.Frame(card, bg=app.BG3, padx=12, pady=10, cursor='hand2')
        header.pack(fill='x')

        avatar = tk.Canvas(header, width=30, height=30, bg=app.BG3, highlightthickness=0)
        avatar.pack(side='left', padx=(0, 10))
        avatar.create_oval(2, 2, 28, 28, fill=app.ACC, outline=app.ACC)
        avatar.create_text(15, 15, text=(acc[:1] or '?').upper(), fill=app.BG, font=app.SANSB)

        chevron = tk.Label(header, text=('▾' if is_open else '▸'), font=app.SANS,
                            bg=app.BG3, fg=app.FG2)
        chevron.pack(side='left', padx=(0, 8))

        name_lbl = tk.Label(header, text=acc, font=app.SANSB, bg=app.BG3, fg=app.FG, anchor='w')
        name_lbl.pack(side='left')

        meta = tk.Frame(header, bg=app.BG3)
        meta.pack(side='left', padx=(24, 0))
        count_var_lbl = tk.Label(meta, text=count_lbl, font=app.SANS, bg=app.BG3, fg=app.FG, anchor='w')
        count_var_lbl.pack(anchor='w')
        tk.Label(meta, text=f"Events ({period_lbl})", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                 anchor='w').pack(anchor='w')

        meta2 = tk.Frame(header, bg=app.BG3)
        meta2.pack(side='left', padx=(24, 0))
        last_ts_var_lbl = tk.Label(meta2, text=last_event_ts, font=app.SANS, bg=app.BG3, fg=app.FG, anchor='w')
        last_ts_var_lbl.pack(anchor='w')
        tk.Label(meta2, text="Last event", font=app.SANSS, bg=app.BG3, fg=app.FG2,
                 anchor='w').pack(anchor='w')

        btn_row = tk.Frame(header, bg=app.BG3)
        btn_row.pack(side='right')
        summary_btn = tk.Button(btn_row, text="📊 Summary", font=app.SANSS,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=8, pady=4, cursor='hand2',
            # Recomputed fresh at click time rather than closing over this
            # build's `entries` snapshot — that snapshot would go stale the
            # moment a live event incrementally updates the cache without a
            # full rebuild (see append_entry()).
            command=lambda a=acc: self._show_summary_popup(a, self._filtered_entries_for(a)))
        summary_btn.pack(side='left', padx=(0, 6))
        runtime_btn = tk.Button(btn_row, text="📈 Runtime Stats", font=app.SANSS,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=8, pady=4, cursor='hand2',
            command=lambda a=acc: self._show_runtime_stats_popup(a))
        runtime_btn.pack(side='left')

        for w in (header, avatar, chevron, name_lbl, meta, meta2):
            w.bind('<Button-1>', lambda e, a=acc: self._toggle_account(a))

        body_outer = tk.Frame(card, bg=app.BG3, padx=12)
        tree = None
        if is_open:
            body_outer.pack(fill='x', pady=(0, 10))
            tree = self._build_event_tree(body_outer, acc, entries)

        return {'card': card, 'header': header, 'chevron': chevron,
                'body_outer': body_outer, 'tree': tree,
                'count_lbl': count_var_lbl, 'last_ts_lbl': last_ts_var_lbl}

    def _period_label(self):
        if not self._filter_date:
            return "24h"
        ds_from, ds_to = self._filter_date
        try:
            disp_from = datetime.strptime(ds_from, '%Y-%m-%d').strftime('%m/%d/%y')
            disp_to   = datetime.strptime(ds_to,   '%Y-%m-%d').strftime('%m/%d/%y')
            return disp_from if ds_from == ds_to else f"{disp_from} → {disp_to}"
        except Exception:
            return ds_from if ds_from == ds_to else f"{ds_from} → {ds_to}"

    def _toggle_account(self, acc):
        if acc in self._open_accounts:
            self._open_accounts.discard(acc)
        else:
            self._open_accounts.add(acc)
        self._rebuild_accounts()

    # ── Per-account event tree ───────────────────────────────────────────────────
    COL_DEFAULTS = {'time': 110, 'type': 110, 'value': 220, 'activity': 420, 'severity': 90}

    def _build_event_tree(self, parent, acc, entries):
        app = self.app
        saved_widths = app.cfg.get('hist_col_widths', {})
        cols = ('time', 'type', 'value', 'activity', 'severity')
        tree = ttk.Treeview(parent, columns=cols, show='headings',
                             height=min(max(len(entries), 3), 18))
        for col, lbl in [('time', 'Time'), ('type', 'Type'), ('value', 'Task'),
                          ('activity', 'Activity / Details'), ('severity', 'Severity')]:
            tree.heading(col, text=lbl, command=lambda c=col: self._on_sort(c))
            tree.column(col, width=saved_widths.get(col, self.COL_DEFAULTS[col]),
                        stretch=(col == 'activity'), anchor='w')

        scr = ttk.Scrollbar(parent, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scr.set)
        scr.pack(side='right', fill='y')
        tree.pack(fill='x', expand=True)

        for etype in ('task', 'quest_completed', 'chat', 'error', 'drop', 'death',
                      'levelup', 'script_event', 'slayer_task', 'slayer_complete',
                      'slayer_skip', 'break', 'info'):
            tree.tag_configure(etype, foreground=self._type_color(etype))

        sorted_entries = self._sort_entries(entries)
        raw_times = {}
        sev_dot = {'Error': '🔴', 'Success': '🟢', 'Info': '⚪'}
        for r in sorted_entries:
            etype = r.get('type', '')
            tag = etype if etype in (
                'task', 'quest_completed', 'chat', 'error', 'drop', 'death',
                'levelup', 'script_event', 'slayer_task', 'slayer_complete',
                'slayer_skip', 'break') else 'info'
            sev_label = self._severity(etype)
            iid = tree.insert('', 'end', values=(
                fmt_ts(r.get('time', '')), etype, r.get('value', ''),
                r.get('activity', ''), f"{sev_dot[sev_label]} {sev_label}"), tags=(tag,))
            raw_times[iid] = r.get('time', '')

        tree.bind('<ButtonRelease-1>', lambda e, t=tree: self._on_col_resize(t))
        tree.bind('<Double-1>', lambda e, t=tree, ac=acc: self._on_event_double_click(e, t, ac))
        tree.bind('<Motion>', lambda e, t=tree: self._on_tree_motion(e, t))
        tree.bind('<Leave>', self._hide_tooltip)
        tree.raw_times = raw_times  # stashed for sort/tooltip lookups
        return tree

    def _sort_entries(self, entries):
        idx_key = {
            'time':     lambda r: r.get('time', ''),
            'type':     lambda r: r.get('type', ''),
            'value':    lambda r: str(r.get('value', '')),
            'activity': lambda r: str(r.get('activity', '')),
            'severity': lambda r: self._severity(r.get('type', '')),
        }.get(self._sort_col, lambda r: r.get('time', ''))
        return sorted(entries, key=idx_key, reverse=self._sort_rev)

    def _sort_key_for(self, entry):
        """Same key derivation as _sort_entries, for a single entry — used
        by _insert_row_sorted to find where one new row belongs without
        re-sorting/rebuilding the whole tree."""
        return {
            'time':     lambda r: r.get('time', ''),
            'type':     lambda r: r.get('type', ''),
            'value':    lambda r: str(r.get('value', '')),
            'activity': lambda r: str(r.get('activity', '')),
            'severity': lambda r: self._severity(r.get('type', '')),
        }.get(self._sort_col, lambda r: r.get('time', ''))(entry)

    def _insert_row_sorted(self, tree, entry):
        """Insert exactly one new row into an already-built, already-open
        account's Treeview, at the position the current sort column/
        direction says it belongs — instead of destroying and rebuilding
        the whole tree for a single live event. Linear scan rather than
        bisect: account event trees are bounded by the same 24h/filtered
        window the rest of this tab already works with, never the
        thousands-of-rows scale a global bisect would matter for."""
        new_key = self._sort_key_for(entry)
        children = tree.get_children()
        insert_at = 'end'
        for idx, iid in enumerate(children):
            if self._sort_col == 'time':
                existing_key = tree.raw_times.get(iid, '')
            else:
                vals = tree.item(iid, 'values')
                col_idx = {'time': 0, 'type': 1, 'value': 2, 'activity': 3, 'severity': 4}[self._sort_col]
                existing_key = vals[col_idx] if vals else ''
            is_before = (new_key < existing_key) if not self._sort_rev else (new_key > existing_key)
            if is_before:
                insert_at = idx
                break

        etype = entry.get('type', '')
        tag = etype if etype in (
            'task', 'quest_completed', 'chat', 'error', 'drop', 'death',
            'levelup', 'script_event', 'slayer_task', 'slayer_complete',
            'slayer_skip', 'break') else 'info'
        sev_dot = {'Error': '🔴', 'Success': '🟢', 'Info': '⚪'}
        sev_label = self._severity(etype)
        iid = tree.insert('', insert_at, values=(
            fmt_ts(entry.get('time', '')), etype, entry.get('value', ''),
            entry.get('activity', ''), f"{sev_dot[sev_label]} {sev_label}"), tags=(tag,))
        tree.raw_times[iid] = entry.get('time', '')

    def _on_sort(self, col):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        self._rebuild_accounts()

    # ── Per-tree interactions ─────────────────────────────────────────────────────
    def _on_col_resize(self, tree):
        app = self.app
        widths = {col: tree.column(col, 'width')
                  for col in ('time', 'type', 'value', 'activity', 'severity')}
        app.cfg['hist_col_widths'] = widths
        save_config(app.cfg)

    def _on_event_double_click(self, event, tree, acc):
        item = tree.identify_row(event.y)
        if not item:
            return
        vals = tree.item(item, 'values')
        if not vals:
            return
        self._show_event_detail_popup(acc, vals)

    def _show_event_detail_popup(self, acc, vals):
        app = self.app
        time_s, etype, value, activity, severity = vals
        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title(f"Event Details — {acc}")
        popup.resizable(False, False)
        popup.transient(app)
        tk.Label(popup, text=f"  {acc}", font=app.SANSB, bg=app.BG2, fg=app.ACC,
                 padx=12, pady=8).pack(fill='x')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x')
        for label, val in [("Time", time_s), ("Type", etype), ("Task", value),
                            ("Activity / Details", activity), ("Severity", severity)]:
            row = tk.Frame(popup, bg=app.BG2)
            row.pack(fill='x', padx=16, pady=4)
            tk.Label(row, text=label, font=app.SANSS, bg=app.BG2, fg=app.FG2,
                     width=16, anchor='w').pack(side='left')
            tk.Label(row, text=val, font=app.SANS, bg=app.BG2, fg=app.FG, anchor='w',
                     wraplength=360, justify='left').pack(side='left', fill='x', expand=True)
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        tk.Button(popup, text="Close", font=app.SANS, bg=app.BG3, fg=app.FG2,
                  relief='flat', padx=12, pady=4, cursor='hand2',
                  command=popup.destroy).pack(pady=8)

    def _on_tree_motion(self, event, tree):
        """Show tooltip for truncated cell text on hover — same logic as
        before, just rebound per-account-tree instead of one shared tree."""
        app = self.app
        item = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not item or not col:
            self._hide_tooltip()
            return
        if (getattr(self, '_tooltip_item', None) == item and
                getattr(self, '_tooltip_col', None) == col and
                getattr(self, '_tooltip_tree', None) is tree):
            return
        self._tooltip_item = item
        self._tooltip_col = col
        self._tooltip_tree = tree
        self._hide_tooltip()
        col_names = {'#1': 0, '#2': 1, '#3': 2, '#4': 3, '#5': 4}
        idx = col_names.get(col)
        if idx is None:
            return
        vals = tree.item(item, 'values')
        text = vals[idx] if vals and idx < len(vals) else ''
        if not text:
            return
        try:
            from tkinter.font import Font
            font = Font(font=app.SANS)
            text_w = font.measure(str(text))
            col_w = tree.column(col, 'width')
            if text_w <= col_w - 8:
                return
        except Exception:
            return
        x = tree.winfo_rootx() + event.x + 12
        y = tree.winfo_rooty() + event.y + 16
        self._tooltip_win = tw = tk.Toplevel(app)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=app.BG4)
        tk.Label(tw, text=str(text), font=app.SANS, bg=app.BG4, fg=app.FG,
                 padx=8, pady=4, wraplength=600, justify='left').pack()

    def _hide_tooltip(self, event=None):
        if getattr(self, '_tooltip_win', None):
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None
        self._tooltip_item = None
        self._tooltip_col = None
        self._tooltip_tree = None

    def _expand_all(self):
        self._open_accounts = set(self._cache.keys())
        self._rebuild_accounts()

    def _collapse_all(self):
        self._open_accounts = set()
        self._rebuild_accounts()

    # ── Summary popup ──────────────────────────────────────────────────────────
    def _show_summary_popup(self, acc, entries):
        app = self.app
        counts = {}
        for r in entries:
            t = r.get('type', '')
            if t and t != 'scan':
                counts[t] = counts.get(t, 0) + 1
        parts = []
        for label, keys in [
            ('Quests', ['quest_completed', 'quest_started']), ('Tasks', ['task']),
            ('Chats', ['chat']), ('Errors', ['error']), ('Drops', ['drop']),
            ('Deaths', ['death']), ('Levels', ['levelup']),
        ]:
            n = sum(counts.get(k, 0) for k in keys)
            parts.append((label, n))

        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title(f"Summary — {acc}")
        popup.resizable(False, False)
        popup.transient(app)
        tk.Label(popup, text=f"  {acc}", font=app.SANSB, bg=app.BG2, fg=app.ACC,
                 padx=12, pady=8).pack(fill='x')
        tk.Label(popup, text=f"  Period: {self._period_label()}", font=app.SANS,
                 bg=app.BG2, fg=app.FG2, padx=12).pack(fill='x')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        for label, n in parts:
            row = tk.Frame(popup, bg=app.BG2)
            row.pack(fill='x', padx=16, pady=2)
            tk.Label(row, text=label, font=app.SANS, bg=app.BG2, fg=app.FG2,
                     width=10, anchor='w').pack(side='left')
            tk.Label(row, text=str(n), font=app.SANSB, bg=app.BG2, fg=app.ACC).pack(side='left')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        tk.Button(popup, text="Close", font=app.SANS, bg=app.BG3, fg=app.FG2,
                  relief='flat', padx=12, pady=4, cursor='hand2',
                  command=popup.destroy).pack(pady=8)

    # ── Runtime stats popup ────────────────────────────────────────────────────
    def _show_runtime_stats_popup(self, acc):
        from py.history import compute_runtime_stats, _fmt_secs
        from datetime import date as _date, datetime as _dt, timedelta as _td
        app = self.app

        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title(f"Runtime Stats — {acc}")
        popup.resizable(False, False)
        popup.transient(app)

        tk.Label(popup, text=f"  {acc}", font=app.SANSB, bg=app.BG2, fg=app.ACC,
                 padx=12, pady=8).pack(fill='x')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(0, 4))

        range_var = tk.StringVar(value='all')
        range_frame = tk.Frame(popup, bg=app.BG2)
        range_frame.pack(fill='x', padx=16, pady=(4, 2))
        tk.Label(range_frame, text="Range:", font=app.SANS, bg=app.BG2, fg=app.FG2).pack(side='left')
        stats_frame = tk.Frame(popup, bg=app.BG2)
        stats_frame.pack(fill='x', padx=16, pady=(2, 8))

        def _show_stats(since_ts=None, until_ts=None):
            for w in stats_frame.winfo_children():
                w.destroy()
            stats = compute_runtime_stats(acc, since_ts=since_ts, until_ts=until_ts)
            rows_data = [
                ('Total running time', _fmt_secs(stats['total_run_secs'])),
                ('Active play time',   _fmt_secs(stats['active_secs'])),
                ('Break time',         _fmt_secs(stats['break_secs'])),
                ('Break %',            f"{stats['break_pct']:.1f}%"),
            ]
            for lbl, val in rows_data:
                row = tk.Frame(stats_frame, bg=app.BG2)
                row.pack(fill='x', pady=2)
                tk.Label(row, text=lbl, font=app.SANS, bg=app.BG2, fg=app.FG2,
                         width=20, anchor='w').pack(side='left')
                tk.Label(row, text=val, font=app.SANSB, bg=app.BG2, fg=app.ACC).pack(side='left')

        def _on_range(*_):
            r = range_var.get()
            today = _date.today()
            if r == 'today':
                _show_stats(since_ts=_dt.combine(today, _dt.min.time()).timestamp())
            elif r == '7d':
                _show_stats(since_ts=_dt.combine(today - _td(days=7), _dt.min.time()).timestamp())
            elif r == '30d':
                _show_stats(since_ts=_dt.combine(today - _td(days=30), _dt.min.time()).timestamp())
            else:
                _show_stats()

        for text, val in [('All time', 'all'), ('Today', 'today'), ('7 days', '7d'), ('30 days', '30d')]:
            tk.Radiobutton(range_frame, text=text, value=val, variable=range_var,
                font=app.SANS, bg=app.BG2, fg=app.FG, activebackground=app.BG2,
                selectcolor=app.BG2, relief='flat', cursor='hand2',
                command=_on_range).pack(side='left', padx=(8, 0))

        _show_stats()

        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        tk.Button(popup, text="Close", font=app.SANS, bg=app.BG3, fg=app.FG2,
                  relief='flat', padx=12, pady=4, cursor='hand2',
                  command=popup.destroy).pack(pady=8)

    # ── Date filter popup ────────────────────────────────────────────────────────
    def _toggle_date_picker(self):
        if hasattr(self, '_date_popup') and self._date_popup and self._date_popup.winfo_exists():
            self._date_popup.lift()
            self._date_popup.focus_force()
            return

        from datetime import date as _date
        app = self.app
        today = _date.today()

        if self._filter_date:
            try:
                d_from = datetime.strptime(self._filter_date[0], '%Y-%m-%d').date()
                d_to   = datetime.strptime(self._filter_date[1], '%Y-%m-%d').date()
            except Exception:
                d_from = d_to = today
        else:
            d_from = d_to = today

        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title("Filter by Date")
        popup.resizable(False, False)
        popup.transient(app)
        self._date_popup = popup

        try:
            bx = self._date_btn.winfo_rootx()
            by = self._date_btn.winfo_rooty() + self._date_btn.winfo_height() + 4
            popup.geometry(f"+{bx}+{by}")
        except Exception:
            pass

        tk.Label(popup, text="Select date range (max 7 days)", font=app.SANSB,
                 bg=app.BG2, fg=app.ACC).pack(padx=14, pady=(10, 2))
        tk.Label(popup, text="Format: MM/DD/YY", font=app.SANS,
                 bg=app.BG2, fg=app.FG2).pack(padx=14, pady=(0, 6))

        def _make_entry_row(parent, label, init_date):
            row = tk.Frame(parent, bg=app.BG2)
            row.pack(fill='x', padx=14, pady=3)
            tk.Label(row, text=label, font=app.SANS, bg=app.BG2, fg=app.FG2,
                     width=7, anchor='w').pack(side='left')
            var = tk.StringVar(value=init_date.strftime('%m/%d/%y'))
            entry = tk.Entry(row, textvariable=var, font=app.SANS, bg=app.BG3,
                             fg=app.FG, insertbackground=app.ACC, relief='flat', width=10)
            entry.pack(side='left', ipady=4, padx=(4, 0))
            return var, entry

        from_var, from_entry = _make_entry_row(popup, "From:", d_from)
        to_var, to_entry = _make_entry_row(popup, "To:", d_to)

        _syncing = [True]
        def _sync_to(*_):
            if _syncing[0]:
                to_var.set(from_var.get())
        def _unsync(*_):
            _syncing[0] = False
        from_var.trace_add('write', _sync_to)
        to_entry.bind('<Key>', _unsync)

        err_lbl = tk.Label(popup, text="", font=app.SANS, bg=app.BG2, fg=app.RED)
        err_lbl.pack(pady=(4, 0))

        def _parse(s):
            for fmt in ('%m/%d/%y', '%m/%d/%Y', '%m-%d-%y', '%m-%d-%Y'):
                try:
                    return datetime.strptime(s.strip(), fmt).date()
                except ValueError:
                    pass
            return None

        def _apply():
            err_lbl.config(text="")
            d1 = _parse(from_var.get())
            d2 = _parse(to_var.get())
            if d1 is None:
                err_lbl.config(text="Invalid From date — use MM/DD/YY"); return
            if d2 is None:
                err_lbl.config(text="Invalid To date — use MM/DD/YY"); return
            if d2 < d1:
                d1, d2 = d2, d1
            if (d2 - d1).days > 6:
                err_lbl.config(text="Maximum range is 7 days."); return
            ds_from = d1.strftime('%Y-%m-%d')
            ds_to   = d2.strftime('%Y-%m-%d')
            self._filter_date = (ds_from, ds_to)
            disp_from = d1.strftime('%m/%d/%y')
            disp_to   = d2.strftime('%m/%d/%y')
            lbl = disp_from if ds_from == ds_to else f"{disp_from} → {disp_to}"
            self._date_lbl.config(text=f"📅 {lbl}")
            self._date_btn.config(fg=app.YEL)
            popup.destroy()
            self.load()

        def _clear():
            self._filter_date = None
            self._date_lbl.config(text="")
            self._date_btn.config(fg=app.FG2)
            popup.destroy()
            self.load()

        bf = tk.Frame(popup, bg=app.BG2)
        bf.pack(pady=(4, 12))
        tk.Button(bf, text="Apply", font=app.SANS, bg=app.ACC, fg=app.BG,
            relief='flat', padx=12, pady=4, cursor='hand2', command=_apply).pack(side='left', padx=6)
        tk.Button(bf, text="Clear / Show 24h", font=app.SANS, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=12, pady=4, cursor='hand2', command=_clear).pack(side='left', padx=6)
        from_entry.focus_set()
