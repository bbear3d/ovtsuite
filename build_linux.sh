#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# Build the app for Linux and package it as a .deb, and optionally an .rpm.
#
#   ./build_linux.sh                  # build on this machine -> dist/<pkg>_<ver>-1_<arch>.deb
#   ./build_linux.sh --rpm            # ... plus dist/<pkg>-<ver>-1.<arch>.rpm
#   ./build_linux.sh --docker --rpm   # build inside Ubuntu 20.04 (portable, see below)
#
# Options
#   --rpm           also build an .rpm (needs rpmbuild: the 'rpm' package on Debian/Ubuntu,
#                   'rpm-build' on Fedora). Package dependencies are written as sonames, so the
#                   one .rpm resolves on Fedora, RHEL/Alma/Rocky and openSUSE alike.
#   --docker        run the whole build in packaging/linux/Dockerfile (Ubuntu 20.04, glibc 2.31).
#                   A PyInstaller bundle needs the glibc it was BUILT against or newer, so
#                   building on a new distro silently drops older ones (Debian 11, Ubuntu 20.04,
#                   Mint 20). The container pins that floor low on purpose. Recommended for any
#                   package you hand to someone else.
#   --skip-build    package the existing PyInstaller output (build/linux/app) without rebuilding
#   --modules LIST  module packages to build (comma-separated ids, default all); each becomes
#                   dist/module-installers/<pkg>-module-<id>_<module ver>_all.deb
#   --base-only     no modules: only the base app's .deb
#   --unsigned      do not sign the module packages (development only; the app refuses them
#                   unless OVT_ALLOW_UNSIGNED=1)
#   --rebuild-packages  rebuild every .deb even when its contents have not changed (by default a
#                   .deb whose files, control data and this script match what it was last built
#                   from is left as it is)
#   --regen-data    regenerate the definition files and search index from Reference/ first.
#                   By default the checked-in ovtsuite/data is packaged as it is -- exactly
#                   what the Windows build (build.ps1) ships -- so both platforms carry the same
#                   data and a build never rewrites tracked files in a shared checkout.
#   --test          run the targeted tests that cover this path first (OpenPort, FTDI, branding);
#                   never the full suite
#   --release N     package release number (default 1)
#   --python PATH   interpreter for the build venv (needs 3.10+). Default: python3, else
#                   python3.13 ... python3.10 on PATH, else a private Python 3.11 fetched with
#                   uv into ~/.cache (nothing system-wide is installed or changed). That makes
#                   an older WSL distro such as Ubuntu 20.04 work as-is -- and building there is
#                   a feature: its glibc 2.31 floor is what makes the packages portable.
#   --venv DIR      build venv (default: ~/.cache/<pkg>-build/venv-<pyver>-<arch>)
#   --update        re-run the dependency install, upgrading, even if nothing changed
#   -h, --help
#
# Layout of what gets installed
#   /opt/<pkg>/                        the PyInstaller one-folder app (the BASE app)
#   /opt/<pkg>/modules/<id>/           a module, from its own .deb (<pkg>-module-<id>)
#   /usr/bin/<pkg>                     launcher (no args -> GUI, args -> CLI)
#   /usr/share/applications/<pkg>.desktop, hicolor icons from packaging/<pkg>.ico
#   /usr/lib/udev/rules.d/70-<pkg>.rules
#       'uaccess' for the OpenPort 2.0 (0403:cc4d, pyusb route), FTDI K-line cables (pyftdi
#       and /dev/ttyUSB* routes) and CH34x K-line cables (/dev/ttyUSB*), so the logged-in
#       user reaches them without root,
#       without the dialout/plugdev groups, and without a re-login; and ModemManager told
#       to leave the OpenPort (a CDC-ACM device) alone.
#
# Every name (package, product, executable, version) is read from the package's branding
# module -- this script spells none of them, so a rename needs no edit here.
#
# Windows checkout under WSL: works, but build output lands on the Windows drive (slow), and
# that drive reports every file as 0777 -- permissions are normalised while staging, so the
# packages come out right regardless.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WANT_RPM=0; DOCKER=0; IN_CONTAINER=0; SKIP_BUILD=0; RUN_TESTS=0; UPDATE=0; REGEN=0
MODULES=all; BASE_ONLY=0; UNSIGNED=0; REBUILD_PKGS=0
RELEASE=1; PY="${BUILD_PYTHON:-}"; VENV=""
PASSTHRU=()

die()  { echo "!! $*" >&2; exit 1; }
warn() { echo "!! WARNING: $*" >&2; }
step() { echo; echo "== $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --rpm)          WANT_RPM=1;   PASSTHRU+=("$1") ;;
        --docker)       DOCKER=1 ;;
        --in-container) IN_CONTAINER=1 ;;
        --skip-build)   SKIP_BUILD=1; PASSTHRU+=("$1") ;;
        --test)         RUN_TESTS=1;  PASSTHRU+=("$1") ;;
        --update)       UPDATE=1;     PASSTHRU+=("$1") ;;
        --regen-data)   REGEN=1;      PASSTHRU+=("$1") ;;
        --modules)      MODULES="${2:?--modules needs a list}"; PASSTHRU+=("$1" "$2"); shift ;;
        --base-only)    BASE_ONLY=1;  PASSTHRU+=("$1") ;;
        --unsigned)     UNSIGNED=1;   PASSTHRU+=("$1") ;;
        --rebuild-packages) REBUILD_PKGS=1; PASSTHRU+=("$1") ;;
        --release)      RELEASE="${2:?--release needs a number}"; PASSTHRU+=("$1" "$2"); shift ;;
        --python)       PY="${2:?--python needs a path}"; shift ;;
        --venv)         VENV="${2:?--venv needs a directory}"; shift ;;
        -h|--help)      sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *)              die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
