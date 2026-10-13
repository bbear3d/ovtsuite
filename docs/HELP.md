# Help pages and the website

One set of Markdown pages is both the in-app help (Help > Help contents, F1,
the standalone tools' Help button) and the help website. `ovtsuite/helpdocs.py`
reads them; `ovtsuite/gui_help.py` shows them; `tools/build_wiki.py` builds the
site.

## Where pages go

| Pages | Folder | Page ids |
|---|---|---|
| The base app's | `ovtsuite/data/help/` | `flashing`, `getting-started`, ... |
| A module's | `ovtsuite/<module>/help/` | `modules/<module>/index`, `modules/<module>/...` |

A module's `help/` folder is inside its own package, so the module build packs
it into the `.ovtmod`, the module's signature covers it, and installing or
removing the module adds or removes its pages. **Every module needs a
`help/index.md`** (tests/test_helpdocs.py checks); it is the module's entry in
the contents. Changing a module's pages changes the module: raise its version.

The in-app help shows the base pages and the **installed** modules' pages; the
website shows every module's.

## Writing a page

```markdown
---
title: ECU read / write
order: 30
context: FlashDialog, PairHeadUnitDialog
---
# ECU read / write

Text, **bold**, lists, tables, `code`, and [links](cables.md).
```

- `title` -- in the contents (default: the first `# heading`).
- `order` -- position among its siblings (default 100; lower first).
- `context` -- the window/dialog/panel **class names** the page documents. F1
  in one of those (or a subclass) opens the page; the innermost one around
  the keyboard focus wins, so a docked panel can have its own page. Modules name their own dialogs
  here; nothing else needs registering. The tests check every name is a real
  class.

Keep to plain Markdown -- headings, paragraphs, **bold**/*italic*, lists,
tables, code, block quotes, links and images. That subset looks the same in
the app (Qt's Markdown) and on the site (MkDocs). No HTML, no scripts.

### Links and images

Link to other pages **relatively, with the `.md`**, exactly as the files sit
in the merged tree: from `flashing.md`, `[cables](cables.md)`; from the
modules page, `[EDC16](modules/edc16/index.md)`; from a module page back to a
base page, `[logging](../../logging.md)`. The tests fail on a link that leads
nowhere.

A link to a page of a module that is not installed shows "this page comes with
the X module" in the app, so base pages may link to any module's pages.

Images go beside the page (`help/images/foo.png`) and are linked relatively.

## The website (WordPress)

```
pip install "mkdocs>=1.6,<2" "mkdocs-material>=9.6,<10"
python tools/build_wiki.py --url https://<your site>/ovt-suite/help
python tools/build_wiki.py --serve          # preview at http://127.0.0.1:8000
```

The build writes a folder of static files (`build/wiki/site`) and a zip of it
in `dist/`. Upload the zip's contents into a folder of the WordPress site, e.g.
`public_html/ovt-suite/help/`, with the host's file manager or SFTP. WordPress
leaves real files and folders alone, so the site is served from there beside
the WordPress pages; add the address to a WordPress menu. Then put the same
address in `ovtsuite/data/help/help.json` (`online_url`) so the help window
shows **Open online**.

This needs file access to the web host (any self-hosted WordPress has it).
WordPress.com plans without SFTP cannot host it; there, publish the folder on
a subdomain or any static host and link to it.

## Screenshots

Pictures in the pages are generated from the real dialogs, never captured by
hand. Each `images/<id>.png` has a *recipe* in `tools/help_shots/<area>.py`:

```python
from help_screenshots import shot

@shot("flash-edc16", "data/help", size=(1100, 360), what="The flasher set to EDC16")
def flash_edc16(ctx):
    ...build the dialog in the state to show...
    return dialog
```

`ctx.main_window(ctx.ref("edc16_lq"))` opens the main window on a reference
file (named in `tools/help_shots/refs.json`, read from `Reference/`; a recipe
whose file is missing is skipped). As in the app, the file is imported into a
project -- named after the car in `Context.PROJECTS`, saved in a temporary
folder -- and its tab starts out compared with the original;
`branch="Stage 1"` forks a branch to work on, `comparison=` imports another
binary as the comparison instead. `ctx.open_project(window, path)` adds
another project's tab, `ctx.project(path)` just makes the `.ovt` (for the
standalone tools). Pop-ups are answered automatically, and a passing status
message is cleared before the picture is taken.

```
python tools/help_screenshots.py                  # all of them
python tools/help_screenshots.py flash-edc16      # one
python tools/help_screenshots.py --check          # every page image has a recipe
.\build.ps1 -HelpShots                            # regenerate before freezing
```

Run it on the machine whose look the help should have (Windows for the
Windows build): windows are rendered with the platform's own style, off
screen. Images are saved as 256-colour PNGs to keep the installers small.
Base-app pages in sub-folders use `../images/...`, so every base image lives
in `ovtsuite/data/help/images/`.
