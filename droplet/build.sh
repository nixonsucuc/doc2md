#!/bin/bash
# Builds doc2md.app, a drag-and-drop droplet, and installs it to ~/Applications.
#
# Everything used here ships with macOS — osacompile, sips, iconutil — so the app
# costs nothing and depends on nothing beyond doc2md itself.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/doc2md.app"
ICON_SRC="$HERE/../dropzone/Convert to Markdown.dzbundle/icon.png"

ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }

echo
echo "Building doc2md.app"
echo

mkdir -p "$DEST"
rm -rf "$APP"

osacompile -o "$APP" "$HERE/droplet.applescript"
ok "compiled droplet"

cp "$HERE/convert.sh" "$APP/Contents/Resources/convert.sh"
chmod +x "$APP/Contents/Resources/convert.sh"
ok "bundled convert.sh"

# osacompile writes a generic droplet icon; swap in the doc2md one. iconutil wants
# a full iconset, so build the sizes it expects from the single PNG.
if [ -f "$ICON_SRC" ]; then
  ICONSET="$(mktemp -d)/doc2md.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/droplet.icns"
  rm -rf "$(dirname "$ICONSET")"
  ok "applied icon"
fi

# Without this the droplet refuses every drop: the Finder decides what an app can
# accept from its declared document types, and osacompile declares none.
plutil -replace CFBundleDocumentTypes -json \
  '[{"CFBundleTypeName":"Document","CFBundleTypeRole":"Viewer","LSItemContentTypes":["public.item"],"CFBundleTypeOSTypes":["****"]}]' \
  "$APP/Contents/Info.plist"
plutil -replace CFBundleName -string "doc2md" "$APP/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string "com.nixonsucuc.doc2md.droplet" "$APP/Contents/Info.plist"
ok "declared accepted file types"

# The Finder caches app metadata aggressively; without a touch the new icon and
# document types can take a relaunch to appear.
touch "$APP"
ok "installed at $APP"

echo
echo "Drag it to your Dock, then drop documents on it."
echo
