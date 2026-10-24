# OVT-Suite module split

Status (2026-10-10): design agreed with Bear, and P1 to P4 are built. The EDC16 flash and
EEPROM checksums and the HVAC firmware map and checksum moved into their modules afterwards. P5 is the
Windows build and installer run on Bear's machine (see "Still to do").

## Goal

Ship OVT-Suite as a **base app** plus optional **control-module modules**, so a user installs
only what they work on.

- The flasher, logger, sniffer and simulator stay universal: they read and write every
  supported module whether or not its module is installed.
- Checksum correction, and definitions/editing, come only from installed modules.

## Where the size went

| Install (Linux build, measured) | Before | After |
|---|---|---|
| App folder | 184 MB | **126 MB** base, plus modules as wanted |
| ZF 8HP module | (in the app) | 20 MB |
| EDC16 module | (in the app) | 5.2 MB |
| ZF 6HP module | (in the app) | 2.5 MB |
| EDC15 / VDO / HVAC / Aisin | (in the app) | 0.9 / 0.3 / <0.1 / <0.1 MB |
| Checksum-only variants | -- | 1 to 200 KB each |

Two things produced the base app's saving:

- **Unused Qt dropped.** `opengl32sw`, Qt Quick/QML, Pdf, VirtualKeyboard, Network, the
  translations and the plugins that pulled them in. Of the Python-side exclusions, only the
  file-level filter in the spec actually removes the DLLs.
- **Module data out of the base.** The module code itself is small.

Measured on Windows before the split: 170 MB installed, of which PySide6 was 92 MB. Expect
roughly the same proportions there.

`cryptography` is now a build dependency (module signatures, and the ZF 8HP Gen2 AES codec).
The Windows build environment never had it, so ZF 8HP Gen2 `.frf` files could not open in the
built app before. It adds about 6 to 14 MB depending on platform.

## Decisions (Bear, 2026-10-10)

