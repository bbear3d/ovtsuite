# The `.ovt` project container

Status: **specification + core implementation** (`ovtsuite/project/`).
Session/GUI wiring is the next phase -- see "Rollout" at the end.

One `.ovt` file replaces the per-platform native containers (`.edc16`,
`.edc15`, `.6hp`, `.8hp`, `.hvac`, `.vdo`) and the `.log` sidecars. It is a
**project**, not a binary format: it holds every binary of a car (or a bench
job) together with its history, branches, original delivery containers,
definitions, datalogs and attachments.

Parsing lives in the **base app** (`ovtsuite/project/`, no module imports), so
the standalone Flasher / Logger / Sniffer can open a project with no modules
installed. Modules are only needed to *edit* a member with full support
(definitions, checksums) -- exactly as for any other file.

---

## 1. Concepts

```
Project
 ├── Set "ECU"            (platform edc16, module edc16 1.0)
 │    ├── members:  flash, mpc, eeprom        (the regions of one control unit)
 │    ├── history:  a tree of checkpoints (root = the as-read original)
 │    ├── branches: Stock, Stage 1, Economy   (named heads in that tree)
 │    ├── originals: the .dim / .sgo / .frf it was imported from (lossless)
 │    └── definitions: packaged XDF / A2L / KP / OLS / DAM for raw members
 ├── Set "TCU"            (platform zf6hp)
 │    └── ...
 ├── Logs        (datalogs, optional cap, oldest unpinned deleted first)
 └── Attachments (dyno sheets, notes, anything)
```

* **Set** -- one control unit (ECU, TCU, cluster, HVAC, other). Has a
  `kind` (badge/tint in the UI) and a `platform` from the base catalogue.
* **Member** -- one memory region of that unit: `flash` (the main image,
  whatever the platform calls it), `mpc`, `eeprom`, `boot`, `program`, `cal`,
  `loader`, or `raw`. A member's description is **structural**: role, base
  address, endianness, word size, platform, and the module id + version that
  produced it. On reopen these fields are trusted; nothing is re-detected.
* **Checkpoint** -- one immutable point in a set's history: a delta against
  its parent covering every member at once. Kinds: `read` (from the car /
  bench), `import` (from a file), `save`, `patch`, `revert`, `merge`.
* **Root** -- a checkpoint with no parent. Its full image is stored. The
  first root of a set is normally the **as-read original**; it can never be
  modified or deleted, and every comparison against "stock" is just a
  reconstruction of it. There is no separate stock binary.
* **Branch** -- a named pointer to a head checkpoint. A branch covers all
  members of the set together, so Stage 1 flash and Stage 1 EEPROM cannot
  drift apart. Branches fork from any checkpoint. A branch may also start
  from its **own independent root** (e.g. a stock SGO imported next to a read
  of an already-tuned car).

### Saving rules

* There is no direct save to a raw `.bin` any more. Opening a raw or legacy
  file creates an untitled project; the first Save asks for an `.ovt` path.
* Every Save of a branch is a new checkpoint on it. Reverting creates a new
  checkpoint whose content equals the older one -- history is append-only.
* Raw binary / original-container export is always available per member,
  for any branch or checkpoint.

---

## 2. Physical format

A ZIP archive (deflate), readable natively by Python (`zipfile`) and .NET
(`System.IO.Compression`). Renaming it to `.zip` lets anyone inspect it.

### 2.1 Structural identification

Like ODF, the **first entry** is `mimetype`, **stored** (uncompressed), with
the exact content `application/x-ovt-project`. So a file is an `.ovt` iff:

| offset | bytes |
|---|---|
| 0  | `50 4B 03 04` (local file header) |
| 8  | compression method `00 00` (stored) |
| 26 | file-name length = 8 |
| 30 | `mimetype` |
| 30+8+extra | `application/x-ovt-project` |

This is a fixed-offset structural test, not an extension or content guess
(`ovtsuite.project.is_project`).

### 2.2 Entries

