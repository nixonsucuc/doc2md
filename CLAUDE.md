# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, so `doc2md` on PATH tracks the source)
pip3 install -e .

# Run all tests (stdlib unittest, not pytest — keep it that way, no new dep)
python3 -m unittest discover -s tests -t .

# Run a single test file / case / method
python3 -m unittest tests.test_doc2md
python3 -m unittest tests.test_doc2md.SomeTestCase
python3 -m unittest tests.test_doc2md.SomeTestCase.test_something

# Run the tool
doc2md report.pdf
doc2md report.pdf --no-vision          # local-only, no network calls
doc2md report.pdf --verbose            # per-image classification decisions

# Droplet (drag-and-drop .app) — rebuild after editing droplet/*.sh or .applescript
./droplet/build.sh
./droplet/convert.sh report.pdf        # convert.sh is also usable standalone

# Dropzone action (requires Dropzone 4 Pro — see below)
./dropzone/install.sh
./dropzone/install.sh --check          # run the doc2md/tesseract/API-key checks only
```

Tests always mock Gemini (no network calls); tests needing the `tesseract`
binary skip themselves when it's absent.

## Architecture

Everything lives in one file, `doc2md.py`, structured as a 6-step pipeline
driven by `convert_document()` and dispatched by `main()`. Sections are marked
with `# ── Name ──` banner comments — use those to navigate rather than
scrolling. The steps, in order:

1. **Route & extract text** (`route_document`) — dispatches by type: images get
   a bare stub (OCR/vision fill it in later), `.eml` gets its own parser,
   PDFs go through `pdf-inspector` (layout-aware, reports per-page
   text/no-text state) with `MarkItDown` as the fallback, everything else goes
   straight to `MarkItDown`. A missing/failing native wheel degrades to
   MarkItDown rather than breaking the run.
2. **Extract embedded images** (`extract_images` + format-specific
   `extract_images_pdf/_zip/_eml`) — plus, for PDFs, `render_pdf_pages`
   rasterizes only the pages `pdf-inspector` flagged as having no text layer
   (scanned pages), and separately `find_diagram_pages` detects pages that
   *have* a text layer but are actually vector diagrams (heuristics: many
   drawing ops, short average line length, few/no embedded images — see the
   `PAGE_DIAGRAM_*` constants). Diagram pages are rendered and marked
   `semantic` with `replaces_page_text=True` so their scrambled coordinate-order
   text is replaced rather than duplicated.
3. **Classify images** (`classify_image`) — buckets each image into `skip`
   (logos/rules/icons), `photo`, `text`, or `semantic` (diagrams/tables/concept
   maps), using color count, edge density, and aspect-ratio heuristics tuned by
   the constants near the top of the file (`MAX_COLORS_DIAGRAM`,
   `PHOTO_STDDEV_THRESHOLD`, etc.). Images pre-classified during rendering
   (step 2) are left alone.
4. **OCR** (`run_ocr_batch`, `ocr_image`) — runs `text`-classified images
   through tesseract (`eng+spa`) in a thread pool. `should_escalate_to_vision`
   then reclassifies an image as `semantic` if its OCR came back as scattered
   fragments rather than prose (`ocr_looks_like_prose`) — a free decision at
   this point since OCR already ran.
5. **Vision analysis** (`analyze_with_vision`, `plan_vision_budget`) — sends
   `semantic` images to Gemini, gated by three budgets tracked in
   `~/.config/doc2md/usage.json`: a per-run confirmation threshold
   (`VISION_WARN_THRESHOLD`, bypassed with `--vision-ok`), a hard per-document
   cap (`VISION_HARD_CAP`), and a daily token budget (`VISION_DAILY_BUDGET`).
   Cost is estimated and printed *before* spending, and reported again after.
   `--no-vision` skips this step entirely and every semantic image is recorded
   as a warning instead of silently dropped.
6. **Assemble Markdown** (`assemble_by_page` when the source has per-page
   structure, else `merge_images_into_markdown`) — interleaves image
   references/OCR text/vision descriptions back into the base Markdown, then
   `normalize_pdf_tables` cleans up table formatting.

Output location is decided by `resolve_output_paths` / `claim_output_folder`:
by default each document gets its own folder under `~/Downloads/doc2md/`,
named after the file stem; a same-named document of a different type (e.g.
`report.docx` vs `report.pdf`) gets a `-ext` suffixed folder instead of
clobbering the first, tracked via a `.doc2md-source` marker file so
re-converting the *same* file keeps overwriting in place rather than
accumulating numbered folders.

`ProcessingReport` (in the data-classes section near the top) accumulates
counters and warnings through all 6 steps and is both written to
`report.txt` and printed to stdout at the end of `main()`.

### Front-ends

`droplet/` and `dropzone/` are thin GUI wrappers that shell out to the
installed `doc2md` binary rather than importing it — both run under a
Python that knows nothing about doc2md's own interpreter/deps, so they
locate the binary by checking pip/pipx install paths (`which` finds nothing
in a GUI process with no shell PATH) and put `/opt/homebrew/bin` back on
PATH so pytesseract can find tesseract. Both read the API key from
`~/.config/doc2md/env` (GUI apps don't inherit `.zshrc`) and infer success
from doc2md's `Saved:` line rather than its exit status, since doc2md exits
0 even on files it couldn't read. Dropzone requires Dropzone 4 Pro
(custom actions are a Pro-only feature); the droplet is the free
equivalent and is what to point users at if they don't have Pro.

### Key constants worth knowing before tuning behavior

All near the top of `doc2md.py` under `# ── Configuration ──`:
`VISION_MODEL` (single line to change model), `PDF_OCR_RENDER_DPI`,
`VISION_TOKENS_PER_IMAGE`/`VISION_DAILY_BUDGET`/`VISION_HARD_CAP`/
`VISION_WARN_THRESHOLD`, and the classification thresholds
(`MAX_COLORS_DIAGRAM`, `EDGE_DENSITY_THRESHOLD`, `PHOTO_*`,
`PAGE_DIAGRAM_*`). Design rationale and measurements behind these values are
in [MIGRATION.md](MIGRATION.md) — check it before changing a threshold.
