"""
ui/settings_tab.py — Settings tab for P2P Monitor (v2.0.0-beta.10 redesign)

Replaces the old single giant scrollable expand/collapse layout with a
left-side section nav + 5 cached pages, shown via tkraise() — consistent
with the cached-frame pattern used for the app's main tab shell and the
Stats tab. All 5 pages are built once, eagerly, when the Settings tab
itself is constructed (no lazy per-section build): Settings has no
expensive disk/network work at build time, so there's no Stats-style
reason to defer it, and building eagerly keeps save()/load_fields() simple
and correct — every page's widgets/vars exist in memory from the start,
so saving always covers every setting regardless of which section is
currently visible.

Sections (per the agreed mockup-driven structure — supersedes the
original 7-section text breakdown):
  1. General Settings    — logs folder, monitoring interval, manual update
                            check (the app's own self-update), debug,
                            paint reference
  2. Discord Alerts       — bot setup, bot instructions, webhooks
  3. Event Notifications  — script events, per-event notify/screenshot/ping
                            grid, levelup-every, hide-paint-overlay grid
  4. Daily Summary        — daily summary + screenshot scheduling
  5. Restarts & Updates   — auto restart, update awareness (DreamBot/script
                            update checking — a different system from
                            General's manual update check)

Every existing config key, default, and behavior is preserved — this is
a UI/organization checkpoint, not a settings/behavior change. Save/load
still iterate over self._vars exactly as before, so app.py's existing
`self._settings.save()` call (from Monitor's Start button) keeps working
unchanged.

All booleans use plain styled tk.Checkbutton (not custom toggle switches
or pill/chip buttons) — agreed simplification over the mockups for lower
risk, since Tkinter has no native toggle-switch widget.
"""

import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from py.discord import post_discord, bot_setup_discord, _embed
from py.util    import now_str, DEBUG_LOG_FILE, is_frozen
from py.config  import save_config, is_logs_root_account_folder


