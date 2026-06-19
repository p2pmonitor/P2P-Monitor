#!/usr/bin/env python3
"""
P2P Monitor v2.0.0-beta.9
Monitors DreamBot P2P Master AI log files, posts events to Discord webhooks.

File structure:
  p2p_monitor.py          — App shell, wiring, tray, lifecycle
  error_rules.json        — Bundled error rule data (repo root; also fetched from GitHub)
  inferno_patterns.json   — Bundled Inferno pattern config (repo root; also fetched from GitHub)
  py/reader.py            — Pure log parsing (parse_lines, slice_*)
  py/error_rules.py       — Remote error rule loader (GitHub → cache → packaged → emergency)
  py/inferno_rules.py     — Remote Inferno pattern loader (GitHub → cache → packaged → emergency)
  py/inferno.py           — Stateful Inferno gear-check and attempt tracker
  py/history.py           — History file I/O
  py/config.py            — Config load/save (config.json)
  py/util.py              — Shared helpers (now_str, fmt_ts)
  py/discord.py           — Embed payloads, post_discord, DiscordRouter, GatewayRunner
  py/screenshot.py        — xdotool, paint hide/show
  py/paint.py             — DreamBot window automation, click commands
  py/watcher.py           — LogWatcher, AccountState, poll loop, backfill
  ui/monitor_tab.py       — Monitor tab
  ui/status_tab.py        — Status tab
  ui/stats_tab.py         — Stats tab (Checkpoint 2: levelup aggregation, KPI cards, chart)
  ui/history_tab.py       — History tab, date picker, tree
  ui/settings_tab.py      — Settings tab, event notifications table
  ui/launcher_tab.py      — DreamBot CLI Launcher tab
"""

import os
import platform as _plat
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

from py.history      import migrate_history
from py.config       import save_config, load_config, sanitize_config
from py.error_rules  import start_background_fetch
from py.inferno_rules import start_background_fetch as start_inferno_fetch
from py.watcher      import LogWatcher
from py              import launcher as _launcher
from ui.monitor_tab   import MonitorTab
from ui.status_tab    import StatusTab
from ui.stats_tab     import StatsTab
from ui.history_tab   import HistoryTab
from ui.launcher_tab  import LauncherTab
from ui.settings_tab  import SettingsTab

# Sans-serif font family — Segoe UI on Windows, DejaVu Sans on Linux/Mac.
# Resolved once at import time; used for nav bar and window chrome in v2+.
# MONO is kept for the raw event log text area and other monospace contexts.
_SANS_FAMILY = 'Segoe UI' if _plat.system() == 'Windows' else 'DejaVu Sans'

VERSION      = "2.0.0-beta.9"
GITHUB_REPO  = "p2pmonitor/P2P-Monitor"

def _is_frozen():
    """Return True when running as a packaged PyInstaller executable."""
    return getattr(sys, 'frozen', False)

# When frozen, __file__ is unreliable — use sys.executable instead.
# Both point to the app entry point in their respective environments.
SCRIPT_PATH = sys.executable if _is_frozen() else os.path.abspath(__file__)

DEFAULT_CFG = {
    "logs_root": "", "webhook_quest": "", "webhook_task": "",
    "webhook_chat": "", "webhook_error": "", "webhook_drops": "", "webhook_default": "",
    "mention_id": "", "check_interval": 5, "beta_updates": False,
    "screenshot_minutes": 60, "bot_token": "",
    "monitor_quests": True, "monitor_tasks": True,
    "monitor_chat": True, "monitor_errors": True, "screenshots_enabled": False,
    "screenshot_on_startup": False,
    "ss_event_task": False, "ss_event_quest": False, "ss_event_chat": False,
    "ss_event_error": False, "ss_event_drops": False,
    "ss_event_death": False, "ss_event_levelup": False,
    "ping_quest": False, "ping_task": False, "ping_chat": False,
    "ping_error": True, "ping_drops": False, "ping_death": True,
    "ping_levelup": False, "ping_update": True, "ping_script_event": True,
    "auto_relaunch_on_update": False,
    "ss_hide_paint_scheduled": False,
    "ss_hide_paint_task": False, "ss_hide_paint_quest": False,
    "ss_hide_paint_chat": False, "ss_hide_paint_error": False,
    "ss_hide_paint_drops": False, "ss_hide_paint_death": False, "ss_hide_paint_levelup": False,
    "ss_hide_paint_ondemand": False, "ss_hide_paint_botss": False,
    "ss_hide_paint_startup": False,
    "summary_enabled": False, "summary_time": "22:00",
    "bot_server_id": "", "bot_setup_done": False,
    "bot_channel_ids": {}, "bot_webhook_urls": {}, "bot_thread_ids": {},
    "muted_accounts": [],
    "webhook_deaths": "", "webhook_levelup": "",
    "monitor_drops": True, "monitor_deaths": True, "monitor_levelups": True,
    "monitor_script_start": True, "monitor_script_pause": True,
    "monitor_script_resume": True, "monitor_script_stop": True,
    "levelup_every": 5,
    "debug": False,
    "enable_usage_stats": True,
    "usage_stats_url": "https://stats.p2pmonitor.workers.dev",
    "launcher_jar": "",
    "launcher_presets": [],
    "auto_restart_enabled": False,
    "auto_restart_min_minutes": 1,
    "auto_restart_max_minutes": 30,
    "auto_restart_game_update_window_only": True,
    "auto_restart_respect_breaks": True,
    "update_check_enabled": True,
    "update_check_interval_hours": 6,
    "update_check_interval_minutes": 0,
    "hist_col_widths": {},
    "ui_section_discord_open": True,
    "ui_section_notifications_open": True,
    "ui_section_auto_restart_open": True,
}

def _send_startup_ping(cfg, log_fn=None):
    """Send anonymous startup ping in background. No retry, no IDs, fails silently."""
    if not cfg.get('enable_usage_stats', True):
        return
    import platform, urllib.request, json as _json
    def _ping():
        try:
            url     = cfg.get('usage_stats_url', 'https://stats.p2pmonitor.workers.dev')
            payload = _json.dumps({'version': VERSION, 'os': platform.system().lower()}).encode()
            req     = urllib.request.Request(url, data=payload,
                                             headers={
                                                 'Content-Type': 'application/json',
                                                 'User-Agent':   f'P2PMonitor/{VERSION}',
                                             })
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            if cfg.get('debug') and log_fn:
                # _log uses self.after(0, ...) internally — safe to call from thread
                log_fn(f'[DEBUG] startup ping failed: {e}')
    threading.Thread(target=_ping, daemon=True).start()


