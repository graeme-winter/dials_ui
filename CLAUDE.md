# CLAUDE.md — DIALS Workflow GUI

Context file for picking this project back up. Read this before touching
`dials_gui.py` again.

## What this project is

A single-file Tkinter GUI (`dials_gui.py`) that wraps the DIALS
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
- `dials_gui.py` — the whole application.
- `README.md` — user-facing usage doc, written alongside the code.

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
  (throttled to roughly every 5th poll in `_poll_runner`), then does a
  definitive redraw from the on-disk `dials.<program>.log` in
  `_finish_step`, and also populates immediately when you re-select an
  already-run step (from its log).
- `matplotlib` is once again a dependency, but a **soft** one: it's
  imported in a `try/except` at module top into `HAVE_MPL`, the backend is
  forced to `TkAgg`, and if the import fails the tab still appears but just
  shows a "install matplotlib to enable this" note. Nothing else in the
  GUI depends on it. (So the old "matplotlib is no longer a dependency"
  note below is obsolete — see the updated risk list.)

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
- `integrate`: a live `ttk.Progressbar` for block processing +, at the
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
the tab running *inside a live Tk event loop against a live DIALS run* —
i.e. the `FigureCanvasTkAgg` embedding, the `after()`-driven live redraw
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
- **Cluster-aware Scale.** If `cluster_N.expt` files exist in the working
  dir (written by correlation_matrix with output clusters), the Scale
  Setup tab shows a **cluster selector** (`self.scale_cluster_var`).
  Picking `cluster_N` makes `_build_command` (i) use `cluster_N.expt/.refl`
  as the inputs regardless of the input fields, and (ii) append
  `output.experiments=scaled_cluster_N.expt`,
  `output.reflections=scaled_cluster_N.refl`,
  `output.html=dials.scale.cluster_N.html`,
  `output.log=dials.scale.cluster_N.log` so repeated per-cluster runs
  never overwrite each other (the tutorial's "mkdir per cluster" kept in
  one directory). `_scale_log_name()`, `_current_log_text()` and
  `_refresh_log_tab()` all honour the cluster-tagged log name so the
  Summary/Full Log/Plots tabs read the right file. Helpers:
  `_available_clusters()` (globs `cluster_(\d+)\.expt`), `_selected_cluster()`.
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
  `Finding strong spots on imageset N` banner blocks — image numbers
  restart per imageset, so each imageset is its own series/line captioned
  by its number, and because every refresh re-parses the whole accumulated
  stdout, earlier imagesets persist rather than being overwritten). The old
  approach — one continuous global series with guessed equal-length
  sweep-boundary lines, via `parse_find_spots`/`parse_find_spots_histograms`
  — was wrong (image numbers restart per imageset, so the dict-keyed
  `parse_find_spots` collapsed every imageset onto image 1..100 and only the
  last survived); those two functions are retained as standalone parsers
  but are no longer used by the plot.
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
DIALS multi-crystal run or a live Tk loop** — same gap as the single-crystal
plots. In particular confirm against real output: that multi `dials.refine`
prints "RMSDs by experiment" in this exact pipe-table form; that
`dials.integrate`'s "Summary vs image number" ID column enumerates data
sets the way `integrate_summary_by_dataset` assumes; that find_spots image
numbering is global across sweeps (the boundary-line assumption); and that
`dials.correlation_matrix.html`'s `var graphs_*` blob names/shapes match
(they did for the supplied file).

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

Everything lives in `dials_gui.py`. Rough map:

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
  stdout lines onto a `queue.Queue` polled by the Tk main loop via
  `after()`, so the GUI doesn't block while e.g. `dials.integrate` runs.
  Confirmed working.
