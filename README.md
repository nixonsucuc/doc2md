# doc2md

Converts documents into LLM-ready Markdown. Text is extracted locally; a vision
model is called only for diagrams that OCR cannot represent.

doc2md is mostly orchestration. The hard parts — parsing PDFs, recognising
characters, describing images — are other people's work, and it is worth knowing
whose before relying on it: see [Built on](#built-on).

## Setup

```bash
pip3 install -e .
./ocr/build.sh          # optional: Apple Vision OCR, ~2x faster than Tesseract
brew install tesseract  # only needed if you skip the step above
```

That installs the dependencies and puts a `doc2md` command on your PATH.

With `./ocr/build.sh`, OCR goes through Apple Vision — no Homebrew package and no
language files, since language support ships with macOS. Measurements and the
engine setting are in [ocr/README.md](ocr/README.md).

Using Tesseract instead, Spanish OCR needs `spa.traineddata` in
`$(brew --prefix)/share/tessdata/`. Do **not** install `tesseract-lang` — it adds
~1.3 GB of unused languages.

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

```bash
./droplet/build.sh     # ~/Applications/doc2md.app  — drag documents onto it
./settings/build.sh    # ~/Applications/doc2md Settings.app
```

Put the droplet in the Dock, or ⌘-drag it onto the Finder toolbar where it
becomes a drop target on every window. Output goes to `~/Downloads/doc2md`
exactly as it does from the command line. Double-clicking it opens Settings.

Both are built from tools that ship with macOS — `osacompile`, `swiftc`, `sips`
— so there is nothing to install and nothing to buy. See
[droplet/README.md](droplet/README.md) and [settings/README.md](settings/README.md).

### Dropzone

[`dropzone/`](dropzone/README.md) holds a full Dropzone 4 action, kept working
and tested. It is the nicest of the front-ends to use if you already live in
Dropzone — one grid slot, modifier keys for local-only and reveal-in-Finder —
but custom actions are a **Dropzone Pro** feature, so it needs a Pro licence to
install. The droplet is the free equivalent and loses nothing but the grid.

## Settings

`./settings/build.sh` builds a small window over the two files doc2md reads:

| File | Holds |
|---|---|
| `~/.config/doc2md/config.json` | output folder, model, caps, daily budget, OCR languages |
| `~/.config/doc2md/env` | `GEMINI_API_KEY`, kept apart from preferences and `chmod 600` |
| `~/.config/doc2md/usage.json` | today's token spend, reset at midnight |

Editing those files by hand and using the window are the same operation — the
window owns no state of its own. Precedence is **CLI flag > config file >
built-in default**, and a corrupt config costs you a preference, never a
conversion.

The classification thresholds are deliberately *not* configurable. They were
calibrated against a sample corpus (see [MIGRATION.md](MIGRATION.md)) and a
slider that silently degrades classification is worse than no slider.

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

## Page layout

OCR returns one line per visual line of text, and Markdown joins consecutive
lines into a single paragraph — so writing that out unchanged turns a scanned
page into one run-on block with no paragraphs, headings or lists. doc2md instead
asks the OCR engine for each line's position and rebuilds the structure from it.

| Signal | What it recovers |
|---|---|
| Vertical gap against the page's median line pitch | Paragraph and section breaks |
| A line starting further right than the one above | Paragraph breaks in typeset books, which indent rather than add leading |
| Line height, plus isolation and length | Headings |
| A leading `1.` / `•` / `-`, plus the hanging indent below it | Lists, with continuation lines kept in their item |
| A trailing hyphen | Words split across a line break, rejoined |

Two findings shaped this and are worth knowing before tuning it:

**Reading order is never re-sorted.** Vision emits observations in reading order
and is already column-aware — it finishes the left column before starting the
right. Sorting by vertical position *destroys* that ordering rather than
establishing it, interleaving the two columns line by line.

**Box height is a poor proxy for font size.** It spans ascenders to descenders,
so it varies with which letters happen to be on the line. Measured across one
page of body text it ranged 0.89–1.22× the median, while a real all-caps heading
measured 1.10× — indistinguishable. Size alone therefore never promotes a line:
it must also be short, isolated, unpunctuated and narrower than the text measure.

Hyphen rejoining drops the hyphen when the continuation is lower-case
(`bet-` + `ter` → `better`) and keeps it when capitalised, where it is far more
often a real compound (`Franco-` + `American`).

Verse is reflowed into paragraphs like anything else. Telling a poem from a
wrapped paragraph needs a reliable right margin, and OCR'd scans do not have one.

## Running headers and footers

Repeated headers and footers are stripped, and their page numbers kept as
`<!-- page 48 -->` markers.

Repetition across pages is the only evidence used. Position alone is unsafe, and
measurably so: on the sample corpus the bottom band of a page routinely holds
ordinary body text, and one page's top band held the section heading
`MAKE YOU LAUGH` in exactly the place a page number would sit. **A document of
fewer than three pages is therefore left alone entirely** — a single page cannot
corroborate anything, and leaving a running head in is far cheaper than deleting
a paragraph of the source.

The threshold is deliberately low. Running heads change per chapter: on a
28-page sample the book title appeared on 24 pages but the chapter author on only
8, so a majority rule would have caught the page numbers and missed every running
head. A width test guards the other side — furniture ran 0.20× the median line
width against body text at 1.00× — so a full-measure line of prose is never
stripped however its wording repeats.

Page numbers are validated against the fact that they ascend through a document:
anything off the longest non-decreasing run is dropped. OCR misreads folios often
enough to matter (one 13-page sample produced `1101`, `1115` and `C` among clean
numbers), and a wrong marker is worse than none when its whole purpose is to be a
citable anchor.

## Built on

Very little of what doc2md does is doc2md's own code. It decides *which* tool to
use, *when* a page needs escalating, and *whether* an API call is worth making —
the actual extraction, recognition and description belong to the projects below.

| | Doing what | Licence |
|---|---|---|
| [MarkItDown](https://github.com/microsoft/markitdown) (Microsoft) | DOCX, PPTX, EPUB, HTML, ZIP, `.msg`, and the PDF fallback | MIT |
| [pdf-inspector](https://github.com/firecrawl/pdf-inspector) (Firecrawl) | Layout-aware PDF extraction in Rust: per-page Markdown, multi-column reading order, table detection, CID fonts, and the encoding and OCR-routing signals | MIT |
| [Apple Vision](https://developer.apple.com/documentation/vision) | OCR, when `ocr/build.sh` has been run — the default on macOS | part of macOS |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) via [pytesseract](https://github.com/madmaze/pytesseract) | OCR fallback, and the only engine off macOS | Apache 2.0 |
| [PyMuPDF](https://pymupdf.readthedocs.io/) (Artifex) | Page rasterizing, image extraction, titles, link annotations | **AGPL-3.0 or commercial** |
| [Pillow](https://pillow.readthedocs.io/) | Every pixel measurement the classifier makes | MIT-CMU |
| [google-genai](https://github.com/googleapis/python-genai) | The vision calls | Apache 2.0 |
| [langdetect](https://github.com/Mimino666/langdetect) | Picking the OCR language for a second pass | MIT |

### What we use from pdf-inspector

PDFs are routed through `pdf-inspector`, which is layout-aware and reports
per-page state. Specifically:

- `detect_pdf()` as a pre-flight — ~7 ms, because it samples content streams
  rather than extracting, and its `markdown` comes back empty by design. One call
  gives the document type, the OCR forecast *and* the declared title, so it also
  replaced a separate PyMuPDF read for the title: same answer on every sample
  tested, one fewer parse, MIT rather than AGPL.
- `extract_pages_markdown()` for the text, per page, with its `needs_ocr` flag
  and the *reason* behind it. A page flagged `suspected_garbled_text` has a text
  layer that is broken rather than absent — a different problem, so it is
  reported differently and raises a warning naming the pages.
- Table and multi-column detection, currently surfaced in the log.

Headings need no help: pdf-inspector already infers H1–H4 from font-size ratios,
correctly declining marginal cases a naive threshold would promote.

That leaves `extract_text_with_positions()` (coordinates, font sizes,
bold/italic), `extract_text_in_regions()` and `extract_text()` unused.
`process_pdf()` and `classify_pdf()` are covered by `detect_pdf()` above —
`classify_pdf` is a cheaper subset, and `process_pdf` returns flat Markdown
rather than the per-page structure the pipeline needs.

Font-size-based heading inference was investigated and is **not** needed:
`extract_pages_markdown()` already emits `# PART I: NON-CONTRADICTION` for a bold
11pt line among 10pt body text. What the text-layer path was actually missing was
end-of-line hyphenation (`familiar- ity`) and running headers, both now fixed —
see [Page layout](#page-layout). One thing tested and ruled out: positional data
cannot un-scramble a vector infographic — sorting by coordinates still yields
`orga nizat ion!`, because the text is letter-spaced along a curve. That case
genuinely needs the vision model.

Two things come from PyMuPDF instead, which is already a dependency:

- **The document's own title**, used as the H1 when the text supplies no heading
  of its own. Titles that are really filenames (`draft.docx`) or authoring-tool
  defaults are rejected — a wrong title is worse than none, since it is the first
  line the model reads.
- **Hyperlinks**, which live in link *annotations* and are therefore invisible to
  every text extractor. A page reading "schedule a free consultation" otherwise
  loses the address entirely. The text under the link rectangle becomes the
  anchor when it reads as language, and a bare URL when it does not.

### A licence note worth reading

doc2md's own code is MIT. **PyMuPDF is not** — it is dual-licensed under AGPL-3.0
or a commercial licence from Artifex. The AGPL is strongly copyleft, so anyone
distributing a combined work that includes PyMuPDF (a bundled app, a hosted
service) needs either to honour the AGPL for the whole of it or to hold an
Artifex licence. Installing it yourself from source, as the setup instructions
do, is the ordinary case and is not affected.

This is a statement of what the licences say, not legal advice. If it matters to
you, [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0 /
BSD-3) covers rendering and basic text extraction and would remove the question
entirely — see below.

### Swapping pieces out

Every stage is a seam, and better tools keep appearing. Nothing here is a
commitment:

| Stage | Now | Worth trying |
|---|---|---|
| PDF text | pdf-inspector | [Docling](https://github.com/docling-project/docling), [Marker](https://github.com/datalab-to/marker), [unstructured](https://github.com/Unstructured-IO/unstructured) |
| Rendering | PyMuPDF | [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) — also settles the licence question above |
| OCR | Apple Vision, falling back to Tesseract | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), [Surya](https://github.com/datalab-to/surya) |
| Vision | Gemini | Any vision model — `VISION_MODEL` is one line, and `analyze_with_vision()` is the only place the SDK is touched |

The pipeline exists to make these choices independent of each other. If you
replace one, the guards, budgets and reporting around it still apply.

## Contributing

Issues and pull requests welcome. The tool is one module, `doc2md.py`, organised
as a six-step pipeline with `# ── Name ──` banner comments — navigate by those
rather than scrolling.

```bash
pip3 install -e .
python3 -m unittest discover -s tests -t .   # stdlib unittest, no pytest
```

Tests never make a network call: Gemini is always mocked, and tests needing the
`tesseract` binary skip themselves when it is absent. Please keep it that way.

Good places to start, roughly in order of how much someone would thank you:

- **A status bar drop target.** The one front-end deliberately not built here. A
  `NSStatusItem` that accepts dropped files would give the Dropzone experience
  with no Pro licence. Two known snags: notifications from an unsigned bundle may
  not register, and a 22px menu bar icon is a small drop target, which is exactly
  why Dropzone uses a fly-out grid.
- **The Dropzone action**, in `dropzone/`. Working but dormant for want of a Pro
  licence, so it gets less real-world use than the droplet and is the most likely
  place for bit-rot. Anyone with Pro who runs it is doing the project a favour.
- **Linux and Windows.** Nothing in `doc2md.py` is macOS-specific, but every
  front-end is, and paths like `/opt/homebrew/bin` are assumed in the GUI
  wrappers.
- **Swap a stage.** The table above lists candidates. Replacing PyMuPDF with
  pypdfium2 would be a self-contained change behind an existing seam, and would
  settle the AGPL question.
- **Heading structure for scanned pages.** These come out of OCR flat. Note that
  font-size inference has been tried and rejected — see MIGRATION.md §11 for the
  measurements, including why bounding-box height and width-per-character both
  fail. Per-character cap height via Vision is the untried approach that would
  work.
- **Classifier calibration.** The thresholds come from a small corpus of mostly
  English and Spanish documents. Handwriting, dense scientific figures and
  non-Latin scripts are unexplored. Measurements beat opinions here — see
  MIGRATION.md for how the current numbers were arrived at.

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
