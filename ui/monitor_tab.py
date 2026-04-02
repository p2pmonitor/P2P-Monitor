"""ui/monitor_tab.py — Monitor tab for P2P Monitor"""
import tkinter as tk
from tkinter import ttk


class MonitorTab:
    def __init__(self, app, parent_frame):
        self.app = app
        self._build(parent_frame)

    def _build(self, f):
        app = self.app

        # ── Controls row ──────────────────────────────────────────────────────
        ctrl = tk.Frame(f, bg=app.BG2, padx=12, pady=8)
        ctrl.pack(fill='x')
        app._btn_start = tk.Button(ctrl, text="▶  START", font=app.MONOL,
            bg=app.GREEN, fg=app.BG, relief='flat', padx=16, pady=6,
            cursor='hand2', command=app._start)
        app._btn_start.pack(side='left', padx=(0, 6))
        app._btn_stop = tk.Button(ctrl, text="■  STOP", font=app.MONOL,
            bg=app.BG3, fg=app.FG2, relief='flat', padx=16, pady=6,
            cursor='hand2', command=app._stop, state='disabled')
        app._btn_stop.pack(side='left', padx=(0, 12))
        tk.Frame(ctrl, bg=app.FG2, width=1, height=24).pack(side='left', padx=8)

        # ── Session counters ──────────────────────────────────────────────────
        sb = tk.Frame(f, bg=app.BG3, padx=12, pady=4)
        sb.pack(fill='x')
        app._sv = {}
        for label, key, color in [
            ("QUESTS",  "quest",   '#88ffbb'),
            ("TASKS",   "task",    app.ACC),
            ("CHATS",   "chat",    app.YEL),
            ("ERRORS",  "error",   app.RED),
            ("DROPS",   "drop",    '#00ff88'),
            ("DEATHS",  "death",   app.RED),
            ("LEVELS",  "levelup", '#00ff88'),
        ]:
            if app._sv:
                tk.Label(sb, text=" │ ", bg=app.BG3, fg=app.FG2, font=app.MONO).pack(side='left')
            fr = tk.Frame(sb, bg=app.BG3)
            fr.pack(side='left')
            tk.Label(fr, text=label + ": ", bg=app.BG3, fg=app.FG2, font=app.MONO).pack(side='left')
            var = tk.StringVar(value='0')
            tk.Label(fr, textvariable=var, bg=app.BG3, fg=color, font=app.MONOB).pack(side='left')
            app._sv[key] = var

        # ── Log text area ─────────────────────────────────────────────────────
        lf = tk.Frame(f, bg=app.BG)
        lf.pack(fill='both', expand=True)
        app._log_text = tk.Text(lf, bg=app.BG, fg=app.FG, font=app.MONO, relief='flat',
            wrap='word', state='disabled', insertbackground=app.ACC,
            selectbackground=app.BG3, padx=12, pady=8, spacing1=2)
        scr = ttk.Scrollbar(lf, command=app._log_text.yview)
        scr.pack(side='right', fill='y')
        app._log_text.pack(fill='both', expand=True)
        app._log_text.configure(yscrollcommand=scr.set)
        for tag, col in [
            ('info',            app.FG2),
            ('ts',              app.FG2),
            ('warn',            app.ACC2),
            ('ok',              '#88ffbb'),
            ('quest',           '#88ffbb'),
            ('task',            app.ACC),
            ('chat',            app.YEL),
            ('error',           app.RED),
            ('drop',            '#00ff88'),
            ('death',           app.RED),
            ('levelup',         '#00ff88'),
            ('slayer_complete',  '#88ffbb'),
            ('slayer_skip',     app.RED),
            ('script_event',    '#ff8888'),
        ]:
            app._log_text.tag_configure(tag, foreground=col)

        # ── Bottom bar ────────────────────────────────────────────────────────
        bf = tk.Frame(f, bg=app.BG, pady=4, padx=12)
        bf.pack(fill='x')
        tk.Button(bf, text="Clear Log", font=app.MONO, bg=app.BG3, fg=app.FG2,
            relief='flat', padx=8, pady=3, cursor='hand2',
            command=self._clear_log).pack(side='right')

    def _clear_log(self):
        t = self.app._log_text
        t.configure(state='normal')
        t.delete('1.0', 'end')
        t.configure(state='disabled')
