"""
launcher_tab.py — DreamBot CLI Launcher tab for P2P Monitor
Allows saving account launch presets and launching DreamBot instances via CLI.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import threading
import shlex
from py.config       import save_config
from py.platform_ops import is_account_process_running



class LauncherTab:
    def __init__(self, app, frame):
        self.app = app
        self.frame = frame
        self._build()

    def _build(self):
        app = self.app
        f   = self.frame

        # ── Top bar: Launcher.jar path + Add Account ──────────────────────────
        top = tk.Frame(f, bg=app.BG)
        top.pack(fill='x', padx=10, pady=(10, 4))

        tk.Label(top, text='Launcher.jar:', font=app.MONO, bg=app.BG, fg=app.FG).pack(side='left')
        self._jar_var = tk.StringVar(value=app.cfg.get('launcher_jar', ''))
        jar_entry = tk.Entry(top, textvariable=self._jar_var, font=app.MONO,
                             bg=app.BG2, fg=app.FG, insertbackground=app.FG, width=52)
        jar_entry.pack(side='left', padx=(4, 4))
        self._jar_var.trace_add('write', lambda *_: self._save_jar())

        tk.Button(top, text='Browse', font=app.MONO, bg=app.BG2, fg=app.FG,
                  activebackground=app.BG2, activeforeground=app.FG,
                  command=self._browse_jar, relief='flat', cursor='hand2').pack(side='left', padx=(0, 16))

        tk.Button(top, text='+ Add Account', font=app.MONOL, bg='#2d6a2d', fg='white',
                  activebackground='#3a8a3a', activeforeground='white',
                  command=self._open_add_dialog, relief='flat', cursor='hand2',
                  padx=10, pady=4).pack(side='left')

        # ── Preset list ───────────────────────────────────────────────────────
        list_frame = tk.Frame(f, bg=app.BG)
        list_frame.pack(fill='both', expand=True, padx=10, pady=4)

        cols = ('account', 'launch', 'edit', 'delete')
        self._tree = ttk.Treeview(list_frame, columns=cols, show='headings',
                                  selectmode='extended', height=16)
        self._tree.heading('account', text='Account')
        self._tree.heading('launch',  text='')
        self._tree.heading('edit',    text='')
        self._tree.heading('delete',  text='')
        self._tree.column('account', width=400, anchor='w')
        self._tree.column('launch',  width=90,  anchor='center')
        self._tree.column('edit',    width=80,  anchor='center')
        self._tree.column('delete',  width=80,  anchor='center')

        vsb = ttk.Scrollbar(list_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        self._tree.bind('<ButtonRelease-1>', self._on_tree_click)

        # ── Bottom bar: Launch Selected + info ───────────────────────────────
        bot = tk.Frame(f, bg=app.BG)
        bot.pack(fill='x', padx=10, pady=(4, 0))

        tk.Button(bot, text='▶  Launch Selected', font=app.MONOL,
                  bg='#1a5276', fg='white',
                  activebackground='#2e86c1', activeforeground='white',
                  command=self._launch_selected, relief='flat', cursor='hand2',
                  padx=10, pady=4).pack(side='left')

        # ── Info bar ─────────────────────────────────────────────────────────
        info_frame = tk.Frame(f, bg=app.BG2, relief='flat')
        info_frame.pack(fill='x', padx=10, pady=(6, 6))

        info_text = (
            "ℹ  Ensure your account is pre-saved in DreamBot's Account Manager with the exact nickname "
            "used above, and your proxy is pre-saved in DreamBot's Proxy Manager with the exact nickname used above."
        )
        tk.Label(info_frame, text=info_text, font=app.MONO, bg=app.BG2, fg=app.FG2,
                 wraplength=820, justify='left', padx=8, pady=6).pack(fill='x')

        self._refresh_tree()

    def _save_jar(self):
        self.app.cfg['launcher_jar'] = self._jar_var.get().strip()
        save_config(self.app.cfg)

    def _browse_jar(self):
        path = filedialog.askopenfilename(
            title='Select Launcher.jar',
            filetypes=[('JAR files', '*.jar'), ('All files', '*.*')]
        )
        if path:
            import os
            self._jar_var.set(os.path.normpath(path))

    def _refresh_tree(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        presets = self.app.cfg.get('launcher_presets', [])
        for i, p in enumerate(presets):
            self._tree.insert('', 'end', iid=str(i),
                              values=(p.get('account', '—'),
                                      '[ Launch ]', '[ Edit ]', '[ Delete ]'))

    def _on_tree_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region != 'cell':
            # Click outside rows — deselect
            self._tree.selection_remove(self._tree.selection())
            return
        col  = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if not item:
            return
        idx = int(item)
        if col == '#2':   # Launch
            self._tree.selection_remove(self._tree.selection())
            self._launch_one(idx)
        elif col == '#3': # Edit
            self._tree.selection_remove(self._tree.selection())
            self._edit_preset(idx)
        elif col == '#4': # Delete
            self._tree.selection_remove(self._tree.selection())
            self._delete_preset(idx)
        # Account column (#1) — allow selection to persist for Launch Selected

    def _launch_one(self, idx):
        presets = self.app.cfg.get('launcher_presets', [])
        if idx >= len(presets):
            return
        self._do_launch(presets[idx])

    def _launch_selected(self):
        selected = self.app.cfg.get('launcher_presets', [])
        items    = self._tree.selection()
        if not items:
            messagebox.showinfo('Launcher', 'Select one or more accounts to launch.')
            return
        for item in items:
            idx = int(item)
            if idx < len(selected):
                self._do_launch(selected[idx])

    def _do_launch(self, preset):
        jar = self._jar_var.get().strip()
        if not jar:
            messagebox.showerror('Launcher', 'Please set the path to Launcher.jar first.')
            return

        account = preset.get('account', '?')

        # Check if this account is already running
        try:
            if is_account_process_running(account, jar_path=jar):
                messagebox.showerror('Already Running',
                    f'"{account}" appears to already be running.\n'
                    f'Close the existing client before launching again.')
                return
        except Exception:
            pass  # allow launch if check fails

        cmd = _build_command(jar, preset)
        self.app._log(f'🚀 [{account}] Launching: {" ".join(shlex.quote(c) for c in cmd)}')

        def _run():
            try:
                import sys
                kwargs = dict(stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=True)
                if sys.platform == 'win32':
                    # Suppress the blank CMD window that appears on Windows
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                subprocess.Popen(cmd, **kwargs)
                self.app.after(0, lambda: self.app._log(f'✅ [{account}] Launch started.'))
            except Exception as e:
                self.app.after(0, lambda: self.app._log(f'❌ [{account}] Launch failed: {e}'))

        threading.Thread(target=_run, daemon=True).start()

    def _delete_preset(self, idx):
        presets = self.app.cfg.get('launcher_presets', [])
        if idx >= len(presets):
            return
        name = presets[idx].get('account', '?')
        if not messagebox.askyesno('Delete Preset',
                                   f'Delete preset for "{name}"?\nThis cannot be undone.'):
            return
        presets.pop(idx)
        self.app.cfg['launcher_presets'] = presets
        save_config(self.app.cfg)
        self._refresh_tree()

    def _edit_preset(self, idx):
        presets = self.app.cfg.get('launcher_presets', [])
        if idx >= len(presets):
            return
        dlg = _PresetDialog(self.app, existing=presets[idx])
        self.frame.wait_window(dlg.window)
        if dlg.result:
            presets[idx] = dlg.result
            self.app.cfg['launcher_presets'] = presets
            save_config(self.app.cfg)
            self._refresh_tree()

    def _open_add_dialog(self):
        dlg = _PresetDialog(self.app)
        self.frame.wait_window(dlg.window)
        if dlg.result:
            presets = self.app.cfg.get('launcher_presets', [])
            presets.append(dlg.result)
            self.app.cfg['launcher_presets'] = presets
            save_config(self.app.cfg)
            self._refresh_tree()


def _build_command(jar, preset):
    """Build the java launch command list from a preset dict."""
    cmd = []

    mem = preset.get('mem', '')
    if mem:
        try:
            m = int(mem)
            cmd += ['java', f'-Xmx{m}M', '-jar', jar]
        except ValueError:
            cmd += ['java', '-jar', jar]
    else:
        cmd += ['java', '-jar', jar]

    script = preset.get('script', 'P2P Master AI').strip()
    if script:
        cmd += ['-script', script]

    account = preset.get('account', '').strip()
    if account:
        cmd += ['-account', account]

    proxy = preset.get('proxy', '').strip()
    if proxy:
        cmd += ['-proxy', proxy]

    if preset.get('covert'):
        cmd.append('-covert')
    if preset.get('nofresh'):
        cmd.append('-nofresh')
    if preset.get('fresh'):
        cmd.append('-fresh')
    if preset.get('menu_manipulation'):
        cmd.append('-menuManipulation')
    if preset.get('no_click_walk'):
        cmd.append('-noClickWalk')

    world = preset.get('world', '').strip()
    if world:
        cmd += ['-world', world]

    custom = preset.get('custom', '').strip()
    if custom:
        # Parse custom args safely
        try:
            cmd += shlex.split(custom)
        except ValueError:
            cmd.append(custom)

    params = preset.get('params', '').strip()
    if params:
        cmd += ['-params', params]

    return cmd


class _PresetDialog:
    """Popup dialog for adding or editing a launch preset."""

    def __init__(self, app, existing=None):
        self.app    = app
        self.result = None

        self.window = tk.Toplevel()
        self.window.title('Edit Preset' if existing else 'Add Account')
        self.window.configure(bg=app.BG)
        self.window.resizable(False, False)
        self.window.grab_set()

        self._vars = {}
        self._build(existing or {})

    def _build(self, p):
        app = self.app
        w   = self.window
        PAD = {'padx': 10, 'pady': 3}

        def row(label, widget_fn, key, default=''):
            frame = tk.Frame(w, bg=app.BG)
            frame.pack(fill='x', **PAD)
            tk.Label(frame, text=label, font=app.MONO, bg=app.BG, fg=app.FG,
                     width=22, anchor='w').pack(side='left')
            var = tk.StringVar(value=p.get(key, default))
            self._vars[key] = var
            widget_fn(frame, var)
            var.trace_add('write', lambda *_: self._update_preview())
            return var

        def entry(frame, var):
            tk.Entry(frame, textvariable=var, font=app.MONO,
                     bg=app.BG2, fg=app.FG, insertbackground=app.FG,
                     width=38).pack(side='left')

        def check(frame, var):
            tk.Checkbutton(frame, variable=var, bg=app.BG, fg=app.FG,
                           selectcolor=app.BG2, activebackground=app.BG,
                           onvalue='1', offvalue='').pack(side='left')

        # ── Fields ────────────────────────────────────────────────────────────
        tk.Label(w, text='─── Basic ───', font=app.MONO, bg=app.BG,
                 fg=app.FG2).pack(fill='x', padx=10, pady=(10, 0))

        row('Account (-account)',    entry, 'account')
        row('Script (-script)',      entry, 'script',  'P2P Master AI')
        row('Proxy (-proxy)',        entry, 'proxy')

        # Memory checkbox + number
        mem_frame = tk.Frame(w, bg=app.BG)
        mem_frame.pack(fill='x', **PAD)
        tk.Label(mem_frame, text='Memory -Xmx (MB)', font=app.MONO,
                 bg=app.BG, fg=app.FG, width=22, anchor='w').pack(side='left')
        mem_en_var = tk.StringVar(value='1' if p.get('mem') else '')
        self._vars['mem_enabled'] = mem_en_var
        tk.Checkbutton(mem_frame, variable=mem_en_var, bg=app.BG, fg=app.FG,
                       selectcolor=app.BG2, activebackground=app.BG,
                       onvalue='1', offvalue='',
                       command=self._update_preview).pack(side='left')
        mem_var = tk.StringVar(value=p.get('mem', '1024'))
        self._vars['mem'] = mem_var
        tk.Entry(mem_frame, textvariable=mem_var, font=app.MONO,
                 bg=app.BG2, fg=app.FG, insertbackground=app.FG,
                 width=8).pack(side='left', padx=4)
        tk.Label(mem_frame, text='(placed before -jar)',
                 font=app.MONO, bg=app.BG, fg=app.FG2).pack(side='left')
        mem_var.trace_add('write', lambda *_: self._update_preview())
        mem_en_var.trace_add('write', lambda *_: self._update_preview())

        # Checkboxes
        def bool_row(label, key):
            frame = tk.Frame(w, bg=app.BG)
            frame.pack(fill='x', **PAD)
            tk.Label(frame, text=label, font=app.MONO, bg=app.BG, fg=app.FG,
                     width=22, anchor='w').pack(side='left')
            var = tk.StringVar(value='1' if p.get(key) else '')
            self._vars[key] = var
            tk.Checkbutton(frame, variable=var, bg=app.BG, fg=app.FG,
                           selectcolor=app.BG2, activebackground=app.BG,
                           onvalue='1', offvalue='',
                           command=self._update_preview).pack(side='left')
            var.trace_add('write', lambda *_: self._update_preview())

        bool_row('-covert',           'covert')
        bool_row('-nofresh',          'nofresh')

        tk.Label(w, text='─── All Options ───', font=app.MONO, bg=app.BG,
                 fg=app.FG2).pack(fill='x', padx=10, pady=(10, 0))

        bool_row('-fresh',            'fresh')
        bool_row('-menuManipulation', 'menu_manipulation')
        bool_row('-noClickWalk',      'no_click_walk')
        row('World (-world)',         entry, 'world')
        row('Custom args',            entry, 'custom')
        row('Params (-params, last)', entry, 'params')

        # ── Command preview ───────────────────────────────────────────────────
        tk.Label(w, text='─── Command Preview ───', font=app.MONO, bg=app.BG,
                 fg=app.FG2).pack(fill='x', padx=10, pady=(10, 0))

        prev_frame = tk.Frame(w, bg=app.BG2)
        prev_frame.pack(fill='x', padx=10, pady=(2, 6))
        self._preview = tk.Label(prev_frame, text='', font=app.MONO, bg=app.BG2,
                                 fg='#7ec8e3', wraplength=560, justify='left',
                                 padx=6, pady=6, anchor='w')
        self._preview.pack(fill='x')

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = tk.Frame(w, bg=app.BG)
        btn_frame.pack(fill='x', padx=10, pady=(4, 10))

        tk.Button(btn_frame, text='Add' if not p.get('account') else 'Save',
                  font=app.MONOL, bg='#2d6a2d', fg='white',
                  activebackground='#3a8a3a', activeforeground='white',
                  command=self._save, relief='flat', cursor='hand2',
                  padx=12, pady=4).pack(side='left', padx=(0, 8))

        tk.Button(btn_frame, text='Cancel', font=app.MONOL, bg=app.BG2, fg=app.FG,
                  activebackground=app.BG2, activeforeground=app.FG,
                  command=self.window.destroy, relief='flat', cursor='hand2',
                  padx=12, pady=4).pack(side='left')

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
        cmd = _build_command(jar, p)
        self._preview.configure(text=' '.join(shlex.quote(c) for c in cmd))

    def _save(self):
        p = self._get_preset()
        if not p.get('account', '').strip():
            messagebox.showwarning('Launcher', 'Account name is required.')
            return
        self.result = p
        self.window.destroy()
