"""Background subprocess runner and small wx-control value adapters.

The .get()/.set() adapters let the ported command-building / plot-source
helpers read GUI field values through a uniform interface (a hold-over from
the original Tk StringVar/BooleanVar API).
"""

from __future__ import annotations

import queue
import subprocess
import threading
from typing import List, Optional


class _WidgetVar:
    """Adapter exposing `.get()` / `.set()` over a wx control that has
    GetValue/SetValue (TextCtrl, ComboBox, CheckBox)."""

    def __init__(self, ctrl, cast=lambda v: v):
        self.ctrl = ctrl
        self._cast = cast

    def get(self):
        return self._cast(self.ctrl.GetValue())

    def set(self, value):
        self.ctrl.SetValue(value)


class _FalseVar:
    """Stand-in for a variable that always reports False / empty - used as a
    safe default when a named field may not exist yet."""

    def get(self):
        return False


class ProcessRunner:
    """Runs a command in a background thread, streaming stdout lines to a
    queue so the wx main loop can poll it without blocking."""

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
