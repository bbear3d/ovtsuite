#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Build the app for macOS: OVT-Suite.app (and a .dmg), with the closed-source
# modules COMPILED into native code -- no module Python source is in the result.
#
#   ./build_mac.sh                    # build for this Mac's architecture
#   ./build_mac.sh --arch universal2  # one app for Apple silicon and Intel (see below)
#   ./build_mac.sh --base-only        # no modules
#
# First time on a Mac: ./setup_mac.sh --build   (checks the compiler, finds a Python,
# makes the build environment; this script does the same itself when it is missing).
#
# Options
#   --arch A        arm64 | x86_64 | universal2 | native (default native = this machine).
#                   x86_64 on an Apple-silicon Mac runs the whole build under Rosetta
#                   (softwareupdate --install-rosetta) and needs a Python that has an Intel
#                   slice -- the python.org installer's, which is universal2.
#                   universal2 needs a universal2 Python (python.org) and every compiled
#                   library in the bundle to be universal2 too; the build checks each
#                   binary with lipo and names the ones that are not. Experimental.
#   --modules LIST  module packages to build (comma-separated ids, default all)
#   --base-only     no modules: only the base app
#   --unsigned      do not sign the module packages (development only; the app refuses
#                   them unless OVT_ALLOW_UNSIGNED=1)
#   --key FILE      the module signing key (default: $OVT_SIGNING_KEY, else ./signing/
#                   module-key.pem when the source package came with one, else
#                   ~/.ovt-signing/module-key.pem; one is created on first use)
#   --sign ID       codesign the app with this identity (a "Developer ID Application: ..."
#                   certificate) instead of ad hoc. Notarization is not done here.
#   --no-dmg        leave out the .dmg (dist/mac/OVT-Suite.app is still made)
#   --skip-build    package the existing PyInstaller output (build/mac/app) again
#   --regen-data    regenerate the definition files and search index from Reference/
#                   first. By default the checked-in ovtsuite/data is packaged as it is,
#                   exactly as the Windows and Linux builds ship it, so no Reference/
#                   folder is needed.
#   --test          run the targeted tests that cover this path first (never the full suite)
#   --python PATH   interpreter for the build venv (needs 3.10+). Default: the python.org
#                   framework Python, then Homebrew's, then python3 on PATH.
#   --venv DIR      build venv (default: ./.venv-build-<pyver>-<arch>)
#   --update        re-run the dependency install, upgrading, even if nothing changed
#   -h, --help
#
# Result (dist/mac/)
#   OVT-Suite.app            the app, ad-hoc signed; modules in Contents/MacOS/modules
#   OVT-Suite-<ver>-<arch>.dmg   the app and an Applications link
#   module-installers/       the .ovtmod packages, for installing a module into another app
#
# A Mac that downloads the .dmg or .app (not one that built it) puts a quarantine flag on
# it; an ad-hoc signed app then opens only through right-click > Open, or after
#   xattr -dr com.apple.quarantine /Applications/OVT-Suite.app
# A Developer ID signature plus notarization (--sign, then xcrun notarytool) avoids that.
#
# Every name (product, executable, version) is read from the package's branding module.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ARCH=native; MODULES=all; BASE_ONLY=0; UNSIGNED=0; KEY=""; SIGN=""; DMG=1
SKIP_BUILD=0; REGEN=0; RUN_TESTS=0; UPDATE=0; PY="${BUILD_PYTHON:-}"; VENV=""
ORIG_ARGS=("$@")