- `DialsGUI(tk.Tk)` — the app. Key methods:
  - `select_step()` — rebuilds the right-hand Notebook (Setup & Run /
    Live Output / Summary / Full Log, **plus a Plots tab on steps whose
    `plot_kind` is set**). Resets the per-step plot state each time and,
    for plot-capable steps, populates the plots from the existing log.
  - `_build_setup_tab()` / `_build_import_inputs()` — renders the
    editable input/parameter fields, plus the Run/Stop/"Run and show
    report" buttons and a status label (`report_status_var`) for the
    report button. `_add_glob_pattern` adds the pattern **verbatim** to
    `self.image_files` (NOT expanded) — `dials.import` does its own
    expansion, and expanding here would put thousands of paths on the
    command line for big sweeps; the listbox shows a non-authoritative
    `glob.glob` count as a hint only, and `image_files` stays the single
    source of truth (each entry is either a real path from Browse or a
    verbatim pattern, both passed straight through by `_build_command`).
  - `_build_command()` — assembles the actual argv list for the main
    pipeline command from field values. Confirmed working.
  - `run_step()` / `_poll_runner()` / `_finish_step()` — the main
    pipeline run/stream/status-update lifecycle. `run_step()` resets the
    `live_output` buffer; `_poll_runner()` accumulates streamed stdout
    into it and drives throttled live plot redraws; `_finish_step()` does
    the definitive redraw from the on-disk log.
  - `_refresh_log_tab()` / `_current_log_text()` — read `dials.<program>.log`
    back after a run; `_current_log_text()` is the shared reader (also
    used to seed plots when re-selecting a step).
  - `_build_plots_tab()` — **new**, builds the embedded `FigureCanvasTkAgg`
    + toolbar (+ the integration progress bar) for a plot-capable step.
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
    `_report_finished()` back on the Tk main thread via `self.after(0, ...)`.
  - `_report_finished()` — re-enables the report button and
    either opens `dials.report.html` with `webbrowser.open()` or shows
    an error dialog with the captured stdout/stderr tail.
  - `launch_tool()` — the sidebar viewer-launch dialog (unrelated to
    the new per-step button; still handles the standalone
    `dials.report` entry in `TOOLS` with its own browser-opening logic,
    which is now somewhat redundant with the per-step button but was
    left in place since it lets the user report on arbitrary file
    combinations, not just the current step's).

## Known minor rough edge (cosmetic, not yet fixed)

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

## How to test changes without a full DIALS/Tk environment

The sandbox this was built in has neither `tkinter` nor DIALS (but it
*does* have `matplotlib`). Techniques used, worth reusing:

1. `python3 -m py_compile dials_gui.py` — catches syntax errors only.
2. To unit-test pure-Python logic (`summarise_log`, and all six
   `parse_*` live-plot functions) without a real Tk install, stub
   `tkinter`/`tkinter.ttk`/`tkinter.filedialog`/`tkinter.messagebox`/
   `tkinter.simpledialog` in `sys.modules` with dummy objects, then
   **import the module normally** (`importlib.import_module`) rather than
   `exec()`-ing its source — the dataclasses in the file need a real
   module in `sys.modules` to resolve their annotations, and an `exec`
   into a hand-built module object breaks that. Then call the parser
   functions directly with the user's sample strings.
3. To test that the GUI degrades gracefully when matplotlib is missing,
   set `sys.modules["matplotlib"] = None` before importing, then assert
   `HAVE_MPL is False`.
4. To test the `_plot_*` drawing methods without Tk, force
   `matplotlib.use("Agg")`, build a tiny fake object carrying just the
   attributes those methods touch (`plot_figure`, `plot_status_var`,
   `integrate_progress`, `integrate_progress_var`, and a bound
   `_set_plot_status`), bind the unbound methods off `DialsGUI` to it,
   and call them with sample text. Assert on the number of axes created
   and on the status string, and `savefig` one figure to eyeball it.

All four techniques were used this round and the test scripts pass. What
none of them prove is that the GUI actually *runs* with a live DIALS
install and a real Tk display — in particular the `FigureCanvasTkAgg`
embedding and the `after()`-driven live redraw during a real
`dials.integrate`/`dials.scale` run. That remains the next real test.

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
  `TkAgg`. If you merge this with another branch/copy of the file, make
  sure there's exactly one such import block and no leftover hard
  `import matplotlib` at top level that would break startup where it's
  absent.
- Live plot parsing assumes DIALS' *stdout* contains the same tables, in
  the same text layout, as the `.log` file and as the samples the user
  pasted. Confirmed against the pasted samples only. If a future DIALS
  release changes a table's columns/spacing, the relevant `parse_*`
  function is where to look first — they're deliberately isolated and
  each has a matching sample in the test scripts.