| Topic | Decision |
|---|---|
| Delivery | **Both.** One `.ovtmod` payload. The app installs it; per-module setup.exe and .deb also exist. |
| Full build | **Base + every module installed through the same loader.** One code path. |
| Full installer | **Components checklist,** with every module ticked. Base-only and per-module installers are also built. |
| OVT Tools | **Accepts modules,** using only their checksum part. The tool installers offer every module's checksum variant. |
| Qt trim | **Same pass.** |
| Granularity | **One module per platform:** edc16, edc15, zf6hp, zf8hp, aisin, vdo, hvac. |
| Checksum-only | **Auto-built for every module.** |
| Versioning | **Module API level** (`MODULE_API = 1`). Base-only releases do not force module rebuilds. |
| Module versions | **Independent of the app's** (Bear, 2026-10-10). EDC15, EDC16 and ZF 6HP are stable at **1.0.0**; ZF 8HP, Aisin, VDO and HVAC are **0.1.0**. Each is set in its own `<id>/module.py`. |
| Compatibility | **Newer apps load older modules** wherever possible (see "Backwards compatibility"). |
| App version | **0.3.0** with the module split (Bear, 2026-10-10). Version numbers are raised only for a release (Bear, 2026-10-14), by 0.0.1, once per release. |
| Module installer location | **`dist\module-installers\`** (Windows setup.exe) and `dist/module-installers/` (Linux .deb), apart from the app's own installers. |
| Protection | **Ed25519 signing, plus a licence slot** (not enforced yet). |
| Aisin | **Ships now:** identification and the 09D checksum. It grows later. |
| EEPROM tools | **In their module.** The EDC16 EEPROM editor ships with EDC16, the EDC15 editor with EDC15. |
| Missing module | **Dialog, then hex editor.** The dialog has an "Install module..." button. |
| EDC16 fallback | **Removed.** A binary is EDC16 only when it carries its CAFEAFFE descriptor; anything unclaimed is "unknown". |
| Licensing | Base app **MPL-2.0**, in a public repository with fresh history (`tools/export_public.py base`). **VDO** module MPL-2.0 in its own repository (`export_public.py vdo`; `license={"terms": "MPL-2.0"}` in its spec). Every other module is closed source under `ovtsuite/modules/EULA.txt`, which the build puts in its package; their installers go on the author's website. FTDI's driver is downloaded by the installer, never shipped (Bear, 2026-10-12). |

## How it works

### Identification (base app, `ovtsuite/platforms/`)

There is one structural detector per platform:

- EDC16: the CAFEAFFE descriptor.
- EDC15: the V4.1 dataset signature.
- ZF 6HP: the ident block or the shift-schedule family, with the EDC16 descriptor absent.
- ZF 8HP: a block descriptor placing itself at a calibration base.
- VDO: the layout anchors.
- Aisin: the 09D checksum descriptor, or the TR-80 block trailers.
- HVAC: recognised from its container.

`identify()` runs them in a fixed order, and a native container's declared kind wins (except
`.edc16`).

I re-checked all 1,626 binaries in the Reference corpus. Every one gets the same platform as
before, except 22 Aisin images that are now recognised.

Format code the flasher needs lives here too, with alias shims at the old names:

- the 8HP format
- the 6HP table scanner
- the `.8hp` and `.vdo` containers
- the VDO layouts

### Registry (`ovtsuite/modules/`)

- `api.py`: `ModuleSpec`, `MODULE_API`, `ChecksumProvider`.
- `registry.py`: `installed()`, `for_platform()`, `checksum(pid, session)`, `missing_text()`.
- `loader.py`: mounts installed modules and applies `OVT_DISABLE_MODULES`.
- `installer.py`: check, install and remove; module locations.
- `package.py`: the `.ovtmod` format and the checksum closure.
- `signing.py`, `licensing.py`.
- `build.py`: ownership, staging, fingerprints, packing and the clean-interpreter check.

Each platform package declares itself in `<id>/module.py`. Hooks are package-relative strings,
so reading a spec imports nothing heavy.

### Sessions

`TunerSession` dispatches through the registry and records three things:

- `session.support`: `full`, `checksum` or `none`.
- `session.binary_identity`
- `session.unsupported_reason`

`is_edc15`, `is_zf6hp` and `is_zf8hp` now mean "a session that module built", so a limited
session never reaches module code.

Stored definitions (`defn`) digest only the base model code (`defn.MODEL_FILES` and
`defn.MODEL_VERSION`) plus the session's own module. So installing or removing another module,
or an app release that leaves model building alone, never invalidates them.

**Once only:** files saved before the split open the slow way on first load, and are
re-stored on their next save.

### Ownership

A module owns two things:

- its package (`ovtsuite/<id>/`);
- the data files matched by its `ModuleSpec.data` globs.

Everything else is the base app's. `build.ownership()` fails the build when a file has two
owners.

Shared engine code stays in the base: the KB format, the anchor matcher and the structure
scanner. Containers and the flasher need them too, and they are small.

Checksum maths is never base code (Bear, 2026-10-10):

- **EDC16** (`edc16/checksum16.py`, formerly `ovtsuite/checksum.py`). The base app's `.sgo`
  decoder asks the EDC16 module to vouch for an EDC16-cipher decode. Without the module those
  sections stay undecoded, the container records `needs_module = "edc16"`, and the file opens
  as an EDC16 binary with "Install the Bosch EDC16 module". The flasher's EDC16 image check is
  structural (the `0xCAFECADE` calibration marker in the window it writes), plus the module's
  checksum layout when it is installed. `ovtsuite checksum` goes through the registry for every
  platform.
- **EDC16 EEPROM** (`edc16/eeprom16.py`, formerly `ovtsuite/edc16_eeprom.py`). The block
  checksums, the immobiliser bytes and the locator. The flasher's OBD "Find EEPROM" scan does the
  memory sweep itself and asks the module to find the EEPROM in it; without the module it refuses
  before sending anything. The patch tool's EEPROM exemptions go through the module too
  (`exempt_ranges(data, region="eeprom")`).
- **HVAC** (`hvac/firmware.py`). The base app keeps the Climatronic container side in
  `flash/hvac_image.py`: sections, the flash loader, the block list and the `.hvac` formats.
  The module owns the per-firmware variable map, the auto-locators, the editable variables
  (`firmware.attach(image)` returns a `Calibration`) and the internal checksum. The flasher's
  post-read checksum line comes from the module and is omitted without it. Saving with
  `fix_checksum=True` without the module raises rather than writing an uncorrected file.

### Packages (`.ovtmod`)

A package is a zip containing:

- `manifest.json`: the SHA-256 of every file, plus API level, variant and licence.
- `signature`: Ed25519 over the manifest.
- `<id>/...` and `data/...`: laid out like the app's package directory.

What `<id>/` holds depends on the module's licence:

- **Open-source modules** (VDO, ZF 5HP) ship their Python source. Byte-code is included,
  hash-checked (PEP 552), so a module in a read-only folder never recompiles. The package runs
  anywhere and is untagged: `vdo-0.1.8.ovtmod`.
- **Closed-source modules** (under the module EULA) ship **no Python source**. The build compiles
  every file with Cython into a native extension module (`checksum16.cp313-win_amd64.pyd`, or a
  `.so` on Linux) beside where the source would be, so the normal import machinery loads it from
  the mounted folder unchanged (`ovtsuite/modules/native.py`). A package's `__init__.py` goes as
  byte-code only (`__init__.pyc`, read by CPython's sourceless loader): it holds only the
  package docstring and re-exports. Any file Cython or the C compiler rejects falls back to
  byte-code too, and the build names it (`... as byte-code only: <reason>`). Help pages, data
  files and the EULA ship as before.

  Compiled code keeps Python semantics: annotations are not used as C types
  (`annotation_typing=False`), docstrings stay, and tracebacks name the module's `.py` file,
  line and function.

  Compiled code runs on one CPython version, OS and machine only. The manifest says which
  (`"python": "cp313"`, `"platform": "win_amd64"`) and so does the file name:
  `edc16-1.0.15-cp313-win_amd64.ovtmod`, `edc16-1.0.15-checksum-cp311-linux_x86_64.ovtmod`. The
  installer refuses a package built for another ("built for Python 3.11 on Linux (x86-64), but
  this app runs Python 3.13 on Windows (64-bit)"), and an installed copy for another platform
  is reported in the Module Manager, not loaded. The updater and Install from GitHub only offer
  the package built for the running app, so the Windows and the Linux build of one version can
  sit among one release's assets.

  The tags come from the interpreter that runs `tools/build_modules.py`, which is the build venv
  the app is frozen with, so they always match the app built beside them.

The **checksum variant** is the import closure of the module's checksum hook, computed from
the source, plus `ModuleSpec.checksum_data` (VDO needs its ROM definitions).

### Install and load

Install checks everything before it writes anything:

- the product;
- the module id, which must be known to this base app;
- the API level;
- the signature, against `data/module_keys.json`;
- every file hash;
- the licence hook.

It then extracts to a temporary folder and swaps it in.

Modules are found in three places:

- `OVTSUITE_MODULES_DIRS`;
- the app's own `modules/` folder (installers, all users);
- the per-user folder: `%LOCALAPPDATA%\OVT-Suite\modules`, or `~/.local/share/ovtsuite/modules`.

At every start each installed module is re-verified (signature and hashes, about 70 ms for all
seven). It is then mounted by appending its folder to the package `__path__`. That needs no
custom importer, and its code finds its data beside it. An in-tree package (a source checkout)
always wins.

### Replacing source installs

Once a compiled copy of a closed module is installed, no copy of its old Python source may stay on
disk (`installer.scrub_source`). A copy "carries source" when its manifest has no `platform` tag,
the module is under the EULA, and `.py` files are listed. Scrubbing happens in three places:

- **Installing a compiled package** into the real locations (the app's `modules` folder or the
  per-user one; this covers the Module Manager, Install from GitHub, the in-app module update and
  `install-module`). The folder it replaces is swapped whole, as always. Then every other
  source-carrying copy of the module, in every modules folder, is deleted. An install into an
  explicit folder (`install-into`, tests) never reaches into the user's own.
- **At every start**, when the mounted copy of a module is compiled. Any source copy left behind
  goes: an older all-users install, or a copy in a folder the install could not write. So does
  any stray `.py`, `.pyc` or `__pycache__` beside the compiled copy that its manifest does not
  list, such as files an installer laid over an old folder. Help, data, the EULA and a module
  setup's own files are not code, so they stay.
- **The Windows installers** (full, base-only, tools) run `OVTSuite.exe scrub-module-sources`
  after installing, with setup's own rights. An all-users install can therefore clean
  `{app}\modules`, which the app cannot write. The full installer's `[InstallDelete]` already
  wipes each module folder it reinstalls. A module's own setup wipes its folder too.

Some cases are handled differently:

- **A copy put there by a module's setup.exe** (it has `unins000.exe`) loses its code but keeps
  its uninstaller, so its Apps & features entry still works. The copy then reads "damaged" and
  never loads.
- **No compiled copy yet:** nothing is touched. The source copy is still the module.
- **At the same version,** a compiled copy is always chosen over a source one.
- **On Linux,** a compiled module `.deb` replaces the source one. dpkg removes the files the old
  package owned, and its `postinst` removes any `.py` or `__pycache__` it never owned.

### Withdrawing source downloads

The releases published before compiled packages (0.4.5 to 0.4.9) still offer the EDC15 and
EDC16 modules with their source. Once the release with compiled packages is published, run:

    python tools/build_modules.py scrub-releases              # lists, deletes nothing
    python tools/build_modules.py scrub-releases --confirm    # deletes (gh, signed in)

- **What it deletes:** each untagged `edc15-*` / `edc16-*` `.ovtmod` (full and checksum), plus the
  module's `OVTSuite-BoschEDC1x-*-setup.exe` and `ovtsuite-module-edc1x_*_all.deb` from releases
  without a compiled package of it.
- **`--modules`** names others (`zf6hp,zf8hp,aisin,hvac`).
- **`--bundles`** adds the full and tools installers of those releases (none were published).
- **It refuses `--confirm`** while no published release has a compiled package of every module
  named.
- **What it leaves:**
  - the base installers, deltas and `SHA256SUMS.txt`, whose entries for the deleted files are
    then simply unused;
  - the local `Reference\ModuleReleases`, which the compatibility test needs.

### User interface

- **Tools > Modules...** lists every module the app knows, with its state, version, variant,
  location and signer. It can install from a file or remove a module, and has "Allow unsigned
  modules" for development builds.
- **The limited-support dialog** offers "Install module...", then reopens the file with the
  module.
- **Tools that need a parameter tree** are disabled for a limited session.
- **The flasher** asks before writing an image whose checksum it cannot correct.
- **Opening a `.ovtmod`** installs it.
- **CLI:** `modules`, `install-module [--system] [--allow-unsigned] [--gui]`, `remove-module`.

## Building

`tools/build_modules.py` makes every incremental decision, and all three build scripts use it:

| Command | Does |
|---|---|
| `plan` | Shows what is stale. |
| `stage-base` | Stages the base-only tree that the spec freezes. |
| `packages` | Builds and checks the stale `.ovtmod` files. |
| `install-into` | Lays packages out installed. |
| `freeze-key` | Decides whether PyInstaller can be skipped entirely. |
| `inno` | Writes the installer component lists. |
| `ensure-key` | Creates the signing key on first use and trusts it in `module_keys.json`. |

| `installer-key` | One content hash of an installer's inputs, used to skip unchanged installers. |

**Rebuild rules:**

- A module's package is rebuilt only when its files change, or its version is bumped.
- The checksum variant is rebuilt only when its closure changes.
- The base app is re-frozen only when base files, packaging inputs or the build mode change.
- **An installer is rebuilt only when its inputs change.** The inputs are its payload folder
  (by file contents, not times), the packaging scripts and icons, and its settings: version,
  compression and the module list. Otherwise the existing setup.exe or .deb is kept. The key it
  was built from is kept in `build\installer\stamps\` (Windows) or `build/linux/stamps/`.
  `-RebuildInstallers` or `--rebuild-packages` forces a rebuild.
- **Compiled packages are per interpreter and platform.** Their fingerprints include the tags,
  and the stamps are kept per platform (`build/.cache/modules-cp313-win_amd64.json`), so a Linux
  build in the same checkout neither reuses nor overwrites the Windows packages. Compiled files
  are cached by source hash in `build/.cache/native/`: the checksum variant reuses the full
  variant's, and an unchanged file is not recompiled. A full EDC16 compile takes about 85 s.
- **Compiling needs** Cython and setuptools (in both build venvs' dependencies) and a C compiler:
  MSVC on Windows (Visual Studio or its Build Tools, "Desktop development with C++"), gcc and the
  interpreter's headers on Linux. The Docker image has them, and `build_linux.sh` checks for
  them.
- **A module whose files changed without a version bump** is still packaged, but the build ends
  with a warning naming it. Bump the version in its `module.py` before releasing; otherwise two
  different packages carry the same version.

### build.ps1 (Windows)

- `-Modules zf8hp,edc16`: package only these modules (default `all`).
- `-BaseOnly`: no modules.
- `-Unsigned`: development builds only. Signed builds keep each new module version in
  `Reference\ModuleReleases` for the compatibility test.
- `-RebuildInstallers`: rebuild every requested installer even if unchanged.
- `-Installers full,base,modules,modules-checksum,flasher,sniffer,logger,tools`. The default
  is `full`, and `main` is the old name for it.

Outputs:

| Path | What it is |
|---|---|
| `dist\modules\*.ovtmod` | the module packages |
| `dist\OVTSuite\` | the base app, with every module installed under `modules\` |
| `dist\OVTSuite-<ver>-setup.exe` | full installer, with the components page |
| `dist\OVTSuite-Base-<ver>-setup.exe` | base-only installer |
| `dist\module-installers\OVTSuite-<Module>-<module ver>-setup.exe` | one per module, named with the module's own version, plus `-Checksum` variants |
| `dist\OVT{Flasher,Sniffer,Logger,Tools}-<ver>-setup.exe` | tool installers; checksum modules offered as components |

### Module installer (`packaging/module_installer.iss`)

- **Refuses** unless OVT-Suite, OVT Tools or OVT Flasher is installed (checked from the uninstall
  keys in HKCU and HKLM).
- **Per-user install** (the default) goes into the per-user modules folder, which the Suite and
  the Tools both read. An all-users install goes into the found install's `modules\`.
- **AppId** is `OVT-Suite.Module.<id>`, so variants and upgrades replace each other.
- **Uninstall:** the base uninstaller first runs each module installer's uninstaller in
  `{app}\modules`.

### build_linux.sh / build.py

- Flags: `--modules`, `--base-only`, `--unsigned` and `--rebuild-packages`.
- The base `.deb` no longer carries `/opt/<pkg>/modules`. Each module is
  `dist/module-installers/<pkg>-module-<id>_<module ver>_<arch>.deb`, with `Depends: <pkg>`.
  `<arch>` is `all` for an open-source module and the build's architecture (`amd64`) for a
  compiled one.
- Built and test-installed in a container: the module `.deb` files mount, and uninstall
  cleanly.

## Backwards compatibility

A newer app keeps loading modules built for older ones, as far as possible:

- **`SUPPORTED_API` only grows.** A level is dropped only after a release that warned about it,
  never silently. A module too old or too new is refused with a reason naming the levels.
- **Hooks get only the arguments they accept.** The app calls every hook through
  `ModuleSpec.call`, which passes only the keyword arguments the module's function declares. The
  app can add an argument (as HVAC's `image=` was) without breaking a module written before it.
- **Later capabilities are optional.** A method or option added after a module was written is
  looked up with a fallback, never assumed present. An example is the EDC16 provider's
  `eeprom_locate`.
- **`ModuleSpec` only gains fields, with defaults.**
- **Released modules are tested against every newer app.** Signed builds keep each new module
  version in `Reference\ModuleReleases`. `tests/test_module_compat.py` installs each one into a
  base-only copy of the current app, in a clean interpreter, imports every hook and calls its
  checksum hook. A base change that breaks an older module fails there before a release.

The weak point until the SDK exists (`docs/MODULE_SDK_DESIGN.md`) is that modules import base
internals. The compatibility test is what catches a break today.

## Still to do

1. **Windows run.** Run `.\build.ps1 -Installers full,base,modules,tools` and install the results.
   I could only syntax-check `build.ps1`, and the Inno scripts could not be compiled here. On the
   first build, the signing key is created at `%USERPROFILE%\.ovt-signing\module-key.pem`
   (**back it up**), and `ovtsuite\data\module_keys.json` is written (**commit it**).
2. **Search-label cache.** It is EDC16-module data now. It goes stale on any `gui.py` change, as
   before, and `build.py --regen` rebuilds it.
3. **External definitions.** XDF, OLS, KP and A2L import onto any binary is next. It plugs in as
   a definition provider for limited sessions.
4. **Third-party modules and encryption.** Proposed in `docs/MODULE_SDK_DESIGN.md`; waiting on the
   decisions listed there.
