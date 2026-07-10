#!/usr/bin/env python3
"""
dials_gui.py — A step-by-step graphical front end for the DIALS
macromolecular crystallography data processing suite.

This GUI wraps the command-line DIALS programs used in the
"Processing in Detail" workflow tutorials (CCP4/DLS & CCP4/APS 2024
workshops, see https://github.com/graeme-winter/dials_tutorials):

    dials.import
    dials.find_spots
    dials.search_beam_position   (optional)
    dials.index
    dials.refine_bravais_settings (optional)
    dials.refine
    dials.integrate
    dials.symmetry
    dials.scale
    dials.merge / dials.export

as well as the interactive viewing tools:

    dials.show
    dials.image_viewer
    dials.reciprocal_lattice_viewer
    dials.report

The GUI does not re-implement any DIALS functionality itself: it only
constructs the correct command line for each stage, runs it as a
subprocess in a chosen working directory, streams the live output to
the screen, and then reads back the DIALS-generated `dials.<program>.log`
file to build a short, readable digest of the result (RMSDs, % indexed,
resolution, space group, merging statistics, etc). Every processing
stage and viewer described in the tutorial WORKFLOW.md is reachable
from this interface, and the intermediate .expt / .refl files can be
freely substituted at every stage, so any of the tutorials on that page
(the basic single-sweep workflow, the "if you're impatient" TLDR path,
optimising the beam centre, forcing a Bravais lattice/space group, and
scaling anomalous vs native data) can be worked through interactively.

Requirements
------------
Python 3.8+ with tkinter (part of the standard library on most
platforms; on some minimal Linux installs `python3-tk` must be
installed separately). DIALS itself must already be installed and set
up in the environment the GUI is launched from (i.e. `dials.import`
etc. must be on $PATH) - this GUI is a front end, not a replacement,
for that installation.

Run with:

    python3 dials_gui.py

"""

from __future__ import annotations

import glob
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Dict, List, Optional, Tuple

# matplotlib is an optional dependency: the live-plotting "Plots" tab is
# only offered if it (and its Tk backend) import successfully. The rest of
# the GUI — the whole pipeline, log summaries, dials.report button — works
# without it, so a missing matplotlib degrades gracefully to "no Plots
# tab" rather than failing to start.
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg,
        NavigationToolbar2Tk,
    )
    from matplotlib.figure import Figure
    HAVE_MPL = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_MPL = False


# --------------------------------------------------------------------------
# Step / field definitions
# --------------------------------------------------------------------------


@dataclass
class ExtraField:
    """A single extra command-line parameter exposed in the GUI."""

    key: str
    label: str
    kind: str = "entry"          # "entry", "check", "combo"
    default: str = ""
    choices: Optional[List[str]] = None
    help: str = ""
    # For "check" fields: the value emitted when ticked. Defaults to
    # "True" (so the arg is `key=True`); set to e.g. "false" for a toggle
    # like joint=false that should emit `joint=false` when ticked.
    check_value: str = "True"

    def build_arg(self, value) -> Optional[str]:
        if self.kind == "check":
            if value:
                return f"{self.key}={self.check_value}"
            return None
        value = (value or "").strip()
        if not value:
            return None
        return f"{self.key}={value}"


@dataclass
class InputSpec:
    label: str
    default: str


@dataclass
class StepDef:
    id: str
    title: str
    program: str
    help: str
    inputs: List[InputSpec]
    extra_fields: List[ExtraField] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    log_file: str = ""
    optional: bool = False
    is_import: bool = False
    # Some steps (merge/export) choose their program dynamically.
    dynamic: bool = False
    # Which live-plot view (if any) this step supports. One of
    # "find_spots", "refine", "integrate", "scale" — or "" for steps with
    # no Plots tab. Drives whether select_step() adds a "Plots" tab and
    # which _refresh_plots_* method it uses.
    plot_kind: str = ""


STEPS: List[StepDef] = [
    StepDef(
        id="import",
        title="1. Import",
        program="dials.import",
        help=(
            "Read image headers and write imported.expt describing the "
            "experiment geometry (detector, beam, goniometer, scan). "
            "Nothing else happens at this stage - if the beam centre, "
            "distance or wavelength look wrong here, fix it before going "
            "further."
        ),
        inputs=[],
        is_import=True,
        extra_fields=[
            ExtraField("image_range", "Image range (start,end)", "entry",
                       help="e.g. 1,1200 - leave blank to use all images"),
        ],
        outputs=["imported.expt"],
        log_file="dials.import.log",
    ),
    StepDef(
        id="find_spots",
        title="2. Find Spots",
        program="dials.find_spots",
        help=(
            "Scan every image for strong reflections and write strong.refl. "
            "This is one of the two most time-consuming steps as every "
            "image is read and processed."
        ),
        inputs=[InputSpec("Experiment file", "imported.expt")],
        extra_fields=[
            ExtraField("nproc", "nproc (blank = all cores)", "entry"),
        ],
        outputs=["strong.refl"],
        log_file="dials.find_spots.log",
        plot_kind="find_spots",
    ),
    StepDef(
        id="search_beam",
        title="3. Search Beam Position (optional)",
        program="dials.search_beam_position",
        help=(
            "Refine the beam centre against the found spots, writing "
            "optimised.expt. Useful if the reciprocal lattice viewer shows "
            "spots that don't line up. The shift reported should usually "
            "be small for a well-calibrated beamline."
        ),
        inputs=[
            InputSpec("Experiment file", "imported.expt"),
            InputSpec("Reflection file", "strong.refl"),
        ],
        outputs=["optimised.expt"],
        log_file="dials.search_beam_position.log",
        optional=True,
    ),
    StepDef(
        id="index",
        title="4. Index",
        program="dials.index",
        help=(
            "Identify the crystal lattice from the strong spot positions "
            "and assign Miller indices, writing indexed.expt / "
            "indexed.refl. Use the experiment file from Import, or from "
            "Search Beam Position if you ran it. Set space_group / "
            "unit_cell here if known, or after inspecting Bravais lattice "
            "options below. For MULTIPLE crystals (many imported sweeps "
            "that do not share an orientation matrix), tick 'multi-crystal "
            "(joint=false)' so each sweep is indexed independently in one "
            "run - see the Cows/Pigs/People workflow."
        ),
        inputs=[
            InputSpec("Experiment file", "imported.expt"),
            InputSpec("Reflection file", "strong.refl"),
        ],
        extra_fields=[
            ExtraField("joint", "multi-crystal (joint=false)", "check",
                       check_value="false",
                       help="index many crystals independently in one run"),
            ExtraField("space_group", "space_group", "entry"),
            ExtraField("unit_cell", "unit_cell", "entry",
                       help="e.g. 78,78,78,90,90,90"),
            ExtraField("max_lattices", "max_lattices", "entry",
                       help="set to 2 (say) if % indexed is low and a "
                            "second lattice is suspected"),
        ],
        outputs=["indexed.expt", "indexed.refl"],
        log_file="dials.index.log",
    ),
    StepDef(
        id="bravais",
        title="5. Bravais Lattice Determination (optional)",
        program="dials.refine_bravais_settings",
        help=(
            "List every Bravais lattice approximately consistent with the "
            "triclinic cell from indexing, with the RMS deviation each "
            "would introduce. If a solution's rmsd is not noticeably worse "
            "than the triclinic one, its lattice / space group is a good "
            "candidate. The simplest way to use a solution is to re-run "
            "Index above with space_group set accordingly (rather than "
            "using bravais_setting_N.expt directly, which needs the "
            "reflections re-indexed to match)."
        ),
        inputs=[
            InputSpec("Experiment file", "indexed.expt"),
            InputSpec("Reflection file", "indexed.refl"),
        ],
        outputs=[],
        log_file="dials.refine_bravais_settings.log",
        optional=True,
    ),
    StepDef(
        id="refine",
        title="6. Refine",
        program="dials.refine",
        help=(
            "Re-refine the crystal/detector/beam models, including "
            "scan-varying refinement of the crystal, writing refined.expt "
            "/ refined.refl. RMSDs should improve slightly relative to "
            "the end of indexing."
        ),
        inputs=[
            InputSpec("Experiment file", "indexed.expt"),
            InputSpec("Reflection file", "indexed.refl"),
        ],
        outputs=["refined.expt", "refined.refl"],
        log_file="dials.refine.log",
        plot_kind="refine",
    ),
    StepDef(
        id="integrate",
        title="7. Integrate",
        program="dials.integrate",
        help=(
            "Build a reflection profile model and integrate the "
            "background-subtracted intensity of every predicted "
            "reflection, writing integrated.expt / integrated.refl. This "
            "is the most computationally expensive step."
        ),
        inputs=[
            InputSpec("Experiment file", "refined.expt"),
            InputSpec("Reflection file", "refined.refl"),
        ],
        extra_fields=[
            ExtraField("prediction.d_min", "prediction.d_min", "entry",
                       help="optional resolution limit, e.g. 1.8"),
            ExtraField("nproc", "nproc (blank = all cores)", "entry"),
        ],
        outputs=["integrated.expt", "integrated.refl"],
        log_file="dials.integrate.log",
        plot_kind="integrate",
    ),
    StepDef(
        id="symmetry",
        title="8. Symmetry (single crystal)",
        program="dials.symmetry",
        help=(
            "Assess spot positions and intensities to identify symmetry "
            "operations present in the data, compose these into a "
            "candidate Laue group / space group, and write "
            "symmetrized.expt / symmetrized.refl. Not needed if the "
            "correct space group was already set at Index. For MULTIPLE "
            "crystals use Cosym (below) instead - it determines symmetry "
            "and resolves indexing ambiguity across all data sets at once."
        ),
        inputs=[
            InputSpec("Experiment file", "integrated.expt"),
            InputSpec("Reflection file", "integrated.refl"),
        ],
        outputs=["symmetrized.expt", "symmetrized.refl"],
        log_file="dials.symmetry.log",
    ),
    StepDef(
        id="cosym",
        title="8b. Cosym (multi-crystal)",
        program="dials.cosym",
        help=(
            "For MULTIPLE crystals: determine the Patterson symmetry AND "
            "resolve indexing ambiguity across all data sets simultaneously "
            "(replaces dials.symmetry). Aligns the lattices in reciprocal "
            "space, estimates the crystal symmetry, and writes "
            "symmetrized.expt / symmetrized.refl plus dials.cosym.html. Run "
            "this instead of Symmetry when you indexed with joint=false."
        ),
        inputs=[
            InputSpec("Experiment file", "integrated.expt"),
            InputSpec("Reflection file", "integrated.refl"),
        ],
        outputs=["symmetrized.expt", "symmetrized.refl"],
        log_file="dials.cosym.log",
        optional=True,
    ),
    StepDef(
        id="correlation_matrix",
        title="8c. Correlation Matrix (multi-crystal)",
        program="dials.correlation_matrix",
        help=(
            "For MULTIPLE crystals, after Cosym: measure the pairwise "
            "similarity of the data sets and cluster the isomorphous ones "
            "using the OPTICS algorithm, writing dials.correlation_matrix "
            ".html (with the correlation / cos-angle matrices, dendrograms "
            "and cluster assignments). Tick 'output clusters' to also write "
            "cluster_0.expt/.refl, cluster_1.expt/.refl, ... which can then "
            "be scaled independently (see Scale). The Plots tab visualises "
            "the matrices, reachability and cluster coordinates from the "
            "HTML output."
        ),
        inputs=[
            InputSpec("Experiment file", "symmetrized.expt"),
            InputSpec("Reflection file", "symmetrized.refl"),
        ],
        extra_fields=[
            ExtraField("significant_clusters.output",
                       "output clusters (write cluster_N.expt/.refl)",
                       "check", default="True",
                       help="on by default; needed to scale clusters separately"),
        ],
        outputs=[],
        log_file="dials.correlation_matrix.log",
        optional=True,
        plot_kind="correlation_matrix",
    ),
    StepDef(
        id="scale",
        title="9. Scale",
        program="dials.scale",
        help=(
            "Correct for radiation damage, beam intensity changes and "
            "sample absorption, writing scaled.expt / scaled.refl and "
            "dials.scale.html. Merging statistics and the error model are "
            "printed at the end - this is where you find out about the "
            "final quality of the data. Tick 'anomalous' for anomalous "
            "data (e.g. SAD/MAD). For MULTIPLE clusters from the "
            "Correlation Matrix step, use the cluster selector below to "
            "scale each cluster_N independently - each run writes its own "
            "scaled_cluster_N.* / dials.scale.cluster_N.* so nothing is "
            "overwritten."
        ),
        inputs=[
            InputSpec("Experiment file", "symmetrized.expt"),
            InputSpec("Reflection file", "symmetrized.refl"),
        ],
        extra_fields=[
            ExtraField("anomalous", "anomalous", "check"),
            ExtraField("absorption_level", "absorption_level", "combo",
                       choices=["", "low", "medium", "high"],
                       help="low (~1%, default), medium (~5%), high (~25%)"),
            ExtraField("d_min", "d_min", "entry",
                       help="optional resolution cutoff from CC-half fit"),
        ],
        outputs=["scaled.expt", "scaled.refl", "dials.scale.html"],
        log_file="dials.scale.log",
        plot_kind="scale",
    ),
    StepDef(
        id="merge_export",
        title="10. Merge / Export",
        program="dials.merge",
        help=(
            "Produce a final MTZ file for downstream use. 'merge' writes "
            "a scaled and merged MTZ (dials.merge) - use this for most "
            "structure solution / molecular replacement pipelines. "
            "'export' writes the scaled but unmerged MTZ (dials.export)."
        ),
        inputs=[
            InputSpec("Experiment file", "scaled.expt"),
            InputSpec("Reflection file", "scaled.refl"),
        ],
        extra_fields=[
            ExtraField("mode", "mode", "combo", default="merge",
                       choices=["merge", "export"]),
            ExtraField("d_min", "d_min", "entry",
                       help="optional resolution cutoff suggested by scaling"),
        ],
        outputs=[],
        log_file="",
        dynamic=True,
    ),
]


