# CLAUDE.md — DIALS Workflow GUI

Context file for picking this project back up. Read this before touching
the code again.

## What this project is

A wxPython GUI (the `dialsgui/` package, launched via `dials_gui.py`) that wraps the DIALS
macromolecular crystallography command-line suite (`dials.import`,
`dials.find_spots`, `dials.index`, `dials.refine`, `dials.integrate`,
`dials.symmetry`, `dials.scale`, `dials.merge`/`dials.export`, plus the
viewers `dials.show`, `dials.image_viewer`,
`dials.reciprocal_lattice_viewer`, `dials.report`) so a user can click
through the workflow described in:

https://github.com/graeme-winter/dials_tutorials/tree/main/ccp4-dls-2024
(and the near-identical `ccp4-aps-2024/WORKFLOW.md` in the same repo,
which is what was actually fetched and used as the reference — the exact
`ccp4-dls-2024` tree couldn't be browsed directly because GitHub's
`robots.txt` blocks the folder-listing / API paths from the fetch tool
used at the time; only individual file pages and search snippets were
reachable).

Files:
- `dials_gui.py` — thin launcher (`from dialsgui.app import main`); keeps
  `python3 dials_gui.py` working. Also holds the user-facing module docstring.
- `dialsgui/` — the application package (see "Package layout" below).
- `make_app.sh` — bundles the launcher **and** the `dialsgui/` package into a
  macOS `.app`, extracting the icon from `dialsgui/icon.py`.
- `pyproject.toml` — pip packaging (setuptools/PEP 621). Distribution name
  `dials-gui`, import package `dialsgui`, console scripts `dials-gui` and
  `dials.gui` → `dialsgui.app:main`. `wxPython` is a hard dependency;
  `matplotlib` is the optional `[plots]` extra (soft dependency, Plots tab
  only). Version lives here (currently `1.0.0`) and is also hard-coded in
  `make_app.sh` (`VERSION`) — bump both together.
- `LICENSE` — BSD-3-Clause.
- `README.md` — user-facing usage doc, written alongside the code.

## Package layout

The code was originally one ~3600-line `dials_gui.py`. It has since been
split (code moved **verbatim**, behaviour unchanged) into `dialsgui/`:

- `dialsgui/icon.py` — `APP_ICON_PNG_BASE64` + `get_app_icon()`. Kept as its
  own small module so `make_app.sh` can still regex the base64 constant out of
  a single file; the constant's parenthesised-string-literal format is
  load-bearing for that regex — don't reflow it.
- `dialsgui/model.py` — the `ExtraField` / `InputSpec` / `StepDef` dataclasses.
  No GUI import.
- `dialsgui/steps.py` — `STEPS`, `TOOLS`, `STATUS_ICONS` (imports `model`).
- `dialsgui/parsers.py` — every pure `summarise_log` / `parse_*` / `corrmat_*`
  / `extract_*` function. **No wx / matplotlib import** — importing this module
  must stay cheap and GUI-free so the parsers can be unit-tested standalone.
- `dialsgui/runner.py` — `ProcessRunner` + the `_WidgetVar` / `_FalseVar`
  value adapters.
- `dialsgui/frame.py` — `DialsFrame` (the ~2000-line GUI class) and the
  **soft** matplotlib import (`HAVE_MPL`, `Figure`, `FigureCanvas`,
  `NavigationToolbar`). This is where the bulk of GUI work happens.
- `dialsgui/app.py` — `main()` (creates the `wx.App`, shows the frame).
- `dialsgui/__init__.py` — deliberately import-light (just a docstring); it
  must **not** import wx, so `import dialsgui.parsers` works without a GUI.

Dead parsers flagged in earlier notes were removed during the split:
`parse_find_spots`, `parse_refine_steps` (superseded by
`parse_all_refine_steps`), `parse_find_spots_histograms`,
`parse_refine_by_experiment`, `parse_cluster_list`, plus the regexes used only
by them (`_IMAGESET_RE`, `_SWEEP_COUNT_RE`, `_RMSD_BY_EXP_RE`, `_CLUSTER_*`).
`_FIND_SPOTS_RE` and `_REFINE_HEADER_RE` were kept — they're still used by the
surviving `parse_find_spots_by_imageset` / `parse_all_refine_steps`.

## GUI toolkit: wxPython (ported from Tkinter)

**This application is now wxPython.** It was originally written in Tkinter
and later ported to wxPython; the port preserved all behaviour, tab layout,
and the pipeline/parsing logic verbatim — only the widget layer changed.
When reading older notes below that mention Tk-isms, mentally map them onto
their wx equivalents:

- `DialsGUI(tk.Tk)` → **`DialsFrame(wx.Frame)`** (the `main()` function now
  creates a `wx.App`, instantiates `DialsFrame`, `Show()`s it, and calls
  `app.MainLoop()`).
