# Migration notes

## 1. Output location and layout

Every conversion now writes into its own folder, named after the source
document, under `~/Downloads/doc2md/`:

```
~/Downloads/doc2md/Annual Report/
    Annual Report.md
    images/
    report.txt
```

Previously output landed in a `doc2md/` folder beside the input document, with
the images folder and report prefixed by the document name
(`Annual Report_images/`, `Annual Report_report.txt`).

- The default destination is the constant `DEFAULT_OUTPUT_DIR` at the top of
  `doc2md.py`. Change that one line to move it.
- `--output DIR` uses `DIR` with the same internal layout.
- `--output FILE.md` writes `FILE.md` and keeps the old prefixed sibling names,
  so several documents can still share one directory.
- Because each document gets its own folder, two inputs with the same filename
  from different source directories no longer overwrite each other's images and
  report — they did under the previous flat layout.

Path resolution now lives in `resolve_output_paths()` instead of being inlined
in `convert_document()`.

## 2. Slimmer dependencies

`markitdown[all]` → `markitdown[docx,pptx,pdf,outlook]`.

The `all` extra pulls converters this tool never invokes. Dropped:

| Extra | Brings | Why it went |
|---|---|---|
| `xlsx`, `xls` | pandas, openpyxl, xlrd | ~67 MB; spreadsheet input not needed. Plain `.csv` still works — that converter is markitdown core. |
| `audio-transcription` | speechrecognition, pydub | ~45 MB; audio is not a document. |
| `youtube-transcription` | youtube-transcript-api | ~9 MB; not a document. |
| `az-doc-intel`, `az-content-understanding` | azure-*, msal, cryptography | ~18 MB, and both are cloud services, which contradicts the local-first design principle. |

`beautifulsoup4` was also removed from the explicit dependency list — nothing in
`doc2md.py` imports it, and markitdown depends on it directly anyway.

**Still supported:** PDF, DOCX, PPTX, EPUB, HTML, CSV, `.msg`, plain text,
Jupyter notebooks, ZIP. EPUB and HTML need no extra — those converters are
markitdown core.

To apply in an existing environment:

```bash
pip uninstall -y pandas openpyxl xlrd speechrecognition pydub youtube-transcript-api azure-ai-documentintelligence azure-ai-contentunderstanding azure-identity
```

Then reinstall from the updated `requirements.txt`.

## 3. Tesseract

`OCR_DEFAULT_LANGS` remains `eng+spa`. `LANG_MAP` was trimmed from 14 languages
to just those two; `ocr_image()` already verified availability before switching
language, so the extra entries were inert.

Do not install `tesseract-lang` — it adds roughly 1.3 GB of traineddata for
languages this tool will never select. A plain `brew install tesseract` gives
`eng`, and `spa.traineddata` (13.6 MB) can be dropped into
`$(brew --prefix)/share/tessdata/` on its own.

## 4. Image files as direct input

`.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tiff`, `.tif` and `.gif` can now be
converted directly — screenshots, scans, exported diagrams.

Previously these produced a **zero-byte Markdown file**. MarkItDown's image
converter only emits EXIF metadata plus an optional LLM caption; doc2md passes
no LLM client, and without `exiftool` installed there was no metadata either, so
the result was empty. doc2md's own OCR and vision stages never ran because
`extract_images()` had no branch for image inputs.

Image inputs now bypass MarkItDown entirely (`extract_base_markdown()`) and feed
straight into the existing classify → OCR → vision pipeline. The extension list
is wider than MarkItDown's `.jpg/.jpeg/.png` because Pillow reads all of them.

Two behavioural details:

- The original file is **copied**, never moved, into the output `images/` folder,
  so the Markdown keeps the same relative-link shape as every other format.
- A directly supplied image is never discarded as an icon. If `classify_image()`
  returns `skip`, `convert_document()` promotes it to `text`, on the grounds that
  the user asked for this file by name.

## 5. Running the tests

```bash
python -m unittest discover -s tests -t . -v
```