TOOLS = [
    ("dials.show", "dials.show", ["Experiment / reflection file(s)"]),
    ("dials.image_viewer", "dials.image_viewer",
     ["Experiment file", "Reflection file (optional)"]),
    ("dials.reciprocal_lattice_viewer", "dials.reciprocal_lattice_viewer",
     ["Experiment file", "Reflection file"]),
    ("dials.report", "dials.report", ["Experiment file", "Reflection file"]),
]


# --------------------------------------------------------------------------
# Log summarisation helpers
# --------------------------------------------------------------------------

_TABLE_LINE = re.compile(r"^\s*[+|].*[+|]\s*$")
_KEYWORD_LINES = re.compile(
    r"(Best solution|Space group|Unit cell|Laue group|% indexed|"
    r"num images|num stills|sequences|Writing experiments|Writing "
    r"reflections|reflections indexed|resolution|RMSD|Suggested|"
    r"High resolution limit|Low resolution limit|Completeness|"
    r"Multiplicity|I/sigma|CC half|Rmerge|Rmeas|Rpim|Error model|"
    r"estimated I/sigma|shift|Reindex operator)",
    re.IGNORECASE,
)


def summarise_log(text: str, max_blocks: int = 8) -> str:
    """Produce a short, readable digest of a DIALS log file."""

    if not text.strip():
        return "(no log output captured yet)"

    lines = text.splitlines()
    out: List[str] = []
    seen_keyword_lines = set()
    blocks_found = 0
    i = 0
    n = len(lines)
    while i < n and blocks_found < max_blocks:
        line = lines[i]
        if _TABLE_LINE.match(line):
            # capture the whole table block
            block = []
            # walk backwards to include a header line above a leading '+--' row
            j = i
            while j > 0 and lines[j - 1].strip() and not _TABLE_LINE.match(
                lines[j - 1]
            ) and len(block) < 1:
                j -= 1
            start = max(i - 2, 0)
            k = i
            while k < n and (
                _TABLE_LINE.match(lines[k]) or "|" in lines[k] or not lines[k].strip()
            ):
                block.append(lines[k])
                k += 1
                if k - i > 40:
                    break
            out.extend(lines[start:i])
            out.extend(block)
            out.append("")
            blocks_found += 1
            i = k
            continue
        if _KEYWORD_LINES.search(line) and line.strip() not in seen_keyword_lines:
            out.append(line.rstrip())
            seen_keyword_lines.add(line.strip())
        i += 1

    if not out:
        # fall back to the tail of the log
        out = ["(no recognised summary patterns found - showing tail of log)", ""]
        out.extend(lines[-40:])

    return "\n".join(out)


# --------------------------------------------------------------------------
# Live-plot data extraction
# --------------------------------------------------------------------------
#
# The functions below turn a step's cumulative stdout / log text into
# structured numeric series for the "Plots" tab. They are all pure
# functions (no Tk / matplotlib dependency) and, crucially, tolerant of
# *partial* input: they can be called repeatedly while a step is still
# running so the plots update live, parsing whatever complete data exists
# so far and ignoring the rest.


def _split_table_row(line: str) -> Optional[List[str]]:
    """Split a '| a | b | c |' pipe-table row into its cell strings, or
    return None if this line isn't a data-bearing pipe row (e.g. it's a
    +----+ / |----| border, or not a table row at all)."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    if set(s) <= set("|+-= "):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


_FIND_SPOTS_RE = re.compile(
    r"Found\s+(\d+)\s+strong\s+pixels\s+on\s+image\s+(\d+)", re.IGNORECASE
)


def parse_find_spots(text: str) -> Dict[str, List[float]]:
    """find_spots: 'Found N strong pixels on image M' -> {image, pixels}
    sorted by image number."""
    by_image: Dict[int, int] = {}
    for m in _FIND_SPOTS_RE.finditer(text):
        by_image[int(m.group(2))] = int(m.group(1))
    images = sorted(by_image)
    return {
        "image": [float(i) for i in images],
        "pixels": [float(by_image[i]) for i in images],
    }


# find_spots processes one imageset at a time; each is introduced by a
# banner block like:
#   --------------------------------
#   Finding strong spots on imageset 33
#   --------------------------------
# and then emits its own "Found N strong pixels on image M" lines (with M
# restarting per imageset). We split on these banners so each imageset is a
# separate series on the plot, captioned by its imageset number.
_FIND_SPOTS_IMAGESET_RE = re.compile(
    r"Finding strong spots on imageset\s+(\d+)", re.IGNORECASE
)


def parse_find_spots_by_imageset(text: str) -> List[Dict[str, object]]:
    """Split find_spots output into per-imageset series.

    Returns a list of {'imageset': int, 'image': [...], 'pixels': [...]}
    in the order the imagesets appear. Any 'Found ...' lines that occur
    before the first banner (or if there are no banners at all) are
    collected under imageset None as a single fallback series, so a
    single-sweep run - which has no banner - still plots.

    Tolerant of streaming: the currently-processing (last) imageset simply
    has fewer points until it finishes."""
    lines = text.splitlines()
    series: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None

    def _new(imageset: Optional[int]) -> Dict[str, object]:
        d: Dict[str, object] = {"imageset": imageset, "image": [], "pixels": []}
        series.append(d)
        return d

    for line in lines:
        banner = _FIND_SPOTS_IMAGESET_RE.search(line)
        if banner:
            current = _new(int(banner.group(1)))
            continue
        m = _FIND_SPOTS_RE.search(line)
        if m:
            if current is None:
                current = _new(None)
            current["image"].append(float(m.group(2)))    # type: ignore[union-attr]
            current["pixels"].append(float(m.group(1)))   # type: ignore[union-attr]

    # Drop any empty banner-only series (e.g. a banner seen but no spot
    # lines yet is fine to keep; but a trailing empty one adds nothing).
    return [s for s in series if s["image"]] or series


_REFINE_HEADER_RE = re.compile(r"Refinement steps", re.IGNORECASE)


def parse_refine_steps(text: str) -> Dict[str, List[float]]:
    """refine: the 'Refinement steps' table -> {step, rmsd_x, rmsd_y,
    rmsd_phi}. Reads rows after the *last* 'Refinement steps' header seen
    (there can be more than one macrocycle)."""
    lines = text.splitlines()
    steps: List[float] = []
    rmsd_x: List[float] = []
    rmsd_y: List[float] = []
    rmsd_phi: List[float] = []

    header_idx = None
    for i, line in enumerate(lines):
        if _REFINE_HEADER_RE.search(line):
            header_idx = i
    if header_idx is None:
        return {"step": [], "rmsd_x": [], "rmsd_y": [], "rmsd_phi": []}

    for line in lines[header_idx + 1:]:
        cells = _split_table_row(line)
        if cells is None:
            if steps and not line.strip():
                break
            continue
        if len(cells) < 5:
            continue
        try:
            step = float(int(cells[0]))
            x = float(cells[2])
            y = float(cells[3])
            phi = float(cells[4])
        except ValueError:
            continue
        steps.append(step)
        rmsd_x.append(x)
        rmsd_y.append(y)
        rmsd_phi.append(phi)

    return {"step": steps, "rmsd_x": rmsd_x, "rmsd_y": rmsd_y, "rmsd_phi": rmsd_phi}


_FRAMES_RE = re.compile(r"Frames:\s*(\d+)\s*->\s*(\d+)")


def parse_integrate_blocks(text: str) -> List[Tuple[int, int]]:
    """integrate: the block table near the start -> list of
    (frame_from, frame_to). [] until the table appears. Identified by a
    header row containing 'Frame From' / 'Frame To'."""
    lines = text.splitlines()
    blocks: List[Tuple[int, int]] = []
    in_table = False
    header_cols: Optional[List[str]] = None

    for line in lines:
        cells = _split_table_row(line)
        if cells is None:
            if in_table and blocks and not line.strip():
                break
            continue
        lowered = [c.lower() for c in cells]
        if "frame from" in lowered and "frame to" in lowered:
            header_cols = lowered
            in_table = True
            blocks = []
            continue
        if not in_table or header_cols is None:
            continue
        if len(cells) != len(header_cols):
            continue
        try:
            ff_idx = header_cols.index("frame from")
            ft_idx = header_cols.index("frame to")
            blocks.append((int(cells[ff_idx]), int(cells[ft_idx])))
        except (ValueError, IndexError):
            continue

    return blocks


def parse_integrate_progress(text: str) -> Dict[str, int]:
    """integrate: count 'Frames: A -> B' block completions so far and
    report the most recent range. The block loop runs twice (profile
    modelling, then integration), so 'completed' can exceed the number of
    blocks; the plotting code accounts for this."""
    matches = _FRAMES_RE.findall(text)
    if not matches:
        return {"completed": 0, "last_from": 0, "last_to": 0}
    last_from, last_to = matches[-1]
    return {
        "completed": len(matches),
        "last_from": int(last_from),
        "last_to": int(last_to),
    }


_SUMMARY_VS_IMAGE_RE = re.compile(r"Summary vs image number", re.IGNORECASE)
_INTEGRATE_SUMMARY_KEYS = [
    "image", "n_full", "n_part", "n_over", "n_ice", "n_sum",
    "n_prf", "ibg", "isigi_sum", "isigi_prf", "cc_prf", "rmsd_xy",
]


def parse_integrate_summary(text: str) -> Dict[str, List[float]]:
    """integrate (end): the 'Summary vs image number' table -> per-column
    series keyed by image number (one row per image)."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _SUMMARY_VS_IMAGE_RE.search(line):
            header_idx = i
    result: Dict[str, List[float]] = {k: [] for k in _INTEGRATE_SUMMARY_KEYS}
    if header_idx is None:
        return result

    started = False
    for line in lines[header_idx + 1:]:
        cells = _split_table_row(line)
        if cells is None:
            if started and not line.strip():
                break
            continue
        if len(cells) < 13:
            continue
        try:
            vals = [
                float(int(cells[1])),   # image
                float(int(cells[2])),   # n_full
                float(int(cells[3])),   # n_part
                float(int(cells[4])),   # n_over
                float(int(cells[5])),   # n_ice
                float(int(cells[6])),   # n_sum
                float(int(cells[7])),   # n_prf
                float(cells[8]),        # ibg
                float(cells[9]),        # isigi_sum
                float(cells[10]),       # isigi_prf
                float(cells[11]),       # cc_prf
                float(cells[12]),       # rmsd_xy
            ]
        except (ValueError, IndexError):
            continue
        started = True
        for k, v in zip(_INTEGRATE_SUMMARY_KEYS, vals):
            result[k].append(v)

    return result