| path | content |
|---|---|
| `mimetype` | see above |
| `manifest.json` | everything structural (section 3) |
| `integrity.json` | SHA-256 of `manifest.json`, optional signature (section 5) |
| `sets/<set>/images/<ckpt>/<member>.bin` | full image of a member at a root checkpoint |
| `sets/<set>/deltas/<ckpt>.ovd` | the delta of a non-root checkpoint (section 4) |
| `sets/<set>/originals/<id>/<filename>` | an original container, byte-for-byte |
| `sets/<set>/defs/<filename>` | a packaged definition (XDF / A2L / KP / OLS / DAM) |
| `sets/<set>/cache/<branch>.defn` | optional definition snapshot cache (advisory, `ovtsuite.defn`) |
| `logs/<id>/<filename>` | a datalog |
| `attachments/<id>/<filename>` | any attachment |

IDs are short lowercase hex strings (`uuid4().hex[:10]`); filenames inside an
id directory keep the user's original name. Full images are stored **only at
roots** (normally the as-read original); every other state, branch heads
included, is rebuilt from its root through the deltas (section 4.2). Deltas
are small, so this costs milliseconds to open and one image per root on disk.

---

## 3. `manifest.json`

```json
{
  "format": "ovt-project",
  "version": 1,
  "id": "9f3c2a71b0",
  "requires": ["sets-v1", "delta-v1"],
  "app": "OVT-Suite 0.3.2",
  "name": "Bear's A6 C6 BHW",
  "created": "2026-10-11T02:40:00",
  "modified": "2026-10-11T03:10:00",
  "settings": {"max_logs": 20},
  "info": {"make": "Audi", "model": "A6", "chassis": "4F", "year": "2006",
           "vin": "WAUZZZ4F06N000001", "engine": "BRE", "notes": "..."},
  "sets": [{
    "id": "a1b2c3d4e5",
    "name": "ECU",
    "kind": "ecu",
    "platform": "edc16",
    "identity": {"part": "03G906016LQ", "software": "..."},
    "active_branch": "f00dfeed01",
    "members": [{
      "id": "flash",
      "role": "flash",
      "name": "External flash",
      "platform": "edc16",
      "module": {"id": "edc16", "version": "1.0"},
      "base": 0, "endian": "big", "word": 1,
      "definition": null,
      "meta": {}
    }],
    "checkpoints": [{
      "id": "c0ffee0001", "parent": null, "kind": "read",
      "ts": "2026-10-11T02:40:00", "label": "Read from car",
      "source": {"tool": "OVT Flasher", "interface": "OpenPort 2.0"},
      "sizes": {"flash": 2097152, "eeprom": 4096},
      "n_bytes": 0
    }],
    "branches": [{"id": "f00dfeed01", "name": "Stock", "head": "c0ffee0001",
                  "created": "...", "note": ""}],
    "originals": [{"id": "...", "filename": "03G906016LQ.sgo", "format": "sgo",
                   "checkpoint": "c0ffee0001", "members": ["flash"]}]
  }],
  "logs": [{"id": "...", "filename": "2026-10-11_0300.csv", "added": "...",
            "set": "a1b2c3d4e5", "pinned": false, "note": ""}],
  "attachments": [{"id": "...", "filename": "dyno.pdf", "added": "...",
                   "set": null, "note": ""}],
  "entries": {"sets/a1b2c3d4e5/images/c0ffee0001/flash.bin":
              {"sha256": "...", "size": 2097152}}
}
```

### Project information

