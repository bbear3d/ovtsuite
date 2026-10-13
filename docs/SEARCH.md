# Find panel

**Edit ▸ Find…** (`Ctrl+F`, `Cmd+F` on macOS) opens a dock at the bottom of the window. **F3 / Shift+F3** step through
the results. One panel serves every tab.

The panel keeps everything -- query, options, results, the selected row -- when
you collapse it (▾) or close it (✕ / `Esc`) and open it again. Queries per
mode, options, scope and history are also remembered between runs.

## What it searches

| Mode | Query | Notes |
|---|---|---|
| **Parameters** | words in any order (`boost limit`) | live as you type; ranked |
| **Values** | `1500`, `10..20`, `>=5`, `!=0`, `nan` | Enter; display units or the raw stored value; optional ± tolerance; axes optional |
| **Address** | `0x1A2B3C`, `1A2B3C-1A2BFF`, `0x1A2B3C+16` | live; an address *inside* a table resolves to the exact cell or axis element |
| **Bytes in parameter** | hex with `??` wildcards, ASCII, integer, float | looks only inside the table currently shown; whole-image byte search is the hex editor's job |

**Options ▾** (parameter searches): fuzzy / exact / regular expression, match
case, whole words (an underscore separates words, so `InjCrv` is a whole word
in `InjCrv_tPreTrbn_C`), which fields to search (symbol, name, category/tier,
description, units), and a tier filter.

**Scope**: this tab (default), this tab + its comparison, or all open tabs.

## Using a result

* Selecting a row (click, ↑/↓, Enter, F3) selects the parameter in the tree and
  shows it in the preview table tab. For value, address and byte searches the
  matching cells are highlighted and scrolled into view.
* **Double-click, Ctrl+Enter or right-click ▸ Open in new table tab** pins it
  in its own table tab.
* A hit in another tab switches to that tab.
* **Enter** in the query box runs the search, then walks the results
  (Shift+Enter goes back).

## How it is built

* `ovtsuite/search.py` -- the engine. Qt-free; matching and ranking, an address
  index built from each entity's placed extents (Z data, X axis, Y axis),
  value and byte scans, cancel tokens, history.
* `ovtsuite/gui_search.py` -- the dock. Searches run on a worker thread; a newer
  search cancels the older one.
* `ovtsuite/data/search_index.json.gz` -- a label cache built from the shipped
  definition files and the reference EDC15 / ZF 6HP images by
  `tools/build_search_index.py` (also run by `build.py`). Use `--cache DIR
  --budget SECONDS` to build in resumable chunks.
  It stores the English tree label for each definition, so a file's search text
  does not need the translation pass (~0.4 s on a 10,000-parameter image). It
  records the digest of the data and of `gui.py` / the translators; if either
  has changed it is ignored and labels are made the normal way -- slower, never
  wrong. Rebuild with `python tools/build_search_index.py` (`--check` reports
  whether it is current).

The address search uses only the extents the model already knows. A table's own
header (the count words in front of its axes) is not part of an entity, so an
address that falls in one resolves to "nothing covers it" and the panel says
how far the nearest parameter is.