_MERGING_HEADER_RE = re.compile(
    r"Merging statistics by resolution bin", re.IGNORECASE
)
_MERGING_KEYS = [
    "d_max", "d_min", "inv_d2", "mult", "completeness", "i_mean",
    "i_over_sigma", "r_merge", "r_meas", "r_pim", "r_anom",
    "cc_half", "cc_anom",
]


def _merging_float(token: str) -> Optional[float]:
    """Parse a merging-stats numeric token, tolerating a trailing
    significance marker '*' (e.g. '1.000*' or '0.114*')."""
    token = token.strip().rstrip("*")
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_scale_merging(text: str) -> Dict[str, object]:
    """scale: the 'Merging statistics by resolution bin' block -> per-bin
    series. Because resolution bins are non-linear, an 'inv_d2' series is
    provided (1 / d^2, d = geometric mean of the bin's d_min and d_max)
    for use as the X axis. The trailing overall-summary row (spanning the
    whole resolution range) is separated out under key 'overall' so it
    doesn't distort the per-bin plots.

    This is a whitespace-columnar table, NOT pipe-delimited."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _MERGING_HEADER_RE.search(line):
            header_idx = i
    result: Dict[str, object] = {k: [] for k in _MERGING_KEYS}
    if header_idx is None:
        return result

    rows: List[List[float]] = []
    started = False
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            if started:
                break
            continue
        if "d_max" in stripped or "d_min" in stripped:
            continue
        tokens = stripped.split()
        if len(tokens) != 14:
            if started:
                break
            continue
        vals = [_merging_float(t) for t in tokens]
        if any(v is None for v in vals):
            if started:
                break
            continue
        started = True
        rows.append(vals)  # type: ignore[arg-type]

    if not rows:
        return result

    overall_row = None
    per_bin_rows = rows
    if len(rows) >= 2:
        last = rows[-1]
        others = rows[:-1]
        min_dmin = min(r[1] for r in others)
        widest_bin_span = max(r[0] - r[1] for r in others)
        span_last = last[0] - last[1]
        if last[1] <= min_dmin + 1e-6 and span_last > widest_bin_span + 1e-6:
            overall_row = last
            per_bin_rows = others

    for r in per_bin_rows:
        (d_max, d_min, _nobs, _nuniq, mult, comp, i_mean, i_sig,
         r_mrg, r_meas, r_pim, r_anom, cc12, ccano) = r
        d_geo = math.sqrt(d_min * d_max)
        result["d_max"].append(d_max)          # type: ignore[union-attr]
        result["d_min"].append(d_min)          # type: ignore[union-attr]
        result["inv_d2"].append(1.0 / (d_geo * d_geo))  # type: ignore[union-attr]
        result["mult"].append(mult)            # type: ignore[union-attr]
        result["completeness"].append(comp)    # type: ignore[union-attr]
        result["i_mean"].append(i_mean)        # type: ignore[union-attr]
        result["i_over_sigma"].append(i_sig)   # type: ignore[union-attr]
        result["r_merge"].append(r_mrg)        # type: ignore[union-attr]
        result["r_meas"].append(r_meas)        # type: ignore[union-attr]
        result["r_pim"].append(r_pim)          # type: ignore[union-attr]
        result["r_anom"].append(r_anom)        # type: ignore[union-attr]
        result["cc_half"].append(cc12)         # type: ignore[union-attr]
        result["cc_anom"].append(ccano)        # type: ignore[union-attr]

    if overall_row is not None:
        result["overall"] = {
            "d_max": overall_row[0],
            "d_min": overall_row[1],
            "completeness": overall_row[5],
            "i_over_sigma": overall_row[7],
            "cc_half": overall_row[12],
        }

    return result


# --------------------------------------------------------------------------
# Multi-crystal (COWS_PIGS_PEOPLE) live-plot data extraction
# --------------------------------------------------------------------------
#
# When many data sets are imported and indexed with joint=False, find_spots,
# refine and integrate each produce output covering N data sets. These
# helpers split that output per data set so the Plots tab can show one page
# per data set. They reuse the single-data-set parsers above where the
# per-image / per-step data isn't itself tagged by data set.


_IMAGESET_RE = re.compile(r"imageset\s+(\d+)", re.IGNORECASE)
_SWEEP_COUNT_RE = re.compile(r"sweep:\s*(\d+)", re.IGNORECASE)


def parse_find_spots_histograms(text: str) -> Dict[int, int]:
    """find_spots (multi): the per-imageset histogram headers
    'NNNN spots found on 100 images' tagged 'for imageset K' -> {imageset:
    total spots}. Used to detect how many data sets there are and to show
    a per-data-set spot-total summary. Returns {} if no such headers."""
    result: Dict[int, int] = {}
    lines = text.splitlines()
    current = None
    for line in lines:
        m = _IMAGESET_RE.search(line)
        if m and "histogram" in line.lower():
            current = int(m.group(1))
            continue
        if current is not None:
            m2 = re.match(r"\s*(\d+)\s+spots found", line)
            if m2:
                result[current] = int(m2.group(1))
                current = None
    return result


_RMSD_BY_EXP_RE = re.compile(r"RMSDs?\s+by\s+experiment", re.IGNORECASE)


def parse_refine_by_experiment(text: str) -> Dict[str, List[float]]:
    """refine (multi): the 'RMSDs by experiment' table ->
    {exp, nref, rmsd_x, rmsd_y, rmsd_z}. One row per experiment/data set.
    Columns are (px)/(px)/(images) for multi-crystal refine. Reads rows
    after the last such header."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _RMSD_BY_EXP_RE.search(line):
            header_idx = i
    keys = ["exp", "nref", "rmsd_x", "rmsd_y", "rmsd_z"]
    result: Dict[str, List[float]] = {k: [] for k in keys}
    if header_idx is None:
        return result

    started = False
    for line in lines[header_idx + 1:]:
        cells = _split_table_row(line)
        if cells is None:
            if started and not line.strip():
                break
            continue
        if len(cells) < 5:
            continue
        try:
            vals = [
                float(int(cells[0])),  # exp id
                float(int(cells[1])),  # nref
                float(cells[2]),       # rmsd_x
                float(cells[3]),       # rmsd_y
                float(cells[4]),       # rmsd_z
            ]
        except ValueError:
            continue
        started = True
        for k, v in zip(keys, vals):
            result[k].append(v)

    return result


def integrate_summary_by_dataset(text: str) -> Dict[int, Dict[str, List[float]]]:
    """integrate (multi): split the 'Summary vs image number' data by its
    first column (the imageset / data set ID, 0..N) into
    {dataset_id: {image, n_full, ...}}.

    DIALS may print the summary either as one big table or as one table per
    imageset (each with its own 'Summary vs image number' header). Either
    way we want *every* such block, keyed by the ID column - so unlike a
    single-table parser we do NOT anchor on the last header only, and we do
    NOT stop at the first blank line (which would end after the first
    block). Instead we scan the whole text and treat any 13+ column pipe
    row whose first two cells are integers as a data row, ignoring header /
    border / unit rows. This is robust to multiple blocks and to streaming
    (partial last block).

    Rows are grouped by ID; within each ID they're kept in the order seen
    (image number order as DIALS emits them)."""
    out: Dict[int, Dict[str, List[float]]] = {}
    if _SUMMARY_VS_IMAGE_RE.search(text) is None:
        return out

    for line in text.splitlines():
        cells = _split_table_row(line)
        if cells is None or len(cells) < 13:
            continue
        # A data row: first cell is the dataset ID (int), second is the
        # image number (int). Header rows ("ID","Image",...) and the
        # "(sum)"/"(prf)" unit-continuation row fail these int parses and
        # are skipped.
        try:
            ds = int(cells[0])
            row = [
                float(int(cells[1])),   # image
                float(int(cells[2])),   # n_full
                float(int(cells[3])),   # n_part
                float(int(cells[4])),   # n_over
                float(int(cells[5])),   # n_ice
                float(int(cells[6])),   # n_sum
                float(int(cells[7])),   # n_prf
                float(cells[8]),        # ibg
                float(cells[9]),        # isigi_sum
                float(cells[10]),       # isigi_prf
                float(cells[11]),       # cc_prf
                float(cells[12]),       # rmsd_xy
            ]
        except (ValueError, IndexError):
            continue
        d = out.setdefault(ds, {k: [] for k in _INTEGRATE_SUMMARY_KEYS})
        for k, v in zip(_INTEGRATE_SUMMARY_KEYS, row):
            d[k].append(v)

    return out


_CLUSTER_HEAD_RE = re.compile(r"^\s*Cluster\s+(\d+)\s*$")
_CLUSTER_DATASETS_RE = re.compile(r"Datasets:\s*([0-9,\s]+)")
_CLUSTER_COMPLETENESS_RE = re.compile(r"Completeness:\s*([0-9.]+)")
_CLUSTER_MULTIPLICITY_RE = re.compile(r"Multiplicity:\s*([0-9.]+)")


