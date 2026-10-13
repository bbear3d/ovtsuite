# Licences, trademarks and legal use

## Licences

| Part | Licence |
|---|---|
| The OVT-Suite base app (this repository) | [Mozilla Public License 2.0](LICENSE) |
| The VDO Cluster module | Mozilla Public License 2.0, in its own repository |
| The other control-unit and checksum modules (EDC16, EDC15, ZF 6HP, ZF 8HP, Aisin, HVAC) | Closed source, under the [module licence agreement](ovtsuite/modules/EULA.txt). Their installers are published on the author's website. |
| Third-party code, data and runtime libraries | Each under its own licence: see **Help > Credits & licences** in the app, or `THIRD-PARTY-NOTICES.txt` beside an installed copy |

The MPL is a file-level copyleft: if you distribute a modified version of a
file from this repository, that file's source must stay available under the
MPL. You may combine it with files under other licences, including
closed-source modules, in a larger work.

FTDI's D2XX/VCP driver is not distributed with OVT-Suite. The Windows
installer can download it from Microsoft's driver catalog (the package
Windows Update installs), under FTDI's own licence terms.

## Trademarks

Volkswagen, Audi, SEAT, Škoda, Bosch, EDC15, EDC16, ZF, Aisin, VDO,
Continental, Tactrix, OpenPort, FTDI, Ross-Tech and other names, trademarks
and part numbers belong to their owners. They are used only to describe what
the software works with. OVT-Suite is an independent project and is not
affiliated with, endorsed by or associated with any of these companies.

## Legal use

OVT-Suite reads, edits and writes the software and data of engine,
transmission and other control units. Doing so:

- can damage the control unit, the engine, the transmission or the vehicle;
- may void the vehicle's warranty and affect its insurance;
- may make the vehicle non-compliant with emissions, safety or
  roadworthiness rules.

Removing, disabling or defeating emissions controls (for example the EGR,
particulate filter, catalytic converter, or the fault codes that monitor
them) on a vehicle used on public roads is illegal in many countries,
including under the US Clean Air Act and EU and UK type-approval law. Changing
a vehicle's recorded mileage is illegal in most countries.

You are responsible for using this software only where and how the law
allows: for example on vehicles used off road or in competition, for
research, or for repairs that keep a vehicle compliant.

## No warranty

The software is provided as is, without warranty of any kind, as the MPL
(sections 6 and 7) and the module licence agreement say. Keep a backup of
every original read before you write anything to a control unit.
