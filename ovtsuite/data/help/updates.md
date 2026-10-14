---
title: Checking for updates
order: 13
context: UpdateDialog
---
# Checking for updates

OVT-Suite can tell you when a newer version is published, and on Windows it
can download and install it for you — the app and the modules you have
installed.

## When it checks

- **At start-up**, a few seconds after the window opens, at most **once a
  day**. Turn this off in **Tools > Options > General > Check for updates at
  start-up**. When the computer is offline the start-up check says nothing.
- **Help > Check for Updates...** checks at any time, and says so when you
  already have the latest version or when the check failed.

The check asks GitHub for the latest release of OVT-Suite. Pre-releases and
drafts are never offered. Nothing else is sent — no files, no settings.

## The update window

The window lists what the new release brings:

| Column | Meaning |
|---|---|
| **Update** | The app itself, and every module you have installed that has a newer version in the release. Modules you do not have are not listed. |
| **Installed** / **New** | The version you have and the new one. |
| **Installed by** | How the update is installed on Windows: the **app installer**, a **module package** (installed inside the app), or the **module installer**. |

Below it are the release notes. The buttons:

- **Download and install** — Windows only (see below).
- **Open release page** — the release on GitHub, with every download.
- **Skip this version** — the start-up check does not offer this release
  again. A later release (or a module added to this one) is offered as
  usual, and **Help > Check for Updates** always shows it.
- **Remind me later** — nothing now; the start-up check offers it again the
  next day.

## Windows: download and install

This is available when OVT-Suite was installed with its setup program.
Nothing is installed until you click **Download and install**, and nothing
of yours is closed without asking.

1. The files are downloaded with a progress bar (**Cancel** stops it).
2. Each file is checked: its size, and its SHA-256 against the release's
   **signed checksum list** (`SHA256SUMS.txt`, signed with the same key as the
   modules). A release without a list is checked against GitHub's own
   checksum. If nothing can vouch for a file, the app says so and asks
   before using it; a list whose signature is wrong stops the update. Module
   packages are also checked against their own signature, as always.
3. **Install the update now?** OVT-Suite will close. Choose **Later** to keep
   the downloaded files: the next **Check for Updates** installs them without
   downloading them again.
4. Every OVT-Suite window closes the normal way, so you are asked about
   unsaved work (**Save selected / Don't save / Cancel**). **Cancel** — or a
   save that fails — stops the update; nothing is installed and the files are
   kept.
5. If other copies of OVT-Suite or its tools (OVT Flasher, Logger, Sniffer,
   Simulator, the MCP server) are running, you are asked to close them first.
6. Module packages are installed for you, then the installer runs on its own
   (no questions) and OVT-Suite starts again when it is done.

When only modules have a newer version and they come as packages, they are
installed straight away without closing the app; restart it to use them.

The installers themselves also wait for every running copy of OVT-Suite to
close before they replace any file, so running a setup program by hand while
the app is open is safe too.

## Linux and other copies

The Linux packages, a copy run from source and a Python install are only
told about the update: use **Open release page**, then install the new
package the way you installed the old one (`sudo apt install ./ovtsuite_...deb`,
`sudo dnf install ./ovtsuite-...rpm`, `git pull`, `pip install -U`). See
[Installing and updating](installing.md#updating).
