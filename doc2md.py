#!/usr/bin/env python3
"""
doc2md.py — Document-to-Markdown Conversion Workflow

Converts documents (PDF, DOCX, PPTX, HTML, EPUB, etc.) into clean,
LLM-friendly Markdown while extracting images and using a vision model
only for semantic visuals (diagrams, flowcharts, infographics).

Usage:
    python doc2md.py <input_file> [--output <path>] [--model <name>]
                                  [--no-vision] [--verbose]

Output defaults to ~/Downloads/doc2md/<document-name>/.

Requires:
    System:  tesseract with eng + spa data (brew install tesseract).
             Do NOT install tesseract-lang; it adds ~1.3 GB of unused languages.
    Python:  pip install "markitdown[docx,pptx,pdf,outlook]" pymupdf pytesseract
             Pillow google-genai langdetect
    Env:     GEMINI_API_KEY (only needed when vision analysis is enabled)
"""

import argparse
import json
import statistics
from datetime import date
import hashlib
import io
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageStat, ImageFilter
import pytesseract
from markitdown import MarkItDown

# Optional imports — gracefully degrade if missing
try:
    from langdetect import detect as langdetect_detect
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

try:
    import pdf_inspector
    HAS_PDF_INSPECTOR = True
except ImportError:
    HAS_PDF_INSPECTOR = False


# ── Configuration ─────────────────────────────────────────────────────────────
VISION_MODEL = "gemini-3.6-flash"  # ← Change this one line to upgrade

# All generated Markdown, image folders, and reports land here unless --output
# is given. A single shared folder, not one beside each input document.
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "doc2md"

# pdf-inspector's OCR reasons, in plain language. "no text layer" and "the text
# is there but broken" send you looking for very different problems.
OCR_REASON_TEXT = {
    "no_text_layer": "no extractable text",
    "suspected_garbled_text": "broken font encoding",
    "image_based": "page is an image",
    "scanned": "scanned page",
}

# Records which document an output folder belongs to, so that two files sharing a
# stem (report.pdf, report.docx) do not overwrite each other while re-converting
# the same file still overwrites in place. See claim_output_folder().
SOURCE_MARKER = ".doc2md-source"

# Tesseract default languages (fast first pass). Also fed to Apple Vision, which
# maps them to BCP-47 itself, so one setting covers both engines.
OCR_DEFAULT_LANGS = "eng+spa"

# "auto" prefers Apple Vision when its helper is built and falls back to
# Tesseract; "vision" and "tesseract" force one. See ocr/README.md.
OCR_ENGINE = "auto"

# Tesseract is tuned for roughly 300 dpi and degrades sharply below about 150 —
# not gracefully, but to nothing at all. A 604×401 newspaper clipping returned
# zero characters at native size and 1120 at 2×. Small images are therefore
# upscaled before OCR; the cap keeps the pixel cost bounded.
OCR_MIN_SHORT_SIDE = 1000
OCR_MAX_UPSCALE = 3

# OCR dominates the runtime of a scanned document — 99% of it, measured on a
# 20-page book, since rasterizing all 20 pages took under a second. pytesseract
# shells out to the tesseract binary and blocks, so threads parallelise it
# cleanly: 12 pages went from 37.1 s to 8.7 s on 8 cores.
OCR_MAX_WORKERS = min(8, (os.cpu_count() or 1))

# Resolution used to rasterize PDF pages that carry no extractable text, so the
# existing OCR stage can read them. 200 dpi is the usual sweet spot for
# tesseract: below ~150 accuracy drops sharply, above ~300 only costs time.
PDF_OCR_RENDER_DPI = 200

# Some PDFs define an oversized page box — one sample declares 20×33 inches — and
# at 200 dpi that renders to 27 megapixels: 11 MB per page and 4.2 s of OCR, for
# a scan whose real detail is far lower. Capping the long side costs nothing in
# accuracy (1411 characters recovered against 1415 uncapped) while cutting OCR
# time by more than half. Normal Letter and A4 pages fall under the cap at 200
# dpi and are unaffected.
PDF_OCR_MAX_LONG_SIDE = 2600

# Page renders are photographic scans by definition — that is why they need OCR —
# so JPEG suits them and PNG does not. Same page: 0.37 MB against 5.9 MB.
PDF_OCR_RENDER_QUALITY = 85

# ── Layout reconstruction ─────────────────────────────────────────────────────
# OCR hands back one line per visual line of text. Markdown joins consecutive
# lines into a single paragraph, so writing them out as-is turns a page into one
# run-on block with no headings, paragraphs or lists. These thresholds drive
# reflow_layout(), which rebuilds that structure from the line geometry.

# A vertical gap this many times the page's median line pitch ends a paragraph.
# Measured on the sample corpus: successive lines of one paragraph sit at
# 1.00–1.15× pitch, while a paragraph break runs 1.5× and a section break 3–4×.
# 1.4 sits in the empty space between the two populations.
LAYOUT_PARA_GAP = 1.4

# A line this many times the median line height counts as genuinely larger type.
# Vision's box height spans ascenders to descenders, so it is a poor proxy for
# font size: measured on one page of body text it ranged 0.89–1.22× the median
# purely by which letters happened to be on the line, while the real heading
# "HEAD" measured 1.10× — indistinguishable. The threshold therefore sits above
# that observed noise ceiling, and size alone never promotes a line; it must
# also be short, isolated and unpunctuated. All-caps headings, which are the
# common case in scanned books and are *shorter* than body text for want of
# descenders, are recognised structurally instead.
LAYOUT_HEADING_HEIGHT = 1.3
LAYOUT_HEADING_MAX_WORDS = 8
# Headings do not fill the measure. A long line at heading size is body text in a
# large font, which is a different thing, so width gates the decision too.
LAYOUT_HEADING_MAX_WIDTH = 0.8

# Vision emits observations in reading order, and that order is column-aware: on
# a two-column page it finishes the left column before starting the right. A
# backwards jump of at least this much therefore means a new column, not a new
# paragraph. Sorting by y would destroy this ordering rather than establish it.
LAYOUT_COLUMN_JUMP = 0.05

# A line starting this much further right than the one above it opens a new
# paragraph. Typeset books mark paragraphs by indenting the first line rather
# than by leading extra space, so without this a whole page of a novel comes back
# as one block: measured on a scanned page, body lines sat at x=0.066–0.069 and
# every paragraph opening at x=0.092–0.097, against 0.003 of noise within a
# column. Comparing against the previous line rather than a column edge keeps a
# block quote — indented as a whole — from breaking on every one of its lines.
LAYOUT_INDENT = 0.015

# ── Running headers and footers ───────────────────────────────────────────────
# The band at the top and bottom of a page in which furniture can live, as a
# fraction of page height.
FURNITURE_BAND = 0.08

# How many pages a line must appear on, in the same band, before it is treated as
# a running header or footer. Deliberately low: running heads change per chapter,
# so on a 28-page sample the book title appeared on 24 pages but the chapter
# author on only 8, and a majority threshold would have caught the page numbers
# while missing every running head.
FURNITURE_MIN_PAGES = 3

# Never strip on a document too short for repetition to mean anything. A single
# dropped page cannot corroborate anything, and positional guessing alone is
# unsafe: on the sample corpus the bottom band routinely holds body text, and a
# page-number-shaped band at the top held the real heading "MAKE YOU LAUGH".
FURNITURE_MIN_DOC_PAGES = 3

# Furniture does not fill the text measure. Measured across a 28-page scan, lines
# in the top and bottom bands ran 0.20× the page's median line width while body
# text ran 1.00×, and the tenth of band lines that were full width were body text
# caught by the band — exactly what must survive. An independent guard alongside
# repetition, so that a full-measure line of prose can never be stripped however
# its wording happens to repeat.
FURNITURE_MAX_WIDTH = 0.7

# ── Vision budget ─────────────────────────────────────────────────────────────
# Measured against gemini-3.6-flash on a rendered A4 page: 1155 input (prompt +
# image) + 1248 thinking + 277 output = 2680. Thinking is the largest single
# component and is invisible unless you read usage_metadata.
#
# Image resolution does NOT affect cost: 827px, 1240px and 1653px renders of the
# same page all cost 1155 input tokens, because Gemini normalises before tiling.
# Only the *number* of images matters, which is why every guard here counts
# images rather than pixels.
VISION_TOKENS_PER_IMAGE = 2680

# Google AI Studio free tier. Used only to express spending as a percentage.
VISION_DAILY_BUDGET = 250_000

# Never send more than this from a single document, no matter what: 50 images is
# ~134k tokens, about half a day's free quota, spent in one drag.
VISION_HARD_CAP = 50

# Above this, hold the vision work and let the caller decide. The document still
# converts; only the descriptions wait.
VISION_WARN_THRESHOLD = 20

USAGE_FILE = Path.home() / ".config" / "doc2md" / "usage.json"

# Settings live beside the usage counter, but separate from the API key in
# `env`: secrets and preferences have different handling, and keeping them apart
# means the settings file can be read, edited and shared without leaking one.
CONFIG_FILE = Path.home() / ".config" / "doc2md" / "config.json"

# Only these are user-settable. The classification thresholds are deliberately
# absent: they were calibrated against a sample corpus (see MIGRATION.md) and
# exposing them in a settings window would invite silently breaking the
# classifier with no way to tell that it had happened.
#
#   setting name -> (module constant, parser, validator)
CONFIGURABLE: dict = {
    "output_dir": ("DEFAULT_OUTPUT_DIR", lambda v: Path(v).expanduser(), None),
    "vision_model": ("VISION_MODEL", str, lambda v: bool(v.strip())),
    "vision_hard_cap": ("VISION_HARD_CAP", int, lambda v: 1 <= v <= 500),
    "vision_warn_threshold": ("VISION_WARN_THRESHOLD", int, lambda v: 0 <= v <= 500),
    "vision_daily_budget": ("VISION_DAILY_BUDGET", int, lambda v: v > 0),
    "ocr_languages": ("OCR_DEFAULT_LANGS", str, lambda v: bool(v.strip())),
    "ocr_engine": ("OCR_ENGINE", str, lambda v: v in {"auto", "vision", "tesseract"}),
}

# ── Page-level diagram detection ──────────────────────────────────────────────
# An infographic drawn in vectors has a real text layer, so it is never
# rasterized for OCR, and holds no embedded image, so nothing reaches the
# classifier. It therefore used to pass straight through as scrambled fragments.
# Measured on the samples that motivated this:
#   Holacracy roadmap (broken):  drawings=49, images=0, avg line=12.8 chars
#   Tractor brochure (fine):     drawings=13, images=6, avg line=28.7 chars
#   Tractor brochure 3 (fine):   drawings= 5, images=8, avg line=43.5 chars
PAGE_DIAGRAM_MIN_DRAWINGS = 20
PAGE_DIAGRAM_MAX_AVG_LINE = 20.0
PAGE_DIAGRAM_MIN_LINES = 8
PAGE_DIAGRAM_MAX_IMAGES = 2

# Image files accepted as direct input (screenshots, scans, exported diagrams).
# Broader than MarkItDown's .jpg/.jpeg/.png because these bypass MarkItDown
# entirely and are read by Pillow, which handles all of these.
IMAGE_INPUT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

# Image classification thresholds.
#
# Colour counts are taken from a NEAREST-neighbour downsample. The previous
# LANCZOS resize interpolated between neighbouring pixels and manufactured
# colours that were never in the source: a two-tone logo measured 225 distinct
# colours, putting it far outside the "line art" band it belongs in. NEAREST
# preserves the original palette and separates line art from text from photos
# cleanly. Every threshold below is expressed on that scale.
MIN_IMAGE_SIZE = 50            # Skip images smaller than 50×50 px
MAX_COLORS_DIAGRAM = 64        # At or below this, the image is line art
EDGE_DENSITY_THRESHOLD = 0.02  # Line art needs at least this much structure
DECORATIVE_MIN_ASPECT = 3.0    # Long thin line art is a rule or divider
DECORATIVE_MAX_SHORT_SIDE = 200  # Small line art is a logo, not a diagram

