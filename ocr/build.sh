#!/bin/bash
# Builds doc2md-ocr, the Apple Vision OCR helper.
#
# Optional: without it doc2md uses Tesseract exactly as before. Installs to
# ~/.local/bin, which doc2md checks along with the repo's own ocr/bin.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/.local/bin}"

command -v swiftc >/dev/null 2>&1 || {
  printf '  \033[31m✗\033[0m swiftc not found. Install the Command Line Tools: xcode-select --install\n'
  exit 1
}

mkdir -p "$DEST"
swiftc -O -swift-version 5 \
  -target "$(uname -m)-apple-macos13.0" \
  -o "$DEST/doc2md-ocr" \
  "$HERE/ocr.swift"

printf '  \033[32m✓\033[0m built %s\n' "$DEST/doc2md-ocr"
printf '\nRun doc2md --verbose to confirm it is picked up.\n'
