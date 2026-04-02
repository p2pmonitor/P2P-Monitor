#!/usr/bin/env python3
"""
P2P Monitor v1.1.6 — Debian 12 native
Monitors DreamBot P2P Master AI log files, posts events to Discord webhooks.

File structure:
  p2p_monitor.py          — App shell, wiring, tray, lifecycle
  py/reader.py            — Pure log parsing (parse_lines, slice_*)
  py/history.py           — History file I/O
  py/config.py            — Config load/save (config.json)
  py/util.py              — Shared helpers (now_str, fmt_ts)
  py/discord.py           — Embed payloads, post_discord, DiscordRouter, GatewayRunner
  py/screenshot.py        — xdotool, paint hide/show
  py/paint.py             — DreamBot window automation, click commands
  py/watcher.py           — LogWatcher, AccountState, poll loop, backfill
  ui/monitor_tab.py       — Monitor tab
  ui/status_tab.py        — Status tab
  ui/history_tab.py       — History tab, date picker, tree
  ui/settings_tab.py      — Settings tab, event notifications table
"""

import os
import re
import shutil
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
from py.config       import save_config, load_config
from py.watcher      import LogWatcher
from ui.monitor_tab  import MonitorTab
from ui.status_tab   import StatusTab
from ui.history_tab  import HistoryTab
from ui.settings_tab import SettingsTab

VERSION     = "1.1.6"
SCRIPT_PATH  = os.path.abspath(__file__)
GITHUB_REPO  = "p2pmonitor/P2P-Monitor"

DEFAULT_CFG = {
    "logs_root": "", "webhook_quest": "", "webhook_task": "",
    "webhook_chat": "", "webhook_error": "", "webhook_drops": "", "webhook_default": "",
    "mention_id": "", "check_interval": 5, "beta_updates": False,
    "screenshot_minutes": 60, "bot_token": "",
    "monitor_quests": True, "monitor_tasks": True,
    "monitor_chat": True, "monitor_errors": True, "screenshots_enabled": False,
    "ss_event_task": False, "ss_event_quest": False, "ss_event_chat": False,
    "ss_event_error": False, "ss_event_drops": False,
    "ss_event_death": False, "ss_event_levelup": False,
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
}

def _ver_tuple(v):
    return tuple(int(x) for x in v.lstrip('v').split('.') if x.isdigit())