105 tests, stdlib `unittest` only — no pytest, so the suite costs no disk space.
Gemini is always mocked; no test makes a network call. The OCR tests skip
themselves if the tesseract binary is missing. Fixture documents are generated
at test time by `tests/fixtures.py` rather than committed as binaries.

One test deliberately pins behaviour that is **wrong but currently relied upon**,
so that changing it is a conscious decision rather than an accident:

### Unknown extensions

MarkItDown treats an unrecognised extension as plain text and passes the raw
bytes through, so a binary file with an odd suffix lands verbatim in the
Markdown rather than raising. `test_unknown_extension_falls_through_to_plain_text`
records this. An extension allow-list in `convert_document()` would be the fix.

## 6. pdf-inspector as the PDF backend

PDFs now go through `pdf-inspector` instead of MarkItDown. `route_document()`
dispatches:

```
image  → no text layer; OCR and vision supply the content
.eml   → stdlib email parser
PDF    → pdf-inspector  (falls back to MarkItDown on failure)
other  → MarkItDown
```

**MarkItDown remains the fallback for PDFs**, which is why `markitdown[pdf]` is
still in the dependency list. pdf-inspector is a pre-1.0 native extension, and
the fallback also covers files it cannot parse at runtime. Costs 18 MB.

### What it bought, measured on real documents

| | MarkItDown | pdf-inspector |
|---|---|---|
| Prose page | one blank line after every PDF line break | paragraphs correctly reflowed |
| Infographic page | jumbled fragments, one bogus table | real heading hierarchy (`#`, `##`, `#####`) |
| Page structure | one flat string, no page boundaries | per-page Markdown |

Per-page output is what makes `assemble_by_page()` possible: images now sit with
the text of the page they came from. Previously **every** PDF image landed in a
trailing "Extracted Images" appendix, because MarkItDown's PDF output contains
no image references for the old matching logic to find.

### The table workaround

pdf-inspector's column detector misfires on single-column prose and wraps whole
paragraphs in empty-celled table syntax:

```
||Regardless of your chosen path, and regardless of whether you seek out||
|---|---|---|
```

`normalize_pdf_tables()` unwraps rows with one filled cell back into paragraphs
while keeping genuine multi-cell rows, and re-emits a correctly sized separator
beneath the first real data row. Without it the migration is a **regression** on
ordinary prose. This is a workaround for an upstream bug, and worth removing if
pdf-inspector fixes it.

### Page-level OCR

pdf-inspector reports which pages have no extractable text but cannot render
them — it does no image work at all. `render_pdf_pages()` rasterizes exactly
those pages with PyMuPDF at `PDF_OCR_RENDER_DPI` (200) and feeds them to the
existing OCR stage.

- **Pages that already yielded text are never rendered or OCR'd.**
- Rendered pages arrive pre-classified as `text` and bypass `classify_image()`.
  Re-deriving would risk a photographed page being labelled `photo` and dropped.
- Page indices are normalised to 0-based on ingest. pdf-inspector is
  inconsistent about this — `PageMarkdown.page` is 0-based while
  `pages_with_columns` on the same result object is 1-based.

## 7. Email (.eml) support

Handled by the standard library `email` module — no new dependency. Emits a
heading, a `From`/`To`/`Cc`/`Date` block, the body (preferring `text/plain`,
converting `text/html` via markdownify), and a list of attachment filenames.

MarkItDown covers Outlook's `.msg`; `.eml` is what every other mail client
exports.

**Image attachments and inline images go through the full image pipeline** —
classify → OCR → vision — because a screenshot pasted into a message is usually
where its real content lives. Non-image attachments are named in the Markdown
but not opened.

## 8. Classifier: pixel stats decide *what to skip*, OCR decides *what to describe*

The original design assumed diagrams are line art — few colours, many edges. The
sample corpus says otherwise. Every real diagram in it is a **screenshot**:
concept maps, a schedule table, a programme card, a hand-drawn cycle on textured
paper. They carry 433–2599 distinct colours. Colour count cannot separate them
from a scanned page, and no threshold on colours, grayscale spread, or edge
density does either:

| Feature | Diagrams | Scanned pages | Separable? |
|---|---|---|---|
| Colours | 433–2599 | 958–1301 | overlaps |
| Grayscale stddev | 25–67 | 36–48 | overlaps |
| Edge density | 0.017–0.041 | 0.044–0.084 | yes, narrowly |

So the pipeline no longer tries to identify a diagram from pixels. It became:

1. **Pixel stats** decide only what is safe to discard (logos, rules, flat
   fills) and what is a photograph. Everything else goes to OCR.
2. **OCR runs** — local, free, deterministic.
3. **`should_escalate_to_vision()`** reads the OCR *result*. Continuous prose
   means OCR did the job and no API call is made. Text scattered across four or
   more short fragments, in an image whose edge density is too low to be a page
   of dense type, means a diagram — escalate.

This matches the project's stated order of preference: deterministic first, AI
only where it adds semantic value, no unnecessary API calls. It also fixes the
original complaint — the `semantic` branch previously almost never fired, so
Gemini was rarely invoked at all.

Two guards keep the cost down:

- Pages rasterized out of a PDF are **never** escalated. A 200-page scanned book
  would otherwise become 200 API calls.
- Fewer than `OCR_MIN_FRAGMENTS` lines means there is no structure to recover; a
  screenshot holding one clean line of text stays with OCR.

Result on the corpus — **14 of 14 correct**, where 9 of 14 were wrong before:

| Sample | Before | After | Decided by |
|---|---|---|---|
| Hero's-journey cycle | `text` | `semantic` | OCR escalation |
| Concept map ×2 | `text` | `semantic` | OCR escalation |
| Schedule table | `text` | `semantic` | OCR escalation |
| Programme card | `text` | `semantic` | OCR escalation |
| Needs sheet | `text` | `semantic` | OCR escalation |
| Scanned page, receipts, newspaper | `text` | `text` | pixels |
| 5 photographs | `text` (3 escalated to the API) | `photo` | pixels |
| Flat logo, two-tone logo | `text` | `skip` | pixels |

### Colour counting

One further fix, independent of the above: colours were counted on a LANCZOS
downsample, which interpolates between neighbouring pixels and invents colours
that were never in the source. A real two-tone logo measured **225** distinct
colours that way; with NEAREST it measures **2**. Every threshold is now
expressed on the NEAREST scale.

### Detecting photographs

Photographs needed a second, independent rule. Edge density is
resolution-dependent — a 4032×3024 phone photo is smooth at the pixel level and
scores *lower* than a small dense scan — so the original rule missed real
photos entirely: 4 of 5 were being routed to OCR, and three were then escalated
to the vision model, which would have **uploaded personal photographs to the
API**.

What holds regardless of resolution is that documents, screenshots and diagrams
sit on a light background, while a photograph fills the frame with continuous
tone. Measured: photographs 0.0–6.2% near-white pixels, diagrams and scans
9.8–84.5%. An edge ceiling accompanies it so that a dark, dense newspaper scan —
which also has no light background — stays on the OCR path where it belongs.

The related fix: escalation no longer fires when OCR found *no* text. That case
is far more often a photograph than an unlabelled diagram, and escalating on no
evidence is how private images reach the API.

### Low-resolution OCR

Tesseract does not degrade below roughly 150 dpi — it returns nothing at all. A
604×401 newspaper clipping produced **0 characters**; at 2× it produced 1120.
`upscale_for_ocr()` now enlarges images whose short side is under
`OCR_MIN_SHORT_SIDE`, capped at `OCR_MAX_UPSCALE`. Larger images are untouched.

### Non-white paper

A scan is not necessarily white. A grey photocopy of a book page has **0.0%**
near-white pixels, so the near-white test alone calls it a photograph and its
text is lost outright. What it does have is a dominant tone: 80% of the page
sits within a narrow band around one grey. Photographs have neither — measured
modal share 3–34% for photographs against 77–81% for documents. Both tests must
now fail before an image is called a photograph.

### Rendered pages supersede their embedded images

On a scanned page the "embedded image" *is* the page. Extracting it and then
rasterizing the same page stored the content twice, OCR'd it twice, and would
have spent a second API call describing the same thing.
`discard_images_on_rendered_pages()` drops the embedded copies — and deletes the
extracted files — for any page that is about to be rendered.