def parse_cluster_list(text: str) -> List[Dict[str, object]]:
    """dials.correlation_matrix (stdout): parse the
    'Cluster N / Number of datasets / Completeness / Multiplicity /
    Datasets:...' blocks into a list of
    {'id': N, 'datasets': [...], 'completeness': f, 'multiplicity': f}.
    Tolerant of streaming: returns whatever complete-enough blocks exist."""
    clusters: List[Dict[str, object]] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = _CLUSTER_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        cid = int(m.group(1))
        block = {"id": cid, "datasets": [], "completeness": None,
                 "multiplicity": None}
        j = i + 1
        while j < n and not _CLUSTER_HEAD_RE.match(lines[j]):
            comp = _CLUSTER_COMPLETENESS_RE.search(lines[j])
            if comp:
                block["completeness"] = float(comp.group(1))
            mult = _CLUSTER_MULTIPLICITY_RE.search(lines[j])
            if mult:
                block["multiplicity"] = float(mult.group(1))
            ds = _CLUSTER_DATASETS_RE.search(lines[j])
            if ds:
                nums = [int(x) for x in re.findall(r"\d+", ds.group(1))]
                block["datasets"] = nums
            j += 1
        clusters.append(block)
        i = j
    return clusters


# --------------------------------------------------------------------------
# dials.correlation_matrix.html embedded-Plotly-JSON extraction
# --------------------------------------------------------------------------
#
# The HTML written by dials.correlation_matrix embeds several
# `var graphs_<name> = { ...JSON... };` assignments that feed Plotly. We
# pull those JSON objects out by name (balanced-brace scan, so nested
# objects are handled) and parse them with the stdlib json module - no JS
# engine needed. The graphs we know how to render with matplotlib are
# listed in CORRMAT_KNOWN_GRAPHS.


