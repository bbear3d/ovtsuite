---
title: Converter
order: 57
context: ConverterDialog, ReviewDialog, TranslatorSettings, WatchDialog
---
# Converter

The converter turns a definition or a binary from one format into another,
with no project or binary open. It reads TunerPro XDF, ASAP2 A2L, Bosch
DAMOS, WinOLS projects (.ols) and map packs (.kp), EcuFlash and RomRaider
XML, and OVT-Suite's own definition JSON and CSV map lists; it writes all of
them except WinOLS projects. Binaries convert between plain BIN, Intel HEX and Motorola
S-record. Calibration value files (DCM and CDFX) are read and written too:
see [Calibration values](#calibration-values-dcm-cdfx).

Open it with **Tools > Converter (definitions, values and binaries)...** in the main
window, or on its own with the **OVT Converter** shortcut. Files dropped on
the shortcut open as the files to convert.

![The converter with a TunerPro XDF and its binary added](images/converter.png)

## The window

The window is laid out in tabs:

- **Files** -- the choice of files only: what goes in (the files to
  convert, the binary), the format to convert to, and every file that
  comes out: the converted file and, for a WinOLS project, the binary and
  MPC it holds, each ticked on its own.
- **Options** -- every option, for every format, all on this one tab (it
  scrolls), so none needs a trip back to the Files tab:
  - **Image and placement (every conversion)** -- the image size and base
    address (filled in from the files once they are read; type over them to
    give your own), tables whose size the image stores, what to do when an
    output file is already there (see
    [Output files already there](#output-files-already-there)), and porting
    onto another software version.
  - **Source: RomRaider definition** -- which ROM of a RomRaider file (see
    [EcuFlash and RomRaider definitions](#ecuflash-and-romraider-definitions)).
    Until a RomRaider definition is added on the Files tab the **ROM:** list
    has nothing to choose from and says so; it is not used for any other
    kind of file.
  - **Target: ...** -- one group per output format: TunerPro XDF (checksum
    plugin, value blocks without an axis, a patch), ASAP2 (addresses),
    WinOLS map pack, DAMOS, calibration values (DCM, CDFX), values written
    into a binary (checksums, record size), binary images (record size,
    fill byte, byte swap, checksums).

  Every option can be changed at any time, whichever format is chosen, so
  you can set a format up before choosing it; the settings are kept and
  used whenever that format is chosen. The group that applies to the files
  and the format chosen has **(applies now)** after its title; each of the
  others has a line in italics saying when it is used. Choosing a format
  scrolls its group into view. The Translation and Tables tabs likewise
  say whether the conversion chosen uses them, and stay editable.
- **Translation** -- translating titles and descriptions.
- **Tables** -- converting only some tables, with the list of the tables
  that will be written.

The preview sits under the tabs, starting just below the Files tab's
Output group, so it has the rest of the window; drag the bar between them
to give either more room (the window remembers where you left it). Under
it, **Convert** and a progress bar that fills smoothly as the preview or
the conversion runs -- reading the files, placing the tables, writing, the
read-back check, then each binary written out -- with the step it is on
beside it. It moves on steadily through a long step and never goes back;
it shows a busy bar only when the work gives no estimate at all. It is full
when everything is done and empties again if the conversion fails.

After a conversion the summary names the file written first -- for
example *Wrote project.kp (17292 maps, folders kept)* -- then each binary
or MPC written beside it, by name. A file left alone because it was
already there (the Options tab's **When an output file exists**) is
named with *left as it was*, so an older file is never taken for this
run's result. The window remembers its settings for next time: every
option on the Options, Translation and Tables tabs (every format's, not
only the chosen one's), the format last chosen, the tab it was left on and
which outputs were ticked. They are saved as you change them, so closing
the window any way -- or the whole program -- keeps them; a converter
window opened and closed without changes leaves the settings another one
saved alone. What belongs to one set of files is not kept: the files, the
binary, the output names, the original image, the patch's binary and name
and the RomRaider ROM. If the files added do not offer the format last
chosen (value files offer only DCM and CDFX), it is chosen again once they
do. The window keeps its size while you work, marking what applies rather
than adding or removing rows.

## Convert a definition

1. **Files** tab, **Files:** drop the file on the list, or press **Add
   files...**. Each file shows what it is. The type comes from the file's
   contents, not its name, so a renamed file is still recognised. A WinOLS
   project also says what it holds, e.g. *WinOLS project: 9477 maps; embeds
   a 4 MB binary + 2 versions (Stage 1, Stage 1_AEE)*.
2. **Binary:** (optional) give the binary the definition is for when you have
   it. It settles three things a definition may leave to the image: where the
   image starts in the ECU's address space, the size of tables whose point
   counts are stored in the image, and (for an XDF) which checksum plugin
   applies. Without it the converter uses what the definition itself says.
   A WinOLS project carries its binary: with no binary given, that one is
   used (the original WinOLS imported), as if you had given it here -- to
   place and check the tables, export values (DCM, CDFX), pick the checksum
   plugin (**Automatic**), write values into a copy of it, or save it as
   BIN, HEX or S-record. The report says when it was used.
3. **Convert to:** pick the format. Its options are on the **Options** tab,
   in the group named after it.
4. Press **Preview**. The list shows every table that will be written. A
   warning mark means the target cannot express that table fully: hover over
   the mark to see why.
5. Check **Save as:**, then press **Convert**. When anything could not be
   expressed fully, a report is saved beside the output (`name.convert.txt`);
   **Report...** shows it.

After writing, the converter reads the result back with OVT-Suite's own
reader and compares every table's address, size and width with what it
meant to write. The summary line says whether that check passed.

## Output files already there

The converter does not silently replace a file. **Options** tab, **Image and
placement**, **When an output file exists:**

- **Ask each time** (the default) -- for every file that is already there,
  Convert stops and asks before doing the work: **Yes** replaces it, **No**
  leaves it alone, **Yes to All** / **No to All** give the same answer for
  every other file of this conversion (the converted file and the binaries a
  WinOLS project writes out with it). Tick **Make this the default** to stop
  being asked: after **Yes** (or Yes to All) the setting becomes
  **Overwrite it**, after **No** (or No to All) **Skip it**; change it back
  here at any time. A file you have just chosen in the **Save as** dialog,
  which already asked about replacing it, is not asked about again.
- **Overwrite it** -- replace it without asking.
- **Skip it** -- leave it as it is, without asking.

A file left alone is not written; the status line and the report (under
*Already there, left alone*) list it. The window remembers the choice; a
preset does not carry it. A watch folder and the command line never ask: see
[Watch a folder](#watch-a-folder) and
[Presets and the command line](#presets-and-the-command-line).

## The binary and MPC of a WinOLS project

A WinOLS project (.ols) holds the binary WinOLS imported (the *original*)
and every version made from it (*Stage 1* ...), each stored whole. A binary
WinOLS put together from several memories -- an EDC16's flash and its
processor's internal flash (MPC), or a gearbox controller's two areas --
lists them as areas #1, #2 ...

With a project among the files, the **Files** tab's **Output** has a row for
each, ticked, its name filled in beside the file saved:

- **Binary:** the project's binary -- `<project>.bin` (or the extension of
  the file WinOLS imported, `.ori`, `.mod` ...). For an EDC16 project that
  holds the MPC too, this is the 1 MB external flash.
- **MPC:** the processor's internal flash (448 or 512 KB), `<project>.mpc`,
  where the project holds it.

Untick any of them -- **Save as:** included, to write only the binaries --
or change a name; **Convert** writes every ticked file in one go and reads
each binary back to check it.

**Other binaries...** (beside them) lists everything else the project
holds: the original whole, each version and each area, none ticked. Tick one to write
it too (into the folder of the file saved); double-click a name to change
it. **Write as:** **As stored** writes the bytes exactly as the project
holds them (no padding, nothing changed); **Intel HEX** or **Motorola
S-record** writes them in that format instead (an area at its own offset).

On the command line: `ovtsuite convert project.ols --list-images` lists
them, `ovtsuite convert project.ols --extract-images -o folder` writes the
original and every version, `--only original version1 original-area2` (or
`--only all`) picks, and `--to hex` / `--to srec` writes that format.

## WinOLS project to map pack

A WinOLS project (.ols) or map pack (.kp) converted to a map pack is passed
through, not rebuilt: every map goes in as the project holds it -- its name,
description, units, scaling, axes and type -- in the folder it is in, and
the project's folder list goes in as it is (names, order). Nothing is
regrouped (an EDC15 project's code blocks stay in their folders, not in
*Codeblock* folders), renamed or left out. Translation, when ticked, is the
one change: it rewrites the descriptions, value and axis labels, units and
folder names -- never a map's name, which is its identifier.

What a map pack of this layout cannot hold is not written, and the report
says so: the hexdump view (WinOLS adds its own), axis point texts and the
other languages a newer project keeps beside each text, and folders inside
folders (each is listed at the top). *Potential maps* is WinOLS's own
search: WinOLS shows it for the map pack as it does for any.

## Port a definition to another software version

A definition is made for one software version. On another version of the
same ECU project parts of the calibration have moved, so its addresses are
right for some tables and wrong for others. Porting finds each table in the
new binary on its own evidence instead of trusting one offset.

1. Add the definition (A2L, DAMOS, WinOLS project, XDF ...) and give the
   binary of the **other** software version in **Binary:**.
2. Tick **The binary is another software version: find each table in it
   (port)**.
3. **Original image (optional):** the binary or HEX the definition was made
   for, if you have it. A WinOLS project carries its own. With it, tables
   that moved are found by their own bytes, and far more are confirmed.
4. Pick the target and convert. The summary says how many tables were found
   where the definition puts them, found moved, moved with their region, or
   lie between confirmed tables.

Tables nothing in the new binary confirms are put in the category
**Unverified - check before writing** (or left out, if you choose so). They
are placed by the offset of the nearest confirmed table and are often
wrong: check them before relying on them. The report lists each one.

Porting works with every target: a definition for the new binary, its values
as a DCM or CDFX, or values written into it. Values are never read from or
written to an unverified table, so a DCM exported from one version can be
written into another safely: what cannot be confirmed is left alone and
listed in the report.

## Compare two definitions

Add two definitions and choose **Differences between the two (CSV)**. Each
table is matched by name and listed when it was:

| Change | Meaning |
|---|---|
| added / removed | in one definition only |
| renamed | in one only, but the other has a table at the same place, size and width under another name |
| moved | its values are at another address |
| axis moved | its values stay, an axis moved |
| resized | other dimensions |
| rescaled | another conversion or unit |
| retyped | another width, sign or float |

With a binary, both are placed in it first, so the addresses compare as file
offsets; with the port option both are ported onto it. **Only some tables**
narrows the comparison.

## Calibration values (DCM, CDFX)

A DCM (the DAMOS exchange file INCA, CANape and WinOLS read) or a CDFX (ASAM
Calibration Data Format) holds values, not table positions: each table by
name, with its physical values and axis points. What you add to the list
decides what the converter offers:

| Files | Convert to | What happens |
|---|---|---|
| A definition, with its binary | DCM or CDFX *(values read from the binary)* | Every table's values are read out of the binary through the definition, with its shared axes. |
| DCM or CDFX files only | DCM or CDFX | The values convert as they are. Several files are merged; a later file's value replaces an earlier one's. |
| A definition and DCM / CDFX files, with the binary | BIN, HEX or S-record *(values written in)* | The values are written into a copy of the binary, each where the definition places its table. |

The binary goes in **Binary:**, or add it to the files. When a job needs one
and none is given, the converter says so before starting.

Writing values into a binary:

- Names are matched exactly, then ignoring case, then by the file's display
  name. A CDFX entry for one element of a block (`name[3]`) writes that
  element.
- A table whose size in the file differs from the definition's is left
  alone. So is a value the definition does not have. Both are marked in the
  preview and listed in the report.
- A value beyond what the cell can hold is stored at the cell's limit, and
  the report says which tables that happened in.
- Shared axes are written from their own entry in the file. A curve or map
  whose axis points in the file differ from a shared or fixed axis is
  reported, not changed.
- **Correct checksums afterwards** is on by default: the installed module
  for the platform corrects them, as in File > Save.
- Afterwards the converter applies the same values to the written file
  again: nothing may change. The summary says whether that check passed.

Values are written as the exact physical value of each cell, in the
shortest form that converts back to the same stored value, so a file
exported here and applied back gives the same bytes. Enumerations (verbal
conversion tables in an A2L) are written as their texts.

DCM has no form for three-dimensional tables; converting a CDFX that has
them to DCM leaves them out and says so.

## EcuFlash and RomRaider definitions

EcuFlash and RomRaider (the Subaru and Mitsubishi tools) describe tables in
XML. Both read and write here, to and from every other definition format.

**EcuFlash** keeps one ROM per file, which usually *includes* a base file
holding the scalings and table layouts. Keep the folders as EcuFlash has
them: the base is found by its id in the ROM's folder or the folder above it
(where EcuFlash keeps `Bases`). If it is not there, the converter says which
base is missing. A file written here is self-contained: it needs no base.

**RomRaider** keeps many ROMs in one file (`ecu_defs.xml`). When such a file
is added on the **Files** tab, the **ROM:** list on the **Options** tab
(group **Source: RomRaider definition**, marked *(applies now)* once one is
added) lists its ROMs -- until then it holds only a line saying to add one:

- **Found from the binary** (the default): the converter uses the ROM whose
  id string is in the binary, the way RomRaider recognises a ROM. The text
  beside the list says which ROM that is, or that none matched.
- Pick a ROM by its id to use that one, with or without a binary — for
  example to convert a definition for a ROM you do not have the binary of.

On/off byte patterns (EcuFlash *bloblists*, RomRaider *Switch* tables) have
no equivalent in the other formats; they are counted in the report, not
converted. Table names with spaces become identifiers in DCM and CDFX
(`Target Boost` is written `Target_Boost`; CDFX keeps the original as its
display name), and values apply back by either form.

## The formats

| Format | Read | Write | Notes |
|---|---|---|---|
| TunerPro XDF | Yes | Yes | Optional checksum plugin block and patches (below) |
| ASAP2 A2L | Yes | Yes | Addresses as file offsets (WinOLS, TunerPro) or ECU addresses (INCA, CANape) |
| Bosch DAMOS | Yes | Yes | Written back into a DAMOS, its own records are kept as they are |
| WinOLS project (.ols) | Yes | No | Its binary is used when none is given; it and its versions can be [extracted](#extract-the-binaries-of-a-winols-project) |
| WinOLS map pack (.kp) | Yes | Yes | Laid out section by section as the map packs WinOLS 5.0 writes; reads back in OVT-Suite and opens in WinOLS. From a WinOLS project or map pack the maps pass through as they are: every map with its own name, description, units, scaling and axes, in the project's own folders (see [WinOLS project to map pack](#winols-project-to-map-pack)) |
| EcuFlash XML | Yes | Yes | Finds the base a ROM includes beside it; writes one self-contained file |
| RomRaider XML | Yes | Yes | Many ROMs per file: found from the binary or picked |
| OVT definition (JSON) | Yes | Yes | Every field of the definition, for scripts and comparisons |
| Map list (CSV) | Yes | Yes | One row per table, for spreadsheets |
| DCM (DAMOS exchange) | Yes | Yes | Calibration values (above) |
| CDFX (ASAM CDF 2.x) | Yes | Yes | Calibration values (above) |
| BIN, Intel HEX, S-record | Yes | Yes | Crop, pad, fill, byte-swap and checksum correction |

**WinOLS folders become categories.** Each map of a WinOLS project or map
pack is put in the category of its folder (and of the folders that folder
is in). A project that lists every map once per code block -- an EDC15's
three copies of its data -- also gets a category per block, *Codeblock 0*,
*Codeblock 1* ..., which each map is in first. In an XDF the code blocks
are numbered first and the folders after them by name. A map in no folder is in *My maps*, as WinOLS shows it.

**A WinOLS map in an XDF** is titled by its description (its name when it
has none), and its description reads *OLS: name | description | Z: value
label | X: axis | Y: axis*, so TunerPro's
tree reads in words and the identifier is still there. Every table and value
carries its decimal places (WinOLS's own), its range (*min* / *max*, what
the stored width can hold) and whether it is signed. Umlauts and other
accented letters stay as they are (*Überwachung*, *Groß*, *°C*).

The converter reads WinOLS's
own map list and writes every map it lists, a map listed twice (in two
folders, or twice in one) included.

## Checksum plugin (XDF)

An XDF can name a TunerPro checksum plugin, so TunerPro corrects the
checksums every time the tune is saved. **Checksum plugin:** offers:

- **None** — no checksum block.
- **Keep the source's** — an XDF converted to XDF keeps its blocks.
- **Automatic** — the plugin for the platform of the binary you gave: one an
  installed module offers, else a TunerPro plugin on this PC made for that
  ECU (an EDC16 binary gets a method named for EDC16).
- A plugin by name — the plugins installed modules know (EDC16, for example).
- **TunerPro: ...** — every checksum plugin installed in TunerPro's plugin
  folder (*Documents\TunerPro Files\Plugins*), one entry per checksum
  method it offers. The XDF names the plugin by its id and the method by its
  number, just as TunerPro does, so it works on any PC with the plugin
  installed. Plugin files are only read, never run.
- **Another TunerPro plugin (DLL)...** — a plugin kept somewhere else. If it
  offers several checksums you choose one; it is then added to the list.

What a plugin does not correct is shown under the choice and written into the
XDF's description.

## A tune as a TunerPro patch (XDF)

A TunerPro patch is a switch in the XDF: TunerPro writes its bytes into the
open binary, shows whether it is applied, and can take it out again. The
converter makes one from two binaries of the same software.

1. Add the definition and choose **TunerPro XDF**.
2. **Binary:** the original. **Add a patch: the changes in:** the changed
   binary (a tune, a fix).
3. Give the patch a name, or leave it to be named after the file. Choose
   **One patch**, or **One per changed table** to switch each change on and
   off on its own.

Each entry is named after the table it changes. Bytes no table covers
(checksums, code) are included by default, named by address; choose
**leave out** when TunerPro's checksum plugin will recompute the checksums,
so the patch keeps showing as applied after saving. The converter checks
that the patch turns the original into the changed binary exactly.

Patches already in an XDF are kept when it is converted to XDF. Other
formats have no place for them, and the converter says so.

## Only some tables

On the **Tables** tab, tick **Only some tables** and give a name pattern, the
categories, or both. **Wildcards** (the default): `*` stands for any text and
`?` for one character, and text without them matches anywhere in a table's
name, symbol or description -- `Smoke` and `*Smoke*` find the same tables,
`AccPed_*_MAP` the pedal maps; case does not matter. **Regular expression**
takes Python's syntax instead. The list under it shows the tables the
conversion will write and how many of all, as you type.

## Translate

On the **Translation** tab, tick **Translate the names and descriptions** to translate titles, descriptions, categories, axis names
and units on the way, into the same format or another. Identifiers (A2L and
DAMOS names) are never changed.

- **Built-in** works offline, German to English, using OVT-Suite's own
  dictionary and the glossary of settled terms (*Driver's Wish*, *Smoke
  Limiter*, *Target Boost* ...). It splits German compounds into their parts
  (*Startzündwinkelrampe* becomes *start ignition angle ramp*) and reads
  umlauts written either way (*Ölpumpe*, *Oelpumpe*). It does not translate
  other languages: a French or Italian definition needs **Online**.
- **Online** sends the texts the built-in dictionary does not know to the
  translator chosen in **Translator settings...**: DeepL, Google, Microsoft,
  LibreTranslate, or the AI service set up for the assistant. Only the texts
  are sent, never binary data. Every result is remembered, so converting the
  same file again needs no connection.

Press **Review...** to see every text and its translation, and change any of
them, before converting.

## Watch a folder

To convert files as they arrive -- a folder your downloads, a colleague or
another tool drop files into -- set up the conversion as usual, then choose
**More ▸ Watch a folder...**:

- **Watch:** the folder files arrive in.
- **Results to:** where the converted files go (by default a *Converted*
  folder inside it; never the watched folder itself).
- **After converting:** leave each file where it is, or move it to a *Done*
  folder inside the watched one.
- **Also convert the files already there**, and **Include the folders inside
  it**, when you want them.

The window says what happens to each kind of file before you start. Files in
the converter's list are kept for every arriving file, so:

- with no files listed, each arriving definition or binary is converted to
  the format chosen (all your HEX files as BIN, all XDFs as A2L);
- with a **definition** listed, each arriving **binary** is the binary: its
  values exported as DCM or CDFX, or a ported XDF made for it;
- with a definition and a **value file** listed, each arriving binary gets
  those values written into it.

A file is taken only once it has stopped changing, so a copy still in
progress is never read; a file changed later is converted again. The log
lists every file (double-click one to open its folder). Watching stops when
you press **Stop watching** or close the converter.

A watch folder has no one to ask, so a result already in the output folder
is replaced only when **When an output file exists:** (Options tab) says
**Overwrite it**; with **Ask each time** or **Skip it** the arriving file is
listed as skipped and the result left alone. A result the watch wrote itself
is always brought up to date when the same file arrives again changed.

## Right-click menu

**More ▸ Add "Convert with OVT Converter" to the right-click menu** adds that
entry to your file manager for the files the converter reads (A2L, DAMOS,
WinOLS, KP, XDF, DCM, CDFX, HEX, S-record, BIN). It opens them in the
converter; files right-clicked together all open in one window. Choose it
again to take it out. The same switch is in **Tools > Options > General**
(**Right-click menu**), so it can be added later if it was not chosen while
installing. The installer offers the same, unticked, and uninstalling
removes it.

It adds a menu entry and nothing else: double-clicking a file still opens
whatever opened it before.

- **Windows**: in Explorer's menu; on Windows 11 under **Show more options**.
  With both the app and the standalone OVT Converter installed, the entry
  shows once; uninstalling one leaves the other's entry working.
- **Linux**: Dolphin and Nemo, in the right-click menu; GNOME Files, under
  **Open With**. Only on the files the converter reads -- never on every
  file, and never made the program that opens them.
- **macOS**: not available -- drop files on OVT Converter instead.

## Presets and the command line

**Save preset...** keeps every setting under a name; pick it from **Preset:**
next time. The same conversion runs from a terminal:

```
ovtsuite convert BPW.a2l --bin car.bin -o BPW.xdf --checksum auto
ovtsuite convert defs/ --to json -o out/
ovtsuite convert original.hex --to bin --size 0x200000
ovtsuite convert map.xdf --preset "C:\Users\me\...\English.json"
ovtsuite convert SC4800.a2l --bin 49AEfF.s19 --to cdfx
ovtsuite convert values.cdfx --to dcm
ovtsuite convert SC4800.a2l changes.dcm --bin 49AEfF.s19 --to srec -o tuned.s19 --fix-checksums
ovtsuite convert ecu_defs.xml --list-roms --bin car.bin
ovtsuite convert ecu_defs.xml --rom A2WC500R --to xdf -o A2WC500R.xdf
ovtsuite convert FK.a2l --bin AA.bin --port --reference FK.hex --to xdf
ovtsuite convert old.a2l new.a2l --to diff -o changes.csv
ovtsuite convert map.xdf --bin stock.bin --patch-with tuned.bin -o map_with_tune.xdf
ovtsuite convert --watch inbox --to xdf -o converted
ovtsuite convert BPW.a2l --watch bins --to dcm --watch-after move
ovtsuite convert --shell-menu install
```

On the command line an output file that is already there is left alone and
said so (`skipped: ... is already there`), and the exit status is 3; give
`--overwrite` to replace it. A preset never decides this. The same goes for
`--watch` and `--extract-images`.

`ovtsuite convert --help` lists every option.
