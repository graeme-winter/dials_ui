# DIALS Workflow GUI

A single-file, dependency-free (standard library only) graphical front end
for stepping through DIALS macromolecular crystallography data processing,
built around the workflow described in the CCP4/DLS & CCP4/APS 2024
tutorials:
https://github.com/graeme-winter/dials_tutorials/tree/main/ccp4-dls-2024

It does not reimplement any DIALS algorithm — it only builds the correct
command line for each stage, runs the real `dials.*` program as a
subprocess, streams the live output to screen, and then reads back the
`dials.<program>.log` file DIALS itself writes to build a short digest
(RMSDs, % indexed, resolution, space group, merging statistics, etc).

## Requirements

* Python 3.8+ with `tkinter` (standard on most systems; on minimal Linux
  installs you may need `sudo apt install python3-tk` or equivalent).
* A working DIALS installation, sourced/activated so that `dials.import`,
  `dials.find_spots`, etc. are on `$PATH`. The GUI shows a warning banner
  at startup if it can't find `dials.import`.
* *(Optional)* `matplotlib`, for the live **Plots** tab (see below). If it
  isn't installed, everything else still works and the Plots tab simply
  shows a note explaining how to enable it (`pip install matplotlib`, or
  `libtbx.pip install matplotlib` inside a DIALS environment).

## Running

```
python3 dials_gui.py
```

## How it maps onto the tutorial

Left-hand sidebar, top to bottom, mirrors the WORKFLOW.md steps:

1. **Import** (`dials.import`) — browse for image/master files, or add a
   glob pattern (e.g. `../data/ins10_?.nxs` for the multi-sweep insulin
   example), optionally set `image_range`.
2. **Find Spots** (`dials.find_spots`).
3. **Search Beam Position** (optional, `dials.search_beam_position`) —
   writes `optimised.expt`; you can then point the Index step's
   "Experiment file" field at `optimised.expt` instead of `imported.expt`.
4. **Index** (`dials.index`) — set `space_group` / `unit_cell` /
   `max_lattices` if needed.
5. **Bravais Lattice Determination** (optional,
   `dials.refine_bravais_settings`) — prints the table of candidate
   lattices/space groups with RMS deviations; per the tutorial, the
   simplest way to use a solution is to go back to Index and set
   `space_group` accordingly, rather than using `bravais_setting_N.expt`
   directly (which needs the reflections re-indexed to match).
6. **Refine** (`dials.refine`).
7. **Integrate** (`dials.integrate`) — optional `prediction.d_min`.
8. **Symmetry Analysis** (`dials.symmetry`).
9. **Scale** (`dials.scale`) — tick `anomalous` for anomalous data, set
   `absorption_level` (low/medium/high) if the sample has significant
   absorption.
10. **Merge / Export** — choose `merge` (`dials.merge`, scaled+merged MTZ)
    or `export` (`dials.export`, scaled but unmerged MTZ), with an
    optional `d_min` cutoff taken from the scaling recommendation.

Every input/output filename box is a plain, editable text field pre-filled
with the standard DIALS naming convention from the tutorial, so you can
freely substitute any intermediate file (e.g. re-run Index against
`optimised.expt`, or Refine against a different `indexed.expt` after
re-indexing with a forced space group).

Each step has four tabs (five on the steps that support live plots — see
below):

* **Setup & Run** — inputs, parameters, a live command-line preview, and
  Run/Stop buttons, plus a **"Run and show report in web browser"**
  button (see below).
* **Live Output** — streamed stdout/stderr as the command runs.
* **Summary** — an automatically extracted digest of the log file (tables,
  RMSDs, % indexed, space group, merging statistics, etc).
* **Full Log** — the raw `dials.<program>.log` DIALS itself wrote, with a
  refresh button.
* **Plots** — *(Find Spots, Refine, Integrate and Scale only)* live-updating
  matplotlib graphs of the key per-step diagnostics (see below).

### Live Plots tab

