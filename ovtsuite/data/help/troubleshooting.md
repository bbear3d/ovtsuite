---
title: Troubleshooting
order: 90
---
# Troubleshooting

The messages below are quoted as the app shows them (in *italics*), with what
they mean and what to do. For a message not listed here, see the page for the
window it came from — press **F1** in that window.

## Opening and identifying files

| Message | Meaning and fix |
|---|---|
| **Limited support** — *\<what the file is\>. Install the \<name\> module for full support.* | The file was identified, but the module for that kind of control unit is not installed. It opens in the hex editor; comparing, patches and the flasher still work. Click **Install module...** in the message, or use **Tools > Modules...** (see [Modules](modules.md#when-a-files-module-is-missing)). |
| *The \<name\> module installed here is the checksum-only variant; install the full \<name\> module for full support.* | Only the checksum-only variant is installed. Checksums are verified and corrected, but there are no definitions: install the full module. |
| *The \<name\> module is installed but ...* | The module is present but refused; the rest of the sentence says why. See [When a module is refused](modules.md#when-a-module-is-refused). |
| *No OVT-Suite module supports this binary.* | The file is not one the app recognises. Check it is a complete read (not a partial dump) of a supported unit. It still opens in the hex editor. |
| *This binary has no parameter definition.* | The file was recognised but no definition matches it. |
| **Partially-decoded .sgo container** — *This .sgo holds an EDC16 image. Its sections are decoded only when the EDC16 module's checksum confirms the decode, so they are shown as 0xFF filler here.* | Install the Bosch EDC16 module and open the file again. |
| **Partially-decoded .sgo container** — *N of M sections in this .sgo could not be decoded (unrecognized cipher) and are shown as 0xFF filler* | Those sections use an encryption the app does not know. The rest of the file is usable. |
| **Climatronic: notes on this file**, **VDO cluster: notes on this file** or **ZF 8HP: notes on this file** | What the module found when it opened the file: an unknown Climatronic firmware (see [4F/4L HVAC](modules/hvac/variables.md#firmware-the-module-does-not-know)), a cluster family whose definition is extrapolated or whose ROM build is unknown (see [VDO Cluster](modules/vdo/index.md#supported-clusters)), or how a ZF 8HP file's parameters were found. |
| **Different platform** — *A comparison has to be the same platform as the primary.* | You cannot compare, say, an EDC16 file with a ZF 6HP file. **Import it into a new project** opens it on its own instead. |

## Checksums

| Message | Meaning and fix |
|---|---|
| *This \<platform\> image has no checksum layout the \<name\> module supports (need ...)* | **Tools > Verify checksums...** found no checksum it can check in this file; the message names the platform and module, and what that module needs (for example a 1 MB EDC16U1 or 2 MB EDC16U31/U34 image with its calibration marker). The app never guesses a checksum layout: if it cannot read the layout from the file itself, it declines. |
| *The internal flash checksum of this Climatronic firmware (\<part\>) is not mapped yet in the 4F/4L HVAC module ...* | The Climatronic firmware's checksum location is not known for this part number, so it cannot be verified or corrected; saving leaves it as it is. See [4F/4L HVAC](modules/hvac/variables.md#the-checksum). |
| *\<file\>. Install the \<name\> module for checksum support.* | The module that knows this file's checksum is not installed. |
| **Checksums cannot be verified** | The checksum layout could not be read from this file, so its state is unknown — not necessarily wrong. |
| **Checksums need correction** — *... Correct them now? (Save also corrects automatically.)* | The file was edited, or came from a tool that did not fix it. **Yes**, then save; or just save. |
| **Checksums not corrected** | The correction was refused, with the reason. Do not flash the file until this is resolved. |

Checksums are corrected on **save** only when the file's module (full or
checksum-only) is installed.

## Saving

| Message | Meaning and fix |
|---|---|
| **Not saved** — *\<file\> could not be saved: ... Your changes are still open and unsaved.* | The file could not be written: no permission, the disk is full, or another program has it open. Save the project somewhere else with **File > Save As...**. |

## Cables and connecting

Set-up for each cable is on [Cables and interfaces](cables.md) — see
[K-line (KKL) cables](cables/kline.md) and [J2534 devices and the OpenPort 2.0](cables/j2534.md).
More flasher-specific cases are in
[Troubleshooting the flasher](flashing/troubleshooting.md).

| What you see | Fix |
|---|---|
| The **Port** / **Device** list says *(no serial ports or J2534 devices found)* or *(no J2534 device or OpenPort found)* | Plug the cable in and press **Refresh**. On Windows, check it appears in Device Manager and that its J2534 driver is installed. On Linux, after installing the package, unplug and replug the cable once so the access rule applies (see [Installing](installing.md#cable-access-udev-rule)). |
| *... OpenPort: pyusb is not installed* after the port list text | Only when running from source: install the `pyusb` package for the direct OpenPort route. |
| *The flashing module could not be loaded (...). It needs the 'pyserial' package.* | Only when running from source: install `pyserial`. |
| *K-line read timed out* | The unit did not answer in time. Check the ignition is on, the right ECU is chosen, and the cable; try a lower **Speed**. |
| *fast init: no StartCommunication response* | The unit did not answer the fast init. Some older units need the slow (5-baud) init: tick **Disable Fast Init**. |
| *Unexpected sync byte: expected 0x55, got 0x...* or *KW1281: cannot sync (repeated 0x55)* | The slow init was garbled — usually the cable or its timing. Try again, or another cable. |
| The connection drops during a read or write | Use a lower speed, a shorter or better cable, and keep the battery on a charger. |

## Reading and writing control units

The details for each unit are on [ECU read / write](flashing.md), in
[Troubleshooting the flasher](flashing/troubleshooting.md) and
[Backups and recovery](flashing/backups-and-recovery.md), and on the module
pages.

| Message | Meaning and fix |
|---|---|
| **Checksum will not be corrected** — *... The \<platform\> checksum of this image will NOT be corrected: it is written exactly as it is ... Write it anyway?* | The file's module is not installed. Answer **No** unless the checksum is already right (a file you have not changed). Install the module (the checksum-only variant is enough). |
| *Open or read a file first, then write it.* (*Choose a file to write first (Open file...).* in OVT Flasher) | **Write flash** writes the binary in the active tab (in OVT Flasher: the file or project chosen with **Open file...**). |
| *Read image did not load: ...* | The data read from the unit could not be opened as a file. Read again. |
| Cluster (VDO K-line) messages — *the cluster did not answer*, *the cluster refused the seed/key login*, *the file is not a recognised VDO Mk4 cluster EEPROM* | See [Reading and writing the cluster](modules/vdo/read-write.md#when-something-goes-wrong). |
| Climatronic messages — *Climatronic needs the CAN / TP 2.0 transport.*, *This image carries no flash loader ...* | See [Writing the Climatronic](modules/hvac/flashing.md#when-something-goes-wrong). |

## Modules

| Message | Meaning and fix |
|---|---|
| *this module is for \<product\>, not OVT-Suite* | The package is for a different program. |
| *OVT-Suite does not know a module called '...' -- it needs a newer OVT-Suite* | Update OVT-Suite, then install the module. |
| *the \<name\> module \<version\> was built for module API N; this OVT-Suite supports ...* | Get the module version made for your app version. |
| *the \<name\> module is unsigned; it was refused ...*, *... is not signed by a trusted key; it was refused ...* | The package is not signed by a key this app trusts. Use the official package. |
| *the package is damaged: ...* | The download is incomplete or corrupt. Download it again. |
| *The \<name\> module \<version\> is installed. Restart OVT-Suite to use it -- the version already loaded stays in use until then.* | Not an error: restart the app. |
| *OVT-Suite is not installed on this computer.* (a module installer) | Install OVT-Suite or OVT Tools first. |
| *OVT-Suite is installed for your user only, not for all users.* (a module installer) | Run the module installer again and choose to install for you only. |

See [Modules](modules.md) for the Module Manager.

## The AI assistant

| Message | Meaning and fix |
|---|---|
| *The assistant is off. Turn it on, and choose a service, in AI settings.* | Nothing is sent until you tick **Use AI inside OVT-Suite** in **Tools > AI > Settings...**. See [AI settings and privacy](ai/settings.md). |
| *No AI service is set up ...* | No service is chosen and no Ollama or LM Studio is running. Choose a service, or start one of them. |
| *... needs an API key (Tools > AI > Settings)* | Paste the service's key in **Tools > AI > Settings...**. |
| *The service refused the API key (401) ...* | The key is wrong, expired or for another service. Paste it again, then **Test connection**. |
| *The service's firewall blocked the request before it reached the service ... -- this is not about the API key* | A firewall in front of the service (Cloudflare, for example) turned the request away; the key was never checked. Try again later or from another network (a VPN can trigger it). If it keeps happening, tell us: the service may be blocking this app. |
| *... is busy (503); trying again in N s...*, then *The service had a problem (503 / 529) ...* | The service (or that model) is overloaded for the moment. The app asks again by itself three times, over about half a minute; if it is still busy, try later, choose another model in **Tools > AI > Settings...**, or switch service. |
| *Rate limit or quota reached ...* | Too many requests, or the account's credit or free daily limit is used up. Wait, or check the plan with the service. |
| *Could not reach ...* | No internet connection, a wrong **Address**, or — for Ollama / LM Studio — the program or its local server is not running. With **When offline, answer with a model on this PC** ticked, a running local model answers instead. |
| *The service rejected the request (400) ...* | Often a model that does not support tools (common with small local models). Choose another model. |
| *... offers no models* | The local server has no model loaded. Download or load one in Ollama / LM Studio. |
| *Stopped after N tool rounds without a final answer.* | The task needed more steps than allowed. Ask for a smaller part of it, or use a larger model. |
| A proposal is not filed: the AI says the safety checks refused it | Normal: the AI is told why and usually corrects it. If it gives up, ask for a smaller change. See [Safety checks](ai/proposals.md#safety-checks). |
| *Open this project / file to preview or accept it.* (Proposals) | The proposal is for something not open. Open it, then preview or accept. |
| *these bytes changed since it was proposed* | The file changed since a fault-code or patch proposal was made, so that part was not written. Ask for the proposal again. |

## Logging, sniffing and the simulator

See [Troubleshooting logging](logging/troubleshooting.md),
[Bus sniffer](sniffer.md) and [ECU simulator](simulator.md).

## The app hangs or crashes

The app keeps diagnostic logs in your settings folder (`%APPDATA%\OVT-Suite`
on Windows, `~/.config/ovtsuite` on Linux):

- `diagnostics.log` — if the window stops responding for a while, what each
  part of the app was doing; and errors that did not stop the app;
- `ovtsuite-fault.log` — the trace of a hard crash in the last run;
- `ovtsuite-crash.log` — written if the app fails to start (beside the
  program, or in the settings folder).

## Reporting a problem

**Help > About OVT-Suite...** shows the version and the installed modules;
its **Copy** button copies them for a bug report. Include the exact message,
what you did, and the logs above — see
[About and credits](about.md#reporting-a-problem).
