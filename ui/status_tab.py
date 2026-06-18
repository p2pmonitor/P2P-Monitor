"""ui/status_tab.py — Status tab for P2P Monitor"""
import threading
import tkinter as tk
from tkinter import ttk


class StatusTab:
    def __init__(self, app, parent_frame):
        self.app = app
        self._refresh_in_flight = False  # prevents thread accumulation
        self._build(parent_frame)
        self._tick_uptime()  # start lightweight minute ticker

    # ── Lightweight uptime tick — pure math, no I/O ────────────────────────────
    def _tick_uptime(self):
        """Every 60 seconds recalculate uptime/break columns from cached state.
        No threads, no watcher calls — just arithmetic on already-known timestamps."""
        if self.app.watcher:
            try:
                rows = self.app.watcher.get_uptime_rows()
                self.app.after(0, lambda: self._update_uptime_cols(rows))
            except Exception:
                pass
        self.app.after(60000, self._tick_uptime)

    def _update_uptime_cols(self, rows):
        """Update only uptime and break_time columns without rebuilding the tree."""
        app = self.app
        # Build lookup by account name → tree item id
        items = {app._st_tree.item(i, 'values')[0]: i
                 for i in app._st_tree.get_children()
                 if app._st_tree.item(i, 'values')}
        for r in rows:
            iid = items.get(r['account'])
            if iid:
                vals = list(app._st_tree.item(iid, 'values'))
                if len(vals) >= 5:
                    vals[3] = r['uptime']
                    vals[4] = r['break_time']
                    app._st_tree.item(iid, values=vals)

    # ── Push-based full refresh — called by watcher events and manual refresh ──
    def refresh(self):
        """Full refresh — checks active sessions then rebuilds the tree."""
        app = self.app
        if not app.watcher:
            return
        def _do():
            try:
                app.watcher.check_active_sessions()
            except Exception:
                pass
            rows = app.watcher.get_account_rows()
            app.after(0, lambda: self._update_tree(rows))
        threading.Thread(target=_do, daemon=True).start()

    def push_refresh(self):
        """Lightweight push from watcher events — no check_active_sessions.
        Guarded by _refresh_in_flight to prevent thread accumulation on Windows
        where each thread takes long enough that multiple can pile up."""
        app = self.app
        if not app.watcher:
            return
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        def _do():
            try:
                rows = app.watcher.get_account_rows()
                app.after(0, lambda: self._update_tree(rows))
            finally:
                self._refresh_in_flight = False
        threading.Thread(target=_do, daemon=True).start()

    def on_tab_shown(self):
        """Called when status tab is selected — full refresh."""
        self.refresh()

    def _build(self, f):
        app = self.app

        hdr = tk.Frame(f, bg=app.BG2, padx=12, pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text="Per-Account Live Status", font=app.MONOL,
                 bg=app.BG2, fg=app.ACC).pack(side='left')
        tk.Button(hdr, text="↻ Refresh", font=app.MONO, bg=app.BG3, fg=app.ACC,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self.refresh).pack(side='right')

        cols = ('account', 'task', 'activity', 'uptime', 'break_time', 'status', 'mute', 'screenshot')
        app._st_tree = ttk.Treeview(f, columns=cols, show='headings', height=22)
        for col, w, lbl in [
            ('account',    160, 'Account'),
            ('task',       160, 'Task'),
            ('activity',   160, 'Activity'),
            ('uptime',      90, 'Uptime'),
            ('break_time',  90, 'Break Time'),
            ('status',     120, 'Status'),
            ('mute',        80, 'Mute'),
            ('screenshot',  90, 'Screenshot'),
        ]:
            app._st_tree.heading(col, text=lbl)
            app._st_tree.column(col, width=w, minwidth=w if col == 'account' else 40, anchor='w')

        scr = ttk.Scrollbar(f, orient='vertical', command=app._st_tree.yview)
        app._st_tree.configure(yscrollcommand=scr.set)
        scr.pack(side='right', fill='y')
        app._st_tree.pack(fill='both', expand=True)

        app._st_tree.tag_configure('ok',     foreground=app.GREEN)
        app._st_tree.tag_configure('quiet',  foreground=app.YEL)
        app._st_tree.tag_configure('silent', foreground=app.RED)
        app._st_tree.tag_configure('break',  foreground=app.FG2)

        app._st_tree.bind('<Button-1>', self._on_click)
        app._st_tree.bind('<Double-1>', self._on_double_click)

        tk.Label(f,
            text="Click Mute to silence  |  Click Screenshot for on-demand  |  Double-click account name → History",
            font=app.MONO, bg=app.BG2, fg=app.FG2).pack(pady=4)

    def _update_tree(self, rows):
        """Update status tree in place — only rebuild if accounts changed.
        Avoids full delete+insert on every event which is expensive on Windows."""
        app  = self.app
        tree = app._st_tree
        # Deselect any selected row — no persistent highlight
        tree.selection_remove(tree.selection())
        # Build current state
        existing = {tree.item(i, 'values')[0]: i
                    for i in tree.get_children()
                    if tree.item(i, 'values')}
        new_accounts = [r['account'] for r in rows]
        # If account set changed, full rebuild is unavoidable
        if set(existing.keys()) != set(new_accounts):
            for item in tree.get_children():
                tree.delete(item)
            existing = {}
        for r in rows:
            s        = r['status']
            tag      = 'silent' if '🔴' in s else ('quiet' if '🟡' in s else 'ok')
            mute_lbl = '[ Unmute ]' if r.get('muted') else '[  Mute  ]'
            vals     = (r['account'], r['task'], r['activity'],
                        r.get('uptime', '—'), r.get('break_time', '—'),
                        r['status'], mute_lbl, '[Screenshot]')
            if r['account'] in existing:
                # Update in place — no delete/insert
                tree.item(existing[r['account']], values=vals, tags=(tag,))
            else:
                tree.insert('', 'end', values=vals, tags=(tag,))

    def _get_tree_account(self, event, required_col):
        """Return (account, item) tuple if event is a cell click on required_col, else None."""
        app = self.app
        if app._st_tree.identify_region(event.x, event.y) != 'cell':
            return None
        item = app._st_tree.identify_row(event.y)
        if not item:
            return None
        return app._st_tree.item(item, 'values')[0], item

    def _on_click(self, event):
        app  = self.app
        # Deselect immediately on any click
        app._st_tree.selection_remove(app._st_tree.selection())
        col = app._st_tree.identify_column(event.x)
        if col == '#7':  # Mute column
            result = self._get_tree_account(event, '#7')
            if not result: return
            account, item = result
            app.watcher.toggle_mute(account)
            self._flash_row(item)
            self.refresh()
        elif col == '#8':  # Screenshot column
            result = self._get_tree_account(event, '#8')
            if not result: return
            account, item = result
            app.watcher.trigger_screenshot(account)
            self._flash_row(item)
            self.refresh()

    def _on_double_click(self, event):
        app    = self.app
        result = self._get_tree_account(event, '#1')
        if not result:
            return
        account, _ = result
        app.show_tab('History')
        app.after(50, lambda: app._history.focus_account(account))

    def _flash_row(self, item):
        app = self.app
        try:
            app._st_tree.tag_configure('flash', background=app.ACC, foreground=app.BG)
            app._st_tree.item(item, tags=('flash',))
            app.after(250, lambda: self._restore_tag(item))
        except Exception:
            pass

    def _restore_tag(self, item):
        app = self.app
        try:
            vals   = app._st_tree.item(item, 'values')
            if not vals:
                return
            status = vals[5] if len(vals) > 5 else ''
            tag    = 'silent' if '🔴' in status else ('quiet' if '🟡' in status else 'ok')
            app._st_tree.item(item, tags=(tag,))
        except Exception:
            pass