[[ "$RELEASE" =~ ^[0-9]+$ ]] || die "--release must be a whole number"

# ------------------------------------------------------------------ docker hand-off
if [ "$DOCKER" = 1 ]; then
    command -v docker >/dev/null 2>&1 || die "docker not found (install Docker, or build without --docker)"
    base="$(basename "$ROOT" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '-')"
    img="${DOCKER_IMAGE:-${base}-linux-build:ubuntu20.04}"
    step "docker image $img"
    docker build -t "$img" -f packaging/linux/Dockerfile packaging/linux
    tty=(); [ -t 1 ] && tty=(-t)
    step "building inside $img"
    # The named volume keeps the build venv and pip cache between runs (PySide6 is large).
    exec docker run --rm "${tty[@]}" \
        -v "$ROOT":/src -w /src \
        -v "${base}-linux-build-cache":/cache -e XDG_CACHE_HOME=/cache \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        "$img" bash ./build_linux.sh --in-container "${PASSTHRU[@]}"
fi

if [ "$IN_CONTAINER" = 1 ]; then
    # Root inside the container: hand everything this run creates back to the host user.
    export PYTHONDONTWRITEBYTECODE=1
    MARK="$(mktemp)"
    give_back() {
        [ -n "${HOST_UID:-}" ] || return 0
        find "$ROOT" -xdev -newer "$MARK" -uid 0 \
            -exec chown -h "$HOST_UID:${HOST_GID:-$HOST_UID}" {} + 2>/dev/null || true
        rm -f "$MARK"
    }
    trap give_back EXIT
fi

# ------------------------------------------------------------------ interpreter + names
TOOLS="${XDG_CACHE_HOME:-$HOME/.cache}/$(basename "$ROOT" | tr '[:upper:]' '[:lower:]')-build-tools"

py_ok() {   # $1 runs and is Python 3.10+
    command -v "$1" >/dev/null 2>&1 \
        && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

private_python() {
    # A python-build-standalone 3.11 managed by uv, all under $TOOLS. Those builds target
    # glibc 2.17 and ship the shared libpython PyInstaller needs, so they run on any distro
    # PySide6 itself supports and do not raise the bundle's glibc floor.
    local uv
    # UV_PYTHON_BIN_DIR: newer uv also drops a python3.x shim on PATH (~/.local/bin); keep
    # that inside $TOOLS too.
    export UV_PYTHON_INSTALL_DIR="$TOOLS/python" UV_PYTHON_BIN_DIR="$TOOLS/bin" UV_CACHE_DIR="$TOOLS/uv-cache"
    uv="$(command -v uv || true)"
    for c in "$TOOLS/uv/uv" "$TOOLS/uv/bin/uv"; do [ -x "$c" ] && uv="$c"; done
    if [ -z "$uv" ]; then
        step "fetching uv into $TOOLS/uv (one-time; nothing system-wide is changed)"
        mkdir -p "$TOOLS/uv"
        # 1) From PyPI with whatever Python 3 is here (any version): the official uv wheel for
        #    this machine, picked by its wheel platform tag, checked against PyPI's sha256.
        if command -v python3 >/dev/null 2>&1; then
            python3 - "$TOOLS/uv" <<'UVEOF' || warn "could not fetch uv from PyPI"
import hashlib, io, json, os, platform, sys, urllib.request, zipfile
dest, arch = sys.argv[1], platform.machine()
meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/uv/json", timeout=60))
for f in meta["urls"]:
    name = f["filename"]
    if not name.endswith(".whl"):
        continue
    plat = name[:-4].split("-")[-1]               # wheel tag: ...-<python>-<abi>-<platform>
    if not any(p.startswith("manylinux") and p.endswith("_" + arch) for p in plat.split(".")):
        continue
    data = urllib.request.urlopen(f["url"], timeout=300).read()
    if hashlib.sha256(data).hexdigest() != f["digests"]["sha256"]:
        sys.exit("uv wheel failed its sha256 check")
    whl = zipfile.ZipFile(io.BytesIO(data))
    for member in whl.namelist():
        if member.endswith(".data/scripts/uv"):
            out = os.path.join(dest, "uv")
            with open(out, "wb") as fh:
                fh.write(whl.read(member))
            os.chmod(out, 0o755)
            print("   uv %s from PyPI (%s)" % (meta["info"]["version"], name))
            sys.exit(0)
sys.exit("no manylinux uv wheel for %s on PyPI" % arch)
UVEOF
        fi
        # 2) No Python 3 at all (or PyPI unreachable): Astral's installer.
        if [ ! -x "$TOOLS/uv/uv" ]; then
            local get=()
            if command -v curl >/dev/null 2>&1; then get=(curl -LsSf https://astral.sh/uv/install.sh)
            elif command -v wget >/dev/null 2>&1; then get=(wget -qO- https://astral.sh/uv/install.sh)
            fi
            if [ ${#get[@]} -gt 0 ]; then
                "${get[@]}" | env UV_INSTALL_DIR="$TOOLS/uv" UV_NO_MODIFY_PATH=1 \
                    INSTALLER_NO_MODIFY_PATH=1 sh || true
            fi
        fi
        for c in "$TOOLS/uv/uv" "$TOOLS/uv/bin/uv"; do [ -x "$c" ] && uv="$c"; done
        [ -n "$uv" ] || die "could not get uv (no network?). Install python3.10+ yourself (e.g. the
   deadsnakes PPA on Ubuntu), or build with --docker"
    fi
    step "private Python 3.11 (uv, under $TOOLS/python)"
    "$uv" python install 3.11 || die "uv could not install Python 3.11"
    PY="$("$uv" python find 3.11)" || die "uv installed Python 3.11 but cannot find it"
}

if [ -n "$PY" ]; then
    # Asked for explicitly (--python or BUILD_PYTHON): use that or stop, never swap silently.
    py_ok "$PY" || die "$PY is missing or older than 3.10 ($("$PY" -V 2>&1 || true)); the app needs 3.10+"
else
    for c in python3 python3.13 python3.12 python3.11 python3.10; do
        if py_ok "$c"; then PY="$c"; break; fi
    done
    if [ -z "$PY" ]; then
        echo "System Python is $(python3 -V 2>&1 || echo 'missing'); the app needs 3.10+."
        private_python
    fi
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

# pyproject.toml's [tool.setuptools.dynamic] version attr is the declared pointer to the
# branding module; load that file directly (importing the package would pull in far more).
eval "$("$PY" - "$ROOT" <<'EOF'
import importlib.util, os, re, shlex, sys
root = sys.argv[1]
m = re.search(r'attr\s*=\s*"([\w.]+)\.__version__"',
              open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read())
if not m:
    sys.exit("pyproject.toml has no version attr pointing at the branding module")
path = os.path.join(root, *m.group(1).split(".")) + ".py"
spec = importlib.util.spec_from_file_location("_branding", path)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
for var, attr in (("PKG", "PKG"), ("APP_NAME", "APP_NAME"), ("APP_LONG", "APP_LONG"),
                  ("APP_DESC", "APP_DESC"), ("EXE", "EXE_NAME"), ("VERSION", "__version__")):
    print("B_%s=%s" % (var, shlex.quote(str(getattr(b, attr)))))
EOF
)"
[[ "$B_PKG" =~ ^[a-z0-9][a-z0-9+.-]+$ ]] || die "package name '$B_PKG' is not a valid Debian/RPM name"
[[ "$B_VERSION" =~ ^[0-9A-Za-z.+~]+$ ]] || die "version '$B_VERSION' is not usable in an RPM (no '-')"

MACH="$(uname -m)"
case "$MACH" in
    x86_64)  DEB_ARCH=amd64 ;;
    aarch64) DEB_ARCH=arm64 ;;
    *)       die "unsupported architecture $MACH (PySide6 wheels exist for x86_64 and aarch64)" ;;
