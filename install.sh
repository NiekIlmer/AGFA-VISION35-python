#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Niek Ilmer <niek@beunie.nl>
# Install the `scan` command system-wide (needs root). Usage: ./install.sh [--uninstall]
# PREFIX defaults to /usr/local.
set -e
PREFIX="${PREFIX:-/usr/local}"
if [ "$1" = "--uninstall" ]; then
    rm -f "$PREFIX/bin/scan"
    rm -rf "$PREFIX/lib/vision35"
    echo "removed $PREFIX/bin/scan and $PREFIX/lib/vision35"
    exit 0
fi
here="$(cd "$(dirname "$0")" && pwd)"
install -d "$PREFIX/lib/vision35" "$PREFIX/bin"
install -m 755 "$here/scan.py" "$PREFIX/lib/vision35/scan.py"
ln -sfn "$PREFIX/lib/vision35/scan.py" "$PREFIX/bin/scan"
echo "installed: $PREFIX/bin/scan -> $PREFIX/lib/vision35/scan.py"
