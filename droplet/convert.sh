#!/bin/bash
# Converts one file with doc2md, for GUI front-ends that have no shell.
#
#   convert.sh FILE [--vision-ok]
#
# --vision-ok approves vision work above doc2md's confirmation threshold; the
# front-end asks the user first, then re-runs with it.
#
# Prints the path of the generated Markdown on success, followed by optional
# TOKENS:n and HELD:n lines.
# Exit 0 converted · 3 unsupported type · 4 conversion failed · 5 doc2md missing
#
# Everything here exists because apps launched from the Finder inherit none of a
# login shell: no PATH beyond the system default, and nothing from .zshrc.

set -uo pipefail

ENV_FILE="$HOME/.config/doc2md/env"

# doc2md itself falls back to a plain-text read for extensions it does not know,
# which puts binary junk in the Markdown. A drop target is the easy way to hit
# that, so unknown types are refused here instead.
SUPPORTED="pdf docx pptx epub html htm csv txt json ipynb eml msg zip xml md png jpg jpeg webp bmp tiff tif gif"

file="${1:-}"
vision_ok="${2:-}"
[ -n "$file" ] || { echo "usage: convert.sh FILE [--vision-ok]" >&2; exit 64; }
[ -f "$file" ] || { echo "Not a file: $(basename "$file")" >&2; exit 3; }

ext="${file##*.}"
ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
case " $SUPPORTED " in
  *" $ext "*) ;;
  *) echo "Unsupported file type: .$ext" >&2; exit 3 ;;
esac

# `which` is useless with no shell PATH, so check where pip and pipx actually put
# things, newest Python first.
DOC2MD=""
for candidate in \
  /Library/Frameworks/Python.framework/Versions/[0-9]*/bin/doc2md \
  "$HOME"/Library/Python/*/bin/doc2md \
  "$HOME/.local/bin/doc2md" \
  /opt/homebrew/bin/doc2md \
  /usr/local/bin/doc2md
do
  [ -x "$candidate" ] && DOC2MD="$candidate" && break
done
[ -n "$DOC2MD" ] || { echo "doc2md is not installed. Run: pip3 install -e ~/Developer/doc2md" >&2; exit 5; }

# Homebrew's bin is what pytesseract needs to find the tesseract binary; without
# it every OCR path fails.
export PATH="$(dirname "$DOC2MD"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# set -a exports everything the file defines, so a plain KEY=value line works
# without the user having to remember `export`.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE" 2>/dev/null || true
  set +a
fi

# Deliberately NOT passing --no-vision when the key is missing. That flag also
# disables the step that decides an image is a diagram at all, so with it the
# document converts silently and there is no way to tell the user what they lost.
# Without a key doc2md returns from the vision call before building a client, so
# nothing is uploaded either way — but the diagrams still get counted.

log="$(mktemp -t doc2md)"
trap 'rm -f "$log"' EXIT

# doc2md writes its step log to stderr and its report to stdout, and exits 0 even
# when it could not read the file — so success is decided by the "Saved:" line,
# not by the exit status.
# 2>&1 shares one file offset between both streams; redirecting them separately to
# the same file would let stdout overwrite stderr from offset zero.
if [ "$vision_ok" = "--vision-ok" ]; then
  "$DOC2MD" "$file" --vision-ok >"$log" 2>&1
else
  "$DOC2MD" "$file" >"$log" 2>&1
fi

saved="$(grep -m1 -E '^[[:space:]]*Saved:' "$log" | sed -E 's/^[[:space:]]*Saved:[[:space:]]*//')"

if [ -n "$saved" ] && [ -f "$saved" ]; then
  printf '%s\n' "$saved"

  # Diagrams whose meaning lives in their layout — concept maps, flowcharts —
  # come back from OCR as scattered fragments, so doc2md routes them to the
  # vision model. With no key they are dropped and the Markdown is quietly
  # thinner than the document. Report the count so the front-end can say why.
  if grep -q "GEMINI_API_KEY not set" "$log"; then
    undescribed="$(sed -nE 's/.*semantic image\(s\)\. Sending.*/x/p' "$log" | head -1)"
    n="$(sed -nE 's/.*Step 5\/6: ([0-9]+) semantic image.*/\1/p' "$log" | head -1)"
    if [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; then
      printf 'UNDESCRIBED:%s\n' "$n"
    fi
  fi

  # Vision was withheld pending approval: the document converted, the diagrams
  # are still describable on a second pass.
  held="$(sed -nE 's/.*Vision held for ([0-9]+) image.*/\1/p' "$log" | head -1)"
  [ -n "$held" ] && printf 'HELD:%s\n' "$held"

  spent="$(sed -nE 's/.*Vision spend: ([0-9,]+) tokens.*/\1/p' "$log" | tr -d ',' | head -1)"
  [ -n "$spent" ] && printf 'TOKENS:%s\n' "$spent"
  exit 0
fi

# Failed. The useful diagnostic is in the report's warning list, not the step log.
grep -E '^[[:space:]]*- ' "$log" | sed -E 's/^[[:space:]]*- //' | head -4 >&2 \
  || echo "doc2md produced no Markdown for this file." >&2
exit 4
