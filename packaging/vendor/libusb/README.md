# libusb-1.0 for the Windows build

`libusb-1.0.dll` is the 64-bit Visual Studio 2022 build from the official
libusb 1.0.30 release. It is PyUSB's back end on Windows: the OpenPort 2.0
direct route (`ovtsuite/flash/openport.py`) and the pyftdi route
(`ovtsuite/flash/ftdi.py`) both reach USB through it. `packaging/ovtsuite.spec`
puts it in the 64-bit frozen build; the 32-bit environment (the J2534 bridge
only) does not carry it. `ovtsuite/flash/libusb_dll.py` makes PyUSB find it.

| | |
|---|---|
| Version | 1.0.30 |
| Source | <https://github.com/libusb/libusb/releases/tag/v1.0.30> (`libusb-1.0.30.7z`, `VS2022\MS64\dll\libusb-1.0.dll`) |
| Archive SHA-256 | `7fb1dfec805b97983763d7d0ae244320da12add1003d4249c96cc4d586398c79` |
| DLL SHA-256 | the one in `SHA256` beside this file (`tests/test_libusb_dll.py` checks it) |
| Licence | LGPL-2.1-or-later, `ovtsuite/data/licenses/LGPL-2.1.txt`; credited in `ovtsuite/credits.py` |

The DLL is unmodified and loaded as a separate library, so it can be replaced
with any compatible build. Its source is at the release page above.

## Updating it

1. Download the new `libusb-<version>.7z` and its `.asc` from the release page.
2. Verify the signature against the release managers' keys
   (<https://github.com/libusb/libusb/blob/master/KEYS>):
   `gpg --import KEYS` then `gpg --verify libusb-<version>.7z.asc libusb-<version>.7z`.
   (1.0.30 was signed by Tormod Volden, primary key
   `C681 8737 9B23 DE9E FC46  651E 2C80 FF56 C683 0A0E`.)
3. Replace the DLL with the archive's `VS2022\MS64\dll\libusb-1.0.dll`, write
   its SHA-256 into `SHA256`, and update the table above, the version in
   `ovtsuite/credits.py` and the licence text if it changed.

## macOS

The macOS app can carry a libusb-1.0 dylib in `Contents/Frameworks`
(`packaging/ovtsuite.spec`, `_macos_libusb`). Nothing is downloaded by the
build; the spec takes the first usable copy of, in order:

1. `packaging/vendor/libusb/macos/libusb-1.0*.dylib` (put an official or
   package-manager build there; not committed by default),
2. the `libusb-package` PyPI wheel, if installed in the build environment,
3. an installed MacPorts (`/opt/local/lib`) or Homebrew copy.

A copy that lacks the build's architecture (both for `--arch universal2`) is
skipped with a warning, because `build_mac.sh`'s architecture check would
otherwise fail the build. With none found the build continues and prints a
warning; the app then needs a system libusb. `ovtsuite/flash/libusb_locate.py`
searches the bundle first, then MacPorts, Homebrew and `OVT_LIBUSB`. Signed
(Developer ID) builds use `packaging/entitlements.plist`
(`disable-library-validation`) so a libusb from MacPorts or Homebrew still
loads under the hardened runtime.