# The photo branch is deliberately conservative: three conditions must hold at
# once. Mislabelling a photo as "text" merely wastes one OCR call and produces
# the same output, whereas mislabelling a text-heavy page as "photo" skips OCR
# and loses its content outright. Calibrated against few samples — see the
# classifier note in MIGRATION.md.
PHOTO_STDDEV_THRESHOLD = 60
PHOTO_MIN_COLORS = 280
PHOTO_MIN_EDGE_DENSITY = 0.05

# A second, independent photo test, needed because edge density is
# resolution-dependent: a 4032×3024 phone photo is smooth at the pixel level and
# scores lower than a small dense scan, so the rule above misses real photos.
#
# What holds regardless of resolution is that a document has a page under it.
# That is asked two ways, because a scan is not necessarily white: a grey
# photocopy of a book page has 0.0% near-white pixels, yet 80% of it sits in a
# narrow band around one dominant grey — the paper. A photograph has neither a
# light margin nor a single dominant tone. Measured: near-white 0.0–6.2% for
# photographs against 9.8–84.5% for documents; modal share 3–34% against 77–81%.
# The edge ceiling then keeps a dark, dense newspaper scan on the OCR path.
PHOTO_MAX_BACKGROUND = 0.05
PHOTO_MAX_MODAL_SHARE = 0.35
PHOTO_MAX_TONAL_EDGE = 0.06
BACKGROUND_LUMINANCE = 235  # At or above this counts as white-paper background
MODAL_BAND_HALFWIDTH = 12   # Luminance spread still counted as "the same tone"

# Escalation from OCR to the vision model.
#
# Colour count cannot tell a screenshotted diagram from a scanned page — real
# diagrams are screenshots with hundreds of colours, not two-tone line art. Two
# signals that do work, and must BOTH agree before an API call is made:
#
#   1. Edge density. Dense small glyphs generate far more edges per pixel than
#      the large sparse shapes of a diagram. Scanned pages measured 0.044–0.084;
#      diagrams and tables measured 0.017–0.041.
#   2. OCR output shape. Continuous document text comes back as long lines;
#      a diagram comes back as scattered short labels.
#
# Requiring both keeps a dense scanned page out of the vision model even if one
# signal misreads, and OCR has already run by then, so the check costs nothing.
SEMANTIC_MAX_EDGE_DENSITY = 0.06
OCR_PROSE_WORDS_PER_LINE = 8.0
# A diagram scatters its text across many separate labels. Below this many
# fragments there is no structure for the vision model to recover — a screenshot
# holding one clean line of text is already fully captured by OCR.
OCR_MIN_FRAGMENTS = 4

# Gemini vision prompt for semantic images
VISION_PROMPT = (
    "Describe this diagram, infographic, flowchart, or concept map in concise "
    "Markdown. Preserve ALL relationships, hierarchy, labels, and data values. "
    "Use headings, nested lists, or tables as appropriate to represent the "
    "structure. Do NOT describe decorative elements, colors, or styling. "
    "Output ONLY the Markdown description, no preamble."
)

# Map langdetect codes → Tesseract language codes.
# Only English and Spanish traineddata are expected to be installed; ocr_image()
# verifies availability before switching, so extra entries would be inert anyway.
LANG_MAP = {"en": "eng", "es": "spa"}


# ── Data Classes ──────────────────────────────────────────────────────────────
@dataclass
class ImageInfo:
    """Metadata for an extracted image."""
    path: Path                        # Saved file path
    source_page: int | None = None    # Page number (0-indexed, PDF only)
    width: int = 0
    height: int = 0
    classification: str = "unknown"   # skip | text | semantic | photo
    ocr_text: str = ""
    vision_description: str = ""
    edge_density: float = 0.0         # Set by classify_image; reused for escalation
    render_discarded: bool = False    # Page render deleted after OCR; omit its link
    background_frac: float = 0.0      # Share of near-white pixels; 0 for a photo
    modal_share: float = 0.0          # Share clustered on one tone; high for paper
    from_page_render: bool = False    # True for pages rasterized out of a PDF
    # A page rendered because it is a diagram: its text layer is scrambled, so a
    # successful description should replace that text rather than sit beside it.
    replaces_page_text: bool = False
    # Positioned OCR lines, when the engine reported geometry, and the structured
    # Markdown rebuilt from them. ocr_text keeps the flat output either way: the
    # classification heuristics are tuned on that shape. See apply_layout().
    layout_lines: list = field(default_factory=list)
    ocr_markdown: str = ""
    page_number: str = ""             # recovered from a stripped running footer


@dataclass
class PdfPageInfo:
    """One page's extracted Markdown, plus whether it still needs OCR."""
    number: int          # 0-indexed, normalised on ingest
    markdown: str = ""
    needs_ocr: bool = False
    # Why pdf-inspector wants OCR: "no_text_layer", "suspected_garbled_text", …
    # A garbled page *has* text, so reporting it as empty would mislead.
    ocr_reason: str = ""
    # Kept apart from `markdown` as well as merged into it: a diagram page's text
    # is replaced wholesale by its vision description, and the links must survive
    # that — an address is content the description cannot reconstruct.
    links: list = field(default_factory=list)


@dataclass
class DocumentSource:
    """
    The text layer of a document, before images are merged in.

    ``pages`` is populated only for PDFs handled by pdf-inspector; every other
    format produces a single flat ``markdown`` string, exactly as before.
    """
    markdown: str
    pages: list[PdfPageInfo] = field(default_factory=list)
    pdf_type: str | None = None
    page_count: int = 0
    title: str = ""        # the document's own title, when it declares one


@dataclass
class ProcessingReport:
    """Collects stats during processing."""
    input_file: str = ""
    pages_processed: int = 0
    images_detected: int = 0
    images_skipped: int = 0
    images_ocr: int = 0
    images_ai: int = 0
    images_photo: int = 0
    warnings: list[str] = field(default_factory=list)
    title: str = ""               # The document's own declared title, if any
    vision_tokens: int = 0        # Actual tokens spent, thinking included
    vision_held: int = 0          # Deferred pending confirmation, not lost
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def processing_time(self) -> float:
        return self.end_time - self.start_time

    def render(self) -> str:
        w = 50
        lines = [
            "═" * w,
            f" Processing Report: {Path(self.input_file).name}",
            "═" * w,
            f" Pages processed:     {self.pages_processed:>5}",
            f" Images detected:     {self.images_detected:>5}",
            f" ├─ Skipped (icons):  {self.images_skipped:>5}",
            f" ├─ OCR processed:    {self.images_ocr:>5}",
            f" ├─ AI analyzed:      {self.images_ai:>5}",
            f" └─ Photos (ref only):{self.images_photo:>5}",
            f" Warnings:            {len(self.warnings):>5}",
        ]
        for warn in self.warnings:
            lines.append(f"  - {warn}")
        if self.vision_tokens:
            lines.append(
                f" Vision tokens:       {self.vision_tokens:>5,}"
                f"  ({100.0 * self.vision_tokens / VISION_DAILY_BUDGET:.0f}% of daily)"
            )
        if self.vision_held:
            lines.append(f" Vision held:         {self.vision_held:>5}  (re-run with --vision-ok)")
        lines.append(f" Processing time:   {self.processing_time:>5.1f}s")
        lines.append("═" * w)
        return "\n".join(lines)


# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("doc2md")


# ── Image Extraction ─────────────────────────────────────────────────────────
def extract_images_pdf(input_path: Path, images_dir: Path, report: ProcessingReport) -> list[ImageInfo]:
    """Extract embedded images from a PDF using PyMuPDF."""
    images: list[ImageInfo] = []
    try:
        doc = fitz.open(str(input_path))
    except Exception as e:
        report.warnings.append(f"Failed to open PDF for image extraction: {e}")
        return images

    report.pages_processed = len(doc)
    seen_xrefs: set[int] = set()

    for page_idx in range(len(doc)):
        page = doc.load_page(page_idx)
        try:
            image_list = page.get_images(full=True)
        except Exception as e:
            report.warnings.append(f"Page {page_idx + 1}: Image listing failed ({e})")
            continue

        for img_info in image_list:
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image.get("ext", "png")

                # Generate a stable filename based on content hash
                content_hash = hashlib.md5(image_bytes).hexdigest()[:8]
                filename = f"page{page_idx + 1}_{content_hash}.{image_ext}"
                save_path = images_dir / filename

                save_path.write_bytes(image_bytes)

                # Get dimensions
                with Image.open(io.BytesIO(image_bytes)) as pil_img:
                    w, h = pil_img.size

                images.append(ImageInfo(
                    path=save_path,
                    source_page=page_idx,
                    width=w,
                    height=h,
                ))
            except Exception as e:
                report.warnings.append(
                    f"Page {page_idx + 1}: Image extraction failed (xref={xref}, {e})"
                )

    doc.close()
    return images


def extract_images_zip(input_path: Path, images_dir: Path, media_prefix: str, report: ProcessingReport) -> list[ImageInfo]:
    """Extract images from a ZIP-based format (DOCX, PPTX)."""
    images: list[ImageInfo] = []
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".svg", ".emf", ".wmf"}

    try:
        with zipfile.ZipFile(str(input_path), "r") as zf:
            for entry in zf.namelist():
                if not entry.startswith(media_prefix):
                    continue
                ext = Path(entry).suffix.lower()
                if ext not in image_extensions:
                    continue

                try:
                    image_bytes = zf.read(entry)
                    filename = Path(entry).name
                    save_path = images_dir / filename
                    save_path.write_bytes(image_bytes)

                    # Get dimensions (skip vector formats)
                    w, h = 0, 0
                    if ext not in {".svg", ".emf", ".wmf"}:
                        with Image.open(io.BytesIO(image_bytes)) as pil_img:
                            w, h = pil_img.size

                    images.append(ImageInfo(
                        path=save_path,
                        width=w,
                        height=h,
                    ))
                except Exception as e:
                    report.warnings.append(f"Failed to extract {entry}: {e}")
    except zipfile.BadZipFile:
        report.warnings.append(f"File is not a valid ZIP archive: {input_path.name}")
    except Exception as e:
        report.warnings.append(f"ZIP extraction error: {e}")

    return images


def _ocr_render_scale(page) -> tuple[float, float]:
    """
    Zoom factors for rasterizing one page for OCR.

    Starts from PDF_OCR_RENDER_DPI and steps down if that would exceed
    PDF_OCR_MAX_LONG_SIDE, so an oversized page box cannot blow up the render.
    """
    zoom = PDF_OCR_RENDER_DPI / 72.0
    long_side_pt = max(page.rect.width, page.rect.height)
    if long_side_pt * zoom > PDF_OCR_MAX_LONG_SIDE:
        zoom = PDF_OCR_MAX_LONG_SIDE / long_side_pt
    return zoom, zoom


def render_pdf_pages(
    input_path: Path,
    page_numbers: list[int],
    images_dir: Path,
    report: ProcessingReport,
    classification: str = "text",
    replaces_page_text: bool = False,
) -> list[ImageInfo]:
    """
    Rasterize specific PDF pages so that scanned content can reach OCR.

    pdf-inspector reports which pages have no extractable text but cannot render
    them — it does no image work at all — so PyMuPDF does the rasterizing.

    The returned images are pre-classified as "text": these pages are *known* to
    need OCR, and running them back through classify_image() would risk a
    photographed page being labelled "photo" and silently dropped.
    """
    if not page_numbers:
        return []

    rendered: list[ImageInfo] = []
    try:
        doc = fitz.open(str(input_path))
    except Exception as e:
        report.warnings.append(f"Could not open PDF to render OCR pages: {e}")
        return rendered

    for page_idx in page_numbers:
        if not 0 <= page_idx < len(doc):
            report.warnings.append(f"OCR page {page_idx + 1} out of range; skipped")
            continue
        try:
            page = doc.load_page(page_idx)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(*_ocr_render_scale(page)))
            suffix = "scan" if classification == "text" else "diagram"
            save_path = images_dir / f"page{page_idx + 1}_{suffix}.jpg"
            with Image.open(io.BytesIO(pixmap.tobytes("png"))) as rgb:
                rgb.convert("RGB").save(
                    save_path, "JPEG", quality=PDF_OCR_RENDER_QUALITY, optimize=True
                )
            rendered.append(ImageInfo(
                path=save_path,
                source_page=page_idx,
                width=pixmap.width,
                height=pixmap.height,
                classification=classification,
                from_page_render=True,
                replaces_page_text=replaces_page_text,
            ))
        except Exception as e:
            report.warnings.append(f"Page {page_idx + 1}: render for OCR failed ({e})")

    doc.close()
    return rendered


