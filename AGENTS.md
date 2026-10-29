# Notes for AI agents working in this repo

## Tests: targeted only -- never the full suite

**Never run the whole test suite** (`pytest`, `pytest tests`, `pytest --slow`
over everything, or `build.py` without `--no-test`). It takes on the order of
fifteen minutes and is not something to run on a change.

Run only the test files that cover what you touched, by name:

    python -m pytest tests/test_search.py -q
    python -m pytest tests/test_gui_search.py -q --slow     # GUI tests are marked slow

* Pick files by area (a change to `gui.py` -> the `test_gui_*` / `test_grid_*`
  files that exercise what you changed, not all of them).
* `--slow` is for the specific files you name, never as a blanket flag.
* GUI tests run offscreen: `QT_QPA_PLATFORM=offscreen`.
* A test that drives a whole flash write over a mock CAN/TP 2.0 ECU records
  the inter-frame waits instead of spending them (see `_skip_frame_waits` in
  `tests/test_tp20_transport.py`, about 10 s with `--slow`); a write paced
  for real takes minutes. Do the same in a new test of that kind.
* If a failure is in an area you did not touch, say so and leave it; do not go
  looking for more failures by running everything.

Other agents may be editing files at the same time. Check `git status` before
editing a shared file such as `ovtsuite/gui.py`, keep edits to it small, and put
new features in their own modules.

## Versions

**Version numbers are raised only for a release** -- never per change or per
feature. A batch of work going into one release raises each version once.

* **The app** (`__version__` in `ovtsuite/branding.py`): raised by 0.0.1 for a
  release (0.3.0 -> 0.3.1). Change only that line; `build.ps1` rewrites
  `packaging/version_info.txt` from it (keep the copy in the repo in step when
  you touch it).
* **Each module** has its own version in `ovtsuite/<id>/module.py`,
  independent of the app's (stable modules 1.x, the rest 0.x). Raise it for a
  release when that module's files changed since its last release; the build
  warns when they changed and the version did not. See `docs/MODULES_PLAN.md`.
* **Stored definitions** (`MODEL_VERSION` in `ovtsuite/defn.py`) is not a
  release number, so the rule above does not apply: bump it when
  a change alters how a session's model is built or restored, so files and
  projects carrying a stored definition rebuild it. Editing a file listed in
  `defn.MODEL_FILES` fails `tests/test_defn.py` until its recorded hash is
  updated -- decide then whether `MODEL_VERSION` needs the bump too.

## Help pages

Every user-facing feature has a help page: the base app's in
`ovtsuite/data/help/`, a module's in `ovtsuite/<module>/help/` (raise the
module's version when they change). A page's `context:` names the dialog
classes F1 opens it for. See `docs/HELP.md`; `tests/test_helpdocs.py` checks
links and contexts.

## Table and cell colours: match the app standard

Every new window or dialog with a table must draw cells and selection the
way the main map editor does, using the shared helpers in
`ovtsuite/gui_cellmarks.py` (read its docstring): alternating row colours so
the selection is the solid accent colour, `CellDelegate` / `selection_text`
so a selected cell's text stays legible with and without focus, in both
themes, the **orange outline** for "differs from the
comparison" (`mark_differs`, drawn by the delegate -- `CellDelegate` or
`OutlineDelegate`, or `draw_outline` in a delegate of your own; a model
answers `OUTLINE_ROLE`; never an orange *fill*, so it lies over a heat map,
the direction tint and the pending yellow), `PENDING_BG` for
changed-not-applied. Checkboxes and other
indicators must stay clearly visible on a selected row in both themes.
Where a window shows a comparison table beside the primary, selecting cells
in the primary selects the same cells in the comparison -- all of them, not
only the current one. (The one exception is the table editor's comparison at
its original size when the two sizes differ: then nothing is linked and every
cell is outlined -- `ovtsuite/gui_cmpsize.py`.) Do not invent a window-local selection style.