die()  { echo "!! $*" >&2; exit 1; }
warn() { echo "!! WARNING: $*" >&2; }
step() { echo; echo "== $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --arch)         ARCH="${2:?--arch needs a value}"; shift ;;
        --modules)      MODULES="${2:?--modules needs a list}"; shift ;;
        --base-only)    BASE_ONLY=1 ;;
        --unsigned)     UNSIGNED=1 ;;
        --key)          KEY="${2:?--key needs a file}"; shift ;;
        --sign)         SIGN="${2:?--sign needs an identity}"; shift ;;
        --no-dmg)       DMG=0 ;;
        --skip-build)   SKIP_BUILD=1 ;;
        --regen-data)   REGEN=1 ;;
        --test)         RUN_TESTS=1 ;;
        --python)       PY="${2:?--python needs a path}"; shift ;;
        --venv)         VENV="${2:?--venv needs a directory}"; shift ;;
        --update)       UPDATE=1 ;;
        --rosetta-child) ;;                       # internal: re-run under arch -x86_64
        -h|--help)      sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *)              die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

[ "$(uname -s)" = "Darwin" ] || die "this builds the macOS app and must run on a Mac (it uses codesign, lipo and hdiutil)"

HOST="$(uname -m)"                                 # arm64 | x86_64 (x86_64 also under Rosetta)
if [ "$ARCH" = native ]; then
    ARCH="$HOST"
    # a Rosetta shell reports x86_64 on an Apple-silicon Mac: that is what it builds
fi
case "$ARCH" in
    arm64|x86_64|universal2) ;;
    *) die "--arch must be native, arm64, x86_64 or universal2" ;;
esac
if [ "$ARCH" = arm64 ] && [ "$HOST" = x86_64 ]; then
    die "an arm64 app cannot be built on an Intel Mac (use --arch x86_64 or universal2 there)"
fi
# x86_64 on an Apple-silicon Mac: run this whole script as an Intel process
if [ "$ARCH" = x86_64 ] && [ "$HOST" = arm64 ]; then
    arch -x86_64 /usr/bin/true 2>/dev/null \
        || die "Rosetta is not installed: softwareupdate --install-rosetta --agree-to-license"
    echo "(building for Intel under Rosetta)"
    exec arch -x86_64 /bin/bash "$0" "${ORIG_ARGS[@]}" --rosetta-child
fi
case "$ARCH" in
    arm64)     ARCHFLAGS_WANT="-arch arm64" ;;
    x86_64)    ARCHFLAGS_WANT="-arch x86_64" ;;
    universal2) ARCHFLAGS_WANT="-arch arm64 -arch x86_64" ;;
esac

# ------------------------------------------------------------------ interpreter + names
py_ok() {   # $1 runs and is Python 3.10+
    command -v "$1" >/dev/null 2>&1 \
        && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}
py_archs() {   # the architectures of $1's own binary
    lipo -archs "$("$1" -c 'import sys; print(sys.executable)')" 2>/dev/null || true
}
py_suits() {   # $1 can build for $ARCH
    local a; a="$(py_archs "$1")"
    case "$ARCH" in
        universal2) [[ "$a" == *arm64* && "$a" == *x86_64* ]] ;;
        *)          [[ "$a" == *"$ARCH"* ]] ;;
    esac
}

if [ -n "$PY" ]; then
    py_ok "$PY" || die "--python $PY is not a Python 3.10+"
else
    for c in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
             /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
             /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
             python3.13 python3.12 python3.11 python3.10 python3; do
        if py_ok "$c" && py_suits "$c"; then PY="$(command -v "$c")"; break; fi
    done
    if [ -z "$PY" ]; then
        die "no Python 3.10+ that can build for $ARCH (Python archs found: $(for c in python3 python3.12 python3.11; do py_archs "$c"; done | tr '\n' ' '))
   Install the python.org macOS installer (it is universal2, so it builds any of the three):
     https://www.python.org/downloads/macos/
   or, for a native build only:  brew install python@3.12"
    fi
fi
py_suits "$PY" || die "$PY has architectures '$(py_archs "$PY")'; building for $ARCH needs a Python that has them (python.org's is universal2)"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