esac
command -v dpkg >/dev/null 2>&1 && DEB_ARCH="$(dpkg --print-architecture)"

echo "$B_APP_NAME $B_VERSION-$RELEASE  ($B_PKG, $MACH, Python $PYVER)"

# Fail before the long build if a packager is missing.
command -v dpkg-deb >/dev/null 2>&1 \
    || die "dpkg-deb not found (Debian/Ubuntu: it is part of dpkg; Fedora: dnf install dpkg)"
if [ "$WANT_RPM" = 1 ]; then
    command -v rpmbuild >/dev/null 2>&1 \
        || die "rpmbuild not found (Debian/Ubuntu: apt install rpm; Fedora: dnf install rpm-build)"
fi

# PySide6 (>= 6.6) publishes manylinux_2_28 wheels only: below glibc 2.28 pip finds none.
SYS_GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
if [ -n "$SYS_GLIBC" ] && [ "$(printf '%s\n2.28\n' "$SYS_GLIBC" | sort -V | head -1)" != "2.28" ]; then
    die "this system has glibc $SYS_GLIBC; PySide6 needs 2.28+ (Ubuntu 20.04 / Debian 10 or newer).
   Use --docker, or a newer WSL distro:  wsl --install -d Ubuntu-20.04"
fi

# PyInstaller imports PySide6 while it builds, so Qt's runtime libraries must be present on
# the BUILD machine too -- a minimal WSL or server install usually lacks some. Checked by
# soname before the long download, with the package to install for each.
if [ "$SKIP_BUILD" = 0 ]; then
    missing=(); pkgs=()
    # no grep -q: under pipefail its early exit SIGPIPEs ldconfig and fails the check at random
    have_lib() { PATH="$PATH:/sbin:/usr/sbin" ldconfig -p 2>/dev/null | grep "[[:space:]]$1 " >/dev/null; }
    for pair in libEGL.so.1:libegl1 libGL.so.1:libgl1 libfontconfig.so.1:libfontconfig1 \
                libxkbcommon.so.0:libxkbcommon0 libdbus-1.so.3:libdbus-1-3 \
                libglib-2.0.so.0:libglib2.0-0; do
        have_lib "${pair%%:*}" || { missing+=("${pair%%:*}"); pkgs+=("${pair##*:}"); }
    done
    command -v objdump >/dev/null 2>&1 || { missing+=(objdump); pkgs+=(binutils); }
    if [ ${#missing[@]} -gt 0 ]; then
        hint="install the packages providing them"
        [ -f /etc/debian_version ] && hint="sudo apt install ${pkgs[*]}"
        die "missing on this build machine: ${missing[*]}
   $hint"
    fi
fi

# ------------------------------------------------------------------ build venv
VENV="${VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/$B_PKG-build/venv-$PYVER-$MACH}"
VPY="$VENV/bin/python"
# Keep in step with $deps in build.ps1. Pillow is build-time only (icon conversion); the
# .spec excludes it from the bundle.
# cryptography: module signatures (Ed25519) and the ZF 8HP Gen2 AES codec
DEPS=('PySide6>=6.6' 'numpy>=1.24' 'pyserial>=3.5' 'pyusb>=1.2' 'pyftdi>=0.55'
      'cryptography>=41' 'pyinstaller>=6.6' 'pillow>=9')
[ "$RUN_TESTS" = 1 ] && DEPS+=('pytest')

step "build venv $VENV"
if [ ! -x "$VPY" ]; then
    mkdir -p "$(dirname "$VENV")"
    "$PY" -m venv "$VENV" \
        || die "could not create the venv (Debian/Ubuntu: apt install python$PYVER-venv)"
fi
stamp="$VENV/.requirements"
want="$(printf '%s\n' "${DEPS[@]}" | sha256sum | cut -c1-16)"
if [ "$UPDATE" = 1 ] || [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
    upgrade=(); [ "$UPDATE" = 1 ] && upgrade=(--upgrade)
    # A distro pip can predate manylinux_2_28 tags (PySide6's wheels): upgrade it first.
    "$VPY" -m pip install --quiet --upgrade pip
    "$VPY" -m pip install "${upgrade[@]}" "${DEPS[@]}"
    echo "$want" > "$stamp"
else
    echo "   requirements unchanged - skipped (--update to refresh)"
fi

# ------------------------------------------------------------------ tests (targeted only)
if [ "$RUN_TESTS" = 1 ]; then
    step "targeted tests"
    QT_QPA_PLATFORM=offscreen "$VPY" -m pytest -q \
        tests/test_openport.py tests/test_ftdi.py tests/test_branding.py
fi

# ------------------------------------------------------------------ PyInstaller build
# Its own dist/work folders under build/linux, so the Windows build's dist/<exe> in a shared
# checkout is never overwritten.
APP_PARENT="$ROOT/build/linux/app"
APP="$APP_PARENT/$B_EXE"
if [ "$SKIP_BUILD" = 1 ]; then
    [ -x "$APP/$B_EXE" ] || die "--skip-build: no previous build at $APP"
    step "reusing $APP"
else
    step "PyInstaller build"
    # Start clean: a failed build must never leave an older bundle behind to be packaged.
    rm -rf "$APP_PARENT"
    regen=(--no-regen); [ "$REGEN" = 1 ] && regen=()
    modopts=(--modules "$MODULES")
    [ "$BASE_ONLY" = 1 ] && modopts+=(--base-only)
    [ "$UNSIGNED" = 1 ] && modopts+=(--unsigned)
    "$VPY" build.py --no-test "${regen[@]}" "${modopts[@]}" --distpath "$APP_PARENT" \
        --workpath "$ROOT/build/linux/work"
    [ -x "$APP/$B_EXE" ] || die "PyInstaller produced no $APP/$B_EXE"
fi

# ------------------------------------------------------------------ glibc floor
# Read from each bundled ELF file's version-reference table ("required from libc.so.6:
# GLIBC_2.28 ..."): the newest node referenced per glibc library is what the bundle needs.
# The overall newest is the oldest glibc it runs on. Both become package dependencies, so
# an install on a too-old distro is refused by the package manager instead of crashing at
# launch; the RPM ones name only nodes the bundle really references, so they always exist.
GLIBC_MIN=""; GLIBC_REFS=""
if command -v objdump >/dev/null 2>&1; then
    GLIBC_REFS="$(find "$APP" -type f \( -name '*.so' -o -name '*.so.*' -o -name "$B_EXE" \) \
                     -exec objdump -p {} + 2>/dev/null \
                  | awk '/^[^ ]/ { lib = "" }
                         /^ +required from / { lib = $3; sub(/:$/, "", lib) }
                         lib != "" && $NF ~ /^GLIBC_[0-9]/ { sub(/^GLIBC_/, "", $NF); print lib, $NF }' \
                  | sort -k1,1 -k2,2V | awk '{ v[$1] = $2 } END { for (l in v) print l, v[l] }' \
                  | sort || true)"
    GLIBC_MIN="$(printf '%s\n' "$GLIBC_REFS" | awk 'NF { print $2 }' | sort -V | tail -1)"
    echo "   glibc floor: ${GLIBC_MIN:-unknown}"
    if [ -n "$GLIBC_MIN" ] && [ "$(printf '%s\n2.31\n' "$GLIBC_MIN" | sort -V | tail -1)" != "2.31" ]; then
        warn "the bundle needs glibc $GLIBC_MIN, so it will not install on Debian 11, Ubuntu 20.04"
        warn "or Mint 20. For a portable package build with --docker."
    fi
else
    warn "objdump not found (binutils) - glibc floor not checked, no libc version dependency"
fi

# ------------------------------------------------------------------ stage the payload
STAGE="$(mktemp -d)"
cleanup_stage() { rm -rf "$STAGE"; }
if [ "$IN_CONTAINER" = 1 ]; then trap 'cleanup_stage; give_back' EXIT; else trap cleanup_stage EXIT; fi
PAY="$STAGE/payload"
OPT="$PAY/opt/$B_PKG"
step "staging"
install -d "$OPT" "$PAY/usr/bin" "$PAY/usr/share/applications" \
           "$PAY/usr/lib/udev/rules.d" "$PAY/usr/share/doc/$B_PKG"
cp -a "$APP/." "$OPT/"
# the base app's package carries no module: each has a .deb of its own (below)
rm -rf "$OPT/modules"
# Normalise permissions: only the executable needs +x in a one-folder bundle.
find "$OPT" -type d -exec chmod 0755 {} +
find "$OPT" -type f -exec chmod 0644 {} +
chmod 0755 "$OPT/$B_EXE"
[ -f README.md ] && install -m 0644 README.md "$PAY/usr/share/doc/$B_PKG/README.md"
# Debian policy: the licences of everything shipped, as the package's copyright
# file (written by tools/collect_licenses.py from ovtsuite/credits.py at freeze).
if [ -f "$APP/THIRD-PARTY-NOTICES.txt" ]; then
    install -m 0644 "$APP/THIRD-PARTY-NOTICES.txt" "$PAY/usr/share/doc/$B_PKG/copyright"
else
    echo "WARNING: $APP/THIRD-PARTY-NOTICES.txt missing -- the package ships no licence notices" >&2
fi

cat > "$PAY/usr/bin/$B_PKG" <<EOF
#!/bin/sh
exec /opt/$B_PKG/$B_EXE "\$@"
EOF
chmod 0755 "$PAY/usr/bin/$B_PKG"

# A file goes through the "gui" verb (a bare path would be read as a CLI
# subcommand, packaging/entry.py); "gui" with no file just opens the window.
# StartupWMClass: on Wayland Qt's app_id is the executable name; lets the dock match the entry.
cat > "$PAY/usr/share/applications/$B_PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$B_APP_NAME
GenericName=$B_APP_LONG
Comment=$B_APP_DESC
Exec=/opt/$B_PKG/$B_EXE gui %f
MimeType=application/x-ovt-project;
Icon=$B_PKG
Terminal=false
Categories=Development;Utility;
Keywords=ECU;TCU;EDC16;EDC15;ZF;6HP;J2534;OpenPort;
StartupWMClass=$B_EXE
EOF
chmod 0644 "$PAY/usr/share/applications/$B_PKG.desktop"

# The .ovt project type: by extension, and by its content -- a ZIP whose first,
# stored entry is "mimetype" holding application/x-ovt-project (byte 38 on;
# docs/OVT_CONTAINER.md section 2.1), so a renamed project is still known.
install -d "$PAY/usr/share/mime/packages"
cat > "$PAY/usr/share/mime/packages/$B_PKG.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-ovt-project">
    <comment>$B_APP_NAME project</comment>
    <sub-class-of type="application/zip"/>
    <icon name="application-x-ovt-project"/>
    <glob pattern="*.ovt"/>
    <magic priority="60">
      <match type="string" offset="38" value="application/x-ovt-project"/>
    </magic>
  </mime-type>
</mime-info>
EOF
chmod 0644 "$PAY/usr/share/mime/packages/$B_PKG.xml"

ICO="$ROOT/packaging/$B_PKG.ico"
if [ -f "$ICO" ]; then
    "$VPY" - "$ICO" "$PAY/usr/share/icons/hicolor" "$B_PKG" <<'EOF' || warn "icon conversion failed - packaging without an icon"
import os, sys
from PIL import Image
ico, dest, name = sys.argv[1:]
im = Image.open(ico)
for size in sorted(im.info.get("sizes", {im.size})):
    d = os.path.join(dest, "%dx%d" % size, "apps")
    os.makedirs(d, exist_ok=True)
    im.ico.getimage(size).save(os.path.join(d, name + ".png"))
    print("   icon %dx%d" % size)
EOF
    [ -d "$PAY/usr/share/icons" ] && find "$PAY/usr/share/icons" -type d -exec chmod 0755 {} + \
        && find "$PAY/usr/share/icons" -type f -exec chmod 0644 {} +
else
    warn "no $ICO - packaging without an icon"
fi

# The .ovt file's own icon (tools/make_file_icons.py), by its MIME type name.
for png in "$ROOT"/packaging/ovt_project/*.png; do
    [ -f "$png" ] || continue
    n=$(basename "$png" .png)
    install -D -m 0644 "$png" \
        "$PAY/usr/share/icons/hicolor/${n}x${n}/mimetypes/application-x-ovt-project.png"
done
[ -d "$PAY/usr/share/icons" ] && find "$PAY/usr/share/icons" -type d -exec chmod 0755 {} +

# uaccess must be applied before 73-seat-late.rules, hence 70-. 'uaccess' grants the user of
# the active local (logind) session; someone working only over SSH has no seat, and for them
# the tty nodes stay in the dialout group as usual (both Debian and Fedora).
#
# Device ids:
#   0403:cc4d   Tactrix OpenPort 2.0 (ovtsuite/flash/openport.py VID/PID)
#   0403:6001 6010 6011 6014 6015
#               FTDI's own FT232R / FT2232 / FT4232 / FT232H / FT-X PIDs -- the set pyftdi
#               enumerates for the direct-FTDI route (raw USB access needed)
#   0403:fa24   Ross-Tech HEX-USB, the non-standard PID in flash/ftdi.py KNOWN_NONSTANDARD
#   tty nodes of ANY FTDI (0403) or WCH (1a86: CH340/CH341/CH9102) adapter -- the pyserial
#               K-line route opens /dev/ttyUSB* whatever the PID (flash/diag.py CH34X_VID)
RULES="$PAY/usr/lib/udev/rules.d/70-$B_PKG.rules"
cat > "$RULES" <<EOF
# $B_APP_NAME: give the logged-in user access to its diagnostic cables (no root, no groups).

# Tactrix OpenPort 2.0 and clones - direct USB (libusb) route. It enumerates as CDC-ACM
# (/dev/ttyACM*), so ModemManager would probe it with AT commands on plug-in -- the same
# "at..." command set the OpenPort speaks -- and fight the app for it: tell it to keep off.
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0403", ATTR{idProduct}=="cc4d", MODE="0660", TAG+="uaccess", ENV{ID_MM_DEVICE_IGNORE}="1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="cc4d", ENV{ID_MM_DEVICE_IGNORE}="1"

# FTDI K-line cables - direct-FTDI (pyftdi / libusb) route
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0403", ATTR{idProduct}=="6001|6010|6011|6014|6015|fa24", TAG+="uaccess"

# FTDI and WCH CH34x K-line cables as /dev/ttyUSB* - serial (pyserial) route
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403|1a86", TAG+="uaccess"
EOF
chmod 0644 "$RULES"

INSTALLED_KB="$(du -sk "$PAY" | cut -f1)"
UDEV_RELOAD='if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=usb --subsystem-match=tty 2>/dev/null || true
fi
if command -v update-mime-database >/dev/null 2>&1; then
  update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi'

mkdir -p "$ROOT/dist"
OUTPUTS=()

# ------------------------------------------------------------------ runtime dependencies
# The spec leaves OS libraries out of the bundle (packaging/ovtsuite.spec, _drop_os_libraries),
# so the target must provide them. Which ones is read from the bundle itself: every ELF
# file's NEEDED entries that nothing inside the bundle satisfies.
#   hard  what the startup path needs -- the executable, libpython, every Python extension
#         module, the xcb platform plugin, and what those pull in inside the bundle
#   soft  what only on-demand Qt plugins need (GTK theme, Wayland, image formats, ...): a
#         plugin whose library is missing is skipped by Qt, so these are Recommends
# libusb is added by hand: pyusb opens it with ctypes at runtime, so no ELF names it.
step "runtime dependencies (from the bundle's NEEDED entries)"
DEPS_OUT="$("$VPY" - "$OPT" "$B_EXE" <<'DEPEOF'
import os, subprocess, sys
root, exe = sys.argv[1], sys.argv[2]
GLIBC = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
         "libutil.so.1", "libresolv.so.2", "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1"}
names, needed = {}, {}
for dp, _, fns in os.walk(root):
    for fn in fns:
        p = os.path.join(dp, fn)
        names.setdefault(fn, p)
        if os.path.islink(p):
            continue
        with open(p, "rb") as fh:
            if fh.read(4) != b"\x7fELF":
                continue
        out = subprocess.run(["objdump", "-p", p], capture_output=True, text=True).stdout
        needed[p] = [ln.split()[1] for ln in out.splitlines() if ln.strip().startswith("NEEDED")]
def is_root(p):
    f = os.path.basename(p)
    return (p == os.path.join(root, exe) or f.startswith("libpython")
            or ".cpython-" in f or ".abi3." in f or os.sep + "lib-dynload" + os.sep in p
            or p.endswith(os.path.join("platforms", "libqxcb.so")))
hard_files, stack = set(), [p for p in needed if is_root(p)]
while stack:
    p = stack.pop()
    if p in hard_files:
        continue
    hard_files.add(p)
    for so in needed.get(p, ()):
        q = names.get(so)
        if q is not None:
            stack.append(os.path.realpath(q) if os.path.islink(q) else q)
ext = lambda p: {so for so in needed[p] if so not in names and so not in GLIBC}
hard = set().union(*(ext(p) for p in hard_files if p in needed)) | {"libusb-1.0.so.0"}
soft = set().union(*(ext(p) for p in needed)) - hard
for so in sorted(hard): print("hard", so)
for so in sorted(soft): print("soft", so)
DEPEOF
)" || die "could not read the bundle's library dependencies"
HARD_SO=($(printf '%s\n' "$DEPS_OUT" | awk '$1 == "hard" { print $2 }'))
SOFT_SO=($(printf '%s\n' "$DEPS_OUT" | awk '$1 == "soft" { print $2 }'))
echo "   hard: ${HARD_SO[*]}"
echo "   soft: ${SOFT_SO[*]}"

# Debian package names by the library naming policy (libfoo.so.N -> libfooN, libfoo-N when
# the name ends in a digit), plus the few upstream exceptions. Not the build host's own
# package database: a 24.04 host would bake its t64-renamed packages into Depends and break
# installs on Debian 12; the t64 packages Provide the policy names, so those work everywhere.
deb_name() {
    case "$1" in
        libz.so.1)                     echo zlib1g ;;
        libharfbuzz.so.0)              echo libharfbuzz0b ;;
        libglib-2.0.so.0|libgobject-2.0.so.0|libgio-2.0.so.0|libgmodule-2.0.so.0|libgthread-2.0.so.0)
                                       echo libglib2.0-0 ;;
        libgdk-3.so.0)                 echo libgtk-3-0 ;;
        libgdk_pixbuf-2.0.so.0)        echo libgdk-pixbuf2.0-0 ;;
        libatk-1.0.so.0)               echo libatk1.0-0 ;;
        libatk-bridge-2.0.so.0)        echo libatk-bridge2.0-0 ;;
        libatspi.so.0)                 echo libatspi2.0-0 ;;
        libbrotlidec.so.1|libbrotlicommon.so.1|libbrotlienc.so.1)
                                       echo libbrotli1 ;;
        *)  local base="${1%%.so.*}" ver="${1#*.so.}"
            ver="${ver%%.*}"; base="$(printf '%s' "$base" | tr '[:upper:]_' '[:lower:]-')"
            case "$base" in *[0-9]) echo "$base-$ver" ;; *) echo "$base$ver" ;; esac ;;
    esac
}
# Confirm the names against the build host's apt index when it has one (a Docker image
# with its lists deleted, or a non-Debian host, has none: then the policy names stand).
apt_has() {
    apt-cache show "$1" >/dev/null 2>&1 && return 0
    apt-cache showpkg "$1" 2>/dev/null \
        | awk '/^Reverse Provides:/ { f = 1; next } f && NF { found = 1 } END { exit !found }'
}
CHECK_APT=0
command -v apt-cache >/dev/null 2>&1 && apt-cache show libc6 >/dev/null 2>&1 && CHECK_APT=1
DEB_HARD=(); DEB_SOFT=(); unknown=()
for so in "${HARD_SO[@]}"; do
    n="$(deb_name "$so")"
    if [ "$CHECK_APT" = 1 ] && ! apt_has "$n"; then unknown+=("$so -> $n"); continue; fi
    DEB_HARD+=("$n")
done
[ ${#unknown[@]} -eq 0 ] || die "no Debian package found for: ${unknown[*]}
   add the right name to deb_name() in build_linux.sh"
for so in "${SOFT_SO[@]}"; do
    n="$(deb_name "$so")"
    if [ "$CHECK_APT" = 1 ] && ! apt_has "$n"; then
        warn "optional $so: no package '$n' on this host's index - left out of Recommends"
        continue
    fi
    DEB_SOFT+=("$n")
done
dedupe() { printf '%s\n' "$@" | awk 'NF && !seen[$0]++' | paste -sd, - | sed 's/,/, /g'; }
DEB_HARD_LIST="$(dedupe "${DEB_HARD[@]}")"
DEB_SOFT_LIST="$(dedupe $(comm -23 <(printf '%s\n' "${DEB_SOFT[@]}" | sort -u) \
                                  <(printf '%s\n' "${DEB_HARD[@]}" | sort -u)))"

# ------------------------------------------------------------------ .deb
# A package is rebuilt only when what it is made from changed: its whole tree (files and
# DEBIAN/control) plus this script, by content (tools/build_modules.py installer-key). The
# key it was last built from is kept in build/linux/stamps.
PKG_STAMPS="$ROOT/build/linux/stamps"
install -d "$PKG_STAMPS"
build_deb() {   # build_deb ROOTDIR OUT
    local root="$1" out="$2" stamp key old
    stamp="$PKG_STAMPS/$(basename "$out").key"
    key="$("$VPY" "$ROOT/tools/build_modules.py" installer-key --source "$root" \
               --file "$ROOT/build_linux.sh" --text "deb|xz|$(basename "$out")" || true)"
    old="$(cat "$stamp" 2>/dev/null || true)"
    if [ "$REBUILD_PKGS" = 0 ] && [ -n "$key" ] && [ "$key" = "$old" ] && [ -f "$out" ]; then
        echo "   up to date - its inputs have not changed ($(basename "$out"))"
        return 0
    fi
    rm -f "$stamp"      # a failed build must never leave a stamp vouching for an old .deb
    # xz rather than the zstd newer Ubuntu dpkg defaults to: Debian's dpkg only reads zstd from 12.
    dpkg-deb --root-owner-group -Zxz --build "$root" "$out"
    [ -n "$key" ] && printf '%s\n' "$key" > "$stamp"
    return 0
}

step "deb"
DEBROOT="$STAGE/deb"
cp -a "$PAY" "$DEBROOT"
install -d "$DEBROOT/DEBIAN"
LIBC_DEP="libc6"; [ -n "$GLIBC_MIN" ] && LIBC_DEP="libc6 (>= $GLIBC_MIN)"
RECOMMENDS=""; [ -n "$DEB_SOFT_LIST" ] && RECOMMENDS="Recommends: $DEB_SOFT_LIST"
cat > "$DEBROOT/DEBIAN/control" <<EOF
Package: $B_PKG
Version: $B_VERSION-$RELEASE
Section: electronics
Priority: optional
Architecture: $DEB_ARCH
Depends: $LIBC_DEP, $DEB_HARD_LIST
$RECOMMENDS
Installed-Size: $INSTALLED_KB
Maintainer: Blair Campbell <bbear3dca@gmail.com>
Description: $B_APP_LONG ($B_APP_NAME)
 $B_APP_DESC.
 .
 Flashes over J2534 / Tactrix OpenPort 2.0 (direct USB) and FTDI K-line
 cables; a udev rule gives the logged-in user access to them without root.
EOF
sed -i '/^$/d' "$DEBROOT/DEBIAN/control"    # an empty Recommends must not end the stanza
for s in postinst postrm; do
    printf '#!/bin/sh\nset -e\n%s\nexit 0\n' "$UDEV_RELOAD" > "$DEBROOT/DEBIAN/$s"
    chmod 0755 "$DEBROOT/DEBIAN/$s"
done
chmod 0755 "$DEBROOT"
DEB_OUT="$ROOT/dist/${B_PKG}_${B_VERSION}-${RELEASE}_${DEB_ARCH}.deb"
build_deb "$DEBROOT" "$DEB_OUT"
OUTPUTS+=("$DEB_OUT")

# ------------------------------------------------------------------ module .debs
# One per module installed in the build ($APP/modules/<id>, laid out installed by
# tools/build_modules.py): <pkg>-module-<id>, into /opt/<pkg>/modules/<id>. Pure Python
# and data, so Architecture: all. The app re-verifies a module's signature and files at
# every start; dpkg only places them.
if [ "$BASE_ONLY" = 0 ] && [ -d "$APP/modules" ]; then
    for MDIR in "$APP"/modules/*/; do
        MID="$(basename "$MDIR")"
        [ -f "$MDIR/manifest.json" ] || continue
        M_NAME="$("$VPY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["name"])' "$MDIR/manifest.json")"
        M_VER="$("$VPY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$MDIR/manifest.json")"
        step "deb: $B_PKG-module-$MID ($M_NAME $M_VER)"
        MROOT="$STAGE/mod-$MID"
        install -d "$MROOT/DEBIAN" "$MROOT/opt/$B_PKG/modules"
        cp -a "$MDIR" "$MROOT/opt/$B_PKG/modules/$MID"
        find "$MROOT/opt" -type d -exec chmod 0755 {} +
        find "$MROOT/opt" -type f -exec chmod 0644 {} +
        MKB="$(du -sk "$MROOT/opt" | cut -f1)"
        {
            echo "Package: $B_PKG-module-$MID"
            echo "Version: $M_VER-$RELEASE"
            echo "Section: electronics"
            echo "Priority: optional"
            echo "Architecture: all"
            echo "Depends: $B_PKG"
            echo "Installed-Size: $MKB"
            echo "Maintainer: Blair Campbell <bbear3dca@gmail.com>"
            echo "Description: $M_NAME module $M_VER for $B_APP_NAME"
            echo " Adds $M_NAME support (definitions, editing and checksum correction)"
            echo " to $B_APP_NAME. The base app identifies, hex-edits and flashes every"
            echo " supported control module without it."
        } > "$MROOT/DEBIAN/control"
        chmod 0755 "$MROOT"
        # the module packages get a folder of their own; an older version of this
        # module's package goes
        install -d "$ROOT/dist/module-installers"
        MOUT="$ROOT/dist/module-installers/${B_PKG}-module-${MID}_${M_VER}-${RELEASE}_all.deb"
        for OLD in "$ROOT/dist/module-installers/${B_PKG}-module-${MID}_"*_all.deb; do
            if [ -e "$OLD" ] && [ "$OLD" != "$MOUT" ]; then rm -f "$OLD"; fi
        done
        build_deb "$MROOT" "$MOUT"
        OUTPUTS+=("$MOUT")
    done
fi

# ------------------------------------------------------------------ .rpm
if [ "$WANT_RPM" = 1 ]; then
    step "rpm"
    TOP="$STAGE/rpmbuild"
    install -d "$TOP/SPECS" "$TOP/RPMS" "$TOP/BUILD"
    LIBC_REQ="$(printf '%s\n' "$GLIBC_REFS" \
                | awk 'NF { printf "Requires:       %s(GLIBC_%s)(64bit)\n", $1, $2 }')"
    # sonames, so the one package resolves on Fedora, RHEL/Alma/Rocky and openSUSE alike
    REQS="$(for s in "${HARD_SO[@]}"; do echo "Requires:       $s()(64bit)"; done
            for s in "${SOFT_SO[@]}"; do echo "Recommends:     $s()(64bit)"; done)"
    cat > "$TOP/SPECS/$B_PKG.spec" <<EOF
# Repackages a finished PyInstaller bundle: no debuginfo, no automatic dependency scan (it
# would demand every library the bundle already carries), and no brp post-processing
# (stripping or byte-compiling inside the bundle can break it).
%global debug_package %{nil}
%global _build_id_links none
%global __os_install_post %{nil}

Name:           $B_PKG
Version:        $B_VERSION
Release:        $RELEASE
Summary:        $B_APP_LONG ($B_APP_NAME)
License:        Proprietary
BuildArch:      $MACH
AutoReqProv:    no
$LIBC_REQ
$REQS

%description
$B_APP_DESC.

Flashes over J2534 / Tactrix OpenPort 2.0 (direct USB) and FTDI K-line
cables; a udev rule gives the logged-in user access to them without root.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}
cp -a "$PAY/." %{buildroot}/

