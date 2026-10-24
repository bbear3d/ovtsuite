# Third-party modules and module encryption

Status (2026-10-10): **proposal, nothing built.** Builds on the module split in
`docs/MODULES_PLAN.md`. The "Decisions needed" section at the end lists what Bear has to choose
before work starts.

## Summary

- **Third-party modules are feasible.** The `.ovtmod` format, the manifest, Ed25519 signing, the
  check-then-install step, the start-up re-verification and the Module Manager all carry over
  unchanged. Three things have to be built:
  1. a stable SDK surface;
  2. an open platform catalogue, where a module brings its own detector;
  3. a trust model for publishers other than Bear.
- **A module is trusted code.** It runs in-process with the user's full rights. A signature proves
  who made it, not that it is safe. No design here changes that.
- **Encryption: none today; native compilation: done.** A package is signed, and a closed
  module's package carries compiled code, not source (Part 2, "Native compilation"). Anyone can
  still unzip it and copy it. The proposal for that is a licence-keyed encrypted payload. Both
  are deterrents. Code that runs on the user's machine can be recovered by someone determined.

## Where things stand

### Already reusable as is

| Piece | Where | Reuse |
|---|---|---|
| Package format, per-file SHA-256 manifest, Ed25519 signature | `modules/package.py`, `modules/signing.py` | Unchanged |
| Install checks everything before writing; atomic swap | `modules/installer.py` | Unchanged |
| Re-verification at every start (about 70 ms for seven modules) | `modules/loader.py` | Unchanged |
| API level: `MODULE_API = 1`, `SUPPORTED_API = (1,)` | `modules/api.py` | Becomes the SDK version |
| Hooks as relative strings, resolved only when used | `ModuleSpec.resolve` | Unchanged |
| Licence slot in every manifest | `modules/licensing.py` | Becomes real (Part 2) |
| Clean-interpreter package check | `modules/build.py: check_package` | Becomes the developer kit's `check` |

### Closed today

| What | Where |
|---|---|
| The installer refuses any module id not in the base catalogue | `installer.py`: `man.id not in platforms.MODULES` |
| Identification is a fixed list of base detectors | `platforms/identify.py` |
| Modules mount into the app package (`ovtsuite.<id>`) | `loader.mount` appends to `ovtsuite.__path__` |
| Modules import base internals freely | see the table below |
| Only keys in the base app's `data/module_keys.json` are trusted | `signing.py` |
| Module tools are wired into the GUI by name | `gui.py` has 18 direct imports of module packages; `cli.py` has 10 |
| Each ECU's flash protocol lives in the base flasher | `flash/edc16.py`, `zf6hp_uds.py`, `aisin09d.py`, ... |

### How tightly first-party modules are tied to base internals

Distinct base-app imports per module:

| zf6hp | edc15 | edc16 | vdo | zf8hp | hvac | aisin |
|---|---|---|---|---|---|---|
| 26 | 12 | 11 | 9 | 7 | 6 | 4 |

The most used are `modules`, `platforms`, `groups`, `model`, `branding` and `progress`. That is
fine while everything ships together, but any refactor of the base app would break an outside
module built against it. That is the SDK's job to stop.

## Part 1: third-party modules

### 1. The SDK surface (`ovtsuite.sdk`)

The SDK is the one import a third-party module may use. Its version is `MODULE_API`.

| Sub-module | Contents |
|---|---|
| `sdk.identify` | `Identity`; the `Detector` protocol: `detect(data) -> Identity or None` |
| `sdk.model` | `Entity`; the tree groups and tier constants |
| `sdk.session` | A documented attribute contract, not the `TunerSession` class. It covers what a `build` hook reads (`binfile`, `original`, `path`) and what it must set (`platform`, `family`, `entities`, `by_key`, `region`, warnings). |
| `sdk.progress` | `Reporter` |
| `sdk.checksum` | `ChecksumProvider`, `ChecksumError`, `SimpleReport`, `FunctionsProvider` |
| `sdk.definitions` | `NoDefinition`; read and write helpers for the definition (KB) format; the anchor matcher |
| `sdk.data` | The data-file resolver, scoped to the module's own files |
| `sdk.tools` | Tool registration: a menu entry plus a dialog factory. Qt types are allowed only here. |

**Compatibility rules:**
- Adding something does not change the level.
- Removing something or changing its behaviour raises `MODULE_API`. The base app then keeps the
  previous level in `SUPPORTED_API` for at least one release, which is the deprecation window.
- Every SDK name is documented, and a test pins the SDK's public names, so a change can't slip in
  unnoticed.

**Enforcement.** The package build check parses the module's code. A third-party module may
import only:
- `ovtsuite.sdk`;
- its own package;
- the standard library;
- dependencies bundled in its own folder.

Anything else fails the build.

**First-party modules** move onto the SDK too; that is what keeps the SDK honest. Until they have
moved, they are exempt from the import check, and the build reports how many internal imports are
left. Aisin, with 4, is the first to move and becomes the reference example.