class SettingsTab:
    """Settings tab. Receives App reference for shared cfg, colours, fonts, watcher."""

    SECTIONS = [
        ('general',       '⚙',  'General Settings'),
        ('discord',       '🤖', 'Discord Alerts'),
        ('notifications', '🔔', 'Event Notifications'),
        ('summary',       '📅', 'Daily Summary'),
        ('restarts',      '🔄', 'Restarts & Updates'),
    ]

    def __init__(self, app, parent_frame):
        self.app = app
        self._vars = {}          # key -> tk variable (shared across all pages)
        self._nav_btns = {}      # section_id -> (wrap_frame, label, indicator)
        self._pages = {}         # section_id -> page frame
        self._active_section = None
        self._build(parent_frame)
        self.load_fields()

    # ── Shell: sidebar + cached pages + persistent save bar ─────────────────
    def _build(self, f):
        app = self.app
        root = tk.Frame(f, bg=app.BG2)
        root.pack(fill='both', expand=True)

        # Persistent save bar — packed with side='bottom' BEFORE the body,
        # so it reserves its strip at the bottom regardless of which
        # section is active. Divider line sits just above it.
        save_bar = tk.Frame(root, bg=app.BG2, padx=16, pady=10)
        save_bar.pack(fill='x', side='bottom')
        tk.Button(save_bar, text="💾  Save Settings", font=app.SANSB,
                  bg=app.ACC, fg=app.BG, relief='flat', padx=16, pady=8,
                  cursor='hand2', command=self.save).pack(side='left')
        tk.Label(save_bar, text="Your changes will be applied immediately.",
                 font=app.SANSS, bg=app.BG2, fg=app.FG2).pack(side='left', padx=(12, 0))
        self._saved_lbl = tk.Label(save_bar, text="", font=app.SANSB, bg=app.BG2, fg=app.GREEN)
        self._saved_lbl.pack(side='left', padx=(12, 0))
        tk.Frame(root, bg=app.BG4, height=1).pack(fill='x', side='bottom')

        body = tk.Frame(root, bg=app.BG2)
        body.pack(fill='both', expand=True)

        self._build_sidebar(body)

        container = tk.Frame(body, bg=app.BG2)
        container.pack(side='left', fill='both', expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        for sid, _icon, _label in self.SECTIONS:
            page = tk.Frame(container, bg=app.BG2)
            page.grid(row=0, column=0, sticky='nsew')
            self._pages[sid] = page

        self._build_general_page(self._pages['general'])
        self._build_discord_page(self._pages['discord'])
        self._build_notifications_page(self._pages['notifications'])
        self._build_summary_page(self._pages['summary'])
        self._build_restarts_page(self._pages['restarts'])

        self.show_section('general')

    def _build_sidebar(self, parent):
        app = self.app
        nav = tk.Frame(parent, bg=app.BG2, width=200)
        nav.pack(side='left', fill='y')
        nav.pack_propagate(False)
        tk.Label(nav, text="SETTINGS", font=app.SANSS, bg=app.BG2, fg=app.FG2
                 ).pack(anchor='w', padx=16, pady=(16, 8))
        for sid, icon, label in self.SECTIONS:
            wrap = tk.Frame(nav, bg=app.BG2)
            wrap.pack(fill='x', padx=8, pady=1)
            indicator = tk.Frame(wrap, width=3, bg=app.BG2)
            indicator.pack(side='left', fill='y')
            lbl = tk.Label(wrap, text=f"{icon}  {label}", font=app.SANSB,
                           bg=app.BG2, fg=app.FG2, anchor='w', padx=12, pady=10,
                           cursor='hand2')
            lbl.pack(side='left', fill='x', expand=True)
            for w in (wrap, lbl):
                w.bind('<Button-1>', lambda e, s=sid: self.show_section(s))
            self._nav_btns[sid] = (wrap, lbl, indicator)

    def show_section(self, section_id):
        """Switch the visible Settings section by id. Raises the cached page
        via tkraise() — no rebuild, no destroy, no widget duplication."""
        app = self.app
        for sid, (wrap, lbl, ind) in self._nav_btns.items():
            if sid == section_id:
                wrap.configure(bg=app.BG3)
                lbl.configure(bg=app.BG3, fg=app.ACC)
                ind.configure(bg=app.ACC)
            else:
                wrap.configure(bg=app.BG2)
                lbl.configure(bg=app.BG2, fg=app.FG2)
                ind.configure(bg=app.BG2)
        if section_id in self._pages:
            self._pages[section_id].tkraise()
        self._active_section = section_id

    # ── Shared row/card builders (used by every page) ───────────────────────
    def _page_header(self, parent, title, subtitle):
        app = self.app
        wrap = tk.Frame(parent, bg=app.BG2, padx=24, pady=20)
        wrap.pack(fill='x')
        tk.Label(wrap, text=title, font=(app.SANS[0], 20, 'bold'),
                 bg=app.BG2, fg=app.FG).pack(anchor='w')
        tk.Label(wrap, text=subtitle, font=app.SANS, bg=app.BG2, fg=app.FG2
                 ).pack(anchor='w', pady=(2, 0))

    def _scrollable_body(self, parent):
        """A vertically-scrollable container for a page's cards, matching
        the original file's mouse-wheel-on-hover scroll behavior."""
        app = self.app
        outer = tk.Frame(parent, bg=app.BG2)
        outer.pack(fill='both', expand=True, padx=24, pady=(0, 12))
        canvas = tk.Canvas(outer, bg=app.BG2, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = tk.Frame(canvas, bg=app.BG2)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _update_scroll(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _update_scroll)
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))

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
        return inner

    def _card(self, parent, icon, title, subtitle=None):
        """A card: icon+title header, optional wrapped subtitle, and a body
        frame for rows. Subtitle wraplength tracks the card's real width
        via <Configure> so it reads correctly whether the card sits in a
        two-column or full-width page."""
        app = self.app
        card = tk.Frame(parent, bg=app.BG3, padx=16, pady=14)
        card.pack(fill='x', pady=(0, 12))
        hdr = tk.Frame(card, bg=app.BG3)
        hdr.pack(fill='x', anchor='w')
        tk.Label(hdr, text=icon, font=(app.SANS[0], 13), bg=app.BG3, fg=app.ACC
                 ).pack(side='left', padx=(0, 8))
        tk.Label(hdr, text=title, font=app.SANSB, bg=app.BG3, fg=app.FG).pack(side='left')
        if subtitle:
            sub_lbl = tk.Label(card, text=subtitle, font=app.SANSS, bg=app.BG3,
                                fg=app.FG2, justify='left', anchor='w')
            sub_lbl.pack(fill='x', anchor='w', pady=(4, 10))
            card.bind('<Configure>', lambda e: sub_lbl.configure(wraplength=max(e.width - 32, 100)))
        body = tk.Frame(card, bg=app.BG3)
        body.pack(fill='x')
        return card, body

    def _row_bool(self, parent, label, attr, default=False, helper=None):
        app = self.app
        row = tk.Frame(parent, bg=app.BG3)
        row.pack(fill='x', pady=4, anchor='w')
        var = tk.BooleanVar(value=bool(app.cfg.get(attr, default)))
        tk.Checkbutton(row, text=label, variable=var, font=app.SANS,
            bg=app.BG3, fg=app.FG, activebackground=app.BG3, activeforeground=app.ACC,
            selectcolor=app.BG2, relief='flat', cursor='hand2', anchor='w'
            ).pack(side='top', anchor='w')
        if helper:
            tk.Label(row, text=helper, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     justify='left', anchor='w').pack(side='top', anchor='w', padx=(24, 0))
        self._vars[attr] = var
        return var

    def _row_text(self, parent, label, attr, helper=None, pw=False, width_label=22):
        app = self.app
        row = tk.Frame(parent, bg=app.BG3)
        row.pack(fill='x', pady=4)
        tk.Label(row, text=label, font=app.SANS, bg=app.BG3, fg=app.FG2,
                 width=width_label, anchor='w').pack(side='left')
        var = tk.StringVar(value=str(app.cfg.get(attr, '')))
        kw = {'show': '•'} if pw else {}
        tk.Entry(row, textvariable=var, font=app.SANS, bg=app.BG4, fg=app.FG,
                  relief='flat', insertbackground=app.ACC, **kw
                 ).pack(side='left', fill='x', expand=True, ipady=4, padx=(8, 0))
        self._vars[attr] = var
        if helper:
            tk.Label(parent, text=helper, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     justify='left', anchor='w').pack(fill='x', pady=(0, 2))
        return var

    def _row_int(self, parent, label, attr, lo, hi, default=None, helper=None, width_label=22):
        app = self.app
        row = tk.Frame(parent, bg=app.BG3)
        row.pack(fill='x', pady=4)
        tk.Label(row, text=label, font=app.SANS, bg=app.BG3, fg=app.FG2,
                 width=width_label, anchor='w').pack(side='left')
        d = default if default is not None else lo
        var = tk.IntVar(value=int(app.cfg.get(attr, d)))
        tk.Spinbox(row, from_=lo, to=hi, textvariable=var, width=6, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                  ).pack(side='left', padx=(8, 0))
        self._vars[attr] = var
        if helper:
            tk.Label(parent, text=helper, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     justify='left', anchor='w').pack(fill='x', pady=(0, 2))
        return var

    def _disclosure(self, parent, icon, title):
        """Collapsible info block (used only for Bot Setup Instructions).
        Starts collapsed. No cfg persistence — this was session-only local
        state before the redesign too, never saved."""
        app = self.app
        holder = tk.Frame(parent, bg=app.BG3)
        holder.pack(fill='x', pady=(4, 12))
        hdr = tk.Frame(holder, bg=app.BG4, cursor='hand2')
        hdr.pack(fill='x')
        tk.Label(hdr, text=icon, font=app.SANS, bg=app.BG4, fg=app.FG2
                 ).pack(side='left', padx=(10, 6), pady=8)
        tk.Label(hdr, text=title, font=app.SANSB, bg=app.BG4, fg=app.FG
                 ).pack(side='left', pady=8)
        chevron = tk.Label(hdr, text='▾', font=app.SANS, bg=app.BG4, fg=app.FG2)
        chevron.pack(side='right', padx=10)
        body = tk.Frame(holder, bg=app.BG3, padx=16)
        state = {'open': False}

        def _toggle(_e=None):
            if state['open']:
                body.pack_forget()
                state['open'] = False
                chevron.configure(text='▾')
            else:
                body.pack(fill='x', pady=(8, 10))
                state['open'] = True
                chevron.configure(text='▴')
        hdr.bind('<Button-1>', _toggle)
        for child in hdr.winfo_children():
            child.bind('<Button-1>', _toggle)
        return body

    def _warning_banner(self, parent, text):
        app = self.app
        banner = tk.Frame(parent, bg=app.BG4)
        banner.pack(fill='x', pady=(8, 0))
        tk.Frame(banner, bg=app.RED, width=4).pack(side='left', fill='y')
        inner = tk.Frame(banner, bg=app.BG4, padx=12, pady=8)
        inner.pack(side='left', fill='x', expand=True)
        lbl = tk.Label(inner, text=f"⚠  {text}", font=app.SANSS, bg=app.BG4,
                        fg=app.RED, justify='left', anchor='w')
        lbl.pack(fill='x', anchor='w')
        banner.bind('<Configure>', lambda e: lbl.configure(wraplength=max(e.width - 24, 100)))
        return banner

    # ── Page 1: General Settings ─────────────────────────────────────────────
    def _build_general_page(self, parent):
        app = self.app
        self._page_header(parent, "General Settings", "Core configuration, tools, and diagnostics")
        inner = self._scrollable_body(parent)

        cols = tk.Frame(inner, bg=app.BG2)
        cols.pack(fill='both', expand=True)
        left = tk.Frame(cols, bg=app.BG2)
        left.pack(side='left', fill='both', expand=True, padx=(0, 6))
        right = tk.Frame(cols, bg=app.BG2)
        right.pack(side='left', fill='both', expand=True, padx=(6, 0))

        # ── Logs folder ──────────────────────────────────────────────────────
        _, body = self._card(left, '📁', 'DreamBot Logs Folder',
                              "Location of DreamBot log files used for monitoring and diagnostics.")
        dr = tk.Frame(body, bg=app.BG3)
        dr.pack(fill='x')
        self._vars['logs_root'] = tk.StringVar(value=app.cfg.get('logs_root', ''))
        tk.Entry(dr, textvariable=self._vars['logs_root'], font=app.SANS, bg=app.BG4, fg=app.FG,
            relief='flat', insertbackground=app.ACC).pack(side='left', fill='x', expand=True, ipady=4)
        tk.Button(dr, text="📁  Browse", font=app.SANS, bg=app.BG4, fg=app.ACC,
            relief='flat', padx=8, pady=4, cursor='hand2',
            command=self._browse_dir).pack(side='left', padx=(6, 0))
        self._logs_root_warn = tk.Label(
            body, text="", font=app.SANSS, bg=app.BG3, fg=app.YEL,
            wraplength=420, justify='left')
        self._logs_root_warn.pack(fill='x', pady=(6, 0))
        body.bind('<Configure>', lambda e: self._logs_root_warn.configure(wraplength=max(e.width - 8, 100)))
        self._vars['logs_root'].trace_add('write', lambda *_: self._check_logs_root_warning())
        self._check_logs_root_warning()

        # ── Manual Update Check (app's own self-update — distinct from the
        # Update Awareness DreamBot/script checker on the Restarts & Updates
        # page) ──────────────────────────────────────────────────────────────
        _, body = self._card(left, '🔄', 'Manual Update Check')
        tk.Label(body, text="P2P Monitor does not auto-update.\n"
                             "Updates are checked manually only and are not auto-installed.",
                 font=app.SANSS, bg=app.BG3, fg=app.FG2, justify='left'
                 ).pack(anchor='w', pady=(0, 8))
        tk.Button(body, text="🔄  Check for Updates", font=app.SANSB,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=14, pady=6,
            cursor='hand2', command=app._check_for_update).pack(anchor='w')
        self._row_bool(body, "Pre-release / Beta Updates", 'beta_updates', default=False,
                       helper="Silent startup check always uses stable only.")

        # ── Paint Reference ──────────────────────────────────────────────────
        _, body = self._card(left, '🖌', 'Paint Reference',
                              "Capture the current screen colors (RGB) to help calibrate "
                              "paint detection and matching.")
        snap_row = tk.Frame(body, bg=app.BG3)
        snap_row.pack(fill='x')
        self._snap_lbl = tk.Label(snap_row, text="", font=app.SANSS, bg=app.BG3, fg=app.GREEN)

        def _do_snap():
            self._snap_lbl.configure(text="⏳ Snapping...", fg=app.YEL)
            app.update_idletasks()
            def _snap():
                from py.screenshot import snap_paint_reference
                ok, msg = snap_paint_reference(log=app._log)
                def _done():
                    self._snap_lbl.configure(
                        text=f"✅ {msg}" if ok else f"❌ {msg}",
                        fg=app.GREEN if ok else app.RED)
                app.after(0, _done)
            threading.Thread(target=_snap, daemon=True).start()
        tk.Button(snap_row, text="🎯  Snap Paint Reference", font=app.SANSB,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=14, pady=6,
            cursor='hand2', command=_do_snap).pack(side='left')
        self._snap_lbl.pack(side='left', padx=12)

        # ── Monitoring Intervals ─────────────────────────────────────────────
        _, body = self._card(right, '🕐', 'Monitoring Intervals')
        self._row_int(body, "Log check interval (seconds):", 'check_interval', 1, 60,
                      helper="How often P2P Monitor checks DreamBot logs for changes.")

        # ── Debug ────────────────────────────────────────────────────────────
        _, body = self._card(right, '🐛', 'Debug')
        self._row_bool(body, "Enable debug logging", 'debug', default=False)
        self._row_bool(body, "Enable anonymous usage stats", 'enable_usage_stats', default=True)
        path_row = tk.Frame(body, bg=app.BG3)
        path_row.pack(fill='x', pady=(8, 0))
        tk.Label(path_row, text="Session debug file:", font=app.SANSS,
                 bg=app.BG3, fg=app.FG2).pack(anchor='w')
        tk.Label(path_row, text=str(DEBUG_LOG_FILE), font=app.SANSS,
                 bg=app.BG4, fg=app.FG2, anchor='w', padx=8, pady=4
                 ).pack(fill='x', pady=(4, 0))

    # ── Page 2: Discord Alerts ───────────────────────────────────────────────
    def _build_discord_page(self, parent):
        app = self.app
        self._page_header(parent, "Discord Alerts", "Configure Discord bot integration for alerts and notifications.")
        inner = self._scrollable_body(parent)

        # ── Bot Setup ────────────────────────────────────────────────────────
        _, body = self._card(inner, '🤖', 'Bot Setup')
        cols = tk.Frame(body, bg=app.BG3)
        cols.pack(fill='x')
        fields_col = tk.Frame(cols, bg=app.BG3)
        fields_col.pack(side='left', fill='both', expand=True, padx=(0, 16))
        status_col = tk.Frame(cols, bg=app.BG3)
        status_col.pack(side='left', fill='y')

        self._row_text(fields_col, "Bot Token:",          'bot_token', pw=True)
        self._row_text(fields_col, "Server ID:",          'bot_server_id')
        self._row_text(fields_col, "Discord Mention ID:", 'mention_id')

        self._bot_setup_lbl = tk.Label(status_col, text="", font=app.SANSS, bg=app.BG3,
                                       fg=app.FG2, wraplength=220, justify='left')
        self._bot_setup_lbl.pack(anchor='w', pady=(0, 8))
        tk.Button(status_col, text="🤖  Run Bot Setup", font=app.SANSB,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=12, pady=6,
            cursor='hand2', command=self._manual_bot_setup).pack(anchor='w')

        if app.cfg.get('bot_setup_done'):
            ch_ids = app.cfg.get('bot_channel_ids', {})
            th_ids = app.cfg.get('bot_thread_ids', {})
            self._bot_setup_lbl.configure(
                text=f"✅ Setup complete — {len(ch_ids)} channels, {len(th_ids)} account(s) ready.",
                fg=app.GREEN)

        # ── Bot Setup Instructions (collapsible) ────────────────────────────
        inst_body = self._disclosure(inner, 'ⓘ', 'Bot Setup Instructions & Permissions')
        tk.Label(inst_body,
            text="Setup:\n"
                 "1. discord.com/developers/home → New Application → Bot → Reset Token → copy it → paste in Bot Token above\n"
                 "2. Privileged Gateway Intents → enable MESSAGE CONTENT INTENT\n"
                 "3. OAuth2 → URL Generator → Scope: bot → Permissions: Send Messages,\n"
                 "   Read Message History, Manage Channels, Manage Webhooks,\n"
                 "   View Channels, Embed Links, Attach Files, Create Public Threads,\n"
                 "   Send Messages in Threads, Manage Threads, Use Slash Commands\n"
                 "4. Copy the generated URL → open in browser → select server → Authorize\n"
                 "5. Right-click your server icon → Copy Server ID → paste above\n"
                 "6. Right-click your name in Discord → Copy User ID → paste in Discord Mention ID above\n"
                 "7. Hit Save then '🤖 Run Bot Setup'\n\n"
                 "Slash commands (registered automatically on first run):\n"
                 "  /ss [account]                    — screenshot(s) → account monitor thread\n"
                 "  /s                               — status of all accounts → #monitor channel\n"
                 "  /force <account> <action> [amt]  — force a skill, action, or time adjustment;\n"
                 "                                      amt (1-20) only applies to -10m / +10m\n"
                 "Tip: /ss and /force inside an account thread target that account only.",
            font=app.SANSS, bg=app.BG3, fg=app.FG2, justify='left').pack(anchor='w')

        # ── Webhooks ─────────────────────────────────────────────────────────
        _, body = self._card(inner, '🔗', 'Webhooks (optional fallback)',
                              "The Default Webhook is the main monitor channel. Any events "
                              "without a specific webhook will fall back to it.")
        wh_cols = tk.Frame(body, bg=app.BG3)
        wh_cols.pack(fill='x')
        wh_left = tk.Frame(wh_cols, bg=app.BG3)
        wh_left.pack(side='left', fill='both', expand=True, padx=(0, 12))
        wh_right = tk.Frame(wh_cols, bg=app.BG3)
        wh_right.pack(side='left', fill='both', expand=True)

        self._row_text(wh_left,  "Default Webhook:",  'webhook_default', width_label=14)
        self._row_text(wh_left,  "Quest Webhook:",     'webhook_quest',   width_label=14)
        self._row_text(wh_left,  "Task Webhook:",      'webhook_task',    width_label=14)
        self._row_text(wh_left,  "Chat Webhook:",       'webhook_chat',    width_label=14)
        self._row_text(wh_right, "Error Webhook:",     'webhook_error',   width_label=14)
        self._row_text(wh_right, "Drops Webhook:",     'webhook_drops',   width_label=14)
        self._row_text(wh_right, "Deaths Webhook:",    'webhook_deaths',  width_label=14)
        self._row_text(wh_right, "Level Up Webhook:",  'webhook_levelup', width_label=14)

        tk.Button(body, text="⚡  Test Webhooks", font=app.SANSB,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=12, pady=6,
            cursor='hand2', command=self._test_webhooks).pack(anchor='w', pady=(8, 0))

    # ── Page 3: Event Notifications ──────────────────────────────────────────
    def _build_notifications_page(self, parent):
        app = self.app
        self._page_header(parent, "Event Notifications", "Configure how and when P2P Monitor sends event notifications.")
        inner = self._scrollable_body(parent)

        # ── Script Events ────────────────────────────────────────────────────
        _, body = self._card(inner, '📜', 'Script Events — Notify when Script:')
        script_row = tk.Frame(body, bg=app.BG3)
        script_row.pack(fill='x')
        for ev_lbl, ev_attr in [
            ("Starts",  'monitor_script_start'),
            ("Pauses",  'monitor_script_pause'),
            ("Resumes", 'monitor_script_resume'),
            ("Stops",   'monitor_script_stop'),
        ]:
            v = tk.BooleanVar(value=bool(app.cfg.get(ev_attr, True)))
            tk.Checkbutton(script_row, text=ev_lbl, variable=v, font=app.SANS,
                bg=app.BG3, fg=app.FG, activebackground=app.BG3, activeforeground=app.ACC,
                selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left', padx=(0, 12))
            self._vars[ev_attr] = v
        ping_se_var = tk.BooleanVar(value=bool(app.cfg.get('ping_script_event', True)))
        tk.Checkbutton(script_row, text="Ping for script events", variable=ping_se_var,
            font=app.SANS, bg=app.BG3, fg=app.FG, activebackground=app.BG3,
            activeforeground=app.ACC, selectcolor=app.BG2, relief='flat',
            cursor='hand2').pack(side='left', padx=(24, 0))
        self._vars['ping_script_event'] = ping_se_var

        # ── Per-event grid ───────────────────────────────────────────────────
        _, body = self._card(inner, '🔔', 'Event Types')
        tbl = tk.Frame(body, bg=app.BG3)
        tbl.pack(anchor='w', fill='x', pady=(4, 0))
        tbl.columnconfigure(0, minsize=140)
        tbl.columnconfigure(1, minsize=90)
        tbl.columnconfigure(2, minsize=110)
        tbl.columnconfigure(3, minsize=90)

        tk.Label(tbl, text="EVENT TYPE",  font=app.SANSB, bg=app.BG3, fg=app.ACC
                 ).grid(row=0, column=0, sticky='w')
        tk.Label(tbl, text="NOTIFY",      font=app.SANSB, bg=app.BG3, fg=app.ACC
                 ).grid(row=0, column=1, sticky='w', padx=(8, 0))
        tk.Label(tbl, text="SCREENSHOT",  font=app.SANSB, bg=app.BG3, fg=app.ACC
                 ).grid(row=0, column=2, sticky='w', padx=(8, 0))
        tk.Label(tbl, text="PING",        font=app.SANSB, bg=app.BG3, fg=app.ACC
                 ).grid(row=0, column=3, sticky='w', padx=(8, 0))

        EVENT_ROWS = [
            ("Quests",    'monitor_quests',   'ss_event_quest',   'ping_quest'),
            ("Tasks",     'monitor_tasks',    'ss_event_task',    'ping_task'),
            ("Chat",      'monitor_chat',     'ss_event_chat',    'ping_chat'),
            ("Errors",    'monitor_errors',   'ss_event_error',   'ping_error'),
            ("Drops",     'monitor_drops',    'ss_event_drops',   'ping_drops'),
            ("Deaths",    'monitor_deaths',   'ss_event_death',   'ping_death'),
            ("Level Ups", 'monitor_levelups', 'ss_event_levelup', 'ping_levelup'),
        ]
        for r, (label, notify_attr, ss_attr, ping_attr) in enumerate(EVENT_ROWS, start=1):
            tk.Label(tbl, text=label, font=app.SANS, bg=app.BG3, fg=app.FG,
                     anchor='w').grid(row=r, column=0, sticky='w', pady=3)

            notify_var = tk.BooleanVar(value=bool(app.cfg.get(notify_attr, True)))
            tk.Checkbutton(tbl, variable=notify_var, font=app.SANS,
                bg=app.BG3, fg=app.FG, activebackground=app.BG3, activeforeground=app.FG,
                selectcolor=app.BG2, relief='flat', cursor='hand2'
                ).grid(row=r, column=1, padx=(16, 0), pady=3)
            self._vars[notify_attr] = notify_var

            ss_var = tk.BooleanVar(value=bool(app.cfg.get(ss_attr, False)))
            tk.Checkbutton(tbl, variable=ss_var, font=app.SANS,
                bg=app.BG3, fg=app.FG, activebackground=app.BG3, activeforeground=app.FG,
                selectcolor=app.BG2, relief='flat', cursor='hand2'
                ).grid(row=r, column=2, padx=(16, 0), pady=3)
            self._vars[ss_attr] = ss_var

            ping_default = app.cfg.get(ping_attr, ping_attr in ('ping_error', 'ping_death'))
            ping_var = tk.BooleanVar(value=bool(ping_default))
            tk.Checkbutton(tbl, variable=ping_var, font=app.SANS,
                bg=app.BG3, fg=app.FG, activebackground=app.BG3, activeforeground=app.FG,
                selectcolor=app.BG2, relief='flat', cursor='hand2'
                ).grid(row=r, column=3, padx=(16, 0), pady=3)
            self._vars[ping_attr] = ping_var

        lvl_row = tk.Frame(body, bg=app.BG3)
        lvl_row.pack(fill='x', pady=(10, 0))
        tk.Label(lvl_row, text="Notify every N levels:", font=app.SANS,
                 bg=app.BG3, fg=app.FG2).pack(side='left')
        lev_var = tk.IntVar(value=int(app.cfg.get('levelup_every', 5)))
        tk.Spinbox(lvl_row, from_=1, to=99, textvariable=lev_var, width=6, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                   ).pack(side='left', padx=(8, 0))
        self._vars['levelup_every'] = lev_var
        tk.Label(lvl_row, text="  (total level milestones are always posted)",
            font=app.SANSS, bg=app.BG3, fg=app.FG2).pack(side='left', padx=(6, 0))

        # ── Hide paint overlay ───────────────────────────────────────────────
        _, body = self._card(inner, '🖌', 'Hide paint overlay during screenshot')
        hp_table = tk.Frame(body, bg=app.BG3)
        hp_table.pack(fill='x', pady=(2, 0))
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
        HP_COLS = 4
        for i, (lbl, key) in enumerate(hp_entries):
            row_i, col = divmod(i, HP_COLS)
            cell = tk.Frame(hp_table, bg=app.BG3)
            cell.grid(row=row_i, column=col, sticky='w', padx=(0, 18), pady=3)
            var = tk.BooleanVar(value=bool(app.cfg.get(key, False)))
            tk.Checkbutton(cell, text=lbl, variable=var, font=app.SANS,
                bg=app.BG3, fg=app.FG2, activebackground=app.BG3, activeforeground=app.ACC,
                selectcolor=app.BG2, relief='flat', cursor='hand2').pack(side='left')
            self._vars[key] = var

    # ── Page 4: Daily Summary ────────────────────────────────────────────────
    def _settings_row(self, parent, icon, title, subtitle, build_control):
        """A single full-width settings row: icon + title/subtitle on the
        left, a control built by build_control(row) on the right, divider
        below. Matches the Daily Summary mockup's list style (distinct from
        the card-grid style used on the other pages, since each setting
        here is independent rather than naturally grouped)."""
        app = self.app
        row = tk.Frame(parent, bg=app.BG2, pady=12)
        row.pack(fill='x')
        left = tk.Frame(row, bg=app.BG2)
        left.pack(side='left', fill='x', expand=True)
        tk.Label(left, text=icon, font=(app.SANS[0], 13), bg=app.BG2, fg=app.ACC
                 ).pack(side='left', anchor='n', padx=(0, 10))
        text_col = tk.Frame(left, bg=app.BG2)
        text_col.pack(side='left', fill='x', expand=True)
        tk.Label(text_col, text=title, font=app.SANSB, bg=app.BG2, fg=app.FG,
                 anchor='w', justify='left').pack(fill='x', anchor='w')
        if subtitle:
            tk.Label(text_col, text=subtitle, font=app.SANSS, bg=app.BG2, fg=app.FG2,
                     anchor='w', justify='left').pack(fill='x', anchor='w')
        control_col = tk.Frame(row, bg=app.BG2)
        control_col.pack(side='right')
        build_control(control_col)
        tk.Frame(parent, bg=app.BG4, height=1).pack(fill='x')

    def _build_summary_page(self, parent):
        app = self.app
        self._page_header(parent, "Daily Summary", "Configure daily summary reports and automatic screenshot options.")
        inner = self._scrollable_body(parent)
        card = tk.Frame(inner, bg=app.BG2)
        card.pack(fill='x')

        def _ctrl_bool(attr, default):
            def build(parent_):
                var = tk.BooleanVar(value=bool(app.cfg.get(attr, default)))
                tk.Checkbutton(parent_, variable=var, font=app.SANS,
                    bg=app.BG2, fg=app.FG, activebackground=app.BG2, activeforeground=app.ACC,
                    selectcolor=app.BG3, relief='flat', cursor='hand2').pack()
                self._vars[attr] = var
            return build

        self._settings_row(card, '✅', "Enable daily summary",
                            "Automatically generate and send a daily summary report.",
                            _ctrl_bool('summary_enabled', False))

        def _build_summary_time(parent_):
            var = tk.StringVar(value=str(app.cfg.get('summary_time', '22:00')))
            tk.Entry(parent_, textvariable=var, font=app.SANS, bg=app.BG4, fg=app.FG,
                      relief='flat', insertbackground=app.ACC, width=10
                     ).pack(ipady=4)
            self._vars['summary_time'] = var
        self._settings_row(card, '🕐', "Send time (HH:MM)",
                            "Set the time of day when the daily summary is sent.",
                            _build_summary_time)

        self._settings_row(card, '⏻', "Screenshot on monitor startup",
                            "Take a screenshot automatically when a monitor starts.",
                            _ctrl_bool('screenshot_on_startup', False))

        self._settings_row(card, '📷', "Enable scheduled screenshots",
                            "Take screenshots automatically at regular intervals.",
                            _ctrl_bool('screenshots_enabled', False))

        def _build_ss_interval(parent_):
            var = tk.IntVar(value=int(app.cfg.get('screenshot_minutes', 60)))
            tk.Spinbox(parent_, from_=5, to=1440, textvariable=var, width=8, font=app.SANS,
                       bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat').pack()
            self._vars['screenshot_minutes'] = var
        self._settings_row(card, '⏱', "Screenshot interval (minutes)",
                            "Set how often scheduled screenshots are taken.",
                            _build_ss_interval)

    # ── Page 5: Restarts & Updates ───────────────────────────────────────────
    def _build_restarts_page(self, parent):
        app = self.app
        self._page_header(parent, "Restarts & Updates", "Configure automatic restarts and update awareness behaviors.")
        inner = self._scrollable_body(parent)

        # ── Auto Restart ─────────────────────────────────────────────────────
        _, body = self._card(inner, '🔄', 'Auto Restart',
                              "Automatically relaunch accounts after Script Stopped is detected.")
        self._row_bool(body, "Auto restart client after Script Stopped",
                       'auto_restart_enabled', default=False)
        self._row_bool(body, "Only auto restart during game update window (Tue/Wed 1–4 AM PT)",
                       'auto_restart_game_update_window_only', default=True)
        self._row_bool(body, "Respect breaks on relaunch",
                       'auto_restart_respect_breaks', default=True)

        tk.Label(body, text="Restart Delay", font=app.SANSB, bg=app.BG3, fg=app.FG
                 ).pack(anchor='w', pady=(10, 0))
        tk.Label(body, text="Restart delay (random within window, ignored when respecting breaks):",
                 font=app.SANSS, bg=app.BG3, fg=app.FG2).pack(anchor='w', pady=(0, 6))
        delay_row = tk.Frame(body, bg=app.BG3)
        delay_row.pack(fill='x')
        tk.Label(delay_row, text="Min minutes:", font=app.SANS, bg=app.BG3, fg=app.FG2
                 ).pack(side='left')
        ar_min_var = tk.IntVar(value=int(app.cfg.get('auto_restart_min_minutes', 1)))
        tk.Spinbox(delay_row, from_=0, to=1440, textvariable=ar_min_var, width=6, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                   ).pack(side='left', padx=(6, 20))
        self._vars['auto_restart_min_minutes'] = ar_min_var
        tk.Label(delay_row, text="Max minutes:", font=app.SANS, bg=app.BG3, fg=app.FG2
                 ).pack(side='left')
        ar_max_var = tk.IntVar(value=int(app.cfg.get('auto_restart_max_minutes', 30)))
        tk.Spinbox(delay_row, from_=1, to=1440, textvariable=ar_max_var, width=6, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                   ).pack(side='left', padx=(6, 0))
        self._vars['auto_restart_max_minutes'] = ar_max_var

        # ── Update Awareness (DreamBot/script updates — distinct from the
        # Manual Update Check on the General Settings page) ─────────────────
        _, body = self._card(inner, '☁', 'Update Awareness')
        self._row_bool(body, "Check for P2P Master AI / DreamBot client updates",
                       'update_check_enabled', default=True,
                       helper="Checks DreamBot SDN for updates and silently falls back if needed.")

        intv_row = tk.Frame(body, bg=app.BG3)
        intv_row.pack(fill='x', pady=(4, 4))
        tk.Label(intv_row, text="Check interval:", font=app.SANS,
                 bg=app.BG3, fg=app.FG2).pack(side='left')
        intv_h_var = tk.IntVar(value=int(app.cfg.get('update_check_interval_hours', 6)))
        tk.Spinbox(intv_row, from_=0, to=24, textvariable=intv_h_var, width=4, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                   ).pack(side='left', padx=(8, 2))
        tk.Label(intv_row, text="h", font=app.SANS, bg=app.BG3, fg=app.FG2).pack(side='left')
        intv_m_var = tk.IntVar(value=int(app.cfg.get('update_check_interval_minutes', 0)))
        tk.Spinbox(intv_row, from_=0, to=59, textvariable=intv_m_var, width=4, font=app.SANS,
                   bg=app.BG4, fg=app.FG, buttonbackground=app.BG4, relief='flat'
                   ).pack(side='left', padx=(6, 2))
        tk.Label(intv_row, text="m   (min: 1m | default: 6h 0m)", font=app.SANSS,
                 bg=app.BG3, fg=app.FG2).pack(side='left', padx=(2, 0))
        self._vars['update_check_interval_hours']   = intv_h_var
        self._vars['update_check_interval_minutes'] = intv_m_var

        self._row_bool(body, "Ping configured user on update alerts", 'ping_update', default=True)
        self._row_bool(body, "Auto-relaunch clients when script/client update is found",
                       'auto_relaunch_on_update', default=False)
        self._warning_banner(body,
            "Auto-relaunch on update will immediately restart affected clients. "
            "This may interrupt your current activity, including critical encounters such as Inferno/Jad.")

    # ── Save ───────────────────────────────────────────────────────────────────
    def save(self):
        """Save every setting from every page, regardless of which section is
        currently visible — all pages are built eagerly at construction time,
        so self._vars always holds every setting's widget variable."""
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
        # Clamp update-check interval: minimum 1 minute, maximum 24 hours
        h = int(app.cfg.get('update_check_interval_hours', 6))
        m = int(app.cfg.get('update_check_interval_minutes', 0))
        total = h * 60 + m
        total = max(1, min(total, 24 * 60))
        app.cfg['update_check_interval_hours']   = total // 60
        app.cfg['update_check_interval_minutes'] = total % 60
        self._vars['update_check_interval_hours'].set(app.cfg['update_check_interval_hours'])
        self._vars['update_check_interval_minutes'].set(app.cfg['update_check_interval_minutes'])
        try:
            save_config(app.cfg)
        except Exception:
            pass
        # Invalidate watcher dir cache so a changed logs_root takes effect immediately
        if app.watcher:
            app.watcher._dirs_last_check = 0
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
    def _check_logs_root_warning(self):
        """Show a warning if logs_root points directly to an account folder."""
        path = self._vars['logs_root'].get().strip()
        if is_logs_root_account_folder(path):
            try:
                from pathlib import Path
                parent = str(Path(path).parent)
            except Exception:
                parent = "the parent Logs folder"
            self._logs_root_warn.configure(
                text=(
                    f"⚠  This path appears to be an account folder, not the parent Logs folder "
                    f"(e.g. {parent}). Only this one account will be monitored — sibling accounts "
                    f"will not be discovered. To monitor multiple accounts, select the parent "
                    f"Logs folder instead."
                )
            )
        else:
            self._logs_root_warn.configure(text="")

    def _browse_dir(self):
        d = filedialog.askdirectory(title="Select DreamBot log folder")
        if d:
            import os
            self._vars['logs_root'].set(os.path.normpath(d))

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
                if is_frozen():
                    # Packaged build — cannot pip-install at runtime.
                    def _frozen_fail():
                        self._bot_setup_lbl.configure(
                            text="❌ discord.py not bundled — reinstall the app",
                            fg=app.RED)
                    app.after(0, _frozen_fail)
                    return
                app.after(0, lambda: self._bot_setup_lbl.configure(
                    text="⏳ Installing discord.py...", fg=app.YEL))
                try:
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
                    # Marshal save() to main thread — Tkinter is not thread-safe
                    app.after(0, self.save)
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