- `tk.StringVar` / `tk.BooleanVar` + `trace_add("write", …)` → each editable
  field is a real wx control (`wx.TextCtrl` / `wx.ComboBox` / `wx.CheckBox`)
  wrapped in a tiny **`_WidgetVar`** adapter that exposes `.get()` / `.set()`
  over the control's `GetValue`/`SetValue`. This is why all the ported
  command-building / plot-source helpers still call `.get()` on entries in
  `self.field_vars` / `self.input_vars` unchanged. "Traces" became ordinary
  event bindings (`EVT_TEXT` / `EVT_COMBOBOX` / `EVT_CHECKBOX`) that call
  `_update_command_preview` (and, for the corr-matrix `use_scaled` toggle and
  the scale cluster selector, their dedicated callbacks).
- `self.workdir` was a `StringVar`; it is now a plain `self.workdir_value`
  string plus a `workdir` **property** returning a shim object with
  `.get()`/`.set()`, again so the many `self.workdir.get()` call sites in the
  ported helpers work untouched. The top-bar `wx.TextCtrl` keeps
  `workdir_value` in sync via an `EVT_TEXT` handler and is updated by
  `.set()`.
- `ttk.Notebook` → `wx.Notebook`; each tab is a `wx.Panel`. `select_step()`
  rebuilds the notebook in `self.main_panel` (clearing `self.main_sizer`
  with `Clear(delete_windows=True)`), the wx analogue of destroying and
  recreating the Tk `self.main` children.
- Read-only text areas (`tk.Text` … `state="disabled"`) →
  `wx.TextCtrl(style=TE_MULTILINE|TE_READONLY|TE_DONTWRAP|HSCROLL)`;
  `_set_text` uses `ChangeValue`, `_append_text` uses `AppendText`.
- `ttk.Progressbar` → **`wx.Gauge`** (fixed 0–100 range). The old
  `progress_bar.config(maximum=…, value=…)` + `progress_var.set(label)`
  pairs are replaced by a single **`_set_progress(fraction, label)`** helper
  (fraction in 0..1 → gauge 0..100, plus the `self.progress_label` text).
  `self.progress_var` no longer exists — it's `self.progress_label`.
- The `after()`-driven poll loop (`_poll_runner` rescheduling itself via
  `self.after(150, …)`) → a repeating **`wx.Timer`** (`self._timer`, ~10 Hz)
  bound to **`_on_timer`**, started in `run_step` and stopped on `done` in
  `_finish_step`. `ProcessRunner` is otherwise identical (background thread +
  `queue.Queue`), and the per-poll body (drain queue, append output, throttle
  plot redraws every 5th poll, finish on `("done", rc)`) is line-for-line the
  same as the old `_poll_runner`.
- `self.after(0, cb)` / `after(ms, cb)` (report worker, delayed report open)
  → `wx.CallAfter` / `wx.CallLater`.
- `messagebox.*` → `wx.MessageBox(msg, caption, style)`; `filedialog.
  askdirectory` → `wx.DirDialog`; `filedialog.askopenfilenames` →
  `wx.FileDialog(FD_OPEN|FD_MULTIPLE)`; `simpledialog.askstring` →
  `wx.TextEntryDialog`; the `launch_tool` `tk.Toplevel` → a modal
  `wx.Dialog` with OK/Cancel.
- Plots-tab page selector / scale cluster / log cluster: the three
  `ttk.Combobox` + `StringVar` pairs are now bare `wx.ComboBox`
  (`CB_READONLY`) read directly via `GetValue()` in `_selected_cluster`,
  `_current_plot_page`, `_scale_log_view_name`, etc.; `_update_plot_pages`
  uses `GetStrings()`/`Set()`/`SetValue()`, and `_select_plot_page` replaces
  the old `self.plot_page_var.set(...)`.

The pure logic — `ExtraField`/`InputSpec`/`StepDef`, `STEPS`, `TOOLS`,
`summarise_log`, and **every** `parse_*` / `corrmat_*` / `extract_*`
function — is byte-for-byte the Tkinter version; it never imported Tk and
still doesn't import wx. Only the classes from `_WidgetVar` / `ProcessRunner`
/ `DialsFrame` downward are wx-specific.

## Status

**The core pipeline GUI is confirmed working well against a real DIALS
install** (all 10 steps, command building, log streaming, Summary/Full
Log tabs, viewer launch buttons). Don't rework any of that without a
specific new bug report.

**A targeted "Plots" tab now exists again — but it is nothing like the
old one.** History: the *original* Plots tab auto-generated bar charts by
grepping every table it could find out of the text logs
(`parse_ascii_tables()`, `parse_merging_stats_block()`,
`extract_plot_series()`, `_build_plots_tab()`, `_refresh_plots()`,
`_add_bar_chart()`, plus a hard `matplotlib` dependency). Per user
feedback it "plotted a lot of irrelevant things" and was deleted wholesale
in favour of the `dials.report` button. The **current** Plots tab is a
deliberate, narrow reimplementation added later, on explicit request, to
show a *fixed, hand-picked* set of live traces for four specific steps —
NOT to re-derive arbitrary charts from arbitrary tables. Keep it that way:
if asked to "add more plots", prefer pointing at `dials.report` unless the
request names a specific, known-useful series.

Design of the current Plots tab:

- It appears **only** on steps whose `StepDef.plot_kind` is set:
  `find_spots`, `refine`, `integrate`, `scale`. All other steps have no
  Plots tab (the old one appeared everywhere — that was part of the
  problem).
