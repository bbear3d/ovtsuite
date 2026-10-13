> **Status: implemented** (items 1-3 and the in-container definition block).
> The shipped design is in `docs/CONTAINERS.md` ("Definition block") and
> `ovtsuite/defn.py`. The text below is the original scoping and is kept
> for the open item: a stock-image hash index that skips matching for
> known factory binaries.

# Definition cache: skipping identification for known files (scoping, not implemented)

Status: **proposal** -- nothing here is built. Written 2026-09-30 alongside the
tree/load speed-ups (see "Already done" at the bottom).

## Problem

Opening an EDC16 image runs `detect_family` (the full matcher against every
family, ~3.5 s of a ~4.2 s load on a 2 MB image) and then the matcher's result is
turned into ~10k entities. For a file we have already identified once -- most
often one of our own `.edc16` saves, or an untouched factory image -- all of
that is recomputed from scratch.

## Two mechanisms, deliberately separate

### A. Definition record inside `.edc16` (any file we saved)

`.edc16` is a directory of typed blocks; readers skip unknown types
(`edc16_container.py`, "forward-friendly"). Add one block, e.g. `BLOCK_DEFN = 8`,
name `defn`, UTF-8 JSON (or compact binary), **no change to VERSION**:

| field | meaning |
|---|---|
| `family` | final family *after* sub-family refinement (`_DATASET_ID_SUBFAMILY`) |
| `region` | `[start, end]` calibration region from `find_cal_region` |
| `kb_digest` | hash of the family's `.json.gz` + `.supplement.json` + matcher version constant |
| `image_len` | length of the external block |
| `placements` | optional: per KB table/constant, `target_addr`, `source`, `note` |
| `anchors` | optional: the `OffsetModel` anchors, to rebuild `model` |

Load path: parse container -> read `defn` -> check `kb_digest` matches the
installed KB and `image_len` matches -> **skip `find_cal_region`,
`detect_family` and `match()`**. Without `placements` this still removes the
~3.5 s identification; with them it removes the matcher too.

Validity -- the important part. Edited *cell values* do not move tables, but a
stale record must never place a table wrongly, so before trusting `placements`
verify cheaply and format-defined (no heuristics): for each placed map/curve,
`try_map`/`try_curve` at the recorded header still parses with the same
dimensions (the same check `_build_entities` already makes). Any failure, any
digest mismatch, or any unknown field -> ignore the record and run the normal
path. The record is advisory; the bytes remain the authority.

Writing: `TunerSession.save()` already repacks the container; it would emit
`defn` from the session's own `family`/`region`/`result`. A plain `.bin` can be
upgraded the first time it is saved as `.edc16`.

Cost: placements for ~10k entities are on the order of 100-300 KB of JSON before
the container's zlib; `history`-style text block precedent already exists.

Open decisions for Bear:
1. Store placements, or only family+region? (placements = fastest, bigger file,
   more invalidation surface)
2. Should `defn` be written automatically on every `.edc16` save, or only when
   the file is first created?
3. Does a MPC/EEPROM-only container need one? (probably not; external block only)

### B. Stock-image index (factory files we hold copies of)

Ship `data/stock_index.json`: `sha256(full external image)` -> `{family,
subfamily, region, label}` for factory images only (the 313-file SGO batch
catalogue is the natural source; `tools/catalogue_sgo*.py` already walk it).
On load, hash the image (2 MB, ~5 ms) and look it up; a hit gives the same
`family`/`region` the record above would, and skips identification.

Limits found while checking real files (`Reference/Binaries`):
* **Only exact-stock matches.** Hashing "everything outside the calibration
  region" does NOT identify tunes: the three LQ variants (`LQ`, `acstart016_patch`,
  `accode16_patch`) share a calibration hash (`9240c6a9`) but differ in the code
  area, because patches live outside the cal region. So tunes use mechanism A,
  not B.
* Stock hits can also carry a placement table, but that doubles the data
  shipped; start with family+region only.
* The index must be generated, not hand-edited: add a `tools/generate_stock_index.py`
  that runs the full path once per file and records the answer, plus a test that
  re-identifies every indexed image from scratch and asserts the shortcut agrees.

## Safety rules (apply to both)

* The shortcut may only *skip work*; it may never change the answer. A test
  suite check runs both paths over every reference binary and `assert`s identical
  entities (names, addresses, sources).
* Any mismatch falls back silently to the full path.
* Matcher/KB changes bump the digest so old records stop applying automatically.

## Suggested order

1. Reuse-the-winner + KB cache (done) -- already trims repeat work.
2. Mechanism A with family+region only, behind the digest/length check.
3. Equivalence test across all reference binaries.
4. Mechanism B stock index (generated).
5. Optional: `placements`/`anchors` in A.

## Already done (2026-09-30)

* Translator gate (`signal_names._STRONG_HINT_LC`) + memoized `translate_german`
  and `_parse_desc` -- tree labels/tooltips ~6.6 s -> ~0.8 s cold.
* Tree is built once per file; tier box / text filter / "show only parameters
  that differ" hide rows (~0.1 s). Tooltips are built on hover.
* `detect_family(want_result=True)` hands the winner's `MatchResult` to the
  session (no second matcher pass); `kb.load_cached` parses each family once.
