# OVT-Suite

**Old VAG Tuner Suite** — a cross-platform (PySide6) tuner for older VW-group
control units, with one editor over several platforms:

| platform | units | status |
|---|---|---|
| **EDC16** | EDC16U31 / EDC16U1 / EDC16U34 TDI ECUs | shipping |
| **EDC15** | EDC15VM+ (VP37) and EDC15P (unit injector) TDI ECUs | shipping |
| **ZF 6HP** | 6HP19 / 6HP26 / 6HP28 transmission control units | shipping |

It discovers maps, curves and scalars from the binary's own structure, names
them from baked-in reference knowledge bases, lets you edit in physical units,
disable trouble codes, and export a TunerPro XDF — no external definition file
required.

The ZF 6HP support was ported from the VAG-6HP-Tuner project.

Every product and package name lives in `ovtsuite/branding.py`; nothing else in
the tree spells them out, so renaming the suite is one command
(`python tools/rename_package.py --to <pkg> --app-name "<name>"`).
`tests/test_branding.py` fails if a stray literal reappears.

> Renamed from **EDC16-Tuner** / `edc16tuner`. `import edc16tuner` still works
> for one release and warns; `import edc16tuner.<submodule>` raises and names
> the replacement, because aliasing submodules loads them twice and gives two
> incompatible copies of every class. Recent-file lists and saved settings are
> migrated from the old locations automatically.

## Install

```
pip install -e .          # or: pip install PySide6  (for the GUI)
```

## Use

```
ovtsuite identify <bin>              # ECU variant + calibration region
ovtsuite scan <bin>                  # structural map/curve detection
ovtsuite match <bin> --family p350   # named identification
ovtsuite tiers <bin>                 # entity count by tuning tier
ovtsuite diff <stock> <mod> --family p350   # named, physical-unit diff
ovtsuite export <bin> out.xdf        # generate a TunerPro XDF
ovtsuite audit <family>              # audit a reference KB
ovtsuite gui <bin> [--compare <bin>] # graphical editor
```

Family autodetects; `--family` overrides it. Families: `p350` (EDC16U31
P350, 1.9/2.0 PD), `p475_83` (P475 8.3, BPW), `p475_84_mf` / `p475_84_hq`
(P475 8.4). The U1 (T5) image is read via the p350 knowledge base.

EDC15 (512 KB C167 images with the Bosch "V4.1" dataset signature) is
detected automatically; families `vm_alh` (EDC15VM+ VP37 1.9 TDI ALH,
038906012A/M/BD/N ...) and `p_pd` (EDC15P unit injector, 038906019GQ ...).
Every command above works on EDC15 too, plus:

```
ovtsuite variants <bin>              # the coding-selected datasets (SG / AG1 / AG2)
ovtsuite vss <bin> --old 195/65R15 --new 205/55R16 [--out t.bin]  # tyre rescale
ovtsuite gearvn <bin>                # v/n gear-recognition thresholds per dataset
ovtsuite dtc <bin> [--disable SG:LDS_K --out t.bin]   # fbw fault paths
ovtsuite msblocks <bin>              # KW1281 measuring blocks with channel names
ovtsuite outtest <bin>               # actuator tests (xcwStell) with channel names
```

See `docs/EDC15.md` for how the EDC15 support works.

ZF 6HP images (transmission control units, big-endian MPC5xx) are detected the
same way; families `zf6hp26` (Audi RS6 5.0 TFSI, 6HP26A) and `zf6hp28`
(6HP28AF GS19). Every command above works on them, plus:

```
ovtsuite tune <bin> --profile list             # the tune profiles
ovtsuite tune <bin> --profile stage1           # dry run: plan, validate, report
ovtsuite tune <bin> --profile stage1 --apply --out tuned.bin
```

`tune` never writes without `--apply`, and refuses a plan the validator
objected to unless `--force` as well. See `docs/ZF6HP_STAGE1.md` for what
Stage 1 is and how it was measured, `docs/ZF6HP_SHIFT_SCHEDULES.md` for which
schedule does what, and `docs/ZF6HP_CHECKSUM_FINDINGS.md` for the CRC.