def _extract_json_object(text: str, start_brace: int) -> Optional[str]:
    """Given the index of an opening '{' in text, return the substring up
    to and including its matching '}', respecting braces inside strings."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start_brace, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start_brace:i + 1]
    return None


def extract_corrmat_graphs(html: str) -> Dict[str, dict]:
    """Return {graph_name: parsed_json_dict} for every
    `var graphs_X = {...}` assignment in a dials.correlation_matrix.html.
    Skips any blob that fails to parse."""
    out: Dict[str, dict] = {}
    for m in re.finditer(r"var\s+(graphs_[A-Za-z0-9_]+)\s*=\s*", html):
        name = m.group(1)
        brace = html.find("{", m.end())
        if brace == -1:
            continue
        blob = _extract_json_object(html, brace)
        if blob is None:
            continue
        try:
            out[name] = json.loads(blob)
        except json.JSONDecodeError:
            continue
    return out


def corrmat_matrix(blob: dict) -> Optional[Dict[str, object]]:
    """cc_cluster / cos_angle_cluster blob -> {'z','order','title'}."""
    data = blob.get("data", [])
    heatmap = next((t for t in data if t.get("type") == "heatmap"), None)
    if heatmap is None or "z" not in heatmap:
        return None
    layout = blob.get("layout", {})
    order = (layout.get("xaxis", {}) or {}).get("ticktext")
    title = (heatmap.get("colorbar", {}) or {}).get("title", "correlation")
    return {"z": heatmap["z"], "order": order, "title": title}


def corrmat_cluster_series(blob: dict) -> List[Dict[str, object]]:
    """reachability / cosym-coordinates blob -> list of per-cluster series
    [{'name','x','y','color'}]. Infinity y-values become None."""
    out = []
    for t in blob.get("data", []):
        ys = []
        for v in t.get("y", []):
            if isinstance(v, float) and (v == float("inf") or v != v):
                ys.append(None)
            else:
                ys.append(v)
        out.append({
            "name": t.get("name", ""),
            "x": t.get("x", []),
            "y": ys,
            "color": (t.get("marker", {}) or {}).get("color"),
        })
    return out


def corrmat_xy(blob: dict) -> Optional[Dict[str, object]]:
    """Single-trace line/bar blob (dimensions, rij histogram) ->
    {'x','y','type','title','xtitle','ytitle'}."""
    data = blob.get("data", [])
    if not data:
        return None
    tr = data[0]
    layout = blob.get("layout", {})
    return {
        "x": tr.get("x", []),
        "y": tr.get("y", []),
        "type": tr.get("type", "line"),
        "title": layout.get("title", ""),
        "xtitle": (layout.get("xaxis", {}) or {}).get("title", ""),
        "ytitle": (layout.get("yaxis", {}) or {}).get("title", ""),
    }


class ProcessRunner:
    """Runs a command in a background thread, streaming stdout lines to a
    queue so the Tk main loop can poll it without blocking."""

    def __init__(self, cmd: List[str], cwd: str):
        self.cmd = cmd
        self.cwd = cwd
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            self.q.put(("line", f"ERROR: could not run {self.cmd[0]}: {exc}\n"))
            self.q.put(("done", -1))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.q.put(("line", f"ERROR launching process: {exc}\n"))
            self.q.put(("done", -1))
            return

        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.q.put(("line", line))
        rc = self.proc.wait()
        self.q.put(("done", rc))

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------


STATUS_ICONS = {
    "pending": "\u2b1c",   # white square
    "running": "\u23f3",   # hourglass
    "done": "\u2705",      # check mark
    "failed": "\u274c",    # cross mark
}


class DialsGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DIALS Workflow GUI")
        self.geometry("1180x760")

        self.workdir = tk.StringVar(value=os.getcwd())
        self.status = {s.id: "pending" for s in STEPS}
        self.selected_step: Optional[StepDef] = None
        self.field_vars: dict = {}      # step id -> {field key: tk.Variable}
        self.input_vars: dict = {}      # step id -> [tk.StringVar per input]
        self.image_files: List[str] = []

        self.runner: Optional[ProcessRunner] = None
        self.running_step_id: Optional[str] = None

        # Live-plot state. `live_output` accumulates the raw stdout of the
        # currently running step so the plot parsers (which want the whole
        # text so far) can be re-run on each poll. The plot widgets are
        # rebuilt per select_step(); `plot_canvas` is None when the
        # current step has no Plots tab or matplotlib is unavailable.
        self.live_output: str = ""
        self.plot_canvas = None          # FigureCanvasTkAgg or None
        self.plot_figure = None          # matplotlib Figure or None
        self.plot_status_var: Optional[tk.StringVar] = None
        self.integrate_progress: Optional[ttk.Progressbar] = None
        self.integrate_progress_var: Optional[tk.StringVar] = None
        self.plot_page_var: Optional[tk.StringVar] = None
        self.plot_page_combo = None
        self.scale_cluster_var: Optional[tk.StringVar] = None
        # Throttle plot redraws while streaming (redrawing on every line is
        # wasteful); only redraw every Nth poll or on completion.
        self._poll_tick = 0

        self._build_layout()
        self._check_dials_available()
        self.select_step(STEPS[0])

    # ---------------------------------------------------------- top bar --
    def _build_layout(self):
        top = ttk.Frame(self, padding=6)
        top.pack(side="top", fill="x")

        ttk.Label(top, text="Working directory:").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.workdir, width=70)
        entry.pack(side="left", padx=4)
        ttk.Button(top, text="Browse...", command=self._choose_workdir).pack(
            side="left"
        )
        self.dials_status_label = ttk.Label(top, text="", foreground="red")
        self.dials_status_label.pack(side="left", padx=12)

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)

        # --- sidebar -------------------------------------------------
        side = ttk.Frame(body, padding=4)
        side.pack(side="left", fill="y")

        ttk.Label(side, text="Pipeline steps", font=("", 11, "bold")).pack(
            anchor="w"
        )
        self.step_buttons: dict = {}
        for s in STEPS:
            btn_frame = ttk.Frame(side)
            btn_frame.pack(fill="x", pady=1)
            icon = ttk.Label(btn_frame, text=STATUS_ICONS["pending"], width=2)
            icon.pack(side="left")
            btn = ttk.Button(
                btn_frame,
                text=s.title,
                command=lambda s=s: self.select_step(s),
                width=32,
            )
            btn.pack(side="left", fill="x", expand=True)
            self.step_buttons[s.id] = (btn, icon)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(side, text="Viewing tools", font=("", 11, "bold")).pack(
            anchor="w"
        )
        for label, program, arg_labels in TOOLS:
            ttk.Button(
                side,
                text=label,
                command=lambda p=program, a=arg_labels: self.launch_tool(p, a),
            ).pack(fill="x", pady=1)

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(
            side, text="Reset all step statuses",
            command=self._reset_statuses,
        ).pack(fill="x")

        # --- main panel -----------------------------------------------
        self.main = ttk.Frame(body, padding=6)
        self.main.pack(side="left", fill="both", expand=True)

    def _check_dials_available(self):
        if shutil.which("dials.import") is None:
            self.dials_status_label.config(
                text="Warning: dials.import not found on $PATH - "
                "make sure your DIALS environment is set up.",
            )
        else:
            self.dials_status_label.config(text="DIALS found on $PATH", foreground="green")

    def _choose_workdir(self):
        d = filedialog.askdirectory(initialdir=self.workdir.get())
        if d:
            self.workdir.set(d)

    def _reset_statuses(self):
        for s in STEPS:
            self.status[s.id] = "pending"
            _, icon = self.step_buttons[s.id]
            icon.config(text=STATUS_ICONS["pending"])

    # ---------------------------------------------------- step display --
    def select_step(self, step: StepDef):
        self.selected_step = step
        for widget in self.main.winfo_children():
            widget.destroy()

        nb = ttk.Notebook(self.main)
        nb.pack(fill="both", expand=True)

        setup_tab = ttk.Frame(nb, padding=8)
        output_tab = ttk.Frame(nb, padding=4)
        summary_tab = ttk.Frame(nb, padding=4)
        log_tab = ttk.Frame(nb, padding=4)
        nb.add(setup_tab, text="Setup & Run")
        nb.add(output_tab, text="Live Output")
        nb.add(summary_tab, text="Summary")
        nb.add(log_tab, text="Full Log")

        self._build_setup_tab(setup_tab, step)
        self.output_text = self._make_readonly_text(output_tab)
        self.summary_text = self._make_readonly_text(summary_tab)
        self.log_text = self._make_readonly_text(log_tab)

        # Plots tab — only for steps that declare a plot_kind, and only if
        # matplotlib is available. Reset per-step plot state first so a
        # step without plots doesn't inherit a stale canvas.
        self.plot_canvas = None
        self.plot_figure = None
        self.plot_status_var = None
        self.integrate_progress = None
        self.integrate_progress_var = None
        self.plot_page_var = None
        self.plot_page_combo = None
        if step.plot_kind and HAVE_MPL:
            plots_tab = ttk.Frame(nb, padding=4)
            nb.add(plots_tab, text="Plots")
            self._build_plots_tab(plots_tab, step)
        elif step.plot_kind and not HAVE_MPL:
            plots_tab = ttk.Frame(nb, padding=8)
            nb.add(plots_tab, text="Plots")
            ttk.Label(
                plots_tab,
                text=(
                    "Live plots for this step need matplotlib, which isn't "
                    "installed in this environment.\n\nInstall it into your "
                    "DIALS/Python environment (e.g. `pip install matplotlib` "
                    "or `libtbx.pip install matplotlib`) and restart the GUI "
                    "to enable the Plots tab. Everything else — running the "
                    "step, the log Summary, and the 'Run and show report in "
                    "web browser' button — works without it."
                ),
                wraplength=760, justify="left",
            ).pack(anchor="w")

        ttk.Button(
            log_tab, text="Refresh from log file",
            command=lambda: self._refresh_log_tab(step),
        ).pack(anchor="w", pady=(4, 0))

        self._refresh_log_tab(step)
        # If a log already exists for this step (e.g. re-selecting a step
        # that ran earlier), populate the plots from it immediately.
        if step.plot_kind and HAVE_MPL:
            self._refresh_plots_from_text(step, self._plot_source_text(step))

    def _make_readonly_text(self, parent) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, wrap="none", height=30)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        txt.config(state="disabled", font=("Courier", 10))
        return txt

    def _set_text(self, widget: tk.Text, content: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.config(state="disabled")

    def _append_text(self, widget: tk.Text, content: str):
        widget.config(state="normal")
        widget.insert("end", content)
        widget.see("end")
        widget.config(state="disabled")

    def _build_setup_tab(self, parent, step: StepDef):
        ttk.Label(
            parent, text=step.help, wraplength=760, justify="left"
        ).pack(anchor="w", pady=(0, 10))

        self.input_vars[step.id] = []
        if step.is_import:
            self._build_import_inputs(parent, step)
        else:
            for spec in step.inputs:
                row = ttk.Frame(parent)
                row.pack(fill="x", pady=2)
                ttk.Label(row, text=spec.label, width=22).pack(side="left")
                var = tk.StringVar(value=spec.default)
                ttk.Entry(row, textvariable=var, width=50).pack(
                    side="left", fill="x", expand=True
                )
                self.input_vars[step.id].append(var)

        self.field_vars[step.id] = {}

        # Scale step: cluster selector for multi-crystal cluster scaling.
        # If cluster_N.expt/.refl files exist (written by the Correlation
        # Matrix step with 'output clusters' ticked), let the user pick one
        # to scale independently; picking a cluster rewrites the input
        # files and adds distinct output.* names so runs don't overwrite.
        self.scale_cluster_var = None
        if step.id == "scale":
            clusters = self._available_clusters()
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=(4, 2))
            ttk.Label(row, text="Cluster to scale", width=22).pack(side="left")
            choices = ["(none - use inputs above)"] + [
                f"cluster_{c}" for c in clusters
            ]
            self.scale_cluster_var = tk.StringVar(value=choices[0])
            combo = ttk.Combobox(
                row, textvariable=self.scale_cluster_var,
                values=choices, width=28, state="readonly",
            )
            combo.pack(side="left")
            if clusters:
                ttk.Label(
                    row,
                    text=f"{len(clusters)} cluster(s) found: "
                         f"{', '.join(str(c) for c in clusters)}",
                    foreground="gray",
                ).pack(side="left", padx=8)
            else:
                ttk.Label(
                    row,
                    text="(no cluster_N files yet - run Correlation Matrix "
                         "with 'output clusters')",
                    foreground="gray",
                ).pack(side="left", padx=8)

        if step.extra_fields:
            ttk.Label(parent, text="Parameters:", font=("", 10, "bold")).pack(
                anchor="w", pady=(10, 2)
            )
        for f in step.extra_fields:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f.label, width=22).pack(side="left")
            if f.kind == "check":
                checked = str(f.default).strip().lower() in ("true", "1", "yes")
                var: tk.Variable = tk.BooleanVar(value=checked)
                ttk.Checkbutton(row, variable=var).pack(side="left")
            elif f.kind == "combo":
                var = tk.StringVar(value=f.default)
                ttk.Combobox(
                    row, textvariable=var, values=f.choices or [], width=20
                ).pack(side="left")
            else:
                var = tk.StringVar(value=f.default)
                ttk.Entry(row, textvariable=var, width=30).pack(side="left")
            if f.help:
                ttk.Label(row, text=f.help, foreground="gray").pack(
                    side="left", padx=8
                )
            self.field_vars[step.id][f.key] = var

        ttk.Label(parent, text="Additional parameters (free text):").pack(
            anchor="w", pady=(10, 2)
        )
        self.extra_params_var = tk.StringVar(value="")
        ttk.Entry(parent, textvariable=self.extra_params_var, width=80).pack(
            anchor="w"
        )

        ttk.Label(parent, text="Command preview:", font=("", 10, "bold")).pack(
            anchor="w", pady=(12, 2)
        )
        self.command_preview = ttk.Label(
            parent, text="", wraplength=900, foreground="blue", justify="left"
        )
        self.command_preview.pack(anchor="w")

        for var in list(self.field_vars[step.id].values()) + self.input_vars[step.id]:
            var.trace_add("write", lambda *_: self._update_command_preview())
        self.extra_params_var.trace_add("write", lambda *_: self._update_command_preview())
        if self.scale_cluster_var is not None:
            self.scale_cluster_var.trace_add(
                "write", lambda *_: self._update_command_preview()
            )

        btn_row = ttk.Frame(parent)
        btn_row.pack(anchor="w", pady=12)
        self.run_button = ttk.Button(
            btn_row, text=f"Run {step.program}" + (" (optional)" if step.optional else ""),
            command=lambda: self.run_step(step),
        )
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(
            btn_row, text="Stop", command=self.stop_running, state="disabled"
        )
        self.stop_button.pack(side="left", padx=6)
        self.report_button = ttk.Button(
            btn_row, text="Run and show report in web browser",
            command=lambda: self.run_and_show_report(step),
        )
        self.report_button.pack(side="left", padx=6)

        self.report_status_var = tk.StringVar(value="")
        ttk.Label(
            parent, textvariable=self.report_status_var, foreground="gray"
        ).pack(anchor="w", pady=(2, 0))

        self._update_command_preview()

    def _build_import_inputs(self, parent, step: StepDef):
        ttk.Label(parent, text="Image files / master file(s):").pack(anchor="w")
        list_frame = ttk.Frame(parent)
        list_frame.pack(fill="x", pady=2)
        self.import_listbox = tk.Listbox(list_frame, height=5, width=90)
        self.import_listbox.pack(side="left", fill="x", expand=True)
        for f in self.image_files:
            self.import_listbox.insert("end", f)
        sb = ttk.Scrollbar(list_frame, command=self.import_listbox.yview)
        sb.pack(side="left", fill="y")
        self.import_listbox.config(yscrollcommand=sb.set)

        btn_row = ttk.Frame(parent)
        btn_row.pack(anchor="w", pady=2)
        ttk.Button(btn_row, text="Browse files...", command=self._browse_images).pack(
            side="left"
        )
        ttk.Button(
            btn_row, text="Add glob pattern...", command=self._add_glob_pattern
        ).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Clear", command=self._clear_images).pack(
            side="left"
        )

    def _browse_images(self):
        files = filedialog.askopenfilenames(
            initialdir=self.workdir.get(),
            title="Select image / master files",
        )
        for f in files:
            self.image_files.append(f)
            self.import_listbox.insert("end", f)
        self._update_command_preview()

    def _add_glob_pattern(self):
        pattern = simpledialog.askstring(
            "Glob pattern",
            "Enter a glob pattern (e.g. ../data/CIX*gz or ../data/ins10_?.nxs).\n"
            "The pattern is passed to dials.import as-is (not expanded here), "
            "so it's fine for it to match thousands of images:",
            parent=self,
        )
        if not pattern:
            return
        pattern = pattern.strip()
        if not pattern:
            return
        # Pass the pattern through verbatim - dials.import does its own shell-
        # style expansion, and for large sweeps expanding here would put
        # thousands of paths on the command line (and in the listbox). Just
        # do a quick, non-authoritative count as a sanity hint to the user.
        try:
            n = len(glob.glob(pattern))
        except Exception:
            n = None
        self.image_files.append(pattern)
        hint = "" if n is None else f"  [matches {n} file(s) now]"
        self.import_listbox.insert("end", pattern + hint)
        self._update_command_preview()

    def _clear_images(self):
        self.image_files = []
        self.import_listbox.delete(0, "end")
        self._update_command_preview()

    # ---------------------------------------------------- command build --
    def _build_command(self, step: StepDef) -> List[str]:
        args: List[str] = []

        cluster = self._selected_cluster()

        if step.is_import:
            args.extend(self.image_files)
        elif step.id == "scale" and cluster is not None:
            # Cluster scaling: use cluster_N.expt/.refl as input regardless
            # of the input fields, so each cluster is scaled independently.
            args.append(f"cluster_{cluster}.expt")
            args.append(f"cluster_{cluster}.refl")
        else:
            for var in self.input_vars[step.id]:
                v = var.get().strip()
                if v:
                    args.append(v)

        for f in step.extra_fields:
            var = self.field_vars[step.id][f.key]
            val = var.get()
            arg = f.build_arg(val)
            if arg:
                args.append(arg)

        extra = getattr(self, "extra_params_var", None)
        if extra is not None:
            extra_text = extra.get().strip()
            if extra_text:
                args.extend(extra_text.split())

        program = step.program
        if step.dynamic and step.id == "merge_export":
            mode = self.field_vars[step.id].get("mode")
            mode_val = mode.get() if mode else "merge"
            program = "dials.export" if mode_val == "export" else "dials.merge"
            # remove the 'mode' pseudo-parameter, it isn't a real dials param
            args = [a for a in args if not a.startswith("mode=")]

        # Cluster scaling: redirect all outputs to cluster-tagged names so
        # repeated Scale runs (one per cluster) don't overwrite each other.
        # This mirrors the tutorial's "mkdir 0 1 2; scale in each" but keeps
        # everything in one working directory.
        if step.id == "scale" and cluster is not None:
            args.extend([
                f"output.experiments=scaled_cluster_{cluster}.expt",
                f"output.reflections=scaled_cluster_{cluster}.refl",
                f"output.html=dials.scale.cluster_{cluster}.html",
                f"output.log=dials.scale.cluster_{cluster}.log",
            ])

        return [program] + args

    def _update_command_preview(self):
        step = self.selected_step
        if step is None or not hasattr(self, "command_preview"):
            return
        try:
            cmd = self._build_command(step)
        except Exception:
            return
        self.command_preview.config(text=" ".join(cmd))

    # -------------------------------------------------------- run step --
    def run_step(self, step: StepDef):
        if self.runner is not None and self.running_step_id is not None:
            messagebox.showwarning(
                "Busy", "Another step is currently running - please wait or stop it."
            )
            return

        workdir = self.workdir.get()
        if not os.path.isdir(workdir):
            messagebox.showerror("Invalid directory", f"{workdir} is not a directory")
            return

        cmd = self._build_command(step)
        if step.is_import and not self.image_files:
            messagebox.showerror(
                "No input files", "Please add at least one image / master file."
            )
            return

        self._set_text(self.output_text, "")
        self._append_text(self.output_text, f"$ {' '.join(cmd)}\n\n")
        self._set_text(self.summary_text, "(running...)")

        # Reset live-plot accumulation for this run and clear any stale
        # figure so plots build up fresh as output streams in.
        self.live_output = ""
        self._poll_tick = 0
        if step.plot_kind and HAVE_MPL:
            self._refresh_plots_from_text(step, "")

        self.status[step.id] = "running"
        self.step_buttons[step.id][1].config(text=STATUS_ICONS["running"])
        self.run_button.config(state="disabled")
        self.stop_button.config(state="normal")

        self.runner = ProcessRunner(cmd, workdir)
        self.running_step_id = step.id
        self.runner.start()
        self.after(100, lambda: self._poll_runner(step))

    def stop_running(self):
        if self.runner:
            self.runner.terminate()

    def _poll_runner(self, step: StepDef):
        if self.runner is None:
            return
        got_line = False
        try:
            while True:
                kind, payload = self.runner.q.get_nowait()
                if kind == "line":
                    self._append_text(self.output_text, payload)
                    self.live_output += payload
                    got_line = True
                elif kind == "done":
                    # Flush a final plot update from everything streamed,
                    # then fall through to _finish_step (which also
                    # re-reads the on-disk log for the definitive version).
                    if step.plot_kind and HAVE_MPL:
                        self._refresh_plots_from_text(step, self.live_output)
                    self._finish_step(step, payload)
                    return
        except queue.Empty:
            pass

        # Live plot update, throttled: redraw roughly every ~0.75s worth of
        # polls when new output has arrived, rather than on every single
        # line (integration alone emits thousands of lines).
        if got_line and step.plot_kind and HAVE_MPL:
            self._poll_tick += 1
            if self._poll_tick % 5 == 0:
                self._refresh_plots_from_text(step, self.live_output)

        self.after(150, lambda: self._poll_runner(step))

    def _finish_step(self, step: StepDef, returncode: int):
        ok = returncode == 0
        self.status[step.id] = "done" if ok else "failed"
        self.step_buttons[step.id][1].config(
            text=STATUS_ICONS["done" if ok else "failed"]
        )
        self.run_button.config(state="normal")
        self.stop_button.config(state="disabled")
        self.runner = None
        self.running_step_id = None
        self._refresh_log_tab(step)
        # Definitive plot update from the on-disk log (the streamed
        # stdout and the log file should agree, but the log is canonical;
        # the "Summary vs image number" and merging-stats tables in
        # particular are written at the very end).
        if step.plot_kind and HAVE_MPL:
            src = self._plot_source_text(step)
            # For correlation_matrix the plots come only from the HTML
            # (stdout has no graph JSON), so use src as-is; for the others
            # prefer the canonical log but fall back to streamed stdout if
            # the log hasn't been flushed yet.
            if step.plot_kind == "correlation_matrix":
                self._refresh_plots_from_text(step, src)
            else:
                self._refresh_plots_from_text(
                    step, src if src.strip() else self.live_output
                )
        if not ok:
            self._append_text(
                self.output_text, f"\n[process exited with code {returncode}]\n"
            )

    # ------------------------------------------------------- clusters --
    def _available_clusters(self) -> List[int]:
        """Scan the working directory for cluster_N.expt files (written by
        dials.correlation_matrix significant_clusters.output=True) and
        return the sorted list of cluster indices N."""
        workdir = self.workdir.get()
        out = []
        try:
            for name in os.listdir(workdir):
                m = re.match(r"cluster_(\d+)\.expt$", name)
                if m:
                    out.append(int(m.group(1)))
        except OSError:
            return []
        return sorted(out)

    def _selected_cluster(self) -> Optional[int]:
        """Return the cluster index currently chosen in the scale step's
        cluster selector, or None if 'none' / not applicable."""
        var = getattr(self, "scale_cluster_var", None)
        if var is None:
            return None
        val = var.get()
        m = re.match(r"cluster_(\d+)$", val or "")
        return int(m.group(1)) if m else None

    def _scale_log_name(self) -> str:
        """The log filename dials.scale will write given the current
        cluster selection (cluster runs redirect output.log)."""
        cluster = self._selected_cluster()
        if cluster is not None:
            return f"dials.scale.cluster_{cluster}.log"
        return "dials.scale.log"

    def _corrmat_html_text(self) -> str:
        """Read dials.correlation_matrix.html from the working directory
        (the source for the correlation_matrix Plots tab). '' if absent."""
        path = os.path.join(self.workdir.get(), "dials.correlation_matrix.html")
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def _plot_source_text(self, step: StepDef) -> str:
        """The text a step's Plots tab should parse: the correlation_matrix
        step plots from its HTML output (which carries the Plotly JSON
        blobs), every other plot step from its .log file."""
        if step.plot_kind == "correlation_matrix":
            return self._corrmat_html_text()
        return self._current_log_text(step)

    def _current_log_text(self, step: StepDef) -> str:
        """Read back the on-disk log for this step (respecting the dynamic
        merge/export log-name choice and cluster scaling). Returns '' if
        not present."""
        log_name = step.log_file
        if step.dynamic and step.id == "merge_export":
            mode = self.field_vars.get(step.id, {}).get("mode")
            mode_val = mode.get() if mode else "merge"
            log_name = "dials.export.log" if mode_val == "export" else "dials.merge.log"
        elif step.id == "scale":
            log_name = self._scale_log_name()
        if not log_name:
            return ""
        path = os.path.join(self.workdir.get(), log_name)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def _refresh_log_tab(self, step: StepDef):
        log_name = step.log_file
        if step.dynamic and step.id == "merge_export":
            mode = self.field_vars.get(step.id, {}).get("mode")
            mode_val = mode.get() if mode else "merge"
            log_name = "dials.export.log" if mode_val == "export" else "dials.merge.log"
        elif step.id == "scale":
            log_name = self._scale_log_name()

        text = ""
        if log_name:
            path = os.path.join(self.workdir.get(), log_name)
            if os.path.exists(path):
                try:
                    with open(path, "r", errors="replace") as fh:
                        text = fh.read()
                except OSError as exc:
                    text = f"(could not read {path}: {exc})"
            else:
                text = f"(log file not found yet: {path})"
        else:
            text = "(no fixed log filename for this step)"

        self._set_text(self.log_text, text)
        self._set_text(self.summary_text, summarise_log(text))

    # ------------------------------------------------------------ plots --
    def _build_plots_tab(self, parent, step: StepDef):
        """Build the Plots tab for a plot-capable step: an embedded
        matplotlib canvas (with the standard navigation toolbar), a status
        line, an optional page selector (per data set for find_spots/
        refine/integrate in multi-crystal mode, per cluster for scale), and
        — for integration — a live block-processing progress bar."""
        top = ttk.Frame(parent)
        top.pack(fill="x")

        self.plot_status_var = tk.StringVar(
            value="(no data yet - run this step, or plots will fill in "
                  "live as it runs)"
        )
        ttk.Label(top, textvariable=self.plot_status_var, foreground="gray").pack(
            side="left", anchor="w"
        )
        refresh_label = (
            "Refresh plots from HTML"
            if step.plot_kind == "correlation_matrix"
            else "Refresh plots from log"
        )
        ttk.Button(
            top, text=refresh_label,
            command=lambda: self._refresh_plots_from_text(
                step, self._plot_source_text(step)
            ),
        ).pack(side="right")

        # Page selector: for steps that can span multiple data sets or
        # clusters, a combobox to flip between one page of plots each.
        # Rebuilt/populated lazily as data arrives (see _update_plot_pages).
        self.plot_page_var = None
        self.plot_page_combo = None
        if step.plot_kind in ("find_spots", "refine", "integrate", "scale"):
            page_frame = ttk.Frame(parent)
            page_frame.pack(fill="x", pady=(4, 2))
            label = "Cluster" if step.plot_kind == "scale" else "Data set"
            ttk.Label(page_frame, text=f"{label}:", width=10).pack(side="left")
            self.plot_page_var = tk.StringVar(value="all")
            self.plot_page_combo = ttk.Combobox(
                page_frame, textvariable=self.plot_page_var,
                values=["all"], width=20, state="readonly",
            )
            self.plot_page_combo.pack(side="left")
            self.plot_page_combo.bind(
                "<<ComboboxSelected>>",
                lambda _e: self._refresh_plots_from_text(
                    step, self.live_output or self._plot_source_text(step)
                ),
            )

        # Integration gets a live progress bar for block processing.
        if step.plot_kind == "integrate":
            prog_frame = ttk.Frame(parent)
            prog_frame.pack(fill="x", pady=(6, 2))
            self.integrate_progress_var = tk.StringVar(value="Blocks: waiting...")
            ttk.Label(
                prog_frame, textvariable=self.integrate_progress_var, width=40
            ).pack(side="left")
            self.integrate_progress = ttk.Progressbar(
                prog_frame, orient="horizontal", mode="determinate", length=400
            )
            self.integrate_progress.pack(side="left", fill="x", expand=True, padx=6)

        self.plot_figure = Figure(figsize=(7.5, 5.0), dpi=100)
        self.plot_canvas = FigureCanvasTkAgg(self.plot_figure, master=parent)
        self.plot_canvas.get_tk_widget().pack(fill="both", expand=True)
        toolbar = NavigationToolbar2Tk(self.plot_canvas, parent, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        self.plot_canvas.draw_idle()

    def _update_plot_pages(self, options: List[str]):
        """Refresh the page-selector combobox's choices, preserving the
        current selection if still valid. `options` is e.g. ['all','0',
        '1',...]. No-op if there's no selector or the options are
        unchanged."""
        combo = getattr(self, "plot_page_combo", None)
        var = getattr(self, "plot_page_var", None)
        if combo is None or var is None:
            return
        if list(combo["values"]) == options:
            return
        combo["values"] = options
        if var.get() not in options:
            var.set(options[0] if options else "all")

    def _current_plot_page(self) -> str:
        var = getattr(self, "plot_page_var", None)
        return var.get() if var is not None else "all"

    def _refresh_plots_from_text(self, step: StepDef, text: str):
        """Re-parse `text` for this step and redraw the figure. Safe to
        call repeatedly (live) and with partial/empty text. Dispatches on
        step.plot_kind."""
        if not HAVE_MPL or self.plot_figure is None or self.plot_canvas is None:
            return
        if self.selected_step is None or self.selected_step.id != step.id:
            return  # user navigated away; don't draw onto another step's tab

        kind = step.plot_kind
        try:
            if kind == "find_spots":
                self._plot_find_spots(text)
            elif kind == "refine":
                self._plot_refine(text)
            elif kind == "integrate":
                self._plot_integrate(text)
            elif kind == "scale":
                self._plot_scale(text)
            elif kind == "correlation_matrix":
                self._plot_correlation_matrix(text)
        except Exception as exc:  # pragma: no cover - defensive redraw guard
            if self.plot_status_var is not None:
                self.plot_status_var.set(f"(plot error: {exc})")
            return
        self.plot_canvas.draw_idle()

    def _set_plot_status(self, msg: str):
        if self.plot_status_var is not None:
            self.plot_status_var.set(msg)

    def _plot_find_spots(self, text: str):
        series = parse_find_spots_by_imageset(text)
        self._update_plot_pages(["all"])
        fig = self.plot_figure
        fig.clear()
        ax = fig.add_subplot(111)

        # Any series with actual points?
        nonempty = [s for s in series if s["image"]]
        if not nonempty:
            self._set_plot_status("(waiting for 'Found N strong pixels' lines...)")
            ax.set_title("Strong pixels per image")
            ax.set_xlabel("Image number")
            ax.set_ylabel("Strong pixels")
            fig.tight_layout()
            return

        # One line per imageset, all on the same axes so earlier imagesets
        # persist as the run proceeds. Image numbers restart at 1 for each
        # imageset, so the shared X axis is the per-imageset image number
        # and each imageset is a separate line (the caption/legend carries
        # the imageset number from the 'Finding strong spots on imageset N'
        # banner).
        labelled = 0
        for s in nonempty:
            iset = s["imageset"]
            label = f"imageset {iset}" if iset is not None else "imageset"
            ax.plot(s["image"], s["pixels"], marker=".", linewidth=1,
                    label=label)
            labelled += 1

        ax.set_title("Strong pixels found per image (one line per imageset)")
        ax.set_xlabel("Image number (within imageset)")
        ax.set_ylabel("Number of strong pixels")
        ax.grid(True, alpha=0.3)
        # Only show a legend when it stays readable; for many imagesets the
        # legend would swamp the plot, so cap it and note the count instead.
        if labelled <= 12:
            ax.legend(fontsize=7, ncol=2 if labelled > 6 else 1)

        n_sets = sum(1 for s in nonempty if s["imageset"] is not None)
        total_images = sum(len(s["image"]) for s in nonempty)
        if n_sets > 1:
            note = f"{n_sets} imagesets, {total_images} images total"
        elif n_sets == 1:
            note = f"imageset {nonempty[0]['imageset']}, {total_images} images"
        else:
            note = f"{total_images} images processed"
        self._set_plot_status(note)
        fig.tight_layout()

    def _plot_refine(self, text: str):
        fig = self.plot_figure
        fig.clear()

        # Multi-crystal refine writes an "RMSDs by experiment" table (one
        # row per data set). If present, show per-experiment final RMSDs;
        # the page selector lets you view all experiments together or the
        # per-step convergence (which multi refine doesn't tabulate per
        # experiment, so "all" is the meaningful view here).
        by_exp = parse_refine_by_experiment(text)
        if by_exp["exp"]:
            self._update_plot_pages(["all"])
            exps = by_exp["exp"]
            ax1 = fig.add_subplot(211)
            ax1.plot(exps, by_exp["rmsd_x"], marker="o", label="RMSD_X (px)")
            ax1.plot(exps, by_exp["rmsd_y"], marker="s", label="RMSD_Y (px)")
            ax1.set_ylabel("Positional RMSD (px)", fontsize=8)
            ax1.set_title("Final RMSDs by experiment (multi-crystal)", fontsize=9)
            ax1.legend(fontsize=7)
            ax1.grid(True, alpha=0.3)

            ax2 = fig.add_subplot(212)
            ax2.plot(exps, by_exp["rmsd_z"], marker="^", color="tab:green",
                     label="RMSD_Z (images)")
            ax2.set_xlabel("Experiment (data set) id", fontsize=8)
            ax2.set_ylabel("RMSD_Z (images)", fontsize=8)
            ax2.legend(fontsize=7)
            ax2.grid(True, alpha=0.3)
            self._set_plot_status(
                f"{len(exps)} experiments (data sets) refined"
            )
            fig.tight_layout()
            return

        # Single-crystal: the "Refinement steps" convergence table.
        self._update_plot_pages(["all"])
        data = parse_refine_steps(text)
        ax = fig.add_subplot(111)
        if not data["step"]:
            self._set_plot_status("(waiting for the 'Refinement steps' table...)")
            ax.set_title("Refinement RMSDs vs step")
            ax.set_xlabel("Refinement step")
            fig.tight_layout()
            return
        steps = data["step"]
        # RMSD_X and RMSD_Y are in mm; RMSD_Phi is in degrees. Put the two
        # positional RMSDs on the left axis and the angular one on a
        # secondary right-hand axis so all three are readable together.
        ax.plot(steps, data["rmsd_x"], marker="o", label="RMSD_X (mm)")
        ax.plot(steps, data["rmsd_y"], marker="s", label="RMSD_Y (mm)")
        ax.set_xlabel("Refinement step")
        ax.set_ylabel("Positional RMSD (mm)")
        ax.grid(True, alpha=0.3)

        ax2 = ax.twinx()
        ax2.plot(steps, data["rmsd_phi"], marker="^", color="tab:green",
                 label="RMSD_Phi (deg)")
        ax2.set_ylabel("Angular RMSD (deg)")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)
        ax.set_title("Refinement RMSDs vs step")
        self._set_plot_status(f"{len(steps)} refinement steps")
        fig.tight_layout()

    def _plot_integrate(self, text: str):
        blocks = parse_integrate_blocks(text)
        progress = parse_integrate_progress(text)

        # --- live progress bar (block processing) ---
        if self.integrate_progress is not None and self.integrate_progress_var is not None:
            n_blocks = len(blocks)
            if n_blocks:
                # The block loop runs twice (profile modelling, then
                # integration). Show progress within the current pass.
                done = progress["completed"]
                pass_no = 1 if done <= n_blocks else 2
                in_pass = done if done <= n_blocks else done - n_blocks
                in_pass = min(in_pass, n_blocks)
                self.integrate_progress.config(maximum=n_blocks, value=in_pass)
                label = (
                    f"Pass {pass_no}/2 - block {in_pass}/{n_blocks}"
                    if done else f"Blocks: 0/{n_blocks}"
                )
                if progress["last_to"]:
                    label += f"  (frames {progress['last_from']} -> {progress['last_to']})"
                self.integrate_progress_var.set(label)
            else:
                self.integrate_progress.config(value=0)
                self.integrate_progress_var.set("Blocks: waiting for block table...")

        # --- end-of-integration line graphs (Summary vs image number) ---
        fig = self.plot_figure
        fig.clear()

        # Split the summary table by data set (ID column). In single-crystal
        # runs there's just one data set (id 0); in multi-crystal runs there
        # are many, and the page selector picks which one to show.
        by_ds = integrate_summary_by_dataset(text)
        if by_ds:
            ds_ids = sorted(by_ds)
            # Page options: one page per data set. (No "all" overlay - with
            # many data sets that would be unreadable; flip between them.)
            self._update_plot_pages([str(d) for d in ds_ids])
            page = self._current_plot_page()
            try:
                sel = int(page)
            except ValueError:
                sel = ds_ids[0]
            if sel not in by_ds:
                sel = ds_ids[0]
            summary = by_ds[sel]
            ds_note = (
                f"  |  data set {sel} of {len(ds_ids)}"
                if len(ds_ids) > 1 else ""
            )
        else:
            summary = parse_integrate_summary(text)
            self._update_plot_pages(["all"])
            ds_note = ""

        if not summary["image"]:
            ax = fig.add_subplot(111)
            n = len(blocks)
            if n:
                self._set_plot_status(
                    f"Integrating {n} blocks - per-image summary plots will "
                    "appear when integration finishes."
                )
            else:
                self._set_plot_status("(waiting for integration to start...)")
            ax.set_title("Integration summary vs image (pending)")
            ax.set_xlabel("Image number")
            fig.tight_layout()
            return

        img = summary["image"]
        # Four stacked panels of the most useful per-image diagnostics.
        ax1 = fig.add_subplot(221)
        ax1.plot(img, summary["isigi_sum"], label="I/sigI (sum)", linewidth=1)
        ax1.plot(img, summary["isigi_prf"], label="I/sigI (prf)", linewidth=1)
        ax1.set_title("I/sigma vs image", fontsize=9)
        ax1.set_xlabel("Image", fontsize=8)
        ax1.legend(fontsize=7)
        ax1.grid(True, alpha=0.3)

        ax2 = fig.add_subplot(222)
        ax2.plot(img, summary["n_full"], label="# full", linewidth=1)
        ax2.plot(img, summary["n_part"], label="# part", linewidth=1)
        ax2.set_title("Reflection counts vs image", fontsize=9)
        ax2.set_xlabel("Image", fontsize=8)
        ax2.legend(fontsize=7)
        ax2.grid(True, alpha=0.3)

        ax3 = fig.add_subplot(223)
        ax3.plot(img, summary["cc_prf"], color="tab:purple", linewidth=1)
        ax3.set_title("CC prf vs image", fontsize=9)
        ax3.set_xlabel("Image", fontsize=8)
        ax3.grid(True, alpha=0.3)

        ax4 = fig.add_subplot(224)
        ax4.plot(img, summary["rmsd_xy"], color="tab:red", linewidth=1)
        ax4.set_title("RMSD XY vs image", fontsize=9)
        ax4.set_xlabel("Image", fontsize=8)
        ax4.grid(True, alpha=0.3)

        self._set_plot_status(
            f"Integration summary over {len(img)} images{ds_note}"
        )
        fig.tight_layout()

    def _plot_scale(self, text: str):
        data = parse_scale_merging(text)
        self._update_plot_pages(["all"])
        cluster = self._selected_cluster()
        cluster_note = f"  |  cluster {cluster}" if cluster is not None else ""
        fig = self.plot_figure
        fig.clear()
        inv = data["inv_d2"]  # type: ignore[assignment]
        if not inv:
            ax = fig.add_subplot(111)
            self._set_plot_status(
                "(waiting for 'Merging statistics by resolution bin'..."
                + (f" - cluster {cluster})" if cluster is not None else ")")
            )
            ax.set_title("Merging statistics vs resolution (pending)")
            fig.tight_layout()
            return

        d_min = data["d_min"]  # type: ignore[assignment]

        def _res_ticks(ax):
            """Label the 1/d^2 x-axis with the actual resolution (d, in A)
            at each tick so the non-linear axis stays readable."""
            import numpy as _np  # matplotlib always brings numpy
            xt = _np.linspace(min(inv), max(inv), 6)
            ax.set_xticks(xt)
            ax.set_xticklabels([f"{(1.0/_np.sqrt(t)):.2f}" for t in xt])
            ax.set_xlabel("Resolution d (A)  [x axis linear in 1/d^2]", fontsize=8)

        # Panel 1: <I/sigI>
        ax1 = fig.add_subplot(221)
        ax1.plot(inv, data["i_over_sigma"], marker=".", linewidth=1)  # type: ignore[index]
        ax1.set_title("<I/sigma> vs resolution", fontsize=9)
        ax1.set_ylabel("<I/sigI>", fontsize=8)
        _res_ticks(ax1)
        ax1.grid(True, alpha=0.3)

        # Panel 2: CC1/2 and CC_anom
        ax2 = fig.add_subplot(222)
        ax2.plot(inv, data["cc_half"], marker=".", label="CC1/2", linewidth=1)  # type: ignore[index]
        ax2.plot(inv, data["cc_anom"], marker=".", label="CC_anom", linewidth=1)  # type: ignore[index]
        ax2.set_title("CC vs resolution", fontsize=9)
        ax2.set_ylabel("CC", fontsize=8)
        ax2.legend(fontsize=7)
        _res_ticks(ax2)
        ax2.grid(True, alpha=0.3)

        # Panel 3: R-factors
        ax3 = fig.add_subplot(223)
        ax3.plot(inv, data["r_merge"], marker=".", label="Rmerge", linewidth=1)  # type: ignore[index]
        ax3.plot(inv, data["r_meas"], marker=".", label="Rmeas", linewidth=1)  # type: ignore[index]
        ax3.plot(inv, data["r_pim"], marker=".", label="Rpim", linewidth=1)  # type: ignore[index]
        ax3.set_title("R-factors vs resolution", fontsize=9)
        ax3.set_ylabel("R", fontsize=8)
        ax3.legend(fontsize=7)
        _res_ticks(ax3)
        ax3.grid(True, alpha=0.3)

        # Panel 4: completeness & multiplicity
        ax4 = fig.add_subplot(224)
        ax4.plot(inv, data["completeness"], marker=".", color="tab:green",  # type: ignore[index]
                 label="Completeness (%)", linewidth=1)
        ax4.set_ylabel("Completeness (%)", fontsize=8)
        ax4b = ax4.twinx()
        ax4b.plot(inv, data["mult"], marker=".", color="tab:orange",  # type: ignore[index]
                  label="Multiplicity", linewidth=1)
        ax4b.set_ylabel("Multiplicity", fontsize=8)
        ax4.set_title("Completeness & multiplicity", fontsize=9)
        _res_ticks(ax4)
        ax4.grid(True, alpha=0.3)

        overall = data.get("overall")
        if isinstance(overall, dict):
            self._set_plot_status(
                f"{len(inv)} resolution bins{cluster_note}  |  overall: "
                f"d_min {overall['d_min']:.2f} A, "
                f"I/sigI {overall['i_over_sigma']:.1f}, "
                f"CC1/2 {overall['cc_half']:.3f}"
            )
        else:
            self._set_plot_status(f"{len(inv)} resolution bins{cluster_note}")
        fig.tight_layout()

    def _plot_correlation_matrix(self, text: str):
        """Plot the diagnostics embedded in dials.correlation_matrix.html.

        Unlike the other plot kinds, the source here is the HTML file
        (passed in as `text`), not a .log - it carries Plotly JSON blobs we
        parse and re-render with matplotlib. Shows: the correlation and
        cos-angle matrices (as heatmaps), the OPTICS reachability plot
        (coloured per cluster), the cosym PCA coordinates (per cluster),
        the dimensions residual curve and the Rij histogram. The page
        selector isn't used here (it's a fixed multi-panel view)."""
        import numpy as _np
        self._update_plot_pages(["all"])
        fig = self.plot_figure
        fig.clear()

        graphs = extract_corrmat_graphs(text) if text else {}
        if not graphs:
            ax = fig.add_subplot(111)
            self._set_plot_status(
                "(run dials.correlation_matrix, or use 'Refresh plots from "
                "log' - reads dials.correlation_matrix.html)"
            )
            ax.set_title("Correlation matrix analysis (pending)")
            fig.tight_layout()
            return

        panels = []  # (draw_fn, present?) collected then laid out on a grid

        cc = graphs.get("graphs_cc_cluster")
        cos = graphs.get("graphs_cos_angle_cluster")
        reach = graphs.get("graphs_reachability")
        coords = graphs.get("graphs_cosym_coordinates_principal_components")
        dims = graphs.get("graphs_dimensions")
        rij = graphs.get("graphs_cosym_rij_histogram_sg")

        def draw_matrix(ax, blob, default_title):
            m = corrmat_matrix(blob)
            if m is None:
                return
            z = _np.array(m["z"])
            im = ax.imshow(z, cmap="YlOrRd", aspect="auto")
            ax.set_title(m.get("title") or default_title, fontsize=9)
            ax.tick_params(labelsize=6)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

        def draw_clusters(ax, blob, title, scatter):
            series = corrmat_cluster_series(blob)
            for s in series:
                color = None  # let matplotlib choose; rgb strings from
                # plotly ("rgb(0.53,0,0.59)") are 0-1 floats and matplotlib
                # wants 0-1 too, but the format differs, so skip explicit
                # colour and rely on the cycle for robustness.
                if scatter:
                    ax.scatter(s["x"], s["y"], s=10, label=s["name"])
                else:
                    ys = [_np.nan if v is None else v for v in s["y"]]
                    ax.bar(s["x"], ys, label=s["name"])
            ax.set_title(title, fontsize=9)
            ax.tick_params(labelsize=6)
            ax.legend(fontsize=6)

        def draw_xy(ax, blob, logy=False):
            xy = corrmat_xy(blob)
            if xy is None:
                return
            if xy["type"] == "bar":
                ax.bar(xy["x"], xy["y"], width=(xy["x"][1] - xy["x"][0]) * 0.9
                       if len(xy["x"]) > 1 else 0.02)
            else:
                ax.plot(xy["x"], xy["y"], marker=".", linewidth=1)
            if logy:
                ax.set_yscale("log")
            ax.set_title(xy["title"] or "", fontsize=9)
            ax.tick_params(labelsize=6)
            ax.grid(True, alpha=0.3)

        if cc is not None:
            panels.append(lambda ax: draw_matrix(ax, cc, "Correlation matrix"))
        if cos is not None:
            panels.append(lambda ax: draw_matrix(ax, cos, "cos(angle) matrix"))
        if reach is not None:
            panels.append(lambda ax: draw_clusters(
                ax, reach, "OPTICS reachability", scatter=False))
        if coords is not None:
            panels.append(lambda ax: draw_clusters(
                ax, coords, "Cosym PCA coordinates", scatter=True))
        if dims is not None:
            panels.append(lambda ax: draw_xy(ax, dims, logy=True))
        if rij is not None:
            panels.append(lambda ax: draw_xy(ax, rij, logy=False))

        n = len(panels)
        if n == 0:
            ax = fig.add_subplot(111)
            ax.set_title("No recognised correlation-matrix graphs found",
                         fontsize=9)
            self._set_plot_status("(no plottable graphs in the HTML)")
            fig.tight_layout()
            return

        ncols = 2
        nrows = (n + ncols - 1) // ncols
        for i, draw in enumerate(panels):
            ax = fig.add_subplot(nrows, ncols, i + 1)
            try:
                draw(ax)
            except Exception:
                ax.set_title("(failed to draw)", fontsize=8)

        # Cluster summary from the cc blob's cluster dict, if present.
        clusters = (cc or {}).get("clusters", {}) if cc else {}
        self._set_plot_status(
            f"correlation_matrix: {n} graphs from HTML"
            + (f"  |  {len(clusters)} dendrogram nodes" if clusters else "")
        )
        fig.tight_layout()

    # --------------------------------------------------------- dials.report --
    def _report_files_for_step(self, step: StepDef) -> List[str]:
        """Work out which .expt/.refl files best represent the result of
        this stage, to hand to `dials.report`. Prefers the stage's own
        freshly-written outputs; falls back to pairing a single new
        output with the other file type from the current input fields;
        falls back again to the step's current inputs for stages (like
        Bravais lattice determination or Merge/Export) that don't
        themselves write a fresh .expt/.refl pair."""

        expt_out = next((o for o in step.outputs if o.endswith(".expt")), None)
        refl_out = next((o for o in step.outputs if o.endswith(".refl")), None)

        input_values = [v.get().strip() for v in self.input_vars.get(step.id, [])]

        if expt_out and refl_out:
            return [expt_out, refl_out]
        if expt_out and not refl_out:
            refl_in = next((v for v in input_values if v.endswith(".refl")), "")
            return [f for f in [expt_out, refl_in] if f]
        if refl_out and not expt_out:
            expt_in = next((v for v in input_values if v.endswith(".expt")), "")
            return [f for f in [expt_in, refl_out] if f]

        if step.is_import:
            return ["imported.expt"]

        return [v for v in input_values if v]

    def run_and_show_report(self, step: StepDef):
        workdir = self.workdir.get()
        if not os.path.isdir(workdir):
            messagebox.showerror("Invalid directory", f"{workdir} is not a directory")
            return
        if shutil.which("dials.report") is None:
            messagebox.showerror(
                "Not found", "dials.report was not found on $PATH."
            )
            return

        files = self._report_files_for_step(step)
        if not files:
            messagebox.showwarning(
                "No files",
                "No experiment/reflection files are set for this step yet - "
                "fill in the input fields above (or run the step first).",
            )
            return

        cmd = ["dials.report"] + files
        self.report_button.config(state="disabled")
        self.report_status_var.set(f"Running: {' '.join(cmd)} ...")

        def worker():
            try:
                result = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True
                )
            except Exception as exc:
                self.after(0, lambda: self._report_finished(
                    step, False, f"Could not launch dials.report: {exc}"
                ))
                return

            html_path = os.path.join(workdir, "dials.report.html")
            if result.returncode != 0 or not os.path.exists(html_path):
                tail = (result.stdout or "")[-1500:] + "\n" + (result.stderr or "")[-1500:]
                self.after(0, lambda: self._report_finished(
                    step, False,
                    f"dials.report exited with code {result.returncode}:\n{tail}",
                ))
                return

            self.after(0, lambda: self._report_finished(step, True, html_path))

        threading.Thread(target=worker, daemon=True).start()

    def _report_finished(self, step: StepDef, ok: bool, detail: str):
        self.report_button.config(state="normal")
        if ok:
            self.report_status_var.set(f"Opened {detail} in your web browser.")
            webbrowser.open(f"file://{detail}")
        else:
            self.report_status_var.set("dials.report failed - see error dialog.")
            messagebox.showerror("dials.report failed", detail)

    # ------------------------------------------------------------ tools --
    def launch_tool(self, program: str, arg_labels: List[str]):
        step = self.selected_step
        defaults = []
        if step is not None:
            if step.is_import:
                defaults = ["imported.expt"]
            else:
                defaults = [v.get() for v in self.input_vars.get(step.id, [])]
        while len(defaults) < len(arg_labels):
            defaults.append("")

        dialog = tk.Toplevel(self)
        dialog.title(f"Launch {program}")
        vars_ = []
        for label, default in zip(arg_labels, defaults):
            row = ttk.Frame(dialog, padding=4)
            row.pack(fill="x")
            ttk.Label(row, text=label, width=28).pack(side="left")
            v = tk.StringVar(value=default)
            ttk.Entry(row, textvariable=v, width=50).pack(side="left")
            vars_.append(v)

        def do_launch():
            args = [v.get().strip() for v in vars_ if v.get().strip()]
            cmd = [program] + args
            workdir = self.workdir.get()
            if shutil.which(program) is None:
                messagebox.showerror(
                    "Not found", f"{program} was not found on $PATH."
                )
                return
            try:
                subprocess.Popen(cmd, cwd=workdir)
            except Exception as exc:
                messagebox.showerror("Error launching tool", str(exc))
                return
            if program == "dials.report":
                # dials.report writes dials.report.html into the cwd;
                # give it a moment then try to open it in a browser.
                def _open_report():
                    html_path = os.path.join(workdir, "dials.report.html")
                    if os.path.exists(html_path):
                        webbrowser.open(f"file://{html_path}")
                self.after(4000, _open_report)
            dialog.destroy()

        btn_row = ttk.Frame(dialog, padding=6)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Launch", command=do_launch).pack(side="left")
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy).pack(
            side="left", padx=4
        )


def main():
    if sys.platform.startswith("win"):
        pass  # no special handling needed currently
    app = DialsGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
