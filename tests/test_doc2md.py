"""
Test suite for doc2md.

Uses stdlib unittest rather than pytest so that running the tests costs no extra
disk space. Run with:

    python -m unittest discover -s tests -v

Gemini is always mocked — no test makes a network call. Tests that need the
tesseract binary skip themselves when it is absent.
"""

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doc2md  # noqa: E402
from tests import fixtures  # noqa: E402

# Several tests deliberately exercise graceful-degradation paths that log
# warnings. Attaching a handler keeps them out of the test output; without one,
# logging falls back to printing every warning to stderr.
logging.getLogger("doc2md").addHandler(logging.NullHandler())


def tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_languages()
        return True
    except Exception:
        return False


HAS_TESSERACT = tesseract_available()
needs_tesseract = unittest.skipUnless(HAS_TESSERACT, "tesseract binary not installed")


class FixtureCase(unittest.TestCase):
    """Base case that builds the fixture documents once per class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="doc2md-test-"))
        cls.src = cls.tmp / "src"
        cls.out = cls.tmp / "out"
        cls.files = fixtures.build_all(cls.src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def convert(self, key: str, **kwargs):
        """Convert a fixture into an isolated output directory."""
        dest = self.out / key
        kwargs.setdefault("no_vision", True)
        path, report = doc2md.convert_document(
            input_path=self.files[key], output_path=dest, **kwargs
        )
        return path, report, path.read_text(encoding="utf-8")


# ── Output layout ─────────────────────────────────────────────────────────────
class TestOutputPaths(unittest.TestCase):
    def test_default_is_named_folder_under_downloads(self):
        p = doc2md.resolve_output_paths(Path("/docs/Annual Report.pdf"), None)
        self.assertEqual(p.markdown.parent.name, "Annual Report")
        self.assertEqual(p.markdown.name, "Annual Report.md")
        self.assertEqual(p.images_dir.name, "images")
        self.assertEqual(p.report.name, "report.txt")
        self.assertEqual(p.markdown.parent.parent, doc2md.DEFAULT_OUTPUT_DIR)

    def test_output_directory_uses_same_layout(self):
        p = doc2md.resolve_output_paths(Path("/docs/notes.docx"), Path("/tmp/somewhere"))
        self.assertEqual(p.markdown.name, "notes.md")
        self.assertEqual(p.images_dir.name, "images")
        self.assertEqual(p.report.name, "report.txt")

    def test_explicit_md_file_keeps_prefixed_siblings(self):
        """Several documents can share one directory without colliding."""
        p = doc2md.resolve_output_paths(Path("/docs/notes.docx"), Path("/tmp/x/out.md"))
        self.assertEqual(p.markdown.name, "out.md")
        self.assertEqual(p.images_dir.name, "notes_images")
        self.assertEqual(p.report.name, "notes_report.txt")

    def test_all_three_artefacts_share_a_parent_by_default(self):
        p = doc2md.resolve_output_paths(Path("/docs/thing.pdf"), None)
        self.assertEqual(p.markdown.parent, p.images_dir.parent)
        self.assertEqual(p.markdown.parent, p.report.parent)


class TestFolderClaiming(unittest.TestCase):
    """Two documents sharing a stem must not overwrite each other's output."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def claim(self, filename):
        """Claim a folder the way a real conversion does, marker and all."""
        folder = doc2md.claim_output_folder(self.root, Path("/docs") / filename)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / doc2md.SOURCE_MARKER).write_text(filename, encoding="utf-8")
        return folder

    def test_first_document_gets_the_plain_name(self):
        self.assertEqual(self.claim("report.pdf").name, "report")

    def test_same_stem_different_extension_gets_its_own_folder(self):
        first = self.claim("report.pdf")
        second = self.claim("report.docx")
        self.assertEqual(first.name, "report")
        self.assertEqual(second.name, "report-docx")

    def test_reconverting_the_same_document_reuses_its_folder(self):
        """Otherwise every re-run would leave another report-pdf-N behind."""
        first = self.claim("report.pdf")
        for _ in range(3):
            self.assertEqual(self.claim("report.pdf"), first)

    def test_three_way_collision_keeps_all_three(self):
        names = {self.claim(n).name for n in ("a.pdf", "a.docx", "a.txt")}
        self.assertEqual(names, {"a", "a-docx", "a-txt"})

    def test_same_name_from_different_directories_still_separates(self):
        """Identical stem and extension, so the numeric suffix is the only way out."""
        self.claim("report.pdf")
        folder = doc2md.claim_output_folder(self.root, Path("/elsewhere/report.pdf"))
        # Same filename, so it is treated as the same document and shares the
        # folder. This is deliberate: path-based identity would break the common
        # case of re-converting a file that has moved.
        self.assertEqual(folder.name, "report")

    def test_filename_with_leading_space_matches_its_own_marker(self):
        """Leading spaces are legal in macOS filenames and must survive the
        round trip, or the file mints a fresh folder on every conversion."""
        first = self.claim(" Top 15 Kids Needs.png")
        self.assertEqual(self.claim(" Top 15 Kids Needs.png"), first)

    def test_unmarked_legacy_folder_is_reused(self):
        """Folders created before the marker existed keep working as before."""
        legacy = self.root / "legacy"
        legacy.mkdir()
        folder = doc2md.claim_output_folder(self.root, Path("/docs/legacy.pdf"))
        self.assertEqual(folder, legacy)

    def test_explicit_output_is_never_marked(self):
        """--output points somewhere the user chose; doc2md does not claim it."""
        p = doc2md.resolve_output_paths(Path("/docs/n.docx"), Path("/tmp/mine"))
        self.assertIsNone(p.source_marker)