### 2. An open platform catalogue

- **Manifest additions:**
  - `platforms`: a list of `{id, name, family}`;
  - a `detect` hook (`".detect:detect"`);
  - a `priority`.
- **Namespacing.** A third-party id is `<publisher>.<name>`, for example `acme.me7`. It mounts
  under a separate package, `ovtsuite_ext.<publisher>.<name>`, not under the app package. It can
  then never shadow the base app or a first-party module.
- **Identification order:**
  1. The base detectors run first, in today's order.
  2. Third-party detectors run only on binaries no base detector claimed. Existing platforms can't
     be taken over this way.
  3. If two third-party detectors claim the same binary, it is ambiguous: the user picks, and File
     info shows both.
- **Detector rules.** A detector must be fast and have no side effects. The package check times it
  against a budget, for example 50 ms on a 4 MB image. A detector that raises counts as not
  claiming the binary.
- **Limit.** A third-party platform can't be recognised until its module is installed, so "Install
  this module" only works for platforms the base app knows. A later addition could be a signed
  index of third-party platforms, which would let the base app name them.

### 3. Extension points

| Extension point | Today | Proposed | Phase |
|---|---|---|---|
| Identify, definitions, checksum | `ModuleSpec` hooks | Same hooks, through the SDK | 1 to 2 |
| Tools (menu items, dialogs) | Hard-coded in `gui.py` | `sdk.tools` registration; the GUI lists what is installed | 2 |
| Native containers | Base `container.py` formats | A container hook (magic, extension, pack and unpack) | 3 |
| Logger decoders (measuring blocks) | Base `datalog` | A decoder hook | 3 |
| Flash protocols | One protocol per ECU in the base flasher | A protocol class over the base transports (J2534, K-line, TP 2.0, ISO-TP) | 5, optional |

Flash protocols carry the most risk, because a bad one bricks a module. If they are allowed at all,
they should only come through the Bear-signed route.

### 4. Trust and distribution

| | A. Bear signs | B. Publisher keys |
|---|---|---|
| How | The developer submits source. Bear builds and signs it, so `module_keys.json` is unchanged. | The manifest carries a publisher id and key. On first install the user sees the key's fingerprint and approves it. Approved keys are kept per user. |
| Quality gate | Bear's review and tests | None beyond the signature |
| Revocation | Stop signing | A revocation list shipped with base updates |
| Effort | Smallest | A trust dialog, key storage, revocation |

**Recommendation: start with A.** B can be added later without changing the package format.

The "Allow unsigned modules" developer switch stays in both models.

**What the base app can still do for safety:**
- **Before a flash write**, if a third-party provider corrected the image, re-verify it and show
  which byte ranges changed outside the provider's declared checksum ranges. The write then needs
  explicit confirmation.
- **Out-of-process checksum and definition hooks** (a subprocess talking over IPC) would contain a
  crash but not malice, and they cost speed. Not recommended at first.

### 5. Developer kit

- **`ovtsuite module new <publisher.name>`**: a template with `module.py`, a detector, a checksum
  provider, a test and a README.
- **`ovtsuite module check` and `ovtsuite module pack`**: an out-of-tree source folder goes in, and
  a signed or unsigned `.ovtmod` comes out. These adapt `tools/build_modules.py` and
  `check_package`.
- **Run against the installed app:** the installed app already loads extra module folders from
  `OVTSUITE_MODULES_DIRS`, so a developer runs their module against the real app without packing.
- **Docs:** the SDK reference, the hook contracts and the Aisin module as the worked example.

### 6. Python and binary compatibility

This is the only true ABI question.

- **Byte-code.** The frozen app pins one CPython minor version. An open-source module's package
  ships hash-checked byte-code built for it, plus the source.
- **Native code (done for the closed modules).** A package that ships compiled code (`.pyd` or
  `.so`) is tied to one OS and one Python version. Its manifest has `python` and `platform` tags
  (`cp313`, `win_amd64`), so does its file name, and the installer refuses a mismatch. See
  `docs/MODULES_PLAN.md`, "Packages".
- **Dependencies.** Pure-Python dependencies are bundled in the module's own `_vendor` folder.
  Native dependencies need one package per platform.

## Part 2: encryption

### Threat model

| Want to stop | Can't stop |
|---|---|
| A paid module being copied to an unlicensed machine | Someone determined recovering code that runs on their own machine (key extraction, memory dumps) |
| Casual reading of algorithms (seed/key, checksum maths) | |

### Options

| Option | Protects | Cost |
|---|---|---|
| Ship byte-code only, no source | Casual reading; decompilers recover most of it | Trivial |
| **Encrypted payload, key delivered with a licence** | Copying to unlicensed machines; nothing readable on disk | Moderate |
| **Native compilation** (Cython or Nuitka) of chosen files (**done**, Cython, every file) | The algorithms themselves; as hard to reverse as any native binary | One build per OS and Python version; needs a C toolchain (MSVC on Windows) |
| Keep the secret on a server | Everything | Not viable: flashing must work offline |