def _ver_tuple(v):
    import re as _re
    s = v.lstrip('v').lower()
    m = _re.match(r'^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$', s)
    if not m:
        return (0, 0, 0, -1, -1)
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    beta_num = m.group(4)
    if beta_num is None:
        return (major, minor, patch, 1, 0)       # stable — ranks above any beta
    return (major, minor, patch, 0, int(beta_num))  # beta


class App(tk.Tk):
    VERSION = VERSION

    # ── Warm dark palette ──────────────────────────────────────────────────────
    # Backgrounds: warm charcoal/espresso/graphite — every shade below is R >= G > B,
    # i.e. genuinely warm-neutral, not a blue/navy-tinted "dark mode" grey.
    BG   = '#13110f'   # main window background — warm near-black
    BG2  = '#1a1714'   # panels, cards, tab content
    BG3  = '#221e19'   # treeviews, elevated sections
    BG4  = '#2c2620'   # borders, separators, table headers

    # Primary accent: muted sage/olive/moss — reserved for active/running/accent states.
    # Desaturated further in beta.3 — beta.2's values still read as vivid "terminal green".
    ACC  = '#7c9468'   # active tab, primary buttons, links
    ACC2 = '#c87830'   # warm amber-orange (secondary highlights)

    # Status / event colours
    GREEN = '#8aac6e'  # running/ok/success/drops — muted moss, slightly brighter than ACC
    RED   = '#d04848'  # error/stopped
    YEL   = '#c8a840'  # level-ups, gold/quests, amber highlights
    PUR   = '#8870b8'  # sparingly (quest badges, lavender)

    # Text
    FG   = '#e4ddd4'   # primary text — warm off-white/cream
    FG2  = '#8c8478'   # secondary/muted text — warm muted grey, not blue-grey

    # ── Fonts ──────────────────────────────────────────────────────────────────
    # Sans-serif — used for nav bar, window chrome, and new UI elements in v2+
    SANS   = (_SANS_FAMILY, 10)
    SANSB  = (_SANS_FAMILY, 10, 'bold')
    SANSL  = (_SANS_FAMILY, 12, 'bold')
    SANSS  = (_SANS_FAMILY, 9)
    BIG    = (_SANS_FAMILY, 15, 'bold')    # window title / large headers
    # Monospace — retained for the raw event log text area and debug output
    MONO   = ('Courier New', 9)
    MONOB  = ('Courier New', 9, 'bold')
    MONOL  = ('Courier New', 10, 'bold')

    def __init__(self):
        super().__init__()
        self.title(f"P2P Monitor v{VERSION}")
        self._tray_icon = None
        self.minsize(960, 680)
        self.configure(bg=self.BG)
        self.cfg     = load_config(DEFAULT_CFG)
        _corrections = sanitize_config(
            self.cfg, DEFAULT_CFG,
            logs_root=self.cfg.get('logs_root', ''),
            log_fn=self._log,
            debug=self.cfg.get('debug', False),
        )
        if _corrections:
            save_config(self.cfg)
        self.watcher = None   # created in _start() to avoid orphaned screenshot worker thread
        self._counts = {k: 0 for k in ('task', 'chat', 'error', 'drop', 'death', 'levelup')}
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Defer remote error rules fetch until after _build() so the Tkinter event
        # loop is running and the monitor tab widget exists to receive the log message.
        # Using after(0, ...) guarantees the fetch fires on the first event loop tick.
        self.after(0, lambda: start_background_fetch(
            log_fn=self._log,
            debug=self.cfg.get('debug', False),
        ))
        self.after(0, lambda: start_inferno_fetch(
            log_fn=self._log,
            debug=self.cfg.get('debug', False),
        ))
        self.after(500, lambda: _send_startup_ping(self.cfg, log_fn=self._log))

    def _style(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        # TNotebook no longer used — navigation is a custom frame-based bar.
        s.configure('TFrame',    background=self.BG2)
        s.configure('TCheckbutton', background=self.BG2, foreground=self.FG, font=self.MONO)
        s.map('TCheckbutton', background=[('active', self.BG2)], foreground=[('active', self.ACC)])
        s.configure('Treeview', background=self.BG3, foreground=self.FG,
                    fieldbackground=self.BG3, font=self.MONO, rowheight=22)
        s.configure('Treeview.Heading', background=self.BG4, foreground=self.ACC,
                    font=self.MONOB, relief='flat')
        s.map('Treeview', background=[('selected', self.ACC)], foreground=[('selected', self.BG)])
        s.configure('TScrollbar', background=self.BG3, troughcolor=self.BG, arrowcolor=self.FG2)

        # ── TCombobox ────────────────────────────────────────────────────────────
        # The 'clam' theme has its own built-in state-based color maps for
        # Combobox that silently override a plain configure() call for
        # certain states (notably 'readonly' and '!focus') — this is why the
        # selected text was unreadable whenever the field wasn't focused: the
        # theme's own default foreground/fieldbackground for those states was
        # winning over our configure() call. Explicit .map() entries for every
        # relevant state combination are required to force our colors to
        # actually stick in all states, not just the default one.
        s.configure('TCombobox', fieldbackground=self.BG3, background=self.BG3,
                    foreground=self.FG, selectbackground=self.BG4,
                    selectforeground=self.FG, arrowcolor=self.FG2, relief='flat')
        s.map('TCombobox',
              fieldbackground=[('readonly', self.BG3), ('disabled', self.BG3),
                                ('focus', self.BG3), ('!focus', self.BG3)],
              foreground=[('readonly', self.FG), ('disabled', self.FG2),
                          ('focus', self.FG), ('!focus', self.FG)],
              selectbackground=[('readonly', self.BG4), ('focus', self.BG4), ('!focus', self.BG4)],
              selectforeground=[('readonly', self.FG), ('focus', self.FG), ('!focus', self.FG)],
              background=[('readonly', self.BG3), ('active', self.BG4), ('!active', self.BG3)],
              arrowcolor=[('readonly', self.FG2), ('active', self.FG), ('!active', self.FG2)])
        # The dropdown popdown list is a plain Tk Listbox, not a ttk widget —
        # it doesn't inherit ttk.Style at all and needs the Tk option database.
        self.option_add('*TCombobox*Listbox.background',         self.BG3)
        self.option_add('*TCombobox*Listbox.foreground',         self.FG)
        self.option_add('*TCombobox*Listbox.selectBackground',   self.ACC)
        self.option_add('*TCombobox*Listbox.selectForeground',   self.BG)
        self.option_add('*TCombobox*Listbox.font',                self.SANS)

    def _build(self):
        # ── Window chrome ──────────────────────────────────────────────────────
        chrome = tk.Frame(self, bg=self.BG, padx=16, pady=11)
        chrome.pack(fill='x')
        tk.Label(chrome, text="P2P MONITOR", font=self.BIG,
                 bg=self.BG, fg=self.ACC).pack(side='left')
        tk.Label(chrome, text="  DreamBot P2P Master AI", font=self.SANS,
                 bg=self.BG, fg=self.FG2).pack(side='left', pady=(2, 0))
        self._status_var = tk.StringVar(value="● STOPPED")
        self._status_lbl = tk.Label(chrome, textvariable=self._status_var,
                                    font=self.SANSB, bg=self.BG, fg=self.RED)
        self._status_lbl.pack(side='right', pady=(2, 0))
        # Hidden until a Linux dependency install completes and the user picks
        # "Later" instead of restarting immediately — see _show_restart_required_notice().
        self._restart_notice_lbl = tk.Label(chrome, text="⚠ Restart required to finish dependency update",
                                            font=self.SANSB, bg=self.BG, fg=self.YEL, cursor='hand2')
        self._restart_notice_lbl.bind('<Button-1>', lambda e: self._on_restart_notice_clicked())
        self._dep_restart_required = False

        # ── Navigation bar ─────────────────────────────────────────────────────
        tk.Frame(self, bg=self.BG4, height=1).pack(fill='x')   # top border
        nav = tk.Frame(self, bg=self.BG, padx=4)
        nav.pack(fill='x')
        tk.Frame(self, bg=self.BG4, height=1).pack(fill='x')   # bottom border

        # Each tab button: label + 2px underline indicator that lights up on active
        self._tab_btns   = {}   # name → (wrap_frame, label, indicator_frame)
        self._active_tab = None

        for name, icon, text in [
            ('Monitor',  '∿',  'MONITOR'),
            ('Status',   '◉',  'STATUS'),
            ('Stats',    '▦',  'STATS'),
            ('History',  '⊙',  'HISTORY'),
            ('Launcher', '▶',  'LAUNCHER'),
            ('Settings', '⚙',  'SETTINGS'),
        ]:
            wrap = tk.Frame(nav, bg=self.BG)
            wrap.pack(side='left')
            lbl = tk.Label(wrap, text=f"{icon}  {text}", font=self.SANSB,
                           bg=self.BG, fg=self.FG2, padx=16, pady=10, cursor='hand2')
            lbl.pack()
            ind = tk.Frame(wrap, height=2, bg=self.BG)   # active underline indicator
            ind.pack(fill='x')
            for widget in (wrap, lbl):
                widget.bind('<Button-1>', lambda e, n=name: self.show_tab(n))
            self._tab_btns[name] = (wrap, lbl, ind)

        # ── Tab content container ──────────────────────────────────────────────
        # All tab frames live in the same grid cell. tkraise() brings one forward
        # without destroying or rebuilding the others — fast tab switching.
        container = tk.Frame(self, bg=self.BG2)
        container.pack(fill='both', expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self._tab_frames = {}
        for name in ('Monitor', 'Status', 'Stats', 'History', 'Launcher', 'Settings'):
            f = tk.Frame(container, bg=self.BG2)
            f.grid(row=0, column=0, sticky='nsew')
            self._tab_frames[name] = f

        # ── Build tab content ──────────────────────────────────────────────────
        MonitorTab(self,        self._tab_frames['Monitor'])
        self._status_tab = StatusTab(self,   self._tab_frames['Status'])
        self._stats_tab  = StatsTab(self,    self._tab_frames['Stats'])
        self._history    = HistoryTab(self,  self._tab_frames['History'])
        self._launcher   = LauncherTab(self, self._tab_frames['Launcher'])
        self._settings   = SettingsTab(self, self._tab_frames['Settings'])

        # Raise Monitor tab first
        self.show_tab('Monitor')

        migrate_history()
        self._status_debounce_id = None
        self.after(100, self._history.load)
        self.after(3000, self._silent_update_check)
        self.after(3500, self._startup_dependency_check)
        self.after(4000, self._prewarm_stats)

    # ── Watcher callbacks ──────────────────────────────────────────────────────
    def _log(self, msg):
        def _do():
            t = self._log_text
            t.configure(state='normal')
            line_count = int(t.index('end-1c').split('.')[0])
            if line_count > 2000:
                t.delete('1.0', f'{line_count - 1800}.0')
            ts = datetime.now().strftime('%H:%M:%S')
            t.insert('end', f"[{ts}] ", 'ts')
            if any(x in msg for x in ['❌', '🚫']):               tag = 'error'
            elif '⚠' in msg:                                        tag = 'warn'
            elif any(x in msg for x in ['🏆', '📜']):              tag = 'quest'
            elif '📋' in msg:                                        tag = 'task'
            elif '💬' in msg:                                        tag = 'chat'
            elif any(x in msg for x in ['📒','💎','💰','🐾','🎁']): tag = 'drop'
            elif '💀' in msg:                                        tag = 'death'
            elif any(x in msg for x in ['🎉', '🎆']):              tag = 'levelup'
            elif '✅' in msg and 'Slayer complete' in msg:           tag = 'slayer_complete'
            elif '⏭️' in msg:                                        tag = 'slayer_skip'
            elif '🖥️' in msg:                                        tag = 'script_event'
            elif any(x in msg for x in ['💓', '🟢', '🗡️']):        tag = 'ok'
            else:                                                    tag = 'info'
            t.insert('end', msg + '\n', tag)
            t.configure(state='disabled')
            t.see('end')
        self.after(0, _do)

    def _on_event(self, etype, folder, v1, v2):
        def _do():
            self._counts[etype] = self._counts.get(etype, 0) + 1
            counter_key = 'quest' if etype == 'quest_completed' else etype
            v = self._sv.get(counter_key)
            if v:
                v.set(str(self._counts[etype]))
            if self._status_debounce_id:
                self.after_cancel(self._status_debounce_id)
            self._status_debounce_id = self.after(2000, self._status_tab.refresh)
            if etype == 'levelup' and getattr(self, '_stats_tab', None):
                self._stats_tab.mark_dirty()
        self.after(0, _do)

    def _on_status_refresh(self):
        self.after(0, self._status_tab.push_refresh)

    def show_tab(self, name: str) -> None:
        """Switch the visible tab by name. Updates the nav underline indicator and
        raises the tab frame via tkraise() — no rebuild, no destroy.
        Triggers on_tab_shown() for tabs that need a data refresh when opened.
        Safe to call from any context: app.show_tab('History')."""
        for tab_name, (wrap, lbl, ind) in self._tab_btns.items():
            if tab_name == name:
                lbl.configure(fg=self.ACC)
                ind.configure(bg=self.ACC)
            else:
                lbl.configure(fg=self.FG2)
                ind.configure(bg=self.BG)
        if name in self._tab_frames:
            self._tab_frames[name].tkraise()
        self._active_tab = name
        # Per-tab refresh hooks
        if name == 'History' and getattr(self, '_history', None):
            self._history.on_tab_shown()
        elif name == 'Status' and getattr(self, '_status_tab', None):
            self._status_tab.on_tab_shown()
        elif name == 'Stats' and getattr(self, '_stats_tab', None):
            self._stats_tab.on_tab_shown()

    # ── Start / Stop ───────────────────────────────────────────────────────────
    def _start(self):
        if not self.cfg.get('logs_root', '').strip():
            messagebox.showwarning("No log directory", "Configure a log directory in Settings first.")
            return
        self._settings.save()
        self._btn_start.configure(state='disabled', bg=self.BG3, fg=self.FG2)
        self._btn_stop.configure(state='normal', bg=self.RED, fg='white')
        self._status_var.set("● RUNNING")
        self._status_lbl.configure(fg=self.GREEN)
        self._counts = {k: 0 for k in self._counts}
        for v in self._sv.values():
            v.set('0')
        self._log("=" * 60)
        self._log(f"▶ Starting P2P Monitor v{VERSION}...")
        self.watcher = LogWatcher(
            self._log, self._on_event, self._on_status_refresh,
            backfill_cb=lambda: self.after(0, self._history.load),
            on_launch_cb=lambda account: _launcher.launch_account(
                self.cfg, account, log_fn=self._log),
            on_launch_all_cb=lambda: _launcher.launch_all(
                self.cfg, log_fn=self._log),
            on_relaunch_cb=lambda account: _launcher.smart_launch(
                self.cfg, account, log_fn=self._log),
            on_relaunch_all_cb=lambda: _launcher.relaunch_all(
                self.cfg, log_fn=self._log),
        )
        self.watcher.start(self.cfg)

    def _stop(self):
        if self.watcher:
            self.watcher.stop()
        self._btn_start.configure(state='normal', bg=self.GREEN, fg=self.BG)
        self._btn_stop.configure(state='disabled', bg=self.BG3, fg=self.FG2)
        self._status_var.set("● STOPPED")
        self._status_lbl.configure(fg=self.RED)
        self._log("■ Monitoring stopped")

    # ── Auto-updater ───────────────────────────────────────────────────────────
    def _check_for_update(self):
        threading.Thread(target=self._do_update_check, daemon=True).start()

    def _startup_dependency_check(self):
        """
        Run once at startup, Linux source installs only. Covers the gap where
        an update was applied by an OLDER updater that didn't yet know about
        _check_and_install_linux_deps — e.g. the beta.3 -> beta.4 transition
        itself, where beta.3's updater copies beta.4's files (including the
        new requirements-linux.txt and the new dependency-check code) but has
        no idea to run it. Once the user restarts into the new version, this
        startup check catches what the old updater couldn't.
        Cheap and silent if nothing is missing (the common case on every
        startup after the first one) — same detection logic and the same
        opt-in prompt as the post-update check, just triggered differently.
        """
        if _is_frozen() or not sys.platform.startswith('linux'):
            return
        install_dir = Path(SCRIPT_PATH).parent
        threading.Thread(target=lambda: self._check_and_install_linux_deps(install_dir),
                         daemon=True).start()

    def _prewarm_stats(self):
        """
        Quietly load + cache the Stats tab's history data a few seconds
        after startup, so a manual click into Stats later doesn't have to
        wait on the disk read — without ever touching a single Tkinter
        widget. StatsTab.prewarm() is data-only by design: it loads levelup
        rows on a background thread and caches them, nothing more. Building
        the actual filter row / KPI cards / chart / donut / panels is
        reserved entirely for the moment the user opens the tab for real
        (on_tab_shown(), via tkraise()), because that is the only point
        where the Stats frame is guaranteed to have real, realized screen
        dimensions. Building or drawing canvases into a frame that isn't
        yet mapped (because some other tab is the one currently raised)
        turned out to be exactly what caused a Linux-specific bug back when
        the chart used matplotlib: a partial build could silently fail
        mid-construction, leaving `self._built` never set and the tab
        rebuilding itself on top of its own broken remains the next time it
        was opened.

        Runs once (this method itself is only ever scheduled a single time,
        via the one self.after(4000, ...) call in _build() — no recurring
        timer). The 4s delay is the same kind of "let the heavier startup
        work settle first" approximation already used for
        _silent_update_check/_startup_dependency_check, not a real idle/CPU
        check — there's no existing load-monitoring infrastructure in this
        app to hook into.

        Never switches tabs, steals focus, or causes flicker — it never
        creates or touches a single widget. StatsTab.prewarm() is itself a
        no-op if the tab was already built (e.g. the user got there first),
        if a prewarm load is already in flight, or if data is already cached.

        Disabled on Linux for this beta: despite prewarm being verified
        data-only (no widget construction at all), Linux still showed the
        duplicate-Stats-section symptom, pointing at a race this fix hasn't
        fully pinned down yet. Stable behavior matters more than first-click
        speed, so prewarm is Windows-only until that's resolved.
        StatsTab itself still has its own build-lock + failure-cleanup guard
        (_building) as defense in depth regardless of platform.
        """
        if sys.platform.startswith('linux'):
            return
        try:
            if getattr(self, '_stats_tab', None):
                self._stats_tab.prewarm()
        except Exception as e:
            self._log(f'⚠ Stats prewarm failed (non-fatal): {e}')

    def _silent_update_check(self):
        threading.Thread(target=self._do_silent_update_check, daemon=True).start()

    def _fetch_release_info(self, include_prerelease=False):
        """
        Return (tag, asset_url) for the best available release.
        include_prerelease=False → /releases/latest (stable only)
        include_prerelease=True  → /releases list, pick highest semver

        For frozen Windows builds, looks for a bare .exe asset (P2P.Monitor.exe).
        For all other builds, looks for P2P-Monitor-*.zip or any .zip.
        """
        import urllib.request, json
        headers = {'Accept': 'application/vnd.github.v3+json',
                   'User-Agent': f'P2PMonitor/{VERSION}'}
        if include_prerelease:
            url = f'https://api.github.com/repos/{GITHUB_REPO}/releases'
        else:
            url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode('utf-8'))
        # /releases returns a list; /releases/latest returns a single object
        if isinstance(data, list):
            if not data:
                return None, None
            def _semver_key(rel):
                return _ver_tuple(rel.get('tag_name', ''))
            data.sort(key=_semver_key, reverse=True)
            release = data[0]
        else:
            release = data
        tag = release.get('tag_name', '')
        assets = release.get('assets', [])

        # Frozen Windows build — look for bare .exe asset
        if _is_frozen() and sys.platform.startswith('win'):
            for asset in assets:
                name = asset.get('name', '')
                if name.endswith('.exe'):
                    return tag, asset['browser_download_url']
            return tag, None

        # Source / Linux — look for zip asset
        asset_url = None
        for asset in assets:
            name = asset.get('name', '')
            if name.startswith('P2P-Monitor-') and name.endswith('.zip'):
                asset_url = asset['browser_download_url']
                break
        if not asset_url:
            for asset in assets:
                if asset.get('name', '').endswith('.zip'):
                    asset_url = asset['browser_download_url']
                    break
        return tag, asset_url

    def _do_silent_update_check(self):
        """Silent startup check — stable releases only, no prompt if already up to date."""
        try:
            tag, _ = self._fetch_release_info(include_prerelease=False)
        except Exception:
            return
        if not tag:
            return
        local_ver  = f'v{VERSION}'
        remote_ver = tag if tag.startswith('v') else f'v{tag}'
        if _ver_tuple(remote_ver) <= _ver_tuple(local_ver):
            return
        def _prompt():
            self._log(f"🔄 Update available: {remote_ver} (current: {local_ver})")
            if not messagebox.askyesno('Update Available',
                    f'New version: {remote_ver}\nYou are on: {local_ver}\n\nWould you like to update?'):
                return
            if _is_frozen() and sys.platform.startswith('win'):
                choice = messagebox.askyesno(
                    'How would you like to update?',
                    'Yes - Auto-Install (trust a stranger)\n'
                    'No  - Take me to GitHub for manual install',
                    icon='question',
                )
                if not choice:
                    import webbrowser
                    webbrowser.open(f'https://github.com/{GITHUB_REPO}/releases/latest')
                    return
            elif not messagebox.askyesno('Update Available',
                    f'New version: {remote_ver}\nYou are on: {local_ver}\n\nUpdate now?'):
                return
            def _fetch_and_apply():
                try:
                    _, asset_url = self._fetch_release_info(include_prerelease=False)
                except Exception as e:
                    self._log(f'❌ Could not fetch release info: {e}')
                    return
                if not asset_url:
                    self._log('❌ No release asset found for this release')
                    return
                self._do_apply_update(remote_ver, asset_url)
            threading.Thread(target=_fetch_and_apply, daemon=True).start()
        self.after(0, _prompt)

    def _do_update_check(self):
        """Manual update check — respects beta opt-in setting."""
        import urllib.error
        include_pre = bool(self.cfg.get('beta_updates', False))
        self._log('🔄 Checking for updates' + (' (including pre-releases)...' if include_pre else '...'))
        try:
            tag, asset_url = self._fetch_release_info(include_prerelease=include_pre)
        except urllib.error.HTTPError as e:
            self.after(0, lambda: messagebox.showerror('Auto-Update', f'GitHub error: {e.code} {e.reason}'))
            return
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('Auto-Update', f'Update check failed: {e}'))
            return
        if not tag:
            self.after(0, lambda: messagebox.showwarning('Auto-Update', 'No releases found.'))
            return
        remote_ver = tag if tag.startswith('v') else f'v{tag}'
        local_ver  = f'v{VERSION}'
        if _ver_tuple(remote_ver) <= _ver_tuple(local_ver):
            self._log(f'✅ Already up to date ({local_ver})')
            self.after(0, lambda: messagebox.showinfo('Auto-Update', f'Already up to date ({local_ver}).'))
            return
        if not asset_url:
            self.after(0, lambda: messagebox.showwarning('Auto-Update',
                f'Release {remote_ver} found but no download asset attached.'))
            return

        def _prompt():
            if not messagebox.askyesno('Update Available',
                    f'New version: {remote_ver}\nCurrent: {local_ver}\n\nWould you like to update?'):
                return
            if _is_frozen() and sys.platform.startswith('win'):
                choice = messagebox.askyesno(
                    'How would you like to update?',
                    'Yes - Auto-Install (trust a stranger)\n'
                    'No  - Take me to GitHub for manual install',
                    icon='question',
                )
                if choice:
                    threading.Thread(target=self._do_apply_update,
                                     args=(remote_ver, asset_url), daemon=True).start()
                else:
                    import webbrowser
                    webbrowser.open(f'https://github.com/{GITHUB_REPO}/releases/latest')
            else:
                threading.Thread(target=self._do_apply_update,
                                 args=(remote_ver, asset_url), daemon=True).start()
        self.after(0, _prompt)

    def _do_apply_update(self, new_ver, asset_url):
        """
        Download release asset and apply update.
        - Frozen Windows: download .exe, write batch file, launch detached, exit app.
        - Source / Linux: download zip, stage, verify manifest, apply .py files.
        """
        if _is_frozen():
            if sys.platform.startswith('win'):
                self._do_win_frozen_update(new_ver, asset_url)
            else:
                # Non-Windows frozen build — open releases page as fallback
                import webbrowser
                self._log(f'🌐 Packaged build — opening download page for {new_ver}')
                webbrowser.open(f'https://github.com/{GITHUB_REPO}/releases/latest')
            return
        import urllib.request, zipfile, io, tempfile, traceback
        install_dir = Path(SCRIPT_PATH).parent
        backup      = SCRIPT_PATH + '.bak'

        self._log(f'⬇️  Downloading {new_ver}...')

        # Download zip into memory
        try:
            req = urllib.request.Request(asset_url,
                headers={'User-Agent': f'P2PMonitor/{VERSION}'})
            with urllib.request.urlopen(req, timeout=60) as r:
                zip_bytes = r.read()
        except Exception as e:
            self._log(f'❌ Download failed: {e}')
            self.after(0, lambda: messagebox.showerror('Update Failed', f'Download failed: {e}'))
            return

        # Stage in a temp dir on the same filesystem as install_dir
        try:
            stage_dir = Path(tempfile.mkdtemp(dir=install_dir, prefix='.update_tmp_'))
        except Exception as e:
            self._log(f'❌ Could not create staging dir: {e}')
            self.after(0, lambda: messagebox.showerror('Update Failed', f'Staging failed: {e}'))
            return

        self._log(f'📦 Staging {new_ver}...')
        try:
            # Extract full zip into staging dir
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(stage_dir)

            # Read manifest — determines which files to apply
            manifest_path = stage_dir / 'update_manifest.txt'
            if manifest_path.exists():
                manifest_lines = manifest_path.read_text(encoding='utf-8').splitlines()
                update_files = []
                for line in manifest_lines:
                    line = line.strip().replace('\\', '/')
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('/') or '..' in line:
                        self._log(f'⚠ Skipping unsafe manifest path: {line}')
                        continue
                    update_files.append(line)
                self._log(f'📋 Manifest: {len(update_files)} file(s) to apply')
            else:
                # Fallback: apply all .py files found in staging dir
                self._log('⚠ No manifest found — applying all .py files')
                update_files = [
                    str(p.relative_to(stage_dir)).replace('\\', '/')
                    for p in stage_dir.rglob('*.py')
                ]

            # Verify all manifest files exist in staging dir before touching install
            missing = [f for f in update_files if not (stage_dir / f).exists()]
            if missing:
                self._log(f'❌ Staging verification failed — missing: {missing}')
                self.after(0, lambda: messagebox.showerror('Update Failed',
                    f'Zip is missing expected files:\n' + '\n'.join(missing)))
                return

            # Back up entry point before any writes
            try:
                shutil.copy2(SCRIPT_PATH, backup)
            except Exception as e:
                self._log(f'⚠ Could not create backup: {e}')

            # Apply staged files to install dir
            applied = 0
            skipped = 0
            errors  = []
            for rel_str in update_files:
                src  = stage_dir / rel_str
                dest = install_dir / rel_str
                try:
                    new_content = src.read_bytes()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() and dest.read_bytes() == new_content:
                        skipped += 1
                        continue
                    dest.write_bytes(new_content)
                    applied += 1
                    self._log(f'  ✅ {rel_str}')
                except Exception as e:
                    errors.append(rel_str)
                    self._log(f'  ❌ {rel_str}: {e}')

            self._log(f'📦 {applied} file(s) updated, {skipped} unchanged')

        except Exception as e:
            self._log(f'❌ Update failed: {e}\n{traceback.format_exc()}')
            self.after(0, lambda: messagebox.showerror('Update Failed', str(e)))
            return
        finally:
            # Always clean up staging dir
            try:
                shutil.rmtree(stage_dir, ignore_errors=True)
            except Exception:
                pass

        if errors:
            msg = f'Update to {new_ver} completed with {len(errors)} error(s):\n' + '\n'.join(errors)
            self._log(f'⚠ {msg}')
            self.after(0, lambda: messagebox.showwarning('Update Incomplete', msg))
        else:
            self._log(f'✅ Updated to {new_ver}')
            if sys.platform.startswith('linux'):
                # Still running on this background update thread — safe to block
                # here on the dependency-confirmation dialog (see method docstring).
                self._check_and_install_linux_deps(install_dir)
            def _restart():
                if messagebox.askyesno('Update Complete',
                        f'Updated to {new_ver}!\n\nRestart now?'):
                    self._restart_app()
            self.after(0, _restart)

    def _check_and_install_linux_deps(self, install_dir):
        """
        Check requirements-linux.txt in install_dir against what's actually
        installed, and prompt before installing anything new via pip.

        Called from two places:
          1. _do_apply_update() — right after a successful Linux source
             update, checking the just-updated requirements-linux.txt.
          2. _startup_dependency_check() — once at app startup, covering the
             case where the update that brought in this dependency was
             applied by an OLDER updater that didn't have this check yet
             (e.g. the beta.3 -> beta.4 transition itself).

        Never touches system/apt packages — only `pip install` for entries
        from requirements-linux.txt that importlib.metadata can't find. If
        nothing is missing, this is a silent no-op (the common case on every
        call after the first one that actually needed to install something).

        Must always be called from a background thread, not the main thread
        — both call sites above run it via threading.Thread. It blocks that
        worker thread on a threading.Event while the confirmation dialog runs
        on the main thread via self.after(), which is safe: only the worker
        thread waits, the Tk mainloop keeps running normally.
        """
        import re
        import subprocess
        import importlib.metadata as _ilmd

        req_path = Path(install_dir) / 'requirements-linux.txt'
        if not req_path.exists():
            return
        try:
            specs = [l.strip() for l in req_path.read_text(encoding='utf-8').splitlines()]
            specs = [l for l in specs if l and not l.startswith('#')]
        except Exception as e:
            self._log(f'⚠ Could not read requirements-linux.txt: {e}')
            return
        if not specs:
            return

        missing = []
        for spec in specs:
            name = re.split(r'[<>=!~]', spec, 1)[0].strip()
            if not name:
                continue
            try:
                _ilmd.version(name)
            except _ilmd.PackageNotFoundError:
                missing.append(spec)
            except Exception:
                continue  # don't let a weird metadata lookup block the update
        if not missing:
            return

        self._log(f'📦 {len(missing)} new Python dependency(ies) needed: ' + ', '.join(missing))

        answer = {}
        event = threading.Event()
        def _ask():
            answer['ok'] = messagebox.askyesno(
                'Install Python Dependencies?',
                f'This update needs {len(missing)} additional Python package(s):\n\n'
                + '\n'.join(f'  • {m}' for m in missing) +
                '\n\nInstall them now via pip?\n\n'
                'This only runs "pip install" for the package(s) above — system\n'
                '(apt) packages and any other setup steps are not touched.')
            event.set()
        self.after(0, _ask)
        event.wait()

        if not answer.get('ok'):
            self._log('⚠ Skipped installing new Python dependencies — install '
                       'manually if Stats or other features look broken: pip3 install '
                       + ' '.join(missing) + ' --break-system-packages')
            return

        self.after(0, lambda: self._log('⏳ Installing Python dependencies...'))
        try:
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', *missing,
                 '--break-system-packages', '--quiet'], timeout=180)
            self._log('✅ Python dependencies installed')
            self.after(0, self._prompt_restart_after_dep_install)
        except Exception as e:
            err_msg = str(e)
            self._log(f'❌ Dependency install failed: {err_msg}')
            cmd_hint = 'pip3 install ' + ' '.join(missing) + ' --break-system-packages'
            self.after(0, lambda: messagebox.showwarning(
                'Dependency Install Failed',
                f'Could not install: {", ".join(missing)}\n\n{err_msg}\n\n'
                f'Install manually with:\n{cmd_hint}'))

    def _prompt_restart_after_dep_install(self):
        """Main-thread only. Shown right after a successful Linux dependency
        pip install (from either _check_and_install_linux_deps caller).
        Restarting now is the only way the newly installed package actually
        gets loaded — Python's import system won't pick it up mid-process."""
        if self._show_restart_now_later_dialog(
                "Dependencies installed successfully.",
                "Please restart P2P Monitor for the new packages to load."):
            self._restart_app()
        else:
            self._show_restart_required_notice()

    def _show_restart_now_later_dialog(self, title_line, body_line):
        """Modal Toplevel with 'Restart Now' / 'Later' buttons — styled like
        the existing _on_close() tray/quit dialog. Returns True if the user
        chose to restart, False otherwise. Main-thread only (creates widgets
        and calls wait_window(), both of which require the main thread)."""
        result = {'restart': False}
        dlg = tk.Toplevel(self)
        dlg.title("P2P Monitor")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.configure(bg=self.BG2)
        tk.Label(dlg, text=title_line, font=self.SANSL,
                 bg=self.BG2, fg=self.FG, padx=24).pack(pady=(16, 4))
        tk.Label(dlg, text=body_line, font=self.SANS,
                 bg=self.BG2, fg=self.FG2, padx=24).pack(pady=(0, 12))
        row = tk.Frame(dlg, bg=self.BG2, padx=16, pady=12)
        row.pack()
        def _choose(restart):
            result['restart'] = restart
            dlg.destroy()
        tk.Button(row, text="Restart Now", font=self.SANSB,
                  bg=self.ACC, fg=self.BG, relief='flat', padx=12, pady=6, cursor='hand2',
                  command=lambda: _choose(True)).pack(side='left', padx=(0, 8))
        tk.Button(row, text="Later", font=self.SANSB,
                  bg=self.BG3, fg=self.FG2, relief='flat', padx=12, pady=6, cursor='hand2',
                  command=lambda: _choose(False)).pack(side='left')
        dlg.update_idletasks()
        x = self.winfo_x() + self.winfo_width()  // 2 - dlg.winfo_width()  // 2
        y = self.winfo_y() + self.winfo_height() // 2 - dlg.winfo_height() // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()
        return result['restart']

    def _show_restart_required_notice(self):
        """Persistent reminder in the window chrome after the user picks
        'Later' instead of restarting immediately. Stays until they actually
        restart — clicking it offers to restart right then."""
        self._dep_restart_required = True
        self._restart_notice_lbl.pack(side='right', padx=(0, 12), pady=(2, 0))
        self._log('⚠ Restart required to finish dependency update.')

    def _on_restart_notice_clicked(self):
        if messagebox.askyesno('Restart P2P Monitor?',
                'Restart now to finish loading the newly installed dependencies?'):
            self._restart_app()

    def _restart_app(self):
        """Cleanly restart P2P Monitor: stop the watcher if running, then
        either open the GitHub releases page (frozen non-Windows builds
        shouldn't reach this at all — guarded defensively) or re-exec the
        current Python process (source installs). Shared by the post-update
        restart prompt and the post-dependency-install restart prompt so
        there's only one restart code path, not two copies of the same logic."""
        if self.watcher:
            self.watcher.stop()
        if _is_frozen():
            import webbrowser
            webbrowser.open(f'https://github.com/{GITHUB_REPO}/releases/latest')
        else:
            os.execv(sys.executable, [sys.executable, SCRIPT_PATH])

    def _do_win_frozen_update(self, new_ver, asset_url):
        """
        Windows frozen (PyInstaller) self-updater.
        Downloads the new .exe, writes a temporary batch file that:
          1. Waits/retries until the running exe is released.
          2. Backs up the old exe as .bak.
          3. Replaces the exe.
          4. Relaunches the updated exe.
          5. Cleans up temp files and self-deletes.
        Launches the batch detached then exits the app cleanly.
        Does not require admin rights. Does not use taskkill /f.
        """
        import urllib.request, tempfile, subprocess, traceback

        old_exe  = Path(sys.executable)
        tmp_dir  = old_exe.parent / '.update_tmp'

        self._log(f'⬇️  Downloading {new_ver}...')
        self.after(0, lambda: None)  # flush UI

        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            new_exe = tmp_dir / 'P2P.Monitor.exe'

            # Download new exe
            req = urllib.request.Request(
                asset_url,
                headers={'User-Agent': f'P2PMonitor/{VERSION}'},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            new_exe.write_bytes(data)
            self._log(f'✅ Downloaded {new_ver} ({len(data) // 1024} KB)')

        except Exception as e:
            self._log(f'❌ Download failed: {e}')
            self.after(0, lambda: messagebox.showerror(
                'Update Failed', f'Could not download {new_ver}:\n{e}'))
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return

        # Paths for the batch — all quoted inside the script
        old_exe_s  = str(old_exe)
        new_exe_s  = str(new_exe)
        tmp_dir_s  = str(tmp_dir)
        bat_path   = tmp_dir / 'update.bat'

        # Batch: wait for exe to be unlocked (retry loop), backup, replace, relaunch, cleanup.
        # taskkill /f is NOT used in the normal path — the app exits cleanly before the
        # batch starts replacing. It is included only as a last-resort after 30s of retrying.
        bat_lines = [
            '@echo off',
            'setlocal',
            '',
            ':: Paths',
            f'set "OLD_EXE={old_exe_s}"',
            f'set "NEW_EXE={new_exe_s}"',
            f'set "TMP_DIR={tmp_dir_s}"',
            f'set "EXE_DIR={str(old_exe.parent)}"',
            f'set "APP_PID={os.getpid()}"',
            '',
            ':: Wait for the app to exit cleanly (up to 30s)',
            'set /a TRIES=0',
            ':WAIT_LOOP',
            '  tasklist /fi "PID eq %APP_PID%" 2>nul | find "%APP_PID%" >nul',
            '  if errorlevel 1 goto DO_REPLACE',
            '  ping -n 1 -w 500 127.0.0.1 >nul',
            '  set /a TRIES=%TRIES%+1',
            '  if %TRIES% lss 60 goto WAIT_LOOP',
            ':: Still running after 30s - force kill as last resort',
            'taskkill /pid %APP_PID% /f >nul 2>&1',
            'ping -n 2 -w 1000 127.0.0.1 >nul',
            '',
            ':DO_REPLACE',
            ':: Replace exe - retry a few times in case of brief lock',
            'set /a REP=0',
            ':REP_LOOP',
            '  copy /y "%NEW_EXE%" "%OLD_EXE%" >nul 2>&1',
            '  if not errorlevel 1 goto REPLACED',
            '  set /a REP=%REP%+1',
            '  if %REP% lss 5 (',
            '    ping -n 2 -w 1000 127.0.0.1 >nul',
            '    goto REP_LOOP',
            '  )',
            '',
            ':: Replace failed - restore backup and stop',
            'echo Update failed: could not replace exe.',
            'goto CLEANUP',
            '',
            ':REPLACED',
            ':: Verify replacement succeeded',
            'if not exist "%OLD_EXE%" goto CLEANUP',
            ':: Wait for old process MEI cleanup before relaunch',
            'ping -n 5 -w 1000 127.0.0.1 >nul',
            ':: Relaunch updated exe',
            'explorer.exe "%OLD_EXE%"',
            '',
            ':CLEANUP',
            ':: Remove temp dir best-effort',
            'ping -n 3 -w 1000 127.0.0.1 >nul',
            'rd /s /q "%TMP_DIR%" >nul 2>&1',
            '',
            ':: Self-delete',
            '(goto) 2>nul & del /f /q "%~f0"',
        ]

        try:
            bat_path.write_text('\r\n'.join(bat_lines), encoding='utf-8-sig')
        except Exception as e:
            self._log(f'❌ Could not write update batch: {e}')
            self.after(0, lambda: messagebox.showerror(
                'Update Failed', f'Could not write update script:\n{e}'))
            return

        self._log(f'🔄 Launching updater - app will close and restart as {new_ver}')

        try:
            self._log(f'🔄 Batch path: {bat_path}')
            self._log(f'🔄 Batch exists: {bat_path.exists()}')
            proc = subprocess.Popen(
                str(bat_path),
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._log(f'🔄 Batch launched (pid {proc.pid}) — closing app for update...')
            self.after(500, self._do_quit)
        except Exception as e:
            self._log(f'❌ Could not launch update batch: {e}')
            self.after(0, lambda: messagebox.showerror(
                'Update Failed', f'Could not launch update script:\n{e}'))

    # ── Tray ───────────────────────────────────────────────────────────────────
    def _make_tray_icon(self):
        img = Image.new('RGB', (64, 64), color=(74, 143, 92))   # ACC sage green
        ImageDraw.Draw(img).rectangle([16, 16, 48, 48], fill=(15, 17, 21))  # BG
        return img

    def _show_window(self, icon=None, item=None):
        self.after(0, self.deiconify)
        self.after(0, self.lift)

    def _quit_from_tray(self, icon, item):
        icon.stop()
        self._tray_icon = None
        self.after(0, self._do_quit)

    def _do_quit(self):
        if self.watcher:
            self.watcher.stop()
        save_config(self.cfg)
        self.destroy()

    def _minimize_to_tray(self):
        if not TRAY_AVAILABLE:
            self.iconify()
            return
        self.withdraw()
        menu = pystray.Menu(
            pystray.MenuItem('Open P2P Monitor', self._show_window, default=True),
            pystray.MenuItem('Quit', self._quit_from_tray))
        icon = pystray.Icon('P2P Monitor', self._make_tray_icon(), 'P2P Monitor', menu)
        self._tray_icon = icon
        threading.Thread(target=icon.run, daemon=True).start()

    def _on_close(self):
        if TRAY_AVAILABLE:
            dlg = tk.Toplevel(self)
            dlg.title("P2P Monitor"); dlg.resizable(False, False)
            dlg.grab_set(); dlg.configure(bg=self.BG2)
            tk.Label(dlg, text="What would you like to do?", font=self.MONOL,
                     bg=self.BG2, fg=self.FG, padx=24, pady=16).pack()
            row = tk.Frame(dlg, bg=self.BG2, padx=16, pady=12); row.pack()
            tk.Button(row, text="Minimize to Tray", font=self.MONO,
                bg=self.ACC, fg=self.BG, relief='flat', padx=12, pady=6, cursor='hand2',
                command=lambda: [dlg.destroy(), self._minimize_to_tray()]).pack(side='left', padx=(0,8))
            tk.Button(row, text="Quit", font=self.MONO,
                bg=self.RED, fg='white', relief='flat', padx=12, pady=6, cursor='hand2',
                command=lambda: [dlg.destroy(), self._do_quit()]).pack(side='left', padx=(0,8))
            tk.Button(row, text="Cancel", font=self.MONO,
                bg=self.BG3, fg=self.FG2, relief='flat', padx=12, pady=6, cursor='hand2',
                command=dlg.destroy).pack(side='left')
            dlg.update_idletasks()
            x = self.winfo_x() + self.winfo_width()  // 2 - dlg.winfo_width()  // 2
            y = self.winfo_y() + self.winfo_height() // 2 - dlg.winfo_height() // 2
            dlg.geometry(f"+{x}+{y}")
        else:
            if messagebox.askyesno("P2P Monitor", "Close and stop monitoring?"):
                self._do_quit()


if __name__ == '__main__':
    try:
        App().mainloop()
    except Exception as e:
        import traceback; traceback.print_exc()
        try:
            messagebox.showerror("P2P Monitor - Startup Error", str(e))
        except Exception:
            pass
