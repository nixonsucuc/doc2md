#!/usr/bin/env python3
"""
Generate assets/icon.png, the source image for every doc2md front-end.

    python3 assets/make_icon.py [output.png]

A document carrying a prompt caret: the file has become model input. That is the
whole idea of the tool — doc2md does not produce documents for people to read, it
produces context for a language model to read.

Drawn at 4x and downsampled, because the size that actually matters is the ~22px
the Finder toolbar renders it at, and fine detail dies there. Two elements only,
for the same reason.
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw

SIZE = 1024          # macOS wants 512@2x; everything smaller is derived by sips
SUPERSAMPLE = 4
W = SIZE * SUPERSAMPLE

INK = (28, 32, 40, 255)      # charcoal, neutral against light and dark menu bars
PAPER = (255, 255, 255, 255)
TEAL = (45, 178, 168, 255)


def build() -> Image.Image:
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square platter, macOS corner radius.
    d.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.225), fill=INK)

    # The page, with a folded corner so it reads as a document at a glance.
    left, right = int(W * 0.20), int(W * 0.80)
    top, bottom = int(W * 0.17), int(W * 0.83)
    fold = int(W * 0.16)
    d.polygon(
        [(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)],
        fill=PAPER,
    )
    d.polygon([(right - fold, top), (right, top + fold), (right - fold, top + fold)], fill=TEAL)

    # The prompt: chevron plus caret. Dark on white keeps it readable when the
    # whole icon is 22 pixels across.
    stroke = int(W * 0.062)
    cx, cy = int(W * 0.36), int(W * 0.50)
    d.line(
        [(cx, cy - int(W * 0.10)), (cx + int(W * 0.11), cy), (cx, cy + int(W * 0.10))],
        fill=INK, width=stroke, joint="curve",
    )
    caret_y = cy + int(W * 0.065)
    d.rounded_rectangle(
        [int(W * 0.53), caret_y, int(W * 0.53) + int(W * 0.16), caret_y + stroke],
        radius=stroke // 2, fill=TEAL,
    )

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "icon.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    build().save(out)
    print(f"wrote {out} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