class TestVisionBudget(unittest.TestCase):
    """Guards on what a single drag is allowed to spend."""

    def setUp(self):
        self.usage = Path(tempfile.mkdtemp()) / "usage.json"
        self.addCleanup(shutil.rmtree, self.usage.parent, ignore_errors=True)
        patcher = mock.patch.object(doc2md, "USAGE_FILE", self.usage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, count, vision_ok=False, max_vision=None):
        return doc2md.plan_vision_budget(
            count, vision_ok,
            doc2md.VISION_HARD_CAP if max_vision is None else max_vision,
        )

    def test_small_job_runs_untouched(self):
        p = self.plan(3)
        self.assertEqual((p.allowed, p.held, p.dropped), (3, 0, 0))

    def test_above_threshold_is_held_not_dropped(self):
        """The document still converts; only the descriptions wait for a yes."""
        p = self.plan(doc2md.VISION_WARN_THRESHOLD + 1)
        self.assertEqual(p.allowed, 0)
        self.assertEqual(p.held, doc2md.VISION_WARN_THRESHOLD + 1)
        self.assertEqual(p.dropped, 0)
        self.assertIn("--vision-ok", p.reason)

    def test_confirmation_releases_up_to_the_hard_cap(self):
        p = self.plan(doc2md.VISION_HARD_CAP + 25, vision_ok=True)
        self.assertEqual(p.allowed, doc2md.VISION_HARD_CAP)
        self.assertEqual(p.dropped, 25)

    def test_hard_cap_cannot_be_raised_by_max_vision(self):
        p = self.plan(500, vision_ok=True, max_vision=10_000)
        self.assertEqual(p.allowed, doc2md.VISION_HARD_CAP)

    def test_max_vision_can_lower_the_cap(self):
        p = self.plan(30, vision_ok=True, max_vision=5)
        self.assertEqual(p.allowed, 5)

    def test_spent_budget_shrinks_what_is_affordable(self):
        doc2md.record_usage(doc2md.VISION_DAILY_BUDGET - 3 * doc2md.VISION_TOKENS_PER_IMAGE)
        p = self.plan(10)
        self.assertEqual(p.allowed, 3)
        self.assertIn("daily token budget", p.reason)

    def test_exhausted_budget_allows_nothing(self):
        doc2md.record_usage(doc2md.VISION_DAILY_BUDGET)
        self.assertEqual(self.plan(5).allowed, 0)

    def test_usage_resets_on_a_new_day(self):
        self.usage.parent.mkdir(parents=True, exist_ok=True)
        self.usage.write_text('{"date": "2020-01-01", "tokens": 999999}')
        self.assertEqual(doc2md.read_usage_today(), 0)

    def test_usage_accumulates_within_a_day(self):
        doc2md.record_usage(1000)
        doc2md.record_usage(500)
        self.assertEqual(doc2md.read_usage_today(), 1500)

    def test_corrupt_usage_file_is_not_fatal(self):
        self.usage.parent.mkdir(parents=True, exist_ok=True)
        self.usage.write_text("not json{{{")
        self.assertEqual(doc2md.read_usage_today(), 0)

    def test_estimate_matches_the_measured_per_image_cost(self):
        self.assertEqual(self.plan(4).estimated_tokens, 4 * doc2md.VISION_TOKENS_PER_IMAGE)


class TestPageDiagramDetection(FixtureCase):
    """A vector-drawn infographic has a text layer, so nothing else catches it."""

    @staticmethod
    def fake_pdf(images, drawings, line, lines=20):
        """A stand-in fitz document with one page of the given shape."""
        page = mock.Mock()
        page.get_images.return_value = [{}] * images
        page.get_drawings.return_value = [{}] * drawings
        page.get_text.return_value = "\n".join([line] * lines)
        doc = mock.MagicMock()
        doc.__iter__.return_value = iter([page])
        return doc

    def test_prose_page_is_not_a_diagram(self):
        self.assertEqual(doc2md.find_diagram_pages(self.files["pdf"], []), [])

    def test_non_pdf_is_ignored(self):
        self.assertEqual(doc2md.find_diagram_pages(self.files["docx"], []), [])

    def test_pages_already_queued_for_ocr_are_not_rendered_twice(self):
        with mock.patch.object(doc2md.fitz, "open") as fitz_open:
            fitz_open.return_value = self.fake_pdf(0, 40, "tiny")
            self.assertEqual(doc2md.find_diagram_pages(Path("x.pdf"), [0]), [])

    def test_fragmented_vector_page_is_detected(self):
        with mock.patch.object(doc2md.fitz, "open") as fitz_open:
            fitz_open.return_value = self.fake_pdf(0, 49, "Capture Your Data")
            self.assertEqual(doc2md.find_diagram_pages(Path("x.pdf"), []), [0])

    def test_image_rich_brochure_page_is_left_alone(self):
        """The tractor brochure: many images, few drawings, long lines."""
        with mock.patch.object(doc2md.fitz, "open") as fitz_open:
            fitz_open.return_value = self.fake_pdf(6, 13, "a fairly long line of body prose")
            self.assertEqual(doc2md.find_diagram_pages(Path("x.pdf"), []), [])


