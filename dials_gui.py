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

The implementation lives in the `dialsgui/` package alongside this file;
this module is a thin launcher so `python3 dials_gui.py` keeps working.
See `dialsgui/frame.py` for the GUI and `dialsgui/parsers.py` for the
pure log-parsing logic.

Requirements
------------
Python 3.8+ with wxPython (install with `pip install wxPython`, or
`libtbx.pip install wxPython` inside a DIALS/cctbx environment — DIALS
already ships wxPython for its own viewers, so it is usually present in a
sourced DIALS environment). DIALS itself must already be installed and
set up in the environment the GUI is launched from (i.e. `dials.import`
etc. must be on $PATH) - this GUI is a front end, not a replacement, for
that installation.

Run with:

    python3 dials_gui.py

"""

from dialsgui.app import main

if __name__ == "__main__":
    main()
