---
title: Licences and legal use
order: 96
context: ModuleLicenceDialog
---
# Licences and legal use

## Licences

| Part | Licence |
|---|---|
| OVT-Suite itself (the base app) | Free software under the Mozilla Public License 2.0. Its source code is published; the link is in **Help > About**. |
| The VDO Cluster module | Free software under the Mozilla Public License 2.0, published on its own. |
| The other modules (EDC16, EDC15, ZF 6HP, ZF 8HP, Aisin, HVAC) | Closed source, under the module licence agreement. |
| Third-party code, data and libraries | Each under its own licence. See [About and credits](about.md). |

### Accepting a module's licence

A closed-source module comes with its licence agreement. On Windows, the
installer shows it before installing: a module's own installer, and the full
installer or the OVT Tools installer when they include modules (OVT-Suite's
own licence, the MPL-2.0, follows it there). The base-only installer shows
the MPL-2.0. Installing a `.ovtmod` file from
**Tools > Modules... > Install from file...** opens the **Module licence**
window instead:

- Read the agreement.
- Tick **I accept the agreement**, then click **Install**.
- **Cancel** leaves the module uninstalled.

The Linux packages install modules without showing their agreement, and a
module can also be copied into place by hand. The first time OVT-Suite (or one of its tools) starts
after that, the **Module licence** window asks you to accept it:

- **Accept** starts with the module, and you are not asked again (for that
  version of the agreement).
- **Decline** starts without the module, as if it were not installed. The
  Module Manager shows it as *not loaded: licence agreement not accepted*,
  and you are asked again at the next start.

Acceptance in the app is kept per user. A Windows installer that showed the
agreement records the acceptance beside the module, so the app does not ask
again.

An open-source module has no agreement to accept. Its licence text is in its
folder as `LICENSE.txt`.

Parts of a module that come from other projects stay under their own
licences, whatever the module's licence says. The VDO module's needle-sweep
patches are one example. You can see these parts and their licences in
**Help > Credits & licences** while the module is installed.

## FTDI's driver

OVT-Suite does not include FTDI's USB driver. If a Windows installer finds
the driver missing, it offers to download and install it from Microsoft's
driver catalog (the package Windows Update installs). FTDI's
own licence terms apply to the driver. See [K-line cables](cables/kline.md).

## Trademarks

Volkswagen, Audi, SEAT, Škoda, Bosch, ZF, Aisin, VDO, Continental, Tactrix,
FTDI, Ross-Tech and other names, trademarks and part numbers belong to their
owners. OVT-Suite uses them only to say what it works with. OVT-Suite is not
affiliated with, endorsed by or associated with any of these companies.

## Legal use

Changing an engine, transmission or cluster control unit can:

- damage the control unit or the vehicle;
- affect the vehicle's warranty and insurance;
- make the vehicle non-compliant with emissions and roadworthiness rules.

In many countries it is illegal to remove, disable or defeat emissions
controls on a vehicle used on public roads. This includes the US Clean Air
Act and EU and UK type-approval law. Changing a vehicle's recorded mileage
is illegal in most countries. Use OVT-Suite only where and how the law allows.

The software comes with no warranty. Before you write anything to a control
unit, keep a backup of its original read.
