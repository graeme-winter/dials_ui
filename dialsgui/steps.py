"""The processing pipeline (STEPS), viewer tools (TOOLS), status icons."""

from typing import List

from .model import ExtraField, InputSpec, StepDef

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
            ExtraField(
                "image_range",
                "Image range (start,end)",
                "entry",
                help="e.g. 1,1200 - leave blank to use all images",
            ),
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
            ExtraField(
                "joint",
                "multi-crystal (joint=false)",
                "check",
                check_value="false",
                help="index many crystals independently in one run",
            ),
            ExtraField("space_group", "space_group", "entry"),
            ExtraField(
                "unit_cell", "unit_cell", "entry", help="e.g. 78,78,78,90,90,90"
            ),
            ExtraField(
                "max_lattices",
                "max_lattices",
                "entry",
                help="set to 2 (say) if % indexed is low and a "
                "second lattice is suspected",
            ),
        ],
        outputs=["indexed.expt", "indexed.refl"],
        log_file="dials.index.log",
        plot_kind="index",
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
            ExtraField(
                "prediction.d_min",
                "prediction.d_min",
                "entry",
                help="optional resolution limit, e.g. 1.8",
            ),
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
            "For MULTIPLE crystals: measure the pairwise similarity of the "
            "data sets and cluster the isomorphous ones using the OPTICS "
            "algorithm, writing dials.correlation_matrix.html (with the "
            "correlation / cos-angle matrices, dendrograms and cluster "
            "assignments). Normally run after Cosym on symmetrized data; you "
            "can ALSO run it after Scale by ticking 'use scaled data' (which "
            "switches the inputs to scaled.expt/.refl and writes to "
            "dials.correlation_matrix.scaled.html so it doesn't overwrite the "
            "earlier run). 'output clusters' (on by default) writes "
            "cluster_0.expt/.refl, cluster_1.expt/.refl, ... which can then "
            "be scaled independently (see Scale). The Plots tab visualises "
            "the matrices, reachability and cluster coordinates from the HTML."
        ),
        inputs=[
            InputSpec("Experiment file", "symmetrized.expt"),
            InputSpec("Reflection file", "symmetrized.refl"),
        ],
        extra_fields=[
            ExtraField(
                "use_scaled",
                "use scaled data (run after scaling)",
                "check",
                help="switch inputs to scaled.expt/.refl and write to "
                "dials.correlation_matrix.scaled.html",
            ),
            ExtraField(
                "significant_clusters.output",
                "output clusters (write cluster_N.expt/.refl)",
                "check",
                default="True",
                help="on by default; needed to scale clusters separately",
            ),
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
            ExtraField(
                "absorption_level",
                "absorption_level",
                "combo",
                choices=["", "low", "medium", "high"],
                help="low (~1%, default), medium (~5%), high (~25%)",
            ),
            ExtraField(
                "d_min",
                "d_min",
                "entry",
                help="optional resolution cutoff from CC-half fit",
            ),
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
            ExtraField(
                "mode", "mode", "combo", default="merge", choices=["merge", "export"]
            ),
            ExtraField(
                "d_min",
                "d_min",
                "entry",
                help="optional resolution cutoff suggested by scaling",
            ),
        ],
        outputs=[],
        log_file="",
        dynamic=True,
    ),
]


TOOLS = [
    ("dials.show", "dials.show", ["Experiment / reflection file(s)"]),
    (
        "dials.image_viewer",
        "dials.image_viewer",
        ["Experiment file", "Reflection file (optional)"],
    ),
    (
        "dials.reciprocal_lattice_viewer",
        "dials.reciprocal_lattice_viewer",
        ["Experiment file", "Reflection file"],
    ),
    ("dials.report", "dials.report", ["Experiment file", "Reflection file"]),
]


STATUS_ICONS = {
    "pending": "\u2b1c",  # white square
    "running": "\u23f3",  # hourglass
    "done": "\u2705",  # check mark
    "failed": "\u274c",  # cross mark
}