class App(tk.Tk):
    VERSION = VERSION
    BG    = '#0f1117'
    BG2   = '#181c27'
    BG3   = '#1e2233'
    BG4   = '#242840'
    ACC   = '#00d4ff'
    ACC2  = '#ff6b35'
    GREEN = '#00ff88'
    RED   = '#ff4444'
    YEL   = '#ffd700'
    PUR   = '#bb86fc'
    FG    = '#e8eaf0'
    FG2   = '#7a8099'
    MONO  = ('Courier New', 9)
    MONOB = ('Courier New', 9, 'bold')
    MONOL = ('Courier New', 10, 'bold')
    BIG   = ('Courier New', 15, 'bold')

    def __init__(self):
        super().__init__()
        self.title(f"P2P Monitor v{VERSION}")
        self._tray_icon = None
        self.minsize(960, 680)
        self.configure(bg=self.BG)
        self.cfg     = load_config(DEFAULT_CFG)
        self.watcher = None   # created in _start() to avoid orphaned screenshot worker thread
        self._counts = {k: 0 for k in ('task', 'chat', 'error', 'drop', 'death', 'levelup')}
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _style(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('TNotebook',     background=self.BG2, borderwidth=0)
        s.configure('TNotebook.Tab', background=self.BG3, foreground=self.FG2,
                    padding=[14, 6], font=self.MONO)
        s.map('TNotebook.Tab', background=[('selected', self.BG2)],
              foreground=[('selected', self.ACC)])
        s.configure('TFrame',    background=self.BG2)
        s.configure('TCheckbutton', background=self.BG2, foreground=self.FG, font=self.MONO)
        s.map('TCheckbutton', background=[('active', self.BG2)], foreground=[('active', self.ACC)])
        s.configure('Treeview', background=self.BG3, foreground=self.FG,
                    fieldbackground=self.BG3, font=self.MONO, rowheight=22)
        s.configure('Treeview.Heading', background=self.BG4, foreground=self.ACC,
                    font=self.MONOB, relief='flat')
        s.map('Treeview', background=[('selected', self.ACC)], foreground=[('selected', self.BG)])
        s.configure('TScrollbar', background=self.BG3, troughcolor=self.BG, arrowcolor=self.FG2)
        s.configure('TCombobox',  fieldbackground=self.BG3, background=self.BG3,
                    foreground=self.FG, selectbackground=self.BG4)

    def _build(self):
        hdr = tk.Frame(self, bg=self.BG); hdr.pack(fill='x')
        tk.Frame(hdr, bg=self.ACC, height=2).pack(fill='x')
        inn = tk.Frame(hdr, bg=self.BG, padx=16, pady=10); inn.pack(fill='x')
        tk.Label(inn, text="P2P MONITOR", font=self.BIG, bg=self.BG, fg=self.ACC).pack(side='left')
        tk.Label(inn, text=f"v{VERSION}  |  DreamBot P2P Master AI", font=self.MONO,
                 bg=self.BG, fg=self.FG2).pack(side='left', padx=(12, 0), pady=(4, 0))
        self._status_var = tk.StringVar(value="● STOPPED")
        self._status_lbl = tk.Label(inn, textvariable=self._status_var, font=self.MONOB,
                                    bg=self.BG, fg=self.RED)
        self._status_lbl.pack(side='right')

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill='both', expand=True)
        frames = {}
        for name in ('Monitor', 'Status', 'History', 'Settings'):
            f = ttk.Frame(self._nb)
            self._nb.add(f, text=f'  {name.upper()}  ')
            frames[name] = f

        MonitorTab(self,       frames['Monitor'])
        self._status_tab = StatusTab(self,   frames['Status'])
        self._history    = HistoryTab(self,  frames['History'])
        self._settings   = SettingsTab(self, frames['Settings'])
        self._history_tab_frame = frames['History']
        self._status_tab_frame  = frames['Status']

        self._nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        migrate_history()
        self._status_debounce_id = None
        self.after(100, self._history.load)
        self.after(3000, self._silent_update_check)

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
            elif '🎉' in msg:                                        tag = 'levelup'
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
        self.after(0, _do)

    def _on_status_refresh(self):
        self.after(0, self._status_tab.refresh)

    def _on_tab_changed(self, event):
        try:
            sel = self._nb.select()
            if sel == str(self._history_tab_frame):
                self._history.on_tab_shown()
            elif sel == str(self._status_tab_frame):
                self._status_tab.refresh()
        except Exception:
            pass

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
        self.watcher = LogWatcher(self._log, self._on_event, self._on_status_refresh,
                                   backfill_cb=lambda: self.after(0, self._history.load))
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

    def _silent_update_check(self):
        threading.Thread(target=self._do_silent_update_check, daemon=True).start()

    def _local_ver(self):
        """Return local version string e.g. 'v1.1.5'."""
        v = VERSION if VERSION.startswith('v') else f'v{VERSION}'
        return v

    def _fetch_release_info(self, include_prerelease=False):
        """
        Return (tag, asset_url) for the best available release.
        include_prerelease=False → /releases/latest (stable only)
        include_prerelease=True  → /releases list, pick highest semver
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
            # Sort by parsed semver descending — not published_at
            def _semver_key(rel):
                tag = rel.get('tag_name', '').lstrip('v')
                try:
                    return tuple(int(x) for x in tag.split('.')[:3])
                except Exception:
                    return (0, 0, 0)
            data.sort(key=_semver_key, reverse=True)
            release = data[0]
        else:
            release = data
        tag = release.get('tag_name', '')
        # Find the release zip — prefer P2P-Monitor-*.zip, fall back to any .zip
        asset_url = None
        for asset in release.get('assets', []):
            name = asset.get('name', '')
            if name.startswith('P2P-Monitor-') and name.endswith('.zip'):
                asset_url = asset['browser_download_url']
                break
        if not asset_url:
            for asset in release.get('assets', []):
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
        local_ver  = self._local_ver()
        remote_ver = tag if tag.startswith('v') else f'v{tag}'
        # Never prompt downgrade (e.g. user is on beta ahead of stable)
        if _ver_tuple(remote_ver) <= _ver_tuple(local_ver):
            return
        def _prompt():
            self._log(f"🔄 Update available: {remote_ver} (current: {local_ver})")
            if messagebox.askyesno('Update Available',
                    f'New version: {remote_ver}\nYou are on: {local_ver}\n\nUpdate now?'):
                def _fetch_and_apply():
                    try:
                        _, asset_url = self._fetch_release_info(include_prerelease=False)
                    except Exception as e:
                        self._log(f'❌ Could not fetch release info: {e}')
                        return
                    if not asset_url:
                        self._log('❌ No zip asset found for this release')
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
        local_ver  = self._local_ver()
        if _ver_tuple(remote_ver) <= _ver_tuple(local_ver):
            self._log(f'✅ Already up to date ({local_ver})')
            self.after(0, lambda: messagebox.showinfo('Auto-Update', f'Already up to date ({local_ver}).'))
            return
        if not asset_url:
            self.after(0, lambda: messagebox.showwarning('Auto-Update',
                f'Release {remote_ver} found but no zip asset attached.'))
            return
        def _prompt():
            if messagebox.askyesno('Update Available',
                    f'New version: {remote_ver}\nCurrent: {local_ver}\n\nUpdate now?'):
                threading.Thread(target=self._do_apply_update,
                                 args=(remote_ver, asset_url), daemon=True).start()
        self.after(0, _prompt)

    def _do_apply_update(self, new_ver, asset_url):
        """
        Download release zip, stage in temp dir, verify via manifest, then apply.
        Staged approach: install dir is not touched until all files are verified.
        Falls back gracefully if manifest is missing (applies all .py files).
        """
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
            self._log(f'✅ Updated to {new_ver} — backup at p2p_monitor.py.bak')
            def _restart():
                if messagebox.askyesno('Update Complete',
                        f'Updated to {new_ver}!\n\nRestart now?'):
                    if self.watcher:
                        self.watcher.stop()
                    os.execv(sys.executable, [sys.executable, SCRIPT_PATH])
            self.after(0, _restart)

    # ── Tray ───────────────────────────────────────────────────────────────────
    def _make_tray_icon(self):
        img = Image.new('RGB', (64, 64), color=(0, 212, 255))
        ImageDraw.Draw(img).rectangle([16, 16, 48, 48], fill=(0, 30, 60))
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
            messagebox.showerror("P2P Monitor — Startup Error", str(e))
        except Exception:
            pass
