# Updates: full installers and delta packages

How an installed copy of OVT-Suite updates itself from GitHub, and what the
build has to publish for it. The code: `ovtsuite/updates.py` (checking,
downloading, the installer chain), `ovtsuite/delta_update.py` (delta packages:
build, chain, pre-flight, apply), `ovtsuite/gui_update.py` (the window side),
`tools/app_release.py` (build side), `packaging/entry.py` (`--apply-update`).
The user's view is the help page `ovtsuite/data/help/updates.md`.

## Two ways to update the app

| | Full installer | Delta packages |
|---|---|---|
| Asset | `OVTSuite-Base-<v>-setup.exe` (or the full one) | `OVTSuite-Update-<a>-to-<b>.zip`, one per release |
| Size | the whole app (~100 MB) | the files that changed |
| Several releases behind | one download | a chain of packages, applied in turn |
| Install kinds | per user and all users (elevates) | per user only |
| Applied by | Inno Setup, silently (`install-update.cmd`) | a helper: the app's own exe, run from a temporary copy |

The app prefers the delta chain and falls back to the full installer whenever
the chain is not usable: no chain to the latest release, a hop missing from a
signed checksum list, a package whose own signature does not check out, the
installed files are not what the first hop expects, an all-users install, a
non-Inno install (Linux, zip, source, a standalone tool's own install), the
packages together are not smaller than the installer, or a delta update of
the same release already failed on this machine (`updates/delta_failed`).

Modules are unaffected: they keep their own `.ovtmod` packages and module
installers. A module installer that has to run after the app is updated runs
after the delta helper too (its batch file is handed to the helper).

## The app manifest

`app-manifest.json` in the app folder, written by
`tools/app_release.py manifest` into `dist\OVTSuite` after everything that
changes a shipped file (the 32-bit bridge copy, Authenticode) and before the
installers are packed, and shipped by the installer:

```json
{"app": "OVT-Suite", "format": 1, "version": "0.4.8",
 "files": {"OVTSuite.exe": "<sha256>", "_internal/...": "<sha256>", ...}}
```

Every file under the folder except `modules\`, the update's own working
folders (`.update-backup`, `.update-stage`), the manifest itself, the
uninstaller (`unins*.exe/.dat/.msg`) and top-level `*.log`. Paths use `/`.
It is signed with the release key (`app-manifest.json.sig`, Ed25519, the key
that signs the modules and `SHA256SUMS.txt`). The bytes are deterministic, so
an unchanged build does not rebuild its installers.

The client does not trust the installed manifest for anything: it hashes the
installed files it is about to replace.

## The delta package

`tools/app_release.py delta --to dist\OVTSuite --out dist` compares the new
build's manifest with an archived release's and writes
`OVTSuite-Update-<from>-to-<to>.zip`:

```
delta.json          what the package does (below)
delta.json.sig      Ed25519 signature over delta.json's exact bytes (required)
app-manifest.json   the new version's manifest (+ .sig), written into the app
payload/<path>      every file that is new or changed
```

`delta.json`:

| Key | |
|---|---|
| `format`, `app` | 1, `OVT-Suite` |
| `from`, `to` | the versions |
| `from_files` | for every file the package writes or deletes, its hash in `from` (`null`: it did not exist) |
| `changed`, `deleted` | the files written (all in `payload/`) and removed |
| `to_manifest` | the whole new manifest (`version`, `files`) |
| `manifest_sha256` | the hash of the `app-manifest.json` bytes in the zip |

Files are compared by hash, so a file that did not change between two builds
(a DLL, a Qt plugin, a data file, an unchanged launcher) stays out of the
package; `OVTSuite.exe`, which carries all the Python code, is in every delta
of a release that changed any code -- see "One copy of the code" below. Every
payload file is checked against the new manifest while it is packed, so a
manifest written before a later change fails the build.

`open_delta` refuses a package unless: its signature is by a key in
`module_keys.json` (no unsigned fallback), every path is relative and inside
the app (never `modules\`), `changed`/`deleted`/`from_files`/`to_manifest`
agree with each other, the payload is exactly `changed`, and the manifest in
the zip is the one `manifest_sha256` names and lists `to_manifest`.

## The release archive

A delta is made against the previous release's manifest -- only the manifest:
the payload comes from the new build. A signed build keeps its manifest in
`Reference\AppReleases\<version>\` (beside `Reference\ModuleReleases`; the
folder is not in git, back it up with the signing key). A later build of the
same version replaces it, so the archive holds the build that was released --
**publish the last build of a version**. If the archive and the published
build ever disagree, users' pre-flight fails and they get the full installer:
safe, just bigger.

Every signed one-folder build runs `tools/app_release.py auto` after the
installers -- nothing to pass, no version to name:

1. The delta packages an earlier build left in `dist\` are removed (this
   build makes its own; a stale one would otherwise be checksummed and
   uploaded).
2. The previous release is chosen: the **highest archived version strictly
   older than this build**, compared as the app compares versions
   (`updates.compare_versions`: numeric, so 0.4.10 follows 0.4.9). Never this
   version itself -- its folder is in the archive from its own last build --
   and never a prerelease: the app chains deltas only through non-prerelease
   releases (`delta_hops`), so a delta from a prerelease would strand everyone
   on the release before it. A prerelease build is made against the last
   release before it, too.
3. `OVTSuite-Update-<previous>-to-<version>.zip` is built and signed.
4. This build's manifest is archived as `<version>\`, replacing the one an
   earlier build of the same version left.

So **rebuilding a version** (files changed, version not raised yet) rebuilds
its delta from the same previous release against the new build and replaces
its archived manifest -- the archive always describes the last build.
**The first release** with an archived manifest (0.4.8) has nothing older to
start from: the build says so, makes no delta and archives it. Deltas start
with 0.4.9 (from 0.4.8).

Overrides: `-DeltaFrom 0.4.8,0.4.9` makes deltas from those releases instead
(a direct delta from an older release shortens chains; one not older than the
build is skipped with a warning); `-DeltaFrom none` makes none but still
archives; `-NoDelta` makes none and leaves the archive alone (a test build
that will not be released); `-Unsigned` builds make none and archive nothing
(not a release; the app would refuse the packages). The deltas go into
`SHA256SUMS.txt` like every other asset. **Upload each delta with the release
it updates to** -- the client only accepts `OVTSuite-Update-<a>-to-<b>.zip`
from release `v<b>`.

## Finding a chain

`updates.fetch_releases` reads `/releases?per_page=100`, page by page, until
the page reaching back to the installed version. `delta_update.find_chain`
takes every non-draft, non-prerelease release's delta assets as edges
`a -> b` (only forward, only from release `b`) and finds the path from the
installed version to the latest with the fewest steps, then the fewest bytes.

## Pre-flight (the app is still open)

1. Each hop is downloaded to `%TEMP%\OVTSuite-update-<latest>` and verified
   against **its own release's** `SHA256SUMS.txt`, which must be signed.
2. Each package is opened and checked (above), and every payload file hashed.
3. The chain is folded into one change (`compose`): each hop's `from_files`
   must be what the hops before it leave (or, for files they did not touch,
   what the previous hop's `to_manifest` says); a file written by several
   hops is taken from the last; one added then removed is removed.
4. The installed files are checked against what the first hop expects. A
   file the update adds may be there already (it is replaced).

Any failure here: the full installer is downloaded instead. Nothing has been
closed or changed.

## Applying: the helper

The app's own `OVTSuite.exe` does the work, because it already carries
Python, the signature check and this code -- but it cannot run from the
folder it replaces. So the app lays out a copy of its folder in
`%TEMP%\OVTSuite-apply-<version>` (`prepare_runner`): **hard links** for
everything (instant, no space; `%TEMP%` and `%LOCALAPPDATA%\Programs` are on
the same volume in practice), **real copies** for every file the update
replaces or deletes, and copies wherever a link cannot be made. The helper
therefore never runs from a file that is being moved, and the old files'
backups can be deleted afterwards. The alternatives considered:

* *Run the helper from the app folder*: Windows cannot replace an exe or DLL
  that is mapped.
* *A PowerShell or batch script*: no signature check, no rollback worth the
  name, and not testable here.
* *A separate tiny updater exe*: another thing to build, sign and keep in
  step; it could not reuse the module-signature code.
* *A full copy of the app folder*: ~150 MB copied on every update; hard links
  give the same isolation for free.

Plan (`delta-plan.json`, beside the downloads): the app folder, the exe to
start again, this process's id, the packages, the versions, the module
installers' batch file (if any), where to put the result and the log.

`OVTSuite.exe --apply-update <plan>` (handled first thing in
`packaging/entry.py`: it opens no window and does **not** take the app mutex):

1. Wait (up to two minutes) for the app's process to exit and for the app
   mutex `OVTSuiteAppRunning` to be free -- every copy of the app and its
   tools closed.
2. Pre-flight again, from scratch (signatures, payload, chain, installed files).
3. Stage every new file in `{app}\.update-stage` (same volume: moving in is a
   rename), checking each hash again.
4. For each file, in turn: move the old one to `{app}\.update-backup`, move
   the new one in (retried briefly when Windows says the file is in use);
   delete what the release removed (into the backup too); write the new
   `app-manifest.json`.
5. Check every written file's hash; update the Inno uninstall entry
   (`HKCU`, else `HKLM`, `Software\Microsoft\Windows\CurrentVersion\Uninstall\{AppId}_is1`,
   only when its `InstallLocation` is this folder): `DisplayVersion`,
   `DisplayName`, `VersionMajor`/`VersionMinor`.
6. On **any** failure in 3-5: every moved file is put back from the backup,
   new ones removed, the registry values restored; the result says
   "restored".
7. Write the result (`update-result.json` in the app's config folder), then
   start the module installers' batch file, or the app (as the same,
   non-elevated user).

At its next start the app reads and deletes the result: success goes to the
status bar; a failure is shown, and that release is marked so the next
**Check for Updates** uses the full installer. It also removes
`.update-backup`, `.update-stage` and the helper's copy.

## The installers and delta-updated folders

A delta-updated folder differs from what the installer's uninstall log
records. `installer.iss`:

* `[InstallDelete]` removes `{app}\_internal` (and the update's working
  folders) before a full install, so no file a later release dropped
  survives a reinstall. Never `{app}\modules`.
* `[UninstallDelete]` removes `{app}\_internal`, top-level `*.exe`, the
  manifest and the working folders, so files a delta added go on uninstall.

Files the installer itself adds from `packaging\` (`ovt_project.ico`) are not
in the manifest; a change to one needs a full installer release.

The tool-only installers (`tool_installer.iss`) leave the manifest out; a
tool's own install is never delta-updated. They ship `OVTSuite.exe` (every
launcher starts it), so `updates.classify_install` tells the two apart by the
manifest: an uninstaller beside the exe is the app's own install only when
`OVTSuite.exe` **and** `app-manifest.json` are there too (or the app's
AppId's `InstallLocation` names the folder). Otherwise a standalone OVT
Flasher/Tools install would count as the app's, and the update would run the
app's installer -- installing the whole app into a second folder.

## One copy of the code (the tool exes)

All the Python code is in **one** place: the PYZ archive PyInstaller appends to
`OVTSuite.exe` (`packaging/ovtsuite.spec`, the default `noarchive=False`).
`OVTFlasher.exe`, `OVTSniffer.exe`, `OVTLogger.exe`, `OVTSimulator.exe` and
`OVTMcp.exe` are not PyInstaller exes: each is a small .NET Framework program
(`packaging/launcher/Launcher.cs`) that starts

    "<its folder>\OVTSuite.exe" --tool <name> <its own arguments, verbatim>

and `packaging/entry.py` (`ovtsuite/launcher.py`) runs that tool exactly as
the tool exes did when they were the app under another name: the same CLI
entry (`--flasher`, ..., `mcp`), the app mutex, the crash and fault logs. The
tool's AppUserModelID and window icon were always set from the tool name by
its window code (`gui._run_tool`), and the AUMID belongs to the process that
owns the window -- `OVTSuite.exe` -- so the taskbar still groups each tool
under its own shortcut.

Why not PyInstaller exes: every `EXE()` appends a PKG that **contains the
PYZ** (PyInstaller 6 `building/api.py`), so six exes were six copies of the
code (several MB each), and a delta carried all six whenever any code changed.
`MERGE()` shares binaries and data between Analyses, never the PYZ. The
interim fix, `noarchive=True` (every module a `.pyc` file in `_internal`,
shared by all exes), put thousands of small files on the start-up path; it is
reverted.

**The launchers.** One C# source, built per tool by `tools/build_launchers.py`
(build.ps1 step "Tool launchers", after PyInstaller, before the manifest, the
Authenticode step and the installers) with a generated `LauncherInfo.cs`
(the tool, its title, console or not) and the tool's icon (`/win32icon`). The
compiler is the C# compiler of the .NET Framework 4.x that is part of
Windows 10/11 (`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`,
C# 5; `OVT_CSC` overrides): nothing to install on the build machine, and the
launchers need only the .NET Framework 4 runtime, also part of Windows 10/11
(Qt 6 needs Windows 10 anyway). The alternatives: a C launcher needs MSVC,
clang or MinGW, none of which a build machine is guaranteed to have; and
PyInstaller has nothing smaller than its bootloader, which needs the archive.

* *Windowed tools* (`/target:winexe`): start `OVTSuite.exe` and exit at once
  (no console, nothing left waiting). They hand on how they were started --
  the shortcut's show state, and the shortcut / AppUserModelID the shell
  names in `STARTUPINFO` -- and allow the new process to take the foreground.
* *OVTMcp* (`/target:exe`): an MCP client talks to the server over
  stdin/stdout, so this one is a console program. It starts `OVTSuite.exe`
  with **its own** standard handles (`STARTF_USESTDHANDLES`, made
  inheritable), waits, and returns the child's exit code; the child is in a
  job object that kills it if the launcher is killed, so a client that kills
  the launcher leaves no server behind. `OVTSuite.exe` is a GUI-subsystem
  (windowed) program, but a GUI process given valid handles this way has them
  as its standard handles, the C runtime opens fds 0-2 on them, and Python
  opens `sys.stdin`/`sys.stdout` on those (PyInstaller 6's windowed bootloader
  leaves stdio to Python: `configure_c_stdio`; nothing in its loader replaces
  the streams). `mcp_server.serve` wraps them as UTF-8. A crash in MCP mode is
  logged to stderr and the crash log, with no dialog. A console *window* as
  stdin (typing JSON-RPC by hand in cmd) is not the supported case: use pipes,
  or `python -m ovtsuite mcp` from source.
* A launcher's version resource is its **own** (`LAUNCHER_VERSION`, 1.0.0.0),
  with the tool's name as FileDescription (what Task Manager shows) and the
  app's ProductName and copyright -- not the app's version. The .NET
  Framework csc has no `/deterministic`, so `build_launchers.py` compiles a
  launcher only when its inputs (source, constants, icon) change and reuses
  the cached exe otherwise (`build\launchers`); after `-CodeSign` the signed
  launchers are kept in the cache (`adopt`), so signtool skips them next time.
  An unchanged launcher therefore keeps its bytes from release to release
  and is not in the delta. `-Clean` empties the cache: the next build's
  launchers are new bytes and ship once in its delta (a few KB each).

**The size trade-off.** Any code change now ships `OVTSuite.exe` whole: the
bootloader (~0.3 MB) plus the PYZ -- the base app's code is ~2.9 MB compressed
(230 modules, measured from the source), the standard library and the
pure-Python dependencies the analysis pulls in (cryptography, pyserial, pyusb,
pyftdi) a few MB more, so roughly 6-10 MB, little of it compressible further
in the zip. With `noarchive` a delta shipped only the changed `.pyc` files
(often under 1 MB) -- but six exes with a fresh version resource each, and a
slower start every time. Against a ~100 MB installer the delta stays small,
and it is one file to replace.

**The helper** (`--apply-update`) runs from a copy of the app folder in which
`OVTSuite.exe` is a real copy (`prepare` always copies it; everything else is
hard-linked) -- with the code inside the exe the copy is self-contained, and
nothing it runs is a file the update moves. After the update the app is
started again with `launcher.relaunch_exe()`: a tool started through its
launcher comes back through that launcher (OVT Flasher, not the main app).

## Testing

`tests/test_delta_update.py` builds fake releases in a temporary folder and
covers the manifest, building and opening deltas (tampering, unsigned,
paths), chain finding, pre-flight, applying a two-step chain, a failure at
every single file move (the folder ends exactly as it was), the registry
entry (a fake `winreg`), the helper's plan end to end, the runner copy (links
vs copies), the build tool, and the window side (offscreen). What cannot be
tested off Windows: the real helper process (PID and mutex waits, hard links
on NTFS, moving files Windows has mapped), the registry, the C# launchers
(compiling, stdio hand-down, the job object) and the Inno scripts -- try a
delta update between two signed builds on a Windows machine, and an MCP
client against `OVTMcp.exe`, before relying on them. `tests/test_launcher.py`
covers the Python side: `--tool` dispatch in `packaging/entry.py`, argument
pass-through (the C# `ArgumentsOf` is mirrored by
`launcher.strip_program_name`), the relaunch target, and
`tools/build_launchers.py` (constants, the csc command line, caching and
`adopt`, with a stand-in compiler).
