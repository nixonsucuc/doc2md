# doc2md

Converts documents into LLM-ready Markdown. Text is extracted locally; a vision
model is called only for diagrams that OCR cannot represent.

## Setup

```bash
brew install tesseract
pip3 install -e .
```

That installs the dependencies and puts a `doc2md` command on your PATH.

Spanish OCR needs `spa.traineddata` in `$(brew --prefix)/share/tessdata/`.
Do **not** install `tesseract-lang` — it adds ~1.3 GB of unused languages.

For diagram descriptions, set a key. Without it, everything else still works.

```bash
export GEMINI_API_KEY=...
```

## Use

```bash
doc2md report.pdf
```

Output lands in `~/Downloads/doc2md/report/` as `report.md`, plus `images/` and
`report.txt` (a processing summary).

| Flag | Effect |
|---|---|
| `--no-vision` | Local only. No API calls, nothing leaves the machine. |
| `--output PATH` | Write somewhere else. A directory, or an `.md` file. |
| `--keep-page-scans` | Keep rasterized PDF pages (deleted after OCR by default). |
| `--verbose` | Show per-image classification decisions. |
| `--vision-ok` | Approve vision work above the 20-image confirmation threshold. |
| `--max-vision N` | Lower the per-document cap for this run (hard limit 50). |

## Drag and drop

Build a droplet and drag documents onto it. Output goes to `~/Downloads/doc2md`
exactly as it does from the command line.

```bash
./droplet/build.sh
```

That installs `~/Applications/doc2md.app` — put it in the Dock or the Finder
toolbar. Details in [droplet/README.md](droplet/README.md).

There is also a [Dropzone 4 action](dropzone/README.md) in `dropzone/`, for the
Dropzone grid. Note that custom actions are a **Dropzone Pro** feature; without
Pro it will not install, and the droplet above is the free equivalent.

## Formats

PDF, DOCX, PPTX, EPUB, HTML, CSV, TXT, JSON, `.ipynb`, `.eml`, `.msg`, ZIP, and
images (`.png .jpg .jpeg .webp .bmp .tiff .gif`).

## What it does with images

| | |
|---|---|
| Logos, rules, icons | Dropped |
| Photographs | Kept as a link, no text extraction |
| Text and scans | OCR |
| Diagrams, tables, concept maps | OCR first; sent to the vision model only if OCR comes back as scattered fragments |

Scanned PDFs are detected automatically — only pages with no text layer are
rasterized and OCR'd. Roughly **6 minutes and a few hundred KB for a 300-page
book**, since page renders are deleted once their text is read.

## Vision cost

Measured on `gemini-3.6-flash`, one image costs **~2,680 tokens** — 1,155 input
(prompt + image), 1,248 thinking, 277 output. Thinking is the largest component
and is invisible unless you read `usage_metadata`.

Image resolution is free: 827px, 1240px and 1653px renders of the same page all
cost 1,155 input tokens, because Gemini normalises before tiling. Only the
*number* of images matters, which is why every guard counts images, not pixels.

Against Google AI Studio's free 250k/day that is ~93 images. Three guards:

| Limit | Value | Behaviour |
|---|---|---|
| Confirmation threshold | 20 images | Held, not lost. Document still converts; re-run with `--vision-ok`. |
| Hard cap | 50 images | Never exceeded by one document, even with `--vision-ok`. |
| Daily budget | 250,000 tokens | Running total in `~/.config/doc2md/usage.json`, resets at midnight. |

Every run prints what it is about to spend before spending it, and what it spent
afterwards.

## Diagram pages

A page drawn in vectors — an infographic, a roadmap — has a real text layer, so
it is never rasterized, and holds no embedded image, so nothing reaches the
classifier. Its text used to come out in coordinate order, scrambled across the
page. Such pages are now detected (many vector drawings, no embedded images,
very short average line length), rendered, and sent to vision; a successful
description replaces the scrambled text rather than sitting beside it.

## Notes

- Use `--no-vision` for anything private. Otherwise diagrams are uploaded to the
  Gemini API.
- Two documents sharing a name (`report.pdf`, `report.docx`) get separate output
  folders — the second becomes `report-docx/`. Re-converting the same document
  keeps overwriting its own folder, so nothing accumulates. Ownership is recorded
  in a `.doc2md-source` file inside each folder.
- GUI front-ends read `GEMINI_API_KEY` from `~/.config/doc2md/env`, since apps
  launched from the Finder never see your shell environment.
- Tests: `python3 -m unittest discover -s tests -t .`
- Design decisions and measurements: [MIGRATION.md](MIGRATION.md)
