"""ui/history_tab.py — History tab for P2P Monitor"""
import subprocess
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk

from py.history import (load_history_accounts, load_history_for,
                         load_history_tail, HISTORY_DIR)
from py.config  import save_config
from py.util    import fmt_ts


class HistoryTab:
    def __init__(self, app, parent_frame):
        self.app              = app
        self._filter_date     = None   # None or (ds_from, ds_to)
        self._cache           = {}     # account -> list of entry dicts
        self._open_accounts   = set()
        self._raw_times       = {}     # tree item id -> raw timestamp str
        self._summary         = {}     # parent item id -> summary string
        self._sort_col        = 'time'
        self._sort_rev        = False
        self._debounce_id     = None
        self._initial_load    = True
        self._build(parent_frame)

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self, f):
        app = self.app

        # Header
        hdr = tk.Frame(f, bg=app.BG2, padx=12, pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text="Event History  (last 24h)", font=app.MONOL,
                 bg=app.BG2, fg=app.ACC).pack(side='left')
        self._date_lbl = tk.Label(hdr, text="", font=app.MONO, bg=app.BG2, fg=app.YEL)
        self._date_lbl.pack(side='left', padx=(12, 0))

        # Toolbar
        tb = tk.Frame(f, bg=app.BG3, padx=12, pady=6)
        tb.pack(fill='x')
        tk.Button(tb, text="Expand All", font=app.MONO, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=8, pady=3, cursor='hand2',
            command=self._expand_all).pack(side='left', padx=(0, 6))
        tk.Button(tb, text="Collapse All", font=app.MONO, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=8, pady=3, cursor='hand2',
            command=self._collapse_all).pack(side='left', padx=(0, 6))
        hist_folder_btn = tk.Button(tb, text="📂 History Folder", font=app.MONO,
            bg=app.BG4, fg=app.FG2, relief='flat', padx=8, pady=3, cursor='hand2',
            command=lambda: subprocess.Popen(['xdg-open', str(HISTORY_DIR)]))
        hist_folder_btn.pack(side='left', padx=(0, 6))
        self._date_btn = tk.Button(tb, text="📅 Filter Date", font=app.MONO,
            bg=app.BG4, fg=app.FG2, relief='flat', padx=8, pady=3, cursor='hand2',
            command=self._toggle_date_picker)
        self._date_btn.pack(side='right', padx=(6, 0))

        # Treeview
        cols = ('time', 'type', 'value', 'activity')
        self._tree = ttk.Treeview(f, columns=cols, show='tree headings', height=22)
        col_defaults = {'#0': 220, 'time': 130, 'type': 100, 'value': 260, 'activity': 500}
        saved_widths = app.cfg.get('hist_col_widths', {})
        self._tree.heading('#0', text='Account')
        self._tree.column('#0', width=saved_widths.get('#0', 220), stretch=False, anchor='w')
        for col, lbl in [('time', 'Time'), ('type', 'Type'),
                          ('value', 'Task'), ('activity', 'Activity')]:
            self._tree.heading(col, text=lbl, command=lambda c=col: self._sort(c))
            self._tree.column(col, width=saved_widths.get(col, col_defaults[col]),
                              stretch=False, anchor='w')

        for tag, col in [
            ('quest_completed', '#88ffbb'),
            ('task',           app.ACC),
            ('break',          app.FG2),
            ('error',          app.RED),
            ('drop',           '#00ff88'),
            ('slayer_complete', '#88ffbb'),
            ('slayer_skip',    app.RED),
            ('death',          app.RED),
            ('levelup',        '#00ff88'),
            ('script_event',   '#ff8888'),
            ('summary',        app.FG2),
            ('account',        app.ACC),
        ]:
            kw = {}
            if tag == 'account':
                kw = {'font': app.MONOB, 'background': app.BG4}
            self._tree.tag_configure(tag, foreground=col, **kw)

        scr  = ttk.Scrollbar(f, orient='vertical',   command=self._tree.yview)
        self._tree.configure(yscrollcommand=scr.set)
        scr.pack(side='right', fill='y')
        self._tree.pack(fill='both', expand=True)

        self._tree.bind('<ButtonRelease-1>', self._on_col_resize)
        self._tree.bind('<<TreeviewOpen>>',  self._on_expand)
        self._tree.bind('<<TreeviewClose>>', self._on_collapse)
        self._tree.bind('<Double-1>',        self._on_double_click)
        self._tree.bind('<Button-1>',        self._on_click)
        self._tree.bind('<Motion>',          self._on_motion)
        self._tree.bind('<Leave>',           self._hide_tooltip)
        self._tooltip_win  = None
        self._tooltip_item = None
        self._tooltip_col  = None

    # ── Public API (called by App) ─────────────────────────────────────────────
    def load(self, force_full=False):
        """Reload cache from disk and rebuild tree."""
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
        self._rebuild_tree()
        if self._initial_load:
            self._initial_load = False

    def append_entry(self, account, entry):
        """Append a new live entry and schedule a debounced tree rebuild."""
        cutoff = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        if account not in self._cache:
            self._cache[account] = []
        self._cache[account].append(entry)
        self._cache[account] = [r for r in self._cache[account]
                                 if r.get('time', '') >= cutoff]
        if not self._filter_date:
            if self._debounce_id:
                self.app.after_cancel(self._debounce_id)
            self._debounce_id = self.app.after(5000, self._rebuild_tree)

    def focus_account(self, account):
        """Collapse all accounts, expand the target account — called from Status tab double-click."""
        for item in self._tree.get_children():
            self._tree.item(item, open=False)
        for item in self._tree.get_children():
            label = self._tree.item(item, 'text') or ''
            if account.lower() in label.lower():
                self._tree.item(item, open=True)
                self._tree.see(item)
                break

    def on_tab_shown(self):
        """Called when the History tab is selected — reload from disk."""
        self.load()

    # ── Tree rebuild ───────────────────────────────────────────────────────────
    def _rebuild_tree(self):
        self._debounce_id = None
        app = self.app
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._raw_times = {}
        self._summary   = {}
        was_open = set(self._open_accounts)
        self._open_accounts = set()

        for acc in sorted(self._cache.keys()):
            entries = sorted(
                enumerate(self._cache[acc]),
                key=lambda x: (x[1].get('time', ''), x[0]),
                reverse=True)
            entries = [r for _, r in entries]
            counts  = {}
            for r in entries:
                t = r.get('type', '')
                if t and t != 'scan':
                    counts[t] = counts.get(t, 0) + 1

            if self._filter_date:
                ds_from, ds_to = self._filter_date
                from datetime import datetime as _dt
                try:
                    disp_from = _dt.strptime(ds_from, '%Y-%m-%d').strftime('%m/%d/%y')
                    disp_to   = _dt.strptime(ds_to,   '%Y-%m-%d').strftime('%m/%d/%y')
                    lbl = disp_from if ds_from == ds_to else f"{disp_from} → {disp_to}"
                except Exception:
                    lbl = ds_from if ds_from == ds_to else f"{ds_from} → {ds_to}"
                count_label = f"{len(entries)} entries ({lbl})"
            else:
                count_label = f"{len(entries)} entries (24h)"

            parent = self._tree.insert('', 'end',
                text=f"  {acc}",
                values=(count_label, '', '', '📊 Summary'),
                tags=('account',), open=(acc in was_open))

            parts = []
            for label, keys in [
                ('Quests', ['quest_completed', 'quest_started']), ('Tasks', ['task']),   ('Chats', ['chat']),
                ('Errors', ['error']), ('Drops', ['drop']),   ('Deaths', ['death']),
                ('Levels', ['levelup']),
            ]:
                n = sum(counts.get(k, 0) for k in keys)
                parts.append(f"{label}: {n}")
            self._summary[parent] = '  │  '.join(parts)

            for r in entries:
                etype = r.get('type', '')
                if etype == 'scan':
                    continue
                raw_time = r.get('time', '')
                tag = etype if etype in (
                    'quest_completed', 'task', 'slayer_task', 'error', 'drop',
                    'slayer_complete', 'slayer_skip', 'break',
                    'death', 'levelup', 'script_event') else 'info'
                iid = self._tree.insert(parent, 'end',
                    values=(fmt_ts(raw_time), etype, r.get('value', ''), r.get('activity', '')),
                    tags=(tag,))
                self._raw_times[iid] = raw_time

            if acc in was_open:
                self._open_accounts.add(acc)

    # ── Sort ───────────────────────────────────────────────────────────────────
    def _sort(self, col):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        col_idx = {'time': 0, 'type': 1, 'value': 2, 'activity': 3}
        idx = col_idx.get(col, 0)
        for parent in self._tree.get_children():
            items = []
            for c in self._tree.get_children(parent):
                vals = self._tree.item(c, 'values')
                key  = self._raw_times.get(c, vals[idx]) if col == 'time' else vals[idx]
                items.append((key, c))
            items.sort(key=lambda x: x[0], reverse=self._sort_rev)
            for i, (_, child) in enumerate(items):
                self._tree.move(child, parent, i)

    # ── Tree events ────────────────────────────────────────────────────────────
    def _on_expand(self, event):
        item = self._tree.focus()
        if item and not self._tree.parent(item):
            self._open_accounts.add(self._tree.item(item, 'text').strip())

    def _on_collapse(self, event):
        item = self._tree.focus()
        if item and not self._tree.parent(item):
            self._open_accounts.discard(self._tree.item(item, 'text').strip())

    def _on_double_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item or self._tree.parent(item):
            return
        self._tree.item(item, open=not self._tree.item(item, 'open'))

    def _on_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        col  = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if not item or self._tree.parent(item):
            return
        if col != '#4':
            return
        vals = self._tree.item(item, 'values')
        if not vals or '📊 Summary' not in str(vals[-1]):
            return
        summary = self._summary.get(item, '')
        if not summary:
            return
        self._show_summary_popup(item, summary, event.x, event.y)

    def _on_col_resize(self, event):
        app = self.app
        widths = {col: self._tree.column(col, 'width')
                  for col in ('#0', 'time', 'type', 'value', 'activity')}
        app.cfg['hist_col_widths'] = widths
        save_config(app.cfg)

    def _on_motion(self, event):
        """Show tooltip for truncated cell text on hover."""
        app  = self.app
        item = self._tree.identify_row(event.y)
        col  = self._tree.identify_column(event.x)
        if not item or not col:
            self._hide_tooltip()
            return
        # Same cell as before — no need to update
        if item == self._tooltip_item and col == self._tooltip_col:
            return
        self._tooltip_item = item
        self._tooltip_col  = col
        self._hide_tooltip()
        # Get cell text
        if col == '#0':
            text = self._tree.item(item, 'text') or ''
        else:
            col_names = {'#1': 0, '#2': 1, '#3': 2, '#4': 3, '#5': 4}
            idx = col_names.get(col)
            if idx is None:
                return
            vals = self._tree.item(item, 'values')
            text = vals[idx] if vals and idx < len(vals) else ''
        if not text:
            return
        # Measure text width vs column width
        try:
            from tkinter.font import Font
            font     = Font(font=app.MONO)
            text_w   = font.measure(str(text))
            col_w    = self._tree.column(col, 'width')
            if text_w <= col_w - 8:  # 8px padding buffer
                return
        except Exception:
            return
        # Show tooltip
        x = self._tree.winfo_rootx() + event.x + 12
        y = self._tree.winfo_rooty() + event.y + 16
        self._tooltip_win = tw = tk.Toplevel(app)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=app.BG4)
        tk.Label(tw, text=str(text), font=app.MONO, bg=app.BG4, fg=app.FG,
                 padx=8, pady=4, wraplength=600, justify='left').pack()

    def _hide_tooltip(self, event=None):
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win  = None
        self._tooltip_item = None
        self._tooltip_col  = None

    def _expand_all(self):
        for item in self._tree.get_children():
            self._tree.item(item, open=True)

    def _collapse_all(self):
        for item in self._tree.get_children():
            self._tree.item(item, open=False)

    # ── Summary popup ──────────────────────────────────────────────────────────
    def _show_summary_popup(self, item, summary, ex, ey):
        app    = self.app
        acc    = self._tree.item(item, 'text').strip()
        from datetime import datetime as _dt2
        def _fmt(ds):
            try: return _dt2.strptime(ds, '%Y-%m-%d').strftime('%m/%d/%y')
            except Exception: return ds
        period = ('Last 24h' if not self._filter_date else
                  (_fmt(self._filter_date[0]) if self._filter_date[0] == self._filter_date[1]
                   else f"{_fmt(self._filter_date[0])} → {_fmt(self._filter_date[1])}"))
        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title(f"Summary — {acc}")
        popup.resizable(False, False)
        popup.transient(app)
        try:
            popup.geometry(f"+{self._tree.winfo_rootx()+ex}+{self._tree.winfo_rooty()+ey+20}")
        except Exception:
            pass
        tk.Label(popup, text=f"  {acc}", font=app.MONOB, bg=app.BG2, fg=app.ACC,
                 padx=12, pady=8).pack(fill='x')
        tk.Label(popup, text=f"  Period: {period}", font=app.MONO, bg=app.BG2, fg=app.FG2,
                 padx=12).pack(fill='x')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        for part in summary.split('  │  '):
            if ':' in part:
                label, val = part.split(':', 1)
                row = tk.Frame(popup, bg=app.BG2)
                row.pack(fill='x', padx=16, pady=2)
                tk.Label(row, text=label.strip(), font=app.MONO, bg=app.BG2, fg=app.FG2,
                         width=10, anchor='w').pack(side='left')
                tk.Label(row, text=val.strip(), font=app.MONOB, bg=app.BG2,
                         fg=app.ACC).pack(side='left')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x', pady=(4, 0))
        tk.Button(popup, text="Close", font=app.MONO, bg=app.BG3, fg=app.FG2,
                  relief='flat', padx=12, pady=4, cursor='hand2',
                  command=popup.destroy).pack(pady=8)

    # ── Date picker ────────────────────────────────────────────────────────────
    def _toggle_date_picker(self):
        # Guard: if popup already open, raise it
        if hasattr(self, '_date_popup') and self._date_popup and self._date_popup.winfo_exists():
            self._date_popup.lift()
            self._date_popup.focus_force()
            return

        from datetime import date as _date, datetime as _dt
        app   = self.app
        today = _date.today()

        # Pre-populate with active filter or today
        if self._filter_date:
            try:
                d_from = _dt.strptime(self._filter_date[0], '%Y-%m-%d').date()
                d_to   = _dt.strptime(self._filter_date[1], '%Y-%m-%d').date()
            except Exception:
                d_from = d_to = today
        else:
            d_from = d_to = today

        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title("Filter by Date")
        popup.resizable(False, False)
        popup.transient(app)
        self._date_popup = popup

        # Position below the Filter Date button
        try:
            bx = self._date_btn.winfo_rootx()
            by = self._date_btn.winfo_rooty() + self._date_btn.winfo_height() + 4
            popup.geometry(f"+{bx}+{by}")
        except Exception:
            pass

        tk.Label(popup, text="Select date range (max 7 days)", font=app.MONOB,
                 bg=app.BG2, fg=app.ACC).pack(padx=14, pady=(10, 2))
        tk.Label(popup, text="Format: MM/DD/YY", font=app.MONO,
                 bg=app.BG2, fg=app.FG2).pack(padx=14, pady=(0, 6))

        def _make_entry_row(parent, label, init_date):
            row = tk.Frame(parent, bg=app.BG2)
            row.pack(fill='x', padx=14, pady=3)
            tk.Label(row, text=label, font=app.MONO, bg=app.BG2, fg=app.FG2,
                     width=7, anchor='w').pack(side='left')
            var = tk.StringVar(value=init_date.strftime('%m/%d/%y'))
            entry = tk.Entry(row, textvariable=var, font=app.MONO, bg=app.BG3,
                             fg=app.FG, insertbackground=app.ACC, relief='flat', width=10)
            entry.pack(side='left', ipady=4, padx=(4, 0))
            return var, entry

        from_var, from_entry = _make_entry_row(popup, "From:", d_from)
        to_var,   to_entry   = _make_entry_row(popup, "To:",   d_to)

        # Auto-sync To when From changes, until user edits To
        _syncing = [True]
        def _sync_to(*_):
            if _syncing[0]:
                to_var.set(from_var.get())
        def _unsync(*_):
            _syncing[0] = False
        from_var.trace_add('write', _sync_to)
        to_entry.bind('<Key>', _unsync)

        err_lbl = tk.Label(popup, text="", font=app.MONO, bg=app.BG2, fg=app.RED)
        err_lbl.pack(pady=(4, 0))

        def _parse(s):
            for fmt in ('%m/%d/%y', '%m/%d/%Y', '%m-%d-%y', '%m-%d-%Y'):
                try:
                    return _dt.strptime(s.strip(), fmt).date()
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
            self._date_lbl.config(text=f"  \U0001f4c5 {lbl}")
            self._date_btn.config(fg=app.YEL)
            popup.destroy()
            self.load()

        def _clear():
            self._filter_date = None
            self._date_lbl.config(text="")
            self._date_btn.config(fg=app.FG2)
            popup.destroy()
            self.load()

        bf = tk.Frame(popup, bg=app.BG2); bf.pack(pady=(4, 12))
        tk.Button(bf, text="Apply", font=app.MONO, bg=app.ACC, fg=app.BG,
            relief='flat', padx=12, pady=4, cursor='hand2', command=_apply).pack(side='left', padx=6)
        tk.Button(bf, text="Clear / Show 24h", font=app.MONO, bg=app.BG4, fg=app.FG2,
            relief='flat', padx=12, pady=4, cursor='hand2', command=_clear).pack(side='left', padx=6)
        from_entry.focus_set()