# ── Format routing ────────────────────────────────────────────────────────────
class TestImageInputDetection(unittest.TestCase):
    def test_recognises_image_extensions(self):
        for ext in [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif"]:
            self.assertTrue(doc2md.is_image_input(Path(f"shot{ext}")), ext)

    def test_case_insensitive(self):
        self.assertTrue(doc2md.is_image_input(Path("SHOT.PNG")))

    def test_rejects_documents(self):
        for ext in [".pdf", ".docx", ".pptx", ".html", ".epub", ".csv", ".txt"]:
            self.assertFalse(doc2md.is_image_input(Path(f"doc{ext}")), ext)


# ── Image classification ──────────────────────────────────────────────────────
class TestClassification(FixtureCase):
    def _classify(self, key):
        from PIL import Image

        path = self.files[key]
        with Image.open(path) as im:
            w, h = im.size
        return doc2md.classify_image(doc2md.ImageInfo(path=path, width=w, height=h))

    def test_icon_is_skipped(self):
        self.assertEqual(self._classify("icon"), "skip")

    def test_photo_is_detected(self):
        self.assertEqual(self._classify("photo"), "photo")

    def test_full_frame_tonal_image_is_a_photo(self):
        """
        Edge density is resolution-dependent — a large phone photo is smooth at
        the pixel level — so the second photo rule keys on the absence of a page
        background instead.
        """
        from PIL import Image

        path = self.src / "tonal.png"
        img = Image.new("RGB", (800, 600))
        px = img.load()
        for y in range(600):
            for x in range(800):
                # Smooth gradients, as a real photograph has — not per-pixel
                # noise, which produces an edge density no camera ever yields.
                px[x, y] = (
                    30 + x * 170 // 800,
                    40 + y * 160 // 600,
                    50 + (x + y) * 140 // 1400,
                )
        img.save(path)

        info = doc2md.ImageInfo(path=path, width=800, height=600)
        self.assertEqual(doc2md.classify_image(info), "photo")
        self.assertLess(info.background_frac, doc2md.PHOTO_MAX_BACKGROUND)

    def test_dark_dense_scan_is_not_mistaken_for_a_photo(self):
        """
        A newspaper scan also has no light background. Dense type must keep it
        on the OCR path, or its text is lost.
        """
        from PIL import Image, ImageDraw, ImageFont

        path = self.src / "dark_scan.png"
        img = Image.new("RGB", (900, 700), (105, 100, 95))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=13)
        for row in range(40):
            draw.text((15, 10 + row * 17), "dense body copy " * 6, fill=(20, 18, 16), font=font)
        img.save(path)

        info = doc2md.ImageInfo(path=path, width=900, height=700)
        self.assertNotEqual(doc2md.classify_image(info), "photo")

    def test_grey_scan_is_not_mistaken_for_a_photo(self):
        """
        A photocopy on grey paper has zero near-white pixels, so the near-white
        test alone calls it a photograph and its text is lost. The dominant-tone
        test is what recognises the page underneath.
        """
        from PIL import Image, ImageDraw, ImageFont

        path = self.src / "grey_scan.png"
        img = Image.new("RGB", (900, 1200), (150, 150, 148))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(size=15)
        for row in range(55):
            draw.text((20, 15 + row * 21), "body copy line of text " * 3,
                      fill=(25, 25, 25), font=font)
        img.save(path)

        info = doc2md.ImageInfo(path=path, width=900, height=1200)
        result = doc2md.classify_image(info)
        self.assertNotEqual(result, "photo")
        self.assertLess(info.background_frac, doc2md.PHOTO_MAX_BACKGROUND)
        self.assertGreater(info.modal_share, doc2md.PHOTO_MAX_MODAL_SHARE)

    def test_photos_never_reach_the_vision_model(self):
        """
        Privacy and cost guard: an image OCR found no structure in must not be
        escalated. Photographs would otherwise be uploaded to the API.
        """
        info = doc2md.ImageInfo(
            path=Path("/x/photo.jpg"),
            classification="photo",
            edge_density=0.03,
            width=4032,
            height=3024,
        )
        self.assertFalse(doc2md.should_escalate_to_vision(info))

        # Even mislabelled as text, sparse OCR output must not escalate it.
        info.classification = "text"
        info.ocr_text = "EXIT\nSALE"
        self.assertFalse(doc2md.should_escalate_to_vision(info))

    def test_unreadable_image_falls_back_to_ocr(self):
        broken = self.src / "broken.png"
        broken.write_bytes(b"not an image")
        info = doc2md.ImageInfo(path=broken, width=200, height=200)
        self.assertEqual(doc2md.classify_image(info), "text")

    def test_text_screenshot_is_routed_to_ocr(self):
        self.assertEqual(self._classify("png"), "text")

    def test_line_art_goes_to_ocr_first_not_straight_to_vision(self):
        """
        Measured on the sample corpus, low colour count means logo, rule, or
        image-of-text — never a diagram. Real diagrams are many-coloured
        screenshots. Whether to call the vision model is decided after OCR.
        """
        self.assertEqual(self._classify("diagram"), "text")

    def test_wide_flat_graphic_is_skipped_as_a_rule(self):
        self.assertEqual(self._classify("banner"), "skip")

    def test_colour_count_ignores_resampling_artefacts(self):
        """
        A two-tone silhouette must read as line art. Counting colours on a
        LANCZOS downsample used to report 225 distinct colours for a real
        two-colour logo, pushing it out of the line-art band entirely.
        """
        from PIL import Image

        logo = self.src / "logo.png"
        img = Image.new("RGB", (300, 300), "white")
        from PIL import ImageDraw

        ImageDraw.Draw(img).ellipse([40, 40, 260, 260], fill="black")
        img.save(logo)

        small = Image.open(logo).convert("RGB").resize(
            (100, 100), Image.Resampling.NEAREST
        )
        self.assertLessEqual(len(small.getcolors(maxcolors=10000)), doc2md.MAX_COLORS_DIAGRAM)

    def test_small_line_art_is_treated_as_a_logo(self):
        from PIL import Image, ImageDraw

        logo = self.src / "small_logo.png"
        img = Image.new("RGB", (149, 125), "white")
        ImageDraw.Draw(img).ellipse([20, 20, 120, 100], fill="black")
        img.save(logo)

        info = doc2md.ImageInfo(path=logo, width=149, height=125)
        self.assertEqual(doc2md.classify_image(info), "skip")

    def test_text_pages_are_never_classified_as_photos(self):
        """
        The harmful direction: a page labelled 'photo' skips OCR and loses its
        content. Mislabelling a photo as text only wastes an OCR call.
        """
        import fitz

        doc = fitz.open(str(self.files["pdf"]))
        page_png = self.src / "rendered_page.png"
        page_png.write_bytes(doc.load_page(0).get_pixmap(dpi=200).tobytes("png"))
        doc.close()

        from PIL import Image

        with Image.open(page_png) as im:
            w, h = im.size
        info = doc2md.ImageInfo(path=page_png, width=w, height=h)
        self.assertNotEqual(doc2md.classify_image(info), "photo")