- It updates **live** off the streamed stdout while the step runs
  (throttled to roughly every 5th poll in `_on_timer`), then does a
  definitive redraw from the on-disk `dials.<program>.log` in
  `_finish_step`, and also populates immediately when you re-select an
  already-run step (from its log).
- `matplotlib` is once again a dependency, but a **soft** one: it's
  imported in a `try/except` at module top into `HAVE_MPL`, the backend is
  forced to `WXAgg` (the wxPython embedding uses `FigureCanvasWxAgg` /
  `NavigationToolbar2WxAgg` from `matplotlib.backends.backend_wxagg`,
  imported under the local aliases `FigureCanvas` / `NavigationToolbar`),
  and if the import fails the tab still appears but just shows a "install
  matplotlib to enable this" note. Nothing else in the GUI depends on it.
  (So the old "matplotlib is no longer a dependency" note below is obsolete
  — see the updated risk list.)

What each `plot_kind` shows (all parsing done by pure, Tk-free functions
near the top of the file — `parse_find_spots`, `parse_refine_steps`,
`parse_integrate_blocks`, `parse_integrate_progress`,
`parse_integrate_summary`, `parse_scale_merging` — each tolerant of
partial/streaming input so live updates work):

- `find_spots`: line graph of strong pixels per image, from
  `Found N strong pixels on image M` lines.
- `refine`: RMSD_X / RMSD_Y (mm, left axis) and RMSD_Phi (deg, right axis)
  vs refinement step, from the "Refinement steps" table. Reads rows after
  the *last* "Refinement steps" header (there can be more than one
  macrocycle).
- `integrate`: a live `wx.Gauge` for block processing +, at the
  end, a 2×2 grid of line graphs vs image number from the "Summary vs
  image number" table (I/sigma sum & prf, full/part counts, CC prf, RMSD
  XY). The progress bar accounts for the fact that the block loop runs
  **twice** (profile modelling pass, then integration pass): it counts
  `Frames: A -> B` lines, compares against the number of blocks in the
  block table, and shows "Pass 1/2" or "Pass 2/2" accordingly.