`info` describes what the project is for: an object of strings, empty ones
left out. The app knows `make`, `model`, `chassis`, `year`, `vin`, `engine`,
`gearbox`, `power`, `odometer`, `ecu`, `customer` and `notes`
(`model.INFO_FIELDS`); any other key (a later version's, or one an import
brought in such as WinOLS's `Fuel`) is kept and shown under its own name.
Edited in File > Project information... and asked for when a new project is
first saved.

### Member fields

| field | meaning |
|---|---|
| `role` | `flash` `mpc` `eeprom` `boot` `program` `cal` `loader` `raw` |
| `base` | logical/VMA base address of byte 0 (MPC 0x400000, etc.) |
| `endian`, `word` | byte order and word size for a raw member's hex view / definition |
| `platform` | base catalogue id; `unknown` for an unrecognised binary |
| `module` | id + version of the module that wrote it (empty for raw) -- lets the base app name the missing module without re-identifying |
| `definition` | `{"path": "sets/<set>/defs/x.xdf", "type": "xdf"}` -- a packaged third-party definition. A raw member **with** a definition opens in the main parameter view; **without** one it opens in the hex editor |
| `meta` | platform facts with no other home (the old `BLOCK_META`: HVAC part number and section map, VDO ROM/coding, 8HP block map) |

A member written by a module that is not installed is opened read-only in the
hex editor and carried through every save byte-for-byte; checksum status is
**always recomputed by the module**, never trusted from the file.

---

## 4. History

### 4.1 Delta format (`.ovd`)

Little-endian. A delta records, per member, the bytes that changed between a
checkpoint's parent and the checkpoint -- **both** the old and the new bytes,
so it can be walked in either direction.

```
magic   4s   "OVD1"
count   u16  number of records
record:
  mid_len u8, member id (ASCII)
  op      u8   1 = patch, 2 = replace
  op 1 (patch: same length before and after)
     offset u32, length u32, old[length], new[length]
  op 2 (replace: the member changed size, appeared or disappeared)
     old_len u32, old[old_len], new_len u32, new[new_len]
     a length of 0xFFFFFFFF means "absent" (no bytes follow)
```

Adjacent changed ranges closer than 16 bytes are merged into one patch record.

### 4.2 Reconstruction

The checkpoints form a tree (parent pointers); full images exist at the
roots. To get the state at checkpoint *C* the reader finds the nearest
checkpoint along tree edges with an intact full image (its root, in a file
this build wrote) and applies the deltas along that path -- `new` bytes
walking parent -> child, `old` bytes walking child -> parent (so a full image
elsewhere in the tree, as an extension or an older writer may leave, is used
too). Each applied patch is checked: the bytes being replaced must equal the
delta's recorded "from" side, so a damaged delta is detected rather than
silently applied. States are cached once rebuilt.

### 4.3 Migration from the old logs

`VERSION` records from a legacy container's `history` block (or a `.log`
sidecar) carry `[start, old_hex, new_hex]` ranges for the main image and a
`regions` map for MPC/EEPROM. The importer walks them newest -> oldest from the
file's current bytes, recovering every earlier state, and rebuilds them as a
checkpoint chain whose root is the state before the first recorded save. What
did not become a checkpoint -- `PATCH` records, free-text entries, any record
that did not fit -- is kept as an attachment (`legacy-history.log`); the
migrated `VERSION` records are not duplicated there.

---

## 5. Integrity and signing

* `manifest.entries` lists the SHA-256 and size of every other entry
  (including entries the reader does not understand).
* `integrity.json` = `{"manifest_sha256": "...", "signature": null | {...}}`.
* On load every entry is hashed. A mismatch marks that entry **damaged** in
  the load report; reconstruction never uses damaged images/deltas (4.2).
  A damaged root image, or a damaged delta, makes the states that depend on
  it unreadable, reported per checkpoint -- every other state, set, log and
  attachment still opens. Re-saving keeps a damaged entry flagged as damaged
  (it is never laundered into a fresh "good" hash).