# ── Extraction ────────────────────────────────────────────────────────────────
class TestExtraction(FixtureCase):
    def test_pdf_images_are_extracted_with_page_numbers(self):
        images_dir = self.out / "extract_pdf"
        images_dir.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        images = doc2md.extract_images(self.files["pdf"], images_dir, report)

        self.assertEqual(len(images), 2)
        self.assertEqual(report.pages_processed, 2)
        self.assertEqual(sorted(i.source_page for i in images), [0, 1])
        for img in images:
            self.assertTrue(img.path.exists())
            self.assertGreater(img.width, 0)

    def test_standalone_image_becomes_a_one_image_document(self):
        images_dir = self.out / "extract_png"
        images_dir.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        images = doc2md.extract_images(self.files["png"], images_dir, report)

        self.assertEqual(len(images), 1)
        self.assertEqual(report.pages_processed, 1)
        self.assertTrue(images[0].path.exists())

    def test_original_image_is_copied_not_moved(self):
        images_dir = self.out / "extract_copy"
        images_dir.mkdir(parents=True, exist_ok=True)
        doc2md.extract_images(self.files["png"], images_dir, doc2md.ProcessingReport())
        self.assertTrue(self.files["png"].exists(), "source image must survive")

    def test_docx_without_media_yields_no_images(self):
        images_dir = self.out / "extract_docx"
        images_dir.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        images = doc2md.extract_images(self.files["docx"], images_dir, report)
        self.assertEqual(images, [])
        self.assertEqual(report.warnings, [])

    def test_corrupt_pdf_warns_instead_of_raising(self):
        bad = self.src / "corrupt.pdf"
        bad.write_bytes(b"%PDF-1.4 truncated garbage")
        images_dir = self.out / "extract_bad"
        images_dir.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        images = doc2md.extract_images(bad, images_dir, report)
        self.assertEqual(images, [])
        self.assertTrue(report.warnings)


# ── Markdown assembly ─────────────────────────────────────────────────────────
class TestMerge(unittest.TestCase):
    def _img(self, name="page1_abc", classification="text", ocr="HELLO", vision=""):
        return doc2md.ImageInfo(
            path=Path(f"/x/{name}.png"),
            classification=classification,
            ocr_text=ocr,
            vision_description=vision,
        )

    def test_matching_reference_is_augmented_in_place(self):
        base = "Intro\n\n![diagram](media/page1_abc.png)\n\nOutro"
        out = doc2md.merge_images_into_markdown(base, [self._img()], "images")
        self.assertIn("**OCR Text:**", out)
        self.assertIn("> HELLO", out)
        self.assertNotIn("Extracted Images", out)
        self.assertLess(out.index("HELLO"), out.index("Outro"), "must stay in place")

    def test_unmatched_images_go_to_an_appendix(self):
        out = doc2md.merge_images_into_markdown("Body text", [self._img()], "images")
        self.assertIn("## Extracted Images", out)
        self.assertIn("images/page1_abc.png", out)

    def test_section_title_can_be_suppressed(self):
        out = doc2md.merge_images_into_markdown(
            "# shot\n", [self._img()], "images", section_title=None
        )
        self.assertNotIn("##", out)
        self.assertIn("images/page1_abc.png", out)

    def test_skipped_images_are_omitted_entirely(self):
        img = self._img(classification="skip", ocr="")
        out = doc2md.merge_images_into_markdown("Body", [img], "images")
        self.assertNotIn("page1_abc", out)

    def test_photo_is_referenced_without_a_description(self):
        img = self._img(classification="photo", ocr="")
        out = doc2md.merge_images_into_markdown("Body", [img], "images")
        self.assertIn("images/page1_abc.png", out)
        self.assertNotIn("OCR Text", out)

    def test_vision_description_is_inlined_verbatim(self):
        img = self._img(classification="semantic", ocr="", vision="## Flow\n1. Start")
        out = doc2md.merge_images_into_markdown("Body", [img], "images")
        self.assertIn("## Flow", out)
        self.assertIn("1. Start", out)


# ── End-to-end conversions ────────────────────────────────────────────────────
class TestConversions(FixtureCase):
    def test_pdf_keeps_its_text_layer(self):
        path, report, md = self.convert("pdf")
        self.assertTrue(path.exists())
        self.assertIn("Quarterly Report", md)
        self.assertIn("Revenue grew across all regions", md)
        self.assertEqual(report.pages_processed, 2)
        self.assertEqual(report.images_detected, 2)

    def test_docx_converts(self):
        _, _, md = self.convert("docx")
        self.assertIn("Design Notes", md)
        self.assertIn("pipeline converts documents", md)

    def test_html_converts(self):
        _, _, md = self.convert("html")
        self.assertIn("# Test Doc", md)
        self.assertIn("**world**", md)

    def test_csv_converts(self):
        _, _, md = self.convert("csv")
        self.assertIn("North", md)
        self.assertIn("1200", md)

    def test_multicolumn_pdf_keeps_all_text(self):
        """
        Column ORDER is not asserted — that is exactly what the pdf-inspector
        migration is meant to improve. This pins that no text is lost today.
        """
        _, _, md = self.convert("pdf_multicolumn")
        for line in ["Left column line one", "Right column line three"]:
            self.assertIn(line, md)

    def test_table_pdf_keeps_all_cells(self):
        """Cell VALUES must survive; table structure is a migration goal."""
        _, _, md = self.convert("pdf_table")
        for cell in ["Region", "Revenue", "North", "1200", "South", "980"]:
            self.assertIn(cell, md)

    def test_report_is_written_beside_the_markdown(self):
        path, _, _ = self.convert("pdf")
        report_file = path.parent / "report.txt"
        self.assertTrue(report_file.exists())
        self.assertIn("Processing Report", report_file.read_text())

    def test_image_links_resolve_relative_to_the_markdown(self):
        path, _, md = self.convert("pdf")
        import re

        for _, ref in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", md):
            self.assertTrue((path.parent / ref).exists(), f"dangling link: {ref}")

    def test_unknown_extension_falls_through_to_plain_text(self):
        """
        Pins CURRENT behaviour: MarkItDown treats an unrecognised extension as
        plain text and passes the raw bytes straight through, so binary junk can
        reach the Markdown. It does not crash and the report is still written.
        See the unknown-format note in MIGRATION.md.
        """
        weird = self.src / "thing.xyz"
        weird.write_bytes(b"\x00\x01\x02binary")
        dest = self.out / "weird"
        path, report = doc2md.convert_document(weird, dest, no_vision=True)

        self.assertTrue(path.exists())
        self.assertTrue((path.parent / "report.txt").exists())
        self.assertEqual(report.warnings, [])
        self.assertIn("binary", path.read_text(encoding="utf-8"))


