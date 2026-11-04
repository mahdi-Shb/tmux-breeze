from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from breeze_artifacts import finalize_artifacts


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactTests(unittest.TestCase):
    def fixture(self, output, path="capture/part-0001.png", name="lifetime"):
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (200, 80), (35, 38, 39)).save(target, "PNG")
        (target.parent / "capture.ansi").write_bytes(b"\x1b[32moriginal\n")
        image = {"file": path, "first_row": 1, "last_row": 16, "width": 200,
                 "height": 80, "row_count": 16,
                 "print": {"report_width_mm": 170, "effective_font_pt": 9.5}}
        pane = {"window_id": "@1", "window_index": 0, "window_name": name,
                "pane_id": "%2", "pane_index": 2,
                "buffers": [{"buffer": "current", "images": [image]}]}
        return {"session_name": "lab", "panes": [pane]}, image

    def test_simple_names_preserve_raw_images_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, image = self.fixture(output)
            original = output / image["file"]
            ansi = original.parent / "capture.ansi"
            hashes = digest(original), digest(ansi)
            finalize_artifacts(output, manifest, simple_names=True)
            self.assertEqual(image["file"], "images/lifetime-p02-rows-0001-0016.png")
            self.assertEqual(image["original_file"], "capture/part-0001.png")
            self.assertEqual(image["source_identity"]["pane_id"], "%2")
            self.assertEqual(digest(output / image["file"]), hashes[0])
            self.assertEqual((digest(original), digest(ansi)), hashes)

    def test_collisions_across_windows_buffers_and_repeated_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, _ = self.fixture(output)
            original = manifest["panes"][0]
            another = deepcopy(original)
            another["window_index"], another["window_id"] = 5, "@5"
            original["buffers"].append({"buffer": "main", "images": [deepcopy(original["buffers"][0]["images"][0])]})
            another["buffers"][0]["images"] *= 2
            # Repeated objects are not a normal manifest; use independent records.
            another["buffers"][0]["images"] = [deepcopy(i) for i in another["buffers"][0]["images"]]
            manifest["panes"].append(another)
            finalize_artifacts(output, manifest, simple_names=True)
            names = [i["file"] for p in manifest["panes"] for b in p["buffers"] for i in b["images"]]
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all((output / name).is_file() for name in names))

    def test_html_preview_escapes_names_and_keeps_labels_outside_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, image = self.fixture(output, name='<script>alert("x")</script>')
            original = digest(output / image["file"])
            image["warnings"] = ['font < 8 pt & small']
            finalize_artifacts(output, manifest, preview=True)
            preview = (output / "preview.html").read_text(encoding="utf-8")
            self.assertIn("&lt;script&gt;", preview)
            self.assertNotIn('<script>alert(', preview)
            self.assertIn("rows 1-16", preview)
            self.assertIn("200 × 80 px", preview)
            self.assertIn("font &lt; 8 pt &amp; small", preview)
            self.assertEqual(digest(output / image["file"]), original)
            with Image.open(output / "contact-sheet.png") as sheet:
                self.assertEqual(sheet.size, (440, 330))

    def test_snippets_support_special_unicode_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, image = self.fixture(output, path="capture/file #_%نام.png")
            finalize_artifacts(output, manifest, snippets="both", report_width_mm=170)
            latex = (output / "images.tex").read_text(encoding="utf-8")
            markdown = (output / "images.md").read_text(encoding="utf-8")
            self.assertIn(r"width=170mm", latex)
            self.assertIn(r"\detokenize{latex-images/figure-0001.png}", latex)
            self.assertIn("TODO: add a caption.", latex)
            self.assertIn("%23_%25", markdown)
            self.assertIn("%20", markdown)
            self.assertNotIn("نام", markdown)
            alias = output / image["latex_file"]
            self.assertEqual(digest(alias), digest(output / image["file"]))
            self.assertEqual(manifest["artifacts"]["latex_image_aliases"][0]["source_file"], image["file"])

    def test_plain_latex_paths_are_relative_and_underscore_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, _ = self.fixture(output, path="window_1/image_1.png")
            finalize_artifacts(output, manifest, snippets="latex")
            latex = (output / "images.tex").read_text(encoding="utf-8")
            self.assertIn(r"\includegraphics[width=\linewidth]{\detokenize{window_1/image_1.png}}", latex)
            self.assertNotIn(r"\documentclass", latex)
            self.assertFalse((output / "images.md").exists())

    def test_preview_contact_sheet_paginates_large_collections(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, image = self.fixture(output)
            manifest["panes"][0]["buffers"][0]["images"] = [deepcopy(image) for _ in range(61)]
            finalize_artifacts(output, manifest, preview=True)
            self.assertEqual(manifest["artifacts"]["contact_sheets"], ["contact-sheet.png", "contact-sheet-002.png"])
            self.assertEqual((output / "preview.html").read_text(encoding="utf-8").count("<figure>"), 61)

    def test_rejects_escape_paths_and_unrecognized_snippet_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest, image = self.fixture(output)
            image["file"] = "../outside.png"
            with self.assertRaises(ValueError):
                finalize_artifacts(output, manifest, preview=True)
            image["file"] = "capture/part-0001.png"
            with self.assertRaises(ValueError):
                finalize_artifacts(output, manifest, snippets="unknown")

    def test_empty_collection_still_creates_valid_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = {"panes": []}
            finalize_artifacts(output, manifest, preview=True, snippets="both")
            self.assertIn("0 images", (output / "preview.html").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifacts"]["contact_sheets"], [])


if __name__ == "__main__":
    unittest.main()