### Confidence

Fifteen real samples with known ground truth: 5 photographs, 5 diagrams, 3
scanned documents (including a grey book scan and a newspaper clipping), 2
logos. All classify safely; the grey book page routes to the vision model rather
than plain OCR, which preserves its content either way.

Escalation requires two independent signals to agree, so one misread cannot
trigger an API call, and a failed vision call falls back to the OCR text rather
than emitting nothing.

## 9. Scanned-document throughput

Benchmarked on a 20-page scanned book with four different scan qualities
(clean 300 dpi, grey 200 dpi, blurry low-resolution, noisy grey). Every page
required OCR.

| | Before | After |
|---|---|---|
| 20 pages | 72 s | **24 s** |
| Disk | 11 MB/page | **0.43 MB/page** |
| A 300-page book | ~40 min, ~3.3 GB | **~6 min, ~130 MB** |

Three changes got there.

**Render size cap.** One sample PDF declares a 20×33 inch page box, which at
200 dpi rasterizes to 27 megapixels — 11 MB and 4.2 s of OCR for a scan whose
real detail is far lower. `PDF_OCR_MAX_LONG_SIDE` caps the long edge and
recovered 1411 characters against 1415 uncapped, so the detail was genuinely
not there. Letter and A4 pages sit under the cap at 200 dpi and are untouched.

**JPEG instead of PNG for renders.** Page renders are photographic scans by
definition — that is *why* they need OCR — so PNG was the wrong container.
Same page: 0.37 MB against 5.9 MB.

**Parallel OCR.** Profiling showed rasterizing all 20 pages took under a second
while OCR took 64 — **99% of the work**. pytesseract shells out to the tesseract
binary and blocks, so `run_ocr_batch()` overlaps them with a thread pool: 12
pages went from 37.1 s to 8.7 s on 8 cores. `OCR_MAX_WORKERS` caps concurrency.

Also fixed in passing: `ocr_image()` called `pytesseract.get_languages()` on
every image, which spawns a subprocess each time. Now cached.

### On scan quality

OCR yield held steady across all four qualities — 4007 to 4246 characters per
page — so the pipeline is not fragile to how good the scan is. The upscaling
added for low-resolution images does **not** apply to scanned PDFs: pages are
rasterized at 200 dpi, well above `OCR_MIN_SHORT_SIDE`. It only affects small
standalone images and small embedded images.

## 10. Small cleanups

- Removed `PHOTO_ENTROPY_THRESHOLD` — nothing read it. The photo branch of
  `classify_image()` used bare literals `60` and `500`; those are now
  `PHOTO_STDDEV_THRESHOLD` and `PHOTO_MIN_COLORS`. Behaviour is unchanged. The
  old name was also misleading: the code measures standard deviation, not
  entropy.
- Removed `ImageInfo.original_ref` — never assigned or read.
- Removed the `verbose` parameter from `convert_document()` — it was accepted
  and ignored; logging is configured in `main()`. **Breaking for programmatic
  callers** passing `verbose=`; the `--verbose` CLI flag is unaffected.
- Removed a duplicated empty-images-directory cleanup and a `report_path` that
  was computed during path resolution and then immediately recomputed.

## 11. Heading inference for OCR'd pages — font size rejected, structure shipped

Font-size heading inference kept coming up as an obvious win. It is not, and
this records why so nobody repeats the experiment.

Headings on scanned pages *are* now recovered, but not by measuring type size.
The two size proxies below both failed and are still rejected; what worked was
giving up on size and reading structure instead. That is §11.4 — read the
failures first, because they are why the shipped rule looks the way it does.

**It is already done where it can be done.** pdf-inspector infers H1–H4 from font
size ratios, and does it well. Given a synthetic page with 28/20/15/12 pt text
over 11 pt body, it produced `#`, `##`, `###` correctly *and* declined to promote
the 12 pt line — the marginal case a naive ratio threshold gets wrong. There is
nothing to add on the PDF text-layer path.

**The gap is scanned pages**, which come out of OCR completely flat. A real
sample — a scanned dance manual page — has an obvious visual hierarchy: a large
bold title, a centred sub-heading, column headers. All of it arrives as
undifferentiated lines.