def extract_images_eml(input_path: Path, images_dir: Path, report: ProcessingReport) -> list[ImageInfo]:
    """
    Extract image attachments and inline images from an email.

    Screenshots pasted into a message are usually where its real content lives,
    so they go through the same classify → OCR → vision pipeline as any other
    embedded image. Non-image attachments are only named in the Markdown.
    """
    from email import policy
    from email.parser import BytesParser

    images: list[ImageInfo] = []
    try:
        with input_path.open("rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
    except Exception as e:
        report.warnings.append(f"Could not parse email for images: {e}")
        return images

    for index, part in enumerate(msg.walk()):
        if part.get_content_maintype() != "image":
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            name = part.get_filename() or f"inline{index}.{part.get_content_subtype()}"
            save_path = images_dir / Path(name).name
            save_path.write_bytes(payload)
            with Image.open(io.BytesIO(payload)) as pil_img:
                w, h = pil_img.size
            images.append(ImageInfo(path=save_path, width=w, height=h))
        except Exception as e:
            report.warnings.append(f"Failed to extract email image {index}: {e}")

    return images


def discard_page_renders(images: list[ImageInfo]) -> int:
    """
    Delete rasterized pages once their text has been read out of them.

    A page render is a means to an end: it exists so OCR has something to read.
    Keeping it stores the whole document a second time — roughly 130 MB for a
    300-page book — for an image that duplicates the source PDF. The extracted
    text stays in the Markdown either way.
    """
    discarded = 0
    for img in images:
        if img.from_page_render and not img.render_discarded:
            img.path.unlink(missing_ok=True)
            img.render_discarded = True
            discarded += 1
    return discarded


def discard_images_on_rendered_pages(
    images: list[ImageInfo], rendered_pages: list[int]
) -> list[ImageInfo]:
    """
    Drop embedded images belonging to pages that are about to be rasterized.

    On a scanned page the "embedded image" *is* the page. Keeping both it and
    the render would store the same content twice, OCR it twice, and — with
    vision enabled — spend a second API call describing the same thing. The
    render supersedes them, so the extracted copies are deleted from disk too.
    """
    superseded = set(rendered_pages)
    kept: list[ImageInfo] = []

    for img in images:
        if img.source_page in superseded:
            logger.debug(f"  {img.path.name}: superseded by page render")
            img.path.unlink(missing_ok=True)
        else:
            kept.append(img)

    return kept


def is_image_input(input_path: Path) -> bool:
    """True if the document itself is an image (screenshot, scan, diagram export)."""
    return input_path.suffix.lower() in IMAGE_INPUT_EXTENSIONS


def ingest_image_file(input_path: Path, images_dir: Path, report: ProcessingReport) -> list[ImageInfo]:
    """
    Treat a standalone image file as a one-image document.

    The file is copied into the images folder so the emitted Markdown keeps the
    same relative-link shape as every other format, and so the original is never
    modified or moved.
    """
    try:
        image_bytes = input_path.read_bytes()
        save_path = images_dir / input_path.name
        save_path.write_bytes(image_bytes)
        with Image.open(io.BytesIO(image_bytes)) as pil_img:
            w, h = pil_img.size
    except Exception as e:
        report.warnings.append(f"Could not read image {input_path.name}: {e}")
        return []

    return [ImageInfo(path=save_path, source_page=0, width=w, height=h)]


def extract_images(input_path: Path, images_dir: Path, report: ProcessingReport) -> list[ImageInfo]:
    """Route to the correct image extractor based on file type."""
    suffix = input_path.suffix.lower()

    if is_image_input(input_path):
        report.pages_processed = 1
        return ingest_image_file(input_path, images_dir, report)
    elif suffix == ".pdf":
        return extract_images_pdf(input_path, images_dir, report)
    elif suffix == ".eml":
        report.pages_processed = 1
        return extract_images_eml(input_path, images_dir, report)
    elif suffix == ".docx":
        report.pages_processed = 1  # DOCX doesn't have a clean page count
        return extract_images_zip(input_path, images_dir, "word/media/", report)
    elif suffix == ".pptx":
        return extract_images_zip(input_path, images_dir, "ppt/media/", report)
    else:
        # For HTML, EPUB, etc. — MarkItDown handles these; no separate extraction
        logger.info(f"No dedicated image extractor for {suffix}; relying on MarkItDown output.")
        report.pages_processed = 1
        return []


# ── Image Classification ─────────────────────────────────────────────────────
def classify_image(img_info: ImageInfo) -> str:
    """
    Classify an image locally using PIL heuristics.
    Returns: "skip" | "text" | "semantic" | "photo"
    """
    # Skip tiny images (icons, bullets, spacers)
    if img_info.width < MIN_IMAGE_SIZE or img_info.height < MIN_IMAGE_SIZE:
        return "skip"

    try:
        with Image.open(img_info.path) as img:
            img_rgb = img.convert("RGB")

            # 1. Unique colours, from a NEAREST downsample so the source palette
            #    survives (see the note beside the thresholds above).
            small = img_rgb.resize((100, 100), Image.Resampling.NEAREST)
            colors = small.getcolors(maxcolors=10000)
            num_colors = len(colors) if colors else 10000

            # 2. Grayscale spread — wide range suggests photographic tone
            gray = img_rgb.convert("L")
            stddev = ImageStat.Stat(gray).stddev[0]

            # 3. Edge density, normalised to 0–1
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_mean = ImageStat.Stat(edges).mean[0] / 255.0
            img_info.edge_density = edge_mean

            # 3b. Share of near-white pixels — the "is there a page under this"
            #     signal. Downsampled first so the cost is fixed for huge images.
            histogram = gray.resize((200, 200), Image.Resampling.BILINEAR).histogram()
            total_px = max(sum(histogram), 1)
            background = sum(histogram[BACKGROUND_LUMINANCE:]) / total_px
            img_info.background_frac = background

            # 3c. Share of pixels clustered around the single most common
            #     luminance. Catches non-white paper, which has a dominant tone
            #     even though it has no near-white pixels at all.
            mode = max(range(256), key=lambda level: histogram[level])
            band = histogram[max(0, mode - MODAL_BAND_HALFWIDTH):mode + MODAL_BAND_HALFWIDTH + 1]
            modal_share = sum(band) / total_px
            img_info.modal_share = modal_share

            # 4. Shape
            short_side = min(img_info.width, img_info.height)
            aspect = max(img_info.width, img_info.height) / max(short_side, 1)

            logger.debug(
                f"  Classification: colors={num_colors}, stddev={stddev:.1f}, "
                f"edge_density={edge_mean:.4f}, aspect={aspect:.1f}, short={short_side}"
            )

            if num_colors <= MAX_COLORS_DIAGRAM:
                # Line art. Measured against the sample corpus, a low colour
                # count means a logo, a rule, or an image of text — NOT a
                # diagram. Real diagrams are screenshots carrying hundreds to
                # thousands of colours, so they never reach this branch. Whether
                # something is worth a vision call is decided after OCR, by
                # should_escalate_to_vision(), which has better evidence.
                if edge_mean < EDGE_DENSITY_THRESHOLD:
                    return "skip"          # Flat fill — a rule or a divider
                if short_side < DECORATIVE_MAX_SHORT_SIDE and aspect < DECORATIVE_MIN_ASPECT:
                    return "skip"          # Small and compact — a logo
                return "text"

            if (
                stddev > PHOTO_STDDEV_THRESHOLD
                and num_colors > PHOTO_MIN_COLORS
                and edge_mean > PHOTO_MIN_EDGE_DENSITY
            ):
                return "photo"

            if (
                background < PHOTO_MAX_BACKGROUND
                and modal_share < PHOTO_MAX_MODAL_SHARE
                and edge_mean < PHOTO_MAX_TONAL_EDGE
                and num_colors > PHOTO_MIN_COLORS
            ):
                # Continuous tone edge to edge: no white margin, no dominant
                # paper tone, and no dense type. That is a photograph.
                return "photo"

            return "text"                  # Default: worth an OCR attempt

    except Exception as e:
        logger.warning(f"Classification failed for {img_info.path.name}: {e}")
        return "text"  # Safe fallback: try OCR


# ── OCR Processing ───────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def available_ocr_languages() -> tuple[str, ...]:
    """
    Tesseract languages installed on this machine.

    Cached because pytesseract answers this by shelling out to the binary, and
    ocr_image() would otherwise pay for a subprocess on every single image.
    """
    try:
        return tuple(pytesseract.get_languages())
    except Exception:
        return ("eng",)


def run_ocr_batch(images: list[ImageInfo]) -> None:
    """
    OCR several images at once, writing the result onto each ImageInfo.

    tesseract runs as a separate process and blocks, so threads overlap the work
    without contending for the GIL. Falls back to a serial loop for a single
    image, where the pool would only add overhead.
    """
    def run(img: ImageInfo) -> None:
        # Each image gets its own list, so the pool never shares one.
        lines: list[LayoutLine] = []
        img.ocr_text = ocr_image(img.path, lines)
        img.layout_lines = lines

    if not images:
        return
    if len(images) == 1:
        run(images[0])
        return

    workers = min(OCR_MAX_WORKERS, len(images))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run, images))


