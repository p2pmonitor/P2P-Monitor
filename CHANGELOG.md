# Changelog

## v2.0.0-beta.9
### Stats tab: matplotlib removed entirely — native Tk Canvas charts on every platform, plus per-panel filter fixes and grouping/layout cleanup

Consolidated entry. Beta.9, beta.10, and beta.11 were all worked through this same Linux Stats crash (and the native-canvas rewrite that followed) but none of them were ever tagged or shipped, so this single beta.9 release covers the whole arc plus the cleanup pass requested on top of it: full matplotlib removal, top-8+Other grouping, dynamic donut sizing, per-panel filter semantics, nice y-axis ticks, chart padding, and MM-DD-YY axis dates.

**Background — how we got here:** the Stats chart/donut originally used matplotlib's `FigureCanvasTkAgg`. On at least one real Linux install, every chart/donut render crashed with `FT_Render_Glyph ... raster overflow`. Root-caused to `canvas.draw()` being called before Tk's geometry manager had given the canvas real pixel dimensions; deferring the draw until after data loads, plus unbinding matplotlib's own `<Map>`-triggered DPI rescaling, narrowed it further, but the underlying fragility remained. Rather than keep chasing matplotlib/FreeType internals, the chart and donut were rewritten with plain `tkinter.Canvas` primitives on Linux (`create_line`/`create_oval` for the line chart, `create_arc(style=PIESLICE)` + a solid center circle for the donut hole) — Tk's own text rendering never touches matplotlib's FreeType wrapper, so it's immune to this class of bug by construction. Windows kept the matplotlib path at that point.