%files
/opt/$B_PKG
/usr/bin/$B_PKG
/usr/share/applications/$B_PKG.desktop
/usr/share/mime/packages/$B_PKG.xml
/usr/lib/udev/rules.d/70-$B_PKG.rules
%doc /usr/share/doc/$B_PKG
$( [ -d "$PAY/usr/share/icons" ] && echo "/usr/share/icons/hicolor/*/apps/$B_PKG.png" )
$( ls "$PAY"/usr/share/icons/hicolor/*/mimetypes/application-x-ovt-project.png >/dev/null 2>&1 \
   && echo "/usr/share/icons/hicolor/*/mimetypes/application-x-ovt-project.png" )

%post
$UDEV_RELOAD

%postun
$UDEV_RELOAD
EOF
    rpmbuild --quiet --define "_topdir $TOP" -bb "$TOP/SPECS/$B_PKG.spec"
    built="$(find "$TOP/RPMS" -name '*.rpm' | head -1)"
    [ -n "$built" ] || die "rpmbuild produced no .rpm"
    RPM_OUT="$ROOT/dist/$(basename "$built")"
    cp "$built" "$RPM_OUT"
    OUTPUTS+=("$RPM_OUT")
fi

# ------------------------------------------------------------------ checksums
# The release's checksum list, for the in-app update check (ovtsuite/updates.py): these
# packages' SHA-256 are merged into dist/SHA256SUMS.txt (beside whatever a Windows build
# put there) and the list is signed with the module signing key (SHA256SUMS.txt.sig).
# Upload both files with the release. See tools/build_modules.py release-sums.
step "release checksums"
sumopts=(--out "$ROOT/dist/SHA256SUMS.txt")
[ "$UNSIGNED" = 1 ] && sumopts+=(--unsigned)
"$VPY" "$ROOT/tools/build_modules.py" release-sums "${sumopts[@]}" "${OUTPUTS[@]}"

# ------------------------------------------------------------------ done
step "done"
for f in "${OUTPUTS[@]}"; do
    printf '   %s  (%s)\n' "${f#"$ROOT"/}" "$(du -h "$f" | cut -f1)"
done
echo
echo "Install:  sudo apt install ./${DEB_OUT#"$ROOT"/}   (+ any ./dist/module-installers/$B_PKG-module-*.deb)"
[ "$WANT_RPM" = 1 ] && echo "          sudo dnf install ./${RPM_OUT#"$ROOT"/}   (zypper install on openSUSE)"
echo "Run:      $B_PKG   (or from the application menu)"
echo "Unplug and replug the OpenPort / K-line cable once after the first install."
