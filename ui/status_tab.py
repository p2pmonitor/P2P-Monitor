"""
ui/status_tab.py — Status tab for P2P Monitor (v2.0.0-beta.11 redesign)

Replaces the single ttk.Treeview with custom Frame-based account-row
cards — necessary to match the mockup's real button widgets, colored
status badges, and avatar circles, none of which a Treeview cell can
render. Safe performance-wise: account counts here are small (a handful
of concurrently-monitored accounts), nothing like History's potentially-
hundreds-of-events-per-account scale.

Preserves exactly:
- refresh() / push_refresh() / on_tab_shown() — same names, same no-arg
  signatures, same threading pattern (background thread for the watcher
  call, app.after(0, ...) to hop back to the Tk thread). p2p_monitor.py's
  three call sites are untouched.
- The smart-diff update pattern: if the account set is unchanged, existing
  rows are updated in place (no destroy/recreate); a full rebuild only
  happens when accounts are added/removed. This is what keeps "no
  duplicate widgets" true across repeated refreshes.
- _tick_uptime()'s 60s pure-math ticker via get_uptime_rows() — still no
  threads, no I/O, just updating two label texts per row in place.
- Mute / Screenshot / double-click-for-history actions call the exact same
  watcher.toggle_mute() / watcher.trigger_screenshot() / app.show_tab(...)
  + app._history.focus_account(...) as before — now via real per-row
  Button widgets and a bound Label instead of treeview column-position
  click detection, which is a robustness improvement, not a behavior one.

Adds, purely additively, a Session Overview card (mirrors Monitor's —
same Status/Uptime/Started/Events fields, same app._session_start_ts/
_counts/_highlights source data) and an Accounts Overview card — both
read-only summaries of data that already exists.
"""
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime

# Minimum genuine overflow (in px) before a scroll container shows its
# scrollbar — without this, even a few pixels of rounding/measurement
# noise (which happens routinely across different font metrics, e.g.
# Windows vs Linux) triggers a scrollbar that barely moves and serves
# no purpose. Only real, meaningful overflow should ever scroll.
_SCROLL_TOLERANCE_PX = 16