**This release finishes the job — matplotlib is gone, not just bypassed on Linux:**
1. **Removed matplotlib entirely from `ui/stats_tab.py`.** The native Tk Canvas chart/donut is now the only rendering path, Linux and Windows alike — no more dual code paths, no `USE_NATIVE_CHART`/`MATPLOTLIB_AVAILABLE` flags, no `_theme_chart_axes()` (matplotlib-only), no "matplotlib not installed" fallback label.
2. **Removed the dependency everywhere it appeared:** `requirements-linux.txt`, `requirements-windows.txt`, and the `matplotlib.backends.backend_tkagg` hidden-import (plus its stale `import matplotlib` verification line) in `p2p_monitor.spec`. `python3-tk`/Tkinter requirements are untouched — still required, never were matplotlib's concern. `install.sh` needed no change — it already installs from `requirements-linux.txt` rather than referencing matplotlib directly. `update_manifest.txt` needed no change — it already tracks `requirements-linux.txt`; `requirements-windows.txt`/`p2p_monitor.spec` are Windows-build-only files and aren't part of the Linux source-updater's file set by existing design.
3. **Levels by Skill panel — top 8 instead of top 5,** remaining skills collapsed into a single trailing `Other (N skills)` entry (unchanged below 9 distinct skills — no `Other` bucket added when there's nothing to collapse). Percentages are based on the total levels shown in that panel.
4. **Donut sizing is now dynamic, not a fixed guess.** The donut frame is resized on every redraw to match the *real rendered height* of the skill-bar rows next to it (now up to 9 rows: top 8 + Other), so it stays visually aligned whether 1 row or 9 are showing, on either platform, regardless of font metrics/DPI. Safe fallback + single `after_idle` retry if Tk hasn't finished laying out the panel yet (e.g. the very first build, before a mainloop pass) — guarded against runaway rescheduling. Added a reentrancy guard around the donut redraw itself: `update_idletasks()` processes Tk's *entire* idle queue, not just the widget it's called on, so a reentrant resize-retry firing mid-redraw could otherwise nest indefinitely under rapid filter changes.
5. **Per-panel filter semantics, fixed to match how each panel is actually used:**
   - KPI cards + Daily Levels chart: obey Account + Skill + Date range (unchanged).
   - **Levels by Skill: now obeys Account + Date range but ignores Skill** — previously it obeyed all three filters, which meant selecting a skill collapsed this panel to a single, useless slice.
   - **Top Accounts: now obeys Skill + Date range but ignores Account** — previously it obeyed all three, meaning selecting an account collapsed this panel to a single row. Example: Skill = Agility, Date = 30D now ranks every account by Agility level-ups in the last 30 days, regardless of any Account filter selected.
6. **Chart polish:** y-axis ticks now round to a clean step (1/2/5/10/20/25/50/100/...) instead of dividing the max value into raw fractional chunks (previously e.g. max=23 produced ticks 0/5/9/14/18/23 — now 0/5/10/15/20/25). Right padding increased and the first/last x-axis labels now anchor to their own edge instead of centering, so the rightmost date label can no longer run off the canvas; the actual last day is now always included among the labeled ticks (previously the label-thinning step could skip past it). Whole-number-only chart still in place. Default date range remains ALL; Account/Skill combobox text visibility unaffected.
7. **Daily Levels Gained chart x-axis dates now display as `MM-DD-YY`** (e.g. `06-19-26`) instead of `YYYY-MM-DD`. Display-only — the underlying stored date strings (still `YYYY-MM-DD`, needed for correct sorting/filtering) are untouched; only the text drawn on the chart's x-axis changed.

**Scope respected:** Monitor, Status, History, Launcher, Settings, the log parser, Discord routing, and updater behavior were not touched. `ui/stats_tab.py` is the only behavioral change; `p2p_monitor.py` got only a version bump and matplotlib-wording cleanup in comments/docstrings (no logic change).

**Validated:** `py_compile` across all touched files; `pyflakes` clean on `ui/stats_tab.py` (pre-existing, unrelated warnings in `p2p_monitor.py` confirmed identical to beta.8 — not introduced here). Live Tk suite under Xvfb against the real `StatsTab` class with synthetic multi-account/multi-skill data: zero matplotlib imports/flags remaining; date presets (7D/30D/90D/1Y/ALL) and Refresh all complete cleanly with no duplicate widgets; top-8+Other grouping confirmed (9 rows, correct trailing count) alongside a ≤8-skill dataset correctly producing no `Other` bucket; donut frame height matches the live-measured skill-panel height and stays square; resize-retry guard confirmed to fall back safely and schedule exactly one retry, never more; Levels by Skill confirmed to ignore the Skill filter while still obeying Account; Top Accounts confirmed to ignore the Account filter while still obeying Skill (row counts cross-checked against `py.stats` aggregation directly); nice-tick-step values spot-checked (23→5, 47→10, 3→1, 180→50); `MM-DD-YY` formatting confirmed both on the helper directly and on actual rendered chart canvas text; no chart text found extending past the canvas's right edge; empty-state, build-error/Retry, and forced chart-render-failure fallback all still behave correctly; revisiting the tab (Stats → Monitor → Stats) confirmed instant with no rebuilt/duplicated root frame. Tuple-padding constructor scan across `p2p_monitor.py` and every `ui/*.py` file: zero instances.

**Files changed:** `ui/stats_tab.py`, `p2p_monitor.py` (version bump + comment cleanup only), `requirements-linux.txt`, `requirements-windows.txt`, `p2p_monitor.spec`, `CHANGELOG.md`.

---

## v2.0.0-beta.8
### Stats prewarm disabled on Linux; race guard + failure cleanup added

**Linux**: prewarm disabled entirely for now. Despite beta.7's prewarm being verified data-only (no widgets/matplotlib touched — confirmed via live test), Linux still showed duplicate Stats sections, pointing at a race not yet pinned down. Per explicit instruction: stability over first-click speed. `_prewarm_stats()` now returns immediately on Linux.

**Windows**: prewarm unchanged, still active.

**New defense-in-depth in `StatsTab` (both platforms):**
- `_building` lock in `_ensure_built()` — a second call while a build is in progress is now a no-op instead of racing.
- If `_build_real_content()` throws partway through, all partial widgets are destroyed and a placeholder is restored before the lock releases — no broken UI left behind, and a later call can retry cleanly.
- Verified live: rapid double `on_tab_shown()` calls produce exactly one filter row; a forced build failure leaves no widgets behind and a subsequent retry recovers correctly.

**Files changed:** `p2p_monitor.py` (version; Linux guard in `_prewarm_stats`), `ui/stats_tab.py` (`_building` lock + cleanup in `_ensure_built`).

---

## v2.0.0-beta.7
### Fix: Stats prewarm caused duplicate Stats sections on Linux; chart polish

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3. Scoped to `ui/stats_tab.py` and `p2p_monitor.py`'s prewarm wiring only.

**Fix: Linux showed two stacked Stats sections after prewarm**
- Root cause: beta.6's prewarm built the *real* Stats widgets — including constructing and immediately `draw()`-ing matplotlib canvases — inside the Stats tab's frame while a different tab was the one actually raised via `tkraise()`. On Windows this happened to work; on Linux, drawing into a frame that isn't yet mapped/realized (and therefore may not have real allocated screen dimensions yet) could fail partway through matplotlib's Agg/FreeType text rendering — consistent with the "FT_Render_Glyph raster overflow" warning. Because the failure happened *inside* `_build_real_content()`, `self._built` was never set to `True`, so the next real visit to the tab ran the entire build again on top of the broken partial one — two stacked copies of the whole tab, exactly as described.
- Fixed by making `StatsTab.prewarm()` **data-only**: it now does nothing but load and cache levelup rows on a background thread. It never creates a single Tkinter widget, never constructs a matplotlib `Figure`/`Canvas`, and never calls `_build_real_content()`. Building the actual UI is now reserved entirely for `on_tab_shown()` — the one moment the Stats frame is guaranteed to have real screen dimensions, because it's in the process of being `tkraise()`'d.
- `_ensure_built()` now consumes the prewarm cache when it exists (`self._prewarm_rows`), so a prewarmed manual open skips a redundant disk read instead of reloading from scratch — prewarm still pays off, it just never touches anything visual.
- A failed prewarm (verified directly by forcing `load_levelup_rows()` to raise) now leaves **zero** widgets behind and caches an empty list rather than leaving the tab in a broken state; a subsequent real open still works normally.
- This removes the entire class of risk by construction rather than chasing the exact FreeType trigger condition — there is nothing in the prewarm path anymore that can build or draw into an unrealized widget.

**Chart polish**
- Daily Levels Gained y-axis now uses `matplotlib.ticker.MaxNLocator(integer=True)` — whole-number ticks only (0, 1, 2, 3...), never fractional ticks like 0.5/1.5, since you can't gain half a level in a day. The Average Per Day KPI card is unaffected and still shows one decimal place.
- Default date range on first Stats load is now **ALL** (previously 30D) — the ALL pill is highlighted by default; 7D/30D/90D/1Y are still one click away.

**Re-verified, not just assumed:**
- Combobox visibility fix (beta.6) — still correct in every state.
- Chart and donut dark theming (beta.6) — still correct; figure, axes, and the underlying Tk canvas widget's own background all confirmed non-white for both charts.
- Skill donut + grouping (beta.6), including the exact boundary: 5 skills produces no "Other" row, 6 skills produces "Other (1 skills)".
- Dependency restart prompt (beta.5) — untouched, its own live test still passes unchanged.
- Repeated Stats → Monitor → Stats switching, Account/Skill filters, date-range buttons, and Refresh all still produce exactly one filter row with zero widget duplication.

**Validation:**
- `python -m compileall .` — clean.
- `pyflakes` on `ui/stats_tab.py` (zero) and `p2p_monitor.py` (still the 14 pre-existing baseline warnings, zero new).
- Tuple-padding constructor scan re-run across `p2p_monitor.py` and every `ui/*.py` — zero instances.
- Live Tk test (real Xvfb display): 22 new checks specifically targeting the data-only prewarm contract (zero widgets created, cache consumed correctly, failure leaves nothing behind) plus the y-axis/default-range/grouping-boundary fixes, plus the existing 40-check beta.6 suite re-run (updated to reflect the corrected two-phase prewarm flow) and the empty-state/beta.4-full-flow/beta.5-restart-dialog suites — all passing, 92 live checks total across 5 test files.

**Files changed:**
- `ui/stats_tab.py` — `prewarm()` rewritten to be data-only; `_ensure_built()` consumes the prewarm cache; integer y-axis ticks; default date preset changed to `ALL`
- `p2p_monitor.py` — version 2.0.0-beta.7; `_prewarm_stats()` docstring corrected to describe the data-only design
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.6
### Stats tab polish: combobox visibility, chart theming, skill donut, prewarm

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3. Scoped entirely to the Stats tab and the shared `ttk.Style` Combobox configuration (the only current consumer of `ttk.Combobox` in the app) — no other tab, parser, Discord, or updater behavior changed.

**Fix: Account/Skill combobox text was unreadable when not focused**
- Root cause: the `clam` ttk theme has its own built-in state-based color maps for `Combobox` that silently override a plain `configure()` call for certain states (notably `readonly` and `!focus`) — exactly the states the dropdowns sit in most of the time. `configure()` only sets the *default* style; per-state `.map()` entries are required to actually force a color to stick in a specific state.
- Added explicit `.map('TCombobox', ...)` entries covering `readonly`, `disabled`, `focus`, and `!focus` for `fieldbackground`, `foreground`, `selectbackground`, `selectforeground`, `background`, and `arrowcolor`.
- The dropdown's open popdown list is a plain Tk `Listbox`, not a ttk widget — it doesn't inherit `ttk.Style` at all. Added `self.option_add('*TCombobox*Listbox...')` entries so the opened-dropdown list is themed too, not just the closed field.
- Verified directly: looked up `ttk.Style().lookup('TCombobox', 'foreground', state)` against the real, unmodified `App._style()` method for every relevant state combination (default, readonly, focus, `!focus`, and specifically `readonly + !focus` — the exact combination that was broken) and confirmed foreground never resolves to the same color as the field background in any of them.

**Fix: Linux chart area rendered as a plain white rectangle**
- Root cause: the figure's facecolor was only set once at initial build, the underlying Tk `Canvas` widget's *own* background (separate from matplotlib's figure/axes facecolor — a distinct layer) was never touched at all, and `ax.clear()` (called on every redraw) resets most per-Axes styling back to matplotlib's light-theme defaults. Any gap between the widget's first paint and a fully-themed redraw could show through as Tk's default white canvas background — apparently more visible/reproducible on Linux than Windows.
- Added a shared `_theme_chart_axes(fig, ax, canvas_widget)` that forces figure patch, axes facecolor, tick colors, label colors, title color, spines, *and* the Tk canvas widget's own `bg` — called at initial build and again at the top of every redraw (after `ax.clear()`), not just once.
- Switched `canvas.draw_idle()` → `canvas.draw()` (forced, synchronous) so the chart never sits unpainted waiting for an idle slot, and added an explicit `canvas.draw()` immediately after initial construction so the canvas is never left showing an unrendered default background before the first real data load completes.
- Same fix applied to the new skill donut chart below, since it shares the identical underlying mechanism.

**Levels by Skill panel: donut chart + grouped "Other" bucket**
- Added a donut chart on the left side of the "Levels by Skill" card; horizontal skill bars stay on the right, now with a small color swatch per row matching the donut's wedge colors.
- New `group_top_n_with_other()` in `py/stats.py` (pure, unit-tested): keeps the top 5 skills individually and collapses the rest into one `Other (N skills)` entry. Applied identically to both the donut and the bars so they always agree with each other. Percentages are computed off the full filtered total, so the grouped view never loses or double-counts levels.
- No "View All Skills" button (intentionally not added, per spec).
- Colors: sage/olive (`ACC`) for the largest slice, then amber (`YEL`) → coral (`RED`) → lavender (`PUR`) → amber-orange (`ACC2`), cycling if ever needed; "Other" always gets a fixed, deliberately neutral muted tan (`#a89a78`) rather than a theme accent, so it reads as "everything else" rather than competing for attention.

**New: Stats prewarm for faster first open**
- The App now schedules a single `self.after(4000, self._prewarm_stats)` call at startup (no recurring timer) that quietly builds the Stats tab's real widgets and kicks off its initial history-aggregation load *before* the user ever clicks the tab — particularly aimed at Linux, where cold-building the matplotlib figures was noticeably slower than on Windows.
- `StatsTab` gained `_ensure_built()`, a single shared guarded-build path now used by both `on_tab_shown()` and the new `prewarm()` — exactly one build code path, not two copies that could drift apart or double-build.
- Building happens inside the Stats tab's existing frame in the shared `tkraise()` stack from Checkpoint 1, which isn't the topmost (visible) frame unless the user is already on Stats — so prewarm never switches tabs, steals focus, or causes visible flicker. If the user clicks into Stats before the 4s timer fires, normal loading proceeds exactly as before (the timer's later call becomes a no-op).
- If prewarm throws for any reason, it's caught and logged (`⚠ Stats prewarm failed (non-fatal)`) without affecting app startup.

**Preserved (re-verified with the live Tk test, not assumed):**
- Stats still builds exactly once; switching Stats → Monitor → Stats repeatedly still produces exactly one filter row and a stable widget count.
- Refresh, Account filter, Skill filter, and date-range buttons all still update KPIs/chart/panels correctly with zero widget duplication.
- The empty state still works correctly with zero levelup history.
- The beta.5 dependency-restart prompt (Restart Now/Later + persistent chrome notice) is untouched and still passes its own live test.

**Validation:**
- `python -m compileall .` — clean.
- `pyflakes` on `p2p_monitor.py` (still exactly the 14 pre-existing baseline warnings, zero new) and `ui/stats_tab.py` (zero).
- Tuple-padding constructor scan (the class of bug found in beta.5) re-run across `p2p_monitor.py` and every `ui/*.py` file — zero instances.
- Live Tk test (real Xvfb display, real `App._style()`, real `StatsTab`): 36 checks covering combobox states, chart/donut theming, skill grouping, prewarm-without-tab-switch, post-prewarm idempotency, and the full filter/refresh/revisit matrix — all passing. Plus the existing empty-state, beta.4 full-flow, and beta.5 restart-dialog live tests re-run unchanged and still passing.

**Files changed:**
- `ui/stats_tab.py` — chart theming overhaul (`_theme_chart_axes`), skill donut chart, `group_top_n_with_other` wiring, `_ensure_built()`/`prewarm()` refactor
- `py/stats.py` — new `group_top_n_with_other()`
- `p2p_monitor.py` — version 2.0.0-beta.6; fixed `TCombobox` style maps + popdown listbox option_add entries in `_style()`; new `_prewarm_stats()` wired via a single startup `self.after()` call
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.4
### Linux dependency-update support in the source updater

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3.

**New: Linux dependency detection in the in-app updater**
- New `requirements-linux.txt` — the single source of truth for Linux pip dependencies (`pystray`, `Pillow`, `psutil`, `tkcalendar`, `matplotlib`). `install.sh` now installs from this file (`pip3 install -r requirements-linux.txt`) instead of a separately-maintained hardcoded package list, so the two can no longer drift out of sync.
- After a successful Linux source update, `_do_apply_update()` now calls a new `_check_and_install_linux_deps()`: it reads the just-updated `requirements-linux.txt`, checks each entry against installed packages via `importlib.metadata`, and — only if something is actually missing — prompts with a Yes/No dialog listing exactly which package(s) before running `pip install` for those specific entries.
- If the user declines, the update still completes and offers to restart as normal; a log line with the manual `pip3 install ...` command is left for later.
- This **never** touches system/apt packages, never runs the full `install.sh`, and never installs anything without an explicit prompt. If nothing is missing (the common steady-state case), this is a silent no-op — no dialog, no extra noise.
- `discord.py` is intentionally **not** in `requirements-linux.txt` — it already has its own on-demand installer (existing code in `ui/settings_tab.py`, triggered only when "Run Bot Setup" is used), since most setups only need webhook notifications and don't need the extra dependency at all.
- This addresses a real gap confirmed by reading the updater code directly: the file-copy-based updater has no dependency-installation step of any kind, so updating via "Check for Update" without also installing matplotlib would previously leave the Stats chart unavailable (gracefully, thanks to beta.3's fallback message — not a crash) until the user manually ran `pip install matplotlib`. This checkpoint closes that gap going forward for any future new dependency, not just matplotlib.
- **Important nuance for the beta.3 → beta.4 transition specifically:** an update is *applied* by whichever updater code is currently running — so upgrading from beta.3 (which predates this feature) to beta.4 is carried out by beta.3's old updater, which has no idea this dependency check exists and will not run it. That one transition still needs `matplotlib` installed manually (`./install.sh` or `pip3 install matplotlib --break-system-packages`) after updating. To close that gap too, `_check_and_install_linux_deps()` now also runs once at startup (`_startup_dependency_check()`, Linux source installs only) — so after restarting into beta.4 (or any future version), the *next* startup catches anything an older updater couldn't, in addition to the existing post-update check. Both call the same detection/prompt logic; the startup path is silent on every run after the first one that actually needed to install something.
- Dependency detection is presence-only (is the package importable at all?), not minimum-version enforcement — it would catch "matplotlib not installed" but not "matplotlib installed but older than required". Acceptable for beta; worth tightening with real version-comparison before the stable v2.0.0 release.

**Polish: restart-required prompt after a dependency install**
- After `_check_and_install_linux_deps()` successfully pip-installs missing packages, it no longer just logs "installed" — it now shows a modal dialog ("Dependencies installed successfully. Please restart P2P Monitor for the new packages to load.") with **Restart Now** / **Later** buttons. The new package was just installed into the running interpreter's site-packages, but Python's import system won't pick it up mid-process — a restart is the only way it actually loads.
- **Restart Now** stops the watcher if running and re-execs the process (`os.execv`) — the exact same restart path the existing post-update "Restart now?" prompt already used. Extracted both into one shared `_restart_app()` helper so there's a single restart code path, not two copies of the same logic.
- **Later** keeps the app open and shows a persistent "⚠ Restart required to finish dependency update" notice in the window chrome (next to the Monitor/Stopped status) until the user actually restarts. Clicking the notice offers to restart (with its own confirmation — restarting is never automatic).
- This applies to both places `_check_and_install_linux_deps()` is called from — the post-update check and the startup check — so the prompt shows up correctly however the install was triggered.
- Never runs `install.sh` or `apt`, and the whole feature is gated to non-frozen Linux installs already (inherited from `_check_and_install_linux_deps()`'s existing guard) — Windows frozen builds never reach this code at all.

**Bugfix: Stats tab created a duplicate filter row every time the tab was revisited**
- Root cause, confirmed by actually running the real `StatsTab` against a real Tk display (via Xvfb) rather than guessing from reading code: `_build_kpis()` passed `pady=(0, 10)` — a tuple — directly to a `tk.Frame()` **constructor**. Tkinter widget constructors only accept a single padding value; tuples are only valid on `.pack()`/`.grid()` calls. This raised a `TclError` on the very first build, *after* `_build_filters()` had already successfully created the filter row but *before* `self._built = True` ever executed.
- Tkinter's event dispatcher swallows exceptions raised inside callbacks (button clicks, etc.) — it prints a traceback and keeps running rather than crashing the app. So the Stats tab looked superficially fine (no visible crash), but `self._built` was permanently stuck at `False`. Every subsequent visit to the tab re-ran the *entire* build from scratch: `_build_filters()` would succeed again (adding another filter row) and then crash again at the same spot, so KPI cards/chart/panels never appeared at all. This is an exact match for the reported symptom.
- Fixed both occurrences (the same mistake was also in `_build_panels()`) by moving the asymmetric padding to the `.pack()` call instead of the constructor.
- Then swept the **entire codebase** (`p2p_monitor.py` and every `ui/*.py` file) with a script that parses every `tk.Frame`/`tk.Label`/`tk.Button`/etc. constructor call — including ones spanning multiple lines — for a tuple `padx=`/`pady=` argument. Found and fixed two more instances of the exact same mistake in the brand-new restart-prompt dialog code added in this same release, before they ever shipped. Confirmed zero remaining instances anywhere in the repo.
- Verified the fix directly: built a real Tk root against a virtual display, ran the actual `StatsTab` class through 5+ simulated tab-revisit cycles, filter changes, date-range clicks, and Refresh clicks, and confirmed exactly one filter row and a stable total widget count throughout — not inferred from code review.
- No changes to `py/stats.py`'s aggregation logic, no changes to Monitor/Status/History/Launcher/Settings/parser/Discord behavior — this was purely a Tkinter constructor-argument bug in `ui/stats_tab.py`.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.4 (unchanged); new `_check_and_install_linux_deps()` method (Checkpoint linux-deps); new `_startup_dependency_check()`; new `_restart_app()`, `_show_restart_now_later_dialog()`, `_show_restart_required_notice()`, `_on_restart_notice_clicked()`, `_prompt_restart_after_dep_install()`; existing post-update restart closure simplified to use the shared `_restart_app()`; hidden restart-notice label added to the window chrome
- `ui/stats_tab.py` — fixed the two tuple-padding constructor bugs in `_build_kpis()` and `_build_panels()` that caused the duplicate filter row bug
- `requirements-linux.txt` — new
- `install.sh` — installs from `requirements-linux.txt` instead of a hardcoded inline list; copies `requirements-linux.txt` into the install dir
- `update_manifest.txt` — added `requirements-linux.txt` (critical — without this the updater would never refresh the file it depends on)
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.3
### Checkpoint 2 — real Stats tab + theme refinement

**Internal beta — not the public 2.0.0 release.** Stable public release remains v1.8.3.

**Stats tab (real implementation, replaces the Checkpoint 1 placeholder)**
- New `py/stats.py`: pure, stdlib-only levelup aggregation (no Tkinter) — `load_levelup_rows`, `filter_rows`, `aggregate_daily_totals`, `aggregate_skill_totals`, `aggregate_account_totals`, `compute_kpis`, `daily_series_for_range`, `date_bounds_for_preset`. Kept separate from the UI so it's unit-testable without a display.
- Data source is existing levelup history records only — no history file format changes. `'Total Level'` milestone-broadcast rows are deliberately excluded from aggregation (they're not a real skill and would double-count alongside the per-skill levelup that normally accompanies them).
- Filters: Account (All Accounts + individual), Skill (All Skills + individual), date range pills (7D / 30D / 90D / 1Y / ALL).
- KPI cards: Total Levels, Average Per Day (divided by calendar days in the selected range, not just days with data), Best Day, Top Account.
- Main chart: Daily Levels Gained, zero-filled across the full date range so quiet days show as real zeros instead of gaps. Uses `matplotlib`/`FigureCanvasTkAgg` when available; falls back to a "matplotlib not installed" message instead of crashing if the backend can't load.
- Lower panels: Levels by Skill and Top Accounts, both ranked bar-lists.
- Empty state ("No level-up data found yet.") shown when there is no levelup history at all; a filtered-to-zero result (e.g. an empty date range) shows zeros/"no data for this filter" instead of the full empty state.
- Performance: disk reads (`load_levelup_rows`, which may touch many history files across accounts) always run on a background thread. Filter and date-range changes only re-filter/re-aggregate the already-loaded in-memory rows and redraw — no disk I/O on every filter click. The two lower panels rebuild only their own row widgets, not the rest of the tab.
- Lazy-built: the Stats tab's real widgets (and first data load) are built on the first time the tab is opened, not at app startup — consistent with the Checkpoint 1 cached-tab architecture.
- A live `levelup` event now marks the Stats tab dirty (`mark_dirty()`); the next time the tab is opened it reloads from disk instead of showing stale data.

**Theme refinement**
- `ACC` and `GREEN` desaturated further toward muted sage/olive/moss — beta.2's values (48%/52% saturation) still read as a fairly vivid "terminal green" against the warm dark background. New values are 30%/36% saturation.
  - `ACC`: `#4a8f5c` → `#7c9468`
  - `GREEN`: `#5cbf72` → `#8aac6e`
- All other tokens (`ACC2`, `RED`, `YEL`, `PUR`, `FG`, `FG2`, backgrounds) already matched the target direction as of beta.2 and are unchanged.
- Confirmed nav bar/window chrome are already sans-serif (`SANS`/`SANSB`/`BIG`) from Checkpoint 1 — no font change needed.
- Hardcoded cyan/neon literals remain in `ui/launcher_tab.py` and `ui/history_tab.py` (e.g. `#2e86c1`, `#00ff88`) — intentionally left untouched. Those tabs get their own redesign in Checkpoint 4, and patching scattered literals now would just be redone there; fixing them piecemeal ahead of that pass isn't worth the churn.
- Monitor tab layout is still unchanged — full visual redesign is Checkpoint 4.

**Install / update plumbing**
- Added `py/stats.py` to `update_manifest.txt` and `install.sh`.
- Added `matplotlib` to `requirements-windows.txt` and `install.sh`'s pip install line.
- Added `matplotlib.backends.backend_tkagg` to `p2p_monitor.spec` hiddenimports (PyInstaller's built-in matplotlib hook handles `mpl-data`/fonts automatically; the TkAgg backend specifically is loaded dynamically and is sometimes missed by static analysis, so it's listed explicitly to match this spec's existing defensive style for third-party packages).
- Verified via simulation: copying *only* the manifest-listed files into a clean directory (what the in-app updater does) produces a working install, including the new Stats module.

**Files changed:**
- `py/stats.py` — new
- `ui/stats_tab.py` — full rewrite (was a Checkpoint 1 placeholder)
- `p2p_monitor.py` — version 2.0.0-beta.3; `ACC`/`GREEN` color tokens; `_on_event` marks Stats tab dirty on live `levelup` events
- `update_manifest.txt` — added `py/stats.py`
- `install.sh` — added `py/stats.py` copy line; added `matplotlib` to pip install line
- `requirements-windows.txt` — added `matplotlib`
- `p2p_monitor.spec` — added `matplotlib.backends.backend_tkagg` to hiddenimports; added verification step to header comment
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.2
### Checkpoint 1 correction — warm theme tokens

**Internal beta — not the public 2.0.0 release.** Still on top of Checkpoint 1 (navigation/theme foundation); Stats tab work hasn't started yet. Stable public release remains v1.8.3.

**Theme correction**
- Beta.1's background tokens (`BG`/`BG2`/`BG3`/`BG4`) and `FG2` were still blue-dominant in every channel comparison (e.g. `BG2 #161a22` = R22/G26/**B34**) — a navy/slate canvas, not the warm charcoal/espresso/graphite that was intended. That's what kept the app reading as "terminal/cyan-ish" even though the accent color itself had already changed away from cyan.
- Corrected all five tokens to genuinely warm neutrals (R ≥ G > B in every shade):
  - `BG`: `#0f1115` → `#13110f`
  - `BG2`: `#161a22` → `#1a1714`
  - `BG3`: `#1d2130` → `#221e19`
  - `BG4`: `#252840` → `#2c2620`
  - `FG2`: `#78788a` → `#8c8478`
- Lightness progression across the four background tiers is preserved (same relative layering/contrast as beta.1), only the hue shifted from cool to warm.
- `ACC`, `ACC2`, `GREEN`, `RED`, `YEL`, `PUR`, `FG` were already correct (sage green, amber, coral, warm cream) and are unchanged.
- Confirmed nav bar and window chrome were already using the sans-serif font tokens (`SANS`/`SANSB`/`BIG`) from beta.1 — no font change was needed, this was purely a color-token fix.
- No layout, behavior, or tab content changed. Monitor tab redesign is still scheduled for Checkpoint 4.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.2; corrected `BG`/`BG2`/`BG3`/`BG4`/`FG2` color tokens
- `CHANGELOG.md` — this entry

---

## v2.0.0-beta.1
### Checkpoint 1 — UI foundation: custom navigation, warm theme palette, Stats tab slot

**Internal beta — not the public 2.0.0 release.**
Use "Include pre-release versions when checking for updates manually" to receive this update.
The stable public release remains v1.8.3 until the full v2.0.0 is ready.

**Navigation architecture**
- Replaced `ttk.Notebook` with a custom frame-based navigation bar using `tkraise()` — tab frames are built once, never destroyed on switch, and re-raised instantly. This eliminates the per-switch rebuild overhead that made Linux feel slightly less responsive than Windows.
- Added `app.show_tab(name)` method (string-based, index-independent) replacing all `_nb.select(N)` calls — forward-compatible with future tab additions.
- Tab order is now locked for v2: Monitor | Status | Stats | History | Launcher | Settings.
- Stats tab added as a placeholder frame (real content in Checkpoint 2).

**Theme: warm dark palette**
- Replaced the cyan-heavy palette with a warm dark scheme: sage/olive green primary accent, warm amber/gold for level highlights, muted coral/red for errors and stopped states, warm cream/off-white text.
- Primary accent `ACC`: `#00d4ff` (cyan) → `#4a8f5c` (sage green)
- `GREEN`: `#00ff88` (neon) → `#5cbf72` (muted)
- `RED`: `#ff4444` (bright) → `#d04848` (coral)
- `YEL`: `#ffd700` (gold) → `#c8a840` (amber)
- `FG`: `#e8eaf0` (cool white) → `#e4ddd4` (warm cream)
- All color tokens are centralized as class attributes on `App` — later checkpoints inherit them without hardcoding.
- Added sans-serif font constants (`SANS`, `SANSB`, `SANSL`, `SANSS`, `BIG`): Segoe UI on Windows, DejaVu Sans on Linux. Applied to window chrome and navigation bar. Monospace (`MONO`) is retained for the raw event log text area.
- Existing tab content (Monitor, Status, History, Launcher, Settings) automatically picks up the new color tokens; font swap inside those tabs happens in Checkpoint 4.

**Install / update plumbing**
- Added `ui/stats_tab.py` to `update_manifest.txt` and `install.sh` — without this, a fresh install or an in-app update would copy/apply `p2p_monitor.py` (which now imports `ui.stats_tab`) without the file it depends on, causing a crash on launch. `p2p_monitor.spec` (PyInstaller) needed no change — it statically discovers local `.py` imports automatically; only non-`.py` assets like `error_rules.json` need explicit `datas` entries.
- Fixed `install.sh`'s version-banner regex to also capture pre-release suffixes (e.g. `-beta.1`) instead of truncating at the first hyphen.

**Files changed:**
- `p2p_monitor.py` — version 2.0.0-beta.1; warm dark color tokens; SANS font constants; custom nav bar; `show_tab()` method; removed `_nb`, `_on_tab_changed`, `_history_tab_frame`, `_status_tab_frame`; tray icon color updated
- `ui/stats_tab.py` — new (Checkpoint 1 placeholder)
- `ui/status_tab.py` — `app._nb.select(2)` → `app.show_tab('History')`
- `update_manifest.txt` — added `ui/stats_tab.py`
- `install.sh` — added `ui/stats_tab.py` copy line; fixed version-banner regex for pre-release suffixes
- `CHANGELOG.md` — this entry

---

## v1.8.3
### Bugfix/cleanup release: update-check timing, reset attribution, Monitor tab cleanup, debug.jsonl

**Fixes**
- Startup update-awareness check (`force=True`) now updates `_last_update_check_slot`, so the periodic check no longer re-runs immediately after startup
- `levelup_every` is now clamped to a minimum of 1 (`max(1, int(...))`, falls back to 5 on bad config values) — previously `levelup_every = 0` could divide by zero
- Script reset attribution (`Escaped ship -> Startup`, `Stuck walking -> Startup`) now uses the task/activity active at the reset line's position in the batch (via each parsed event's `_line_idx`), instead of whatever task the batch ends on — fixes occasional mislabeling when a reset and a new task land in the same read batch. A contained fallback also covers the case where no task context is known at all (e.g. the very first batch ever processed for an account, where `slice_tasks()` had already suppressed the `Task is X` announcement because of a nearby `> Locking` line) — in that case the nearest `Task is X` / `Activity is Y` is read directly from this batch's raw lines, before the reset line only. This fallback never parses or emits task events, never updates `state.last_task` / `state.last_activity`, and never touches `py/reader.py`, `slice_tasks()`, Slayer parsing, or Discord routing.
- `Script Stopped` now logs to the Monitor tab before the corresponding `Auto restart scheduled/skipped ...` line — previously the auto-restart message could appear first

**New: structured session debug log**
- New `~/.p2p_monitor/debug.jsonl`, truncated on monitor startup
- Important diagnostics (history dedupe, launcher/relaunch internals, Discord thread rate-limiting, daily summary internals) are now always written here, independent of the debug checkbox
- The debug checkbox now only controls whether a short human-readable line also mirrors live to the Monitor tab — Monitor tab behavior stays append-only, with no replay of old debug entries and no auto-sorting

**History dedupe diagnostics**
- When `_dedup_history_file()` removes duplicate rows, it now writes a `history_dedupe` entry to `debug.jsonl` with the cleanup timestamp, account, history file path, duplicate count, type counts, and the removed entries themselves (capped at 200, with `truncated: true` if more were removed)
- Dedupe behavior itself is unchanged — this is diagnostics only
- Covers both backfill-triggered dedupe and History tab loads

**Monitor tab cleanup**
- Moved internal/plumbing lines to debug-only: Discord thread membership rate-limiting, daily summary send/failure internals, and launcher internals (closing client, waiting before relaunch, relaunching, client PID confirmed)
- Monitor tab keeps real account/script/task/error/drop/death/level/update events

**New: script event ping toggle**
- New setting "Ping for script events" (`ping_script_event`, default `True`) under Event Notifications — when off, script start/stop/pause/resume Discord messages still post but without a mention ping. Task/error/drop/death/level ping settings are unaffected.

**Files changed:**
- `py/watcher.py` — update-check timing fix; `levelup_every` clamp; reset attribution via `_line_idx` plus a raw-line fallback (`_task_ctx_from_raw_lines()`) for batches with no prior task context; `_maybe_schedule_auto_restart()` returns `(status, message)` instead of logging directly; `_debug_entry()` helper; `debug.jsonl` reset on `start()`; daily summary and thread rate-limit lines moved to debug
- `py/history.py` — `_dedup_history_file()` writes `history_dedupe` diagnostics to `debug.jsonl`; accepts optional `account` param
- `py/launcher.py` — `_discover_and_cache()` and `relaunch_account()` move internal lines to `debug.jsonl`, mirroring to Monitor only when the debug checkbox is enabled
- `py/discord.py` — `post_script_event()` respects new `ping_script_event` config key
- `py/util.py` — new `write_debug_entry()` / `reset_debug_log()` helpers for `~/.p2p_monitor/debug.jsonl`
- `ui/settings_tab.py` — new "Ping for script events" checkbox
- `p2p_monitor.py` — version 1.8.3; `ping_script_event` added to `DEFAULT_CFG`
- `README.md` — corrected SDN/Worker wording
- `CHANGELOG.md` — softened v1.8.2 compile-metadata claim; removed references to test files not present in the repo

---

## v1.8.2
### DreamBot SDN as primary update source + configurable check interval

**Primary update source: DreamBot SDN API**
- Update awareness now checks `https://sdn.dreambot.org/scripts/all` as primary source
- Finds P2P Master AI by exact name match (`name == "P2P Master AI"`) with optional safety checks on `id == 1500` and `author == "Aeglen"`
- Extracts the latest script version from the SDN response; compile metadata may be logged for debug if present but is not used for update decisions
- Cloudflare Worker (`p2p-sdn-watch.p2pmonitor.workers.dev`) kept as silent fallback — used automatically if SDN is unreachable, returns bad JSON, or does not contain P2P Master AI
- Source and fallback details are debug-only — no user-facing alerts for source failures alone
- Thanks to **@Ziggy** for finding the DreamBot SDN API endpoint

**Version comparison: Decimal-aware**
- SDN returns versions as JSON numbers (e.g. `2.15` for 2.150, dropping trailing zero)
- New `_sdn_ver_tuple()` uses `Decimal` arithmetic to pad fractional parts to 3 digits: `2.15 → (2, 150)`, `2.149 → (2, 149)` — so `2.15` correctly compares as newer than `2.149`
- Local title versions and all comparisons now use the same Decimal-based path

**Configurable update check interval**
- New HH:MM interval control in Settings → Update Awareness: default 6h 0m, minimum 1m, maximum 24h 0m
- Replaces the previous fixed UTC slot schedule (00:20, 06:20, 12:20, 18:20)
- Startup check still fires immediately on monitor launch
- Periodic checks fire based on wall-clock elapsed time since last check
- `0h 0m` clamps to `0h 1m`; values over `24h 0m` clamp to `24h 0m`
- Config keys: `update_check_interval_hours`, `update_check_interval_minutes`

**Files changed:**
- `p2p_monitor.py` — version 1.8.2; `update_check_interval_hours` / `update_check_interval_minutes` added to DEFAULT_CFG
- `py/watcher.py` — `_SDN_URL`, `_WORKER_URL`, `_SDN_SCRIPT_NAME/ID/AUTHOR` constants; `_sdn_ver_tuple()` with Decimal comparison; `_ver_tuple()` delegates to `_sdn_ver_tuple`; `_fetch_from_sdn()` + `_fetch_from_worker()` replace `_fetch_latest_version()`; `_check_update_awareness()` uses elapsed-time interval; `_last_update_check_slot` initialised to `0.0`
- `ui/settings_tab.py` — HH:MM interval spinboxes; updated description text; clamping in `save()`
- Validation covered SDN parsing, fallback behavior, Decimal version comparison, interval logic, and clamping.

---

## v1.8.1
### Real Discord pings, independent screenshot controls, auto-relaunch on update, runtime stats

---

**Real Discord pings**
- Mentions in embed descriptions (`<@user>`) appeared visually but did not trigger Discord notifications; fixed by moving real pings to top-level message `content` with explicit `allowed_mentions: {"users": ["id"]}`
- `<@user>` removed from embed descriptions — no function there now that real pings go in message content
- New `normalize_mention_id()` helper: accepts raw ID (`123`), `<@123>`, or `<@!123>` — all normalise to `123`
- New `apply_ping(payload, mention_id, enabled)` helper: adds `content` and `allowed_mentions` to any payload dict
- **New Ping column** in the Event Notifications settings table (alongside Notify and Screenshot): Quests, Tasks, Chat, Errors, Drops, Deaths, Level Ups each have an independent Ping checkbox
- Script lifecycle events (Started, Stopped, Paused, Resumed) always ping when a mention ID is configured
- Update awareness alerts have a separate Ping toggle in the Update Awareness section (default on)
- New config keys: `ping_quest`, `ping_task`, `ping_chat`, `ping_error`, `ping_drops`, `ping_death`, `ping_levelup`, `ping_update`

**Event screenshots no longer depend on scheduled screenshots (bug fix)**
- Previously `screenshots_enabled` (labelled "Enable scheduled screenshots") acted as a global kill switch — if off, all screenshots stopped including per-event ones, on-demand `/ss`, and startup
- Fixed: `screenshots_enabled` now gates scheduled screenshots only; each screenshot type is independently controlled
- `ScreenshotService.enqueue()` now returns `True` if queued, `False` if refused; `LogWatcher._enqueue_screenshot()` propagates that bool
- `DiscordRouter.post_event()` and `post_drop()`: if screenshot enqueue returns `True`, return immediately; if `False`, debug-log and fall through to embed-only — prevents duplicate messages (embed-only + screenshot-with-embed)
- `ScreenshotService._worker()`: if `take_screenshot()` fails and the item has a payload+URL (event/drop screenshot), posts the embed without the image as fallback; scheduled screenshots with no payload remain debug-only
- Startup screenshots gated by `screenshot_on_startup`; event screenshots gated by `ss_event_*` only; on-demand always works

**Update awareness: auto-relaunch on update (default off)**
- New setting: **Auto-relaunch clients when script/client update is found**
- When enabled: affected accounts are relaunched automatically with a 5-second stagger; accounts without a launcher preset are listed for manual action in the Discord alert
- Warning shown in Settings UI and Discord message: can interrupt any current activity including Inferno/Jad
- When disabled: normal grouped update alert sent, recommends `/relaunch <account>` when ready
- Separate dedupe stores for alert suppression vs relaunch suppression — a new version triggers both independently; when auto-relaunch fires, alert keys are also saved so the same update does not later produce a normal "Update Available" alert
- New config key: `auto_relaunch_on_update`
- New state file: `~/.p2p_monitor/update_relaunch_state.json`

**Update alerts: grouped and pingable**
- One Discord message per check listing all accounts by update category (both / script / client)
- Pings once per check if `ping_update` is enabled — not once per account

**Grouped repeated task/lock failures**
- Rapid burst of related lock/error events (e.g. multiple farming patch failures at once) now groups into one Discord alert and one monitor line
- 3-second debounce window per account; duplicate details deduped; one ping if ping_error is enabled
- Standalone errors (non-lock) and errors separated by more than 3 seconds still send individually

**Lock failure wording**
- Generic lock fallback changed from `Quest abandoned: X` to `Task locked/skipped: X`
- Covers non-quest locks (Gold BF, Sailing, etc.); specific `error_rules.json` matches unchanged

**Runtime stats in History tab**
- Each account row in the History tab shows **📈 Runtime Stats** alongside **📊 Summary**
- Opens a popup showing: Total running time, Active play time, Break time, Break %
- Range filters: All time, Today, 7 days, 30 days
- Break time calculated from actual elapsed intervals — not from planned break length in the log
- `py/history.py`: new `compute_runtime_stats(account, since_ts, until_ts)` and `_fmt_secs()` functions

**Files changed:**
- `p2p_monitor.py` — version 1.8.1; new `ping_*` and `auto_relaunch_on_update` keys in DEFAULT_CFG
- `py/reader.py` — lock fallback wording
- `py/discord.py` — `_desc()` removes mention embed; `normalize_mention_id()` + `apply_ping()`; `post_event()` enqueue fallback; `post_drop()` enqueue fallback; `post_task()` applies `ping_task`; `post_script_event()` always pings when mention set; `combined_daily_summary_payload()` removes inline mention
- `py/watcher.py` — `handle_event()` wires `apply_ping` per event type; `_enqueue_screenshot()` returns bool; `_check_update_awareness()` ping + auto-relaunch + separate dedupe stores; `_build_relaunch_alert_payload()`; `_load/save_relaunch_state()`; `_maybe_burst_error()` + `_flush_error_burst()` for grouped failures; `trigger_screenshot()` restored; `_do_screenshot()` no longer gates on-demand behind `screenshots_enabled`; `AccountState` gets burst buffer fields
- `py/screenshot.py` — `enqueue()` returns bool, trigger-specific guards (scheduled/startup/event); capture-failure embed-only fallback in `_worker()`
- `py/history.py` — `compute_runtime_stats()`, `_fmt_secs()`, supporting helpers
- `ui/settings_tab.py` — Ping column in event table; `ping_update` and `auto_relaunch_on_update` settings with warning label
- `ui/history_tab.py` — Runtime Stats pseudo-button in account row; `_show_runtime_stats_popup()`
- `tests/test_v181.py` — 42 tests covering all items above

---

## v1.8.0
### Stable release — Update Awareness, Screenshot Reliability, PID-First Window Lookup

---

**Update awareness: grouped Discord alert**
- The update-awareness check now sends **one Discord message per check** instead of one per account/window
- The grouped embed lists all accounts that need updates, split into up to three fields (only included when non-empty), ordered: `Both script + DreamBot update needed` → `P2P Master AI script update needed` → `DreamBot client update needed`
- Each account appears as a bullet with specific version detail, e.g. `• Account: v2.141 → v2.143, DreamBot 4.1.67 shows NEW CLIENT AVAILABLE`
- Embed description: `Recommended: use /relaunch <account> to restart and load the latest version.`
- Account name, DreamBot client version, and script version are now all parsed from the window title (`DreamBot 4.1.67 - Account - P2P Master AI v2.141 - proxy`)
- Dedupe key is now per-account and includes account name, DreamBot version, local script version, latest version, and NEW CLIENT AVAILABLE flag — any of these changing allows a new alert

**Update awareness: schedule changed to UTC 6-hour slots**
- Previously: startup + daily at 2:00 PM PC local time
- Now: startup + every 6 hours at minute 20 UTC (00:20, 06:20, 12:20, 18:20), aligned shortly after the Cloudflare Worker cache refresh at :17
- Only one check fires per UTC slot even if the monitor is running across the minute boundary
- "No update found" and "no DreamBot windows found" log lines are now debug-only — main log only shows alerts and failures

**Screenshot logging: all screenshot-flow noise removed**
- All successful screenshot log messages are now fully silent in both normal and debug mode: no "Screenshot queued", no "Screenshot captured", no "Screenshot sent"
- All screenshot failure messages are now debug-only: "Screenshot failed", "Bot screenshot failed", "Gateway not ready — screenshot dropped", "No default webhook configured for screenshot", "Screenshot worker error", "Screenshot queue full"
- `ScreenshotService` now has an internal `_dbg()` helper that gates all screenshot messages behind the debug flag
- No change to screenshot capture, paint hide/show, Discord upload, or queue behaviour

**PID-first screenshot window lookup**
- `take_screenshot` now resolves the DreamBot window by PID first, falling back to title-based lookup only if needed
- **PID path:** reads cached PID from `launcher_state.json` via callback → `find_windows_for_pid(pid)` → accepts first visible window with `DreamBot` in title (account name not required — the PID is the ownership signal)
- **Title fallback:** existing `find_window_ids_by_name(account)` logic unchanged, still requires both `DreamBot` and account name in title to prevent wrong-window captures; on success, resolves and caches the window's PID for future screenshots
- New `find_windows_for_pid(pid)` platform helper in `platform_ops.py`: Linux via `xdotool search --pid`, Windows via `EnumWindows + GetWindowThreadProcessId`; filters visible + DreamBot title; never raises
- New public `get_account_pid(account)` / `set_account_pid(account, pid)` wrappers in `launcher.py` expose the existing `launcher_state.json` PID cache to the rest of the app
- `ScreenshotService` wired with `get_account_pid` and `set_account_pid` callbacks from watcher; `take_screenshot` receives them as optional keyword args
- **Startup PID cache population:** when `_startup_catchup` confirms an account is active, a short daemon thread calls `discover_account_process(account)` and saves the result — so PID-first lookup works on the very first screenshot of the session, even for clients not launched by the monitor

**Files changed:**
- `p2p_monitor.py` — version bump to 1.8.0; `inferno_rules.start_background_fetch()` wired at startup
- `py/watcher.py` — UTC slot scheduler; grouped alert; title parser extended with account + DB version; no-update log to debug; `_get_account_pid_cb` / `_set_account_pid_cb` / `_startup_cache_pid` methods; PID callbacks wired into `ScreenshotService`; startup PID daemon thread per active account
- `py/screenshot.py` — `take_screenshot` PID-first window resolution with title fallback and PID save-back; all screenshot log calls converted to `_dbg()`; new `_dbg()` helper on `ScreenshotService`; `find_windows_for_pid` and `get_pid_for_window` added to imports
- `py/launcher.py` — public `get_account_pid()` / `set_account_pid()` wrappers
- `py/platform_ops.py` — `find_windows_for_pid(pid)` with Linux (`xdotool`) and Windows (`EnumWindows`) implementations
- `ui/settings_tab.py` — update awareness label updated to reflect UTC 6-hour schedule
- `CHANGELOG.md`, `README.md` — updated for stable release
- `tests/test_update_awareness.py` — 27 tests: update awareness schedule, grouped alert, dedupe, screenshot silence/debug
- `tests/test_pid_screenshot.py` — 19 tests: launcher public API, `find_windows_for_pid`, `take_screenshot` PID-first/fallback/save-back, watcher PID callbacks

---

## v1.8.0-beta.3
### Local DreamBot Update Awareness + `/relaunch` command

**New: `/relaunch` Discord slash command**
- `/relaunch account:<name>` — restarts the named account; closes the client if open, then launches fresh (destructive counterpart to `/launch`)
- `/relaunch account:all` — restarts all preset accounts; open clients are closed first
- `/launch` is unchanged — still skips already-open accounts (non-destructive)

**New: DreamBot / P2P Master AI update awareness**
- Reads local DreamBot window titles once at monitor startup and daily at 2:00 PM PC local time
- Fetches `latest_version` from `https://p2p-sdn-watch.p2pmonitor.workers.dev/p2p-master-ai/latest`
- Compares numerically (so `v2.9` < `v2.143` correctly)
- Three alert cases posted to the main monitor Discord channel:
  - Script outdated only → ping with local vs latest version + suggest `/relaunch`
  - Script outdated + `NEW CLIENT AVAILABLE` in title → ping both updates needed
  - Script current + `NEW CLIENT AVAILABLE` in title → ping DreamBot client update only
- No alert when everything is current
- Deduplicated: state persisted in `~/.p2p_monitor/update_check_state.json`; same condition does not alert again after app restart
- If Cloudflare endpoint is unreachable, only the `NEW CLIENT AVAILABLE` alert can still fire
- Configurable: **Update Awareness** checkbox in Settings (default on)

**Auto-restart hardening: 0-minute delay**
- If the selected random delay is 0 minutes, schedules restart after 10 seconds instead of instantly
- Logged/reported as "in 10 seconds" — not "0 minutes"
- Preserves normal behavior for delays > 0

---

## v1.8.0-beta.2
### Auto Restart After Script Stopped (Stage 2 of v1.8.0)

**Auto restart client after Script Stopped**
- New setting: **Auto restart client after Script Stopped** (default off)
- When enabled, the monitor schedules a `relaunch_account()` call after detecting `Stopped P2P Master AI!` — this closes the DreamBot client, waits 10 seconds, and launches the account fresh (the script was stopped but the client is still open)
- All restarts use the Stage 1 launcher backend (`py/launcher.py`) — no duplicate process-control logic

**Relaunch safe delay shortened: 15 s → 10 s**

**Random restart delay window**
- New settings: **Restart delay min minutes** (default 1) and **Restart delay max minutes** (default 30)
- Each account gets an independently random delay within the window, so accounts do not all relaunch at the same moment
- Monitor tab logs the scheduled delay: `⏰ [Account] Auto restart scheduled in 7m after Script Stopped`

**Respect breaks on relaunch**
- New setting: **Respect breaks on relaunch** (default on)
- When enabled and the account was mid-break when the script stopped, restart is scheduled at the break's calculated end time instead of the random window
- Falls back to random delay if break end cannot be determined safely
- Monitor tab logs the schedule: `⏰ [Account] Auto restart scheduled at 6:40 AM (break end) after Script Stopped`

**Game update window gate**
- New setting: **Only auto restart during game update window (Tue/Wed 1–4 AM PT)** (default on)
- Hardcoded window: Tuesday and Wednesday 1:00 AM – 4:00 AM `America/Los_Angeles` (handles PST/PDT automatically)
- When the checkbox is off, every Script Stopped can schedule auto restart
- Monitor tab logs when skipped: `🔄 [Account] Auto restart skipped — outside game update window (Tue/Wed 1–4 AM PT)`

**Manual stop suppression**
- Detects manual stop signature in recent log lines: `User initiated script stop via Control Bar.`
- When found, auto restart is suppressed: `🔄 [Account] Auto restart skipped — manual script stop detected`
- Checked across the current log batch and a 30-line rolling buffer per account (handles split-batch edge case)

**Monitor-initiated relaunch loop prevention**
- When the monitor closes a client (via `/launch`, relaunch_account, or auto restart), a 5-minute suppress window is set before `terminate_process_tree` is called
- The resulting `Stopped P2P Master AI!` log line does not re-trigger auto restart during this window
- Monitor tab logs when suppressed: `🔄 [Account] Auto restart skipped — monitor-initiated relaunch in progress`

**Pending timer cleanup**
- If the monitor is stopped while a restart timer is pending, the timer is cancelled cleanly in `LogWatcher.stop()`
- Timer callbacks re-check `_running`, preset existence, and suppress state before calling relaunch

---

## v1.8.0-beta.1
### Safe Launcher Backend + Discord `/launch` (Stage 1 of v1.8.0)

**New: `py/launcher.py`** — shared launcher backend used by both the Launcher tab and Discord slash commands.

- `build_command(jar, preset)` — command construction moved out of the UI; single source of truth for both the tab and Discord
- `launch_account(cfg, account)` — fresh launch; refuses with a clear message if the account is already running (preserves UI safety behaviour)
- `relaunch_account(cfg, account)` — safely identifies and closes the existing DreamBot client, waits 15 seconds, then relaunches
- `smart_launch(cfg, account)` — auto-dispatch: relaunch if running, fresh launch if not (used by Discord and launch_all)
- `launch_all(cfg)` — smart-launch every preset account with a 5-second stagger between launches

**Safety contract (never violated):**
- Never kills by generic process name (`java.exe`, `DreamBot` wildcard, etc.)
- Only closes a client when the DreamBot window title can be matched to the requested account name
- If multiple windows match (ambiguous) → refuses with explanation
- If saved PID exists but window match fails → refuses with explanation
- Saved PID state (`~/.p2p_monitor/launcher_state.json`) is always validated before use; it is a cache, not truth

**New platform helpers in `py/platform_ops.py`:**
- `get_pid_for_window(window_id)` — Linux: xdotool getwindowpid; Windows: GetWindowThreadProcessId
- `is_pid_running(pid)` — psutil preferred, falls back to OS primitives
- `get_process_cmdline(pid)` — psutil preferred, falls back to /proc on Linux
- `terminate_process_tree(pid)` — psutil preferred; falls back to taskkill /T /F (Win) or SIGTERM/SIGKILL (Linux)
- `find_account_window_and_pid(account)` — window-title lookup + PID resolution; raises ValueError on ambiguous matches

**New Discord slash command: `/launch account:<name>`**
- If not running → launch it
- If running → close safely + wait 15s + relaunch
- `/launch account:all` → smart-launch all preset accounts with stagger
- Autocomplete shows all configured launcher presets + "All accounts"
- Per-account result icons: ✅ launched/relaunched, ⚠️ skipped (ambiguous/already-up), ❌ failed

**Launcher tab refactored:**
- `_do_launch` now delegates entirely to `launcher.launch_account`
- `_build_command` removed from the tab; UI imports `build_command` from `py/launcher.py`
- Existing UI behaviour preserved: error dialog if already running, log on success/failure

**Wiring:**
- `p2p_monitor.py` creates `on_launch_cb` / `on_launch_all_cb` lambdas using `_launcher.smart_launch` and `_launcher.launch_all`
- `LogWatcher` accepts and forwards these callbacks to `GatewayRunner` as thin passthrough (no launcher logic in watcher)

---

## v1.7.0
### Inferno Tracker

Added a stateful Inferno tracker that monitors gear checks and active Inferno attempts, posting outcomes and milestones to the Tasks Discord channel.

**Two tracked concepts:**

**Inferno Gear Check**
- Opens a gear-check window on `You have the stats and quests needed for Inferno` or `Possible gear clear for Infernal Cape`
- `Inferno requirements not met` emits its event unconditionally — no gear-check window required, covering the real-world path where only `Bossing Step 0` precedes the failure
- Buffers `Resource check failed N [...]` lines (no-colon format only — prevents false positives from Fishing/Questing resource checks)
- Emits exactly one outcome per gear-check window:
  - `Inferno gear check passed` — on `You have the gear needed for Inferno!` (resource buffer discarded)
  - `Inferno requirements not met` — on requirements failure line (unconditional)
  - `Inferno gear check failed: missing usable gear/supplies — <detail>` — if window closes with buffered failures; detail is capped and included in the message
  - `Inferno gear check failed: unknown reason — <detail>` — if window closes with suspicious lines but no resource failures or known pass/fail
- Status/monitor tab updates to `Task: Inferno / Activity: Gear Check` when window opens

**Inferno Attempt**
- Starts only on `[GAME] Wave: 1` — not on `Jumping in`
- Caches high-ping data and merges it into the start message: `Inferno started — Wave 1, ping 194ms, high ping override used`
- Tracks every wave internally; status tab shows current wave for all waves
- Discord milestone events sent only at waves: 7, 15, 24, 31, 41, 48, 56, 63, 67, 68, 69
- Each milestone deduplicated per attempt — replayed or duplicate log lines never double-send
- Death: `Inferno failed — died on Wave X after Yh Mm Ss` (highest wave before death)
- Success requires `Your TzKal-Zuk kill count is: X` — Wave 69 alone is only a milestone
- State resets cleanly on death, success, and script stop

**Architecture:**
- Hard state machine in `py/inferno.py` (`InfernoTracker` class)
- Soft regex patterns and milestone list in `inferno_patterns.json`, fetched from GitHub on startup (same fallback chain as `error_rules.json`: remote → cache → packaged → emergency)
- Pattern loader in `py/inferno_rules.py` — mirrors `py/error_rules.py`; bad remote JSON never crashes the monitor
- One `InfernoTracker` instance per `AccountState` in `watcher.py`
- All events route to the Tasks Discord channel via `post_task()`
- History tab records Inferno events as task-type entries

**Files changed:**
- `py/inferno.py` — new: `InfernoTracker` state machine, `_CompiledPatterns`
- `py/inferno_rules.py` — new: remote pattern loader (GitHub → cache → packaged → emergency)
- `inferno_patterns.json` — new: soft regex/milestone config (bundle with each release)
- `py/watcher.py` — added `InfernoTracker` to `AccountState`; feeds lines in `_process_lines`; resets on script stop
- `p2p_monitor.py` — version bump to 1.7.0; wires `inferno_rules.start_background_fetch()`
- `p2p_monitor.spec` — added `inferno_patterns.json` to bundled datas
- `install.sh` — added `inferno_patterns.json`, `py/inferno.py`, `py/inferno_rules.py`
- `update_manifest.txt` — added all new Inferno files
- `README.md` — added Inferno tracking to features; updated Windows updating section

---

## v1.6.0
### Windows Packaged Self-Updater & Source Update Improvements

**Windows packaged auto-update**
- The Windows `.exe` can now detect and apply updates from within the app (Settings → 🔄 Check for Update)
- On update, the new `.exe` is downloaded from the GitHub release asset, the old binary is replaced, and the app prompts to relaunch — no manual download required
- Manual download from the [Releases](https://github.com/p2pmonitor/P2P-Monitor/releases/latest) page remains available as a safe fallback

**Update manifest (`update_manifest.txt`)**
- Introduced `update_manifest.txt` at the repo root — a plain list of files the updater applies from each release zip
- Linux/source updates now apply only the files listed in the manifest rather than replacing everything, allowing selective updates without re-running `install.sh`
- The manifest is checked on every update so new files added in future versions are automatically included

**Reliability fixes**
- Discord thread self-healing: if a monitored thread is deleted while the bot is running, the monitor detects the 404 on next post, invalidates the stale thread ID, and re-creates the thread automatically — no restart needed
- Discord channel and webhook self-healing follows the same pattern: stale IDs are evicted and re-created on detection

**Files changed:**
- `p2p_monitor.py` — Windows updater logic; version bump to 1.6.0
- `update_manifest.txt` — new: manifest for selective file updates
- `py/discord.py` — thread/channel/webhook self-healing on post failure

---

### Remote Error Rules

Error detection patterns (`ERROR_TRIGGERS`, `_LOCK_REASON_PATTERNS`, `_SILENT_LOCK_NAMES`) have been moved out of `py/reader.py` into a GitHub-hosted JSON file (`error_rules.json`). Error patterns can now be added or updated without requiring users to upgrade the monitor.

**Fallback order:**
1. **GitHub remote** — fetched from repo on startup in a background thread
2. **Local cache** — `~/.p2p_monitor/error_rules_cache.json` — last valid downloaded copy
3. **Packaged JSON** — `error_rules.json` bundled with the app release
4. **Emergency fallback** — tiny hardcoded Python dict (empty rule set); monitor never crashes even if all other sources fail

**How it works:**
- On startup, `start_background_fetch()` is called after config loads — non-blocking, UI is never delayed
- Initial rules load synchronously from packaged JSON before the background fetch completes, so the first log poll always has a valid rule set
- If remote fetch succeeds, in-memory rules are replaced immediately and the result is saved to cache
- `parse_lines()` calls `get_rules()` at parse time — remote updates apply to future log polls without restarting
- Full-file validation on every source — bad regex or missing fields reject the entire file and fall back safely
- Debug mode logs which source was used: `[ERROR_RULES] Loaded from remote / cache / packaged JSON / emergency fallback`

**Files changed:**
- `py/error_rules.py` — new module: loader, validator, compiler, cache manager, fallback handler
- `py/reader.py` — removed hardcoded rule data; imports `get_rules()` from `error_rules`
- `error_rules.json` — new file in repo root; upload to GitHub and bundle with each release
- `p2p_monitor.spec` — updated `datas` to bundle `error_rules.json` with the Windows exe
- `update_manifest.txt` — added `py/error_rules.py` and `error_rules.json`
- `p2p_monitor.py` — wires `start_background_fetch()` in `App.__init__` after config loads

## v1.4.2
### Discord Thread Duplication & Deleted Thread Recovery Fix

**Issue 1 — sanitize_config bad prune (thread duplication on restart)**

- Fixed `sanitize_config()` incorrectly pruning `bot_thread_ids` when `logs_root` is set directly to an account folder (e.g. `C:\Users\<user>\DreamBot\Logs\Accname`) instead of the parent Logs folder
- Root cause: step 5 of sanitize iterated `logs_root` looking for account subfolders; when `logs_root` is itself an account folder it contains only log files, so no subfolders are found and all thread IDs are pruned as "missing" — every monitor restart created a full new set of 8 Discord threads
- Fix: sanitize now detects this condition via new helper `is_logs_root_account_folder()` — if `logs_root` contains `logfile-*.log` files directly, the subfolder-based prune is skipped entirely
- Single-account mode (pointing `logs_root` at an account folder directly) continues to work for monitoring — only the destructive pruning is suppressed
- Added a yellow warning label in Settings → DreamBot Logs Folder that appears whenever `logs_root` points to an account folder, explaining that thread IDs will be lost on restart and suggesting the parent Logs folder path
- Affected files: `py/config.py`, `ui/settings_tab.py`

**Issue 2 — deleted Discord thread recovery gap**

- Fixed `_bot_add_user_to_thread()` silently ignoring 404 / 10003 errors when a Discord thread has been manually deleted
- Previous behavior: stale thread ID stayed in config, membership check failed silently, running Bot Setup again had no effect because `_ensure_threads_for_account()` saw all 8 channel entries present and skipped thread creation entirely
- Fix: `_bot_add_user_to_thread(account, thread_id, token)` now accepts `account` as a first parameter and detects 404 / 10003 on both the GET membership check and the PUT add call
- On 404: new `_recover_deleted_thread(account, thread_id, token)` removes only the specific stale thread ID from config, evicts the account from `_threads_verified`, saves config, and spawns `_ensure_threads_for_account(account)` in a background thread to recreate only the missing thread
- Channel IDs and webhook URLs are not touched — recovery is targeted to the deleted thread only
- All call sites updated to pass `account` to `_bot_add_user_to_thread()`
- Fixed `_bot_force_panel()` incorrectly passing `channel_id` (a plain channel or fallback ID) to `_bot_add_user_to_thread()` — membership add now only fires when a confirmed saved monitor thread ID exists for the account
- Affected files: `py/watcher.py`

**Manual workaround for users already affected**

Close the monitor, open `config.json`, delete the account entry under `bot_thread_ids` (or clear `bot_thread_ids` entirely), save, reopen the monitor, and press Start. The monitor will find or create one clean set of threads. Also correct `logs_root` in Settings to point to the parent Logs folder.

## v1.4.1
### Config Sanitization Bugfix

- Fixed `sanitize_config()` deleting legitimate config keys that were never declared in `DEFAULT_CFG` — `launcher_jar`, `launcher_presets`, `hist_col_widths`, `screenshot_on_startup`, `ui_section_discord_open`, and `ui_section_notifications_open` are now included with correct defaults so the sanitizer preserves them
- Affected files: `p2p_monitor.py`

## v1.4.0
### Discord Self-Healing, Config Cleanup & Level 99 Detection

---

**Discord self-healing**

- When a Discord thread, channel, or webhook is deleted (error 10003 Unknown Channel / 10015 Unknown Webhook), the monitor now automatically detects the 404, invalidates the stale ID from config, recreates the missing resource, and retries the failed message once — no manual intervention or restart needed
- Retried messages include a footer note: "⚠ Thread/channel was recreated — screenshot may be delayed"
- Thread recovery: removes stale thread ID → evicts account from verified set → re-creates thread → retries post
- Channel recovery: removes channel ID + webhook URL + all associated threads → re-runs bot setup → retries post
- Webhook recovery: removes stale webhook URL → re-runs bot setup to recreate → retries post
- 401 Unauthorized: flags `bot_setup_done = False` and logs "update token in Settings and re-run Bot Setup"
- 403 Forbidden / 50001 Missing Access: logs "re-invite the bot and re-run Bot Setup" without clearing setup state
- Recovery limited to one retry per failure — no infinite retry loops
- Screenshot-queued posts also self-heal: `ScreenshotService` receives a `handle_post_error` callback and wraps both `post_discord` and `post_bot_image` calls with recovery; the retry lambda captures the image path so the screenshot is included in the retry post; file cleanup happens only after recovery completes
- New DiscordRouter callbacks: `invalidate_threads`, `ensure_threads`, `run_bot_setup`, `save_cfg`
- Affected files: `py/discord.py`, `py/watcher.py`, `py/screenshot.py`

---

**Config cleanup on startup**

- New `sanitize_config()` function runs at app startup after `load_config()`
- Removes unknown/stale config keys (e.g. old `github_token` from prior versions)
- Validates types: coerces int-like strings to int, 0/1 to bool where expected; resets values that can't be coerced
- Validates `bot_channel_ids` and `bot_webhook_urls`: removes entries with invalid channel names or empty values
- Validates `bot_thread_ids` structure: removes malformed entries, invalid channel names, empty IDs; normalizes int IDs to strings
- Prunes `bot_thread_ids` for accounts whose log folders no longer exist (only when `logs_root` is set and valid)
- Logs all corrections in debug mode; saves config only if corrections were made
- Affected files: `py/config.py`, `p2p_monitor.py`

---

**Level 99 detection**

- New regex `SKILL_99_RE` detects the special game message: "Congratulations, you've reached the highest possible {skill} level of 99"
- Level 99 events use the title "🎆 Level 99! 🎆" instead of "🎉 Level Up!" in Discord embeds (gold color unchanged)
- Level 99 **always notifies** regardless of the `levelup_every` interval setting — you never miss a max level achievement
- Log prefix uses 🎆 for level 99, 🎉 for regular level ups
- The `_is_99` flag flows from reader → watcher → discord cleanly; regular level ups are completely unaffected
- Affected files: `py/reader.py`, `py/discord.py`, `py/watcher.py`

---

**Other changes**

- Version bump: v1.3.16 → v1.4.0
- Updated `_log` tag detection in `p2p_monitor.py` to recognize 🎆 emoji for level 99 log coloring
- Updated DiscordRouter docstring to document new recovery callbacks


## v1.3.16
### Code Quality, Reliability & Windows Click/Screenshot Fixes

---

**Code quality & reliability (all platforms)**

- Fixed `set_last_seen` doing a full `load_offsets` + `save_offsets` disk cycle on every poll tick — now skips the write entirely if the stored value is already current; prevents unnecessary disk wear and reduces corruption risk on ungraceful shutdown (`py/history.py`)
- Fixed `slice_tasks` BREAK START entries getting an incorrect line index of `len(arr)` — the search hint passed to `_find_ts` was `"Break"` which never matches any real log line; now passes the actual log line as the search hint so timestamps and sort order are correct by design rather than by accident (`py/reader.py`)
- Fixed dead column condition `col == 'action'` in status tab column setup — column named `'action'` does not exist; corrected to `col == 'account'` so the account column gets its intended `minwidth` (`ui/status_tab.py`)
- Fixed unused `log_files = _get_log_files(d)` glob call in the main poll loop — result was immediately discarded on every tick; removed to eliminate redundant disk I/O (`py/watcher.py`)
- Fixed dir cache not invalidating when log folder is changed in Settings — `_dirs_last_check` is now reset to 0 on save so the new path takes effect within one poll cycle instead of up to 30 seconds later (`ui/settings_tab.py`, `py/watcher.py`)
- Fixed `_startup_catchup` on log rotation running synchronously on the status refresh thread — moved to a daemon thread; prevents UI stutter on large log files during rotation (`py/watcher.py`)
- Removed `BotRunner` tombstone class — no longer imported anywhere (`py/discord.py`)
- Removed redundant `import threading` inside `_send_startup_ping` — already imported at module level (`p2p_monitor.py`)
- Removed unused top-level imports `shutil` and `re` from `p2p_monitor.py`
- Fixed blank line between `def load_offsets` and its docstring (`py/history.py`)
- Added comment documenting the intentional deferred import of `discord.py` inside `ScreenshotService._worker` to prevent circular import (`py/discord.py`)

---

**Windows screenshot & click coordinate fixes**

**Root cause:** `get_window_geometry()` uses `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` which includes the invisible drop shadow border around DreamBot windows. This made the geometry origin and height larger than the actual clickable client area — clicking at DWM-based coordinates landed below the real window bottom (e.g. y=788 when client bottom was 680), causing clicks to hit whatever was underneath DreamBot. Crop and reference capture were unaffected because they operate in DWM/image space consistently.

- Added `get_window_title(window_id)` helper to `platform_ops.py` — retrieves actual window title via `GetWindowTextW` on Windows / `xdotool getwindowname` on Linux
- `take_screenshot()` now verifies both `"dreambot"` and the account name appear in the chosen HWND's title before any capture attempt; aborts with a clear error if not matched — prevents Discord or other windows from being captured via stale HWNDs (`py/screenshot.py`)
- Added `_get_paint_click_coords(wid)` — uses `ClientToScreen(hwnd, 0,0)` as anchor to get the real client area origin on the desktop, then adds `PAINT_BTN_X/Y_OFFSET` as client-relative offsets with no DPI scaling; `take_screenshot()` now uses this for all paint toggle clicks; crop/reference/visibility detection continues using DWM-based `_get_paint_btn_coords` unchanged (`py/screenshot.py`)
- Added `_get_client_click_pos(wid, offset_x, offset_y)` — same `ClientToScreen` anchor approach for force clicks; on Linux falls back to existing DWM geometry + DPI-scaled offset path which was already correct (`py/paint.py`)
- Updated all four force-click functions (`click_at_offset`, `do_force_skill`, `do_force_panel`, `do_force`) to use `_get_client_click_pos` on Windows (`py/paint.py`)
- Replaced `GetSystemMetrics(76/77/78/79)` virtual desktop bounds in `_click_at_windows` with `EnumDisplayMonitors` union — `GetSystemMetrics` returns logical pixels on scaled monitors causing wrong normalization on multi-monitor setups; `EnumDisplayMonitors` returns physical pixel rects regardless of DPI context; `GetSystemMetrics` retained as fallback (`py/platform_ops.py`)
- Restored DPI context after click injection via `SetThreadDpiAwarenessContext(old_ctx)` — previously the context was set but never restored (`py/platform_ops.py`)
- `_capture_btn_crop()` Windows path now validates crop box bounds before calling `img.crop()` — returns `None` if crop box falls outside image bounds; `_paint_full_cap.png` temp file cleaned up after each crop (`py/screenshot.py`)
- `_save_paint_reference()` now verifies crop file exists and has non-zero size before calling `shutil.move`; an existing valid reference can no longer be overwritten by a failed or empty crop (`py/screenshot.py`)


### Windows Screenshot & Click Coordinate Fixes

**Root cause:** `get_window_geometry()` uses `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` which includes the invisible drop shadow border around DreamBot windows. This made the geometry origin and height larger than the actual clickable client area — clicking at DWM-based coordinates landed below the real window bottom (e.g. y=788 when client bottom was 680), causing clicks to hit whatever was underneath DreamBot (ChatGPT, taskbar, etc.). Crop and reference capture were unaffected because they operate in DWM/image space consistently.

**Screenshot paint hide/show click fix (`py/screenshot.py`):**
- Added `_get_paint_click_coords(wid)` — uses `ClientToScreen(hwnd, 0,0)` as anchor to get the real client area origin on the desktop, then adds `PAINT_BTN_X/Y_OFFSET` as client-relative offsets with no DPI scaling; returns `None` on Linux where client area equals frame
- `take_screenshot()` now uses `_get_paint_click_coords` for all three paint toggle click sites, falling back to `btn_coords` on Linux; crop/reference/visibility detection continues using `_get_paint_btn_coords` (DWM-based) unchanged

**Force click fix (`py/paint.py`):**
- Added `_get_client_click_pos(wid, offset_x, offset_y)` — same `ClientToScreen` anchor approach; on Linux falls back to existing DWM geometry + DPI-scaled offset path which was already correct
- Updated all four force-click functions (`click_at_offset`, `do_force_skill`, `do_force_panel`, `do_force`) to use `_get_client_click_pos` on Windows

**Click injection fix (`py/platform_ops.py`):**
- Replaced `GetSystemMetrics(76/77/78/79)` virtual desktop bounds with `EnumDisplayMonitors` union — `GetSystemMetrics` returns logical pixels on scaled monitors causing wrong normalization; `EnumDisplayMonitors` returns physical pixel rects for every monitor regardless of DPI context; `GetSystemMetrics` retained as fallback
- Restored DPI context after click via `SetThreadDpiAwarenessContext(old_ctx)`

**Screenshot HWND guard (`py/screenshot.py`):**
- Added `get_window_title(window_id)` to `platform_ops.py` — retrieves actual window title via `GetWindowTextW` on Windows / `xdotool getwindowname` on Linux
- `take_screenshot()` now verifies both `"dreambot"` and the account name appear in the chosen HWND's title before any capture; aborts with a clear error if not matched — prevents Discord or other windows from being captured via stale HWNDs

**Crop/reference safety guards (`py/screenshot.py`):**
- `_capture_btn_crop()` Windows path now validates crop box bounds before calling `img.crop()` — returns `None` if `rel_x0 < 0`, `rel_y0 < 0`, or crop extends past image edge; `_paint_full_cap.png` temp file cleaned up after each crop
- `_save_paint_reference()` now verifies crop file exists and has non-zero size before calling `shutil.move`; an existing valid reference can no longer be overwritten by a failed or empty crop


### Code Quality & Reliability Fixes

- Fixed `set_last_seen` doing a full `load_offsets` + `save_offsets` disk cycle on every call — now reads and writes only when the stored value has actually changed, and skips the write entirely if the value is already current; prevents unnecessary disk wear and reduces corruption risk on ungraceful shutdown (`py/history.py`)
- Fixed `slice_tasks` BREAK START entries getting an incorrect line index of `len(arr)` — the search hint passed to `_find_ts` was `"Break"` (the synthesised task name) which never matches any real log line; now passes the actual log line as the search hint so timestamps and sort order are correct by design rather than by accident (`py/reader.py`)
- Fixed dead column condition `col == 'action'` in status tab column setup — column named `'action'` does not exist; corrected to `col == 'account'` so the account column gets its intended `minwidth` (`ui/status_tab.py`)
- Fixed unused `log_files = _get_log_files(d)` glob call in the main poll loop — result was immediately discarded on every tick; removed to eliminate redundant disk I/O every poll interval (`py/watcher.py`)
- Fixed dir cache not invalidating when log folder is changed in Settings — `_dirs_last_check` is now reset to 0 on save so the new path takes effect within one poll cycle instead of up to 30 seconds later (`ui/settings_tab.py`, `py/watcher.py`)
- Fixed `_startup_catchup` on log rotation running synchronously on the status refresh thread — moved to a daemon thread matching the initial startup catchup path; prevents UI stutter on large log files during rotation (`py/watcher.py`)
- Removed `BotRunner` tombstone class — no longer imported anywhere; dead code removed (`py/discord.py`)
- Removed redundant `import threading` inside `_send_startup_ping` — `threading` is already imported at module level (`p2p_monitor.py`)
- Removed unused top-level imports `shutil` and `re` from `p2p_monitor.py` — both are used only in submodules
- Fixed blank line between `def load_offsets` and its docstring — cosmetic editing artifact (`py/history.py`)
- Added comment documenting the intentional deferred import of `discord.py` inside `ScreenshotService._worker` to prevent circular import; guards against future refactors accidentally moving it to module level (`py/discord.py`)


### Windows DPI Scaling Fix for Button Clicks
- Added `get_window_dpi_scale(window_id)` helper in `platform_ops.py` — queries `GetDpiForWindow` for the actual DPI of the monitor the window is on; returns scale factor (1.0 at 100%, 1.25 at 125%, 1.5 at 150% etc); returns 1.0 on Linux and on any error
- Fixed paint hide/show click landing in wrong position at non-100% DPI — `PAINT_BTN_X_OFFSET` and `PAINT_BTN_Y_OFFSET` were hardcoded at 100% DPI assumptions; now scaled by DPI factor using `round()` for accurate physical pixel positions
- Fixed paint reference crop capturing wrong area at non-100% DPI — `PAINT_BTN_CROP_W` and `PAINT_BTN_CROP_H` now scaled by DPI factor; affects both Linux ImageMagick crop and Windows BitBlt crop
- Fixed all force commands (`/force Stats`, `/force Loot`, `/force Hide`, `/force +10m`, `/force -10m`, `/force Skip`) clicking wrong position at non-100% DPI — all `CLICK_OFFSETS` in `paint.py` now scaled by DPI factor at point of use
- Linux unaffected — `get_window_dpi_scale` always returns 1.0 on Linux, all multiplications are no-ops
- Note: only hardcoded UI offsets are scaled; window bounds are already physical pixels from the v1.3.14 DWM fix and are not scaled

### Paint Constants Centralized
- Moved `PAINT_BTN_X_OFFSET`, `PAINT_BTN_Y_OFFSET`, `PAINT_BTN_CROP_W`, `PAINT_BTN_CROP_H` from `paint.py` and `screenshot.py` into `platform_ops.py` as the single source of truth; both files import from there
- Added `paint_ref_scale.txt` companion file saved alongside the paint reference image — stores the DPI scale at snap time; `_paint_is_visible` reads this on every call and auto-resnaps the reference if scale has changed by more than 0.05 (e.g. window moved to different monitor or user changed DPI setting); guarded against resnap loops

## v1.3.14
### Windows Screenshot Overhaul
- Added `WindowBounds` NamedTuple for clean geometry results across all capture/geometry callers
- Added `_get_window_bounds(hwnd, debug_log=None)` shared helper — tries `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` first (true visible rendered frame), falls back to `GetWindowRect`; both use screen coordinate space consistent with `GetDC(NULL)` + BitBlt; DPI awareness set and restored per call
- Fixed Windows screenshot capturing wrong position and wrong content at non-100% DPI scaling and multi-monitor setups — root cause was mixed coordinate spaces (`GetClientRect`/`ClientToScreen` vs screen DC); now uses DWM bounds + `GetDC(NULL)` + BitBlt in consistent Pattern A
- Added proper `argtypes`/`restype` on all GDI/user32 calls (`GetDC`, `CreateCompatibleDC`, `CreateCompatibleBitmap`, `SelectObject`, `BitBlt`, `GetDIBits`, `DeleteObject`, `DeleteDC`, `ReleaseDC`, `PrintWindow`) — prevents handle truncation on 64-bit Windows where handles are 8 bytes but ctypes defaults to 4-byte `c_int`
- Added three-path fallback: DWM + BitBlt → GetWindowRect + BitBlt → PrintWindow last resort; silent to normal users, detailed logging in Debug Mode
- `get_window_geometry()` now uses `_get_window_bounds` — paint button coordinates and force command click positions use the same geometry as capture, ensuring alignment
- `capture_window_image()` accepts optional `debug_log` callback — passed through to capture backend for per-call geometry logging in Debug Mode
- Fixed stale comment in `screenshot.py` referencing PrintWindow; updated `platform_ops.py` docstring to accurately describe DWM-first capture backend
- Cleaned duplicate entries in CHANGELOG.md

## v1.3.13
### Windows Screenshot Fix
- Fixed screenshot capturing wrong position and cutting off content at non-100% DPI scaling (e.g. 125%) — replaced `GetClientRect` + `ClientToScreen` with `GetWindowRect` which returns the true physical pixel position and size of the window directly from Windows, no coordinate conversion or DPI math needed; works correctly at any DPI scaling setting

## v1.3.12
### Bug Fix
- Fixed launcher not working after v1.3.11 — the new `os.path.isfile(jar)` check introduced in v1.3.11 used `os` without importing it at the module level, causing a silent `NameError` that prevented any launch from completing on both Linux and Windows

## v1.3.11
### Bug Fix
- Fixed last-seen marker being lost on monitor shutdown — `save_offsets` was overwriting `offsets.json` with only in-memory byte offsets, clobbering `__last_seen` keys written directly to disk by `set_last_seen`; shutdown now merges disk contents with in-memory offsets before saving so both are preserved

## v1.3.10
### Bug Fixes
- Fixed backfill writing history entries with current time instead of the actual log timestamp — `parse_lines` returns the timestamp in the `ts` field but the backfill was calling `ev.get('time')` which always returned `None`, causing `append_history` to fall back to the current time; now correctly passes `ev.get('ts')`
- Fixed last-seen marker not advancing to end of file — marker is now always set to the final raw line of each file after all chunks are processed
- Fixed last-seen marker being lost on monitor shutdown — `save_offsets` was overwriting `offsets.json` with only in-memory byte offsets, clobbering `__last_seen` keys written directly to disk; shutdown now merges disk contents with in-memory offsets before saving

## v1.3.9
### Bug Fixes
- Fixed `append_history` call in backfill using wrong argument format — was passing the full event dict as the second argument instead of unpacking `type`, `value`, `activity`, `timestamp` as separate positional args; caused backfill error on startup
- Fixed break time showing in status tab for offline accounts — break time now shows `—` when account is offline, matching uptime behavior
- Fixed backfill last-seen marker not advancing to end of file — `new_last_seen` was only updated inside `_process_chunk` when events were found; now always set to the final line of each file after all chunks are processed, preventing re-processing of already-seen content on next startup

## v1.3.8
### History Duplication Fix
- Fixed root cause of duplicate history entries — `_backfill_history` was using filename sort to determine the active log file, which disagreed with `_get_active_log_file`'s mtime-based selection; the real active file was being processed from byte 0 as a rotated file, re-writing events already recorded live; backfill now uses mtime consistently with the poll loop
- Reverted `_base_log_name` rotation-suffix stripping — DreamBot `.log.1` files are independent older session files not rotated versions of `.log`; stripping the suffix caused incorrect scanned-set lookups
- Fixed break time persisting in status tab when account goes offline — `break_time` is now cleared when an account is detected as having no active session

### Windows Screenshot Fix
- Replaced `PrintWindow` with `BitBlt` from screen DC — `PrintWindow` was triggering DreamBot's Java renderer to repaint multiple times causing visible flickering and occasional black frames; `BitBlt` reads the screen compositor output directly with no repaints; window is already focused by caller so it is guaranteed to be on screen

## v1.3.7
### Windows Core Fixes
- Fixed monitor not detecting script activity on Windows — active log file selection was using newest-by-filename as fallback when handle scan is unreliable; DreamBot creates a new log file on each launch so the newest filename is often a nearly-empty new session file while the real active file is older; now uses most-recently-modified file (mtime) which correctly identifies the file DreamBot is actively writing to
- Fixed clicks landing on wrong window/monitor — replaced `mouse_event` with `MOUSEEVENTF_ABSOLUTE` with `PostMessage(WM_LBUTTONDOWN/UP)` sent directly to the target window handle; coordinates are client-relative via `ScreenToClient`, no normalization math, works correctly on any monitor layout and any DPI scaling
- Fixed intermittent black screenshots — added 150ms sleep after `PrintWindow` before reading the bitmap; DreamBot uses hardware-accelerated Java2D (DirectX) and the GPU needs time to composite the frame into the capture buffer

### Duplicate Launch Fix
- Replaced psutil cmdline inspection for duplicate launch detection with window title lookup using `find_window_ids_by_name` — psutil cmdline access fails silently on both Linux and Windows due to process access restrictions; window title matching is already proven to work correctly on both platforms

## v1.3.6
### Critical Windows Fix — Active Log File Detection
- Fixed monitor not detecting any script activity on Windows — the fallback log file selection was using newest-by-filename which picked a newly created empty file instead of the file DreamBot was actively writing to; fallback now uses most-recently-modified mtime which correctly identifies the actively-written file regardless of filename order; also naturally ignores rotated .log.1 files which are not touched after rotation

### Windows Click Fix — Multi-Monitor and DPI
- Replaced mouse_event(MOUSEEVENTF_ABSOLUTE) with PostMessage(WM_LBUTTONDOWN/UP) for all Windows clicks — old approach required coordinate normalization across the virtual desktop which broke on multi-monitor setups; new approach uses WindowFromPoint to find the exact window under the target coordinates, converts to client-relative coords, and sends directly to the window message queue; works correctly on any monitor layout and any DPI scaling, cursor does not physically move

### Windows Screenshot Fix
- Added 150ms sleep after PrintWindow before reading the bitmap — DreamBot uses hardware-accelerated Java2D rendering; without the sleep the captured bitmap was black mid-frame while the GPU was still compositing

### History Duplicate Fix
- Fixed duplicate history entries after log rotation — scanned records now use base filename without rotation suffix

### Startup Fix  
- Fixed startup task appearing for offline accounts — offline accounts now have _startup_done=True set immediately

### Force Command Fixes
- Fixed all three force commands failing when DreamBot window was minimized — now restores before getting geometry
- Fixed PrintWindow returning 0x0 client area for minimized windows

### Duplicate Launch Fix
- Fixed duplicate DreamBot client detection on both Linux and Windows

### Screenshot During Break Fix
- Fixed scheduled screenshots firing during breaks or for offline accounts

### UI Fixes
- Launcher tab: selection persists on account rows for Launch Selected; deselects only on action clicks
- History tab: deselects when clicking empty space
- Path browse dialogs normalize separators on Windows

### Bug Fixes
- Fixed duplicate history entries after log rotation — `logfile-X.log.1` was not recognised as already scanned after rotating from `logfile-X.log`; scanned records now use the base filename (without rotation suffix) so rotated files are correctly skipped
- Fixed startup task appearing for offline accounts — `get_account_rows` was calling `_startup_catchup` on accounts where `_startup_done=False` because they were skipped at startup; offline accounts now have `_startup_done=True` set immediately
- Fixed all three force commands (`do_force_panel`, `do_force_skill`, `do_force`) getting window geometry before focusing — if the window was minimized, geometry returned `None` and the command bailed silently; now restores if minimized, focuses, waits, then gets geometry
- Fixed `capture_window_image` (`PrintWindow`) failing with `window has no client area (0x0)` when window is minimized — now restores window before calling `GetClientRect`
- Increased sleep after restore from minimized to 0.5s (was 0.3s) — gives window time to render before geometry query and click

### UI Fixes
- Fixed launcher tab deselecting account immediately on click — selection now persists for account rows so "Launch Selected" works; deselect only fires on action column clicks (Launch/Edit/Delete) and clicks outside rows
- Fixed history tab not deselecting on click outside a row — now deselects when clicking empty space or non-row regions
- Added os.path.normpath to path browse callbacks in Settings and Launcher — normalises forward/back slashes from filedialog on Windows
- Fixed duplicate launch detection on both Linux and Windows — psutil cmdline fetched upfront via process_iter can return empty/None for processes that change state during iteration; now filters to java processes first then calls proc.cmdline() individually with per-process error handling
- Fixed scheduled screenshots firing during breaks — break check now also tests _break_start_ts (covers transition states) and script_running (skips offline accounts)

## v1.3.5
### Window Matching & Screenshot Fixes
- Fixed wrong window being captured on Windows — `find_window_ids_by_name` now requires both "DreamBot" AND the account name in the window title, preventing false matches against Discord threads or other windows containing the account name
- Replaced `ImageGrab.grab()` with `PrintWindow` Win32 API for window capture — captures the window's own render buffer regardless of whether it's in the background, minimized, or covered; eliminates black screenshots
- Paint button crop on Windows now uses `PrintWindow` + client-relative coordinate crop instead of screen region grab — works correctly regardless of window position or occlusion
- Added `SetThreadDpiAwarenessContext` to window geometry and click operations — coordinates are physical pixels on all DPI scaling settings; fixes click positions on 125%, 150%, 200% scaled displays

### Paint Reference
- Added "Snap Paint Reference" button in Settings → Paint Reference — focuses a DreamBot window, captures the paint button region, saves as reference, restores focus; includes instructions for correct state
- Paint reference capture now passes window handle (`wid`) through the detection chain so crops use `PrintWindow` rather than screen grabs

### UI Polish
- Split status tab action column into separate Mute and Screenshot columns — each has its own click zone, no more guessing the midpoint
- Split launcher tab action column into separate Launch, Edit, and Delete columns — same fix
- Treeview rows deselect on click and on refresh — no persistent blue highlight

## v1.3.4
### Windows Debug Noise Fix
- Fixed repeated `[DEBUG] handle scan unreliable` messages appearing in the monitor log on Windows — the message now only logs once per session on first occurrence instead of every time `check_active_sessions` or `_is_folder_active` runs

## v1.3.3
### Discord Rate Limit Fix
- Fixed HTTP 429 rate limit errors when adding the mention user to multiple Discord threads on startup — now checks thread membership first (GET before PUT) and skips the add if already a member; on subsequent startups where the user is already in all threads, no PUT requests are made at all
- If a PUT does hit a 429, waits the `retry_after` duration from the response and retries once before logging failure
- Fixed membership check not running for threads that already existed in config — `_ensure_threads_for_account` now verifies membership for all threads on every startup, not just newly created ones; the GET check is cheap so existing members are detected and skipped instantly

### Windows Launcher Fix
- Fixed blank CMD window appearing when launching DreamBot from the Launcher tab on Windows — added `CREATE_NO_WINDOW` creation flag on Windows so the process launches silently in the background; closing the CMD no longer kills DreamBot; Linux behavior unchanged

## v1.3.2
### Windows Performance Fix
- Fixed severe Windows lag on monitor start — `open_files()` on Windows queries NtQueryObject for every handle in every Java process; a running DreamBot JVM keeps hundreds of handles open, causing psutil to block for minutes even when filtering to java processes only
- Windows `get_open_log_handles()` now returns `reliable=False` immediately instead of attempting the scan; watcher uses name-based log file selection (newest `logfile-*.log`) as fallback, which already worked correctly
- Linux behavior completely unchanged — still uses `readlink /proc/*/fd/*`

## v1.3.1
### Bug Fixes
- Fixed Windows UI becoming unresponsive after starting the monitor — `push_refresh()` was spawning a new thread on every watcher event with no guard; threads accumulated faster than they completed, flooding the Tkinter event queue with pending treeview rebuilds; fixed with `_refresh_in_flight` flag
- Fixed status tab treeview doing a full delete+rebuild on every refresh — now updates rows in place; only rebuilds if the account set changes; significantly reduces Tkinter rendering work on every event
- Fixed task and activity showing `--` when restarting the monitor mid-break — now correctly shows "Break" with the break length in the activity column
- Fixed Discord developer link in README and Settings tab — `discord.com/developers` → `discord.com/developers/home`

## v1.3.0
### Performance Fix
- Fixed severe lag on Windows — `get_open_log_handles()` was scanning all running processes via psutil `open_files()` on every call, which is expensive on Windows
- Windows scan now filtered to `java`/`javaw` processes only — DreamBot always runs as Java; reduces scan from 100+ processes to 2-5
- Added 15-second result cache to `get_open_log_handles()` — scan runs at most once per 15 seconds regardless of how many times it's called; worst-case delay detecting a closed client is 15 seconds
- Linux behavior unchanged — `readlink /proc/*/fd/*` is already fast and benefits from the cache as a bonus

## v1.3.0-beta.1
### Windows Port — First Beta

This release introduces cross-platform support. Linux behavior is unchanged.
Windows is a first beta and requires manual validation before production use.

**Platform abstraction layer**
- Created `py/platform_ops.py` — owns all OS-specific operations; rest of codebase never calls platform tools directly
- `get_open_log_handles()` — Linux: `readlink /proc/*/fd/*`; Windows: psutil process scan; returns `HandleScanResult` with `paths`, `reliable`, and `reason` fields so callers can distinguish a confirmed empty scan from an unreliable one
- `is_account_process_running()` — psutil-based duplicate launch detection on both platforms; pgrep no longer used
- `open_path()` — Linux: xdg-open; Windows: os.startfile()
- `find_window_ids_by_name()` — Linux: xdotool; Windows: Win32 EnumWindows + title matching via ctypes
- `capture_window_image()` — Linux: ImageMagick import; Windows: PIL.ImageGrab + Win32 GetWindowRect
- `get_focused_window()`, `get_window_geometry()`, `is_window_minimized()`, `restore_window()`, `raise_and_focus_window()`, `minimize_window()`, `click_at()` — full window control API with Linux (xdotool/xprop) and Windows (ctypes Win32) backends
- `supports_paint_detection()` — returns True on Linux (ImageMagick) and True on Windows when Pillow is available; False otherwise
- `normalize_path()` — cross-platform path normalization for open-handle comparisons
- All platform_ops functions fail safely and never raise

**Watcher reliability on Windows**
- `HandleScanResult` distinguishes reliable scans (trust empty = no handles) from unreliable scans (psutil missing, scan failed)
- `check_active_sessions()` and `_is_folder_active()` fail open on unreliable scans — never force sessions offline based on an untrusted result
- `stop()` EOF pin skipped when scan is unreliable
- Path comparisons normalized with `os.sep` throughout

**Screenshot — end-to-end on Windows**
- `take_screenshot()` fully wired through platform_ops: find window → check minimized → restore → raise/focus → wait → capture → restore prior window
- Paint hide/show detection on Windows uses Pillow `ImageGrab.grab()` + `ImageChops.difference()` — same threshold as Linux; no ImageMagick required
- Paint detection skipped gracefully on Windows if Pillow unavailable; screenshot still succeeds
- `SCREENSHOT_DIR` moved from `/tmp/screenshots` to `~/.p2p_monitor/screenshots` — persistent across reboots
- `os.devnull` replaces `/dev/null` in ImageMagick compare call

**Force commands on Windows**
- `paint.py` fully routed through platform_ops — no direct xdotool/X11 calls
- `do_force()`, `do_force_skill()`, `do_force_panel()` use `get_window_geometry()`, `raise_and_focus_window()`, `click_at()` from platform_ops
- Windows `raise_and_focus_window()` uses AttachThreadInput for reliable foreground activation
- Windows `click_at()` uses `MOUSEEVENTF_ABSOLUTE` with normalized 0–65535 coordinates

**Updater — frozen/packaged builds**
- `_is_frozen()` helper detects PyInstaller packaged execution
- `SCRIPT_PATH` uses `sys.executable` when frozen, `__file__` for source installs
- Frozen builds: update check opens GitHub releases page in browser; no in-place file patching; no `os.execv`
- Source installs: existing staged patch + restart behavior unchanged

**Runtime dependency handling**
- Both discord.py pip-install paths (`discord.py` gateway runner and settings bot setup) detect frozen mode and emit a clear error instead of attempting pip install
- `is_frozen()` centralized in `py/util.py`; imported by all callers

**Windows packaging**
- `requirements-windows.txt` — psutil, Pillow, pystray, discord.py
- `p2p_monitor.spec` — PyInstaller spec with hidden imports for all bundled deps; `console=False`
- `WINDOWS_BUILD.md` — build steps, known beta limitations, distribution, troubleshooting

**Debug logging**
- Debug mode toggle in Settings → Debug — routes previously silent internal failures to monitor tab
- `_dbg()` helper on LogWatcher; `log_fn + debug` params on history/config functions
- `config.py` now logs load/save failures in debug mode

**Anonymous usage stats**
- Startup ping sends version + OS to `stats.p2pmonitor.workers.dev` on app open; background thread; 3s timeout; no retry; no personal data
- Can be disabled in Settings → Debug → Enable anonymous usage stats

**Install fix**
- `install.sh` now copies `py/platform_ops.py` and `ui/launcher_tab.py` — previously missing, causing ImportError on fresh installs
- Added `psutil` to pip install step in `install.sh`
- `install.sh`, `update_manifest.txt`, and actual repo files are fully in sync

---

## v1.2.4
### Reliability & Launcher

- Added debug mode toggle in Settings — routes previously silent internal failures to the monitor tab; covers history reads/writes, config I/O, offset restore, backfill, rotation, and screenshot operations
- Replaced `pgrep -f account` in launcher with `psutil` process inspection — checks for java + `-jar` + account name in command line, reducing false positives; falls back to `pgrep` if psutil unavailable
- Added event dict contract documentation above `handle_event()` in watcher.py
- Added TODO markers to large functions noting intentional deferral of refactoring

---

## v1.2.3
### Slayer & History Fixes

- Fixed status tab staying stuck on "Fetching task..." after a new Slayer task is assigned
- Added skip notification for tasks cancelled immediately as unsupported — task ID 126 identified as Spiritual creatures; other unknown IDs show as "Unknown task (ID X)"
- Added automatic history dedup after backfill — cleans duplicate entries left by previous versions
- Fixed duplicate log message ("Removed" + "Cleaned") for history dedup

---

## v1.2.2
### History Dedup Fix

- Fixed history duplication when DreamBot closes before the monitor is stopped — active log files are EOF-pinned on monitor exit so backfill on next startup finds nothing new to re-read

---

## v1.2.1
### Status & Stability Fixes

- Fixed task and activity showing blank in status tab when monitor starts mid-session
- Fixed status tab not updating to new Slayer task after completing
- Added dedup to history writes — identical consecutive entries are silently dropped
- Added "Screenshot on monitor startup" toggle in Settings (defaults off)

---

## v1.2.0
### CLI Launcher & Log Detection

- Added CLI Launcher tab for launching DreamBot clients directly from the monitor
- Replaced `lsof` with `readlink /proc/*/fd/*` for active log file detection — faster and no external dependency
- Fixed duplicate `_auto_refresh` thread leak in status tab
- Fixed teleport reason now captures item name correctly
- Eight reader.py bug fixes: script events all occurrences, slayer dedup resets, cancel search range, single-anchor complete, pet+collection dedup, QuestStep traversal reason, slice_last_task no Break, teleport reason from brackets