## What it does

- **Structural discovery** — curves `[N][axis][data]` and maps
  `[Nx][Ny][X][Y][Z]` (and group maps) located by their headers; ~99.5%
  recall against reference definitions.
- **Reference identification** — anchor matching + a piecewise-constant
  offset model place every known parameter across calibrations and firmware
  versions, with a provenance tier per placement.
- **Tuning tiers** — Core / Common / Advanced / Expert, layered on the A2L
  functional categories.
- **Physical-unit editing** — decode/encode via the A2L equation, stored
  (not reserved) dimensions, column-major Z, undo/redo.
- **DTC / Fault-Path editor** — enable/disable trouble codes via their DSM
  class on EDC15/16, or their `kdi_Tool` entry on ZF; restore from stock. On
  ZF the on/off byte belongs to a whole fault class, so the dialog says how
  many codes a change takes with it before writing.
- **Transmission views (ZF)** — shift-schedule and TCC maps with gear-shift
  column headers and pedal-percentage rows, Z shown as stored output-shaft
  speed, engine speed with the converter locked, or road speed from the
  calibration's own final drive and your tyre size. Edits typed in any of
  those modes are converted back.
- **Auto-tune (ZF)** — a profile resolves targets by symbol, plans every
  write, validates against the gearbox, and only then applies, as one undo
  step. Nothing is expressed as an absolute engine speed: profiles scale
  against anchors read out of the image in front of them.
- **Measuring-block editor** — view and edit the signal-block structure a
  scan tool reads (255 groups x 4 channels), each channel a searchable
  dropdown of signal ids named from the A2L (see docs/SIGNAL_CATALOG.md).
- **Comparison** — a second binary paired by identity, with direct /
  axis-mismatch / resampled overlays so differently-structured tables still
  compare meaningfully.
- **XDF export** — corrected dimensions and the tier/category tree, cleaner
  than the source definitions.

## Flash checksums

Edited binaries are checksum-corrected automatically on save (Bosch EDC16
block + span, sum-to-`0xD01FE500`; EDC15: the V4.1 region checksums, rev.1 /
rev.2 tables; ZF 6HP: CRC-16/CCITT-FALSE over a descriptor-delimited region),
so saved/exported images (1 MB U1, 2 MB U31/U34, 512 KB EDC15, 1-2 MB ZF) are
ready to flash. The ZF checksum declines rather than guesses on an image whose
block boundaries it cannot read from the image itself. `ovtsuite checksum <bin>` verifies; `--fix` writes
corrections. Checksum correction comes from the installed module for that
control unit.

## Build a desktop app

Windows, with version stamping and an optional installer (recommended on
Windows — mirrors the VAG-6HP-Tuner build):

```
.\build.ps1                 # build dist\OVTSuite\OVTSuite.exe, and
                            #   dist\OVTSuite-<ver>-setup.exe if Inno Setup is present
.\build.ps1 -Test          # run the test suite first (for a build you ship)
.\build.ps1 -OneFile       # single portable .exe
.\build.ps1 -Clean         # wipe build/ dist/ and the build venv first
.\build.ps1 -Update        # re-run the dependency install (upgrading)
.\build.ps1 -NoInstaller   # skip the Inno Setup step
.\build.ps1 -FastInstaller # lzma2/fast installer: bigger, much quicker to build
```

Linux, as a `.deb` (and optionally an `.rpm`):

```
./build_linux.sh                  # dist/ovtsuite_<ver>-1_<arch>.deb
./build_linux.sh --rpm            # ... plus dist/ovtsuite-<ver>-1.<arch>.rpm
./build_linux.sh --docker --rpm   # build in Ubuntu 20.04: installs on any distro with glibc 2.31+
./build_linux.sh --test           # targeted tests first (OpenPort, FTDI, branding)
./build_linux.sh --skip-build     # repackage the last build
```

