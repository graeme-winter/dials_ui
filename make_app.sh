#!/bin/bash
#
# make_app.sh — bundle dials_gui.py into a double-clickable dials_gui.app
#
# Usage:
#   ./make_app.sh                 # bundles ./dials_gui.py -> ./dials_gui.app
#   ./make_app.sh path/to/foo.py  # bundles a differently-named/located script
#
# Run this from (or point it at) the directory containing dials_gui.py.
# The dialsgui/ package must sit next to it (dials_gui.py is a thin launcher
# that imports it); both are copied into the bundle.
# It must be run on macOS: it uses macOS's `sips`/`iconutil` to turn the
# icon embedded in dialsgui/icon.py into a proper .icns, and produces a
# standard macOS .app bundle (Contents/MacOS, Contents/Resources, Info.plist).
#
# The resulting .app is a *thin* wrapper: it does not freeze Python or
# wxPython into the bundle, it just launches your system `python3` on
# dials_gui.py (which is copied into the bundle's Resources). That means
# whatever Python environment already has wxPython + DIALS on PATH is
# what runs the GUI — nothing about your existing setup changes.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_PATH="${1:-dials_gui.py}"
APP_NAME="dials_gui"                      # -> dials_gui.app
BUNDLE_ID="org.necat.dialsgui"
DISPLAY_NAME="DIALS Workflow GUI"
VERSION="1.0.0"
# Python interpreter used at launch time; override with:
#   DIALS_GUI_PYTHON=/path/to/python3 ./make_app.sh
PYTHON_BIN_DEFAULT="python3"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: .app bundles are a macOS concept — this script must be run on macOS" >&2
    echo "       (it needs the built-in 'sips' and 'iconutil' tools to build the icon)" >&2
    exit 1
fi

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "error: can't find '$SCRIPT_PATH'" >&2
    echo "       run this from the directory containing dials_gui.py, or pass its path" >&2
    exit 1
fi

for tool in sips iconutil python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: required tool '$tool' not found on PATH" >&2
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
SCRIPT_FILE="$(basename "$SCRIPT_PATH")"
APP_DIR="$SCRIPT_DIR/${APP_NAME}.app"

echo "Bundling $SCRIPT_DIR/$SCRIPT_FILE -> $APP_DIR"

if [[ -e "$APP_DIR" ]]; then
    echo "  removing existing $APP_DIR"
    rm -rf "$APP_DIR"
fi

# ---------------------------------------------------------------------------
# 1. Bundle skeleton
# ---------------------------------------------------------------------------
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cp "$SCRIPT_DIR/$SCRIPT_FILE" "$APP_DIR/Contents/Resources/$SCRIPT_FILE"

# The launcher (dials_gui.py) is a thin shim that imports the dialsgui/
# package sitting next to it, so the package must be bundled too.
PKG_DIR="$SCRIPT_DIR/dialsgui"
if [[ ! -d "$PKG_DIR" ]]; then
    echo "error: expected the 'dialsgui' package directory next to $SCRIPT_FILE" >&2
    echo "       (looked in $PKG_DIR)" >&2
    exit 1
fi
cp -R "$PKG_DIR" "$APP_DIR/Contents/Resources/dialsgui"
rm -rf "$APP_DIR/Contents/Resources/dialsgui/__pycache__"

# ---------------------------------------------------------------------------
# 2. Extract the icon embedded in dialsgui/icon.py (APP_ICON_PNG_BASE64) and
#    build an .icns from it via sips + iconutil.
# ---------------------------------------------------------------------------
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

python3 - "$PKG_DIR/icon.py" "$WORKDIR/icon_src.png" <<'PYEOF'
import base64
import re
import sys

script_path, out_path = sys.argv[1], sys.argv[2]
with open(script_path, "r") as f:
    source = f.read()

match = re.search(
    r'APP_ICON_PNG_BASE64\s*=\s*\((.*?)\)', source, re.DOTALL
)
if not match:
    print("error: could not find APP_ICON_PNG_BASE64 constant in the script", file=sys.stderr)
    sys.exit(1)

# The constant is a parenthesised concatenation of quoted string literals,
# one per line — pull out the quoted chunks and join them.
chunks = re.findall(r'"([^"]*)"', match.group(1))
b64 = "".join(chunks)
if not b64:
    print("error: APP_ICON_PNG_BASE64 constant was found but is empty", file=sys.stderr)
    sys.exit(1)

with open(out_path, "wb") as f:
    f.write(base64.b64decode(b64))
PYEOF

ICONSET="$WORKDIR/AppIcon.iconset"
mkdir -p "$ICONSET"

# Standard macOS iconset sizes. sips will upscale from the source PNG as
# needed (the source icon is small, so the larger sizes will be soft, but
# this reproduces exactly the image you supplied at every size macOS asks
# for).
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$WORKDIR/icon_src.png" \
        --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$WORKDIR/icon_src.png" \
        --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done

iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns"

# ---------------------------------------------------------------------------
# 3. Launcher executable
# ---------------------------------------------------------------------------
LAUNCHER="$APP_DIR/Contents/MacOS/$APP_NAME"

cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Auto-generated launcher — runs the bundled dials_gui.py with the system
# Python. Override the interpreter with the DIALS_GUI_PYTHON env var.
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
RESOURCES="\$HERE/../Resources"
PYTHON_BIN="\${DIALS_GUI_PYTHON:-$PYTHON_BIN_DEFAULT}"

if ! command -v "\$PYTHON_BIN" >/dev/null 2>&1; then
    osascript -e 'display alert "DIALS Workflow GUI" message "Could not find '"'"'python3'"'"' on PATH. Set the DIALS_GUI_PYTHON environment variable to a Python with wxPython installed, or launch dials_gui.py directly from a terminal where your DIALS environment is sourced." as critical'
    exit 1
fi

exec "\$PYTHON_BIN" "\$RESOURCES/$SCRIPT_FILE" "\$@"
EOF

chmod +x "$LAUNCHER"

# ---------------------------------------------------------------------------
# 4. Info.plist
# ---------------------------------------------------------------------------
cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$DISPLAY_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$DISPLAY_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon.icns</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHumanReadableCopyright</key>
    <string></string>
</dict>
</plist>
EOF

# ---------------------------------------------------------------------------
# 5. Nudge Finder/Dock to pick up the new icon immediately
# ---------------------------------------------------------------------------
touch "$APP_DIR"
if command -v qlmanage >/dev/null 2>&1; then
    qlmanage -r cache >/dev/null 2>&1 || true
fi
killall Finder >/dev/null 2>&1 || true

echo "Done: $APP_DIR"
echo "Double-click it, or run: open \"$APP_DIR\""