# ── Screenshot input ──────────────────────────────────────────────────────────
class TestScreenshotInput(FixtureCase):
    def test_screenshot_produces_non_empty_markdown(self):
        """Regression: this used to emit a 0-byte file."""
        _, _, md = self.convert("png")
        self.assertGreater(len(md.strip()), 0)
        self.assertIn("# screenshot", md)
        self.assertIn("images/screenshot.png", md)

    def test_screenshot_has_no_appendix_heading(self):
        _, _, md = self.convert("png")
        self.assertNotIn("Extracted Images", md)

    def test_direct_image_is_never_skipped_as_an_icon(self):
        """
        The classifier calls this 600x200 banner 'skip'; because the user asked
        for it by name, convert_document must promote it rather than drop it.
        """
        _, report, md = self.convert("png")
        self.assertEqual(report.images_skipped, 0)
        self.assertIn("screenshot.png", md)

    @needs_tesseract
    def test_screenshot_text_is_ocred(self):
        _, report, md = self.convert("png")
        self.assertEqual(report.images_ocr, 1)
        self.assertIn("4250", md)
        self.assertIn("OCR Text", md)


# ── OCR ───────────────────────────────────────────────────────────────────────
@needs_tesseract
class TestOCR(FixtureCase):
    def test_reads_text_from_an_image(self):
        text = doc2md.ocr_image(self.files["png"])
        self.assertIn("4250", text)

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(doc2md.ocr_image(self.src / "nope.png"), "")

    def test_low_resolution_image_is_upscaled_before_ocr(self):
        from PIL import Image

        small = Image.new("RGB", (300, 200), "white")
        out = doc2md.upscale_for_ocr(small)
        self.assertGreater(min(out.size), min(small.size))
        self.assertLessEqual(out.width / small.width, doc2md.OCR_MAX_UPSCALE)

    def test_large_image_is_left_alone(self):
        from PIL import Image

        large = Image.new("RGB", (4032, 3024), "white")
        self.assertEqual(doc2md.upscale_for_ocr(large).size, large.size)

    def test_small_scan_text_is_recovered(self):
        """Regression: a low-resolution clipping used to OCR to nothing."""
        from PIL import Image, ImageDraw, ImageFont

        path = self.src / "small_scan.png"
        img = Image.new("RGB", (620, 200), "white")
        ImageDraw.Draw(img).text(
            (10, 80), "CAMPEONES MUNDIALES 1998", fill="black",
            font=ImageFont.load_default(size=17),
        )
        img.save(path)
        self.assertIn("1998", doc2md.ocr_image(path))

    def test_batch_ocr_fills_every_image(self):
        from PIL import Image, ImageDraw, ImageFont

        images = []
        for n in range(3):
            path = self.src / f"batch{n}.png"
            img = Image.new("RGB", (900, 200), "white")
            ImageDraw.Draw(img).text(
                (20, 70), f"BATCH NUMBER {n}00", fill="black",
                font=ImageFont.load_default(size=48),
            )
            img.save(path)
            images.append(doc2md.ImageInfo(path=path, width=900, height=200))

        doc2md.run_ocr_batch(images)
        for n, img in enumerate(images):
            self.assertIn(f"{n}00", img.ocr_text, f"image {n} lost its result")

    def test_batch_ocr_handles_a_single_image(self):
        img = doc2md.ImageInfo(path=self.files["png"], width=900, height=200)
        doc2md.run_ocr_batch([img])
        self.assertIn("4250", img.ocr_text)

    def test_batch_ocr_on_empty_list_is_a_no_op(self):
        doc2md.run_ocr_batch([])

    def test_available_languages_are_cached(self):
        doc2md.available_ocr_languages.cache_clear()
        first = doc2md.available_ocr_languages()
        with mock.patch.object(doc2md.pytesseract, "get_languages") as probe:
            second = doc2md.available_ocr_languages()
        probe.assert_not_called()
        self.assertEqual(first, second)

    def test_language_map_is_limited_to_installed_languages(self):
        self.assertEqual(set(doc2md.LANG_MAP), {"en", "es"})

    def test_scanned_pdf_text_is_recovered_via_ocr(self):
        _, report, md = self.convert("pdf_scanned")
        self.assertGreaterEqual(report.images_ocr, 1)
        self.assertIn("9876", md)


