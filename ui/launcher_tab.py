"""
ui/launcher_tab.py — Launcher tab for P2P Monitor (v2.0.0-beta.14 redesign)

Warm dark card-based layout matching the rest of v2.0, built against an
explicit visual reference (column-header row with icons, sage avatar
circles, muted dark buttons with colored icon+text, solid-green Add
Account / Launch Selected, plain inline helper text).

v3.0.0 (Detuks client): config keys launcher_path + launcher_presets.
Preset fields: account (--jl-account, case-sensitive), script (--script),
script_profile (--scriptArgs), software_rendering (--software-rendering),
rl_properties (--rl-properties), custom (extra client args after `--`).
v2.x DreamBot presets are migrated on load — see
py.launcher.migrate_launcher_config().
- The click-to-select + "Launch Selected" mechanism — kept exactly as
  before, just reimplemented over Frame rows instead of a Treeview
  (clicking a row's account area toggles selection with a visible
  highlight; clicking Launch/Edit/Delete acts immediately and doesn't
  toggle selection — same split as the old column-based click handler).
- Add/Edit/Delete dialog behavior and delete confirmation.

Upgrades, all backend functions that already existed before this
checkpoint but the UI never called:
  avatar, refreshed via discover_account_process() — always off the Tk
  main thread, never on a continuous ticker (refreshed once at tab
  build, once on each on_tab_shown(), and once after any launch/relaunch
  completes — there's no continuous live event stream to hook into here
  the way Monitor/Status have, so continuous polling would just be
  unnecessary background load for a tab that's mostly about taking
  action, not live-monitoring).
- Window-position capture/restore on relaunch lives entirely in
  py/launcher.py/py/platform_ops.py (relaunch_account/set_window_geometry)
  — this file has no direct involvement in that beyond calling
  the Launcher tab's explicit launch/relaunch operations.

Per-account in-flight guard: a Launch button disables itself the moment
it's clicked and stays disabled until that account's launch/relaunch
attempt resolves, so repeated clicks (or a "Launch Selected" overlapping
a just-clicked single Launch) can never spawn two operations for the same
account at once.
"""
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from py.config   import save_config
from py.launcher import (launch_account, relaunch_account, build_command, list_presets,
                          discover_account_process, format_command,
                          validate_launcher_path, DEFAULT_SCRIPT, DEFAULT_SCRIPT_PROFILE,
                          account_home, default_homes_root, validate_home,
                          copy_profiles_to_homes, main_home_from_logs_root)

_IS_WINDOWS = os.name == 'nt'

# Minimum genuine overflow (in px) before a scroll container shows its
# scrollbar — without this, even a few pixels of rounding/measurement
# noise (which happens routinely across different font metrics, e.g.
# Windows vs Linux) triggers a scrollbar that barely moves and serves
# no purpose. Only real, meaningful overflow should ever scroll.
_SCROLL_TOLERANCE_PX = 16


