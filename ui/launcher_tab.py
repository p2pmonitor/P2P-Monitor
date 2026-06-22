"""
ui/launcher_tab.py — Launcher tab for P2P Monitor (v2.0.0-beta.14 redesign)

Warm dark card-based layout matching the rest of v2.0, built against an
explicit visual reference (column-header row with icons, sage avatar
circles, muted dark buttons with colored icon+text, solid-green Add
Account / Launch Selected, plain inline helper text).

Preserves exactly:
- Config keys: launcher_jar, launcher_presets — same shape, same fields
  per preset (account/script/proxy/mem/covert/nofresh/fresh/
  menu_manipulation/no_click_walk/world/custom/params). _PresetDialog's
  _get_preset()/_build() field set is untouched.
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
  smart_launch(), which dispatches into it.

Per-account in-flight guard: a Launch button disables itself the moment
it's clicked and stays disabled until that account's launch/relaunch
attempt resolves, so repeated clicks (or a "Launch Selected" overlapping
a just-clicked single Launch) can never spawn two operations for the same
account at once.
"""
import os
import shlex
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from py.config   import save_config
from py.launcher import (launch_account, relaunch_account, build_command, list_presets,
                          discover_account_process)


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

        # ── Launcher .jar Path ──────────────────────────────────────────────
        tk.Label(root, text="Launcher .jar Path", font=app.SANSB, bg=app.BG2, fg=app.FG
                 ).pack(anchor='w')
        jar_row = tk.Frame(root, bg=app.BG2)
        jar_row.pack(fill='x', pady=(8, 16))
        self._jar_var = tk.StringVar(value=app.cfg.get('launcher_jar', ''))
        jar_entry = tk.Entry(jar_row, textvariable=self._jar_var, font=app.SANS,
                              bg=app.BG3, fg=app.FG, insertbackground=app.ACC, relief='flat')
        jar_entry.pack(side='left', fill='x', expand=True, ipady=7, padx=(0, 8))
        self._jar_var.trace_add('write', lambda *_: self._save_jar())

        tk.Button(jar_row, text='📁  Browse', font=app.SANSB, bg=app.BG4, fg=app.FG,
                  relief='flat', padx=14, pady=8, cursor='hand2',
                  command=self._browse_jar).pack(side='left', padx=(0, 8))

        tk.Button(jar_row, text='+  Add Account', font=app.SANSB, bg=app.GREEN, fg=app.BG,
                  relief='flat', padx=14, pady=8, cursor='hand2',
                  command=self._open_add_dialog).pack(side='left')

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

        def _sync_scroll(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
            content_h = self._rows_frame.winfo_reqheight()
            visible_h = canvas.winfo_height()
            needs_scroll = content_h > visible_h > 1
            if needs_scroll and not sb.winfo_ismapped():
                sb.pack(side='right', fill='y')
            elif not needs_scroll and sb.winfo_ismapped():
                sb.pack_forget()
                canvas.yview_moveto(0)
        self._rows_frame.bind('<Configure>', _sync_scroll)
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
            "ⓘ  Ensure your account is pre-saved in DreamBot's Account Manager with the exact nickname "
            "used above, and your proxy is pre-saved in DreamBot's Proxy Manager with the exact nickname used above."
        )
        info_lbl = tk.Label(info_row, text=info_text, font=app.SANSS, bg=app.BG2, fg=app.FG2,
                             justify='left', anchor='w')
        info_lbl.pack(fill='x', anchor='w')
        info_row.bind('<Configure>', lambda e: info_lbl.configure(wraplength=max(e.width - 4, 100)))

        self._refresh_rows()

    ROW_BTN_WIDTH = 11  # character width for each of the 3 right-side buttons

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

    def _save_jar(self):
        self.app.cfg['launcher_jar'] = self._jar_var.get().strip()
        save_config(self.app.cfg)

    def _browse_jar(self):
        path = filedialog.askopenfilename(
            title='Select Launcher.jar',
            filetypes=[('JAR files', '*.jar'), ('All files', '*.*')]
        )
        if path:
            self._jar_var.set(os.path.normpath(path))

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

    def _validate_jar_path(self, show_error=True):
        """Single source of truth for jar-path validation. Returns the jar
        path string if valid, or None. Shows at most one error dialog
        (when show_error=True) — callers that need to validate once for a
        whole batch (Launch Selected) call this themselves before looping,
        then pass the already-confirmed path through so the per-account
        path (_launch_one) never re-validates or re-errors."""
        jar = self._jar_var.get().strip()
        if not jar:
            if show_error:
                messagebox.showerror('Launcher', 'Please set the path to Launcher.jar first.')
            return None
        if not os.path.isfile(jar):
            if show_error:
                messagebox.showerror('Launcher',
                    f'Launcher.jar not found at:\n{jar}\n\nPlease check the path in Settings.')
            return None
        return jar

    def _run_launch_op(self, account, op_fn, jar=None):
        """Shared in-flight-guard + background-thread plumbing for both a
        normal launch (op_fn=launch_account — refuses if already running,
        same as before) and an explicit user-confirmed relaunch
        (op_fn=relaunch_account — only ever reached via the Relaunch
        button in the 'already running' dialog, never automatically)."""
        app = self.app
        if account in self._in_flight:
            return  # in-flight guard — ignore repeated clicks

        if jar is None:
            jar = self._validate_jar_path()
            if jar is None:
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

    def _launch_one(self, account, _jar_prevalidated=None):
        self._run_launch_op(account, launch_account, jar=_jar_prevalidated)

    def _relaunch_one(self, account):
        """Only ever called from the explicit Relaunch button below — never
        triggered automatically by a plain Launch click."""
        self._run_launch_op(account, relaunch_account, jar=None)

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
        # Validate the jar path exactly once for the whole batch — fixes
        # the repeated-error-dialog issue when multiple accounts are
        # selected and the jar path is missing/invalid.
        jar = self._validate_jar_path()
        if jar is None:
            return
        for account in list(self._selected):
            self._launch_one(account, _jar_prevalidated=jar)

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
    """Popup dialog for adding or editing a launch preset. Restyled to the
    warm dark palette/sans-serif fonts; the field set, _get_preset()'s
    data shape, and _save()'s validation are byte-for-byte the same as
    before — only colors/fonts changed."""

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

        row('Account (-account)',    entry, 'account')
        row('Script (-script)',      entry, 'script',  'P2P Master AI')
        row('Proxy (-proxy)',        entry, 'proxy')

        # Memory checkbox + number
        mem_frame = tk.Frame(w, bg=app.BG2)
        mem_frame.pack(fill='x', **PAD)
        tk.Label(mem_frame, text='Memory -Xmx (MB)', font=app.SANS,
                 bg=app.BG2, fg=app.FG, width=22, anchor='w').pack(side='left')
        mem_en_var = tk.StringVar(value='1' if p.get('mem') else '')
        self._vars['mem_enabled'] = mem_en_var
        tk.Checkbutton(mem_frame, variable=mem_en_var, bg=app.BG2, fg=app.FG,
                       selectcolor=app.BG3, activebackground=app.BG2,
                       onvalue='1', offvalue='',
                       command=self._update_preview).pack(side='left')
        mem_var = tk.StringVar(value=p.get('mem', '1024'))
        self._vars['mem'] = mem_var
        tk.Entry(mem_frame, textvariable=mem_var, font=app.SANS,
                 bg=app.BG3, fg=app.FG, insertbackground=app.ACC, relief='flat',
                 width=8).pack(side='left', padx=4, ipady=3)
        tk.Label(mem_frame, text='(placed before -jar)',
                 font=app.SANSS, bg=app.BG2, fg=app.FG2).pack(side='left')
        mem_var.trace_add('write', lambda *_: self._update_preview())
        mem_en_var.trace_add('write', lambda *_: self._update_preview())

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

        bool_row('-covert',           'covert')
        bool_row('-nofresh',          'nofresh')

        tk.Label(w, text='All Options', font=app.SANSB, bg=app.BG2,
                 fg=app.ACC).pack(fill='x', padx=14, pady=(10, 2))

        bool_row('-fresh',            'fresh')
        bool_row('-menuManipulation', 'menu_manipulation')
        bool_row('-noClickWalk',      'no_click_walk')
        row('World (-world)',         entry, 'world')
        row('Custom args',            entry, 'custom')
        row('Params (-params, last)', entry, 'params')

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
        # Resolve mem: only set if enabled
        if not p.get('mem_enabled'):
            p['mem'] = ''
        del p['mem_enabled']
        # Convert bools
        for bkey in ('covert', 'nofresh', 'fresh', 'menu_manipulation', 'no_click_walk'):
            p[bkey] = bool(p.get(bkey))
        return p

    def _update_preview(self):
        p   = self._get_preset()
        jar = self.app.cfg.get('launcher_jar', '/path/to/Launcher.jar').strip() or '/path/to/Launcher.jar'
        cmd = build_command(jar, p)
        self._preview.configure(text=' '.join(shlex.quote(c) for c in cmd))

    def _save(self):
        p = self._get_preset()
        if not p.get('account', '').strip():
            messagebox.showwarning('Launcher', 'Account name is required.')
            return
        self.result = p
        self.window.destroy()