eval "$("$PY" - "$ROOT" <<'EOF'
import importlib.util, os, shlex, sys
spec = importlib.util.spec_from_file_location(
    "_branding", os.path.join(sys.argv[1], "ovtsuite", "branding.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
for var, attr in (("PKG", "PKG"), ("APP_NAME", "APP_NAME"), ("EXE", "EXE_NAME"),
                  ("VERSION", "__version__")):
    print("B_%s=%s" % (var, shlex.quote(str(getattr(b, attr)))))
EOF
)"
echo "$B_APP_NAME $B_VERSION  ($B_PKG, $ARCH, Python $PYVER at $PY)"

# ------------------------------------------------------------------ build machine checks
if [ "$SKIP_BUILD" = 0 ]; then
    missing=()
    command -v codesign >/dev/null 2>&1 || missing+=(codesign)
    command -v lipo >/dev/null 2>&1 || missing+=(lipo)
    [ "$DMG" = 1 ] && { command -v hdiutil >/dev/null 2>&1 || missing+=(hdiutil); }
    # the closed modules are compiled (Cython -> C -> .so): the Xcode command line tools
    if [ "$BASE_ONLY" = 0 ]; then
        if ! xcode-select -p >/dev/null 2>&1 || ! command -v cc >/dev/null 2>&1; then
            missing+=("a C compiler (Xcode command line tools)")
        fi
        "$PY" -c 'import os, sys, sysconfig
sys.exit(not os.path.isfile(os.path.join(sysconfig.get_paths()["include"], "Python.h")))' \
            || missing+=("Python.h for $PY")
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        die "missing on this Mac: ${missing[*]}
   Command line tools:  xcode-select --install   (a window asks; takes a few minutes)
   Or run ./setup_mac.sh --build, which checks all of this and says what to do."
    fi
fi

# ------------------------------------------------------------------ build venv
VENV="${VENV:-$ROOT/.venv-build-$PYVER-$ARCH}"
VPY="$VENV/bin/python"
REQ=()
for f in requirements-mac.txt requirements-mac-build.txt; do
    for d in "$ROOT" "$ROOT/tools/mac_package"; do
        [ -f "$d/$f" ] && { REQ+=(-r "$d/$f"); continue 2; }
    done
    die "$f not found (looked in $ROOT and $ROOT/tools/mac_package)"
done
[ "$RUN_TESTS" = 1 ] && EXTRA_DEPS=(pytest) || EXTRA_DEPS=()

step "build venv $VENV"
if [ ! -x "$VPY" ]; then "$PY" -m venv "$VENV" || die "could not create the venv"; fi
stamp="$VENV/.requirements"
want="$(cat "${REQ[@]:1:1}" "${REQ[@]:3:1}" | shasum -a 256 | cut -c1-16)${EXTRA_DEPS[*]:-}"
if [ "${UPDATE:-0}" = 1 ] || [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
    upgrade=()
    [ "${UPDATE:-0}" = 1 ] && upgrade=(--upgrade)
    "$VPY" -m pip install --quiet --upgrade pip
    "$VPY" -m pip install ${upgrade[@]+"${upgrade[@]}"} "${REQ[@]}" ${EXTRA_DEPS[@]+"${EXTRA_DEPS[@]}"}
    echo "$want" > "$stamp"
else
    echo "   requirements unchanged - skipped (--update to refresh)"
fi

# ------------------------------------------------------------------ module signing key
if [ "$BASE_ONLY" = 0 ] && [ "$UNSIGNED" = 0 ]; then
    if [ -z "$KEY" ]; then
        if   [ -n "${OVT_SIGNING_KEY:-}" ];          then KEY="$OVT_SIGNING_KEY"
        elif [ -f "$ROOT/signing/module-key.pem" ];  then KEY="$ROOT/signing/module-key.pem"
        fi
    fi
    if [ -n "$KEY" ]; then
        [ -f "$KEY" ] || die "signing key $KEY not found"
        chmod 600 "$KEY" 2>/dev/null || true
        export OVT_SIGNING_KEY="$KEY"
        echo "   module signing key: $KEY"
    else
        warn "no signing key: build_modules.py will create ~/.ovt-signing/module-key.pem and trust it in ovtsuite/data/module_keys.json (fine for a build you only use yourself)"
    fi
fi

# ------------------------------------------------------------------ tests (targeted only)
if [ "$RUN_TESTS" = 1 ]; then
    step "targeted tests"
    QT_QPA_PLATFORM=offscreen "$VPY" -m pytest -q \
        tests/test_openport.py tests/test_ftdi.py tests/test_branding.py
fi

# ------------------------------------------------------------------ PyInstaller build
# its own dist/work folders under build/mac, so a build for another platform in a shared
# checkout is never overwritten
APP_PARENT="$ROOT/build/mac/app"
OUT="$ROOT/dist/mac"
APP="$APP_PARENT/$B_APP_NAME.app"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
export ARCHFLAGS="$ARCHFLAGS_WANT"                 # the modules' C compiler, and pip's builds
export OVTSUITE_ARCH="$ARCH"
[ "$ARCH" = "$HOST" ] && [ "$ARCH" != universal2 ] && export OVTSUITE_ARCH=""   # PyInstaller's default
[ -n "$SIGN" ] && export OVTSUITE_CODESIGN_IDENTITY="$SIGN"

if [ "$SKIP_BUILD" = 1 ]; then
    [ -d "$APP" ] || die "--skip-build: no previous build at $APP"
    step "reusing $APP"
else
    step "PyInstaller build ($ARCH)"
    rm -rf "$APP_PARENT"                           # a failed build must leave no older app behind
    regen=(--no-regen); [ "$REGEN" = 1 ] && regen=()
    modopts=(--modules "$MODULES")
    [ "$BASE_ONLY" = 1 ] && modopts+=(--base-only)
    [ "$UNSIGNED" = 1 ] && modopts+=(--unsigned)
    "$VPY" build.py --no-test ${regen[@]+"${regen[@]}"} "${modopts[@]}" \
        --distpath "$APP_PARENT" --workpath "$ROOT/build/mac/work"
    [ -d "$APP" ] || die "PyInstaller produced no $APP"
fi
MACOS="$APP/Contents/MacOS"
[ -x "$MACOS/$B_EXE" ] || die "no executable $MACOS/$B_EXE in the bundle"

# ------------------------------------------------------------------ modules into the bundle
# (build.py installed them beside the one-folder build, which the bundle replaced)
step "modules"
rm -rf "$MACOS/modules"
if [ "$BASE_ONLY" = 0 ]; then
    "$VPY" tools/build_modules.py install-into "$MACOS/modules" --packages dist/modules \
        --modules "$MODULES" --variant full
    # no Python source of a module may be in the bundle: compiled code only
    # (open-source modules -- MPL-2.0, e.g. vdo and zf5hp -- ship their own LICENSE.txt
    # and are source by design; only the closed ones must be compiled)
    srcs=""
    for m in "$MACOS/modules"/*/; do
        ls "$m"*/LICENSE.txt >/dev/null 2>&1 && continue
        srcs="$srcs$(find "$m" -name '*.py' | head -20)"
    done
    [ -z "$srcs" ] || die "module source files in the bundle:
$srcs"
    echo "   modules: $(ls "$MACOS/modules" | tr '\n' ' ') -- no .py source"
else
    echo "   (base app only)"
fi
# the licence notices PyInstaller put beside the one-folder build
if [ -f "$APP_PARENT/$B_EXE/THIRD-PARTY-NOTICES.txt" ]; then
    mkdir -p "$APP/Contents/Resources"
    cp "$APP_PARENT/$B_EXE/THIRD-PARTY-NOTICES.txt" "$APP/Contents/Resources/"
fi
rm -rf "$APP_PARENT/$B_EXE"                        # the one-folder build: only the .app is wanted

# ------------------------------------------------------------------ the launcher
# OVT-Suite.app is ONE app that launches every program of the suite: its bundle executable
# is this script, which opens the tool picker (ovtsuite --launcher); the editor and each tool
# are the real executable, started from there. Files opened with the app go to the editor.
step "launcher"
cat > "$MACOS/launch" <<EOF
#!/bin/bash
exec "\$(dirname "\$0")/$B_EXE" --launcher "\$@"
EOF
chmod 755 "$MACOS/launch"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable launch" "$APP/Contents/Info.plist" \
    || die "could not point the bundle at the launcher"
echo "   $B_APP_NAME.app opens the tool picker"

# ------------------------------------------------------------------ architecture check
step "architecture check ($ARCH)"
bad=0
while IFS= read -r f; do
    a="$(lipo -archs "$f" 2>/dev/null)" || continue            # not a Mach-O
    case "$ARCH" in
        universal2) [[ "$a" == *arm64* && "$a" == *x86_64* ]] || { echo "   not universal2 ($a): ${f#"$APP"/}"; bad=1; } ;;
        *)          [[ "$a" == "$ARCH" ]] || { echo "   wrong architecture ($a): ${f#"$APP"/}"; bad=1; } ;;
    esac