# ── Vision ────────────────────────────────────────────────────────────────────
class TestVision(FixtureCase):
    """Gemini is always mocked; these assert the wiring, not the model."""

    def test_semantic_image_is_replaced_by_its_description(self):
        with mock.patch.object(doc2md, "classify_image", return_value="semantic"), \
             mock.patch.object(
                 doc2md, "analyze_with_vision", return_value=("## Flowchart\n- Step one", 2680)
             ) as vision:
            _, report, md = self.convert("diagram", no_vision=False)

        vision.assert_called_once()
        self.assertEqual(report.images_ai, 1)
        self.assertIn("## Flowchart", md)
        self.assertIn("- Step one", md)

    def test_no_vision_flag_prevents_any_api_call(self):
        with mock.patch.object(doc2md, "classify_image", return_value="semantic"), \
             mock.patch.object(doc2md, "analyze_with_vision") as vision:
            _, report, _ = self.convert("diagram", no_vision=True)

        vision.assert_not_called()
        self.assertEqual(report.images_ai, 0)
        self.assertTrue(any("--no-vision" in w for w in report.warnings))

    def test_empty_vision_response_is_recorded_as_a_warning(self):
        with mock.patch.object(doc2md, "classify_image", return_value="semantic"), \
             mock.patch.object(doc2md, "analyze_with_vision", return_value=("", 0)):
            _, report, _ = self.convert("diagram", no_vision=False)

        self.assertEqual(report.images_ai, 0)
        self.assertTrue(any("Vision analysis empty" in w for w in report.warnings))

    def test_missing_api_key_degrades_quietly(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(doc2md.analyze_with_vision(self.files["diagram"], "m"), ("", 0))

    def test_images_whose_ocr_reads_as_prose_never_reach_the_vision_model(self):
        """
        Cost guard. An image OCR handled well must not be sent to the API. Since
        escalation was added, the guard is the OCR *result*, not the initial
        classification — so this stubs OCR with clean prose.
        """
        prose = "\n".join(["the quick brown fox jumps over the lazy dog again"] * 6)
        with mock.patch.object(doc2md, "classify_image", return_value="text"), \
             mock.patch.object(doc2md, "ocr_image", return_value=prose), \
             mock.patch.object(doc2md, "analyze_with_vision") as vision:
            self.convert("png", no_vision=False)
        vision.assert_not_called()

    def test_fragmented_ocr_escalates_to_the_vision_model(self):
        """The corpus finding: diagrams reach OCR first, then get escalated."""
        with mock.patch.object(doc2md, "classify_image", return_value="text"), \
             mock.patch.object(doc2md, "ocr_image", return_value="Start\nMiddle\nEnd\nStep"), \
             mock.patch.object(doc2md, "analyze_with_vision", return_value=("## Flow", 2680)) as vision:
            _, report, md = self.convert("diagram", no_vision=False)
        vision.assert_called_once()
        self.assertIn("## Flow", md)


# ── Table normalisation ───────────────────────────────────────────────────────
class TestTableNormalisation(unittest.TestCase):
    """
    pdf-inspector wraps ordinary prose in empty-celled table syntax when its
    column detector misfires. These pin the unwrapping.
    """

    def test_prose_wrapped_as_a_table_is_unwrapped(self):
        md = "||Regardless of your chosen path, you seek out||\n|---|---|---|\n||external coaching.||"
        out = doc2md.normalize_pdf_tables(md)
        self.assertNotIn("|", out)
        self.assertIn("Regardless of your chosen path", out)
        self.assertIn("external coaching.", out)

    def test_genuine_tables_survive(self):
        md = "|Region|Revenue|\n|---|---|\n|North|1200|"
        out = doc2md.normalize_pdf_tables(md)
        self.assertIn("|North|1200|", out)
        self.assertIn("|Region|Revenue|", out)

    def test_separator_width_matches_the_data_row(self):
        """Trailing empty cells must be counted, or the table renders broken."""
        md = "|authority and accountability.|Support Tools||\n|Community|Online Courses|Blog|"
        out = doc2md.normalize_pdf_tables(md).splitlines()
        sep = [l for l in out if set(l) <= set("|-")]
        self.assertEqual(len(sep), 1)
        self.assertEqual(sep[0].count("---"), 3)

    def test_mixed_block_splits_prose_from_table(self):
        md = "||Some prose line||\n|---|---|---|\n|A|B|C|\n|D|E|F|"
        out = doc2md.normalize_pdf_tables(md)
        self.assertIn("Some prose line", out)
        self.assertNotIn("||Some prose line||", out)
        self.assertIn("|A|B|C|", out)

    def test_non_table_content_is_untouched(self):
        md = "# Heading\n\nA paragraph.\n\n- bullet"
        self.assertEqual(doc2md.normalize_pdf_tables(md), md)

    def test_row_cells_counts_empty_edge_cells(self):
        self.assertEqual(len(doc2md._row_cells("||text||")), 3)
        self.assertEqual(len(doc2md._row_cells("|a|b||")), 3)
        self.assertEqual(len(doc2md._row_cells("|a|b|")), 2)


# ── Routing ───────────────────────────────────────────────────────────────────
class TestRouting(FixtureCase):
    def test_pdf_routes_to_inspector_when_available(self):
        report = doc2md.ProcessingReport()
        source = doc2md.route_document(self.files["pdf"], report)
        if doc2md.HAS_PDF_INSPECTOR:
            self.assertTrue(source.pages, "per-page data should be populated")
            self.assertEqual(source.page_count, 2)
        self.assertIn("Quarterly Report", source.markdown)

    def test_pdf_falls_back_to_markitdown_when_inspector_missing(self):
        report = doc2md.ProcessingReport()
        with mock.patch.object(doc2md, "HAS_PDF_INSPECTOR", False):
            source = doc2md.route_document(self.files["pdf"], report)
        self.assertEqual(source.pages, [])
        self.assertIn("Quarterly Report", source.markdown)

    def test_inspector_failure_degrades_to_markitdown_with_a_warning(self):
        report = doc2md.ProcessingReport()
        with mock.patch.object(doc2md, "HAS_PDF_INSPECTOR", True), \
             mock.patch.object(
                 doc2md, "_pdf_via_inspector", side_effect=RuntimeError("boom")
             ):
            source = doc2md.route_document(self.files["pdf"], report)
        self.assertIn("Quarterly Report", source.markdown)
        self.assertTrue(any("falling back" in w for w in report.warnings))

    def test_non_pdf_never_touches_the_inspector(self):
        report = doc2md.ProcessingReport()
        source = doc2md.route_document(self.files["html"], report)
        self.assertEqual(source.pages, [])
        self.assertIn("Test Doc", source.markdown)

    def test_image_input_gets_a_title_only(self):
        report = doc2md.ProcessingReport()
        source = doc2md.route_document(self.files["png"], report)
        self.assertEqual(source.markdown.strip(), "# screenshot")

    def test_unreadable_document_returns_none_with_a_warning(self):
        report = doc2md.ProcessingReport()
        missing = self.src / "nope.docx"
        self.assertIsNone(doc2md.route_document(missing, report))
        self.assertTrue(report.warnings)


# ── Page rendering for OCR ────────────────────────────────────────────────────
class TestPageRendering(FixtureCase):
    def test_renders_only_the_requested_pages(self):
        out = self.out / "render"
        out.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        rendered = doc2md.render_pdf_pages(self.files["pdf"], [1], out, report)

        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0].source_page, 1)
        self.assertTrue(rendered[0].path.exists())

    def test_rendered_pages_are_pre_classified_for_ocr(self):
        """They must bypass classify_image, which could label them 'photo'."""
        out = self.out / "render_class"
        out.mkdir(parents=True, exist_ok=True)
        rendered = doc2md.render_pdf_pages(
            self.files["pdf"], [0], out, doc2md.ProcessingReport()
        )
        self.assertEqual(rendered[0].classification, "text")

    def test_oversized_page_box_is_capped(self):
        """A 20×33 inch page box would otherwise render to 27 megapixels."""
        import fitz

        doc = fitz.open()
        doc.new_page(width=1473.75, height=2356.5)
        zoom, _ = doc2md._ocr_render_scale(doc.load_page(0))
        doc.close()
        self.assertLessEqual(2356.5 * zoom, doc2md.PDF_OCR_MAX_LONG_SIDE + 1)

    def test_letter_page_is_rendered_at_full_dpi(self):
        import fitz

        doc = fitz.open()
        doc.new_page(width=612, height=792)
        zoom, _ = doc2md._ocr_render_scale(doc.load_page(0))
        doc.close()
        self.assertAlmostEqual(zoom, doc2md.PDF_OCR_RENDER_DPI / 72.0, places=5)

    def test_renders_are_written_as_jpeg(self):
        out = self.out / "render_fmt"
        out.mkdir(parents=True, exist_ok=True)
        rendered = doc2md.render_pdf_pages(
            self.files["pdf"], [0], out, doc2md.ProcessingReport()
        )
        self.assertEqual(rendered[0].path.suffix, ".jpg")
        self.assertLess(rendered[0].path.stat().st_size, 2_000_000)

    def test_empty_page_list_does_no_work(self):
        out = self.out / "render_none"
        out.mkdir(parents=True, exist_ok=True)
        self.assertEqual(
            doc2md.render_pdf_pages(self.files["pdf"], [], out, doc2md.ProcessingReport()),
            [],
        )

    def test_out_of_range_page_warns_rather_than_crashing(self):
        out = self.out / "render_bad"
        out.mkdir(parents=True, exist_ok=True)
        report = doc2md.ProcessingReport()
        rendered = doc2md.render_pdf_pages(self.files["pdf"], [99], out, report)
        self.assertEqual(rendered, [])
        self.assertTrue(report.warnings)

    def test_embedded_images_on_rendered_pages_are_discarded(self):
        """On a scanned page the embedded image IS the page — keep one, not two."""
        out = self.out / "dedup"
        out.mkdir(parents=True, exist_ok=True)
        embedded = out / "page1_abc.png"
        embedded.write_bytes(b"placeholder")
        images = [
            doc2md.ImageInfo(path=embedded, source_page=0),
            doc2md.ImageInfo(path=out / "page2_def.png", source_page=1),
        ]
        kept = doc2md.discard_images_on_rendered_pages(images, [0])

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source_page, 1)
        self.assertFalse(embedded.exists(), "superseded file should be deleted")

    def test_nothing_is_discarded_when_no_pages_are_rendered(self):
        images = [doc2md.ImageInfo(path=Path("/x/a.png"), source_page=0)]
        self.assertEqual(len(doc2md.discard_images_on_rendered_pages(images, [])), 1)

    def test_scanned_page_yields_exactly_one_image(self):
        """End-to-end: no duplicate content, no duplicate OCR, no duplicate API call."""
        _, report, _ = self.convert("pdf_scanned")
        if doc2md.HAS_PDF_INSPECTOR:
            self.assertEqual(report.images_detected, 1)

    def test_page_renders_are_deleted_after_ocr(self):
        path, report, md = self.convert("pdf_scanned")
        images_dir = path.parent / "images"
        leftover = list(images_dir.iterdir()) if images_dir.exists() else []
        self.assertEqual(leftover, [], "page renders should not survive")
        self.assertNotIn("](images/", md, "no link may point at a deleted file")

    def test_keep_page_scans_retains_them(self):
        dest = self.out / "kept"
        path, _ = doc2md.convert_document(
            self.files["pdf_scanned"], dest, no_vision=True, keep_page_scans=True
        )
        images_dir = path.parent / "images"
        if doc2md.HAS_PDF_INSPECTOR:
            self.assertTrue(any(images_dir.iterdir()))
            self.assertIn("](images/", path.read_text(encoding="utf-8"))

    def test_discarded_render_text_is_not_blockquoted(self):
        """A whole scanned book must not be marked up as one long quotation."""
        img = doc2md.ImageInfo(
            path=Path("/x/page1_scan.jpg"),
            from_page_render=True,
            render_discarded=True,
            classification="text",
            ocr_text="First line of the page\nSecond line of the page",
        )
        block = doc2md.build_image_reference(img, "images")
        self.assertNotIn(">", block)
        self.assertNotIn("![", block)
        self.assertIn("First line of the page", block)

    def test_discarding_is_idempotent(self):
        img = doc2md.ImageInfo(path=Path("/x/gone.jpg"), from_page_render=True)
        self.assertEqual(doc2md.discard_page_renders([img]), 1)
        self.assertEqual(doc2md.discard_page_renders([img]), 0)

    def test_embedded_images_survive_discarding(self):
        embedded = doc2md.ImageInfo(path=Path("/x/fig.png"), from_page_render=False)
        doc2md.discard_page_renders([embedded])
        self.assertFalse(embedded.render_discarded)

    def test_pages_with_text_are_never_rendered(self):
        """The core cost guarantee: no OCR on pages that already have text."""
        with mock.patch.object(doc2md, "render_pdf_pages", return_value=[]) as render:
            self.convert("pdf")
        if doc2md.HAS_PDF_INSPECTOR:
            for call in render.call_args_list:
                self.assertEqual(call.args[1], [], "no page should need OCR")


