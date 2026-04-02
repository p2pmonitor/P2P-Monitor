"""ui/status_tab.py — Status tab for P2P Monitor"""
import threading
import tkinter as tk
from tkinter import ttk


class StatusTab:
    def __init__(self, app, parent_frame):
        self.app = app
        self._build(parent_frame)

    def _build(self, f):
        app = self.app

        hdr = tk.Frame(f, bg=app.BG2, padx=12, pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text="Per-Account Live Status", font=app.MONOL,
                 bg=app.BG2, fg=app.ACC).pack(side='left')
        tk.Button(hdr, text="↻ Refresh", font=app.MONO, bg=app.BG3, fg=app.ACC,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self.refresh).pack(side='right')

        cols = ('account', 'task', 'activity', 'uptime', 'break_time', 'status', 'action')
        app._st_tree = ttk.Treeview(f, columns=cols, show='headings', height=22)
        for col, w, lbl in [
            ('account',    160, 'Account'),
            ('task',       160, 'Task'),
            ('activity',   160, 'Activity'),
            ('uptime',      90, 'Uptime'),
            ('break_time',  90, 'Break Time'),
            ('status',     120, 'Status'),
            ('action',     180, 'Action'),
        ]:
            app._st_tree.heading(col, text=lbl)
            app._st_tree.column(col, width=w, minwidth=w if col == 'action' else 40, anchor='w')

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

    def refresh(self):
        app = self.app
        if not app.watcher:
            return
        def _do():
            rows = app.watcher.get_account_rows()
            app.after(0, lambda: self._update_tree(rows))
        threading.Thread(target=_do, daemon=True).start()

    def _update_tree(self, rows):
        app = self.app
        for item in app._st_tree.get_children():
            app._st_tree.delete(item)
        for r in rows:
            s   = r['status']
            tag = 'silent' if '🔴' in s else ('quiet' if '🟡' in s else 'ok')
            mute_lbl = '[ Unmute ]' if r.get('muted') else '[  Mute  ]'
            app._st_tree.insert('', 'end',
                values=(r['account'], r['task'], r['activity'],
                        r.get('uptime', '—'), r.get('break_time', '—'),
                        r['status'], f"{mute_lbl}  [Screenshot]"),
                tags=(tag,))

    def _get_tree_account(self, event, required_col):
        """Return (account, item) tuple if event is a cell click on required_col, else None."""
        app = self.app
        if app._st_tree.identify_region(event.x, event.y) != 'cell':
            return None
        if app._st_tree.identify_column(event.x) != required_col:
            return None
        item = app._st_tree.identify_row(event.y)
        if not item:
            return None
        return app._st_tree.item(item, 'values')[0], item

    def _on_click(self, event):
        app    = self.app
        result = self._get_tree_account(event, '#7')
        if not result:
            return
        account, item = result
        col_bbox = app._st_tree.bbox(item, '#7')
        if col_bbox:
            rel_x  = event.x - col_bbox[0]
            cell_w = col_bbox[2]
            if rel_x < cell_w * 0.5:
                app.watcher.toggle_mute(account)
                self._flash_row(item)
                self.refresh()
                return
        app.watcher.trigger_screenshot(account)
        self._flash_row(item)
        self.refresh()

    def _on_double_click(self, event):
        app    = self.app
        result = self._get_tree_account(event, '#1')
        if not result:
            return
        account, _ = result
        app._nb.select(2)
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
