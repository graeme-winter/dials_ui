"""The wxPython application frame (DialsFrame) and all its GUI logic."""

from __future__ import annotations

import glob
import os
import queue
import re
import shutil
import subprocess
import threading
import webbrowser
from typing import Dict, List, Optional, Tuple

import wx

# matplotlib is an optional dependency: the live-plotting "Plots" tab is only
# offered if it (and its wxAgg backend) import successfully. Everything else
# works without it, so a missing matplotlib degrades to "no Plots tab".
try:
    import matplotlib

    matplotlib.use("WXAgg")
    from matplotlib.backends.backend_wxagg import (
        FigureCanvasWxAgg as FigureCanvas,
    )
    from matplotlib.backends.backend_wxagg import (
        NavigationToolbar2WxAgg as NavigationToolbar,
    )
    from matplotlib.figure import Figure

    HAVE_MPL = True
except Exception:  # pragma: no cover - depends on environment
    HAVE_MPL = False

from .icon import get_app_icon
from .model import StepDef
from .parsers import (
    corrmat_cluster_series,
    corrmat_matrix,
    corrmat_xy,
    cosym_dendrogram,
    cosym_hist_series,
    cosym_scatter_series,
    extract_corrmat_graphs,
    integrate_summary_by_dataset,
    parse_all_refine_steps,
    parse_find_spots_by_imageset,
    parse_index_progress,
    parse_integrate_blocks,
    parse_integrate_progress,
    parse_integrate_summary,
    parse_scale_merging,
    summarise_log,
)
from .runner import ProcessRunner, _FalseVar, _WidgetVar
from .steps import STATUS_ICONS, STEPS, TOOLS


class DialsFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="DIALS Workflow GUI", size=(1180, 760))
        self.SetIcon(get_app_icon())

        self.workdir_value = os.getcwd()
        self.status = {s.id: "pending" for s in STEPS}
        self.selected_step: Optional[StepDef] = None
        self.field_vars: dict = {}  # step id -> {field key: _WidgetVar}
        self.input_vars: dict = {}  # step id -> [_WidgetVar per input]
        self.image_files: List[str] = []

        self.runner: Optional[ProcessRunner] = None
        self.running_step_id: Optional[str] = None

        # Live-plot state. `live_output` accumulates the raw stdout of the
        # currently running step so the plot parsers (which want the whole
        # text so far) can be re-run on each poll. The plot widgets are
        # rebuilt per select_step(); `plot_canvas` is None when the current
        # step has no Plots tab or matplotlib is unavailable.
        self.live_output: str = ""
        self.plot_canvas = None  # FigureCanvasWxAgg or None
        self.plot_figure = None  # matplotlib Figure or None
        self.plot_status_label = None  # wx.StaticText or None
        self.progress_bar = None  # wx.Gauge or None
        self.progress_label = None  # wx.StaticText or None
        self.plot_page_combo = None  # wx.ComboBox or None
        self.scale_cluster_combo = None  # wx.ComboBox or None
        self.log_cluster_combo = None  # wx.ComboBox or None
        # Redrawing the figure on every streamed line is wasteful; only
        # redraw every Nth poll or on completion.
        self._poll_tick = 0

        # Widgets rebuilt per step and referenced elsewhere.
        self.output_text = None
        self.summary_text = None
        self.log_text = None
        self.command_preview = None
        self.report_status_label = None
        self.run_button = None
        self.stop_button = None
        self.report_button = None
        self.extra_params_ctrl = None
        self.import_listbox = None
        self.notebook = None

        self._build_layout()
        self._check_dials_available()

        # Poll timer for the running subprocess. wx has no direct analogue of
        # Tk's after()-scheduled polling loop; a repeating timer polls the
        # ProcessRunner queue while a step runs and is otherwise idle.
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)

        self.select_step(STEPS[0])

        # On startup, silently pick up any existing pipeline progress in the
        # initial working directory (cwd) so the GUI reflects work already
        # done there, as if it had been run through the GUI. Silent so it
        # doesn't nag when starting in an empty directory; the user can also
        # re-run this any time via the "Load state from working dir" button.
        try:
            self._load_state_from_workdir(announce=False)
        except Exception:
            pass

    # ---------------------------------------------------------- workdir --
    @property
    def workdir(self):
        # Kept as a property so the many `self.workdir.get()` call sites in
        # the ported pure-logic helpers continue to work unchanged.
        parent = self

        class _WD:
            def get(_self):
                return parent.workdir_value

            def set(_self, v):
                parent.workdir_value = v
                if parent.workdir_ctrl is not None:
                    parent.workdir_ctrl.SetValue(v)

        return _WD()

    # ---------------------------------------------------------- top bar --
    def _build_layout(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- top bar ---
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(
            wx.StaticText(panel, label="Working directory:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.ALL,
            4,
        )
        self.workdir_ctrl = wx.TextCtrl(panel, value=self.workdir_value, size=(480, -1))
        top.Add(self.workdir_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        self.workdir_ctrl.Bind(
            wx.EVT_TEXT,
            lambda _e: setattr(self, "workdir_value", self.workdir_ctrl.GetValue()),
        )
        browse_btn = wx.Button(panel, label="Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, lambda _e: self._choose_workdir())
        top.Add(browse_btn, 0, wx.ALL, 4)
        load_btn = wx.Button(panel, label="Load state from working dir")
        load_btn.Bind(wx.EVT_BUTTON, lambda _e: self._load_state_from_workdir())
        top.Add(load_btn, 0, wx.ALL, 4)
        self.dials_status_label = wx.StaticText(panel, label="")
        self.dials_status_label.SetForegroundColour(wx.RED)
        top.Add(self.dials_status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)
        outer.Add(top, 0, wx.EXPAND)

        # --- body: sidebar + main ---
        body = wx.BoxSizer(wx.HORIZONTAL)

        side = wx.BoxSizer(wx.VERTICAL)
        hdr = wx.StaticText(panel, label="Pipeline steps")
        hdr.SetFont(hdr.GetFont().Bold())
        side.Add(hdr, 0, wx.ALL, 4)

        self.step_buttons: dict = {}
        for s in STEPS:
            row = wx.BoxSizer(wx.HORIZONTAL)
            icon = wx.StaticText(panel, label=STATUS_ICONS["pending"], size=(20, -1))
            row.Add(icon, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 2)
            btn = wx.Button(panel, label=s.title, size=(260, -1))
            btn.Bind(wx.EVT_BUTTON, lambda _e, st=s: self.select_step(st))
            row.Add(btn, 1, wx.EXPAND)
            side.Add(row, 0, wx.EXPAND | wx.BOTTOM, 1)
            self.step_buttons[s.id] = (btn, icon)

        side.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)
        vh = wx.StaticText(panel, label="Viewing tools")
        vh.SetFont(vh.GetFont().Bold())
        side.Add(vh, 0, wx.ALL, 4)
        for label, program, arg_labels in TOOLS:
            b = wx.Button(panel, label=label)
            b.Bind(
                wx.EVT_BUTTON,
                lambda _e, p=program, a=arg_labels: self.launch_tool(p, a),
            )
            side.Add(b, 0, wx.EXPAND | wx.BOTTOM, 1)

        side.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)
        reset_btn = wx.Button(panel, label="Reset all step statuses")
        reset_btn.Bind(wx.EVT_BUTTON, lambda _e: self._reset_statuses())
        side.Add(reset_btn, 0, wx.EXPAND)

        body.Add(side, 0, wx.EXPAND | wx.ALL, 4)

        # main panel holds the per-step notebook, rebuilt by select_step().
        self.main_panel = wx.Panel(panel)
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.main_panel.SetSizer(self.main_sizer)
        body.Add(self.main_panel, 1, wx.EXPAND | wx.ALL, 6)

        outer.Add(body, 1, wx.EXPAND)
        panel.SetSizer(outer)
        self._root_panel = panel

    def _check_dials_available(self):
        if shutil.which("dials.import") is None:
            self.dials_status_label.SetLabel(
                "Warning: dials.import not found on $PATH - "
                "make sure your DIALS environment is set up."
            )
            self.dials_status_label.SetForegroundColour(wx.RED)
        else:
            self.dials_status_label.SetLabel("DIALS found on $PATH")
            self.dials_status_label.SetForegroundColour(wx.Colour(0, 128, 0))
        self.dials_status_label.GetParent().Layout()

    def _choose_workdir(self):
        dlg = wx.DirDialog(
            self, "Choose working directory", defaultPath=self.workdir_value
        )
        if dlg.ShowModal() == wx.ID_OK:
            d = dlg.GetPath()
            self.workdir_value = d
            self.workdir_ctrl.SetValue(d)
            # Reflect any existing progress in the newly-chosen directory.
            self.image_files = []
            try:
                self._load_state_from_workdir(announce=False)
            except Exception:
                pass
        dlg.Destroy()

    def _reset_statuses(self):
        for s in STEPS:
            self.status[s.id] = "pending"
            _, icon = self.step_buttons[s.id]
            icon.SetLabel(STATUS_ICONS["pending"])

    def _step_outputs_present(self, step: StepDef) -> bool:
        """True if this step looks 'done' judging by files in the working
        directory: its declared output files exist, or (for steps that
        declare none) its log file exists."""
        workdir = self.workdir.get()

        def here(name: str) -> bool:
            return os.path.exists(os.path.join(workdir, name))

        # merge/export writes an MTZ or a log depending on the mode; treat
        # either produced log as done.
        if step.id == "merge_export":
            return (
                here("dials.merge.log")
                or here("dials.export.log")
                or here("merged.mtz")
                or here("scaled.mtz")
            )
        # scale may have run per-cluster (no plain scaled.expt) - accept any
        # scale log/result as evidence it ran.
        if step.id == "scale":
            if here("scaled.expt") or here("dials.scale.log"):
                return True
            try:
                for nm in os.listdir(workdir):
                    if re.match(r"dials\.scale\.cluster_\d+\.log$", nm) or re.match(
                        r"scaled_cluster_\d+\.expt$", nm
                    ):
                        return True
            except OSError:
                pass
            return False
        # correlation_matrix declares no outputs; use its HTML/log.
        if step.id == "correlation_matrix":
            return (
                here("dials.correlation_matrix.html")
                or here("dials.correlation_matrix.log")
                or here("dials.correlation_matrix.scaled.html")
            )

        if step.outputs:
            return all(here(o) for o in step.outputs if o.endswith((".expt", ".refl")))
        if step.log_file:
            return here(step.log_file)
        return False

    def _load_state_from_workdir(self, announce: bool = True):
        """Infer pipeline progress from files already in the working
        directory and update the step status icons accordingly, as if the
        steps had been run through the GUI. Also repopulates the Import file
        list from imported.expt if present. Non-destructive: it only marks
        steps done where evidence exists; others are left pending."""
        workdir = self.workdir.get()
        if not os.path.isdir(workdir):
            if announce:
                wx.MessageBox(
                    f"Working directory does not exist:\n{workdir}",
                    "Load state",
                    wx.OK | wx.ICON_WARNING,
                )
            return 0

        done = 0
        for s in STEPS:
            if self._step_outputs_present(s):
                self.status[s.id] = "done"
                self.step_buttons[s.id][1].SetLabel(STATUS_ICONS["done"])
                done += 1
            else:
                # don't clobber a 'running' state; otherwise reset to pending
                if self.status.get(s.id) != "running":
                    self.status[s.id] = "pending"
                    self.step_buttons[s.id][1].SetLabel(STATUS_ICONS["pending"])

        # If Import ran, reflect imported.expt as the import 'file' so the
        # Import command preview and downstream defaults make sense. We only
        # set this if the user hasn't already queued specific images.
        if (
            os.path.exists(os.path.join(workdir, "imported.expt"))
            and not self.image_files
        ):
            self.image_files = ["imported.expt"]

        # Refresh the currently-displayed step so its Log/Plots/inputs pick
        # up whatever is now on disk.
        if self.selected_step is not None:
            self.select_step(self.selected_step)

        if announce:
            wx.MessageBox(
                f"Marked {done} step(s) as done based on files in\n{workdir}",
                "Load state",
                wx.OK | wx.ICON_INFORMATION,
            )
        return done

    # ---------------------------------------------------- step display --
    def select_step(self, step: StepDef):
        self.selected_step = step

        # Rebuild the main panel's notebook from scratch for this step.
        self.main_sizer.Clear(delete_windows=True)
        self.notebook = wx.Notebook(self.main_panel)

        setup_tab = wx.Panel(self.notebook)
        output_tab = wx.Panel(self.notebook)
        summary_tab = wx.Panel(self.notebook)
        log_tab = wx.Panel(self.notebook)
        self.notebook.AddPage(setup_tab, "Setup & Run")
        self.notebook.AddPage(output_tab, "Live Output")
        self.notebook.AddPage(summary_tab, "Summary")
        self.notebook.AddPage(log_tab, "Full Log")

        self._build_setup_tab(setup_tab, step)
        self.output_text = self._make_readonly_text(output_tab)
        self.summary_text = self._make_readonly_text(summary_tab)
        self.log_text = self._make_readonly_text(log_tab)

        # Reset per-step plot state first so a step without plots doesn't
        # inherit a stale canvas.
        self.plot_canvas = None
        self.plot_figure = None
        self.plot_status_label = None
        self.progress_bar = None
        self.progress_label = None
        self.plot_page_combo = None
        if step.plot_kind and HAVE_MPL:
            plots_tab = wx.Panel(self.notebook)
            self.notebook.AddPage(plots_tab, "Plots")
            self._build_plots_tab(plots_tab, step)
        elif step.plot_kind and not HAVE_MPL:
            plots_tab = wx.Panel(self.notebook)
            self.notebook.AddPage(plots_tab, "Plots")
            s = wx.BoxSizer(wx.VERTICAL)
            lbl = wx.StaticText(
                plots_tab,
                label=(
                    "Live plots for this step need matplotlib, which isn't "
                    "installed in this environment.\n\nInstall it into your "
                    "DIALS/Python environment (e.g. `pip install matplotlib` "
                    "or `libtbx.pip install matplotlib`) and restart the GUI "
                    "to enable the Plots tab. Everything else — running the "
                    "step, the log Summary, and the 'Run and show report in "
                    "web browser' button — works without it."
                ),
            )
            lbl.Wrap(760)
            s.Add(lbl, 0, wx.ALL, 8)
            plots_tab.SetSizer(s)

        # Full Log tab controls. For the scale step, add a selector to choose
        # WHICH log to show: the plain dials.scale.log (default) or any
        # completed cluster's dials.scale.cluster_N.log. This is independent
        # of the Plots-tab cluster view.
        self.log_cluster_combo = None
        log_ctrl = wx.BoxSizer(wx.HORIZONTAL)
        refresh_log_btn = wx.Button(log_tab, label="Refresh from log file")
        refresh_log_btn.Bind(
            wx.EVT_BUTTON, lambda _e, st=step: self._refresh_log_tab(st)
        )
        log_ctrl.Add(refresh_log_btn, 0, wx.ALL, 2)
        if step.id == "scale":
            result_clusters = self._scale_result_clusters()
            choices = ["dials.scale.log (default)"] + [
                f"cluster_{c}" for c in result_clusters
            ]
            log_ctrl.Add(
                wx.StaticText(log_tab, label="   Log:"),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
                6,
            )
            self.log_cluster_combo = wx.ComboBox(
                log_tab,
                choices=choices,
                value=choices[0],
                style=wx.CB_READONLY,
                size=(200, -1),
            )
            log_ctrl.Add(self.log_cluster_combo, 0, wx.ALL, 2)
            self.log_cluster_combo.Bind(
                wx.EVT_COMBOBOX,
                lambda _e, st=step: self._refresh_log_tab(st),
            )
        # Insert the log controls above the read-only text in the log tab.
        log_sizer = log_tab.GetSizer()
        log_sizer.Insert(0, log_ctrl, 0, wx.EXPAND)
        log_tab.Layout()

        self._refresh_log_tab(step)
        # If a log already exists for this step (e.g. re-selecting a step that
        # ran earlier), populate the plots from it immediately.
        if step.plot_kind and HAVE_MPL:
            self._refresh_plots_from_text(step, self._plot_source_text(step))

        self.main_sizer.Add(self.notebook, 1, wx.EXPAND)
        self.main_panel.Layout()

    def _make_readonly_text(self, parent) -> wx.TextCtrl:
        sizer = parent.GetSizer()
        if sizer is None:
            sizer = wx.BoxSizer(wx.VERTICAL)
            parent.SetSizer(sizer)
        txt = wx.TextCtrl(
            parent,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL,
        )
        txt.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        sizer.Add(txt, 1, wx.EXPAND | wx.ALL, 4)
        return txt

    def _set_text(self, widget: wx.TextCtrl, content: str):
        widget.ChangeValue(content)

    def _append_text(self, widget: wx.TextCtrl, content: str):
        widget.AppendText(content)

    def _build_setup_tab(self, parent, step: StepDef):
        sizer = wx.BoxSizer(wx.VERTICAL)
        parent.SetSizer(sizer)

        help_lbl = wx.StaticText(parent, label=step.help)
        help_lbl.Wrap(760)
        sizer.Add(help_lbl, 0, wx.ALL, 6)

        self.input_vars[step.id] = []
        if step.is_import:
            self._build_import_inputs(parent, sizer, step)
        else:
            for spec in step.inputs:
                row = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(
                    wx.StaticText(parent, label=spec.label, size=(170, -1)),
                    0,
                    wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                    2,
                )
                ctrl = wx.TextCtrl(parent, value=spec.default, size=(360, -1))
                row.Add(ctrl, 1, wx.ALL, 2)
                sizer.Add(row, 0, wx.EXPAND)
                var = _WidgetVar(ctrl)
                self.input_vars[step.id].append(var)
                ctrl.Bind(wx.EVT_TEXT, lambda _e: self._update_command_preview())

        self.field_vars[step.id] = {}

        # Scale step: cluster selector for multi-crystal cluster scaling.
        # If cluster_N.expt/.refl files exist (written by the Correlation
        # Matrix step with 'output clusters' ticked), let the user pick one to
        # scale independently; picking a cluster rewrites the input files and
        # adds distinct output.* names so runs don't overwrite.
        self.scale_cluster_combo = None
        if step.id == "scale":
            clusters = self._available_clusters()
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(
                wx.StaticText(parent, label="Cluster to scale", size=(170, -1)),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                2,
            )
            choices = ["(none - use inputs above)"] + [f"cluster_{c}" for c in clusters]
            self.scale_cluster_combo = wx.ComboBox(
                parent,
                choices=choices,
                value=choices[0],
                style=wx.CB_READONLY,
                size=(220, -1),
            )
            row.Add(self.scale_cluster_combo, 0, wx.ALL, 2)
            if clusters:
                note = (
                    f"{len(clusters)} cluster(s) found: "
                    f"{', '.join(str(c) for c in clusters)}"
                )
            else:
                note = (
                    "(no cluster_N files yet - run Correlation Matrix "
                    "with 'output clusters')"
                )
            note_lbl = wx.StaticText(parent, label=note)
            note_lbl.SetForegroundColour(wx.Colour(128, 128, 128))
            row.Add(note_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            sizer.Add(row, 0, wx.EXPAND | wx.TOP, 4)

            def _on_cluster_change(_e):
                c = self._selected_cluster()
                exp_var, refl_var = self.input_vars["scale"][:2]
                if c is not None:
                    exp_var.set(f"cluster_{c}.expt")
                    refl_var.set(f"cluster_{c}.refl")
                else:
                    exp_var.set("symmetrized.expt")
                    refl_var.set("symmetrized.refl")
                self._update_command_preview()

            self.scale_cluster_combo.Bind(wx.EVT_COMBOBOX, _on_cluster_change)

        if step.extra_fields:
            ph = wx.StaticText(parent, label="Parameters:")
            ph.SetFont(ph.GetFont().Bold())
            sizer.Add(ph, 0, wx.ALL, 4)
        for f in step.extra_fields:
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(
                wx.StaticText(parent, label=f.label, size=(170, -1)),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                2,
            )
            if f.kind == "check":
                checked = str(f.default).strip().lower() in ("true", "1", "yes")
                cb = wx.CheckBox(parent)
                cb.SetValue(checked)
                row.Add(cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 2)
                var = _WidgetVar(cb)
                cb.Bind(wx.EVT_CHECKBOX, lambda _e: self._update_command_preview())
            elif f.kind == "combo":
                cb = wx.ComboBox(
                    parent, value=f.default, choices=f.choices or [], size=(160, -1)
                )
                row.Add(cb, 0, wx.ALL, 2)
                var = _WidgetVar(cb)
                cb.Bind(wx.EVT_COMBOBOX, lambda _e: self._update_command_preview())
                cb.Bind(wx.EVT_TEXT, lambda _e: self._update_command_preview())
            else:
                ctrl = wx.TextCtrl(parent, value=f.default, size=(220, -1))
                row.Add(ctrl, 0, wx.ALL, 2)
                var = _WidgetVar(ctrl)
                ctrl.Bind(wx.EVT_TEXT, lambda _e: self._update_command_preview())
            if f.help:
                hl = wx.StaticText(parent, label=f.help)
                hl.SetForegroundColour(wx.Colour(128, 128, 128))
                row.Add(hl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            sizer.Add(row, 0, wx.EXPAND)
            self.field_vars[step.id][f.key] = var

        sizer.Add(
            wx.StaticText(parent, label="Additional parameters (free text):"),
            0,
            wx.LEFT | wx.TOP,
            6,
        )
        self.extra_params_ctrl = wx.TextCtrl(parent, value="", size=(560, -1))
        self.extra_params_ctrl.Bind(
            wx.EVT_TEXT, lambda _e: self._update_command_preview()
        )
        sizer.Add(self.extra_params_ctrl, 0, wx.LEFT | wx.BOTTOM, 6)

        cph = wx.StaticText(parent, label="Command preview:")
        cph.SetFont(cph.GetFont().Bold())
        sizer.Add(cph, 0, wx.LEFT | wx.TOP, 6)
        self.command_preview = wx.StaticText(parent, label="")
        self.command_preview.SetForegroundColour(wx.BLUE)
        sizer.Add(self.command_preview, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # For correlation_matrix, toggling 'use scaled data' changes both the
        # input files and which HTML/log files the Plots/Log tabs read.
        if step.id == "correlation_matrix":
            us = self.field_vars[step.id].get("use_scaled")
            if us is not None:

                def _on_use_scaled(_e):
                    exp_var, refl_var = self.input_vars["correlation_matrix"][:2]
                    if bool(us.get()):
                        exp_var.set("scaled.expt")
                        refl_var.set("scaled.refl")
                    else:
                        exp_var.set("symmetrized.expt")
                        refl_var.set("symmetrized.refl")
                    self._update_command_preview()
                    self._refresh_log_tab(step)
                    if step.plot_kind and HAVE_MPL:
                        self._refresh_plots_from_text(
                            step, self._plot_source_text(step)
                        )

                us.ctrl.Bind(wx.EVT_CHECKBOX, _on_use_scaled)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.run_button = wx.Button(
            parent,
            label=f"Run {step.program}" + (" (optional)" if step.optional else ""),
        )
        self.run_button.Bind(wx.EVT_BUTTON, lambda _e: self.run_step(step))
        btn_row.Add(self.run_button, 0, wx.ALL, 2)
        self.stop_button = wx.Button(parent, label="Stop")
        self.stop_button.Enable(False)
        self.stop_button.Bind(wx.EVT_BUTTON, lambda _e: self.stop_running())
        btn_row.Add(self.stop_button, 0, wx.ALL, 2)
        self.report_button = wx.Button(
            parent, label="Run and show report in web browser"
        )
        self.report_button.Bind(
            wx.EVT_BUTTON, lambda _e: self.run_and_show_report(step)
        )
        btn_row.Add(self.report_button, 0, wx.ALL, 2)
        sizer.Add(btn_row, 0, wx.TOP, 8)

        self.report_status_label = wx.StaticText(parent, label="")
        self.report_status_label.SetForegroundColour(wx.Colour(128, 128, 128))
        sizer.Add(self.report_status_label, 0, wx.ALL, 4)

        self._update_command_preview()

    def _build_import_inputs(self, parent, sizer, step: StepDef):
        sizer.Add(
            wx.StaticText(parent, label="Image files / master file(s):"),
            0,
            wx.LEFT | wx.TOP,
            4,
        )
        self.import_listbox = wx.ListBox(parent, size=(-1, 100), style=wx.LB_SINGLE)
        for f in self.image_files:
            self.import_listbox.Append(f)
        sizer.Add(self.import_listbox, 0, wx.EXPAND | wx.ALL, 4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        browse = wx.Button(parent, label="Browse files...")
        browse.Bind(wx.EVT_BUTTON, lambda _e: self._browse_images())
        btn_row.Add(browse, 0, wx.ALL, 2)
        addglob = wx.Button(parent, label="Add glob pattern...")
        addglob.Bind(wx.EVT_BUTTON, lambda _e: self._add_glob_pattern())
        btn_row.Add(addglob, 0, wx.ALL, 2)
        clear = wx.Button(parent, label="Clear")
        clear.Bind(wx.EVT_BUTTON, lambda _e: self._clear_images())
        btn_row.Add(clear, 0, wx.ALL, 2)
        sizer.Add(btn_row, 0)

    def _browse_images(self):
        dlg = wx.FileDialog(
            self,
            "Select image / master files",
            defaultDir=self.workdir.get(),
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            for f in dlg.GetPaths():
                self.image_files.append(f)
                if self.import_listbox is not None:
                    self.import_listbox.Append(f)
            self._update_command_preview()
        dlg.Destroy()

    def _add_glob_pattern(self):
        dlg = wx.TextEntryDialog(
            self,
            "Enter a glob pattern (e.g. ../data/CIX*gz or ../data/ins10_?.nxs).\n"
            "The pattern is passed to dials.import as-is (not expanded here), "
            "so it's fine for it to match thousands of images:",
            "Glob pattern",
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        pattern = dlg.GetValue().strip()
        dlg.Destroy()
        if not pattern:
            return
        # Pass the pattern through verbatim - dials.import does its own shell-
        # style expansion, and for large sweeps expanding here would put
        # thousands of paths on the command line (and in the listbox). Just do
        # a quick, non-authoritative count as a sanity hint to the user.
        try:
            n = len(glob.glob(pattern))
        except Exception:
            n = None
        self.image_files.append(pattern)
        hint = "" if n is None else f"  [matches {n} file(s) now]"
        if self.import_listbox is not None:
            self.import_listbox.Append(pattern + hint)
        self._update_command_preview()

    def _clear_images(self):
        self.image_files = []
        if self.import_listbox is not None:
            self.import_listbox.Clear()
        self._update_command_preview()

    # ---------------------------------------------------- command build --
    def _build_command(self, step: StepDef) -> List[str]:
        args: List[str] = []

        cluster = self._selected_cluster()
        # correlation_matrix "use scaled data" pseudo-toggle
        cm_use_scaled = step.id == "correlation_matrix" and bool(
            self.field_vars.get(step.id, {}).get("use_scaled", _FalseVar()).get()
        )

        if step.is_import:
            args.extend(self.image_files)
        else:
            # The input fields are the single source of truth. For cluster
            # scaling the "Cluster to scale" selector has filled them with
            # cluster_N.expt/.refl, and for correlation_matrix the "use scaled
            # data" toggle has set them to scaled.expt/.refl - so we just read
            # the fields here.
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

        if self.extra_params_ctrl is not None:
            extra_text = self.extra_params_ctrl.GetValue().strip()
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
            args.extend(
                [
                    f"output.experiments=scaled_cluster_{cluster}.expt",
                    f"output.reflections=scaled_cluster_{cluster}.refl",
                    f"output.html=dials.scale.cluster_{cluster}.html",
                    f"output.log=dials.scale.cluster_{cluster}.log",
                ]
            )

        # correlation_matrix on scaled data: drop the GUI-only 'use_scaled'
        # pseudo-flag and redirect the HTML/log to '.scaled.' names so this
        # run doesn't overwrite the earlier (post-cosym) correlation matrix.
        if step.id == "correlation_matrix":
            args = [a for a in args if not a.startswith("use_scaled=")]
            if cm_use_scaled:
                args.extend(
                    [
                        "output.html=dials.correlation_matrix.scaled.html",
                        "output.log=dials.correlation_matrix.scaled.log",
                    ]
                )

        return [program] + args

    def _update_command_preview(self):
        step = self.selected_step
        if step is None or self.command_preview is None:
            return
        try:
            cmd = self._build_command(step)
        except Exception:
            return
        self.command_preview.SetLabel(" ".join(cmd))
        self.command_preview.Wrap(880)
        parent = self.command_preview.GetParent()
        if parent is not None:
            parent.Layout()

    # -------------------------------------------------------- run step --
    def run_step(self, step: StepDef):
        if self.runner is not None and self.running_step_id is not None:
            wx.MessageBox(
                "Another step is currently running - please wait or stop it.",
                "Busy",
                wx.OK | wx.ICON_WARNING,
            )
            return

        workdir = self.workdir.get()
        if not os.path.isdir(workdir):
            wx.MessageBox(
                f"{workdir} is not a directory",
                "Invalid directory",
                wx.OK | wx.ICON_ERROR,
            )
            return

        cmd = self._build_command(step)
        if step.is_import and not self.image_files:
            wx.MessageBox(
                "Please add at least one image / master file.",
                "No input files",
                wx.OK | wx.ICON_ERROR,
            )
            return

        self._set_text(self.output_text, "")
        self._append_text(self.output_text, f"$ {' '.join(cmd)}\n\n")
        self._set_text(self.summary_text, "(running...)")

        # Reset live-plot accumulation for this run and clear any stale figure
        # so plots build up fresh as output streams in.
        self.live_output = ""
        self._poll_tick = 0
        if step.plot_kind and HAVE_MPL:
            self._refresh_plots_from_text(step, "")

        self.status[step.id] = "running"
        self.step_buttons[step.id][1].SetLabel(STATUS_ICONS["running"])
        if self.run_button is not None:
            self.run_button.Enable(False)
        if self.stop_button is not None:
            self.stop_button.Enable(True)

        self.runner = ProcessRunner(cmd, workdir)
        self.running_step_id = step.id
        self._running_step = step
        self.runner.start()
        # Poll the runner's queue via a repeating timer (~10 Hz).
        self._timer.Start(100)

    def stop_running(self):
        if self.runner:
            self.runner.terminate()

    def _on_timer(self, _evt):
        step = getattr(self, "_running_step", None)
        if self.runner is None or step is None:
            self._timer.Stop()
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
                    # Flush a final plot update from everything streamed, then
                    # fall through to _finish_step (which also re-reads the
                    # on-disk log for the definitive version).
                    self._timer.Stop()
                    if step.plot_kind and HAVE_MPL:
                        self._refresh_plots_from_text(step, self.live_output)
                    self._finish_step(step, payload)
                    return
        except queue.Empty:
            pass

        # Live plot update, throttled: redraw roughly every ~0.5s worth of
        # polls when new output has arrived, rather than on every single line
        # (integration alone emits thousands of lines).
        if got_line and step.plot_kind and HAVE_MPL:
            self._poll_tick += 1
            if self._poll_tick % 5 == 0:
                self._refresh_plots_from_text(step, self.live_output)

    def _finish_step(self, step: StepDef, returncode: int):
        ok = returncode == 0
        self.status[step.id] = "done" if ok else "failed"
        self.step_buttons[step.id][1].SetLabel(STATUS_ICONS["done" if ok else "failed"])
        if self.run_button is not None:
            self.run_button.Enable(True)
        if self.stop_button is not None:
            self.stop_button.Enable(False)
        self.runner = None
        self.running_step_id = None
        self._running_step = None
        # If a scale cluster run just finished, point the Plots "view cluster"
        # page at it so the results are shown immediately (and the newly-
        # written log is now on disk to build the page list from). Keep the
        # Full Log tab on its current selection (default plain), but refresh
        # that selector's choices so the new cluster is now pickable there too.
        if step.id == "scale" and ok:
            run_cluster = self._selected_cluster()
            if run_cluster is not None and self.plot_page_combo is not None:
                self._select_plot_page(f"cluster_{run_cluster}")
            combo = self.log_cluster_combo
            if combo is not None:
                choices = ["dials.scale.log (default)"] + [
                    f"cluster_{c}" for c in self._scale_result_clusters()
                ]
                cur = combo.GetValue()
                combo.Set(choices)
                combo.SetValue(cur if cur in choices else choices[0])
        self._refresh_log_tab(step)
        # Definitive plot update from the on-disk log (the streamed stdout and
        # the log file should agree, but the log is canonical; the "Summary vs
        # image number" and merging-stats tables in particular are written at
        # the very end).
        if step.plot_kind and HAVE_MPL:
            src = self._plot_source_text(step)
            if step.plot_kind in ("correlation_matrix", "cosym"):
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
        dials.correlation_matrix significant_clusters.output=True) and return
        the sorted list of cluster indices N."""
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
        'Cluster to scale' selector (what to RUN next), or None if 'none' /
        not applicable."""
        combo = self.scale_cluster_combo
        if combo is None:
            return None
        val = combo.GetValue()
        m = re.match(r"cluster_(\d+)$", val or "")
        return int(m.group(1)) if m else None

    def _scale_result_clusters(self) -> List[int]:
        """Cluster indices that already have a scale result on disk
        (dials.scale.cluster_N.log). These are the clusters whose results can
        be VIEWED in the Plots / Full Log tabs, independent of which cluster
        is queued to run."""
        workdir = self.workdir.get()
        out = []
        try:
            for name in os.listdir(workdir):
                m = re.match(r"dials\.scale\.cluster_(\d+)\.log$", name)
                if m:
                    out.append(int(m.group(1)))
        except OSError:
            return []
        return sorted(out)

    def _scale_view_cluster(self) -> Optional[int]:
        """Which cluster's *results* the scale Plots/Log tabs should show,
        taken from the Plots-tab page selector. 'plain' or 'all' -> None (the
        non-cluster dials.scale.log). This is separate from _selected_cluster
        (the run target) so you can review cluster 0's results while cluster 1
        is queued to run."""
        page = self._current_plot_page()
        m = re.match(r"cluster_(\d+)$", page or "")
        return int(m.group(1)) if m else None

    def _scale_log_name(self) -> str:
        """The scale log filename to READ for the Plots / Full Log tabs.

        Prefers the Plots-tab 'view cluster' selection (so completed clusters
        can be reviewed while another is queued). Falls back to the run-target
        cluster (useful mid-run before the page list is built), then to the
        plain dials.scale.log."""
        view = self._scale_view_cluster()
        if view is not None:
            return f"dials.scale.cluster_{view}.log"
        run = self._selected_cluster()
        page = self._current_plot_page()
        if run is not None and page in (None, "", "all", "plain"):
            if page != "plain":
                return f"dials.scale.cluster_{run}.log"
        return "dials.scale.log"

    def _scale_log_view_name(self) -> str:
        """The scale log filename the FULL LOG tab should show, from its own
        'Log:' selector (default dials.scale.log). Independent of the Plots
        tab's cluster view."""
        combo = self.log_cluster_combo
        if combo is not None:
            m = re.match(r"cluster_(\d+)$", combo.GetValue() or "")
            if m:
                return f"dials.scale.cluster_{m.group(1)}.log"
        return "dials.scale.log"

    def _read_workdir_file(self, name: str) -> str:
        """Read a file from the working directory by name. '' if absent."""
        path = os.path.join(self.workdir.get(), name)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def _corrmat_html_text(self) -> str:
        """Read the correlation-matrix HTML from the working directory (the
        source for the correlation_matrix Plots tab). If 'use scaled data' is
        ticked, read the '.scaled.' variant this GUI writes for post-scaling
        runs; otherwise the default. '' if absent."""
        use_scaled = bool(
            self.field_vars.get("correlation_matrix", {})
            .get("use_scaled", _FalseVar())
            .get()
        )
        name = (
            "dials.correlation_matrix.scaled.html"
            if use_scaled
            else "dials.correlation_matrix.html"
        )
        return self._read_workdir_file(name)

    def _cosym_html_text(self) -> str:
        """Read dials.cosym.html from the working directory (the source for
        the cosym Plots tab, which carries the Plotly JSON blobs). '' if
        absent."""
        return self._read_workdir_file("dials.cosym.html")

    def _plot_source_text(self, step: StepDef) -> str:
        """The text a step's Plots tab should parse: correlation_matrix and
        cosym plot from their HTML output (which carries the Plotly JSON
        blobs), every other plot step from its .log file."""
        if step.plot_kind == "correlation_matrix":
            return self._corrmat_html_text()
        if step.plot_kind == "cosym":
            return self._cosym_html_text()
        return self._current_log_text(step)

    def _corrmat_log_name(self) -> str:
        """The log filename dials.correlation_matrix writes given the current
        'use scaled data' selection."""
        use_scaled = bool(
            self.field_vars.get("correlation_matrix", {})
            .get("use_scaled", _FalseVar())
            .get()
        )
        return (
            "dials.correlation_matrix.scaled.log"
            if use_scaled
            else "dials.correlation_matrix.log"
        )

    def _current_log_text(self, step: StepDef) -> str:
        """Read back the on-disk log for this step (respecting the dynamic
        merge/export log-name choice and cluster scaling). Returns '' if not
        present."""
        log_name = step.log_file
        if step.dynamic and step.id == "merge_export":
            mode = self.field_vars.get(step.id, {}).get("mode")
            mode_val = mode.get() if mode else "merge"
            log_name = "dials.export.log" if mode_val == "export" else "dials.merge.log"
        elif step.id == "scale":
            log_name = self._scale_log_name()
        elif step.id == "correlation_matrix":
            log_name = self._corrmat_log_name()
        if not log_name:
            return ""
        return self._read_workdir_file(log_name)

    def _refresh_log_tab(self, step: StepDef):
        log_name = step.log_file
        if step.dynamic and step.id == "merge_export":
            mode = self.field_vars.get(step.id, {}).get("mode")
            mode_val = mode.get() if mode else "merge"
            log_name = "dials.export.log" if mode_val == "export" else "dials.merge.log"
        elif step.id == "scale":
            # The Full Log tab has its own 'Log:' selector (default
            # dials.scale.log); it is independent of the Plots-tab cluster view.
            log_name = self._scale_log_view_name()
        elif step.id == "correlation_matrix":
            log_name = self._corrmat_log_name()

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

        if self.log_text is not None:
            self._set_text(self.log_text, text)
        if self.summary_text is not None:
            self._set_text(self.summary_text, summarise_log(text))

    # ------------------------------------------------------------ plots --
    def _build_plots_tab(self, parent, step: StepDef):
        """Build the Plots tab for a plot-capable step: an embedded
        matplotlib canvas (with the standard navigation toolbar), a status
        line, an optional page selector (per data set for find_spots/refine/
        integrate in multi-crystal mode, per cluster for scale), and — for
        integration — a live block-processing progress bar."""
        sizer = wx.BoxSizer(wx.VERTICAL)
        parent.SetSizer(sizer)

        top = wx.BoxSizer(wx.HORIZONTAL)
        self.plot_status_label = wx.StaticText(
            parent,
            label=(
                "(no data yet - run this step, or plots will fill in "
                "live as it runs)"
            ),
        )
        self.plot_status_label.SetForegroundColour(wx.Colour(128, 128, 128))
        top.Add(self.plot_status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        refresh_label = (
            "Refresh plots from HTML"
            if step.plot_kind in ("correlation_matrix", "cosym")
            else "Refresh plots from log"
        )
        refresh_btn = wx.Button(parent, label=refresh_label)
        refresh_btn.Bind(
            wx.EVT_BUTTON,
            lambda _e: self._refresh_plots_from_text(
                step, self._plot_source_text(step)
            ),
        )
        top.Add(refresh_btn, 0, wx.ALL, 2)
        sizer.Add(top, 0, wx.EXPAND)

        # Page selector: for steps that can span multiple data sets or
        # clusters, a combobox to flip between one page of plots each. Rebuilt/
        # populated lazily as data arrives (see _update_plot_pages).
        self.plot_page_combo = None
        if step.plot_kind in ("find_spots", "refine", "integrate", "scale"):
            page_row = wx.BoxSizer(wx.HORIZONTAL)
            label = "Cluster" if step.plot_kind == "scale" else "Data set"
            page_row.Add(
                wx.StaticText(parent, label=f"{label}:", size=(70, -1)),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                2,
            )
            self.plot_page_combo = wx.ComboBox(
                parent,
                choices=["all"],
                value="all",
                style=wx.CB_READONLY,
                size=(180, -1),
            )
            page_row.Add(self.plot_page_combo, 0, wx.ALL, 2)

            def _on_page_change(_e, s=step):
                # Use the canonical source for the current page. For scale,
                # _plot_scale reads the selected cluster's log itself (pass "").
                # For others, prefer the live stream only while THIS step is
                # actively running; otherwise read the on-disk log, so
                # reviewing a completed step still parses correctly.
                if s.plot_kind == "scale":
                    self._refresh_plots_from_text(s, "")
                    self._refresh_log_tab(s)
                else:
                    running = self.running_step_id == s.id
                    src = (
                        self.live_output
                        if running and self.live_output
                        else self._plot_source_text(s)
                    )
                    self._refresh_plots_from_text(s, src)

            self.plot_page_combo.Bind(wx.EVT_COMBOBOX, _on_page_change)

            if step.plot_kind == "scale":
                hint = wx.StaticText(
                    parent, label="(pick a completed cluster to view its results)"
                )
                hint.SetForegroundColour(wx.Colour(128, 128, 128))
                page_row.Add(hint, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            sizer.Add(page_row, 0, wx.EXPAND | wx.TOP, 2)

        # Integration and multi-crystal indexing get a live progress bar.
        if step.plot_kind in ("integrate", "index"):
            prog_row = wx.BoxSizer(wx.HORIZONTAL)
            initial = (
                "Blocks: waiting..."
                if step.plot_kind == "integrate"
                else "Indexing: waiting..."
            )
            self.progress_label = wx.StaticText(parent, label=initial, size=(280, -1))
            prog_row.Add(self.progress_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 2)
            self.progress_bar = wx.Gauge(parent, range=100, size=(400, -1))
            prog_row.Add(self.progress_bar, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 6)
            sizer.Add(prog_row, 0, wx.EXPAND | wx.TOP, 4)

        self.plot_figure = Figure(figsize=(7.5, 5.0), dpi=100)
        self.plot_canvas = FigureCanvas(parent, -1, self.plot_figure)
        sizer.Add(self.plot_canvas, 1, wx.EXPAND | wx.ALL, 2)
        toolbar = NavigationToolbar(self.plot_canvas)
        toolbar.Realize()
        sizer.Add(toolbar, 0, wx.EXPAND)
        self.plot_canvas.draw()

    def _update_plot_pages(self, options: List[str]):
        """Refresh the page-selector combobox's choices, preserving the
        current selection if still valid. `options` is e.g. ['all','0',
        '1',...]. No-op if there's no selector or the options are unchanged."""
        combo = self.plot_page_combo
        if combo is None:
            return
        if list(combo.GetStrings()) == options:
            return
        cur = combo.GetValue()
        combo.Set(options)
        combo.SetValue(cur if cur in options else (options[0] if options else "all"))

    def _select_plot_page(self, value: str):
        combo = self.plot_page_combo
        if combo is None:
            return
        if value in combo.GetStrings():
            combo.SetValue(value)

    def _current_plot_page(self) -> str:
        combo = self.plot_page_combo
        return combo.GetValue() if combo is not None else "all"

    def _refresh_plots_from_text(self, step: StepDef, text: str):
        """Re-parse `text` for this step and redraw the figure. Safe to call
        repeatedly (live) and with partial/empty text. Dispatches on
        step.plot_kind."""
        if not HAVE_MPL or self.plot_figure is None or self.plot_canvas is None:
            return
        if self.selected_step is None or self.selected_step.id != step.id:
            return  # user navigated away; don't draw onto another step's tab

        kind = step.plot_kind
        try:
            if kind == "find_spots":
                self._plot_find_spots(text)
            elif kind == "index":
                self._plot_index(text)
            elif kind == "refine":
                self._plot_refine(text)
            elif kind == "integrate":
                self._plot_integrate(text)
            elif kind == "scale":
                self._plot_scale(text)
            elif kind == "correlation_matrix":
                self._plot_correlation_matrix(text)
            elif kind == "cosym":
                self._plot_cosym(text)
        except Exception as exc:  # pragma: no cover - defensive redraw guard
            self._set_plot_status(f"(plot error: {exc})")
            return
        self.plot_canvas.draw_idle()

    def _set_plot_status(self, msg: str):
        if self.plot_status_label is not None:
            self.plot_status_label.SetLabel(msg)
            self.plot_status_label.Wrap(700)
            parent = self.plot_status_label.GetParent()
            if parent is not None:
                parent.Layout()

    def _set_progress(self, fraction: Optional[float], label: str):
        """Update the wx.Gauge (0..100) and its label. fraction None -> 0."""
        if self.progress_bar is not None:
            val = 0 if fraction is None else int(max(0.0, min(1.0, fraction)) * 100)
            self.progress_bar.SetValue(val)
        if self.progress_label is not None:
            self.progress_label.SetLabel(label)

    def _plot_index(self, text: str):
        """Index step: for multi-crystal indexing (joint=false) DIALS prints
        'Indexing imageset id <id> (k/N)' as it works through the imagesets.
        Drive a progress bar from the (k/N) count (reliable). There's no
        per-image line graph for indexing, so the figure just carries a
        short explanatory note; the progress bar is the real content."""
        prog = parse_index_progress(text)

        if self.progress_bar is not None and self.progress_label is not None:
            if prog is not None and prog["total"] > 0:
                self._set_progress(
                    prog["done"] / prog["total"],
                    f"Indexing imageset {prog['imageset_id']} "
                    f"({prog['done']}/{prog['total']})",
                )
            else:
                self._set_progress(
                    0,
                    "Indexing: waiting... (per-imageset progress appears for "
                    "multi-crystal joint=false runs)",
                )

        fig = self.plot_figure
        fig.clear()
        ax = fig.add_subplot(111)
        ax.axis("off")
        if prog is not None and prog["total"] > 0:
            frac = prog["done"] / prog["total"]
            msg = (
                f"Indexing multiple crystals\n\n"
                f"{prog['done']} / {prog['total']} imagesets started "
                f"({frac*100:.0f}%)\n\n"
                f"most recent: imageset id {prog['imageset_id']}"
            )
            self._set_plot_status(f"indexing {prog['done']}/{prog['total']} imagesets")
        else:
            msg = (
                "Indexing.\n\nFor multiple crystals (joint=false), a progress "
                "bar tracks the\n'Indexing imageset id <id> (k/N)' output "
                "above.\n\nFor a single crystal there is no per-imageset "
                "progress;\ncheck the Summary / Full Log tabs for the result."
            )
            self._set_plot_status("(no multi-crystal indexing progress yet)")
        ax.text(
            0.5, 0.5, msg, ha="center", va="center", fontsize=11, transform=ax.transAxes
        )
        fig.tight_layout()

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
        # and each imageset is a separate line (the legend carries the
        # imageset number from the 'Finding strong spots in imageset N'
        # banner). With more imagesets than the default colour cycle (10),
        # colours would repeat, so we draw from a larger colormap and cycle
        # marker shapes too, keeping every line visually distinct.
        import numpy as _np

        try:
            import matplotlib as _mpl

            # matplotlib.colormaps (>=3.5) replaces the deprecated
            # matplotlib.cm.get_cmap; fall back for very old versions.
            try:
                cmap = _mpl.colormaps["tab20"]
            except (AttributeError, KeyError):
                import matplotlib.cm as _cm

                cmap = _cm.get_cmap("tab20")
        except Exception:
            cmap = None
        markers = [".", "o", "s", "^", "v", "D", "x", "+", "*", "<", ">", "p"]

        n = len(nonempty)
        labelled = 0
        for i, s in enumerate(nonempty):
            iset = s["imageset"]
            label = f"imageset {iset}" if iset is not None else "imageset"
            color = cmap(i % 20) if cmap is not None else None
            marker = (
                markers[(i // 20) % len(markers)]
                if n > 20
                else markers[i % len(markers)]
            )
            ax.plot(
                s["image"],
                s["pixels"],
                marker=marker,
                markersize=3,
                linewidth=1,
                color=color,
                label=label,
            )
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

        # Parse every "Refinement steps" table. Single-crystal refine emits
        # one (or a few macrocycle) table(s); multi-crystal joint=false
        # refine emits one per experiment/run. We show the RMSD-vs-step
        # CONVERGENCE for each run (not just the final RMSDs), and the page
        # selector picks which run. This is the fix for "only shows final
        # RMSD per experiment": each run's full convergence is available.
        tables = parse_all_refine_steps(text)

        if not tables:
            self._update_plot_pages(["all"])
            ax = fig.add_subplot(111)
            self._set_plot_status("(waiting for the 'Refinement steps' table...)")
            ax.set_title("Refinement RMSDs vs step")
            ax.set_xlabel("Refinement step")
            fig.tight_layout()
            return

        # One page per refinement run. Label by the original experiment id
        # from the "Selected group..." marker when available ("run 1 (id
        # 0)"), else just "run k". Single-crystal refine has one run.
        def _label(k, tbl):
            ids = tbl.get("ids", "")
            return f"run {k + 1} (id {ids})" if ids != "" else f"run {k + 1}"

        page_labels = [_label(k, t) for k, t in enumerate(tables)]
        self._update_plot_pages(page_labels)
        page = self._current_plot_page()
        # Map the selected page label back to a table index.
        sel = 0
        if page in page_labels:
            sel = page_labels.index(page)
        data = tables[sel]

        steps = data["step"]
        # RMSD_X/Y are a length (mm for a single sweep, px for multi-crystal
        # refine); RMSD_Phi/Z is angular (deg) or images. Two positional on
        # the left axis, the third on a secondary right axis.
        ax = fig.add_subplot(111)
        ax.plot(steps, data["rmsd_x"], marker="o", label="RMSD_X")
        ax.plot(steps, data["rmsd_y"], marker="s", label="RMSD_Y")
        ax.set_xlabel("Refinement step")
        ax.set_ylabel("Positional RMSD (mm or px)")
        ax.grid(True, alpha=0.3)

        ax2 = ax.twinx()
        ax2.plot(
            steps, data["rmsd_phi"], marker="^", color="tab:green", label="RMSD_Phi/Z"
        )
        ax2.set_ylabel("Angular RMSD (deg) / RMSD_Z (images)")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)

        if len(tables) > 1:
            ax.set_title(f"Refinement RMSDs vs step - run {sel + 1} of {len(tables)}")
            self._set_plot_status(
                f"run {sel + 1}/{len(tables)}: {len(steps)} refinement steps "
                f"(use the Data set selector to switch run)"
            )
        else:
            ax.set_title("Refinement RMSDs vs step")
            self._set_plot_status(f"{len(steps)} refinement steps")
        fig.tight_layout()

    def _plot_integrate(self, text: str):
        blocks = parse_integrate_blocks(text)
        progress = parse_integrate_progress(text)

        # --- live progress bar (block processing) ---
        if self.progress_bar is not None and self.progress_label is not None:
            n_blocks = len(blocks)
            if n_blocks:
                # The block loop runs twice (profile modelling, then
                # integration). Show progress within the current pass.
                done = progress["completed"]
                pass_no = 1 if done <= n_blocks else 2
                in_pass = done if done <= n_blocks else done - n_blocks
                in_pass = min(in_pass, n_blocks)
                label = (
                    f"Pass {pass_no}/2 - block {in_pass}/{n_blocks}"
                    if done
                    else f"Blocks: 0/{n_blocks}"
                )
                if progress["last_to"]:
                    label += (
                        f"  (frames {progress['last_from']} -> {progress['last_to']})"
                    )
                self._set_progress(in_pass / n_blocks, label)
            else:
                self._set_progress(0, "Blocks: waiting for block table...")

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
            ds_note = f"  |  data set {sel} of {len(ds_ids)}" if len(ds_ids) > 1 else ""
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

        self._set_plot_status(f"Integration summary over {len(img)} images{ds_note}")
        fig.tight_layout()

    def _plot_scale(self, text: str):
        # Build the "view cluster" page list from clusters that have a scale
        # result on disk, so completed clusters can be reviewed even while a
        # different cluster is queued to run. Pages: "plain" (the
        # non-cluster dials.scale.log, if present) plus "cluster_N" for each
        # dials.scale.cluster_N.log found. If nothing cluster-specific
        # exists yet, fall back to a single "all" page using the text passed
        # in (covers the live-run and single-crystal cases).
        result_clusters = self._scale_result_clusters()
        plain_exists = os.path.exists(
            os.path.join(self.workdir.get(), "dials.scale.log")
        )
        pages: List[str] = []
        if plain_exists:
            pages.append("plain")
        pages.extend(f"cluster_{c}" for c in result_clusters)

        if pages:
            self._update_plot_pages(pages)
            # Read the log for the currently-selected view page (this is what
            # lets you flip between completed clusters' results).
            view = self._scale_view_cluster()
            if view is not None:
                src = self._read_workdir_file(f"dials.scale.cluster_{view}.log")
                cluster_note = f"  |  cluster {view}"
            else:
                src = self._read_workdir_file("dials.scale.log")
                cluster_note = "  |  (unclustered scale)"
            # Prefer freshly-streamed text if it clearly contains the merging
            # table and the on-disk log doesn't yet (mid-run).
            if (
                "Merging statistics by resolution bin" in (text or "")
                and "Merging statistics by resolution bin" not in src
            ):
                src = text
            text = src
        else:
            self._update_plot_pages(["all"])
            cluster = self._selected_cluster()
            cluster_note = f"  |  cluster {cluster}" if cluster is not None else ""

        data = parse_scale_merging(text)
        fig = self.plot_figure
        fig.clear()
        inv = data["inv_d2"]  # type: ignore[assignment]
        if not inv:
            ax = fig.add_subplot(111)
            self._set_plot_status(
                "(waiting for 'Merging statistics by resolution bin'..."
                + (cluster_note + ")" if cluster_note else ")")
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
        ax4.plot(
            inv,
            data["completeness"],
            marker=".",
            color="tab:green",  # type: ignore[index]
            label="Completeness (%)",
            linewidth=1,
        )
        ax4.set_ylabel("Completeness (%)", fontsize=8)
        ax4b = ax4.twinx()
        ax4b.plot(
            inv,
            data["mult"],
            marker=".",
            color="tab:orange",  # type: ignore[index]
            label="Multiplicity",
            linewidth=1,
        )
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
                ax.bar(
                    xy["x"],
                    xy["y"],
                    width=(xy["x"][1] - xy["x"][0]) * 0.9 if len(xy["x"]) > 1 else 0.02,
                )
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
            panels.append(
                lambda ax: draw_clusters(
                    ax, reach, "OPTICS reachability", scatter=False
                )
            )
        if coords is not None:
            panels.append(
                lambda ax: draw_clusters(
                    ax, coords, "Cosym PCA coordinates", scatter=True
                )
            )
        if dims is not None:
            panels.append(lambda ax: draw_xy(ax, dims, logy=True))
        if rij is not None:
            panels.append(lambda ax: draw_xy(ax, rij, logy=False))

        n = len(panels)
        if n == 0:
            ax = fig.add_subplot(111)
            ax.set_title("No recognised correlation-matrix graphs found", fontsize=9)
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

    def _plot_cosym(self, text: str):
        """Plot the diagnostics embedded in dials.cosym.html.

        Like _plot_correlation_matrix, the source here is the HTML file
        (passed in as `text`), not a .log - it carries the same Plotly JSON
        blobs, which we parse and re-render with matplotlib. Shows: the cosym
        coordinate scatter (Axis 0 vs Axis 1), the Rij-matrix histogram, the
        unit-cell parameter scatter (a/b/c pairs) and histogram, and the
        unit-cell clustering dendrogram. The page selector isn't used here
        (it's a fixed multi-panel view, same as correlation_matrix)."""
        self._update_plot_pages(["all"])
        fig = self.plot_figure
        fig.clear()

        graphs = extract_corrmat_graphs(text) if text else {}
        if not graphs:
            ax = fig.add_subplot(111)
            self._set_plot_status(
                "(run dials.cosym, or use 'Refresh plots from HTML' - reads "
                "dials.cosym.html)"
            )
            ax.set_title("Cosym analysis (pending)")
            fig.tight_layout()
            return

        coords = graphs.get("graphs_cosym_coordinates")
        rij = graphs.get("graphs_cosym_rij_histogram")
        uc_scatter = graphs.get("graphs_uc_scatter")
        uc_hist = graphs.get("graphs_uc_hist")
        uc_clustering = graphs.get("graphs_uc_clustering")

        def _pairs(s):
            # drop index-aligned points where either coordinate failed to parse
            xs, ys = [], []
            for x, y in zip(s["x"], s["y"]):
                if x is not None and y is not None:
                    xs.append(x)
                    ys.append(y)
            return xs, ys

        def draw_coords(ax):
            for s in cosym_scatter_series(coords):
                xs, ys = _pairs(s)
                ax.scatter(xs, ys, s=4, alpha=0.5, label=s["name"])
            ax.set_title("Cosym coordinates", fontsize=9)
            ax.set_xlabel("Axis 0", fontsize=7)
            ax.set_ylabel("Axis 1", fontsize=7)
            ax.tick_params(labelsize=6)

        def draw_rij(ax):
            xy = corrmat_xy(rij)
            if xy is None:
                return
            xs = xy["x"]
            width = (xs[1] - xs[0]) * 0.9 if len(xs) > 1 else 0.02
            ax.bar(xs, xy["y"], width=width)
            ax.set_title(xy["title"] or "Rij histogram", fontsize=9)
            ax.set_xlabel(xy["xtitle"] or "r_ij", fontsize=7)
            ax.tick_params(labelsize=6)
            ax.grid(True, alpha=0.3)

        def draw_uc_scatter(ax):
            for s in cosym_scatter_series(uc_scatter):
                xs, ys = _pairs(s)
                ax.scatter(xs, ys, s=6, label=s["name"])
            ax.set_title("Unit cell parameters", fontsize=9)
            ax.set_xlabel("Å", fontsize=7)
            ax.tick_params(labelsize=6)
            ax.legend(fontsize=6)

        def draw_uc_hist(ax):
            for s in cosym_hist_series(uc_hist):
                if s["values"]:
                    ax.hist(s["values"], bins=20, alpha=0.5, label=s["name"])
            ax.set_title("Unit cell distribution", fontsize=9)
            ax.set_xlabel("Å", fontsize=7)
            ax.set_ylabel("Frequency", fontsize=7)
            ax.tick_params(labelsize=6)
            ax.legend(fontsize=6)

        def draw_dendrogram(ax):
            for seg in cosym_dendrogram(uc_clustering):
                xs, ys = _pairs(seg)
                if xs:
                    ax.plot(xs, ys, linewidth=0.8, color="steelblue")
            ax.set_title("Unit cell clustering", fontsize=9)
            ax.set_xlabel("Dataset", fontsize=7)
            ax.set_ylabel("Distance (Å²)", fontsize=7)
            ax.tick_params(labelsize=6)

        panels = []
        if coords is not None:
            panels.append(draw_coords)
        if rij is not None:
            panels.append(draw_rij)
        if uc_scatter is not None:
            panels.append(draw_uc_scatter)
        if uc_hist is not None:
            panels.append(draw_uc_hist)
        if uc_clustering is not None:
            panels.append(draw_dendrogram)

        n = len(panels)
        if n == 0:
            ax = fig.add_subplot(111)
            ax.set_title("No recognised cosym graphs found", fontsize=9)
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

        self._set_plot_status(f"cosym: {n} graphs from HTML")
        fig.tight_layout()

    # --------------------------------------------------------- dials.report --
    def _report_files_for_step(self, step: StepDef) -> List[str]:
        """Work out which .expt/.refl files best represent the result of this
        stage, to hand to `dials.report`. Prefers the stage's own freshly-
        written outputs; falls back to pairing a single new output with the
        other file type from the current input fields; falls back again to the
        step's current inputs for stages (like Bravais lattice determination or
        Merge/Export) that don't themselves write a fresh .expt/.refl pair."""

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
            wx.MessageBox(
                f"{workdir} is not a directory",
                "Invalid directory",
                wx.OK | wx.ICON_ERROR,
            )
            return
        if shutil.which("dials.report") is None:
            wx.MessageBox(
                "dials.report was not found on $PATH.",
                "Not found",
                wx.OK | wx.ICON_ERROR,
            )
            return

        files = self._report_files_for_step(step)
        if not files:
            wx.MessageBox(
                "No experiment/reflection files are set for this step yet - "
                "fill in the input fields above (or run the step first).",
                "No files",
                wx.OK | wx.ICON_WARNING,
            )
            return

        cmd = ["dials.report"] + files
        self.report_button.Enable(False)
        self.report_status_label.SetLabel(f"Running: {' '.join(cmd)} ...")

        def worker():
            try:
                result = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True
                )
            except Exception as exc:
                wx.CallAfter(
                    self._report_finished,
                    step,
                    False,
                    f"Could not launch dials.report: {exc}",
                )
                return

            html_path = os.path.join(workdir, "dials.report.html")
            if result.returncode != 0 or not os.path.exists(html_path):
                tail = (
                    (result.stdout or "")[-1500:] + "\n" + (result.stderr or "")[-1500:]
                )
                wx.CallAfter(
                    self._report_finished,
                    step,
                    False,
                    f"dials.report exited with code {result.returncode}:\n{tail}",
                )
                return

            wx.CallAfter(self._report_finished, step, True, html_path)

        threading.Thread(target=worker, daemon=True).start()

    def _report_finished(self, step: StepDef, ok: bool, detail: str):
        if self.report_button is not None:
            self.report_button.Enable(True)
        if ok:
            if self.report_status_label is not None:
                self.report_status_label.SetLabel(
                    f"Opened {detail} in your web browser."
                )
            webbrowser.open(f"file://{detail}")
        else:
            if self.report_status_label is not None:
                self.report_status_label.SetLabel(
                    "dials.report failed - see error dialog."
                )
            wx.MessageBox(detail, "dials.report failed", wx.OK | wx.ICON_ERROR)

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

        dialog = wx.Dialog(self, title=f"Launch {program}", size=(560, -1))
        dsizer = wx.BoxSizer(wx.VERTICAL)
        ctrls = []
        for label, default in zip(arg_labels, defaults):
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(
                wx.StaticText(dialog, label=label, size=(220, -1)),
                0,
                wx.ALIGN_CENTER_VERTICAL | wx.ALL,
                4,
            )
            ctrl = wx.TextCtrl(dialog, value=default, size=(300, -1))
            row.Add(ctrl, 1, wx.ALL, 4)
            dsizer.Add(row, 0, wx.EXPAND)
            ctrls.append(ctrl)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        launch_btn = wx.Button(dialog, wx.ID_OK, label="Launch")
        cancel_btn = wx.Button(dialog, wx.ID_CANCEL, label="Cancel")
        btn_row.Add(launch_btn, 0, wx.ALL, 4)
        btn_row.Add(cancel_btn, 0, wx.ALL, 4)
        dsizer.Add(btn_row, 0, wx.ALIGN_LEFT)
        dialog.SetSizerAndFit(dsizer)

        if dialog.ShowModal() == wx.ID_OK:
            args = [c.GetValue().strip() for c in ctrls if c.GetValue().strip()]
            cmd = [program] + args
            workdir = self.workdir.get()
            if shutil.which(program) is None:
                wx.MessageBox(
                    f"{program} was not found on $PATH.",
                    "Not found",
                    wx.OK | wx.ICON_ERROR,
                )
                dialog.Destroy()
                return
            try:
                subprocess.Popen(cmd, cwd=workdir)
            except Exception as exc:
                wx.MessageBox(str(exc), "Error launching tool", wx.OK | wx.ICON_ERROR)
                dialog.Destroy()
                return
            if program == "dials.report":
                # dials.report writes dials.report.html into the cwd; give it
                # a moment then try to open it in a browser.
                def _open_report():
                    html_path = os.path.join(workdir, "dials.report.html")
                    if os.path.exists(html_path):
                        webbrowser.open(f"file://{html_path}")

                wx.CallLater(4000, _open_report)
        dialog.Destroy()
