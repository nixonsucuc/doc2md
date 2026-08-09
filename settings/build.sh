#!/bin/bash
# Builds "doc2md Settings.app" with swiftc — no Xcode project, no dependencies
# beyond the Command Line Tools.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/doc2md Settings.app"
ICON_SRC="$HERE/../assets/icon.png"

ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo
echo "Building doc2md Settings.app"
echo

command -v swiftc >/dev/null 2>&1 \
  || die "swiftc not found. Install the Command Line Tools: xcode-select --install"

mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Swift 6 defaults to strict concurrency checking, which this single-window app
# has no need of; language mode 5 keeps the build quiet without weakening it.
# -parse-as-library: with a single source file swiftc would otherwise treat it as
# a script with top-level code, which @main is not allowed to coexist with.
swiftc -O -swift-version 5 -parse-as-library \
  -target "$(uname -m)-apple-macos13.0" \
  -o "$APP/Contents/MacOS/doc2md Settings" \
  "$HERE/Settings.swift"
ok "compiled"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>doc2md Settings</string>
  <key>CFBundleDisplayName</key><string>doc2md Settings</string>
  <key>CFBundleExecutable</key><string>doc2md Settings</string>
  <key>CFBundleIdentifier</key><string>com.nixonsucuc.doc2md.settings</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
ok "wrote Info.plist"

if [ -f "$ICON_SRC" ]; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size*2)) $((size*2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$(dirname "$ICONSET")"
  ok "applied icon"
fi

# Ad-hoc signature. Unsigned SwiftUI apps are killed on launch by Gatekeeper on
# Apple silicon; this is enough for a locally built app run by its author.
codesign --force --sign - "$APP" 2>/dev/null && ok "ad-hoc signed" || true

touch "$APP"
ok "installed at $APP"
echo
