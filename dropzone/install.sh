#!/bin/bash
# Installs the "Convert to Markdown" Dropzone action.
#
# Checks the things the action depends on but cannot fix on its own, then hands
# the bundle to Dropzone, which owns the .dzbundle type and runs its own install
# flow (it will ask which grid slot to put the action in).

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$BUNDLE_DIR/Convert to Markdown.dzbundle"
ENV_FILE="$HOME/.config/doc2md/env"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo
echo "doc2md → Dropzone"
echo

[ -d "$BUNDLE" ] || die "Bundle not found at $BUNDLE"
[ -d "/Applications/Dropzone 4.app" ] || [ -d "$HOME/Applications/Dropzone 4.app" ] \
  || die "Dropzone 4 not found in /Applications or ~/Applications."
ok "Dropzone 4 found"

# The action locates this itself at run time; failing here just gives a better
# error now than a dialog later.
DOC2MD=""
for candidate in \
  /Library/Frameworks/Python.framework/Versions/*/bin/doc2md \
  "$HOME"/Library/Python/*/bin/doc2md \
  "$HOME/.local/bin/doc2md" \
  /opt/homebrew/bin/doc2md \
  /usr/local/bin/doc2md
do
  if [ -x "$candidate" ]; then DOC2MD="$candidate"; break; fi
done
[ -n "$DOC2MD" ] || die "doc2md not on this machine. Run: pip3 install -e $(dirname "$BUNDLE_DIR")"
ok "doc2md at $DOC2MD"

# Dropzone launches from the GUI session, which has no Homebrew PATH. The action
# puts /opt/homebrew/bin back, but only if tesseract is actually installed there.
if command -v tesseract >/dev/null 2>&1; then
  ok "tesseract at $(command -v tesseract)"
else
  warn "tesseract not installed — OCR will be unavailable. Fix: brew install tesseract"
fi

# Dropzone never sources a shell rc, so a key exported in .zshrc is invisible to
# it. This file is where the action looks instead.
if [ -f "$ENV_FILE" ] && grep -q '^GEMINI_API_KEY=.\+' "$ENV_FILE" 2>/dev/null; then
  ok "GEMINI_API_KEY configured"
else
  mkdir -p "$(dirname "$ENV_FILE")"
  if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'TEMPLATE'
# Read by the doc2md Dropzone action. Dropzone does not source your shell rc,
# so a key exported in .zshrc will not reach it — put it here instead.
# Without a key, conversions still run; diagrams just are not described.
# GEMINI_API_KEY=your-key-here
TEMPLATE
    chmod 600 "$ENV_FILE"
  fi
  warn "No GEMINI_API_KEY — diagrams won't be described. Add one to $ENV_FILE"
fi

if [ "${1:-}" = "--check" ]; then
  echo
  echo "Checks only; not installing."
  exit 0
fi

echo
echo "Handing the bundle to Dropzone — pick a grid slot when it asks."
open "$BUNDLE"
echo