### Proposed encrypted format (manifest version 2)

- **Payload.** Each file is encrypted with AES-256-GCM under a random per-module content key, with
  its own nonce. `cryptography` is already a dependency.
- **Verification is unchanged.** The manifest hashes the encrypted files and the signature covers
  the manifest, so install and start-up verification work exactly as now. A package can be checked
  without being decrypted.
- **Manifest field.** `encryption: {"alg": "AES-256-GCM", "key_id": ...}`. Encrypted packages carry
  byte-code only, never source.
- **Licence file.** Bear issues and signs it. It holds:
  - the content key, wrapped to the licensee (an X25519 key from the machine or the account);
  - the module id;
  - an expiry, if wanted.

  It is installed once and checked offline.
- **Loading.** An import finder decrypts modules in memory, and plaintext never touches disk. The
  data-file resolver reads module data through the same decryptor.
- **`licensing.check` becomes real.** It checks the licence's signature against Bear's licence key,
  then the module id, the expiry and the binding. It runs at both install and load, as now.
- **Machine binding caveats.** Hardware changes and VMs break a binding to the machine, so
  re-issuing a licence has to be easy. Binding to the account is gentler but easier to share.

### Native compilation

**Done (2026-10-24).** A closed-source module's package (every module under the module EULA:
EDC15, EDC16, ZF 6HP, ZF 8HP, Aisin, HVAC) ships no Python source. The open-source modules (VDO,
ZF 5HP) are unchanged.

- **Every file is compiled**, not only the ones worth protecting: checksum, seed/key, map
  finders, feature switches and knowledge-base code alike. That costs only build time, and it
  leaves no choice to get wrong. `ovtsuite/modules/native.py` runs Cython (pure-Python mode,
  `annotation_typing=False`, so the semantics are the source's) and then the C compiler through
  setuptools. Each file becomes an extension module beside where its source would be.
  `module.py` is one too: the registry imports it like any other module.
- **Byte-code only, no source**, for what is not compiled:
  - each package `__init__.py`, which holds only a docstring and re-exports;
  - any file Cython or the C compiler rejects, with the reason printed by the build.

  Cython is stricter than CPython about provably unbound locals. It caught one real bug, in
  `edc16/fsw_dpf.py` `describe()`, and that is now fixed. All of EDC16 compiles today.
- **Loading is unchanged.** A mounted install folder is on the app package's `__path__`, and
  CPython's own finders load `.pyd`/`.so` and sourceless `.pyc` from it. `__file__` points at the
  extension module, so `dirname(dirname(__file__))/data` still finds the module's data. Help
  pages, data files and the EULA ship as before. The manifest hashes and the signature cover the
  compiled files exactly as they covered the source.
- **Tagged per platform** (Part 1, section 6): `edc16-1.0.15-cp313-win_amd64.ovtmod`. The
  installer and the start-up check refuse a mismatch, and the updater only offers this app's
  build.
- **The build** (`tools/build_modules.py packages`) compiles and then runs `check_package` in a
  clean interpreter against the base-only app. For a compiled package, that check also asserts
  that no `.py` was installed and that every module file it imported was an extension module or
  byte-code. `build.ps1` builds the Windows packages (MSVC). `build_linux.sh` builds the Linux
  ones in its Docker image (gcc, glibc 2.31), and its module `.deb`s are then
  architecture-specific.
- **A source checkout is unaffected.** In-tree modules are plain `.py` and always win over an
  installed copy, so tests and development never see compiled code.

This combines with encryption: the encrypted payload would stop copying, and native code
already stops reading.

## Phased plan

| Phase | Work | Rough size |
|---|---|---|
| 1 | `ovtsuite.sdk` facade; move Aisin onto it; the import check; pin the SDK's names in tests | 1 to 2 sessions |
| 2 | Open catalogue: `ovtsuite_ext` namespace, `detect` hook, identification order, ambiguity handling; `sdk.tools` registration; move the GUI's 18 hard-coded module imports onto it | 2 sessions |
| 3 | Developer kit (`module new`, `check`, `pack`); docs; container and logger hooks | 1 to 2 sessions |
| 4 | Encrypted payload, licence files, real `licensing.check`, decrypting importer (native compilation in the build: done) | 2 sessions |
| 5 | Optional: publisher keys and revocation; the flash protocol hook | 1 to 2 sessions |

First-party modules keep working at every phase. Each phase ships on its own.

## Decisions needed

1. **Trust model:** Bear signs everything (A), publisher keys (B), or A now and B later?
2. **Flash protocols:** may third-party modules add them at all, and only if Bear signs them?
3. **Encryption scope:** paid modules only, or every module? Are checksum-only variants
   encrypted? Leaving them clear keeps OVT Tools simple.
4. **Licence binding:** per machine, per account, or per user with a number of activations? Does
   it expire?
5. **Native compilation:** decided and done. Every file of a closed module is compiled.
6. **Publisher ids:** the naming scheme, and who assigns them.
