"""
settings_tab.py — Settings tab for P2P Monitor
Merged EVENT NOTIFICATIONS table: event row + Notify checkbox + Screenshot checkbox.
Script Events: Notify column only — no Screenshot column, no hide paint entry.
"""

import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from py.discord import post_discord, bot_setup_discord, _embed
from py.util    import now_str
from py.config  import save_config


class SettingsTab:
    """Settings tab. Receives App reference for shared cfg, colours, fonts, watcher."""

    def __init__(self, app, parent_frame):
        self.app = app
        self._vars = {}   # key -> tk variable
        self._build(parent_frame)
        self.load_fields()

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self, f):
        app = self.app
        canvas = tk.Canvas(f, bg=app.BG2, highlightthickness=0)
        sb = ttk.Scrollbar(f, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = tk.Frame(canvas, bg=app.BG2)
        win   = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _update_scroll(e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _update_scroll)
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))
        def _on_enter(_):
            canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-1*(e.delta//120), 'units'))
            canvas.bind_all('<Button-4>',   lambda e: canvas.yview_scroll(-1, 'units'))
            canvas.bind_all('<Button-5>',   lambda e: canvas.yview_scroll(1,  'units'))
        def _on_leave(_):
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')
        canvas.bind('<Enter>', _on_enter)
        canvas.bind('<Leave>', _on_leave)


        def section(title):
            tk.Frame(inner, bg=app.ACC, height=1).pack(fill='x', padx=16, pady=(16, 0))
            row = tk.Frame(inner, bg=app.BG2); row.pack(fill='x', padx=16, pady=(4, 2))
            tk.Label(row, text=title, font=app.MONOB, bg=app.BG2, fg=app.ACC).pack(side='left')

        def field(lbl, attr, pw=False, parent=None, padx=16):
            p   = parent or inner
            row = tk.Frame(p, bg=app.BG2); row.pack(fill='x', padx=padx, pady=2)
            tk.Label(row, text=lbl, font=app.MONO, bg=app.BG2, fg=app.FG2,
                     width=28, anchor='w').pack(side='left')
            var = tk.StringVar(value=app.cfg.get(attr, ''))
            kw  = {'show': '•'} if pw else {}
            tk.Entry(row, textvariable=var, font=app.MONO, bg=app.BG3, fg=app.FG,
                     relief='flat', insertbackground=app.ACC,
                     **kw).pack(side='left', fill='x', expand=True, ipady=4, padx=(4, 0))
            self._vars[attr] = var

        def intfield(lbl, attr, lo, hi):
            row = tk.Frame(inner, bg=app.BG2); row.pack(fill='x', padx=16, pady=2)
            tk.Label(row, text=lbl, font=app.MONO, bg=app.BG2, fg=app.FG2,
                     width=28, anchor='w').pack(side='left')
            default = app.cfg.get(attr, lo)
            var = tk.IntVar(value=int(default))
            tk.Spinbox(row, from_=lo, to=hi, textvariable=var, width=8, font=app.MONO,
                       bg=app.BG3, fg=app.FG, buttonbackground=app.BG4,
                       relief='flat').pack(side='left', padx=(4, 0))
            self._vars[attr] = var

        def boolfield(lbl, attr, default=False, parent=None):
            p = parent or inner
            row = tk.Frame(p, bg=app.BG2); row.pack(fill='x', padx=16, pady=2)
            var = tk.BooleanVar(value=bool(app.cfg.get(attr, default)))
            tk.Checkbutton(row, text=lbl, variable=var, font=app.MONO,
                bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.ACC,
                selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left')
            self._vars[attr] = var

        def collapsible(title, cfg_key):
            holder = tk.Frame(inner, bg=app.BG2); holder.pack(fill='x', padx=0, pady=(8, 0))
            hdr    = tk.Frame(holder, bg=app.BG3); hdr.pack(fill='x')
            is_open = tk.BooleanVar(value=bool(app.cfg.get(cfg_key, True)))
            body    = tk.Frame(holder, bg=app.BG2)
            arrow   = '▼' if is_open.get() else '▶'
            btn = tk.Button(hdr, text=f"{arrow}  {title}",
                font=app.MONOB, bg=app.BG3, fg=app.ACC, relief='flat',
                padx=10, pady=6, cursor='hand2', anchor='w')
            btn.pack(side='left', fill='x', expand=True)
            def _toggle(b=btn, v=is_open, bdy=body, t=title, ck=cfg_key):
                if v.get():
                    bdy.pack_forget(); v.set(False); b.configure(text=f"▶  {t}"); app.cfg[ck] = False
                else:
                    bdy.pack(fill='x', padx=8, pady=(4, 4)); v.set(True); b.configure(text=f"▼  {t}"); app.cfg[ck] = True
            btn.configure(command=_toggle)
            if is_open.get():
                body.pack(fill='x', padx=8, pady=(4, 4))
            return holder, body, _toggle

        # ── Logs folder ───────────────────────────────────────────────────────
        section("DREAMBOT LOGS FOLDER")
        dr = tk.Frame(inner, bg=app.BG2); dr.pack(fill='x', padx=16, pady=4)
        self._vars['logs_root'] = tk.StringVar(value=app.cfg.get('logs_root', ''))
        tk.Entry(dr, textvariable=self._vars['logs_root'], font=app.MONO, bg=app.BG3, fg=app.FG,
            relief='flat', insertbackground=app.ACC).pack(side='left', fill='x', expand=True, ipady=4)
        tk.Button(dr, text="Browse", font=app.MONO, bg=app.BG3, fg=app.ACC,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self._browse_dir).pack(side='left', padx=(6, 0))

        # ── Discord Alerts ────────────────────────────────────────────────────
        _, discord_body, _ = collapsible("DISCORD ALERTS", 'ui_section_discord_open')

        field("Bot Token:",         'bot_token',    pw=True,  parent=discord_body)
        field("Server ID:",         'bot_server_id',          parent=discord_body)
        field("Discord Mention ID:", 'mention_id',             parent=discord_body)

        setup_row = tk.Frame(discord_body, bg=app.BG2); setup_row.pack(fill='x', padx=8, pady=(6, 2))
        self._bot_setup_lbl = tk.Label(setup_row, text="", font=app.MONO, bg=app.BG2, fg=app.FG2,
                                       wraplength=500, justify='left')
        self._bot_setup_lbl.pack(side='left', padx=(0, 12), fill='x', expand=True)
        tk.Button(setup_row, text="🤖  Run Bot Setup", font=app.MONO, bg=app.BG3, fg=app.ACC,
            relief='flat', padx=12, pady=5, cursor='hand2',
            command=self._manual_bot_setup).pack(side='right')

        if app.cfg.get('bot_setup_done'):
            ch_ids = app.cfg.get('bot_channel_ids', {})
            th_ids = app.cfg.get('bot_thread_ids', {})
            self._bot_setup_lbl.configure(
                text=f"✅ Setup complete — {len(ch_ids)} channels, {len(th_ids)} account(s) ready.",
                fg=app.GREEN)

        # Bot instructions (collapsible sub-section)
        self._inst_expanded = tk.BooleanVar(value=False)
        inst_holder = tk.Frame(discord_body, bg=app.BG2); inst_holder.pack(fill='x', padx=8, pady=(8, 0))
        inst_hdr = tk.Frame(inst_holder, bg=app.BG3); inst_hdr.pack(fill='x')
        inst_inner = tk.Frame(inst_holder, bg=app.BG2)

        def toggle_instructions():
            if self._inst_expanded.get():
                inst_inner.pack_forget(); self._inst_expanded.set(False)
                inst_btn.configure(text="▶  Bot Setup Instructions & Permissions")
            else:
                inst_inner.pack(fill='x', padx=8, pady=(4, 0)); self._inst_expanded.set(True)
                inst_btn.configure(text="▼  Bot Setup Instructions & Permissions")

        inst_btn = tk.Button(inst_hdr, text="▶  Bot Setup Instructions & Permissions",
            font=app.MONOB, bg=app.BG3, fg=app.ACC2, relief='flat',
            padx=8, pady=5, cursor='hand2', anchor='w', command=toggle_instructions)
        inst_btn.pack(side='left', fill='x', expand=True)
        tk.Label(inst_inner,
            text="  Setup:\n"
                 "  1. discord.com/developers → New Application → Bot → Reset Token → copy it → paste in Bot Token above\n"
                 "  2. Privileged Gateway Intents → enable MESSAGE CONTENT INTENT\n"
                 "  3. OAuth2 → URL Generator → Scope: bot → Permissions: Send Messages,\n"
                 "     Read Message History, Manage Channels, Manage Webhooks,\n"
                 "     View Channels, Embed Links, Attach Files, Create Public Threads,\n"
                 "     Send Messages in Threads, Manage Threads, Use Slash Commands\n"
                 "  4. Copy the generated URL → open in browser → select server → Authorize\n"
                 "  5. Right-click your server icon → Copy Server ID → paste above\n"
                 "  6. Right-click your name in Discord → Copy User ID → paste in Discord Mention ID above\n"
                 "  7. Hit Save then '🤖 Run Bot Setup'\n\n"
                 "  Slash commands (registered automatically on first run):\n"
                 "    /ss [account]                    — screenshot(s) → account monitor thread\n"
                 "    /s                               — status of all accounts → #monitor channel\n"
                 "    /force <account> <action> [amt]  — force a skill, action, or time adjustment;\n"
                 "                                       amt (1-20) only applies to -10m / +10m\n"
                 "  Tip: /ss and /force inside an account thread target that account only.",
            font=app.MONO, bg=app.BG2, fg=app.FG2, justify='left').pack(anchor='w', padx=8, pady=(4, 8))

        # Webhooks (collapsible)
        wh_frame_holder = tk.Frame(discord_body, bg=app.BG2); wh_frame_holder.pack(fill='x', padx=8, pady=(8, 0))
        self._wh_expanded = tk.BooleanVar(value=False)
        wh_hdr = tk.Frame(wh_frame_holder, bg=app.BG3); wh_hdr.pack(fill='x')
        wh_inner = tk.Frame(wh_frame_holder, bg=app.BG2)

        def toggle_webhooks():
            if self._wh_expanded.get():
                wh_inner.pack_forget(); self._wh_expanded.set(False)
                wh_btn.configure(text="▶  Webhooks  (optional fallback)")
            else:
                wh_inner.pack(fill='x'); self._wh_expanded.set(True)
                wh_btn.configure(text="▼  Webhooks  (optional fallback)")

        wh_btn = tk.Button(wh_hdr, text="▶  Webhooks  (optional fallback)",
            font=app.MONOB, bg=app.BG3, fg=app.ACC2, relief='flat',
            padx=8, pady=5, cursor='hand2', anchor='w', command=toggle_webhooks)
        wh_btn.pack(side='left', fill='x', expand=True)

        tk.Frame(wh_inner, bg=app.BG2, height=6).pack()
        tk.Label(wh_inner,
            text="  Default Webhook is the main monitor channel. If no other webhooks are\n"
                 "  filled in, all events will be sent to the Default Webhook.",
            font=app.MONO, bg=app.BG2, fg=app.FG2, justify='left').pack(fill='x', pady=(0, 4))
        field("Default Webhook:",  'webhook_default',  parent=wh_inner, padx=0)
        field("Quest Webhook:",    'webhook_quest',    parent=wh_inner, padx=0)
        field("Task Webhook:",     'webhook_task',     parent=wh_inner, padx=0)
        field("Chat Webhook:",     'webhook_chat',     parent=wh_inner, padx=0)
        field("Error Webhook:",    'webhook_error',    parent=wh_inner, padx=0)
        field("Drops Webhook:",    'webhook_drops',    parent=wh_inner, padx=0)
        field("Deaths Webhook:",   'webhook_deaths',   parent=wh_inner, padx=0)
        field("Level Up Webhook:", 'webhook_levelup',  parent=wh_inner, padx=0)
        wh_btn_row = tk.Frame(wh_inner, bg=app.BG2); wh_btn_row.pack(fill='x', pady=(4, 0))
        tk.Button(wh_btn_row, text="🔔  TEST WEBHOOKS", font=app.MONO,
            bg=app.BG3, fg=app.ACC, relief='flat', padx=12, pady=5,
            cursor='hand2', command=self._test_webhooks).pack(side='right')

        # ── EVENT NOTIFICATIONS ───────────────────────────────────────────────
        _, notif_body, _ = collapsible("EVENT NOTIFICATIONS", 'ui_section_notifications_open')

        tk.Label(notif_body,
            text="  Toggle Discord notifications and screenshots per event type.",
            font=app.MONO, bg=app.BG2, fg=app.FG2, justify='left').pack(anchor='w', padx=8, pady=(4, 6))

        # Script Events — inline checkboxes at top
        script_row = tk.Frame(notif_body, bg=app.BG2); script_row.pack(fill='x', padx=8, pady=(0, 8))
        tk.Label(script_row, text="Script Events - Notify when Script:", font=app.MONO,
                 bg=app.BG2, fg=app.FG2).pack(side='left', padx=(0, 6))
        for ev_lbl, ev_attr in [
            ("Starts",  'monitor_script_start'),
            ("Pauses",  'monitor_script_pause'),
            ("Resumes", 'monitor_script_resume'),
            ("Stops",   'monitor_script_stop'),
        ]:
            v = tk.BooleanVar(value=bool(app.cfg.get(ev_attr, True)))
            tk.Checkbutton(script_row, text=ev_lbl, variable=v, font=app.MONO,
                bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.ACC,
                selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left', padx=(0, 8))
            self._vars[ev_attr] = v

        # Grid table — col 0: label, col 1: Notify cb, col 2: Screenshot cb
        tbl = tk.Frame(notif_body, bg=app.BG2)
        tbl.pack(anchor='w', padx=16, pady=(0, 4))
        tbl.columnconfigure(0, minsize=140)
        tbl.columnconfigure(1, minsize=80)
        tbl.columnconfigure(2, minsize=100)

        # Header row
        tk.Label(tbl, text="",           font=app.MONOB, bg=app.BG2, fg=app.ACC).grid(row=0, column=0, sticky='w')
        tk.Label(tbl, text="Notify",     font=app.MONOB, bg=app.BG2, fg=app.ACC).grid(row=0, column=1, sticky='w', padx=(8,0))
        tk.Label(tbl, text="Screenshot", font=app.MONOB, bg=app.BG2, fg=app.ACC).grid(row=0, column=2, sticky='w', padx=(8,0))

        EVENT_ROWS = [
            ("Quests",    'monitor_quests',   'ss_event_quest'),
            ("Tasks",     'monitor_tasks',    'ss_event_task'),
            ("Chat",      'monitor_chat',     'ss_event_chat'),
            ("Errors",    'monitor_errors',   'ss_event_error'),
            ("Drops",     'monitor_drops',    'ss_event_drops'),
            ("Deaths",    'monitor_deaths',   'ss_event_death'),
            ("Level Ups", 'monitor_levelups', 'ss_event_levelup'),
        ]
        for r, (label, notify_attr, ss_attr) in enumerate(EVENT_ROWS, start=1):
            tk.Label(tbl, text=label, font=app.MONO, bg=app.BG2, fg=app.FG,
                     anchor='w').grid(row=r, column=0, sticky='w', pady=2)

            notify_var = tk.BooleanVar(value=bool(app.cfg.get(notify_attr, True)))
            tk.Checkbutton(tbl, variable=notify_var, font=app.MONO,
                bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.FG,
                selectcolor=app.BG2, relief='flat', cursor='hand2').grid(row=r, column=1, padx=(16, 0), pady=2)
            self._vars[notify_attr] = notify_var

            ss_var = tk.BooleanVar(value=bool(app.cfg.get(ss_attr, False)))
            tk.Checkbutton(tbl, variable=ss_var, font=app.MONO,
                bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.FG,
                selectcolor=app.BG2, relief='flat', cursor='hand2').grid(row=r, column=2, padx=(16, 0), pady=2)
            self._vars[ss_attr] = ss_var

        # Level every-N row
        lvl_row = tk.Frame(notif_body, bg=app.BG2); lvl_row.pack(fill='x', padx=8, pady=(2, 8))
        tk.Label(lvl_row, text="    Notify every N levels:", font=app.MONO,
                 bg=app.BG2, fg=app.FG2, width=28, anchor='w').pack(side='left')
        lev_var = tk.IntVar(value=int(app.cfg.get('levelup_every', 5)))
        tk.Spinbox(lvl_row, from_=1, to=99, textvariable=lev_var, width=6, font=app.MONO,
                   bg=app.BG3, fg=app.FG, buttonbackground=app.BG4, relief='flat').pack(side='left', padx=(4, 0))
        self._vars['levelup_every'] = lev_var
        tk.Label(lvl_row, text=" levels  (total level milestones always posted)",
            font=app.MONO, bg=app.BG2, fg=app.FG2).pack(side='left', padx=(6, 0))

        # ── Hide paint overlay (end of EVENT NOTIFICATIONS) ─────────────────
        tk.Frame(notif_body, bg=app.BG4, height=1).pack(fill='x', padx=8, pady=(6, 4))
        hp_hdr_row = tk.Frame(notif_body, bg=app.BG2); hp_hdr_row.pack(fill='x', padx=8, pady=(4, 4))
        tk.Label(hp_hdr_row, text="  Hide paint overlay during screenshot:",
            font=app.MONO, bg=app.BG2, fg=app.FG2).pack(side='left')
        hp_table = tk.Frame(notif_body, bg=app.BG3, padx=10, pady=8)
        hp_table.pack(fill='x', padx=8, pady=(0, 8))
        hp_entries = [
            ("Scheduled",  'ss_hide_paint_scheduled'),
            ("Task",       'ss_hide_paint_task'),
            ("Quest",      'ss_hide_paint_quest'),
            ("Chat",       'ss_hide_paint_chat'),
            ("Error",      'ss_hide_paint_error'),
            ("Drops",      'ss_hide_paint_drops'),
            ("On-demand",  'ss_hide_paint_ondemand'),
            ("Bot /ss",    'ss_hide_paint_botss'),
            ("Startup",    'ss_hide_paint_startup'),
            ("Death",      'ss_hide_paint_death'),
            ("Level Up",   'ss_hide_paint_levelup'),
        ]
        HP_COLS = 6
        for i, (lbl, key) in enumerate(hp_entries):
            row_i, col = divmod(i, HP_COLS)
            cell = tk.Frame(hp_table, bg=app.BG3)
            cell.grid(row=row_i, column=col, sticky='w', padx=(0, 14), pady=2)
            var = tk.BooleanVar(value=bool(app.cfg.get(key, False)))
            tk.Checkbutton(cell, text=lbl, variable=var, font=app.MONO,
                bg=app.BG3, fg=app.FG2, activebackground=app.BG3, activeforeground=app.ACC,
                selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left')
            self._vars[key] = var

        # ── Monitoring Intervals ──────────────────────────────────────────────
        section("MONITORING INTERVALS")
        intfield("Log check interval (seconds):", 'check_interval', 1, 60)

        # ── Daily Summary ─────────────────────────────────────────────────────
        section("DAILY SUMMARY")
        boolfield("Enable daily summary", 'summary_enabled')
        field("Send time (HH:MM):", 'summary_time')

        # Scheduled screenshots — now lives in Daily Summary section
        ss_row = tk.Frame(inner, bg=app.BG2); ss_row.pack(fill='x', padx=16, pady=2)
        ss_var = tk.BooleanVar(value=bool(app.cfg.get('screenshots_enabled', False)))
        tk.Checkbutton(ss_row, text="Enable scheduled screenshots", variable=ss_var, font=app.MONO,
            bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.ACC,
            selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left')
        self._vars['screenshots_enabled'] = ss_var

        int_row = tk.Frame(inner, bg=app.BG2); int_row.pack(fill='x', padx=16, pady=2)
        tk.Label(int_row, text="Screenshot interval (minutes):", font=app.MONO,
                 bg=app.BG2, fg=app.FG2, width=28, anchor='w').pack(side='left')
        int_var = tk.IntVar(value=int(app.cfg.get('screenshot_minutes', 60)))
        tk.Spinbox(int_row, from_=5, to=1440, textvariable=int_var, width=8, font=app.MONO,
                   bg=app.BG3, fg=app.FG, buttonbackground=app.BG4, relief='flat').pack(side='left', padx=(4, 0))
        self._vars['screenshot_minutes'] = int_var

        # ── Auto-update ───────────────────────────────────────────────────────
        section("AUTO-UPDATE")
        upd_row = tk.Frame(inner, bg=app.BG2); upd_row.pack(fill='x', padx=16, pady=6)
        tk.Button(upd_row, text="🔄  Check for Update", font=app.MONOL,
            bg=app.BG3, fg=app.ACC, relief='flat', padx=14, pady=6,
            cursor='hand2', command=app._check_for_update).pack(side='left')

        beta_row = tk.Frame(inner, bg=app.BG2); beta_row.pack(fill='x', padx=16, pady=(0, 4))
        beta_var = tk.BooleanVar(value=bool(app.cfg.get('beta_updates', False)))
        tk.Checkbutton(beta_row, text="Include pre-release versions when checking for updates manually",
            variable=beta_var, font=app.MONO,
            bg=app.BG2, fg=app.FG2, activebackground=app.BG2, activeforeground=app.ACC,
            selectcolor=app.BG3, relief='flat', cursor='hand2').pack(side='left')
        self._vars['beta_updates'] = beta_var
        tk.Label(beta_row, text="  (silent startup check always uses stable only)",
            font=app.MONO, bg=app.BG2, fg=app.FG2).pack(side='left')

        tk.Frame(inner, bg=app.BG2, height=8).pack()
        tk.Button(inner, text="💾  SAVE SETTINGS", font=app.MONOL,
            bg=app.ACC, fg=app.BG, relief='flat', padx=20, pady=8,
            cursor='hand2', command=self.save).pack(pady=12)
        self._saved_lbl = tk.Label(inner, text="", font=app.MONOB, bg=app.BG2, fg=app.GREEN)
        self._saved_lbl.pack()
        tk.Frame(inner, bg=app.BG2, height=20).pack()

    # ── Save ───────────────────────────────────────────────────────────────────
    def save(self):
        app = self.app
        for attr, var in self._vars.items():
            try:
                val = var.get()
                if isinstance(val, str):
                    val = val.strip()
                app.cfg[attr] = val
            except Exception:
                pass
        # Remove deprecated keys so they don't linger across upgrades
        for dead_key in ('ss_event_script', 'ss_hide_paint_script',
                         'bot_channel_id', 'bot_poll_interval',
                         '_slash_commands_deleted'):
            app.cfg.pop(dead_key, None)
        try:
            save_config(app.cfg)
        except Exception:
            pass
        self._saved_lbl.configure(text="✅ Settings saved", fg=app.GREEN)
        app.after(3000, lambda: self._saved_lbl.configure(text=""))

    def load_fields(self):
        """Sync UI widgets from app.cfg (called after cfg is loaded)."""
        for attr, var in self._vars.items():
            try:
                val = self.app.cfg.get(attr)
                if val is None:
                    continue
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(val))
                elif isinstance(var, tk.IntVar):
                    var.set(int(val))
                else:
                    var.set(str(val))
            except Exception:
                pass

    # ── Actions ────────────────────────────────────────────────────────────────
    def _browse_dir(self):
        d = filedialog.askdirectory(title="Select DreamBot log folder")
        if d:
            self._vars['logs_root'].set(d)

    def _test_webhooks(self):
        self.save()
        app     = self.app
        tested  = 0
        for key, label in [('default','Default'),('quest','Quest'),('task','Task'),
                            ('chat','Chat'),('error','Error'),('drops','Drops'),
                            ('deaths','Deaths'),('levelup','Level Up')]:
            url = app.cfg.get(f'webhook_{key}', '').strip()
            if not url:
                continue
            payload = _embed(f"P2P Monitor v{app.VERSION if hasattr(app, 'VERSION') else '5'} — {label} Webhook Test",
                             f"Webhook is working correctly ✅\n{now_str()}", [], 0x3399ff)
            ok, err = post_discord(url, payload)
            app._log(f"{'✅' if ok else '🚫'} Test {label}: {'OK' if ok else err}")
            tested += 1
        if not tested:
            app._log("⚠ No webhooks configured to test")

    def _manual_bot_setup(self):
        self.save()
        app = self.app
        token     = app.cfg.get('bot_token', '').strip()
        server_id = app.cfg.get('bot_server_id', '').strip()
        if not token:
            messagebox.showwarning("Bot Setup", "Bot Token is required.")
            return
        if not server_id:
            messagebox.showwarning(
                "Bot Setup",
                "Server ID is required for bot setup.\n\n"
                "Right-click your server icon in Discord → Copy Server ID,\n"
                "then paste it in the Server ID field and save.")
            return
        self._bot_setup_lbl.configure(text="⏳ Running setup...", fg=app.YEL)
        app.update_idletasks()

        def _do():
            # Ensure discord.py is installed before setup
            try:
                import discord  # noqa: F401
            except ImportError:
                app.after(0, lambda: self._bot_setup_lbl.configure(
                    text="⏳ Installing discord.py...", fg=app.YEL))
                try:
                    import subprocess, sys
                    subprocess.check_call(
                        [sys.executable, '-m', 'pip', 'install', 'discord.py',
                         '--break-system-packages', '--quiet'],
                        timeout=120)
                    app._log("🤖 discord.py installed")
                except Exception as e:
                    def _fail(msg=str(e)):
                        self._bot_setup_lbl.configure(
                            text=f"❌ discord.py install failed: {msg[:50]}", fg=app.RED)
                    app.after(0, _fail)
                    return

            ok, msg = (app.watcher._run_bot_setup(log_fn=app._log)
                       if hasattr(app, 'watcher') and app.watcher
                       else (False, "Monitor not running — start monitor first"))
            if not ok:
                try:
                    result = bot_setup_discord(token, server_id, log_fn=app._log)
                    app.cfg.update(result)
                    self.save()
                    ok, msg = True, "OK"
                except Exception as e:
                    msg = str(e)
            def _done():
                if ok:
                    ch_ids = app.cfg.get('bot_channel_ids', {})
                    th_ids = app.cfg.get('bot_thread_ids', {})
                    self._bot_setup_lbl.configure(
                        text=f"✅ Setup complete — {len(ch_ids)} channels, {len(th_ids)} account(s)",
                        fg=app.GREEN)
                else:
                    self._bot_setup_lbl.configure(text=f"❌ {msg[:60]}", fg=app.RED)
            app.after(0, _done)
        threading.Thread(target=_do, daemon=True).start()
