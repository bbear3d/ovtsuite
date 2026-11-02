---
title: Definition packs
order: 50
---
# Definition packs

A definition pack says what a module's bytes mean: where modules are
reached (the address table), how a module identifies itself and which
session it wants, the texts of its fault codes, the names of its measuring
groups, the meaning of its coding digits and adaptation channels, the data
identifiers of a UDS module, and the procedures that can be run on it.

Packs come in three layers; a more specific layer wins over a more general
one:

1. **Bundled** -- the `vag-generic` pack inside OVT Diag: the address
   tables and the generic module documents.
2. **Modules** -- a platform module can ship a pack (the Bosch EDC16 module
   ships its fault texts). Installing or removing the module adds or removes
   it.
3. **Yours** -- packs in `<config>/diag/definitions`. **Install pack...**
   takes a pack folder or zip; **Installed packs...** lists what is loaded
   and reports a pack that failed to load; **Open definitions folder** shows
   the folder; **Reload definitions** picks up edits without restarting.

A bad pack never stops OVT Diag from opening: it is skipped and listed with
the reason.

## Your local pack

OVT Diag keeps what it learns from a car in a pack of your own,
`my-local`: the addresses a gateway's installation list names that the
bundled table did not know (**Save new addresses to my local pack** on the
Gateway list page), and the K-line baud rate a module answered at. Edit it
freely; it is yours to contribute.

## On the command line

```
ovtsuite diag-defs list                      # every pack, its origin and version
ovtsuite diag-defs check <dir>               # validate a pack
ovtsuite diag-defs build <dir>               # compile it to pack.json.gz
ovtsuite diag-defs install <dir|zip>         # into your definitions folder
ovtsuite diag-defs remove <pack id>
ovtsuite diag-defs schema [<kind>]           # the JSON schema of a document kind
ovtsuite diag-defs import-odx <file.odx>     # draft documents from an ODX
```

A pack is a folder with a `pack.yaml` manifest and one YAML document per
file; the kinds are `addressing`, `module`, `faults`, `measuring`, `coding`,
`adaptation`, `dids`, `security` and `procedure`. Every document carries a
`match` block (address, protocol, part number pattern, software version,
platform, generation) that says which modules it applies to.

A `module` document also says how the module encodes a fault word
(`faults: {encoding: vag}` or `sae`), which is what makes a KWP2000 code
read as `01044` rather than a wrong P-code; see [Faults](faults.md).
