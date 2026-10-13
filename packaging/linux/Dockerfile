# Linux build image for build_linux.sh --docker.
#
# Ubuntu 20.04 = glibc 2.31. A PyInstaller bundle needs the glibc it was built against or
# newer, so building here keeps the packages installable on Debian 11+, Ubuntu 20.04+,
# Mint 20+, Fedora, RHEL/Alma/Rocky 9 and openSUSE Leap 15.6+ instead of only on distros
# as new as the build machine. PySide6's wheels (manylinux_2_28) need glibc 2.28, so 2.31
# costs nothing. Same approach as the Audi-Seat-Hacker Linux build.
#
# The app needs Python 3.10+ and 20.04 only ships 3.9, so the interpreter is a uv-managed
# python-build-standalone 3.11 (itself built for glibc 2.17, with the shared libpython
# PyInstaller needs).
#
# build_linux.sh builds and runs this automatically; by hand:
#   docker build -t <name> -f packaging/linux/Dockerfile packaging/linux
#   docker run --rm -v "$PWD":/src -w /src <name> bash ./build_linux.sh --in-container [--rpm]
FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

# binutils: objdump for PyInstaller's dependency scan and the glibc-floor check.
# dpkg (dpkg-deb) is in the base image; rpm provides rpmbuild for --rpm.
# The rest are Qt's X11 runtime libraries: PyInstaller imports PySide6 at build time to find
# its plugins, which fails without them (the libEGL.so.1 error).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates binutils rpm \
        libusb-1.0-0 libglib2.0-0 libdbus-1-3 libfontconfig1 libegl1 libgl1 \
        libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-xinerama0 libxcb-xkb1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv python install 3.11 \
    && ln -s "$(uv python find 3.11)" /usr/local/bin/python3.11 \
    && python3.11 -c "import sys, venv, ensurepip; print(sys.version)"

ENV BUILD_PYTHON=/usr/local/bin/python3.11
WORKDIR /src