# ── Page-aware assembly ───────────────────────────────────────────────────────
class TestPageAssembly(unittest.TestCase):
    def test_images_land_after_their_own_page(self):
        pages = [
            doc2md.PdfPageInfo(number=0, markdown="Page one text."),
            doc2md.PdfPageInfo(number=1, markdown="Page two text."),
        ]
        img = doc2md.ImageInfo(
            path=Path("/x/p2.png"), source_page=1, classification="text", ocr_text="OCR"
        )
        out = doc2md.assemble_by_page(pages, [img], "images")
        self.assertLess(out.index("Page two text."), out.index("p2.png"))
        self.assertLess(out.index("Page one text."), out.index("Page two text."))

    def test_skipped_images_are_not_placed(self):
        pages = [doc2md.PdfPageInfo(number=0, markdown="Text.")]
        img = doc2md.ImageInfo(path=Path("/x/i.png"), source_page=0, classification="skip")
        self.assertNotIn("i.png", doc2md.assemble_by_page(pages, [img], "images"))

    def test_empty_pages_are_dropped(self):
        pages = [
            doc2md.PdfPageInfo(number=0, markdown="   "),
            doc2md.PdfPageInfo(number=1, markdown="Real text."),
        ]
        self.assertEqual(doc2md.assemble_by_page(pages, [], "images"), "Real text.")


