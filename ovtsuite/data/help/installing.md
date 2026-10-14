---
title: Installing and updating
order: 12
---
# Installing and updating

This page covers every installer OVT-Suite comes in, what each choice in
them does, how to update and uninstall, and where the app keeps your
settings. For adding modules from inside the app, see [Modules](modules.md).

## Which installer

| Installer (Windows) | Installs |
|---|---|
| `OVTSuite-<version>-setup.exe` | The app, with a **Components** page listing every module — all ticked. |
| `OVTSuite-Base-<version>-setup.exe` | The base app only. Add modules later. |
| `OVTSuite-<Module>-<module version>-setup.exe` | One module, for an installed OVT-Suite or OVT Tools. `...-Checksum-...` is that module's checksum-only variant. |
| `OVTFlasher-<version>-setup.exe`, `OVTSniffer-...`, `OVTLogger-...` | One standalone tool, without the main app. |
| `OVTTools-<version>-setup.exe` | **OVT Tools**: the flasher, sniffer and logger together, without the main app. |

On Linux there is a `.deb` (and an `.rpm`) for the app, and a `.deb` per
module — see [Linux](#linux).

## Windows: the main installer

1. Run `OVTSuite-<version>-setup.exe`.
2. **Install for you, or for all users.** Setup asks first. *For you* (the
   default) needs no administrator rights and installs into
   `%LOCALAPPDATA%\Programs\OVT-Suite`; *for all users* asks for
   administrator rights and installs into `C:\Program Files\OVT-Suite`.
3. **Folder.** Accept the folder or pick another.
4. **Components** (full installer only):

   | Setup type | Installs |
   |---|---|
   | **Full installation (every module)** | The app and every module. |
   | **Base app only (add modules later)** | The app alone. |
   | **Choose modules** | The app and the modules you tick. |

   The first component, *OVT-Suite: identify, hex editor, compare, flasher,
   logger, sniffer*, is always installed. Each module is listed with its
   version and what it covers.
5. **Additional tasks:**

   | Task | Default | Does |
   |---|---|---|
   | **Create a desktop shortcut** | off | A desktop shortcut to OVT-Suite. |
   | **Open OVT projects (.ovt) with OVT-Suite** | on | Double-clicking a project opens it in the app (in the window already open, when the app is running), and projects show the OVT project icon. The extension belongs to OVT-Suite alone. |
   | **Associate .bin files with OVT-Suite** | off | Adds OVT-Suite as the program for `.bin` files. `.bin` is used by many programs, so it is off unless you want it. |
   | **Download the EDC16 boot-sector database ...** | off | See [The boot-sector database](#the-boot-sector-database). Offered only when the installer was built with it. |

   Module packages (`.ovtmod`) are always associated: opening one installs
   it (see [Modules](modules.md#installing-a-module)).

   Files of earlier versions (`.edc16`, `.6hp` ...) are no longer associated;
   an update removes the associations an earlier version made. They are
   brought into a project with **File > Import**.
6. **Install**, then **Launch OVT-Suite** if you want to start it at once.

The Start menu gets an **OVT-Suite** folder with **OVT-Suite**, **OVT
Flasher**, **OVT Sniffer**, **OVT Logger**, **OVT Simulator** and
**Uninstall OVT-Suite**. Each tool opens on its own, with its own taskbar
button and icon; pinning one to the taskbar pins that tool.

The modules the full installer installs go into the app's own `modules`
folder; the Module Manager shows them as installed **for all users**, or
**for you (with the app)** when OVT-Suite was installed for you only.

## Windows: the tool installers

`OVTFlasher`, `OVTSniffer`, `OVTLogger` and `OVTTools` install the
standalone tools without the main app, each into its own folder
(`...\OVT Flasher`, `...\OVT Tools` and so on), with its own entry in the
system's app list. They can sit beside the main app or be used instead of
it.

- A single tool's shortcut goes straight into the Start menu; **OVT Tools**
  gets an **OVT Tools** folder with all three. **Create desktop shortcuts**
  is offered, off by default.
- The flasher corrects a file's checksum before writing it, which needs that
  control unit's module. These installers therefore offer every module's
  **checksum-only** variant as a component, all ticked (when the installer
  was built with them).
- The flasher installers (**OVT Flasher**, **OVT Tools**) also offer the
  boot-sector database download.

## Module installers

`OVTSuite-<Module>-<version>-setup.exe` adds one module:

- It needs OVT-Suite, OVT Tools or OVT Flasher installed first. Without one
  it stops: *OVT-Suite is not installed on this computer. Install OVT-Suite
  (or OVT Tools) first, then run this module's installer again.*
- **Installed for you** (the default, no administrator rights): into your
  own modules folder, `%LOCALAPPDATA%\OVT-Suite\modules\<module>`, which
  OVT-Suite and the tools both read.
- **Installed for all users**: into the `modules` folder of the all-users
  OVT-Suite (or OVT Tools) it finds. If OVT-Suite is installed for you only,
  it says so and asks you to run it again and install for you.
- It is listed in the app list as *OVT-Suite \<Module\> module \<version\>*.
  The full and checksum-only installers of one module replace each other, and
  a newer version replaces the older.

The app checks every module's signature and files each time it starts; the
installer only places the files.

## The boot-sector database

The EDC16 flasher's *virtual boot read* needs a database of EDC16 boot
sectors. It is not part of any installer or module: tick **Download the EDC16
boot-sector database** in the installer and it is downloaded during setup,
checked against the expected checksum, and put in an `addons` folder in the
install. If the download fails, setup says so and carries on; the flasher
works without it, and you can run setup again later to retry.

Without the installer (Linux, or to add it later), copy `edc16_bootdb.bin`
into the `addons` folder in the app's settings folder:
`%APPDATA%\OVT-Suite\addons` on Windows, `~/.config/ovtsuite/addons` on Linux.

## Linux

Install the app's package, and a package for each module you want:

```
sudo apt install ./ovtsuite_<version>-1_amd64.deb
sudo apt install ./ovtsuite-module-vdo_<module version>-1_all.deb    # one per module
```

On Fedora, RHEL / Alma / Rocky or openSUSE, install the `.rpm` with
`sudo dnf install ./ovtsuite-<version>-1.x86_64.rpm` (or `zypper install`).
There are no module `.rpm` packages: add modules from inside the app with
**Tools > Modules...** and a `.ovtmod` file.

The package installs:

| Where | What |
|---|---|
| `/opt/ovtsuite/` | The app. Module packages add `/opt/ovtsuite/modules/<module>/`. |
| `/usr/bin/ovtsuite` | The launcher: `ovtsuite` with no arguments opens the main window. |
| Application menu | **OVT-Suite**, with its icon. |
| File type | `.ovt` projects are known by name and by content (a renamed one too), open with OVT-Suite, and show the OVT project icon. |
| `/usr/lib/udev/rules.d/70-ovtsuite.rules` | Cable access (below). |
| `/usr/share/doc/ovtsuite/copyright` | The third-party notices (see [About and credits](about.md)). |

The standalone tools have no menu entries on Linux; start them from a
terminal with `ovtsuite --flasher`, `ovtsuite --logger`, `ovtsuite
--sniffer` or `ovtsuite --simulator`.

### Cable access (udev rule)

The rule gives the user logged in at the computer access to the cables,
without root, without joining the `dialout` or `plugdev` groups, and without
logging out:

- the Tactrix OpenPort 2.0 (and clones) — and it tells ModemManager to leave
  the OpenPort alone, which would otherwise probe it on plug-in and fight the
  app for it;
- FTDI K-line cables, both directly over USB and as `/dev/ttyUSB*`;
- CH340 / CH341 / CH9102 (WCH) K-line cables as `/dev/ttyUSB*`.

Installing the package reloads the rules. **Unplug and replug a cable that
was already connected** so the rule applies to it. Someone working only over
SSH (not logged in at the computer) is not covered by the rule; they need the
usual `dialout` group for the serial cables.

See [Cables and interfaces](cables.md) for the cables themselves.

## Updating

OVT-Suite checks for a newer version itself and, on Windows, can download
and install it (and your modules' updates) for you: see
[Checking for updates](updates.md). To update by hand:

- **Windows:** run the new installer over the old one. It recognises the
  existing install and updates it in place, in the same folder; your
  settings, recent files and modules you installed for yourself are kept.
  The full installer re-installs the modules you tick. Module installers
  update their module the same way.
- **Linux:** install the new package with `apt install` (or `dnf`) as above.
- **Modules** have their own versions. An updated module installed while the
  app is running is used after a restart (see
  [Modules](modules.md#when-a-new-module-takes-effect)). A newer app keeps
  loading modules made for older versions wherever it can.
- **From EDC16-Tuner** (the app's earlier name): the first run carries your
  connection and flash settings across once, and the recent-files lists are
  read from the old settings folder until the app saves new ones. The
  Windows installer moves an install still in the old default folder and
  removes the old Start menu entries and shortcuts.

## Uninstalling

- **Windows:** **Settings > Apps**, **OVT-Suite**, **Uninstall** (or
  **Uninstall OVT-Suite** in the Start menu). This removes the app and the
  modules in its own folder, running each module installer's own uninstaller
  first. Modules installed **for you** stay, because the standalone tools may
  use them; remove them from the Module Manager first, or delete
  `%LOCALAPPDATA%\OVT-Suite\modules`. Your settings folder is left as it is.
- The tool installers and module installers each uninstall on their own from
  the app list.
- **Linux:** `sudo apt remove ovtsuite` (and `ovtsuite-module-<id>` for
  each module), or `sudo dnf remove ovtsuite`.

## Where the app keeps things

**Your settings and lists** — in your settings folder:
`%APPDATA%\OVT-Suite` on Windows, `~/.config/ovtsuite` on Linux:

| File / folder | Holds |
|---|---|
| `recent.json`, `recent_comparisons.json` | **File > Open Recent** (projects) and **File > Import recent as comparison** (12 each). |
| `settings.json` | The last folder used for each kind of file (binaries, logs, exports, EEPROMs, patches ...). |
| `addons/` | Optional add-ons, such as the boot-sector database. |
| `diagnostics.log`, `ovtsuite-fault.log` | Diagnostic logs written when the app hangs or crashes — attach them to a bug report. |

**Preferences** — connection and flash settings, the projects and tabs to
restore, recently closed projects, the parameter-tree options — are kept by
Qt: in the registry under `HKEY_CURRENT_USER\Software\OVT-Suite\OVT-Suite` on Windows,
in `~/.config/OVT-Suite/OVT-Suite.conf` on Linux.

**Your modules** (installed for you): `%LOCALAPPDATA%\OVT-Suite\modules`, or
`~/.local/share/ovtsuite/modules` on Linux.

**A project's saved versions, logs, patches and attachments** are all inside
its `.ovt`: copying the `.ovt` copies them too. See
[Projects](projects.md) and [Editing and saving](editing.md).

**Crash log:** if the app fails to start, it writes `ovtsuite-crash.log`
beside the program, or in your settings folder when the program's folder is
read-only.
