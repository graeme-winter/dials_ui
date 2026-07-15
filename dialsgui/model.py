"""Declarative step / field definitions (dataclasses). No GUI dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ExtraField:
    """A single extra command-line parameter exposed in the GUI."""

    key: str
    label: str
    kind: str = "entry"  # "entry", "check", "combo"
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
