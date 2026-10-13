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
* If a failure is in an area you did not touch, say so and leave it; do not go
  looking for more failures by running everything.

Other agents may be editing files at the same time. Check `git status` before
editing a shared file such as `ovtsuite/gui.py`, keep edits to it small, and put
new features in their own modules.

## Versions

* **The app** (`__version__` in `ovtsuite/branding.py`): every major feature
  addition raises it by 0.0.1 (0.3.0 -> 0.3.1). Change only that line;
  `build.ps1` rewrites `packaging/version_info.txt` from it (keep the copy in
  the repo in step when you touch it).
* **Each module** has its own version in `ovtsuite/<id>/module.py`,
  independent of the app's (stable modules 1.x, the rest 0.x). Raise it
  whenever that module's files change; the build warns when they changed and
  the version did not. See `docs/MODULES_PLAN.md`.

## Help pages

Every user-facing feature has a help page: the base app's in
`ovtsuite/data/help/`, a module's in `ovtsuite/<module>/help/` (raise the
module's version when they change). A page's `context:` names the dialog
classes F1 opens it for. See `docs/HELP.md`; `tests/test_helpdocs.py` checks
links and contexts.