def upscale_for_ocr(img: Image.Image) -> Image.Image:
    """
    Enlarge an image that is too low-resolution for Tesseract to read.

    Below roughly 150 dpi Tesseract does not degrade — it returns nothing. A
    604×401 newspaper clipping yielded 0 characters at native size and 1120 at
    2×. Images already large enough are returned untouched.
    """
    short_side = min(img.size)
    if short_side <= 0 or short_side >= OCR_MIN_SHORT_SIDE:
        return img

    factor = min(OCR_MAX_UPSCALE, -(-OCR_MIN_SHORT_SIDE // short_side))
    if factor < 2:
        return img

    logger.debug(f"  Upscaling {img.size} by {factor}× for OCR")
    return img.resize(
        (img.width * factor, img.height * factor), Image.Resampling.LANCZOS
    )



@lru_cache(maxsize=1)
def find_vision_ocr() -> str:
    """
    Locate the Apple Vision helper, if it has been built.

    Optional by design: absent, everything falls back to Tesseract and nothing
    else changes. Built by ocr/build.sh.
    """
    if sys.platform != "darwin":
        return ""
    candidates = [
        Path(__file__).resolve().parent / "ocr" / "bin" / "doc2md-ocr",
        Path.home() / ".local" / "bin" / "doc2md-ocr",
        Path("/opt/homebrew/bin/doc2md-ocr"),
        Path("/usr/local/bin/doc2md-ocr"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def ocr_with_vision(img_path: Path, lang_str: str,
                    layout: list | None = None) -> str:
    """
    Recognise text with Apple Vision, via the helper binary.

    Pass a list as ``layout`` to also collect per-line geometry; it costs nothing
    extra, since the helper reports both from the same recognition pass, and it
    is what reflow_layout() needs to rebuild paragraphs and headings. Callers
    that only want the text leave it out and are unaffected.

    Measured against Tesseract on the sample corpus, at the same 200 dpi render:

        scanned page   1388 chars in 1.6 s   vs 1359 in 3.6 s
        brochure page  1283 chars in 0.4 s   vs 1286 in 0.8 s
        concept map     350 chars in 0.3 s   vs  248 in 0.4 s
        newspaper photo 721 chars in 0.4 s   vs    0 — Tesseract read nothing

    Roughly twice as fast, never worse, and markedly better on degraded or
    photographed sources, which is what it was built for. It also needs no
    traineddata: language support is part of the OS.

    Returns "" on any failure so the caller can fall back rather than lose a page.
    """
    binary = find_vision_ocr()
    if not binary:
        return ""
    command = [binary, str(img_path), lang_str]
    if layout is not None:
        command.append("--json")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    if layout is None:
        return completed.stdout.strip()

    # A helper predating --json ignores the flag and prints plain lines. Parsing
    # yields nothing then, so the text is taken from the raw output and the page
    # simply goes unstructured rather than coming back empty.
    lines = parse_layout_lines(completed.stdout)
    if not lines:
        return completed.stdout.strip()
    layout.extend(lines)
    return "\n".join(line.text for line in lines).strip()


def ocr_image(img_path: Path, layout: list | None = None) -> str:
    """
    Run OCR with auto language detection.

    Apple Vision is preferred when its helper is present; Tesseract is the
    fallback and the only option off macOS. Set ocr_engine to force one.

    Pass a list as ``layout`` to collect per-line geometry alongside the text.
    Only Vision reports it; a Tesseract result leaves the list empty, and the
    page keeps its flat unstructured text rather than failing.
    1. First pass with eng+spa
    2. Detect language with langdetect
    3. Re-run with detected language if different
    """
    available_langs = available_ocr_languages()

    # Build initial language string from what's available
    initial_langs = []
    for lang in OCR_DEFAULT_LANGS.split("+"):
        if lang in available_langs:
            initial_langs.append(lang)
    if not initial_langs:
        initial_langs = ["eng"]
    lang_str = "+".join(initial_langs)

    # First pass. Vision reads the file directly — it does its own scaling, so the
    # upscale that Tesseract needs below 300 dpi is neither required nor helpful.
    if OCR_ENGINE in ("auto", "vision") and find_vision_ocr():
        text = ocr_with_vision(img_path, lang_str, layout)
        if text:
            return text
        if OCR_ENGINE == "vision":
            return ""
        logger.debug(f"  Vision returned nothing for {img_path.name}; trying Tesseract")

    # Decoded here rather than at the top of the function: only Tesseract needs
    # the pixels. Vision reads the file itself and does its own scaling, so on
    # the default path this decode — and the upscale after it — never happen.
    try:
        with Image.open(img_path) as opened:
            pil_img = upscale_for_ocr(opened)
            text = pytesseract.image_to_string(pil_img, lang=lang_str).strip()

            if not text or len(text) < 5:
                return text

            # Auto-detect language and re-run if needed. Inside the `with`, since
            # the re-run needs the same decoded image.
            if HAS_LANGDETECT:
                try:
                    detected = langdetect_detect(text)
                    tess_lang = LANG_MAP.get(detected)
                    if (tess_lang and tess_lang not in initial_langs
                            and tess_lang in available_langs):
                        logger.info(
                            f"  Re-running OCR with detected language: "
                            f"{detected} → {tess_lang}"
                        )
                        text = pytesseract.image_to_string(
                            pil_img, lang=tess_lang
                        ).strip()
                except Exception:
                    pass  # langdetect can fail on short or ambiguous text

            return text
    except Exception as e:
        logger.warning(f"Cannot read image for OCR: {img_path.name} ({e})")
        return ""


def ocr_looks_like_prose(text: str) -> bool:
    """
    True if OCR output reads like continuous document text rather than the
    scattered labels you get from running OCR over a diagram.

    Uses mean words per non-empty line: a scanned page of prose averaged 12.5,
    while diagrams, concept maps and tables averaged 1.8–5.1.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    words_per_line = sum(len(line.split()) for line in lines) / len(lines)
    return words_per_line >= OCR_PROSE_WORDS_PER_LINE


def should_escalate_to_vision(img: ImageInfo) -> bool:
    """
    Decide whether an OCR'd image would be better described by the vision model.

    Called only after OCR has run, so a confident local result costs nothing.
    Pages rasterized out of a PDF are never escalated: they are known to be
    document pages, and a long scanned document would otherwise turn into one
    API call per page.
    """
    if img.classification != "text" or img.from_page_render:
        return False
    if img.edge_density >= SEMANTIC_MAX_EDGE_DENSITY:
        return False  # Dense small glyphs — this is a page of text, OCR has it
    fragments = [line for line in img.ocr_text.splitlines() if line.strip()]
    if len(fragments) < OCR_MIN_FRAGMENTS:
        # Includes the no-text-at-all case. An image OCR found nothing in could
        # be an unlabelled diagram, but it is far more often a photograph, and
        # escalating on no evidence would send private photos to the API.
        return False  # Too little structure to be a diagram worth describing
    return not ocr_looks_like_prose(img.ocr_text)


# ── Layout Reconstruction ────────────────────────────────────────────────────
@dataclass
class LayoutLine:
    """
    One recognised line of text, with the geometry needed to place it.

    Coordinates are normalised to the page, 0–1, with the origin at the bottom
    left — Vision's convention, kept rather than converted so that anyone
    comparing this against the helper's output sees the same numbers.
    """
    text: str
    x: float = 0.0        # left edge
    y: float = 0.0        # bottom edge
    width: float = 0.0
    height: float = 0.0
    confidence: float = 1.0


def parse_layout_lines(payload: str) -> list[LayoutLine]:
    """
    Read the JSON-lines geometry emitted by ``doc2md-ocr --json``.

    Malformed lines are skipped rather than raising: a page whose geometry cannot
    be read should fall back to flat text, not lose its content altogether.
    """
    lines: list[LayoutLine] = []
    for raw in payload.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
            lines.append(LayoutLine(
                text=str(item["t"]),
                x=float(item.get("x", 0.0)),
                y=float(item.get("y", 0.0)),
                width=float(item.get("w", 0.0)),
                height=float(item.get("h", 0.0)),
                confidence=float(item.get("c", 1.0)),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return lines


# A leading list marker: a bullet glyph, or a number/letter followed by . or ).
# Anchored and deliberately narrow — anything looser starts treating "1998 was"
# and initials like "J. Smith" as list items.
LIST_MARKER_RE = re.compile(
    r"^\s*(?:([•·◦‣▪–—*+-])|(\d{1,3})[.)]|([a-z])[.)])\s+(?=\S)"
)


def list_marker(text: str) -> tuple[str, str] | None:
    """
    Split a line into (markdown marker, remaining text) when it opens a list item.

    Returns None for ordinary lines. Numbered items keep their own number so a
    list starting at 3 still reads as 3; bullets are normalised to "-".
    """
    match = LIST_MARKER_RE.match(text)
    if not match:
        return None
    bullet, number, letter = match.groups()
    rest = text[match.end():].strip()
    if not rest:
        return None  # A lone marker is a stray glyph, not a list item.
    if number:
        return f"{number}.", rest
    if letter:
        return f"{letter}.", rest
    return "-", rest


# Punctuation that a heading does not end on. A trailing comma, colon or full
# stop means the line is part of a sentence, however short and however large it
# measured; a trailing hyphen means it is a word broken across a line break.
HEADING_TRAILING_PUNCTUATION = ",;:.!?-–—"


def looks_like_heading(text: str) -> bool:
    """
    Whether a line's *wording* could be a heading, ignoring its geometry.

    Only ever used to veto: a line that reads like part of a sentence is not
    promoted no matter how it measures. Geometry decides the rest.
    """
    text = text.strip()
    if not text or len(text.split()) > LAYOUT_HEADING_MAX_WORDS:
        return False
    if text.rstrip('"\'’”').endswith(tuple(HEADING_TRAILING_PUNCTUATION)):
        return False
    return any(character.isalpha() for character in text)


def join_wrapped(previous: str, addition: str) -> str:
    """
    Append a wrapped line to the one above it, repairing a split word.

    A trailing hyphen is dropped when the continuation is lower-case, which is
    ordinary end-of-line hyphenation ("bet-" + "ter" → "better"). It is kept when
    the continuation is capitalised, because that is far more often a real
    compound broken across lines ("Franco-" + "American") than a hyphenated word
    that happens to resume with a capital.
    """
    if previous.endswith("-") and addition[:1].isalpha():
        if addition[:1].islower():
            return previous[:-1] + addition
        return previous + addition
    return previous + " " + addition


def _median(values: list[float], fallback: float) -> float:
    return statistics.median(values) if values else fallback


def reflow_layout(lines: list[LayoutLine]) -> str:
    """
    Rebuild paragraphs, headings and lists from positioned OCR lines.

    Reading order is taken as given and never re-sorted — see LAYOUT_COLUMN_JUMP.
    Decisions come from four signals: the vertical gap to the previous line
    (section breaks), the left edge against the line above (paragraph breaks in
    typeset books, which indent rather than add leading), the line's height and
    isolation (headings), and a leading marker glyph (lists).

    Verse and other hard-wrapped text is reflowed into paragraphs like anything
    else. Distinguishing a poem from a wrapped paragraph needs the right margin
    to be reliable, and on OCR'd scans it is not; running the two together costs
    a line break, while the alternative costs paragraph structure on every page.
    """
    lines = [ln for ln in lines if ln.text.strip()]
    if not lines:
        return ""

    gaps = [
        lines[i - 1].y - lines[i].y
        for i in range(1, len(lines))
        if 0 < lines[i - 1].y - lines[i].y < 0.1
    ]
    pitch = _median(gaps, 0.02)
    median_height = _median([ln.height for ln in lines], 0.02)
    median_width = _median([ln.width for ln in lines], 1.0)

    blocks: list[str] = []
    paragraph = ""
    pending_marker = ""
    marker_x = 0.0

    def flush() -> None:
        nonlocal paragraph, pending_marker
        if paragraph:
            blocks.append(f"{pending_marker} {paragraph}" if pending_marker else paragraph)
        paragraph = ""
        pending_marker = ""

    for index, line in enumerate(lines):
        text = line.text.strip()
        delta = lines[index - 1].y - line.y if index else 0.0

        new_column = index > 0 and delta < -LAYOUT_COLUMN_JUMP
        # Two observations at nearly the same height are two halves of one visual
        # line — Vision splits a line whenever the spacing widens, which a hanging
        # list number reliably does. Never a break, whatever the indents say.
        # Measured against the line's own height rather than the page pitch: the
        # pitch is a median over the whole page and says nothing useful on a page
        # with only a handful of lines, while half a line height is the same
        # question the eye asks — do these two boxes sit at the same level?
        # abs(), because either half may be reported a hair higher than the other;
        # a real column change is an order of magnitude further and cannot be
        # mistaken for one here.
        overlap = max(line.height, lines[index - 1].height if index else 0) * 0.5
        same_line = index > 0 and abs(delta) < overlap
        separated = delta > pitch * LAYOUT_PARA_GAP
        indented = (
            index > 0
            and not new_column
            and not same_line
            and line.x > lines[index - 1].x + LAYOUT_INDENT
        )
        marker = list_marker(text)

        # Inside a list item, the hanging indent of a continuation line is the
        # normal shape of the item, not the start of something new — measured at
        # 0.072 on one sample, far past the paragraph-indent threshold. The item
        # ends at the next marker, a wide gap, a column change, or an outdent
        # back to where the marker itself began.
        if pending_marker and not marker and not separated and not new_column:
            indented = indented and line.x + LAYOUT_INDENT < marker_x

        # Isolation on both sides is the signal that survives OCR. A heading sits
        # in its own whitespace; a short body line does not, and the line below a
        # heading starts a paragraph rather than continuing one.
        below = lines[index + 1] if index + 1 < len(lines) else None
        gap_below = line.y - below.y if below else 1.0
        isolated_above = index == 0 or new_column or separated or indented
        isolated_below = gap_below > pitch * LAYOUT_PARA_GAP or gap_below < 0

        is_heading = (
            not marker
            and not same_line
            and looks_like_heading(text)
            and line.width < median_width * LAYOUT_HEADING_MAX_WIDTH
            and isolated_above
            and (
                # Genuinely larger type, or the all-caps section head that
                # measures no taller than body text but stands alone.
                line.height > median_height * LAYOUT_HEADING_HEIGHT
                or (text == text.upper() and isolated_below)
            )
        )

        if new_column or separated or indented or is_heading or marker:
            flush()

        if is_heading:
            blocks.append(f"## {text}")
            continue
        if marker:
            pending_marker, text = marker
            marker_x = line.x

        paragraph = join_wrapped(paragraph, text) if paragraph else text

    flush()
    return "\n\n".join(blocks)


# Digits and roman numerals are replaced before comparing lines across pages, so
# that "page 12" and "page 13" count as the same running footer. Roman numerals
# are matched only as whole words to keep "I" and "did" apart from real numbering.
FURNITURE_DIGITS_RE = re.compile(r"\d+")
FURNITURE_ROMAN_RE = re.compile(r"\b[ivxlcdm]{1,7}\b", re.IGNORECASE)
FURNITURE_NOISE_RE = re.compile(r"[^\w#]+")
# Roman numerals need two characters at least. A lone "C" or "I" is far more
# often a misread letter from a running head than a page number.
PAGE_NUMBER_RE = re.compile(r"^[^\w]*(\d{1,4}|[ivxlcdm]{2,7})[^\w]*$", re.IGNORECASE)
# A page number sharing its line with a running head, as "12  Chapter Title" or
# "Chapter Title  12". Only ever applied to a line already known to be furniture,
# where a number at the edge cannot be anything else.
EDGE_NUMBER_RE = re.compile(
    r"^[^\w]*(\d{1,4}|[ivxlcdm]{2,7})\b|\b(\d{1,4}|[ivxlcdm]{2,7})[^\w]*$",
    re.IGNORECASE,
)

ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
# Markdown emphasis around a running head, which pdf-inspector adds for italics.
MARKDOWN_EMPHASIS_RE = re.compile(r"[*_`]+")


def furniture_key(text: str) -> str:
    """
    Normalise a line so the same running header matches itself across pages.

    Page numbers vary by definition, so digits and roman numerals collapse to a
    marker; punctuation and case are dropped because OCR is inconsistent about
    both. What survives is the wording, which is what actually repeats.
    """
    key = FURNITURE_DIGITS_RE.sub("#", text.strip().lower())
    key = FURNITURE_ROMAN_RE.sub("#", key)
    return FURNITURE_NOISE_RE.sub("", key)


def page_number_of(text: str) -> str:
    """
    The page number in a line of furniture, or "" if there is not one.

    Accepts a line that is only a number, and a number sitting at either edge of
    a running head. The looser edge match is safe only because the caller has
    already established, from repetition, that the line is furniture.
    """
    text = MARKDOWN_EMPHASIS_RE.sub("", text).strip()
    match = PAGE_NUMBER_RE.match(text)
    if match:
        return match.group(1)
    match = EDGE_NUMBER_RE.search(text)
    if match:
        return match.group(1) or match.group(2)
    return ""


def page_number_value(text: str) -> int | None:
    """Read a page number as an integer, accepting arabic or roman."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    if not text or any(character not in ROMAN_VALUES for character in text):
        return None
    total = 0
    for position, character in enumerate(text):
        value = ROMAN_VALUES[character]
        following = [ROMAN_VALUES[c] for c in text[position + 1:]]
        total += -value if following and value < max(following) else value
    return total


def keep_consistent_page_numbers(numbers: list[str]) -> list[str]:
    """
    Blank out page numbers that break the document's own ordering.

    OCR misreads folios often enough to matter — one 13-page sample produced
    "1101", "1115" and "C" among otherwise clean numbers — and a wrong page
    marker is worse than none, because its whole purpose is to be a citable
    anchor. Page numbers ascend through a document, so the longest non-decreasing
    run is kept and everything off it is dropped.

    Pages are not required to be consecutive: these documents are page drags, so
    gaps are normal and only the direction is checked.
    """
    values = [page_number_value(number) if number else None for number in numbers]
    indexes = [i for i, value in enumerate(values) if value is not None]
    if len(indexes) < 2:
        return numbers

    # Longest non-decreasing subsequence over the candidates, by index.
    best: list[int] = []
    previous: dict[int, int | None] = {}
    for i in indexes:
        chain = [j for j in best if values[j] <= values[i]]
        base = chain[-1] if chain else None
        previous[i] = base
        length = (best.index(base) + 1) if base is not None else 0
        if length == len(best):
            best.append(i)
        else:
            best[length] = i

    keep: set[int] = set()
    walk: int | None = best[-1] if best else None
    while walk is not None:
        keep.add(walk)
        walk = previous[walk]

    return [
        number if index in keep else ""
        for index, number in enumerate(numbers)
    ]


# End-of-line hyphenation, once the line break itself is gone: pdf-inspector
# joins wrapped lines with a space and leaves the hyphen, giving "familiar- ity".
HYPHEN_WRAP_RE = re.compile(r"(\w)-\s+(\w)")


def dehyphenate(text: str) -> str:
    """
    Repair words split across a line break in already-joined text.

    Same rule as join_wrapped(): the hyphen goes when the continuation is
    lower-case, and stays when it is capitalised, where it is far more often a
    real compound. Requires a word character immediately before the hyphen, so
    Markdown list markers and spaced dashes are left alone.
    """
    def repair(match: re.Match) -> str:
        head, tail = match.group(1), match.group(2)
        return head + tail if tail.islower() else f"{head}-{tail}"

    return HYPHEN_WRAP_RE.sub(repair, text)


def strip_text_layer_furniture(pages: list["PdfPageInfo"]) -> int:
    """
    Remove running headers and footers from a PDF that has a real text layer.

    Only the first and last non-empty line of each page are ever candidates —
    that is where furniture lives, and restricting it there means a header that
    got merged into a paragraph is left alone rather than cut out of the middle
    of the prose. As with the OCR path, repetition across pages is the evidence
    and a short document is untouched.
    """
    if len(pages) < FURNITURE_MIN_DOC_PAGES:
        return 0

    edges: list[set[str]] = []
    seen: dict[str, set[int]] = {}
    for index, page in enumerate(pages):
        lines = [line for line in page.markdown.splitlines() if line.strip()]
        candidates = {lines[0], lines[-1]} if lines else set()
        edges.append(candidates)
        for line in candidates:
            seen.setdefault(furniture_key(line), set()).add(index)

    drop = {
        key for key, page_numbers in seen.items()
        if key and len(page_numbers) >= FURNITURE_MIN_PAGES
    }
    if not drop:
        return 0

    removed = 0
    bodies: list[str] = []
    numbers: list[str] = []
    for index, page in enumerate(pages):
        kept: list[str] = []
        number = ""
        for line in page.markdown.splitlines():
            if line in edges[index] and furniture_key(line) in drop:
                removed += 1
                number = number or page_number_of(line)
                continue
            kept.append(line)
        bodies.append("\n".join(kept).strip())
        numbers.append(number)

    for page, body, number in zip(pages, bodies,
                                  keep_consistent_page_numbers(numbers)):
        page.markdown = f"<!-- page {number} -->\n\n{body}" if number and body else body
    return removed


def find_running_furniture(pages: list[list[LayoutLine]]) -> set[str]:
    """
    Identify running headers and footers by what repeats across pages.

    Repetition is the whole of the evidence. Position alone is not enough: on the
    sample corpus the bottom band of a page routinely holds ordinary body text,
    and one page's top band held the section heading "MAKE YOU LAUGH" in exactly
    the place a page number would sit. Stripping either would be worse than
    leaving a running head in, so a short document is left entirely alone.
    """
    if len(pages) < FURNITURE_MIN_DOC_PAGES:
        return set()

    seen: dict[str, set[int]] = {}
    for number, lines in enumerate(pages):
        measure = _median([ln.width for ln in lines if ln.text.strip()], 1.0)
        for line in lines:
            if not line.text.strip():
                continue
            in_band = line.y >= 1.0 - FURNITURE_BAND or line.y <= FURNITURE_BAND
            if in_band and line.width < measure * FURNITURE_MAX_WIDTH:
                seen.setdefault(furniture_key(line.text), set()).add(number)

    return {
        key for key, page_numbers in seen.items()
        if key and len(page_numbers) >= FURNITURE_MIN_PAGES
    }


def strip_running_furniture(images: list["ImageInfo"]) -> int:
    """
    Drop repeated headers and footers from every OCR'd page of a document.

    Page numbers survive as HTML comments: once a line is known to be furniture,
    recognising a bare numeral in it is free, and keeping the number preserves a
    citable anchor that the surrounding prose has no other way to express.
    """
    pages = [img for img in images if img.layout_lines]
    if len(pages) < FURNITURE_MIN_DOC_PAGES:
        return 0

    drop = find_running_furniture([img.layout_lines for img in pages])
    if not drop:
        return 0

    removed = 0
    for img in pages:
        kept: list[LayoutLine] = []
        numbers: list[str] = []
        for line in img.layout_lines:
            if furniture_key(line.text) in drop:
                removed += 1
                number = page_number_of(line.text)
                if number:
                    numbers.append(number)
            else:
                kept.append(line)
        img.layout_lines = kept
        img.page_number = numbers[0] if numbers else ""

    for img, number in zip(pages, keep_consistent_page_numbers(
            [img.page_number for img in pages])):
        img.page_number = number
    return removed


def apply_layout(images: list["ImageInfo"]) -> int:
    """
    Turn each OCR'd page's geometry into structured Markdown.

    Written to ``ocr_markdown`` rather than over ``ocr_text``, which stays as the
    flat one-line-per-line output. The classification heuristics downstream —
    ocr_looks_like_prose() above all — are tuned on that shape, and reflowing
    first would make every page look like prose and silently disable escalation
    to the vision model.

    Returns the number of running header/footer lines removed.
    """
    stripped = strip_running_furniture(images)
    for img in images:
        if not img.layout_lines:
            continue
        body = reflow_layout(img.layout_lines)
        if body and img.page_number:
            body = f"<!-- page {img.page_number} -->\n\n{body}"
        img.ocr_markdown = body
    return stripped


# ── Settings ─────────────────────────────────────────────────────────────────
def load_config(path: Path | None = None) -> dict:
    """
    Read the settings file, keeping only keys that are known and values that are
    sane.

    A settings file is edited by a GUI, by hand, and by future versions of this
    tool, so it is treated as untrusted input: anything unparseable is dropped
    and the built-in default stands. A corrupt config must never stop a
    conversion — the worst it should cost is a preference.
    """
    source = CONFIG_FILE if path is None else path
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}

    clean: dict = {}
    for key, (_, parse, valid) in CONFIGURABLE.items():
        if key not in raw or raw[key] is None:
            continue
        try:
            value = parse(raw[key])
        except (TypeError, ValueError):
            continue
        if valid is not None and not valid(value):
            continue
        clean[key] = value

    # A confirmation threshold above the hard cap can never fire, which would
    # silently disable the confirmation step. Treat that as "always confirm".
    cap = clean.get("vision_hard_cap", VISION_HARD_CAP)
    if clean.get("vision_warn_threshold", VISION_WARN_THRESHOLD) > cap:
        clean["vision_warn_threshold"] = cap

    return clean


def apply_config(path: Path | None = None) -> list[str]:
    """
    Override the module defaults from the settings file.

    Rebinding module globals rather than threading a config object through every
    function is deliberate: these values are read from a dozen places in one
    single-process run, and the alternative is a parameter on almost every
    signature for a value that never changes mid-run.

    Returns a description of what changed, for the log.
    """
    applied = []
    for key, value in load_config(path).items():
        constant = CONFIGURABLE[key][0]
        if globals().get(constant) != value:
            globals()[constant] = value
            applied.append(f"{key}={value}")
    return applied


# ── Vision Budget ────────────────────────────────────────────────────────────
def read_usage_today() -> int:
    """Tokens already spent on vision today. Zero if the file is absent or stale."""
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if data.get("date") != date.today().isoformat():
        return 0  # yesterday's total; the counter resets at midnight local time
    return int(data.get("tokens", 0))


def record_usage(tokens: int) -> None:
    """Add to today's running total. Never fatal — this is bookkeeping."""
    if tokens <= 0:
        return
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(
            json.dumps({"date": date.today().isoformat(),
                        "tokens": read_usage_today() + tokens}),
            encoding="utf-8",
        )
    except OSError:
        pass


@dataclass
class VisionPlan:
    """How many images to send, and why the rest are not being sent."""
    allowed: int = 0
    held: int = 0          # deferred pending confirmation; re-runnable
    dropped: int = 0       # refused outright by the cap or the daily budget
    reason: str = ""

    @property
    def estimated_tokens(self) -> int:
        return self.allowed * VISION_TOKENS_PER_IMAGE


def plan_vision_budget(count: int, vision_ok: bool, max_vision: int) -> VisionPlan:
    """
    Decide how much of a document's vision work to actually do.

    Three limits, most permissive first: the caller's own --max-vision, the hard
    cap that no single document may exceed, and what is left of the daily budget.
    Separately, anything above the warn threshold is *held* rather than dropped —
    the document still converts, and the caller can approve the rest.
    """
    if count <= 0:
        return VisionPlan()

    if count > VISION_WARN_THRESHOLD and not vision_ok:
        return VisionPlan(
            held=count,
            reason=(
                "%d images is above the confirmation threshold of %d "
                "(~%s tokens). Re-run with --vision-ok to describe them."
                % (count, VISION_WARN_THRESHOLD,
                   f"{count * VISION_TOKENS_PER_IMAGE:,}")
            ),
        )

    allowed = min(count, max_vision, VISION_HARD_CAP)
    reason = ""
    if allowed < count:
        reason = "capped at %d image(s) per document" % allowed

    remaining = VISION_DAILY_BUDGET - read_usage_today()
    affordable = max(0, remaining // VISION_TOKENS_PER_IMAGE)
    if affordable < allowed:
        allowed = affordable
        reason = (
            "only %s of the %s daily token budget is left, enough for %d image(s)"
            % (f"{max(0, remaining):,}", f"{VISION_DAILY_BUDGET:,}", affordable)
        )

    return VisionPlan(allowed=allowed, dropped=count - allowed, reason=reason)


def find_diagram_pages(input_path: Path, exclude: list[int]) -> list[int]:
    """
    Pages that are diagrams despite having a text layer.

    A vector-drawn infographic is never rasterized (it has text) and holds no
    embedded image (it is drawn, not placed), so without this it reaches neither
    OCR nor the classifier and its text comes out in coordinate order — scrambled
    across the page. See the thresholds above for the measurements behind this.
    """
    if input_path.suffix.lower() != ".pdf":
        return []
    try:
        doc = fitz.open(str(input_path))
    except Exception:
        return []

    skip = set(exclude)
    found = []
    for index, page in enumerate(doc):
        if index in skip:
            continue
        try:
            if len(page.get_images(full=True)) > PAGE_DIAGRAM_MAX_IMAGES:
                continue
            if len(page.get_drawings()) < PAGE_DIAGRAM_MIN_DRAWINGS:
                continue
            lines = [ln.strip() for ln in page.get_text().splitlines() if ln.strip()]
            if len(lines) < PAGE_DIAGRAM_MIN_LINES:
                continue
            if statistics.mean(len(ln) for ln in lines) <= PAGE_DIAGRAM_MAX_AVG_LINE:
                found.append(index)
        except Exception:
            continue
    doc.close()
    return found


# ── Vision Analysis ──────────────────────────────────────────────────────────
def analyze_with_vision(img_path: Path, model_name: str) -> tuple[str, int]:
    """
    Send an image to Gemini for semantic description.

    Returns the description and the tokens it actually cost. The reported total
    includes thinking tokens, which are roughly half the bill and appear nowhere
    else — estimating from prompt and output alone understates it by ~2x.
    """
    if not HAS_GENAI:
        logger.warning("google-genai SDK not installed. Skipping vision analysis.")
        return "", 0

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set. Skipping vision analysis.")
        return "", 0

    try:
        client = genai.Client(api_key=api_key)
        pil_img = Image.open(img_path)

        response = client.models.generate_content(
            model=model_name,
            contents=[VISION_PROMPT, pil_img],
        )
        usage = getattr(response, "usage_metadata", None)
        spent = getattr(usage, "total_token_count", 0) or 0
        return (response.text.strip() if response.text else ""), spent
    except Exception as e:
        logger.warning(f"Vision analysis failed for {img_path.name}: {e}")
        return "", 0


# ── Markdown Assembly ─────────────────────────────────────────────────────────
def build_image_reference(img_info: ImageInfo, images_dir_name: str) -> str:
    """
    Build the markdown block for a processed image.

    A discarded page render contributes its text but no image link, since the
    file it would point at has been deleted.
    """
    if img_info.render_discarded:
        # The render was only ever a carrier for this text. With no image beside
        # it there is nothing to annotate, so it becomes the body of the page —
        # blockquoting it would mark an entire scanned book as a quotation.
        if img_info.vision_description:
            return img_info.vision_description
        if img_info.ocr_markdown:
            return img_info.ocr_markdown
        # No geometry to rebuild from. One line per line is all that is left, and
        # Markdown will run them together — worse than the reflowed version, but
        # this is the Tesseract path, where the coordinates were never available.
        return "\n".join(
            line.strip() for line in img_info.ocr_text.splitlines() if line.strip()
        )

    rel_path = f"{images_dir_name}/{img_info.path.name}"
    lines = [f"![{img_info.path.stem}]({rel_path})"]

    if img_info.classification == "semantic" and img_info.vision_description:
        lines.append("")
        lines.append(img_info.vision_description)

    elif img_info.ocr_text:
        # Also covers an escalated image whose vision call came back empty —
        # the OCR text is worse than a description, but far better than nothing.
        lines.append("")
        lines.append("> **OCR Text:**")
        for ocr_line in img_info.ocr_text.splitlines():
            stripped = ocr_line.strip()
            if stripped:
                lines.append(f"> {stripped}")

    return "\n".join(lines)


def merge_images_into_markdown(
    base_markdown: str,
    images: list[ImageInfo],
    images_dir_name: str,
    section_title: str | None = "Extracted Images",
) -> str:
    """
    Merge image descriptions into the base markdown.

    Strategy:
    - If base markdown contains image references, try to match and augment them.
    - Append a section for any unmatched images. Pass section_title=None to omit
      the heading, which suits single-image documents where the image is not an
      appendix to anything.
    """
    # Find existing image references in the markdown
    img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    existing_refs = img_pattern.findall(base_markdown)

    # Track which images have been matched
    matched_images: set[int] = set()
    augmented_md = base_markdown

    # Try to match extracted images to existing references
    for alt, ref_path in existing_refs:
        ref_basename = Path(ref_path).stem.lower()
        for i, img in enumerate(images):
            if i in matched_images:
                continue
            img_basename = img.path.stem.lower()
            # Match by name similarity
            if ref_basename in img_basename or img_basename in ref_basename:
                if img.classification in ("text", "semantic") and (img.ocr_text or img.vision_description):
                    replacement = build_image_reference(img, images_dir_name)
                    old_ref = f"![{alt}]({ref_path})"
                    augmented_md = augmented_md.replace(old_ref, replacement, 1)
                    matched_images.add(i)
                    break

    # Append unmatched images that have content
    unmatched = [
        img for i, img in enumerate(images)
        if i not in matched_images and img.classification != "skip"
    ]

    if unmatched:
        sections = [f"\n\n---\n\n## {section_title}\n"] if section_title else ["\n"]
        for img in unmatched:
            block = build_image_reference(img, images_dir_name)
            sections.append(block)
            sections.append("")  # blank line between images

        augmented_md += "\n".join(sections)

    return augmented_md


def assemble_by_page(
    pages: list[PdfPageInfo],
    images: list[ImageInfo],
    images_dir_name: str,
) -> str:
    """
    Interleave each page's images directly after that page's text.

    This is only possible for PDFs routed through pdf-inspector, which is the
    one backend that reports per-page Markdown. MarkItDown emits a single flat
    string with no image references for PDFs, which is why every image used to
    land in a trailing appendix, detached from the text it belongs to.

    Images with no known page still go to the end, via the caller.
    """
    by_page: dict[int, list[ImageInfo]] = {}
    for img in images:
        if img.source_page is not None and img.classification != "skip":
            by_page.setdefault(img.source_page, []).append(img)

    sections: list[str] = []
    for page in pages:
        page_images = by_page.get(page.number, [])
        # A described diagram page supersedes its own text layer, which is the
        # scrambled coordinate-order extraction the description was made to fix.
        # If the description never arrived, the scrambled text is still better
        # than an empty page, so it stays.
        superseded = any(
            img.replaces_page_text and img.vision_description for img in page_images
        )
        if page.markdown.strip() and not superseded:
            sections.append(page.markdown.strip())
        for img in page_images:
            sections.append(build_image_reference(img, images_dir_name))
        # Links survive a supersede. The description replaces the scrambled text,
        # but it cannot invent a URL that was only ever a link annotation.
        if superseded and page.links:
            sections.append(
                "**Links:**\n" + "\n".join(
                    f"- [{a}]({u})" if a else f"- <{u}>" for a, u in page.links
                )
            )

    return "\n\n".join(sections)


# ── Page Count for Non-PDF ───────────────────────────────────────────────────
def estimate_page_count(input_path: Path, markdown_text: str) -> int:
    """Estimate page count for non-PDF formats."""
    suffix = input_path.suffix.lower()

    if suffix == ".pdf":
        try:
            doc = fitz.open(str(input_path))
            count = len(doc)
            doc.close()
            return count
        except Exception:
            return 0
    elif suffix == ".pptx":
        # Count slides by looking for slide XML entries
        try:
            with zipfile.ZipFile(str(input_path), "r") as zf:
                slides = [n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n)]
                return len(slides)
        except Exception:
            return 0
    else:
        # For DOCX, HTML, etc. — estimate from markdown length
        # ~3000 chars ≈ 1 page
        return max(1, len(markdown_text) // 3000)


# ── Table Normalisation ───────────────────────────────────────────────────────
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _row_cells(line: str) -> list[str]:
    """
    Split a Markdown table row into stripped cell values.

    Strips exactly one delimiting pipe from each end — str.strip("|") would eat
    consecutive empty cells at the edges, undercounting the row's real width.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _row_kind(line: str) -> str:
    """Classify a table row as 'separator', 'prose' (0-1 filled cells), or 'data'."""
    cells = _row_cells(line)
    if cells and all(_SEPARATOR_CELL.match(c) for c in cells if c):
        if not any(c and not _SEPARATOR_CELL.match(c) for c in cells):
            return "separator"
    return "prose" if len([c for c in cells if c]) <= 1 else "data"


def normalize_pdf_tables(markdown: str) -> str:
    """
    Undo spurious table markup around ordinary prose.

    pdf-inspector's column detector sometimes fires on a single-column page and
    wraps whole paragraphs in empty-celled table syntax (``||text||`` followed by
    a ``|---|---|---|`` separator). Those rows carry no tabular information and
    actively mislead an LLM reading the output.

    Rows with two or more filled cells are genuine table content and are kept —
    with a separator row guaranteed beneath the first of them, so the table still
    renders. Rows with one filled cell are unwrapped back into paragraphs.
    """
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        kinds = [_row_kind(line) for line in block]
        if "data" not in kinds:
            # Nothing tabular here at all — unwrap the lot.
            for line, kind in zip(block, kinds):
                if kind != "separator":
                    text = " ".join(c for c in _row_cells(line) if c).strip()
                    if text:
                        out.append(text)
        else:
            seen_data = False
            for line, kind in zip(block, kinds):
                if kind == "data":
                    out.append(line)
                    if not seen_data:
                        seen_data = True
                        out.append("|" + "|".join(["---"] * len(_row_cells(line))) + "|")
                elif kind == "prose":
                    text = " ".join(c for c in _row_cells(line) if c).strip()
                    if text:
                        out.append("" if out and out[-1] else "")
                        out.append(text)
                # separators are re-emitted above, so the originals are dropped
        block.clear()

    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()

    return "\n".join(out)


# ── PDF Metadata ──────────────────────────────────────────────────────────────
def usable_title(title: str | None) -> str:
    """
    Keep a declared PDF title only when it is worth putting at the top.

    Plenty of PDFs carry a useless one: the authoring tool's default, or the
    original filename with its extension still attached. Those are rejected,
    because a wrong title is worse than none — it is the first line the model
    reads.
    """
    title = (title or "").strip()
    if len(title) < 3 or len(title) > 200:
        return ""
    lowered = title.lower()
    if lowered.endswith((".pdf", ".doc", ".docx", ".indd", ".ai", ".qxd")):
        return ""
    # Authoring-tool defaults, and titles that are just a bare number.
    if lowered in {"untitled", "document", "microsoft word document", "pdf document"}:
        return ""
    if re.fullmatch(r"[\d\s.\-_]+", title):
        return ""
    return title


def read_pdf_title(input_path: Path) -> str:
    """
    Fallback title reader, for PDFs that never reach pdf-inspector.

    pdf-inspector's detect_pdf() supplies the title on the normal path and is
    preferred: it is MIT where PyMuPDF is AGPL, and it is one call that also
    yields the document type. This exists for the MarkItDown fallback, when the
    native wheel is missing or has failed.
    """
    try:
        with fitz.open(str(input_path)) as doc:
            return usable_title((doc.metadata or {}).get("title"))
    except Exception:
        return ""


def read_pdf_links(input_path: Path) -> dict[int, list[tuple[str, str]]]:
    """
    Harvest hyperlink annotations, keyed by 0-based page.

    A URL that lives in a link annotation rather than in the text is invisible to
    every text extractor, pdf-inspector included — its "URL linking" covers URLs
    written out in the text. So a page reading "schedule a free consultation"
    loses the address entirely, which for a tool whose output is model context is
    a real loss rather than a cosmetic one.

    The text under the link rectangle becomes the anchor, so the result can be a
    proper Markdown link instead of a bare URL with no indication of what it was
    attached to.
    """
    links: dict[int, list[tuple[str, str]]] = {}
    try:
        with fitz.open(str(input_path)) as doc:
            for index, page in enumerate(doc):
                seen: set[str] = set()
                for link in page.get_links():
                    uri = (link.get("uri") or "").strip()
                    if not uri or uri in seen:
                        continue
                    seen.add(uri)
                    anchor = ""
                    try:
                        rect = link.get("from")
                        if rect is not None:
                            anchor = usable_anchor(
                                " ".join(page.get_text("text", clip=rect).split())
                            )
                    except Exception:
                        anchor = ""
                    links.setdefault(index, []).append((anchor, uri))
    except Exception:
        return {}
    return links


def usable_anchor(text: str) -> str:
    """
    Keep link anchor text only when it reads as language.

    The text under a link rectangle comes from the same layer as everything else,
    so on a page whose text is scrambled the anchor is scrambled too — one sample
    yielded "Every organizat q Schedule a free consultation d oach to disc".
    Stray single letters are the giveaway, since real prose has almost none
    beyond "a" and "I". A bare URL beats a corrupted label.
    """
    text = text.strip()
    if not text or len(text) > 80:
        return ""
    tokens = text.split()
    stray = sum(1 for t in tokens if len(t) == 1 and t.lower() not in {"a", "i"})
    return "" if stray >= 2 else text


def merge_links_into_page(markdown: str, links: list[tuple[str, str]]) -> str:
    """
    Attach a page's links to its Markdown.

    Preferred form is an inline Markdown link, but only when the anchor text
    appears exactly once on the page — substituting into ambiguous text would
    silently corrupt it. Everything else is appended as a short list, which keeps
    the address rather than guessing where it belonged.
    """
    if not links:
        return markdown

    leftovers = []
    for anchor, uri in links:
        if anchor and markdown.count(anchor) == 1 and f"]({uri})" not in markdown:
            markdown = markdown.replace(anchor, f"[{anchor}]({uri})", 1)
        else:
            leftovers.append(f"- [{anchor}]({uri})" if anchor else f"- <{uri}>")

    if leftovers:
        markdown = markdown.rstrip() + "\n\n**Links:**\n" + "\n".join(leftovers)
    return markdown


# ── Document Routing ──────────────────────────────────────────────────────────
def _pdf_via_inspector(input_path: Path, report: ProcessingReport) -> DocumentSource:
    """
    Extract per-page Markdown with pdf-inspector.

    pdf-inspector is inconsistent about page indexing — ``PageMarkdown.page`` is
    0-based while the summary lists are 1-based — so page numbers are normalised
    to 0-based here, matching ImageInfo.source_page, and nowhere else.
    """
    # Pre-flight. detect_pdf samples content streams instead of extracting — ~7 ms,
    # and its markdown comes back empty by design — yet returns the full result
    # object, so one call covers the document type, the OCR forecast *and* the
    # declared title. Preferred over reading the title from PyMuPDF: same answer
    # on every sample tested, one fewer parse, and MIT rather than AGPL.
    title = ""
    try:
        summary = pdf_inspector.detect_pdf(str(input_path))
        title = usable_title(summary.title)
        logger.info(
            "  %s PDF, %d page(s), confidence %.2f%s%s"
            % (summary.pdf_type, summary.page_count, summary.confidence,
               f", {len(summary.pages_needing_ocr)} needing OCR"
               if summary.pages_needing_ocr else "",
               f" — {title!r}" if title else "")
        )
    except Exception:
        pass  # advisory only; extraction below is what actually matters

    result = pdf_inspector.extract_pages_markdown(str(input_path))
    links = read_pdf_links(input_path)

    pages = [
        PdfPageInfo(
            number=page.page,
            markdown=merge_links_into_page(
                dehyphenate(normalize_pdf_tables(page.markdown or "")),
                links.get(page.page, []),
            ),
            needs_ocr=bool(page.needs_ocr),
            ocr_reason=(getattr(page, "ocr_reason", "") or ""),
            links=links.get(page.page, []),
        )
        for page in result.pages
    ]

    if getattr(result, "pages_with_tables", None):
        logger.info(f"  Tables detected on page(s): {result.pages_with_tables}")
    if getattr(result, "pages_with_columns", None):
        logger.info(f"  Multi-column layout on page(s): {result.pages_with_columns}")

    stripped = strip_text_layer_furniture(pages)
    if stripped:
        logger.info(f"  {stripped} running header/footer line(s) removed")

    found_links = sum(len(v) for v in links.values())
    if found_links:
        logger.info(f"  {found_links} hyperlink(s) recovered from link annotations")

    # Group by reason rather than lumping every page under "no extractable text":
    # a page flagged suspected_garbled_text *has* a text layer, it is just broken,
    # and saying otherwise sends you looking for the wrong problem.
    by_reason: dict[str, list[int]] = {}
    for page in pages:
        if page.needs_ocr:
            by_reason.setdefault(page.ocr_reason or "no_text_layer", []).append(
                page.number + 1
            )
    for reason, numbers in by_reason.items():
        logger.info(f"  Page(s) to OCR — {OCR_REASON_TEXT.get(reason, reason)}: {numbers}")
        if reason == "suspected_garbled_text":
            report.warnings.append(
                f"Broken font encoding on page(s) {numbers}; "
                "text was unreadable so the page was OCR'd instead."
            )

    return DocumentSource(
        markdown="\n\n".join(p.markdown for p in pages if p.markdown.strip()),
        pages=pages,
        page_count=len(pages),
        title=title,
    )


def _eml_to_markdown(input_path: Path) -> str:
    """
    Convert an RFC-822 email to Markdown: a header block, then the body.

    Uses only the standard library. MarkItDown handles Outlook's .msg but not
    the .eml format that every other mail client exports.
    """
    from email import policy
    from email.parser import BytesParser

    with input_path.open("rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)

    lines = [f"# {msg.get('Subject') or input_path.stem}", ""]
    for header in ("From", "To", "Cc", "Date"):
        value = msg.get(header)
        if value:
            lines.append(f"**{header}:** {value}  ")
    lines.append("")

    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            try:
                from markdownify import markdownify

                content = markdownify(content)
            except ImportError:
                content = re.sub(r"<[^>]+>", "", content)
        lines.append(content.strip())

    attachments = [
        part.get_filename()
        for part in msg.iter_attachments()
        if part.get_filename()
    ]
    if attachments:
        lines += ["", "**Attachments:** " + ", ".join(attachments)]

    return "\n".join(lines)


def route_document(input_path: Path, report: ProcessingReport) -> DocumentSource | None:
    """
    Dispatch a document to the right text-extraction backend.

        image  → no text layer; the OCR and vision stages supply the content
        PDF    → pdf-inspector, which is layout-aware and reports per-page state
        other  → MarkItDown

    Falls back to MarkItDown if pdf-inspector is unavailable or fails, so a
    missing native wheel degrades rather than breaking the most common format.

    Returns None if extraction failed outright; a warning is recorded first.
    """
    if is_image_input(input_path):
        return DocumentSource(markdown=f"# {input_path.stem}\n", page_count=1)

    if input_path.suffix.lower() == ".eml":
        try:
            return DocumentSource(markdown=_eml_to_markdown(input_path), page_count=1)
        except Exception as e:
            report.warnings.append(f"Email parsing failed: {e}")
            return None

    if input_path.suffix.lower() == ".pdf":
        if HAS_PDF_INSPECTOR:
            try:
                return _pdf_via_inspector(input_path, report)
            except Exception as e:
                report.warnings.append(
                    f"pdf-inspector failed, falling back to MarkItDown: {e}"
                )
        else:
            logger.info("  pdf-inspector not installed; using MarkItDown for PDF.")

    try:
        return DocumentSource(markdown=MarkItDown().convert(str(input_path)).text_content)
    except Exception as e:
        report.warnings.append(f"MarkItDown conversion failed: {e}")
        return None


# ── Output Layout ─────────────────────────────────────────────────────────────
@dataclass
class OutputPaths:
    """Resolved destinations for one conversion's Markdown, images, and report."""
    markdown: Path
    images_dir: Path
    report: Path
    # Where to record which document produced this folder, or None when the
    # caller chose the location with --output and the folder is not ours to mark.
    source_marker: Path | None = None


def claim_output_folder(root: Path, input_path: Path) -> Path:
    """
    Pick the output folder for a document, keeping distinct documents apart.

    Naming the folder after the stem alone means report.pdf and report.docx both
    resolve to root/report/report.md, and the second conversion silently
    overwrites the first. Adding the extension unconditionally would rename every
    folder for a collision that usually never happens, so the extension is
    appended only once a different document has actually claimed the plain name.

    Which document owns a folder is recorded in a marker file inside it, so
    re-converting the *same* document keeps overwriting in place — the folder is
    stable across runs and does not accumulate report-2, report-3.

    Folders from before the marker existed have none, and are reused as they
    always were; they get a marker the next time they are written to.
    """
    stem = input_path.stem
    ext = input_path.suffix.lstrip(".").lower() or "file"

    candidates = [stem, f"{stem}-{ext}"]
    candidates += [f"{stem}-{ext}-{n}" for n in range(2, 100)]

    for name in candidates:
        folder = root / name
        marker = folder / SOURCE_MARKER
        if not folder.exists():
            return folder
        try:
            # Only the trailing newline is stripped. Leading and trailing spaces
            # are legal in macOS filenames and some documents really do have them,
            # so .strip() here would stop a file matching its own marker and mint
            # a new folder on every run.
            if marker.read_text(encoding="utf-8").rstrip("\n") == input_path.name:
                return folder  # same document again → overwrite in place
        except OSError:
            return folder  # unmarked or unreadable: pre-existing folder, reuse it

    return root / f"{stem}-{ext}"


def resolve_output_paths(input_path: Path, output: Path | None) -> OutputPaths:
    """
    Decide where this run's artefacts go.

    Default — a folder named after the document, under DEFAULT_OUTPUT_DIR:

        ~/Downloads/doc2md/annual-report/
            annual-report.md
            images/
            report.txt

    ``--output DIR``  uses DIR with that same layout.
    ``--output FILE`` writes FILE, with siblings beside it prefixed by the
    document name so that several documents can share one directory.
    """
    stem = input_path.stem

    if output is None:
        folder = claim_output_folder(DEFAULT_OUTPUT_DIR, input_path)
        return OutputPaths(
            markdown=folder / f"{stem}.md",
            images_dir=folder / "images",
            report=folder / "report.txt",
            source_marker=folder / SOURCE_MARKER,
        )
    else:
        output = Path(output).resolve()
        if output.suffix.lower() != ".md":
            folder = output  # treat as a directory
        else:
            return OutputPaths(
                markdown=output,
                images_dir=output.parent / f"{stem}_images",
                report=output.parent / f"{stem}_report.txt",
            )

    return OutputPaths(
        markdown=folder / f"{stem}.md",
        images_dir=folder / "images",
        report=folder / "report.txt",
    )


# ── Main Pipeline ─────────────────────────────────────────────────────────────
def convert_document(
    input_path: Path,
    output_path: Path | None = None,
    model_name: str = VISION_MODEL,
    no_vision: bool = False,
    keep_page_scans: bool = False,
    vision_ok: bool = False,
    max_vision: int = VISION_HARD_CAP,
) -> tuple[Path, ProcessingReport]:
    """
    Main conversion pipeline.

    Returns:
        Tuple of (output_markdown_path, processing_report)
    """
    report = ProcessingReport(input_file=str(input_path))
    report.start_time = time.time()

    paths = resolve_output_paths(input_path, output_path)
    output_path, images_dir, report_path = paths.markdown, paths.images_dir, paths.report
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if paths.source_marker is not None:
        # Claim the folder before any work, so a crash midway still leaves it
        # attributed to this document rather than free for the next one to take.
        try:
            paths.source_marker.write_text(input_path.name, encoding="utf-8")
        except OSError:
            pass  # marker is an optimisation; a read-only folder still converts
    images_dir_name = images_dir.name

    logger.info(f"Converting: {input_path.name}")
    logger.info(f"Output:     {output_path.name}")

    # ── Step 1: Extract the text layer ───────────────────────────────────
    logger.info("Step 1/6: Converting document to Markdown...")
    source = route_document(input_path, report)
    if source is None:
        report.end_time = time.time()
        return output_path, report
    base_markdown = source.markdown
    if source.page_count:
        report.pages_processed = source.page_count

    # ── Step 2: Extract embedded images ──────────────────────────────────
    logger.info("Step 2/6: Extracting embedded images...")
    images_dir.mkdir(parents=True, exist_ok=True)
    images = extract_images(input_path, images_dir, report)

    # Pages with no extractable text are rasterized so OCR can reach them.
    # Pages that already yielded text are never rendered — that is the whole
    # point of asking pdf-inspector first.
    ocr_pages = [p.number for p in source.pages if p.needs_ocr]
    if ocr_pages:
        images = discard_images_on_rendered_pages(images, ocr_pages)
        logger.info(f"  Rendering {len(ocr_pages)} page(s) for OCR at {PDF_OCR_RENDER_DPI} dpi...")
        images.extend(render_pdf_pages(input_path, ocr_pages, images_dir, report))

    # Pages that are diagrams despite carrying a text layer. Rendered and marked
    # semantic so they go straight to vision: OCR would only reproduce the same
    # scrambled fragments the text layer already gives.
    if not no_vision:
        diagram_pages = find_diagram_pages(input_path, ocr_pages)
        if diagram_pages:
            pretty = ", ".join(str(n + 1) for n in diagram_pages)
            logger.info(f"  Page(s) drawn as diagrams, routing to vision: {pretty}")
            images = discard_images_on_rendered_pages(images, diagram_pages)
            images.extend(render_pdf_pages(
                input_path, diagram_pages, images_dir, report,
                classification="semantic", replaces_page_text=True,
            ))

    report.images_detected = len(images)

    if not images:
        logger.info("  No images found.")

    # Update page count if not set by extractor
    if report.pages_processed == 0:
        report.pages_processed = estimate_page_count(input_path, base_markdown)

    # ── Step 3: Classify images ──────────────────────────────────────────
    logger.info("Step 3/6: Classifying images...")
    for img in images:
        # Rendered OCR pages arrive pre-classified; re-deriving would risk
        # mislabelling a photographed page as "photo" and dropping its text.
        if img.classification == "unknown":
            img.classification = classify_image(img)
        # A directly supplied image *is* the document — never discard it as an icon
        if img.classification == "skip" and is_image_input(input_path):
            img.classification = "text"
        logger.info(f"  {img.path.name}: {img.classification} ({img.width}×{img.height})")

        if img.classification == "skip":
            report.images_skipped += 1
        elif img.classification == "photo":
            report.images_photo += 1

    # ── Step 4: OCR text images ──────────────────────────────────────────
    text_images = [img for img in images if img.classification == "text"]
    if text_images:
        logger.info(
            f"Step 4/6: Running OCR on {len(text_images)} image(s) "
            f"across {min(OCR_MAX_WORKERS, len(text_images))} worker(s)..."
        )
        run_ocr_batch(text_images)
        for img in text_images:
            if img.ocr_text:
                report.images_ocr += 1
                logger.info(f"  {img.path.name}: extracted {len(img.ocr_text)} chars")
            else:
                logger.info(f"  {img.path.name}: no text detected")
    else:
        logger.info("Step 4/6: No images need OCR.")

    # Images whose OCR came back as scattered labels are diagrams, not text.
    # This runs after OCR so the decision is free when OCR already did the job.
    if not no_vision:
        for img in images:
            if should_escalate_to_vision(img):
                img.classification = "semantic"
                report.images_ocr = max(0, report.images_ocr - 1)
                logger.info(
                    f"  {img.path.name}: OCR fragmented "
                    f"(edge={img.edge_density:.4f}) → routing to vision"
                )

    # Rebuild paragraphs, headings and lists from the OCR line geometry, and drop
    # running headers and footers. Deliberately after escalation: that decision
    # reads the flat one-line-per-line text, and reflowing first would make every
    # page look like prose and stop any diagram ever reaching the vision model.
    laid_out = [img for img in images if img.layout_lines]
    if laid_out:
        stripped = apply_layout(laid_out)
        logger.info(
            f"  Layout rebuilt for {len(laid_out)} page(s)"
            + (f"; {stripped} running header/footer line(s) removed" if stripped else "")
        )

    # ── Step 5: Vision analysis for semantic images ──────────────────────
    semantic_images = [img for img in images if img.classification == "semantic"]
    if semantic_images and not no_vision:
        plan = plan_vision_budget(len(semantic_images), vision_ok, max_vision)
        spent_today = read_usage_today()

        # Say what this will cost *before* spending it, every time.
        logger.info(
            "Step 5/6: %d semantic image(s). Sending %d → ~%s tokens "
            "(%.0f%% of the %s daily budget; %s already used today)."
            % (len(semantic_images), plan.allowed, f"{plan.estimated_tokens:,}",
               100.0 * plan.estimated_tokens / VISION_DAILY_BUDGET,
               f"{VISION_DAILY_BUDGET:,}", f"{spent_today:,}")
        )
        if plan.reason:
            logger.info(f"  {plan.reason}")

        if plan.held:
            report.vision_held = plan.held
            report.warnings.append(f"Vision held for {plan.held} image(s): {plan.reason}")
        if plan.dropped:
            report.warnings.append(
                f"Vision skipped for {plan.dropped} image(s): {plan.reason}"
            )

        for img in semantic_images[:plan.allowed]:
            img.vision_description, spent = analyze_with_vision(img.path, model_name)
            report.vision_tokens += spent
            if img.vision_description:
                report.images_ai += 1
                logger.info(f"  {img.path.name}: generated description ({len(img.vision_description)} chars, {spent:,} tokens)")
            else:
                logger.info(f"  {img.path.name}: vision analysis returned empty")
                report.warnings.append(f"Vision analysis empty for {img.path.name}")

        record_usage(report.vision_tokens)
        if report.vision_tokens:
            logger.info(
                "  Vision spend: %s tokens. Today: %s of %s (%.0f%%)."
                % (f"{report.vision_tokens:,}",
                   f"{read_usage_today():,}", f"{VISION_DAILY_BUDGET:,}",
                   100.0 * read_usage_today() / VISION_DAILY_BUDGET)
            )
    elif semantic_images and no_vision:
        logger.info(f"Step 5/6: Skipping vision analysis (--no-vision flag). {len(semantic_images)} image(s) skipped.")
        for img in semantic_images:
            report.warnings.append(f"Semantic image skipped (--no-vision): {img.path.name}")
    else:
        logger.info("Step 5/6: No semantic images to analyze.")

    # Rasterized pages have served their purpose once OCR and vision have read
    # them; keeping them would store the whole document twice.
    if not keep_page_scans:
        freed = discard_page_renders(images)
        if freed:
            logger.info(f"  Discarded {freed} page render(s) after OCR")

    # ── Step 6: Assemble final Markdown ──────────────────────────────────
    logger.info("Step 6/6: Assembling final Markdown...")
    if source.title:
        report.title = source.title
    if source.pages:
        # Per-page Markdown lets images sit with the text they belong to.
        final_markdown = assemble_by_page(source.pages, images, images_dir_name)
        placed = {id(i) for p in source.pages for i in images if i.source_page == p.number}
        leftovers = [i for i in images if id(i) not in placed]
        if leftovers:
            final_markdown = merge_images_into_markdown(
                final_markdown, leftovers, images_dir_name
            )
    else:
        final_markdown = merge_images_into_markdown(
            base_markdown,
            images,
            images_dir_name,
            section_title=None if is_image_input(input_path) else "Extracted Images",
        )

    # Write output
    # A document that declares a title but whose text opens with no heading gets
    # one, so the first thing a model reads says what it is looking at. When the
    # text already leads with a heading, that heading is the better one — it came
    # from the page, not from metadata that may be a stale authoring artefact.
    if source.title and not re.match(r"\s*#\s", final_markdown):
        final_markdown = f"# {source.title}\n\n{final_markdown}"

    output_path.write_text(final_markdown, encoding="utf-8")
    logger.info(f"  Saved: {output_path}")

    # Write report
    report.end_time = time.time()
    report_text = report.render()
    report_path.write_text(report_text, encoding="utf-8")
    logger.info(f"  Report: {report_path}")

    # Clean up empty images directory
    if images_dir.exists() and not any(images_dir.iterdir()):
        try:
            images_dir.rmdir()
        except OSError:
            pass

    return output_path, report


# ── CLI Entry Point ───────────────────────────────────────────────────────────
def main():
    # Before the parser is built: several --help strings and defaults quote these
    # values, so the settings file has to be in effect by then.
    config_notes = apply_config()

    parser = argparse.ArgumentParser(
        description="Convert documents to clean, LLM-friendly Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python doc2md.py report.pdf\n"
            "  python doc2md.py slides.pptx --output notes.md\n"
            "  python doc2md.py paper.pdf --model gemini-3.6-flash --verbose\n"
            "  python doc2md.py manual.docx --no-vision\n"
        ),
    )
    parser.add_argument("input_file", help="Path to the document to convert")
    parser.add_argument("--output", "-o", help=f"Output Markdown file path (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--model", "-m", default=VISION_MODEL, help=f"Vision model name (default: {VISION_MODEL})")
    parser.add_argument("--no-vision", action="store_true", help="Skip AI vision analysis for diagrams")
    parser.add_argument("--keep-page-scans", action="store_true",
                        help="Keep rasterized PDF pages (deleted after OCR by default)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--vision-ok", action="store_true",
                        help=f"Approve vision work above the {VISION_WARN_THRESHOLD}-image confirmation threshold")
    parser.add_argument("--max-vision", type=int, default=VISION_HARD_CAP, metavar="N",
                        help=f"Cap vision calls for this run (hard limit {VISION_HARD_CAP})")

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stderr,
    )
    # Silence third-party noisy loggers
    for noisy_logger in ["pdfminer", "fitz", "PIL", "urllib3", "google", "httpx", "httpcore"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    if config_notes:
        logger.debug(f"Settings from {CONFIG_FILE}: {', '.join(config_notes)}")

    # Validate input
    input_path = Path(args.input_file).resolve()
    if not input_path.exists():
        logger.error(f"Error: File not found: {input_path}")
        sys.exit(1)

    output_path = Path(args.output).resolve() if args.output else None

    # Run pipeline
    _, report = convert_document(
        input_path=input_path,
        output_path=output_path,
        model_name=args.model,
        no_vision=args.no_vision,
        keep_page_scans=args.keep_page_scans,
        vision_ok=args.vision_ok,
        max_vision=args.max_vision,
    )

    # Print report
    print()
    print(report.render())


if __name__ == "__main__":
    main()