Four of the steps carry an extra **Plots** tab that fills in live, from the
program's streamed stdout, while the step runs (and is re-read from the
on-disk log afterwards, and whenever you re-select an already-run step).
It complements the `dials.report` HTML rather than replacing it: these are
the handful of "is it going well?" traces you watch *while* a long step
runs, on the same axes DIALS prints them in.

* **Find Spots** — a line graph of the number of strong pixels found per
  image (from the `Found N strong pixels on image M` output), updating
  image-by-image as the scan is processed.
* **Refine** — line graphs of RMSD_X, RMSD_Y (mm, left axis) and RMSD_Phi
  (deg, right axis) versus refinement step, from the "Refinement steps"
  table, so you can see the refinement converge.
* **Integrate** — a live **progress bar** tracking block processing (parsed
  from the block table and the per-block `Frames: A -> B` output; the bar
  correctly reflects that integration passes over the blocks twice, once
  for profile modelling and once for integration). When integration
  finishes, the tab shows four line graphs versus image number, taken from
  the "Summary vs image number" table: I/sigma (sum and prf), full/partial
  reflection counts, CC prf, and RMSD XY.
* **Scale** — line graphs of the per-resolution-bin merging statistics
  (<I/sigma>, CC1/2 and CC_anom, Rmerge/Rmeas/Rpim, and
  completeness/multiplicity). Because resolution bins are non-linear, the
  X axis is drawn linearly in **1/d²** (using the geometric mean of each
  bin's d_min and d_max) but tick-labelled with the actual resolution in
  Å. The trailing overall-summary row is excluded from the per-bin traces
  (so it doesn't distort them) and instead reported in the status line.

If `matplotlib` isn't installed, the Plots tab is still present but shows a
short note on how to enable it; nothing else is affected.

### Run and show report in web browser

Every step's Setup & Run tab has a **"Run and show report in web
browser"** button. This runs `dials.report` on the `.expt`/`.refl` files
that best represent that stage's result (its own freshly written
outputs where it produces both, otherwise the appropriate pairing from
the step's current input fields), then opens the resulting
`dials.report.html` in your default browser — the same interactive
Plotly report DIALS generates for its own `dials.report` command, just
one click away at every stage rather than something you'd otherwise
have to run by hand. This replaces an earlier attempt at an in-app
"Plots" tab that tried to reconstruct charts from the text log files
directly; using DIALS' own report generation is far more reliable and
avoids showing irrelevant, over-generated charts.


The sidebar also has direct launch buttons, always available, for the
interactive viewers used throughout the tutorial:

* `dials.show` — text summary of any `.expt`/`.refl` file.
* `dials.image_viewer` — view raw images, with found spots or integrated
  reflections overlaid once available.
* `dials.reciprocal_lattice_viewer` — check indexing quality / beam centre.
* `dials.report` — generates an HTML report, which the GUI will try to
  open in your browser a few seconds after launching it.

Each of these opens a small dialog pre-filled with sensible default
filenames (based on the currently selected step) that you can edit before
launching; they run as independent, non-blocking processes so you can
keep working in the main window while they're open.

## Notes / limitations

* Only one pipeline step runs at a time; the Run buttons for other steps
  are effectively blocked while something is executing (use Stop to kill
  a running step).
* The "Summary" tab uses simple pattern matching (table borders and a
  handful of DIALS-specific keywords) to condense the log — it is meant
  as a quick-glance digest, not a replacement for the Full Log tab or for
  `dials.report`.
* The core pipeline (all 10 steps, command building, log streaming, the
  Summary/Full Log tabs, and the sidebar viewer buttons) has been
  confirmed working end-to-end against a real DIALS installation. An
  earlier in-app "Plots" tab that auto-generated bar charts from every
  table it could find in the log text was removed for plotting too many
  irrelevant things; the current **Plots** tab is the deliberate,
  targeted replacement — it appears only on the four steps where a small
  fixed set of traces is genuinely useful (Find Spots, Refine, Integrate,
  Scale), parses only the specific outputs described above, and updates
  live. The "Run and show report in web browser" button (the full,
  Plotly-based `dials.report`) remains available on every step for the
  complete picture.
* The live Plots tab needs `matplotlib`; without it the tab shows a note
  and the rest of the GUI is unaffected.