done < <(find "$APP" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -100 \))
if [ "$bad" = 1 ]; then
    [ "$ARCH" = universal2 ] \
        && die "some binaries are single-architecture: a universal2 build needs a universal2 wheel of each (pip download shows which are missing), or build arm64 and x86_64 separately"
    die "binaries of the wrong architecture are in the bundle"
fi
echo "   every binary is $ARCH"

# ------------------------------------------------------------------ sign
# Signed AFTER the modules went in: any file added later breaks the seal.
step "codesign (${SIGN:-ad hoc})"
# Sign (and package) a clean copy on the local disk: a checkout in Google Drive / iCloud
# gets Finder and file-provider attributes put back on its folders, which a strict signature
# check calls "detritus" ("resource fork, Finder information, or similar detritus not allowed").
SIGNDIR="$(mktemp -d)"; trap 'rm -rf "$SIGNDIR" "${STAGE:-}"' EXIT
ditto --noextattr --norsrc "$APP" "$SIGNDIR/$B_APP_NAME.app"
APP="$SIGNDIR/$B_APP_NAME.app"; MACOS="$APP/Contents/MacOS"
# The modules' compiled files were signed ad hoc when their packages were built (their hashes
# are in a signed manifest, and signing changes a file); signing them again here, by the same
# name and with the same options, gives the same bytes, so the manifests still match.
if [ -n "$SIGN" ]; then
    # disable-library-validation (packaging/entitlements.plist): lets the hardened app load a
    # libusb installed by MacPorts/Homebrew (not signed by us) when none is bundled.
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ROOT/packaging/entitlements.plist" --sign "$SIGN" "$APP"
else
    codesign --force --deep --timestamp=none --sign - "$APP"