* **Signing is optional.** `signature` = `{"alg": "ed25519", "key": "<public
  hex>", "signer": "name", "sig": "<hex>"}` over the exact `manifest.json`
  bytes (which already contain every entry's hash). Verifying reports
  *valid / invalid / unsigned* and the key fingerprint; whether a key is
  *trusted* is the app's decision (a user key list), not the file's. Same
  Ed25519 code as module signing (`cryptography` package).

---

## 6. Forward compatibility

PNG-style critical / ancillary split:

* `requires` lists **critical** features. A reader that does not implement
  every one refuses the file with a clear message ("needs a newer
  OVT-Suite: feature X"). Version 1 readers know `sets-v1`, `delta-v1`.
* Everything else is **ancillary**: unknown keys on any object are kept and
  written back unchanged; unknown archive entries are kept byte-for-byte.
* New *optional* features add keys/entries without touching `requires`.
  New *mandatory* semantics add a feature name to `requires`.

---

## 7. Logs and attachments

* `settings.max_logs` (0 / absent = unlimited). Adding a log beyond the cap
  deletes the **oldest unpinned** logs first. A **pinned** log is never
  auto-deleted and does not count toward the cap.
* The cap never touches history checkpoints or attachments.
* A log or attachment may be tied to a set (`set`) or project-wide (`null`).

---

## 8. Import / export

| source | becomes |
|---|---|
| `.edc16` `.edc15` `.6hp` `.8hp` `.hvac` `.vdo` | one set; blocks -> members (`EXTERNAL`->`flash`, `MPC`->`mpc`, `EEPROM`->`eeprom`, `LOADER`->`loader`, `BOOT/PROGRAM/CAL`); `META` -> member meta; `history` block -> checkpoint chain (4.3); `defn` -> cache |
| `.dim` | set with `flash` (chunk 2), `mpc` (chunk 1), `eeprom` (chunk 3); original embedded |
| `.sgo` | set with the flat image as `flash` (+ `mpc` when an internal-flash section decodes); original embedded |
| `.frf` | set with the calibration image as `flash`; original embedded (read-only format) |
| `.bin` / anything else | set with one `flash` member (`raw` when unrecognised); a `.log` sidecar's `VERSION` records are migrated |
| `.ols` (WinOLS project) | set with its embedded image as `flash` (an EDC16 image WinOLS assembled from flash + MPC is split by its own segment table into `flash` + `mpc`); the `.ols` packaged as the definition (type `ols`); root labelled with the WinOLS version it holds; the WinOLS vehicle / ECU details fill empty `info` fields |
| `.xdf` `.a2l` `.kp` `.ols` `.dam` alongside a raw | packaged definition on that member (type from the file's content) |
| `.xdf` `.a2l` `.kp` `.dam` alone | refused: a definition holds no binary (the GUI asks for the binary, or attaches it to the open set) |

Export, per member and per branch/checkpoint:

* **raw bytes** (`.bin`) -- always;
* **original format** -- when the set has an original of that member:
  unchanged content returns the original bytes exactly; changed content is
  rebuilt for `.dim` (untouched chunks carried through) and `.sgo` (only
  changed sections re-encoded); `.frf` has no writer and is raw-only;
* **legacy native container** (`.edc16` etc.) for older builds.

---

## 9. Crash recovery (auto-save)

`ovtsuite/project/autosave.py`. While a project is open the app keeps a
**recovery entry** in the user's config directory
(`%APPDATA%\OVT-Suite\recovery\<id>\`, `~/.config/ovtsuite/recovery/<id>/`):

| file | content |
|---|---|
| `lock` | held with an OS file lock for as long as the owning app runs |
| `entry.json` | project path/name, app version, time, and per open branch: set, branch, the saved head checkpoint it is relative to, delta file |
| `w_<set>_<branch>.ovd` | the working bytes of that branch as a delta (section 4.1) against its saved head |
| `base.ovt` | a full copy of the project, only while it has unsaved *structural* changes (never saved, new branches/checkpoints, ...) |

* **No checksum correction.** A snapshot freezes the working bytes exactly as
  edited and writes them on a background thread; the GUI thread only copies
  references. Checksums are corrected by the normal Save, as always.
* Snapshots are periodic and skipped when nothing changed (revision token).
* After a successful Save the entry is deleted (nothing is unsaved).
* **On a proper exit the entry is deleted whether the user saved or refused
  to save.** Only a crash (or a kill) leaves one behind.
* At startup, entries whose lock can be taken (owner gone -- never a live
  instance) are offered: **Recover** opens the project with the edits loaded
  as *unsaved* changes, **Discard** deletes the entry. If the saved `.ovt`
  changed on disk after the autosave, recovery still applies each delta to
  the head checkpoint it was made against, and says so.

### 9.1 Changes made by other programs

`ovtsuite/project/disksync.py` (no Qt), `ovtsuite/gui_disksync.py`. A
project loaded from, or saved to, a file remembers that file's size, time
and sha256, plus its manifest and blobs as they were then (the *base*).

* `Project.save` refuses (`DiskChanged`) to write over that file when its
  content changed or it is gone, unless `force=True`. A save to any other
  path is not checked. The check runs after the bytes are built, just before
  the atomic write.
* `disksync.merge(mine, theirs)` is a three-way merge against the base. It
  works record by record, by id: sets, members, checkpoints, branches,
  originals, logs, attachments and patches, plus the fields of `info`,
  `settings` and unknown keys.
  * A checkpoint's content (parent, sizes, log, delta) comes whole from one
    side; only its label is merged by field.
  * A branch both sides moved keeps the other side's head, and ours becomes
    a new branch, `<name> (mine)`.
  * Each blob follows the record it belongs to. A definition cache that
    both sides changed is taken from the other side, because it is advisory.
  * A checkpoint whose parent the other side merged away is re-parented to
    its nearest surviving ancestor, with its delta recomputed.
  * A field both sides changed differently is a `Conflict`, resolved by the
    caller's choices.
* In the GUI, every save of an open project goes through `gui_disksync.write`
  (Reload & merge / Overwrite / Save mine as a copy / Cancel). The
  background definition write on close skips a changed file. A watcher
  (stat every 2 s, and when the app is activated) reloads a project with
  nothing unsaved and asks about one with unsaved work. Unsaved tab edits on
  a branch that moved are re-applied to the new head, or kept on a
  `(mine)` branch where they overlap.
* After crash recovery from `base.ovt`, the base is the real file as it is
  at recovery.

---

## 10. In the main window

`ovtsuite/gui_project.py` (Qt) over `ovtsuite/project/sessionbridge.py` (no
Qt). Help page: `ovtsuite/data/help/projects.md`.

* **One tab per set and branch**, labelled `<project> ▸ <set> · <branch>`.
  A tab edits its branch through an ordinary `TunerSession` opened from a
  *working file* -- the state written as the platform's native container
  (with the branch's definition cache, so it opens fast) or a plain `.bin` --
  so every editor and tool works unchanged.
* **Opening**: the tabs the project had open last time on this computer
  (QSettings, keyed by the project's `id`), or every set on its active branch
  the first time. Anything else opens from the Project panel or File > Open
  from project. A tab closes on its own; the project closes with its last
  tab. Several projects can be open at once; cross-project comparison and
  copy work like any two tabs.
* **Comparison**: a project tab opens compared with the as-read original
  (the root of its branch); any version can be picked instead. A version of
  the same set is a `TunerSession.sibling` of the tab's session -- it shares
  the definition, so a comparison costs no second identification/match.
* **History and patches**: the working file's change log is the branch
  written as change-log text (`sessionbridge.history_text`): one `VERSION`
  record per checkpoint (id = checkpoint id, ranges from its delta) plus the
  checkpoint's own log lines (`Checkpoint.log`: edit summaries and `PATCH`
  records). So Version history (name, compare, go back), Compare with saved
  version, Apply / Remove patch and Generate patch from history work on a
  project tab unchanged. A save reads back what the session appended
  (`sessionbridge.read_log`): the new lines become the checkpoint's log, a
  renamed version renames its checkpoint, and the working log is rewritten
  from the project. Merging versions is not offered (append-only).
* **Saving is projects only**: a tab that is not a project yet becomes one on
  its first Save / Save As -- the file as on disk is imported (its own
  history migrated, a `.dim`/`.sgo`/`.frf` kept as an original) and the
  tab's saved content is committed on top. Raw / original / native formats
  are File > Export.
* **Reads** (flasher, full backup, EDC16 full read, 6HP28 calibration read,
  VDO cluster) go straight into a project (`importers.import_read`: root
  kind `read`): into the active tab's project when the user says so (a new
  root branch of a set of the same platform, or a new set), else a new
  untitled one, snapshotted for crash recovery at once. The standalone
  flasher saves a read as an `.ovt` (or the raw image).
* **Packaged definitions**: a raw member's definition (attached in the
  Project panel, found beside a `.bin` on import, or an imported `.ols`)
  builds the session's entities (`ovtsuite/xdf_session.py`; support
  `"definition"`). Every format is parsed by `ovtsuite/definitions/` into
  one model (`Definition` / `TableDef` / `Storage` / `AxisDef`), each from
  its own fields, never from the binary's contents:
  * **XDF** -- address, size, sign, byte order (`mmedtypeflags` 0x02),
    storage order (0x04), equation, `BASEOFFSET`;
  * **A2L** -- a real ASAP2 reader (tokens, nested blocks, each object's
    grammar): `RECORD_LAYOUT` elements placed in position order with their
    `ALIGNMENT_*`; `STD_AXIS`, `COM_AXIS` (`AXIS_PTS`), `CURVE_AXIS`,
    `FIX_AXIS`; `COMPU_METHOD` identity / linear / rational (inverted) /
    formula / value tables; `BYTE_ORDER`; `FUNCTION` / `GROUP` as
    categories; addresses placed on the image from the `MEMORY_SEGMENT`s;
    axis counts stored in the image (`NO_AXIS_PTS_X/Y`) read from it, and a
    non-static layout re-packed by them;
  * **OLS / KP** -- the WinOLS record list read field by field, in the
    layouts of format versions 114 / 117 / 292 / 397 / 479 / 597 (documented
    in `definitions/winols.py`); a list that does not parse to its declared
    count is reported, not resynchronised by guessing;
  * **DAM** -- DAMOS text (format in `definitions/dam.py`): records split on
    `;` outside `{}`; values, curves, maps, shared axes, group maps and text;
    addresses = `/PBA` + address, placed on the image by the top of the
    `/SND` code/data segments; point counts read from the axis blocks in the
    image; `/REG`/`/REP` conversions `stored = (p1*x+p2)/(p3*x+p4)+p5`
    (reciprocal ones as rational equations, editable); `/SRC` names each
    axis' input. Checked against an EDC15P+ DAMOS and the WinOLS project
    built from it: all 5739 parameters at the same addresses with the same
    sizes and values (WinOLS leaves the 18 reciprocal conversions raw).
  A table stored in the other byte order than the session reads, or outside
  the binary, is left out and counted in the status message.
* **Saving**: Save = the whole project (every edited tab becomes a checkpoint
  on its branch, one write of the `.ovt`); **Save tab only** commits just the
  current tab. The tab's session saves to its working file first -- the usual
  checksum correction and region folding -- and that is what is committed.
  Closing a tab asks about that tab only; the exit dialog saves each ticked
  tab on its own.
* **Project panel** (View > Project panel): sets, branches (open, active,
  rename, new branch from here, delete), each branch's history (compare,
  revert, branch from a version), logs (add, pin, remove, cap) and
  attachments.
* **Crash recovery**: every open project has an `AutoSaver`, ticked every
  minute (section 9); the startup dialog offers recovery before last
  session's tabs; a proper exit discards it.
* `manifest.id` -- a stable id for the project (kept across saves and
  renames).

## 11. Rollout

1. **Core** -- done: `ovtsuite/project/`, `tests/test_ovt_project.py`.
2. **Session bridge + GUI** -- done (section 10), `tests/test_gui_project.py`.
3. **Projects everywhere** -- done: Save writes `.ovt` only, reads go into
   projects, patches/history on checkpoints, XDF definitions for raw members,
   `.ovt` associated by the installers (Windows: `associate_ovt`; the older
   per-platform extensions are no longer associated and an update removes
   what earlier versions registered; Linux: shared-mime-info type
   `application/x-ovt-project` + `.desktop` MimeType).
4. **Definitions and WinOLS** -- done: A2L / KP / OLS / DAM build a tree
   (`ovtsuite/definitions/`, `tests/test_definitions.py`); `.ols` opens as a
   project; project information (`info`), File > New project..., Add files.
5. **Next**: the KWHack EEPROM editor's save inside a project; multi-version
   WinOLS projects (only the version embedded in the file is imported);
   retiring the native container writers (reading stays) once nothing in the
   app writes them.