- `scale`: 2×2 grid of per-resolution-bin merging stats (<I/sigma>; CC1/2
  & CC_anom; Rmerge/Rmeas/Rpim; completeness & multiplicity). **X axis is
  1/d²** (d = geometric mean of the bin's d_min/d_max) because resolution
  bins are non-linear, but tick labels show the actual d in Å. Trailing
  significance markers (`*`) on cc1/2 / cc_ano are stripped. The final
  overall-summary row (spans the whole resolution range) is detected and
  separated out of the per-bin series — it's reported in the status line
  instead — so it doesn't distort the plots. **Overall-row detection**
  keys off the last data row having the minimum d_min *and* a resolution
  span wider than any single bin (not off d_max equality — DIALS'
  overall d_max, e.g. 54.95, can differ slightly from the first bin's,
  e.g. 55.01; an earlier draft that compared d_max exactly failed on the
  real data, so don't "simplify" it back to that).

**This new Plots tab was tested in the sandbox with matplotlib present
(via the Agg backend) and against all the real sample outputs the user
provided** — the parsers and every `_plot_*` method were unit-tested and
sample figures rendered and eyeballed. What still hasn't been tested is
the tab running *inside a live wx event loop against a live DIALS run* —
i.e. the `FigureCanvasWxAgg` embedding, the `wx.Timer`-driven live redraw
cadence during a real multi-minute `dials.integrate`, and that the
streamed stdout actually contains these tables in the same form DIALS
writes them to the `.log` (the samples were pasted by the user; confirm
the live stdout matches). That's the same "never run in a real GUI"
gap called out below.

**The `dials.report` per-step button is unchanged** and still the primary,
full-fidelity view; the Plots tab complements it with a few live traces,
it does not replace it.

## Multi-crystal workflow (COWS_PIGS_PEOPLE) — added later

A second, larger extension added support for the multi-crystal tutorial
(`ccp4-dls-2024/COWS_PIGS_PEOPLE.md`): import many sweeps, process them
together, cluster by isomorphism, scale each cluster. Read that tutorial
before touching any of this. What was added:

- **Index gets a `joint=false` toggle.** New `ExtraField.check_value`
  field lets a ticked checkbox emit an arbitrary `key=value` (here
  `joint=false`) instead of the default `key=True`. One `dials.index` run
  indexes every crystal independently — do NOT loop per data set.
  Separately, `ExtraField.default` now controls a checkbox's *initial*
  state: `_build_setup_tab` reads it (`"true"/"1"/"yes"` → ticked), so a
  check field can start checked. `joint` and `anomalous` leave `default`
  empty (start unticked); `significant_clusters.output` sets `default="True"`.
- **Two new steps**, both `optional=True`, inserted between Symmetry and
  Scale: `cosym` (`dials.cosym`, id `cosym`, title "8b") which replaces
  `dials.symmetry` for many crystals and writes the same
  `symmetrized.expt/.refl`; and `correlation_matrix`
  (`dials.correlation_matrix`, id `correlation_matrix`, title "8c") with a
  `significant_clusters.output` checkbox (**checked by default**, emits
  `significant_clusters.output=True`, so cluster_N files are written unless
  the user unticks it) and `plot_kind="correlation_matrix"`.
  Symmetry was retitled "8. Symmetry (single crystal)".
- **Cluster-aware Scale (two selectors: RUN target vs VIEW target).** If
  `cluster_N.expt` files exist (from correlation_matrix with output
  clusters), the Scale Setup tab shows a **"Cluster to scale" selector**
  (`self.scale_cluster_var`) — the RUN target. A trace on it fills the
  Experiment/Reflection input fields with `cluster_N.expt/.refl` (or
  restores `symmetrized.*` on '(none)'), so the input fields are the single
  source of truth; `_build_command` just reads the fields and, when a
  cluster is the run target (`_selected_cluster()`), appends
  `output.experiments=scaled_cluster_N.expt` /
  `output.reflections=scaled_cluster_N.refl` /
  `output.html=dials.scale.cluster_N.html` /
  `output.log=dials.scale.cluster_N.log` so per-cluster runs never overwrite
  each other (tutorial's "mkdir per cluster", one directory). NOTE:
  `_build_command` no longer overrides the inputs itself (it used to) — the
  selector-driven field fill does that now.

  Separately, the Scale **Plots tab page selector** is the VIEW target for
  the PLOTS: `_plot_scale` builds its page list from
  `_scale_result_clusters()` (globs `dials.scale.cluster_(\d+)\.log` —
  clusters with results ON DISK) plus a "plain" page if `dials.scale.log`
  exists, and reads the selected cluster's log itself via
  `_read_workdir_file`. `_scale_view_cluster()` reads the page;
  `_scale_log_name()` (used by plot seeding) prefers the view target,
  falling back to the run target then plain.

  The **Full Log tab has its OWN independent cluster selector**
  (`self.log_cluster_var` / `self.log_cluster_combo`, built in
  `select_step`'s log-tab section for scale), defaulting to
  "dials.scale.log (default)" and offering each `dials.scale.cluster_N.log`
  found. `_refresh_log_tab` for scale uses `_scale_log_view_name()` (reads
  that selector) — NOT `_scale_log_name()` — so the Full Log (and the
  Summary digest built from the same text) is decoupled from the Plots view
  and stays on the plain log by default. The Summary tab therefore follows
  the Full Log selector, not the Plots page. `_finish_step` for a scale
  cluster run sets the PLOTS page to the just-scaled cluster (fresh results
  shown) and refreshes the Full Log selector's *choices* to include the new
  cluster, but leaves the Full Log selection on its default. This lets you
  scale cluster 0, then cluster 1, and still flip back to cluster 0's plots,
  while independently viewing any cluster's full log. Helpers:
  `_available_clusters()` (run-target list, globs `cluster_(\d+)\.expt`),
  `_selected_cluster()` (run target), `_scale_result_clusters()` /
  `_scale_view_cluster()` (plots view target), `_scale_log_view_name()`
  (full-log view target), `_read_workdir_file()`.
- **Per-data-set / per-cluster plot pagination.** `_build_plots_tab` adds
  a page-selector combobox (`self.plot_page_var` / `plot_page_combo`) for
  find_spots/refine/integrate/scale. `_update_plot_pages(options)` repopulates
  it lazily as data arrives (preserving a valid selection);
  `_current_plot_page()` reads it. Integrate uses it to show one data set
  at a time (`integrate_summary_by_dataset` groups the "Summary vs image
  number" rows by their first column, the imageset/data-set ID 0..N). NOTE:
  this parser was rewritten after an initial version only ever showed the
  *last* data set - DIALS may print the summary as one combined table OR as
  one table per imageset (each with its own "Summary vs image number"
  header), and the first version anchored on the last header and stopped at
  the first blank line, so it captured only the final block. The current
  version scans the whole text and treats any 13+ column pipe row whose
  first two cells are integers as a data row (header/unit/border rows fail
  the int parse), so it collects every block regardless of layout. If a
  future DIALS changes the column count or the ID-first ordering, this is
  the spot to revisit. refine shows the multi "RMSDs by
  experiment" table when present (`parse_refine_by_experiment`) else the
  single-crystal convergence table; find_spots draws one line per imageset
  on shared axes (`parse_find_spots_by_imageset` splits the output on the
  banner blocks — image numbers restart per imageset, so each imageset is
  its own series/line, coloured from `tab20` with cycling markers so many
  imagesets stay distinct, and because every refresh re-parses the whole
  accumulated stdout, earlier imagesets persist rather than being
  overwritten). IMPORTANT: the banner regex `_FIND_SPOTS_IMAGESET_RE` must
  match DIALS' actual wording, which is **"Finding strong spots IN imageset
  N"** (confirmed against dials.algorithms.spot_finding.finder) — an earlier
  version used "on imageset" and never matched, so every "Found N strong
  pixels on image M" line fell into a single fallback series and the plot
  showed one joined line in one colour (the reported bug). The regex now
  accepts in/on and optional "strong" for version robustness. The old
  approach — one continuous global series with guessed equal-length
  sweep-boundary lines, via `parse_find_spots`/`parse_find_spots_histograms`
  — was wrong for a second reason too (image numbers restart per imageset,
  so the dict-keyed `parse_find_spots` collapsed every imageset onto image
  1..100 and only the last survived); those two functions are retained as
  standalone parsers but are no longer used by the plot.
- **correlation_matrix plots come from HTML, not a `.log`.** This is the
  one plot kind whose source is `dials.correlation_matrix.html`, because
  the plottable data lives in `var graphs_X = {...}` Plotly-JSON blobs
  embedded in that file (stdout only has the textual cluster list).
  `_plot_source_text(step)` returns the HTML for this step and the `.log`
  for all others, and is used at every plot-refresh call site (select_step
  seeding, `_finish_step`, the Refresh button, the page combo). During a
  live run the graphs simply show "pending" until the HTML is written at
  the end — that's expected. Extraction: `extract_corrmat_graphs()`
  (balanced-brace scan + stdlib `json`; `Infinity` in the reachability
  data parses fine via json's default `parse_constant`), then
  `corrmat_matrix` / `corrmat_cluster_series` / `corrmat_xy` shape
  individual blobs. `_plot_correlation_matrix` draws up to six panels:
  correlation + cos-angle heatmaps, OPTICS reachability, cosym PCA
  coordinates, dimensions residual (log-y), Rij histogram. The
  `graphs_pca_analysis` SPLOM blob is deliberately NOT plotted (too complex
  for a small matplotlib panel). Plotly rgb() colours are 0-1 floats in a
  format matplotlib won't take directly, so cluster colours are left to
  matplotlib's cycle rather than parsed — don't "fix" this by feeding the
  rgb strings straight in.
- **cosym plots also come from HTML, not a `.log`** (added 2026-07-15,
  mirroring correlation_matrix on explicit request). The cosym StepDef gained
  `plot_kind="cosym"`; `_plot_source_text` returns `dials.cosym.html`
  (via `_cosym_html_text()`) for it, and `_finish_step` / the Refresh button
  treat it like correlation_matrix (plot from HTML, never fall back to
  `live_output`; "Refresh plots from HTML" label; no page selector — a fixed
  multi-panel view). Extraction reuses the **generic** `extract_corrmat_graphs`
  (the `var graphs_X = {...}` scanner is program-agnostic — the "corrmat" name
  is historical). New shaping helpers in `parsers.py`: `cosym_scatter_series`
  (multi-trace scatter → float-coerced x/y; DIALS emits these as *strings*, so
  `_to_floats` is load-bearing), `cosym_hist_series` (Plotly histograms carry
  x-values only — matplotlib bins them via `ax.hist`), `cosym_dendrogram`
  (each trace is one bracket → line segments). The Rij histogram reuses
  `corrmat_xy` (same single-bar-trace shape). `_plot_cosym` draws up to five
  panels from `graphs_cosym_coordinates` (Axis 0/1 scatter),
  `graphs_cosym_rij_histogram` (bar), `graphs_uc_scatter` (a/b/c-pair
  scatter, overlaid), `graphs_uc_hist` (unit-cell histograms, overlaid), and
  `graphs_uc_clustering` (dendrogram lines). `graphs_pca_analysis`-style SPLOM
  blobs aren't present here. Also: `_plotly_text` (new, shared by `corrmat_xy`)
  now strips HTML tags from titles/axis-labels — DIALS writes `r<sub>ij</sub>`
  and `Distance (Å<sup>2</sup>)` which used to render with the raw tags.
  **Tested against the user's real `/Users/graeme/data/cpp/demo/dials.cosym.html`**:
  all 5 graphs extract, all parsers verified, `_plot_cosym` rendered (Agg
  backend) → 5 axes and eyeballed. Same live-wx-loop gap as the other plots.
- **"Open HTML in web browser" buttons** (added 2026-07-15) on the Setup &
  Run button row for the four steps that write their own HTML report:
  symmetry (`dials.symmetry.html`), cosym (`dials.cosym.html`),
  correlation_matrix (`dials.correlation_matrix.html`, or the `.scaled.`
  variant when 'use scaled data' is ticked) and scale. Scale opens **every**
  per-cluster `dials.scale.cluster_N.html` if any were written, else the
  plain `dials.scale.html`. `_html_files_for_step(step)` resolves the
  existing basename(s); `_open_html_for_step(step)` `webbrowser.open`s each
  (or shows a "run the step first" message if none exist yet). This is
  separate from the per-step `dials.report` button — it opens the program's
  OWN html, not a freshly generated dials.report.html. `_corrmat_html_name()`
  was factored out of `_corrmat_html_text` so both share the use_scaled logic.
- **The cluster list stdout parser** `parse_cluster_list` reads the
  `Cluster N / Completeness / Multiplicity / Datasets:...` blocks. It's
  available for future use (e.g. auto-populating the cluster selector from
  the correlation_matrix log) but the selector currently globs files
  instead, which is more robust to what actually got written.

**Testing done for the multi-crystal work (sandbox, matplotlib present via
Agg):** all new parsers unit-tested against the tutorial's sample tables
and the user's real `dials.correlation_matrix.html`; every new/changed
`_plot_*` method exercised and figures eyeballed (the correlation-matrix
panel correctly shows the three cows/pigs/people clusters); `_build_command`
tested for the joint toggle, cosym, correlation_matrix, and cluster scaling
(inputs overridden + distinct output.* names, verified no overwrite);
module still imports with matplotlib absent. **Not tested against a live
DIALS multi-crystal run or a live wx loop** — same gap as the single-crystal
plots. In particular confirm against real output: that multi `dials.refine`
prints "RMSDs by experiment" in this exact pipe-table form; and that
`dials.correlation_matrix.html`'s `var graphs_*` blob names/shapes match
(they did for the supplied file). Two things previously flagged here have
since been corrected against real DIALS output: the find_spots banner
wording ("Finding strong spots **in** imageset N", not "on"), and the
integrate summary parser (now collects every "Summary vs image number"
block and groups by the ID column, rather than reading only the last block
— which had shown just one data set).

## How report-file selection works (`_report_files_for_step`)

For a given step, in order:
1. If the step's declared `outputs` include both a `.expt` and a
   `.refl` file (index, refine, integrate, symmetry, scale), use those
   two directly.
2. If it only produces one of the two (e.g. `find_spots` → only
   `strong.refl`; `search_beam` → only `optimised.expt`), pair it with
   the *other* file type currently sitting in that step's own input
   fields (so `find_spots` reports with `[<its Experiment-file input>,
   "strong.refl"]`).
3. If the step declares no `.expt`/`.refl` outputs at all (Bravais
   lattice determination, Merge/Export — the latter only writes MTZ),
   fall back to whatever is currently in that step's own input fields
   (e.g. Bravais uses `indexed.expt`/`indexed.refl`; Merge/Export uses
   `scaled.expt`/`scaled.refl`).
4. Import is a special first case (no refl exists yet) and resolves to
   `["imported.expt"]` via the same logic (the "only one output" branch
   already produces this, since there's no `.refl` to pair with).

Verified by hand for all 10 `STEPS` entries during development; not
verified against real DIALS behaviour.

## Architecture (for whoever edits this next)

The code lives in the `dialsgui/` package (see "Package layout" above for
which module holds what). Rough map of the pieces:

- `ExtraField` / `InputSpec` / `StepDef` (dataclasses) — declarative
  description of each pipeline stage: program name, input file fields,
  extra CLI parameters, expected outputs, log filename, optional flag.
- `STEPS: List[StepDef]` — the 10-stage pipeline definition, in order.
  Confirmed working — don't touch without a specific new bug.
- `TOOLS` — the four sidebar viewer launch buttons (program name + arg
  labels), including a standalone `dials.report` entry independent of
  the new per-step button (lets the user report on arbitrary files, not
  just the current step's). Confirmed working.
- `summarise_log(text)` — regex-based digest (keyword lines + table
  blocks) shown in the "Summary" tab. Pure function, no Tk dependency.
  Confirmed working.
- The live-plot parsers — `parse_find_spots`, `parse_refine_steps`,
  `parse_integrate_blocks`, `parse_integrate_progress`,
  `parse_integrate_summary`, `parse_scale_merging` — pure, Tk-free,
  streaming-tolerant functions near the top of the file. Unit-tested
  against the user's real sample outputs (see testing section).
- `ProcessRunner` — runs a command in a background thread, pushes
  stdout lines onto a `queue.Queue` polled by the wx main loop via a
  repeating `wx.Timer` (`_on_timer`), so the GUI doesn't block while e.g.
  `dials.integrate` runs. Confirmed working.
- `DialsFrame(wx.Frame)` — the app. Key methods:
  - `select_step()` — rebuilds the right-hand Notebook (Setup & Run /
    Live Output / Summary / Full Log, **plus a Plots tab on steps whose
    `plot_kind` is set**). Resets the per-step plot state each time and,
    for plot-capable steps, populates the plots from the existing log.
  - `_build_setup_tab()` / `_build_import_inputs()` — renders the
    editable input/parameter fields, plus the Run/Stop/"Run and show
    report" buttons and a status label (`report_status_label`) for the
    report button. `_add_glob_pattern` adds the pattern **verbatim** to
    `self.image_files` (NOT expanded) — `dials.import` does its own
    expansion, and expanding here would put thousands of paths on the
    command line for big sweeps; the listbox shows a non-authoritative
    `glob.glob` count as a hint only, and `image_files` stays the single
    source of truth (each entry is either a real path from Browse or a
    verbatim pattern, both passed straight through by `_build_command`).
  - `_build_command()` — assembles the actual argv list for the main
    pipeline command from field values. Confirmed working.
  - `run_step()` / `_on_timer()` / `_finish_step()` — the main
    pipeline run/stream/status-update lifecycle. `run_step()` resets the
    `live_output` buffer and starts the `wx.Timer`; `_on_timer()`
    accumulates streamed stdout into it and drives throttled live plot
    redraws (and stops the timer + calls `_finish_step` on `("done", rc)`);
    `_finish_step()` does the definitive redraw from the on-disk log.
  - `_refresh_log_tab()` / `_current_log_text()` — read `dials.<program>.log`
    back after a run; `_current_log_text()` is the shared reader (also
    used to seed plots when re-selecting a step).
  - `_build_plots_tab()` — **new**, builds the embedded `FigureCanvasWxAgg`
    + toolbar (+ the integration `wx.Gauge` progress bar) for a plot-capable
    step.
  - `_refresh_plots_from_text()` — **new**, dispatches on `plot_kind` to
    the right `_plot_*` method; guards against drawing onto a step the
    user has navigated away from.
  - `_plot_find_spots()` / `_plot_refine()` / `_plot_integrate()` /
    `_plot_scale()` — **new**, the actual figure-drawing per step.
  - `_report_files_for_step()` — picks the `.expt`/`.refl`
    pair for the report button (see above).
  - `run_and_show_report()` — runs `dials.report` in a
    background thread (`subprocess.run`, not the streaming
    `ProcessRunner`, since this is a fire-and-forget-then-open-browser
    action rather than something the user watches live) and calls
    `_report_finished()` back on the wx main thread via `wx.CallAfter(...)`.
  - `_report_finished()` — re-enables the report button and
    either opens `dials.report.html` with `webbrowser.open()` or shows
    an error dialog with the captured stdout/stderr tail.
  - `launch_tool()` — the sidebar viewer-launch dialog (unrelated to
    the new per-step button; still handles the standalone
    `dials.report` entry in `TOOLS` with its own browser-opening logic,
    which is now somewhat redundant with the per-step button but was
    left in place since it lets the user report on arbitrary file
    combinations, not just the current step's).

## Later additions: index progress, refine per-run, corr-matrix after scaling

Three more changes after the multi-crystal work:

- **Index progress bar.** Index gained `plot_kind="index"`. `parse_index_progress`
  reads `Indexing imageset id <id> (k/N)` (multi-crystal joint=false only)
  and `_plot_index` drives a progress bar from `(k/N)` — the user confirmed
  that count is reliable. The old `integrate_progress`/`integrate_progress_var`
  attributes were renamed to generic `progress_bar`/`progress_var` and the
  bar is now built for `plot_kind in ("integrate","index")`. There's no
  per-image line graph for indexing, so the figure just shows a short note;
  the bar is the content. (The parser function `parse_integrate_progress`
  keeps its name — only the GUI attributes were renamed.)
- **Refine shows per-run convergence, not just final RMSDs (bug fix), and
  groups by run correctly (second bug fix).** `_plot_refine` no longer
  special-cases the "RMSDs by experiment" table; it uses
  `parse_all_refine_steps`, which now groups by the
  `Selected group of experiments to refine with original ids: N` marker
  that precedes each refinement run, and within each run keeps only the
  LAST "Refinement steps" table (the final macrocycle). An earlier version
  kept EVERY table, so scan-varying refinement (which prints several
  macrocycle tables per run) massively inflated the run count — that was
  the "shows far more runs than there really are" bug. Each returned table
  carries an `ids` field (the original experiment id string) used for the
  page label "run k (id N)". No markers (single-crystal, or old format) ->
  keep the single last table as one run. `parse_refine_by_experiment` is
  unused by the plot now but kept as a standalone parser.
- **Page selector no longer blanks / jumps to first when reviewing (bug
  fix).** The Plots page-change callback (`_on_page_change`) used to pass
  `self.live_output`, which is stale/empty when reviewing a completed step
  (it holds the last *run's* stream, or another step's), so the re-parse
  produced a different/empty set of pages and the chosen page vanished ->
  `_update_plot_pages` reset the selection and the plot went blank. Now the
  callback uses the live stream ONLY while THIS step is actively running
  (`self.running_step_id == step.id`), otherwise the canonical on-disk
  source via `_plot_source_text`. Applies to refine and integrate pages
  alike.
- **Correlation matrix can run after scaling.** New GUI-only `use_scaled`
  check field on the correlation_matrix step (a pseudo-flag, never emitted
  as a real arg — stripped in `_build_command`). Its trace callback (like
  the scale "Cluster to scale" selector) fills the Experiment/Reflection
  input fields with `scaled.expt/.refl` when ticked and reverts them to the
  `symmetrized.*` defaults when unticked, so the input fields are the single
  source of truth (`_build_command` just reads them — it no longer overrides
  inputs itself). When the toggle is on, `_build_command` also redirects
  outputs to `dials.correlation_matrix.scaled.html` / `.scaled.log` so the
  post-scaling run doesn't clobber the post-cosym one; `_corrmat_html_text`,
  `_corrmat_log_name`, `_current_log_text` and `_refresh_log_tab` read the
  `.scaled.` files when the toggle is on, and the same trace refreshes the
  Log and Plots tabs immediately. `_FalseVar` is a tiny always-False
  stand-in used as the safe default when looking up the field.
- **Load state from the working directory.** `_load_state_from_workdir()`
  infers step completion from files on disk (`_step_outputs_present(step)`:
  a step's declared `.expt`/`.refl` outputs exist, or for
  scale/merge_export/correlation_matrix a suitable log/HTML/MTZ or
  per-cluster result exists) and sets the status icons accordingly, as if
  run through the GUI. It also seeds `image_files=["imported.expt"]` when
  present. It's called silently at startup on the cwd, silently after a
  Browse… change of working directory, and with a confirmation dialog from
  the "Load state from working dir" button. Non-destructive (only marks
  done where evidence exists; never clobbers a 'running' step).

`self.report_button` and `self.report_status_var` are rebound every
time `select_step()` rebuilds the Setup & Run tab (same pattern the
code already used for `self.run_button`/`self.stop_button`). If the
user clicks "Run and show report" on one step and then navigates to a
*different* step before it finishes, the completion callback
(`_report_finished`) will re-enable/update whichever step's button and
label are currently bound to those attribute names, not necessarily the
one that was actually running. The `dials.report` subprocess still
completes and the browser still opens correctly either way — this only
affects which on-screen button/label reflects the "done" state. Not
worth fixing preemptively; only address if it actually confuses anyone
in practice.

## How to test changes without a full DIALS/wx environment

The sandbox this was ported in has neither wxPython (it won't build there —
no GTK dev libs) nor DIALS, but it *does* have `matplotlib`. Techniques
used for the port, worth reusing:

1. `python3 -m py_compile dialsgui/*.py dials_gui.py` — catches syntax
   errors only.
2. To unit-test pure-Python logic (`summarise_log`, and all the `parse_*` /
   `corrmat_*` / `extract_*` functions), **just `import dialsgui.parsers`** —
   since the split, that module (and `dialsgui.model` / `dialsgui.steps`)
   imports with no wx or matplotlib dependency, so no `sys.modules` stubbing
   is needed anymore. Call the parser functions directly with the user's
   sample strings. (Pre-split, this needed a stubbed `wx` in `sys.modules`;
   that dance is now obsolete for parser tests — keep it only for the
   frame-level tests below if wx is genuinely unavailable.)
3. To test that the GUI degrades gracefully when matplotlib is missing, set
   `sys.modules["matplotlib"] = None` before importing, then assert
   `HAVE_MPL is False`.
4. To smoke-test the **wx-specific** code (constructor, `_build_layout`,
   `select_step`, `_build_command`) without a display, build a *functional*
   `wx` stub whose widget classes are tiny objects recording
   `GetValue`/`SetValue`/`SetLabel`/`Bind`/etc. (a `wx.BoxSizer` with a
   no-op `Add`/`Clear`, a `wx.Notebook` with `AddPage`, a `wx.Timer`, dialog
   classes returning `ID_CANCEL`). Then instantiate `DialsFrame()` and loop
   over the steps calling `select_step(step)` and `_build_command(step)`,
   asserting the argv is what you expect. This catches attribute typos and
   wrong method signatures in the ported GUI layer.
5. To test the `_plot_*` drawing methods with matplotlib present but no
   display, force `matplotlib.use("Agg")` and stub only
   `matplotlib.backends.backend_wxagg` (back `FigureCanvasWxAgg` with the
   real `FigureCanvasAgg`, and a no-op `NavigationToolbar2WxAgg`) so
   `HAVE_MPL` comes out True; then instantiate `DialsFrame` (with the wx
   stub from technique 4), `select_step` each plot-capable step, call
   `_refresh_plots_from_text(step, sample_text)`, and assert on
   `len(frame.plot_figure.axes)` and `frame.plot_status_label.GetLabel()`.
   NOTE: the plot state attributes were renamed in the port —
   `plot_status_var`→`plot_status_label`, and the old
   `integrate_progress`/`integrate_progress_var` are now
   `progress_bar`(`wx.Gauge`)/`progress_label`, updated via the
   `_set_progress(fraction, label)` helper — so a test that pokes those
   directly must use the new names.

All five techniques were used for the port and pass (find_spots→1 axis,
index→1, refine→2, integrate→4, scale→5, correlation_matrix→pending, with
the same status strings the Tkinter version produced). What none of them
prove is that the GUI actually *runs* with a live DIALS install and a real
wx display — in particular the `FigureCanvasWxAgg` embedding and the
`wx.Timer`-driven live redraw during a real `dials.integrate`/`dials.scale`
run. That remains the next real test.

## Known unresolved risk areas (lower priority)

- Default output filenames per step (`imported.expt`, `strong.refl`,
  `indexed.expt`/`.refl`, `refined.*`, `integrated.*`,
  `symmetrized.*`, `scaled.*`) match the tutorial's prose and are
  implicitly confirmed correct by the user's successful pipeline run.
- `dials.merge` vs `dials.export` log filenames
  (`dials.merge.log`/`dials.export.log`) are assumed, not specifically
  confirmed by the user (they may not have exercised both branches).
- No test of the "Additional parameters (free text)" field's shell-
  splitting (`str.split()`) against parameters containing spaces or
  quotes (e.g. a `unit_cell` value with spaces around commas).
- `matplotlib` is a **soft** dependency again (only for the Plots tab):
  imported under `try/except` into `HAVE_MPL`, backend forced to
  `WXAgg` (with `FigureCanvasWxAgg` / `NavigationToolbar2WxAgg` from
  `matplotlib.backends.backend_wxagg`). If you merge this with another
  branch/copy of the file, make sure there's exactly one such import block
  and no leftover hard `import matplotlib` at top level that would break
  startup where it's absent.
- Live plot parsing assumes DIALS' *stdout* contains the same tables, in
  the same text layout, as the `.log` file and as the samples the user
  pasted. Confirmed against the pasted samples only. If a future DIALS
  release changes a table's columns/spacing, the relevant `parse_*`
  function is where to look first — they're deliberately isolated and
  each has a matching sample in the test scripts.