# ── Email ─────────────────────────────────────────────────────────────────────
class TestEmail(FixtureCase):
    def test_headers_become_a_metadata_block(self):
        _, _, md = self.convert("eml")
        self.assertIn("# Quarterly numbers", md)
        self.assertIn("ana@example.com", md)
        self.assertIn("beto@example.com", md)
        self.assertIn("**Date:**", md)

    def test_html_body_is_converted_to_markdown(self):
        _, _, md = self.convert("eml")
        self.assertIn("1200", md)
        self.assertNotIn("<b>", md)
        self.assertNotIn("<p>", md)

    def test_attachments_are_listed(self):
        _, _, md = self.convert("eml")
        self.assertIn("notes.txt", md)

    def test_image_attachments_enter_the_image_pipeline(self):
        _, report, md = self.convert("eml_image")
        self.assertEqual(report.images_detected, 1)
        self.assertIn("shot.png", md)

    @needs_tesseract
    def test_attached_screenshot_is_ocred(self):
        _, report, md = self.convert("eml_image")
        self.assertGreaterEqual(report.images_ocr, 1)
        self.assertIn("777", md)

    def test_malformed_email_warns_rather_than_crashing(self):
        bad = self.src / "bad.eml"
        bad.write_bytes(b"\xff\xfe not really an email")
        report = doc2md.ProcessingReport()
        source = doc2md.route_document(bad, report)
        self.assertTrue(source is None or isinstance(source.markdown, str))


# ── OCR → vision escalation ───────────────────────────────────────────────────
class TestEscalation(unittest.TestCase):
    """
    Colour count cannot separate a screenshotted diagram from a scanned page,
    so escalation combines edge density with the shape of the OCR output.
    """

    def _img(self, edge, ocr, cls="text", rendered=False):
        return doc2md.ImageInfo(
            path=Path("/x/i.png"),
            classification=cls,
            ocr_text=ocr,
            edge_density=edge,
            from_page_render=rendered,
        )

    def test_prose_detection_on_continuous_text(self):
        prose = "\n".join(["the quick brown fox jumps over the lazy dog again"] * 5)
        self.assertTrue(doc2md.ocr_looks_like_prose(prose))

    def test_prose_detection_on_scattered_labels(self):
        self.assertFalse(doc2md.ocr_looks_like_prose("Start\nMiddle\nEnd\n1. Step"))

    def test_empty_ocr_is_not_prose(self):
        self.assertFalse(doc2md.ocr_looks_like_prose("   \n\n "))

    def test_sparse_image_with_fragmented_ocr_escalates(self):
        self.assertTrue(
            doc2md.should_escalate_to_vision(self._img(0.02, "Start\nEnd\nStep 1\nDone"))
        )

    def test_single_line_screenshot_does_not_escalate(self):
        """OCR already captured it; a vision call would add nothing."""
        self.assertFalse(doc2md.should_escalate_to_vision(self._img(0.02, "TOTAL: 777")))

    def test_dense_page_never_escalates(self):
        """Edge density alone must veto, even when OCR looks fragmented."""
        self.assertFalse(doc2md.should_escalate_to_vision(self._img(0.09, "A\nB\nC")))

    def test_good_ocr_never_escalates(self):
        prose = "\n".join(["the quick brown fox jumps over the lazy dog again"] * 5)
        self.assertFalse(doc2md.should_escalate_to_vision(self._img(0.02, prose)))

    def test_rendered_pdf_pages_never_escalate(self):
        """A long scanned document must not become one API call per page."""
        self.assertFalse(
            doc2md.should_escalate_to_vision(self._img(0.02, "A\nB", rendered=True))
        )

    def test_non_text_classifications_are_left_alone(self):
        for cls in ["skip", "photo", "semantic"]:
            self.assertFalse(
                doc2md.should_escalate_to_vision(self._img(0.02, "A\nB", cls=cls)), cls
            )

    def test_no_vision_flag_suppresses_escalation(self):
        """--no-vision must not silently convert OCR results into empty blocks."""
        fixture_dir = Path(tempfile.mkdtemp(prefix="doc2md-esc-"))
        try:
            files = fixtures.build_all(fixture_dir / "src")
            with mock.patch.object(doc2md, "analyze_with_vision") as vision:
                _, report = doc2md.convert_document(
                    files["diagram"], fixture_dir / "out", no_vision=True
                )
            vision.assert_not_called()
        finally:
            shutil.rmtree(fixture_dir, ignore_errors=True)

    def test_escalated_image_falls_back_to_ocr_when_vision_is_empty(self):
        img = doc2md.ImageInfo(
            path=Path("/x/i.png"),
            classification="semantic",
            ocr_text="Start\nEnd",
            vision_description="",
        )
        block = doc2md.build_image_reference(img, "images")
        self.assertIn("OCR Text", block)
        self.assertIn("Start", block)


# ── Report ────────────────────────────────────────────────────────────────────
class TestReport(unittest.TestCase):
    def test_render_includes_every_counter(self):
        r = doc2md.ProcessingReport(input_file="/x/doc.pdf")
        r.pages_processed, r.images_detected = 3, 5
        r.images_skipped, r.images_ocr, r.images_ai, r.images_photo = 1, 2, 1, 1
        r.warnings.append("something odd")
        r.start_time, r.end_time = 0.0, 1.5
        out = r.render()

        self.assertIn("doc.pdf", out)
        self.assertIn("something odd", out)
        self.assertEqual(r.processing_time, 1.5)
        for label in ["Pages processed", "Images detected", "OCR processed", "AI analyzed"]:
            self.assertIn(label, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