The package installs to `/opt/ovtsuite` with an `ovtsuite` launcher, a menu
entry and a udev rule that gives the logged-in user the OpenPort 2.0, FTDI
and CH34x K-line cables without root or group membership (and keeps
ModemManager off the OpenPort). No Python 3.10+ on the build machine -- e.g.
an Ubuntu 20.04 WSL distro -- is fine: the script fetches a private Python 3.11
with uv into `~/.cache` and changes nothing system-wide. Building on such an
old distro is in fact the portable choice. It checks up front for the Qt
libraries PyInstaller needs and prints the `apt install` line for any missing.

A bundle only runs on the glibc it was built against or newer: the script reports that floor, writes it into
both packages' dependencies, and warns when it shuts out older distros -- for anything you hand out,
build on an old distro (like that 20.04 WSL) or with `--docker`. PyInstaller output goes to
`build/linux/`, so a Windows `dist\OVTSuite` in the same checkout is untouched.

`build.ps1` finds a Python 3.10+ interpreter, provisions an isolated build
venv, syncs `packaging\version_info.txt` to `ovtsuite.__version__`, optionally
runs the tests, then drives PyInstaller via `packaging\ovtsuite.spec`.

Repeat builds are incremental: an existing build venv is reused without
interpreter discovery, dependencies are only installed when the requirement
list changes, the layout catalogue is only rebuilt when a reference binary or
the code/KB it depends on changes, and `build\` is kept so PyInstaller reuses
its own analysis cache (the 32-bit J2534 bridge likewise). Each step prints
its duration. `-Clean` forces everything from scratch.

## Layout

This repository is the **base app**. The control-unit modules are not in it:
the VDO Cluster module is published in its own repository, and the others are
closed source (see [Modules](#modules)).

- `ovtsuite/` — the package: a Qt-free core (`session`, `model`, `match`,
  `layout`, `structure`, `kb`) plus `gui.py`, behind one `TunerSession` seam
  and one `Entity` model.
  - `platforms/` identifies every supported file, whether or not its module
    is installed.
  - `modules/` finds, checks and loads the modules (`docs/MODULES_PLAN.md`,
    `docs/MODULE_SDK_DESIGN.md`).
  - `flash/`, `datalog/`, `sniff/` and `logtune/` are the flasher, logger,
    sniffer and log-based tuning tools, which work without any module.
  - `ai/`, `autotune/`, `project/` and `definitions/` are the AI assistant,
    the platform-neutral plan/target/validate spine, `.ovt` projects, and
    user definition files.
  - `data/` holds the help pages, icons, licence texts and the base app's
    data.
  - `branding.py` holds every product and package name.
- `tools/` — build helpers: `build_modules.py` (the base/module split and
  module packages), `collect_licenses.py`, `help_screenshots.py`,
  `build_wiki.py`, `make_file_icons.py`, `rename_package.py`, and
  `export_public.py` (licence headers).
- `tests/` — the base app's tests (pytest). GUI tests are marked slow and
  skip without PySide6 and its platform libraries.
- `packaging/` — the PyInstaller spec, the Inno Setup installers and the
  Linux packaging. `addons/` — optional downloads the installers offer.
- `docs/` — design notes for help pages, modules, containers and search.

## Modules

The base app in this repository identifies every supported file and has the
hex editor, comparison, patches, version history and the flasher, logger,
sniffer and simulator. Editing and checksum correction for each kind of
control unit come from **modules** (`docs/MODULES_PLAN.md`). The VDO Cluster
module is open source in its own repository; the others are closed source,
and their installers are published on the author's website.

## Licence

OVT-Suite's base app is free software under the
[Mozilla Public License 2.0](LICENSE). Contributions are accepted under the
same licence (see [CONTRIBUTING.md](CONTRIBUTING.md)). Third-party code and
libraries keep their own licences; the app lists them under
**Help > Credits & licences**.

## Trademarks and legal use

OVT-Suite is an independent project, not affiliated with or endorsed by
Volkswagen, Audi, Bosch, ZF, Aisin, VDO/Continental or any other maker whose
names and part numbers it uses to describe compatibility. Modifying a control
unit can damage the vehicle, and defeating emissions controls on a road
vehicle is illegal in many countries: use the software only where and how the
law allows. It comes with no warranty. See [LEGAL.md](LEGAL.md).