fi
codesign --verify --deep --strict "$APP" 2>/dev/null && echo "   signature verifies" \
    || warn "the app's signature does not verify strictly (codesign --verify --deep --strict -vv)"

# ------------------------------------------------------------------ smoke test
step "smoke test"
runner=()
[ "$ARCH" = x86_64 ] && [ "$HOST" = arm64 ] && runner=(arch -x86_64)
if QT_QPA_PLATFORM=offscreen ${runner[@]+"${runner[@]}"} "$MACOS/$B_EXE" --help >/dev/null 2>&1; then
    echo "   $B_EXE --help ran"
else
    warn "the built app did not run ($MACOS/$B_EXE --help); run it from a Terminal to see why"
fi

# ------------------------------------------------------------------ outputs
step "dist/mac"
mkdir -p "$OUT"
rm -rf "$OUT/$B_APP_NAME.app"
ditto "$APP" "$OUT/$B_APP_NAME.app"                # keeps the signature and permissions
if [ "$BASE_ONLY" = 0 ] && [ -d dist/modules ]; then
    mkdir -p "$OUT/module-installers"
    cp -f dist/modules/*.ovtmod "$OUT/module-installers/" 2>/dev/null || true
fi
OUTPUTS=("$OUT/$B_APP_NAME.app")

if [ "$DMG" = 1 ]; then
    STAGE="$(mktemp -d)"
    # One app goes into /Applications: OVT-Suite.app is the launcher for the whole suite
    # (it opens the tool picker; ovtsuite/gui_toolpicker.py).
    ditto "$APP" "$STAGE/$B_APP_NAME.app"
    ln -s /Applications "$STAGE/Applications"
    DMG_OUT="$OUT/$B_APP_NAME-$B_VERSION-$ARCH.dmg"
    rm -f "$DMG_OUT"
    VOLNAME="$B_APP_NAME $B_VERSION"
    # a window that shows what to do: the app, an arrow, Applications. Best effort -- the
    # layout is set through Finder (AppleScript), so it needs a logged-in desktop session;
    # without one the .dmg is plain but still holds the app and the Applications link.
    mkdir -p "$STAGE/.background"
    "$VPY" tools/mac_package/dmg_background.py "$STAGE/.background/background.png" "$B_APP_NAME" \
        2>/dev/null || rm -rf "$STAGE/.background"
    RW="$(mktemp -d)/rw.dmg"
    hdiutil create -quiet -volname "$VOLNAME" -srcfolder "$STAGE" -fs HFS+ \
        -format UDRW -ov "$RW"
    MNT="/Volumes/$VOLNAME"
    hdiutil detach -quiet "$MNT" 2>/dev/null || true
    hdiutil attach -quiet -readwrite -noverify -noautoopen "$RW" >/dev/null
    if [ -d "$STAGE/.background" ]; then bg='set background picture of opts to file ".background:background.png"'; else bg=""; fi
    osascript >/dev/null 2>&1 <<OSA || warn "could not lay out the .dmg window (no Finder session?); it is still usable"
tell application "Finder"
  tell disk "$VOLNAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 120, 800, 520}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 96
    set text size of opts to 13
    $bg
    set position of item "$B_APP_NAME.app" of container window to {150, 185}
    set position of item "Applications" of container window to {450, 185}
    close
    open
    update without registering applications
    delay 2
    close
  end tell
end tell
OSA
    sync
    hdiutil detach -quiet "$MNT" || { sleep 3; hdiutil detach -force -quiet "$MNT"; }
    hdiutil convert -quiet "$RW" -format UDZO -imagekey zlib-level=9 -o "$DMG_OUT"
    rm -f "$RW"
    OUTPUTS+=("$DMG_OUT")
fi

# ------------------------------------------------------------------ checksums
# for the in-app update check (ovtsuite/updates.py): merged into dist/SHA256SUMS.txt beside
# whatever the Windows and Linux builds put there, and signed with the module signing key
if [ "$UNSIGNED" = 0 ] && [ "$BASE_ONLY" = 0 ]; then
    sumargs=()
    [ -n "${DMG_OUT:-}" ] && sumargs+=("$DMG_OUT")
    [ -d "$OUT/module-installers" ] && sumargs+=("$OUT/module-installers")
    if [ ${#sumargs[@]} -gt 0 ]; then
        step "release checksums"
        "$VPY" tools/build_modules.py release-sums --out "$ROOT/dist/SHA256SUMS.txt" \
            "${sumargs[@]}" || warn "checksums not written"
    fi
fi

step "done"
for f in "${OUTPUTS[@]}"; do
    printf '   %s  (%s)\n' "${f#"$ROOT"/}" "$(du -sh "$f" | cut -f1)"
done
echo
echo "Run:      open \"${OUT#"$ROOT"/}/$B_APP_NAME.app\"      (or $B_EXE from Terminal: ${OUT#"$ROOT"/}/$B_APP_NAME.app/Contents/MacOS/$B_EXE --help)"
echo "Install:  drag $B_APP_NAME.app to /Applications"
