"""ui/stats_tab.py — Stats tab for P2P Monitor (placeholder, Checkpoint 1)

Full implementation (levelup aggregation, KPI cards, chart, filters) arrives in
Checkpoint 2. This file exists so the nav-bar slot and tab order are locked:
  Monitor | Status | Stats | History | Launcher | Settings
"""
import tkinter as tk


class StatsTab:
    """Stats tab placeholder. Checkpoint 2 will fill this frame with real content."""

    def __init__(self, app, parent_frame):
        self.app = app
        self._build(parent_frame)

    def _build(self, f):
        app = self.app
        inner = tk.Frame(f, bg=app.BG2)
        inner.pack(fill='both', expand=True)

        # Centre the placeholder vertically with a spacer
        tk.Frame(inner, bg=app.BG2).pack(expand=True)

        icon_lbl = tk.Label(inner, text="▦", font=(app.BIG[0], 48), bg=app.BG2, fg=app.ACC)
        icon_lbl.pack(pady=(0, 12))

        tk.Label(inner, text="Level Stats", font=app.BIG, bg=app.BG2, fg=app.FG).pack()
        tk.Label(inner,
                 text="Aggregation, KPI cards, and chart coming in Checkpoint 2.",
                 font=app.SANS, bg=app.BG2, fg=app.FG2).pack(pady=(8, 0))

        tk.Frame(inner, bg=app.BG2).pack(expand=True)

    def on_tab_shown(self):
        """Called whenever the Stats tab is raised. No-op until Checkpoint 2."""
        pass
