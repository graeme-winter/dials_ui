"""Pure, GUI-free parsers turning DIALS stdout / log text into series.

Every function here is tolerant of partial (streaming) input, so the live
Plots tab can call them repeatedly while a step is still running. No wx or
matplotlib import belongs in this module.
"""

from __future__ import annotations

import json
import math
import re
from typing import Dict, List, Optional, Tuple

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
            while (
                j > 0
                and lines[j - 1].strip()
                and not _TABLE_LINE.match(lines[j - 1])
                and len(block) < 1
            ):
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


# find_spots processes one imageset at a time; each is introduced by a
# banner block like:
#   --------------------------------------------------------------------
#   Finding strong spots in imageset 0
#   --------------------------------------------------------------------
# and then emits its own "Found N strong pixels on image M" lines (with M
# a per-imageset frame number, restarting at 1 for each imageset). We split
# on these banners so each imageset is a separate series on the plot,
# captioned by its imageset number.
#
# The real DIALS wording is "Finding strong spots IN imageset N" (see
# dials.algorithms.spot_finding.finder). We accept "in" or "on", and allow
# the phrase to appear with or without the word "strong", to be robust to
# small wording changes across DIALS versions.
_FIND_SPOTS_IMAGESET_RE = re.compile(
    r"Finding\s+(?:strong\s+)?spots\s+(?:in|on)\s+imageset\s+(\d+)",
    re.IGNORECASE,
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
            current["image"].append(float(m.group(2)))  # type: ignore[union-attr]
            current["pixels"].append(float(m.group(1)))  # type: ignore[union-attr]

    # Drop any empty banner-only series (e.g. a banner seen but no spot
    # lines yet is fine to keep; but a trailing empty one adds nothing).
    return [s for s in series if s["image"]] or series


_REFINE_HEADER_RE = re.compile(r"Refinement steps", re.IGNORECASE)


_REFINE_GROUP_RE = re.compile(
    r"Selected group of experiments to refine with original ids:\s*([0-9,\s]+)",
    re.IGNORECASE,
)


def _parse_one_refine_table(
    lines: List[str], start: int
) -> Tuple[Dict[str, List[float]], int]:
    """Parse a single 'Refinement steps' table whose header is at/after
    `start`. Returns (table_dict, index_after_table). table_dict is empty
    if no data rows were found."""
    tbl = {"step": [], "rmsd_x": [], "rmsd_y": [], "rmsd_phi": []}
    j = start
    n = len(lines)
    started = False
    while j < n:
        cells = _split_table_row(lines[j])
        if cells is None:
            if started and not lines[j].strip():
                break
            j += 1
            continue
        if len(cells) < 5:
            j += 1
            continue
        try:
            step = float(int(cells[0]))
            x = float(cells[2])
            y = float(cells[3])
            phi = float(cells[4])
        except ValueError:
            j += 1
            continue
        started = True
        tbl["step"].append(step)
        tbl["rmsd_x"].append(x)
        tbl["rmsd_y"].append(y)
        tbl["rmsd_phi"].append(phi)
        j += 1
    return tbl, j


def parse_all_refine_steps(text: str) -> List[Dict[str, List[float]]]:
    """refine (multi-crystal joint=false): one convergence table per
    refinement RUN, in order.

    A run is delimited by a
    'Selected group of experiments to refine with original ids: <ids>'
    line. Within each run, refinement may print several 'Refinement steps'
    tables (e.g. static then scan-varying macrocycles); we keep only the
    LAST one in the run (the final convergence). This avoids the earlier
    bug where every macrocycle table counted as a separate 'run', inflating
    the run count well beyond the number of experiments actually refined.

    Each returned dict is {'step','rmsd_x','rmsd_y','rmsd_phi','ids'} where
    'ids' is the original experiment id string from the group marker (or ''
    if the run wasn't introduced by a marker - e.g. single-crystal refine,
    which has no such marker and yields a single run from its lone table).

    RMSD columns may be mm (single sweep) or px (multi-crystal); we take
    columns 2/3/4 after the integer step in column 0."""
    lines = text.splitlines()
    n = len(lines)

    # Find the group-marker positions.
    markers = [
        (i, m.group(1).strip())
        for i, line in enumerate(lines)
        for m in [_REFINE_GROUP_RE.search(line)]
        if m
    ]

    tables: List[Dict[str, List[float]]] = []

    if not markers:
        # No group markers (single-crystal refine, or an older/661 DIALS
        # format): fall back to keeping the LAST 'Refinement steps' table in
        # the whole output, so we don't over-count macrocycles. (Previously
        # this returned every table; the final one is the meaningful
        # convergence for a single run.)
        last = None
        i = 0
        while i < n:
            if _REFINE_HEADER_RE.search(lines[i]):
                tbl, j = _parse_one_refine_table(lines, i + 1)
                if tbl["step"]:
                    last = tbl
                i = max(j, i + 1)
            else:
                i += 1
        if last is not None:
            last["ids"] = ""
            tables.append(last)
        return tables

    # With markers: for each run (marker i .. next marker), keep the LAST
    # 'Refinement steps' table found within that span.
    for idx, (mstart, ids) in enumerate(markers):
        mend = markers[idx + 1][0] if idx + 1 < len(markers) else n
        last = None
        i = mstart + 1
        while i < mend:
            if _REFINE_HEADER_RE.search(lines[i]):
                tbl, j = _parse_one_refine_table(lines, i + 1)
                if tbl["step"]:
                    last = tbl
                i = max(j, i + 1)
            else:
                i += 1
        if last is not None:
            last["ids"] = ids
            tables.append(last)
    return tables


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


# Multi-crystal indexing (joint=false) processes one imageset at a time and
# prints a line like "Indexing imageset id 19 (20/36)". The (k/N) is a
# reliable progress measure: k of N imagesets started. We take the last
# such line seen.
_INDEX_PROGRESS_RE = re.compile(
    r"Indexing\s+imageset\s+id\s+(\d+)\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)",
    re.IGNORECASE,
)


def parse_index_progress(text: str) -> Optional[Dict[str, int]]:
    """index (multi): parse 'Indexing imageset id <id> (k/N)' progress
    lines. Returns {'imageset_id': id, 'done': k, 'total': N} for the most
    recent line, or None if no such line has appeared (single-crystal
    indexing, or not started yet)."""
    matches = _INDEX_PROGRESS_RE.findall(text)
    if not matches:
        return None
    iset, done, total = matches[-1]
    return {"imageset_id": int(iset), "done": int(done), "total": int(total)}


_SUMMARY_VS_IMAGE_RE = re.compile(r"Summary vs image number", re.IGNORECASE)
_INTEGRATE_SUMMARY_KEYS = [
    "image",
    "n_full",
    "n_part",
    "n_over",
    "n_ice",
    "n_sum",
    "n_prf",
    "ibg",
    "isigi_sum",
    "isigi_prf",
    "cc_prf",
    "rmsd_xy",
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
    for line in lines[header_idx + 1 :]:
        cells = _split_table_row(line)
        if cells is None:
            if started and not line.strip():
                break
            continue
        if len(cells) < 13:
            continue
        try:
            vals = [
                float(int(cells[1])),  # image
                float(int(cells[2])),  # n_full
                float(int(cells[3])),  # n_part
                float(int(cells[4])),  # n_over
                float(int(cells[5])),  # n_ice
                float(int(cells[6])),  # n_sum
                float(int(cells[7])),  # n_prf
                float(cells[8]),  # ibg
                float(cells[9]),  # isigi_sum
                float(cells[10]),  # isigi_prf
                float(cells[11]),  # cc_prf
                float(cells[12]),  # rmsd_xy
            ]
        except (ValueError, IndexError):
            continue
        started = True
        for k, v in zip(_INTEGRATE_SUMMARY_KEYS, vals):
            result[k].append(v)

    return result


_MERGING_HEADER_RE = re.compile(r"Merging statistics by resolution bin", re.IGNORECASE)
_MERGING_KEYS = [
    "d_max",
    "d_min",
    "inv_d2",
    "mult",
    "completeness",
    "i_mean",
    "i_over_sigma",
    "r_merge",
    "r_meas",
    "r_pim",
    "r_anom",
    "cc_half",
    "cc_anom",
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
    for line in lines[header_idx + 1 :]:
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
        (
            d_max,
            d_min,
            _nobs,
            _nuniq,
            mult,
            comp,
            i_mean,
            i_sig,
            r_mrg,
            r_meas,
            r_pim,
            r_anom,
            cc12,
            ccano,
        ) = r
        d_geo = math.sqrt(d_min * d_max)
        result["d_max"].append(d_max)  # type: ignore[union-attr]
        result["d_min"].append(d_min)  # type: ignore[union-attr]
        result["inv_d2"].append(1.0 / (d_geo * d_geo))  # type: ignore[union-attr]
        result["mult"].append(mult)  # type: ignore[union-attr]
        result["completeness"].append(comp)  # type: ignore[union-attr]
        result["i_mean"].append(i_mean)  # type: ignore[union-attr]
        result["i_over_sigma"].append(i_sig)  # type: ignore[union-attr]
        result["r_merge"].append(r_mrg)  # type: ignore[union-attr]
        result["r_meas"].append(r_meas)  # type: ignore[union-attr]
        result["r_pim"].append(r_pim)  # type: ignore[union-attr]
        result["r_anom"].append(r_anom)  # type: ignore[union-attr]
        result["cc_half"].append(cc12)  # type: ignore[union-attr]
        result["cc_anom"].append(ccano)  # type: ignore[union-attr]

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
                float(int(cells[1])),  # image
                float(int(cells[2])),  # n_full
                float(int(cells[3])),  # n_part
                float(int(cells[4])),  # n_over
                float(int(cells[5])),  # n_ice
                float(int(cells[6])),  # n_sum
                float(int(cells[7])),  # n_prf
                float(cells[8]),  # ibg
                float(cells[9]),  # isigi_sum
                float(cells[10]),  # isigi_prf
                float(cells[11]),  # cc_prf
                float(cells[12]),  # rmsd_xy
            ]
        except (ValueError, IndexError):
            continue
        d = out.setdefault(ds, {k: [] for k in _INTEGRATE_SUMMARY_KEYS})
        for k, v in zip(_INTEGRATE_SUMMARY_KEYS, row):
            d[k].append(v)

    return out


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
                return text[start_brace : i + 1]
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
        out.append(
            {
                "name": t.get("name", ""),
                "x": t.get("x", []),
                "y": ys,
                "color": (t.get("marker", {}) or {}).get("color"),
            }
        )
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