class LauncherTab:
    def __init__(self, app, frame):
        self.app = app
        self.frame = frame
        self._row_widgets = {}   # account -> dict of widget refs
        self._selected = set()   # accounts currently selected (Launch Selected)
        self._in_flight = set()  # accounts currently launching/relaunching
        self._status_cache = {}  # account -> 'open' | 'closed' | 'unknown'
        self._status_refresh_in_flight = False  # guards against overlapping background scans
        self._build()
        self.on_tab_shown()

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self):
        app = self.app
        root = tk.Frame(self.frame, bg=app.BG2, padx=16, pady=16)
        root.pack(fill='both', expand=True)

        # ── Detuks launcher path ────────────────────────────────────────────
        tk.Label(root, text=("Detuks Launcher (launcher.exe)" if _IS_WINDOWS
                             else "Detuks Client AppImage"),
                 font=app.SANSB, bg=app.BG2, fg=app.FG).pack(anchor='w')
        path_row = tk.Frame(root, bg=app.BG2)
        path_row.pack(fill='x', pady=(8, 16))
        self._path_var = tk.StringVar(value=app.cfg.get('launcher_path', ''))
        path_entry = tk.Entry(path_row, textvariable=self._path_var, font=app.SANS,
                              bg=app.BG3, fg=app.FG, insertbackground=app.ACC, relief='flat')
        path_entry.pack(side='left', fill='x', expand=True, ipady=7, padx=(0, 8))
        self._path_var.trace_add('write', lambda *_: self._save_path())

        tk.Button(path_row, text='📁  Browse', font=app.SANSB, bg=app.BG4, fg=app.FG,
                  relief='flat', padx=14, pady=8, cursor='hand2',
                  command=self._browse_path).pack(side='left', padx=(0, 8))

        tk.Button(path_row, text='+  Add Account', font=app.SANSB, bg=app.GREEN, fg=app.BG,
                  relief='flat', padx=14, pady=8, cursor='hand2',
                  command=self._open_add_dialog).pack(side='left')

        self._build_homes_card(root)

        # ── Card: Launcher Accounts ─────────────────────────────────────────
        card = tk.Frame(root, bg=app.BG3, padx=14, pady=12)
        card.pack(fill='both', expand=True)
        tk.Label(card, text='Launcher Accounts', font=app.SANSB, bg=app.BG3, fg=app.FG
                 ).pack(anchor='w', pady=(0, 10))

        self._build_column_header(card)

        outer = tk.Frame(card, bg=app.BG3)
        outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(outer, bg=app.BG3, highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        self._scroll_canvas = canvas
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        self._scrollbar = sb
        self._rows_frame = tk.Frame(canvas, bg=app.BG3)
        win = canvas.create_window((0, 0), window=self._rows_frame, anchor='nw')

        needs_scroll = False

        def _sync_scroll(_e=None):
            nonlocal needs_scroll
            canvas.configure(scrollregion=canvas.bbox('all'))
            content_h = self._rows_frame.winfo_reqheight()
            visible_h = canvas.winfo_height()
            needs_scroll = (content_h - visible_h) > _SCROLL_TOLERANCE_PX and visible_h > 1
            if needs_scroll and not sb.winfo_ismapped():
                sb.pack(side='right', fill='y')
            elif not needs_scroll and sb.winfo_ismapped():
                sb.pack_forget()
                canvas.yview_moveto(0)
        self._rows_frame.bind('<Configure>', _sync_scroll)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfig(win, width=e.width), _sync_scroll()))

        # Mousewheel/trackpad scroll must be a no-op when content doesn't
        # actually overflow the visible area — without the needs_scroll
        # guard, yview_scroll() still nudges the view by a fraction of
        # whatever scrollregion canvas.bbox('all') reports (which can be a
        # few pixels larger than the visible area from rounding alone),
        # letting the user drag content down even though there's nothing
        # real to scroll to. That's the "invisible scroll" bug.
        def _on_enter(_):
            canvas.bind_all('<MouseWheel>', lambda e: needs_scroll and canvas.yview_scroll(-1 * (e.delta // 120), 'units'))
            canvas.bind_all('<Button-4>',   lambda e: needs_scroll and canvas.yview_scroll(-1, 'units'))
            canvas.bind_all('<Button-5>',   lambda e: needs_scroll and canvas.yview_scroll(1,  'units'))
        def _on_leave(_):
            canvas.unbind_all('<MouseWheel>')
            canvas.unbind_all('<Button-4>')
            canvas.unbind_all('<Button-5>')
        canvas.bind('<Enter>', _on_enter)
        canvas.bind('<Leave>', _on_leave)

        self._empty_lbl = tk.Label(self._rows_frame,
            text="No launcher accounts yet — click \"+ Add Account\" to create your first preset.",
            font=app.SANS, bg=app.BG3, fg=app.FG2)

        # ── Bottom: Launch Selected + inline helper text ────────────────────
        bot = tk.Frame(root, bg=app.BG2)
        bot.pack(fill='x', pady=(12, 0))
        tk.Button(bot, text='▶  Launch Selected', font=app.SANSB, bg=app.GREEN, fg=app.BG,
                  relief='flat', padx=16, pady=10, cursor='hand2',
                  command=self._launch_selected).pack(side='left')

        info_row = tk.Frame(root, bg=app.BG2)
        info_row.pack(fill='x', pady=(8, 0))
        info_text = (
            "ⓘ  Each account must be saved in the Detuks Client launcher. The account name is its "
            "display name there and is CASE-SENSITIVE (e.g. MyAccount ≠ myaccount). With per-account "
            "homes on, every account the monitor launches gets its own log — any number of accounts "
            "are tracked exactly. Without them, the client writes all accounts to one shared log and "
            "monitoring is exact for one account at a time."
        )
        info_lbl = tk.Label(info_row, text=info_text, font=app.SANSS, bg=app.BG2, fg=app.FG2,
                             justify='left', anchor='w')
        info_lbl.pack(fill='x', anchor='w')
        info_row.bind('<Configure>', lambda e: info_lbl.configure(wraplength=max(e.width - 4, 100)))

        self._refresh_rows()

    ROW_BTN_WIDTH = 11  # character width for each of the 3 right-side buttons

    # ── Per-account Detuks homes (v3.1.0) ─────────────────────────────────────
    def _build_homes_card(self, root):
        app = self.app
        card = tk.Frame(root, bg=app.BG3, padx=14, pady=10)
        card.pack(fill='x', pady=(0, 12))
        top = tk.Frame(card, bg=app.BG3)
        top.pack(fill='x')
        self._homes_on = tk.BooleanVar(value=bool(app.cfg.get('detuks_homes_enabled', False)))
        tk.Checkbutton(top, text='  Per-account Detuks homes (each account gets its own log)',
                       variable=self._homes_on, font=app.SANSB, bg=app.BG3, fg=app.FG,
                       selectcolor=app.BG4, activebackground=app.BG3, activeforeground=app.FG,
                       command=self._save_homes).pack(side='left')

        row = tk.Frame(card, bg=app.BG3)
        row.pack(fill='x', pady=(8, 0))
        tk.Label(row, text='Homes folder:', font=app.SANS, bg=app.BG3, fg=app.FG
                 ).pack(side='left', padx=(0, 8))
        self._homes_root = tk.StringVar(
            value=app.cfg.get('detuks_homes_root', '') or default_homes_root())
        tk.Entry(row, textvariable=self._homes_root, font=app.SANS, bg=app.BG4, fg=app.FG,
                 insertbackground=app.ACC, relief='flat'
                 ).pack(side='left', fill='x', expand=True, ipady=5, padx=(0, 8))
        self._homes_root.trace_add('write', lambda *_: self._save_homes())
        tk.Button(row, text='📁  Browse', font=app.SANSS, bg=app.BG4, fg=app.FG, relief='flat',
                  padx=10, pady=4, cursor='hand2', command=self._browse_homes
                  ).pack(side='left', padx=(0, 8))
        tk.Button(row, text='📋  Copy P2P profiles into homes', font=app.SANSS, bg=app.BG4,
                  fg=app.ACC, relief='flat', padx=10, pady=4, cursor='hand2',
                  command=self._copy_profiles).pack(side='left')

        self._homes_warn = tk.Label(card, text='', font=app.SANSS, bg=app.BG3, fg=app.RED,
                                    anchor='w', justify='left')
        self._homes_warn.pack(fill='x')
        help_txt = (
            "Each account is launched with its own Java home: <homes folder>/<account> "
            "(spaces become _). The Detuks client then keeps that account's settings, script "
            "profiles and log there — so the monitor reads one log per account. Only accounts "
            "launched by the monitor (or with the copied command) get this. A new home starts "
            "with default client settings: set each preset's Settings file (--rl-properties) to "
            "your existing settings.properties, and use \"Copy P2P profiles into homes\" so the "
            "script finds your profiles (only .p2p files are copied; existing ones are never "
            "overwritten). The homes folder must not contain spaces.")
        hl = tk.Label(card, text=help_txt, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                      anchor='w', justify='left')
        hl.pack(fill='x', pady=(6, 0))
        card.bind('<Configure>', lambda e: (hl.configure(wraplength=max(e.width - 30, 100)),
                                            self._homes_warn.configure(
                                                wraplength=max(e.width - 30, 100))))
        self._check_homes_warning()

    def _save_homes(self):
        cfg = self.app.cfg
        cfg['detuks_homes_enabled'] = bool(self._homes_on.get())
        root = self._homes_root.get().strip()
        cfg['detuks_homes_root'] = '' if root == default_homes_root() else root
        save_config(cfg)
        self._check_homes_warning()
        self._invalidate_watcher_dirs()

    def _invalidate_watcher_dirs(self):
        w = getattr(self.app, 'watcher', None)
        if w is not None:
            try:
                w._dirs_last_check = 0
            except Exception:
                pass

    def _check_homes_warning(self):
        if not hasattr(self, '_homes_warn'):
            return
        root = self._homes_root.get().strip() or default_homes_root()
        msg = ''
        if self._homes_on.get() and any(ch.isspace() for ch in root):
            msg = ('⚠ The homes folder contains a space — launches will be refused. '
                   'Choose a folder without spaces (e.g. C:\\DetuksHomes).')
        self._homes_warn.configure(text=msg)

    def _browse_homes(self):
        d = filedialog.askdirectory(title='Select the folder for per-account Detuks homes')
        if d:
            self._homes_root.set(os.path.normpath(d))

    def _copy_profiles(self):
        if not self._homes_on.get():
            messagebox.showinfo('Launcher', 'Turn on per-account Detuks homes first.')
            return
        def _do():
            ok, summary, detail = copy_profiles_to_homes(self.app.cfg)
            def _show():
                text = summary + ('\n\n' + '\n'.join(detail) if detail else '')
                (messagebox.showinfo if ok else messagebox.showwarning)('Copy P2P profiles', text)
                try:
                    self.app._log(f'📋 Copy P2P profiles: {summary}')
                except Exception:
                    pass
            self.app.after(0, _show)
        threading.Thread(target=_do, daemon=True).start()

    def _build_column_header(self, parent):
        app = self.app
        hdr = tk.Frame(parent, bg=app.BG4, padx=12, pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text='👤  ACCOUNT', font=app.SANSS, bg=app.BG4, fg=app.FG2
                 ).pack(side='left')
        btns = tk.Frame(hdr, bg=app.BG4)
        btns.pack(side='right')
        for text in ('▶  LAUNCH', '✏  EDIT', '🗑  DELETE'):
            tk.Label(btns, text=text, font=app.SANSS, bg=app.BG4, fg=app.FG2,
                     width=self.ROW_BTN_WIDTH, anchor='center').pack(side='left')

    def _save_path(self):
        self.app.cfg['launcher_path'] = self._path_var.get().strip()
        save_config(self.app.cfg)

    def _browse_path(self):
        if _IS_WINDOWS:
            title, types = 'Select Detuks launcher.exe', [('Launcher', '*.exe'), ('All files', '*.*')]
        else:
            title, types = 'Select Detuks Client AppImage', [('AppImage', '*.AppImage'), ('All files', '*')]
        initial = None
        if _IS_WINDOWS:
            # Default Detuks install folder on Windows.
            d = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'BotClient')
            if os.path.isdir(d):
                initial = d
        path = filedialog.askopenfilename(title=title, filetypes=types,
                                          **({'initialdir': initial} if initial else {}))
        if path:
            self._path_var.set(os.path.normpath(path))

    # ── Rows ─────────────────────────────────────────────────────────────────────
    def _refresh_rows(self):
        """Full rebuild of the (typically small) set of account rows — cheap
        and simplest-correct against duplicate widgets. Status dots are
        repainted from whatever's already cached in self._status_cache;
        actually re-detecting status is a separate, explicitly-triggered
        background step (see _refresh_status_async), never implied by a
        plain row rebuild."""
        app = self.app
        for w in self._rows_frame.winfo_children():
            if w is not self._empty_lbl:
                w.destroy()
        self._row_widgets = {}

        presets = list_presets(app.cfg)
        self._empty_lbl.pack_forget()
        if not presets:
            self._empty_lbl.pack(pady=30)
            return

        for preset in presets:
            account = preset.get('account', '—')
            self._row_widgets[account] = self._build_row(account)

    def _build_row(self, account):
        app = self.app
        row = tk.Frame(self._rows_frame, bg=app.BG3, pady=6)
        row.pack(fill='x')
        tk.Frame(row, bg=app.BG4, height=1).pack(fill='x', side='bottom')

        select_strip = tk.Frame(row, bg=app.BG3, width=3)
        select_strip.pack(side='left', fill='y')

        info_cell = tk.Frame(row, bg=app.BG3, cursor='hand2')
        info_cell.pack(side='left', fill='x', expand=True, padx=(12, 0))

        avatar = tk.Canvas(info_cell, width=36, height=36, bg=app.BG3, highlightthickness=0)
        avatar.pack(side='left', padx=(0, 10))
        avatar.create_oval(2, 2, 34, 34, fill=app.ACC, outline=app.ACC, tags='circle')
        avatar.create_text(18, 18, text=(account[:1] or '?').upper(), fill=app.BG,
                           font=app.SANSB, tags='letter')
        dot = avatar.create_oval(25, 25, 35, 35, fill=app.FG2, outline=app.BG3, width=1, tags='dot')

        name_lbl = tk.Label(info_cell, text=account, font=app.SANSB, bg=app.BG3, fg=app.FG, anchor='w')
        name_lbl.pack(side='left')

        for w in (info_cell, avatar, name_lbl):
            w.bind('<Button-1>', lambda e, a=account: self._toggle_select(a))

        btns = tk.Frame(row, bg=app.BG3)
        btns.pack(side='right', padx=(0, 12))

        launch_btn = tk.Button(btns, text='▶  Launch', font=app.SANS, bg=app.BG4, fg=app.ACC,
            relief='flat', width=self.ROW_BTN_WIDTH, cursor='hand2',
            command=lambda a=account: self._launch_one(a))
        launch_btn.pack(side='left', padx=(0, 6), ipady=4)

        edit_btn = tk.Button(btns, text='✏  Edit', font=app.SANS, bg=app.BG4, fg=app.ACC2,
            relief='flat', width=self.ROW_BTN_WIDTH, cursor='hand2',
            command=lambda a=account: self._edit_preset(a))
        edit_btn.pack(side='left', padx=(0, 6), ipady=4)

        delete_btn = tk.Button(btns, text='🗑  Delete', font=app.SANS, bg=app.BG4, fg=app.RED,
            relief='flat', width=self.ROW_BTN_WIDTH, cursor='hand2',
            command=lambda a=account: self._delete_preset(a))
        delete_btn.pack(side='left', ipady=4)

        w = {'row': row, 'info_cell': info_cell, 'avatar': avatar, 'dot': dot,
             'select_strip': select_strip,
             'launch_btn': launch_btn, 'edit_btn': edit_btn, 'delete_btn': delete_btn}
        self._apply_status_dot(w, self._status_cache.get(account, 'unknown'))
        if account in self._selected:
            self._paint_selected(w, True)
        return w

    def _apply_status_dot(self, w, status):
        app = self.app
        color = {'open': app.GREEN, 'closed': app.FG2, 'unknown': app.YEL}.get(status, app.FG2)
        w['avatar'].itemconfig(w['dot'], fill=color)

    def _toggle_select(self, account):
        if account in self._selected:
            self._selected.discard(account)
        else:
            self._selected.add(account)
        w = self._row_widgets.get(account)
        if w:
            self._paint_selected(w, account in self._selected)

    def _paint_selected(self, w, selected):
        w['select_strip'].configure(bg=self.app.ACC if selected else self.app.BG3)

    # ── Status detection (background-only, never on the Tk main thread) ────────
    def on_tab_shown(self):
        """Called once when the Launcher tab is selected, and once at
        construction. Status detection involves real process/window
        lookups (discover_account_process), so it's never run inline —
        always handed to a background thread, with results applied back
        via app.after(0, ...)."""
        self._refresh_status_async()

    def _refresh_status_async(self):
        app = self.app
        if self._status_refresh_in_flight:
            return  # a scan is already running — don't stack another
        accounts = [p.get('account', '').strip() for p in list_presets(app.cfg)
                    if p.get('account', '').strip()]
        if not accounts:
            return

        self._status_refresh_in_flight = True

        def _do():
            try:
                results = {}
                for acc in accounts:
                    try:
                        info = discover_account_process(acc)
                        results[acc] = 'open' if info else 'closed'
                    except ValueError:
                        results[acc] = 'unknown'  # ambiguous match — don't guess
                    except Exception:
                        results[acc] = 'unknown'
                app.after(0, lambda: self._apply_status_results(results))
            finally:
                # Always clears, including any unexpected error above — a
                # stuck flag would silently disable all future refreshes.
                self._status_refresh_in_flight = False
        threading.Thread(target=_do, daemon=True).start()

    def _apply_status_results(self, results):
        self._status_cache.update(results)
        for acc, status in results.items():
            w = self._row_widgets.get(acc)
            if w:
                self._apply_status_dot(w, status)

    # ── Launch / Relaunch (smart dispatch, in-flight guarded) ──────────────────
    def _set_in_flight(self, account, flag):
        if flag:
            self._in_flight.add(account)
        else:
            self._in_flight.discard(account)
        w = self._row_widgets.get(account)
        if w:
            w['launch_btn'].configure(state='disabled' if flag else 'normal',
                                       text='⏳  ...' if flag else '▶  Launch')

    def _validate_launcher_path(self, show_error=True):
        """Single source of truth for launcher-path validation (delegates to
        py.launcher.validate_launcher_path — exists, and executable on
        Linux). Returns the path or None. Shows at most one error dialog
        (when show_error=True) — callers that need to validate once for a
        whole batch (Launch Selected) call this themselves before looping,
        then pass the already-confirmed path through so the per-account
        path (_launch_one) never re-validates or re-errors."""
        path, why = validate_launcher_path({'launcher_path': self._path_var.get()})
        if not path and show_error:
            messagebox.showerror('Launcher', why)
        return path

    def _run_launch_op(self, account, op_fn, launcher=None):
        """Shared in-flight-guard + background-thread plumbing for both a
        normal launch (op_fn=launch_account — refuses if already running,
        same as before) and an explicit user-confirmed relaunch
        (op_fn=relaunch_account — only ever reached via the Relaunch
        button in the 'already running' dialog, never automatically)."""
        app = self.app
        if account in self._in_flight:
            return  # in-flight guard — ignore repeated clicks

        if launcher is None:
            launcher = self._validate_launcher_path()
            if launcher is None:
                return

        self._set_in_flight(account, True)

        def _run():
            result = op_fn(app.cfg, account, log_fn=app._log)
            def _show():
                self._set_in_flight(account, False)
                if result.action == 'skipped':
                    if 'already running' in result.message.lower():
                        self._show_already_running_dialog(account, result.message)
                    else:
                        messagebox.showerror('Launcher', result.message)
                elif not result.ok:
                    app._log(f'❌ [{account}] {result.message}')
                self._refresh_status_async()  # reflect the new open/closed state
            app.after(0, _show)
        threading.Thread(target=_run, daemon=True).start()

    def _launch_one(self, account, _path_prevalidated=None):
        self._run_launch_op(account, launch_account, launcher=_path_prevalidated)

    def _relaunch_one(self, account):
        """Only ever called from the explicit Relaunch button below — never
        triggered automatically by a plain Launch click."""
        self._run_launch_op(account, relaunch_account, launcher=None)

    def _show_already_running_dialog(self, account, message):
        """Replaces a plain OK-only error for the specific 'already
        running' case with an explicit choice: Relaunch (close + reopen,
        user-confirmed) or Cancel (do nothing). Any other 'skipped' reason
        (e.g. an ambiguous multiple-window match) still falls back to the
        plain error in _run_launch_op — relaunching can't fix that case
        anyway."""
        app = self.app
        popup = tk.Toplevel(app, bg=app.BG2)
        popup.title('Already Running')
        popup.resizable(False, False)
        popup.transient(app)
        popup.grab_set()

        tk.Label(popup, text='⚠  Already Running', font=app.SANSB, bg=app.BG2, fg=app.YEL,
                 padx=16, pady=10).pack(fill='x', anchor='w')
        tk.Frame(popup, bg=app.BG4, height=1).pack(fill='x')
        msg_lbl = tk.Label(popup, text=message, font=app.SANS, bg=app.BG2, fg=app.FG,
                            wraplength=360, justify='left', padx=16, pady=12, anchor='w')
        msg_lbl.pack(fill='x')
        tk.Label(popup, text='Relaunch here is an immediate manual override — '
                             'it closes and restarts now, ignoring Respect '
                             'Break. Use Discord /relaunch to queue around '
                             'break windows.',
                 font=app.SANSS, bg=app.BG2, fg=app.FG2, wraplength=360,
                 justify='left', padx=16, anchor='w').pack(fill='x', pady=(0, 4))

        btn_row = tk.Frame(popup, bg=app.BG2, padx=16)
        btn_row.pack(fill='x', pady=(0, 14))

        def _do_relaunch():
            popup.destroy()
            self._relaunch_one(account)

        tk.Button(btn_row, text='🔄  Relaunch', font=app.SANSB, bg=app.ACC2, fg=app.BG,
                  relief='flat', padx=14, pady=6, cursor='hand2',
                  command=_do_relaunch).pack(side='left', padx=(0, 8))
        tk.Button(btn_row, text='Cancel', font=app.SANSB, bg=app.BG3, fg=app.FG2,
                  relief='flat', padx=14, pady=6, cursor='hand2',
                  command=popup.destroy).pack(side='left')

    def _launch_selected(self):
        if not self._selected:
            messagebox.showinfo('Launcher', 'Select one or more accounts to launch.')
            return
        # Validate the launcher path exactly once for the whole batch — fixes
        # the repeated-error-dialog issue when multiple accounts are
        # selected and the path is missing/invalid.
        launcher = self._validate_launcher_path()
        if launcher is None:
            return
        for account in list(self._selected):
            self._launch_one(account, _path_prevalidated=launcher)

    # ── Edit / Delete / Add ──────────────────────────────────────────────────────
    def _find_preset_index(self, account):
        presets = self.app.cfg.get('launcher_presets', [])
        return next((i for i, p in enumerate(presets) if p.get('account') == account), None)

    def _delete_preset(self, account):
        presets = self.app.cfg.get('launcher_presets', [])
        idx = self._find_preset_index(account)
        if idx is None:
            return
        if not messagebox.askyesno('Delete Preset',
                                   f'Delete preset for "{account}"?\nThis cannot be undone.'):
            return
        presets.pop(idx)
        self.app.cfg['launcher_presets'] = presets
        save_config(self.app.cfg)
        self._selected.discard(account)
        self._status_cache.pop(account, None)
        self._refresh_rows()

    def _edit_preset(self, account):
        presets = self.app.cfg.get('launcher_presets', [])
        idx = self._find_preset_index(account)
        if idx is None:
            return
        dlg = _PresetDialog(self.app, existing=presets[idx])
        self.frame.wait_window(dlg.window)
        if dlg.result:
            old_account = account
            was_selected = old_account in self._selected

            presets[idx] = dlg.result
            self.app.cfg['launcher_presets'] = presets
            save_config(self.app.cfg)

            new_account = dlg.result.get('account', '').strip()
            if new_account and new_account != old_account:
                # The old name no longer refers to anything real — drop its
                # stale state rather than risk Launch Selected (or an
                # in-flight check) acting on an account that doesn't exist
                # anymore. Cached status isn't transferred to the new name:
                # it was detected for the old window-title match, which may
                # no longer be valid, and a fresh detection is cheap (see
                # _refresh_status_async() below) — dropping it is safe.
                self._selected.discard(old_account)
                self._status_cache.pop(old_account, None)
                self._in_flight.discard(old_account)
                if was_selected:
                    self._selected.add(new_account)

            self._refresh_rows()
            self._refresh_status_async()

    def _open_add_dialog(self):
        dlg = _PresetDialog(self.app)
        self.frame.wait_window(dlg.window)
        if dlg.result:
            presets = self.app.cfg.get('launcher_presets', [])
            presets.append(dlg.result)
            self.app.cfg['launcher_presets'] = presets
            save_config(self.app.cfg)
            self._refresh_rows()
            self._refresh_status_async()


class _PresetDialog:
    """Popup dialog for adding or editing a launch preset (Detuks fields,
    v3.0.0). Live command preview shows the exact, correctly quoted command
    for this platform."""

    def __init__(self, app, existing=None):
        self.app    = app
        self.result = None

        self.window = tk.Toplevel()
        self.window.title('Edit Preset' if existing else 'Add Account')
        self.window.configure(bg=app.BG2)
        self.window.resizable(False, False)
        self.window.grab_set()

        self._vars = {}
        self._build(existing or {})

    def _build(self, p):
        app = self.app
        w   = self.window
        PAD = {'padx': 14, 'pady': 4}

        def row(label, widget_fn, key, default=''):
            frame = tk.Frame(w, bg=app.BG2)
            frame.pack(fill='x', **PAD)
            tk.Label(frame, text=label, font=app.SANS, bg=app.BG2, fg=app.FG,
                     width=22, anchor='w').pack(side='left')
            var = tk.StringVar(value=p.get(key, default))
            self._vars[key] = var
            widget_fn(frame, var)
            var.trace_add('write', lambda *_: self._update_preview())
            return var

        def entry(frame, var):
            tk.Entry(frame, textvariable=var, font=app.SANS,
                     bg=app.BG3, fg=app.FG, insertbackground=app.ACC, relief='flat',
                     width=38).pack(side='left', ipady=3)

        def check(frame, var):
            tk.Checkbutton(frame, variable=var, bg=app.BG2, fg=app.FG,
                           selectcolor=app.BG3, activebackground=app.BG2,
                           onvalue='1', offvalue='').pack(side='left')

        # ── Fields ────────────────────────────────────────────────────────────
        tk.Label(w, text='Basic', font=app.SANSB, bg=app.BG2,
                 fg=app.ACC).pack(fill='x', padx=14, pady=(14, 2))

        row('Account (--jl-account)', entry, 'account')
        tk.Label(w, text='Exact display name saved in the Detuks launcher — case-sensitive. '
                         'Spaces are fine (passed as one argument).',
                 font=app.SANSS, bg=app.BG2, fg=app.FG2, anchor='w', justify='left',
                 wraplength=520).pack(fill='x', padx=14)
        row('Script (--script)',          entry, 'script',         DEFAULT_SCRIPT)
        row('Script profile (--scriptArgs)', entry, 'script_profile', DEFAULT_SCRIPT_PROFILE)

        # Checkboxes
        def bool_row(label, key):
            frame = tk.Frame(w, bg=app.BG2)
            frame.pack(fill='x', **PAD)
            tk.Label(frame, text=label, font=app.SANS, bg=app.BG2, fg=app.FG,
                     width=22, anchor='w').pack(side='left')
            var = tk.StringVar(value='1' if p.get(key) else '')
            self._vars[key] = var
            tk.Checkbutton(frame, variable=var, bg=app.BG2, fg=app.FG,
                           selectcolor=app.BG3, activebackground=app.BG2,
                           onvalue='1', offvalue='',
                           command=self._update_preview).pack(side='left')
            var.trace_add('write', lambda *_: self._update_preview())

        tk.Label(w, text='All Options', font=app.SANSB, bg=app.BG2,
                 fg=app.ACC).pack(fill='x', padx=14, pady=(10, 2))

        bool_row('--software-rendering', 'software_rendering')

        rl_var = row('Settings file (--rl-properties)', entry, 'rl_properties')
        rl_btn_row = tk.Frame(w, bg=app.BG2)
        rl_btn_row.pack(fill='x', padx=14)
        tk.Label(rl_btn_row, text='', width=22, bg=app.BG2).pack(side='left')

        def _browse_rl():
            path = filedialog.askopenfilename(
                title='Select settings.properties',
                filetypes=[('Properties', '*.properties'), ('All files', '*.*')])
            if path:
                rl_var.set(os.path.normpath(path))
        tk.Button(rl_btn_row, text='📁  Browse', font=app.SANSS, bg=app.BG4, fg=app.FG,
                  relief='flat', padx=8, pady=2, cursor='hand2',
                  command=_browse_rl).pack(side='left')
        tk.Label(rl_btn_row, text='  optional — default is the client\'s own settings.properties',
                 font=app.SANSS, bg=app.BG2, fg=app.FG2).pack(side='left')
        main_home = main_home_from_logs_root((app.cfg.get('logs_root') or '').strip())
        if main_home:
            main_rl = os.path.join(main_home, '.detuksosrs', 'settings.properties')
            if os.path.isfile(main_rl):
                use_row = tk.Frame(w, bg=app.BG2)
                use_row.pack(fill='x', padx=14)
                tk.Label(use_row, text='', width=22, bg=app.BG2).pack(side='left')
                tk.Button(use_row, text='Use my main settings', font=app.SANSS, bg=app.BG4,
                          fg=app.FG, relief='flat', padx=8, pady=2, cursor='hand2',
                          command=lambda: rl_var.set(main_rl)).pack(side='left')
                tk.Label(use_row, text=f'  {main_rl}', font=app.SANSS, bg=app.BG2,
                         fg=app.FG2).pack(side='left')

        if app.cfg.get('detuks_homes_enabled', False):
            row('Home folder (optional)', entry, 'home_dir')
            self._home_hint = tk.Label(w, text='', font=app.SANSS, bg=app.BG2, fg=app.FG2,
                                       anchor='w', justify='left', wraplength=520)
            self._home_hint.pack(fill='x', padx=14)

        row('Custom client args',     entry, 'custom')

        # ── Command preview ───────────────────────────────────────────────────
        tk.Label(w, text='Command Preview', font=app.SANSB, bg=app.BG2,
                 fg=app.ACC).pack(fill='x', padx=14, pady=(10, 2))

        prev_frame = tk.Frame(w, bg=app.BG3)
        prev_frame.pack(fill='x', padx=14, pady=(2, 8))
        self._preview = tk.Label(prev_frame, text='', font=app.MONO, bg=app.BG3,
                                 fg=app.ACC2, wraplength=560, justify='left',
                                 padx=8, pady=8, anchor='w')
        self._preview.pack(fill='x')

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = tk.Frame(w, bg=app.BG2)
        btn_frame.pack(fill='x', padx=14, pady=(4, 14))

        tk.Button(btn_frame, text='Add' if not p.get('account') else 'Save',
                  font=app.SANSB, bg=app.GREEN, fg=app.BG,
                  relief='flat', cursor='hand2', command=self._save,
                  padx=14, pady=6).pack(side='left', padx=(0, 8))

        tk.Button(btn_frame, text='Cancel', font=app.SANSB, bg=app.BG3, fg=app.FG2,
                  relief='flat', cursor='hand2', command=self.window.destroy,
                  padx=14, pady=6).pack(side='left')

        self._update_preview()

    def _get_preset(self):
        p = {}
        for key, var in self._vars.items():
            p[key] = var.get()
        # Strip only surrounding whitespace — never case (--jl-account is
        # case-sensitive) and never inner spaces (names may contain spaces).
        for skey in ('account', 'script', 'script_profile', 'rl_properties', 'custom',
                     'home_dir'):
            p[skey] = (p.get(skey) or '').strip()
        p['software_rendering'] = bool(p.get('software_rendering'))
        return p

    def _update_preview(self):
        if not hasattr(self, '_preview'):
            return  # fields still being built
        p    = self._get_preset()
        dflt = r'%LOCALAPPDATA%\BotClient\launcher.exe' if _IS_WINDOWS else '/path/to/DetuksClient.AppImage'
        path = self.app.cfg.get('launcher_path', '').strip() or dflt
        home = account_home(self.app.cfg, p) if p.get('account') else None
        if hasattr(self, '_home_hint'):
            self._home_hint.configure(
                text=(f'Blank = automatic. This account\'s home: {home}' if home
                      else 'Blank = automatic: <homes folder>/<account>'))
        self._preview.configure(text=format_command(build_command(path, p), home))

    def _save(self):
        p = self._get_preset()
        if not p['account']:
            messagebox.showwarning('Launcher', 'Account name is required.')
            return
        if p['account'][:1] in '\'"' or p['account'][-1:] in '\'"':
            messagebox.showwarning(
                'Launcher',
                'Enter the account name without quotes — names with spaces are '
                'already passed as a single argument.')
            return
        home = account_home(self.app.cfg, p)
        if home and validate_home(home):
            messagebox.showwarning('Launcher', validate_home(home))
            return
        if not p.get('home_dir'):
            p.pop('home_dir', None)
        self.result = p
        self.window.destroy()