### Why bounding-box height does not work

Apple Vision returns a bounding box per observation, so height looks like a
free font-size proxy. It is not. On the sample page, the largest heading —
`CHOREOGRAPHED FLOOR PROGRESSION` — ranked **23rd of 72 lines** by box height,
and the `Jackknife Contraction` sub-heading fell *below* the page median.

The box measures glyph extent, not type size. An all-caps heading has no
descenders and yields a short box; a body line containing parentheses and
`y`/`g`/`p` yields a tall one. The signal is close to inverted.

### Why width-per-character does not work either

Width divided by character count is a much better proxy — it put that same
heading at **rank 1 of 72**. With a baseline taken from prose lines (≥40
characters) and candidates limited to lines of ≥12 characters, it behaved well on
clean sources:

| Page | Promoted | Correct |
|---|---|---|
| Scanned manual page | `CHOREOGRAPHED FLOOR PROGRESSION` | yes, plus one borderline false positive |
| Synthetic report | all three headings | yes |
| Synthetic manual | two of three headings | yes |
| Product brochure | the headline and title | yes |
| Concept map | nothing — abstained | yes |

Two faults sank it.

**It collapses on degraded sources.** On a photographed 1998 newspaper clipping
it promoted **eleven** lines, most of them OCR noise: `nic ineinsidisoterskin`,
`ses con sekiu.nlemiana`. Character widths vary wildly when recognition is
uncertain, and degraded scans are precisely where OCR'd pages come from. A
heuristic that works on clean input and fails on hard input is backwards.

**Equal headings get unequal levels.** In the synthetic report, `Revenue and
Margin` measured 1.65× baseline and `Operating Costs` 1.55× — identical 16 pt
headings in the source. Any fixed band splits them into `##` and `###`, which
tells a reader the second is subordinate to the first. Inconsistent structure
misleads more than no structure does; flat text at least makes no false claims.

### What would still change the verdict on size

Per-character geometry rather than per-line averages — Vision can return a
bounding box for a character range, so cap height could be measured directly
instead of inferred from average advance width. That is immune to both faults:
it does not care about descenders, and it does not care how many characters a
line has. It costs an extra request per line, which is why it was not tried here.

Still untried, and still the only route to a *reliable* type-size signal.

### 11.4 What shipped instead: structure, not size

The failures above share a premise — that a heading is recognised by being
bigger. Drop it, and the problem becomes easy, because a heading is recognised
by *standing alone*. `reflow_layout()` promotes a line only when all of these
hold:

| Test | Why |
|---|---|
| Isolated above and below | A heading sits in its own whitespace; a short body line does not |
| Narrower than the text measure | Full-measure lines are prose, whatever their size |
| At most 8 words | Beyond that it is a sentence |
| No trailing `,;:.!?-` | Punctuation means it continues; this alone killed `## like saying "yes."` |
| All-caps **or** ≥1.3× median height | The two shapes a heading actually takes |

Size appears only in the last row, as one of two alternatives, and never
promotes a line on its own. The all-caps branch matters more: in scanned books
that is the common heading style, and it is the case pure size inference gets
*backwards* — an all-caps line has no descenders, so it measures **shorter**
than the body text around it.

The 1.3 threshold comes from the same failure mode §11.1 documents. Measured
across one page of body text, box height ranged 0.89–1.22× the median purely by
which letters fell on each line, while the real heading `HEAD` measured 1.10×.
1.3 clears that observed noise ceiling; anything lower fires on prose.

Verified on the corpus: `## HEAD` and `## SHOULDERS` recovered from a page where
both had previously been swallowed into a paragraph, with no false positives on
the same page. A first version without the punctuation and width gates emitted
`## like saying "yes."` and `## Up-Down` while still missing both real headings —
which is the measurement that produced the table above.

**Known miss:** title-case headings that are neither all-caps nor larger, such as
`Attitudes of the Head`. Nothing distinguishes them from a short body line
without the per-character cap height above. Left flat deliberately — a missing
heading costs less than a wrong one.
