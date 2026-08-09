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

# Records which document an output folder belongs to, so that two files sharing a
# stem (report.pdf, report.docx) do not overwrite each other while re-converting
# the same file still overwrites in place. See claim_output_folder().
SOURCE_MARKER = ".doc2md-source"

# Tesseract default languages (fast first pass)
OCR_DEFAULT_LANGS = "eng+spa"

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


@dataclass
class PdfPageInfo:
    """One page's extracted Markdown, plus whether it still needs OCR."""
    number: int          # 0-indexed, normalised on ingest
    markdown: str = ""
    needs_ocr: bool = False


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
    if not images:
        return
    if len(images) == 1:
        images[0].ocr_text = ocr_image(images[0].path)
        return

    workers = min(OCR_MAX_WORKERS, len(images))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for img, text in zip(images, pool.map(lambda i: ocr_image(i.path), images)):
            img.ocr_text = text


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



def ocr_image(img_path: Path) -> str:
    """
    Run Tesseract OCR with auto language detection.
    1. First pass with eng+spa
    2. Detect language with langdetect
    3. Re-run with detected language if different
    """
    try:
        pil_img = upscale_for_ocr(Image.open(img_path))
    except Exception as e:
        logger.warning(f"Cannot open image for OCR: {img_path.name} ({e})")
        return ""

    available_langs = available_ocr_languages()

    # Build initial language string from what's available
    initial_langs = []
    for lang in OCR_DEFAULT_LANGS.split("+"):
        if lang in available_langs:
            initial_langs.append(lang)
    if not initial_langs:
        initial_langs = ["eng"]
    lang_str = "+".join(initial_langs)

    # First pass
    try:
        text = pytesseract.image_to_string(pil_img, lang=lang_str).strip()
    except Exception as e:
        logger.warning(f"OCR failed for {img_path.name}: {e}")
        return ""

    if not text or len(text) < 5:
        return text

    # Auto-detect language and re-run if needed
    if HAS_LANGDETECT:
        try:
            detected = langdetect_detect(text)
            tess_lang = LANG_MAP.get(detected)
            if tess_lang and tess_lang not in initial_langs and tess_lang in available_langs:
                logger.info(f"  Re-running OCR with detected language: {detected} → {tess_lang}")
                text = pytesseract.image_to_string(pil_img, lang=tess_lang).strip()
        except Exception:
            pass  # langdetect can fail on short or ambiguous text

    return text


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


# ── Document Routing ──────────────────────────────────────────────────────────
def _pdf_via_inspector(input_path: Path, report: ProcessingReport) -> DocumentSource:
    """
    Extract per-page Markdown with pdf-inspector.

    pdf-inspector is inconsistent about page indexing — ``PageMarkdown.page`` is
    0-based while the summary lists are 1-based — so page numbers are normalised
    to 0-based here, matching ImageInfo.source_page, and nowhere else.
    """
    result = pdf_inspector.extract_pages_markdown(str(input_path))

    pages = [
        PdfPageInfo(
            number=page.page,
            markdown=normalize_pdf_tables(page.markdown or ""),
            needs_ocr=bool(page.needs_ocr),
        )
        for page in result.pages
    ]

    if getattr(result, "pages_with_tables", None):
        logger.info(f"  Tables detected on page(s): {result.pages_with_tables}")
    if getattr(result, "pages_with_columns", None):
        logger.info(f"  Multi-column layout on page(s): {result.pages_with_columns}")

    needing_ocr = [p.number + 1 for p in pages if p.needs_ocr]
    if needing_ocr:
        logger.info(f"  Page(s) with no extractable text, will be OCR'd: {needing_ocr}")

    return DocumentSource(
        markdown="\n\n".join(p.markdown for p in pages if p.markdown.strip()),
        pages=pages,
        page_count=len(pages),
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