class StatusTab:
    def __init__(self, app, parent_frame):
        self.app = app
        self._refresh_in_flight = False  # prevents thread accumulation
        self._row_widgets = {}           # account -> dict of widget refs
        self._build(parent_frame)
        self._tick_uptime()

    # ── Build ──────────────────────────────────────────────────────────────────
    def _build(self, f):
        app = self.app
        root = tk.Frame(f, bg=app.BG2, padx=16, pady=16)
        root.pack(fill='both', expand=True)

        right = tk.Frame(root, bg=app.BG2)
        right.pack(side='left', fill='both', expand=True)

        self._build_stat_strip(right)
        self._build_account_table(right)

    def _card(self, parent, title=None, icon=None):
        app = self.app
        card = tk.Frame(parent, bg=app.BG3, padx=14, pady=12)
        card.pack(fill='x', pady=(0, 12))
        if title:
            hdr = tk.Frame(card, bg=app.BG3)
            hdr.pack(fill='x', anchor='w', pady=(0, 8))
            if icon:
                tk.Label(hdr, text=icon, font=(app.SANS[0], 12), bg=app.BG3, fg=app.ACC
                         ).pack(side='left', padx=(0, 6))
            tk.Label(hdr, text=title, font=app.SANSS, bg=app.BG3, fg=app.FG2
                     ).pack(side='left')
        return card

    # ── Session Overview (mirrors Monitor's — same source data) ─────────────────
    # ── Stat strip ────────────────────────────────────────────────────────────────
    def _build_stat_strip(self, parent):
        app = self.app
        strip = tk.Frame(parent, bg=app.BG2)
        strip.pack(fill='x', pady=(0, 12))
        self._stat_widgets = {}
        specs = [
            ('active',    "ACTIVE ACCOUNTS", '👤', app.GREEN),
            ('on_break',  "ON BREAK",         '☕', app.YEL),
            ('logged_in', "LOGGED IN",        '✅', app.GREEN),
            ('muted',     "MUTED",            '🔇', app.FG2),
        ]
        for key, label, icon, color in specs:
            cell = tk.Frame(strip, bg=app.BG3, padx=10, pady=8)
            cell.pack(side='left', fill='x', expand=True, padx=(0, 8))
            top = tk.Frame(cell, bg=app.BG3)
            top.pack(fill='x', anchor='w')
            tk.Label(top, text=icon, font=(app.SANS[0], 12), bg=app.BG3, fg=color
                     ).pack(side='left', padx=(0, 4))
            tk.Label(top, text=label, font=app.SANSS, bg=app.BG3, fg=app.FG2
                     ).pack(side='left')
            row = tk.Frame(cell, bg=app.BG3)
            row.pack(fill='x', anchor='w', pady=(4, 0))
            count_lbl = tk.Label(row, text="0", font=(app.SANS[0], 18, 'bold'),
                                  bg=app.BG3, fg=color)
            count_lbl.pack(side='left')
            pct_lbl = tk.Label(row, text="0%", font=app.SANSS, bg=app.BG3, fg=app.FG2)
            pct_lbl.pack(side='left', padx=(8, 0), pady=(6, 0))
            self._stat_widgets[key] = (count_lbl, pct_lbl)

    # ── Per-account live status table ────────────────────────────────────────────
    def _build_account_table(self, parent):
        app = self.app
        card = tk.Frame(parent, bg=app.BG3, padx=14, pady=12)
        card.pack(fill='both', expand=True)

        hdr = tk.Frame(card, bg=app.BG3)
        hdr.pack(fill='x', pady=(0, 8))
        tk.Label(hdr, text="📊", font=(app.SANS[0], 12), bg=app.BG3, fg=app.ACC
                 ).pack(side='left', padx=(0, 6))
        tk.Label(hdr, text="PER-ACCOUNT LIVE STATUS", font=app.SANSB, bg=app.BG3, fg=app.FG
                 ).pack(side='left')
        self._last_updated_lbl = tk.Label(hdr, text="", font=app.SANSS, bg=app.BG3, fg=app.FG2)
        self._last_updated_lbl.pack(side='right', padx=(8, 0))
        tk.Button(hdr, text="↻ Refresh", font=app.SANSS, bg=app.BG4, fg=app.ACC,
            relief='flat', padx=8, pady=3, cursor='hand2',
            command=self.refresh).pack(side='right')

        col_hdr = tk.Frame(card, bg=app.BG3)
        col_hdr.pack(fill='x', pady=(0, 4))
        # Calibrated against the actual rendered pixel widths of the row
        # values below (which use app.SANS, a different/larger font than
        # this header's app.SANSS) — same declared character count in two
        # different fonts doesn't render to the same pixel width, which
        # was the real cause of the header/row column misalignment. The
        # ACCOUNT column specifically also has a units mismatch on top of
        # that: name_cell below is sized in raw pixels (170), not characters.
        # The Mute/Screenshot button area is now right-pegged in the row
        # (see _build_row) — these two header placeholders mirror that
        # with plain pixel-width frames rather than character-width
        # labels, for the same reason the ACCOUNT column needed pixel
        # units instead of characters: button widths aren't expressible
        # in character counts that'd actually match. Sized to the wider
        # ("🔇 Unmute") state plus its gap, so the header's blank space
        # comfortably covers either mute-state width without drifting
        # row to row.
        tk.Frame(col_hdr, bg=app.BG3, width=101).pack(side='right', padx=(0, 6))
        tk.Frame(col_hdr, bg=app.BG3, width=80).pack(side='right', padx=(0, 3))

        # Pixel-width frames, not character-width labels — matches the
        # ACCOUNT column's and the Mute/Screenshot placeholders' existing
        # approach above, now applied to every column. Character-count
        # widths can never truly align across two different fonts (this
        # header uses SANSS, the row values below use SANS) — the same
        # declared width renders to a different pixel width in each, which
        # is exactly what kept drifting on Windows even after recalibrating
        # the character counts twice. Pixel widths are deterministic
        # regardless of font metrics, so this is the actual fix rather
        # than a third guess at new character-count numbers. Values below
        # are the exact measured reqwidth of each corresponding row widget
        # in _build_row (task_lbl=112, activity_lbl=148, uptime_lbl=76,
        # break_lbl=67, badge_wrap=102) — keep these in sync if those ever
        # change.
        for text, pw in [("ACCOUNT", 170), ("TASK", 112), ("ACTIVITY", 148),
                         ("UPTIME", 76), ("BREAK", 67), ("STATUS", 102)]:
            cell = tk.Frame(col_hdr, bg=app.BG3, width=pw, height=19)
            cell.pack_propagate(False)
            cell.pack(side='left', padx=(0, 4))
            tk.Label(cell, text=text, font=app.SANSS, bg=app.BG3, fg=app.FG2,
                     anchor='w').pack(anchor='w', fill='x')

        # Canvas+Scrollbar wrap — same pattern as History/Launcher/Settings.
        # Without this, the row list (one row per monitored account, no
        # upper bound) forces the whole app window taller as accounts are
        # added, exactly like the Goals & Maxing table did before its own
        # fix — this was the one tab that had never gotten this treatment.
        outer = tk.Frame(card, bg=app.BG3)
        outer.pack(fill='both', expand=True)
        canvas = tk.Canvas(outer, bg=app.BG3, highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        self._scroll_canvas = canvas
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        self._scrollbar = sb
        self._rows_container = tk.Frame(canvas, bg=app.BG3)
        win = canvas.create_window((0, 0), window=self._rows_container, anchor='nw')

        needs_scroll = False

        def _sync_scroll(_e=None):
            nonlocal needs_scroll
            canvas.configure(scrollregion=canvas.bbox('all'))
            content_h = self._rows_container.winfo_reqheight()
            visible_h = canvas.winfo_height()
            needs_scroll = (content_h - visible_h) > _SCROLL_TOLERANCE_PX and visible_h > 1
            if needs_scroll and not sb.winfo_ismapped():
                sb.pack(side='right', fill='y')
            elif not needs_scroll and sb.winfo_ismapped():
                sb.pack_forget()
                canvas.yview_moveto(0)
        self._rows_container.bind('<Configure>', _sync_scroll)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfig(win, width=e.width), _sync_scroll()))

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

        self._empty_lbl = tk.Label(self._rows_container,
            text="No accounts yet — start monitoring to see live status here.",
            font=app.SANS, bg=app.BG3, fg=app.FG2)

        footer = tk.Frame(parent, bg=app.BG2, pady=6)
        footer.pack(fill='x')
        footer_lbl = tk.Label(footer,
            text="ⓘ  Mute silences the account  •  Screenshot captures on-demand  •  "
                 "Double-click account name to open history",
            font=app.SANSS, bg=app.BG2, fg=app.FG2, justify='center')
        footer_lbl.pack(fill='x')
        footer.bind('<Configure>', lambda e: footer_lbl.configure(wraplength=max(e.width - 8, 100)))

    def _status_color(self, status_text):
        app = self.app
        if 'Offline' in status_text:
            return app.RED
        if 'On Break' in status_text:
            return app.YEL
        if 'Starting' in status_text:
            return app.YEL
        return app.GREEN  # Logged In

    @staticmethod
    def _clip(text, n):
        """Hard-cap text to n characters with an ellipsis. Without this, a
        long task/activity name has nothing stopping it from growing past
        its label's nominal width — which used to just make the whole
        window wider (the original bug), but now that this row lives
        inside a fixed-width Canvas, an overlong label would instead push
        Mute/Screenshot off the visible edge with no horizontal scroll to
        recover them. Capping the text itself keeps the row's total width
        bounded and predictable regardless of what the data contains."""
        text = str(text)
        return text if len(text) <= n else text[:max(0, n - 1)] + '…'

    def _build_row(self, account, r):
        app = self.app
        row = tk.Frame(self._rows_container, bg=app.BG3, pady=6)
        row.pack(fill='x')
        tk.Frame(row, bg=app.BG4, height=1).pack(fill='x', side='bottom')

        # Mute/Screenshot are packed first with side='right' so they're
        # always pinned to the row's right edge — packed in reverse
        # visual order (Screenshot first, ends up rightmost; Mute second,
        # lands to its left) so they still read left-to-right as
        # Mute/Screenshot. This matters beyond just consistency with the
        # header: mute_btn's own text changes ("🔇 Unmute" vs "🔊 Mute")
        # depending on mute state, so under the old sequential left-to-
        # right pack, two rows could have their buttons start at two
        # different x-positions for no reason other than one account
        # happening to be muted — a real per-row misalignment, not just a
        # font-metric difference. Right-pinning makes every row's buttons
        # land in the exact same place regardless of that.
        muted = bool(r.get('muted'))
        ss_btn = tk.Button(row, text="📷 Screenshot", font=app.SANSS,
            bg=app.BG4, fg=app.ACC, relief='flat', padx=3, pady=3,
            cursor='hand2', command=lambda a=account: self._on_screenshot_click(a))
        ss_btn.pack(side='right', padx=(0, 6))

        mute_btn = tk.Button(row, text=("🔇 Unmute" if muted else "🔊 Mute"), font=app.SANSS,
            bg=app.BG4, fg=(app.FG2 if muted else app.ACC), relief='flat', padx=3, pady=3,
            cursor='hand2', command=lambda a=account: self._on_mute_click(a))
        mute_btn.pack(side='right', padx=(0, 3))

        # Avatar circle + name (double-click → history)
        name_cell = tk.Frame(row, bg=app.BG3, width=170)
        name_cell.pack(side='left', fill='y', padx=(0, 4))
        name_cell.pack_propagate(False)
        avatar = tk.Canvas(name_cell, width=24, height=24, bg=app.BG3, highlightthickness=0)
        avatar.pack(side='left', padx=(0, 6))
        avatar.create_oval(2, 2, 22, 22, fill=app.ACC, outline=app.ACC)
        avatar.create_text(12, 12, text=(account[:1] or '?').upper(), fill=app.BG, font=app.SANSB)
        text_col = tk.Frame(name_cell, bg=app.BG3, cursor='hand2')
        text_col.pack(side='left', fill='both', expand=True)
        name_lbl = tk.Label(text_col, text=self._clip(account, 18), font=app.SANSB, bg=app.BG3, fg=app.ACC,
                             cursor='hand2', anchor='w')
        name_lbl.pack(anchor='w', fill='x')
        for w in (name_lbl, text_col):
            w.bind('<Double-1>', lambda e, a=account: self._open_history(a))

        # Pixel-width frames, not character-width labels — must match
        # col_hdr's header cells exactly (same reasoning: a character
        # count renders to a different pixel width on a different font/
        # platform, so only fixed pixel widths can guarantee the row
        # stays aligned with the header on Windows the same way it does
        # here). name_cell above and badge_wrap below already used this
        # approach; task/activity/uptime/break now match.
        def _fixed_cell(parent, width, text, fg):
            cell = tk.Frame(parent, bg=app.BG3, width=width)
            cell.pack_propagate(False)
            cell.pack(side='left', fill='y', padx=(0, 4))
            lbl = tk.Label(cell, text=text, font=app.SANS, bg=app.BG3, fg=fg, anchor='w')
            lbl.pack(anchor='w', fill='x')
            return lbl

        task_lbl = _fixed_cell(row, 112, self._clip(r['task'], 12), app.FG)
        activity_lbl = _fixed_cell(row, 148, self._clip(r['activity'], 16), app.FG2)
        uptime_lbl = _fixed_cell(row, 76, r.get('uptime', '—'), app.FG)
        break_lbl = _fixed_cell(row, 67, r.get('break_time', '—'), app.FG)

        status_text = r['status'].split(' ', 1)[-1] if ' ' in r['status'] else r['status']
        badge_color = self._status_color(r['status'])
        badge_wrap = tk.Frame(row, bg=app.BG3, width=102)
        badge_wrap.pack(side='left', padx=(0, 4))
        badge = tk.Label(badge_wrap, text=f" {status_text} ", font=app.SANSB,
                          bg=badge_color, fg=app.BG, padx=6, pady=2)
        badge.pack(anchor='w')

        return {'frame': row, 'task': task_lbl, 'activity': activity_lbl,
                'uptime': uptime_lbl, 'break_time': break_lbl, 'badge': badge,
                'mute_btn': mute_btn, 'screenshot_btn': ss_btn}

    # ── Actions ────────────────────────────────────────────────────────────────────
    def _on_mute_click(self, account):
        app = self.app
        if not app.watcher:
            return
        app.watcher.toggle_mute(account)
        self._flash_row(account)
        self.refresh()

    def _on_screenshot_click(self, account):
        app = self.app
        if not app.watcher:
            return
        app.watcher.trigger_screenshot(account)
        self._flash_row(account)
        self.refresh()

    def _open_history(self, account):
        app = self.app
        app.show_tab('History')
        app.after(50, lambda: app._history.focus_account(account))

    def _flash_row(self, account):
        app = self.app
        w = self._row_widgets.get(account)
        if not w:
            return
        try:
            orig_bg = app.BG3
            w['frame'].configure(bg=app.ACC)
            for child in w['frame'].winfo_children():
                try:
                    child.configure(bg=app.ACC)
                except tk.TclError:
                    pass
            app.after(200, lambda: self._restore_row_bg(account, orig_bg))
        except Exception:
            pass

    def _restore_row_bg(self, account, bg):
        w = self._row_widgets.get(account)
        if not w:
            return
        app = self.app
        try:
            w['frame'].configure(bg=bg)
            for child in w['frame'].winfo_children():
                try:
                    if child not in (w['badge'], w['mute_btn'], w.get('screenshot_btn')):
                        child.configure(bg=bg)
                except tk.TclError:
                    pass

            # Restore action buttons after flash so their text never stays
            # invisible — _flash_row() forces every child's bg to app.ACC,
            # and these two were deliberately excluded from the loop above
            # (their background is always app.BG4 regardless of mute
            # state, never the generic row bg), but nothing previously
            # restored them afterward at all. mute_btn's fg can itself be
            # app.ACC (unmuted state) — left at bg=app.ACC, that's
            # foreground-equals-background, which is what made the text
            # disappear.
            w['mute_btn'].configure(bg=app.BG4)
            if w.get('screenshot_btn'):
                w['screenshot_btn'].configure(bg=app.BG4)
        except Exception:
            pass

    # ── Lightweight uptime tick — pure math, no I/O ────────────────────────────
    def _tick_uptime(self):
        """Every 60 seconds recalculate uptime/break text from cached state.
        No threads, no watcher calls beyond the one cheap get_uptime_rows()
        — just label-text updates on existing row widgets."""
        if self.app.watcher:
            try:
                rows = self.app.watcher.get_uptime_rows()
                self.app.after(0, lambda: self._update_uptime_cols(rows))
            except Exception:
                pass
        self.app.after(60000, self._tick_uptime)

    def _update_uptime_cols(self, rows):
        for r in rows:
            w = self._row_widgets.get(r['account'])
            if w:
                w['uptime'].configure(text=r['uptime'])
                w['break_time'].configure(text=r['break_time'])

    # ── Push-based full refresh — called by watcher events and manual refresh ──
    def refresh(self):
        """Full refresh — checks active sessions then rebuilds rows."""
        app = self.app
        if not app.watcher:
            self._update_rows([])
            return
        def _do():
            try:
                app.watcher.check_active_sessions()
            except Exception:
                pass
            rows = app.watcher.get_account_rows()
            app.after(0, lambda: self._update_rows(rows))
        threading.Thread(target=_do, daemon=True).start()

    def push_refresh(self):
        """Lightweight push from watcher events — no check_active_sessions.
        Guarded by _refresh_in_flight to prevent thread accumulation."""
        app = self.app
        if not app.watcher:
            return
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        def _do():
            try:
                rows = app.watcher.get_account_rows()
                app.after(0, lambda: self._update_rows(rows))
            finally:
                self._refresh_in_flight = False
        threading.Thread(target=_do, daemon=True).start()

    def on_tab_shown(self):
        """Called when Status tab is selected — full refresh."""
        self.refresh()

    def _update_rows(self, rows):
        """Smart diff: only a full rebuild when the account *set* changed;
        otherwise every existing row is updated in place — this is what
        keeps repeated refreshes from duplicating widgets."""
        app = self.app
        new_accounts = {r['account'] for r in rows}
        existing_accounts = set(self._row_widgets.keys())

        if new_accounts != existing_accounts:
            for w in self._row_widgets.values():
                w['frame'].destroy()
            self._row_widgets = {}
            for r in rows:
                self._row_widgets[r['account']] = self._build_row(r['account'], r)
        else:
            for r in rows:
                w = self._row_widgets.get(r['account'])
                if not w:
                    continue
                w['task'].configure(text=r['task'])
                w['activity'].configure(text=r['activity'])
                w['uptime'].configure(text=r.get('uptime', '—'))
                w['break_time'].configure(text=r.get('break_time', '—'))
                status_text = r['status'].split(' ', 1)[-1] if ' ' in r['status'] else r['status']
                w['badge'].configure(text=f" {status_text} ", bg=self._status_color(r['status']))
                muted = bool(r.get('muted'))
                w['mute_btn'].configure(text=("🔇 Unmute" if muted else "🔊 Mute"),
                                         fg=(app.FG2 if muted else app.ACC))

        if rows:
            self._empty_lbl.pack_forget()
        else:
            self._empty_lbl.pack(pady=20)

        self._update_stat_strip(rows)
        self._last_updated_lbl.configure(text=f"Last updated: {self._now_str()}")

    @staticmethod
    def _now_str():
        return datetime.now().strftime('%H:%M:%S')

    def _update_stat_strip(self, rows):
        total = len(rows)
        active = sum(1 for r in rows if 'Offline' not in r['status'])
        on_break = sum(1 for r in rows if 'On Break' in r['status'])
        logged_in = sum(1 for r in rows if 'Logged In' in r['status'])
        muted = sum(1 for r in rows if r.get('muted'))

        def pct(n):
            return f"{round(n / total * 100)}%" if total else "0%"

        for key, n in [('active', active), ('on_break', on_break),
                       ('logged_in', logged_in), ('muted', muted)]:
            count_lbl, pct_lbl = self._stat_widgets[key]
            count_lbl.configure(text=str(n))
            pct_lbl.configure(text=pct(n))
