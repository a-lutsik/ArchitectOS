from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from backend.architectos.files import FileStore, sniff_image_type

# 1×1 transparent PNG.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ImageSniffTests(unittest.TestCase):
    def test_png_header(self) -> None:
        mime, ext = sniff_image_type(PNG_1X1) or ("", "")
        self.assertEqual(mime, "image/png")
        self.assertEqual(ext, ".png")

    def test_jpeg_header(self) -> None:
        mime, ext = sniff_image_type(b"\xff\xd8\xff\xdb\x00") or ("", "")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(ext, ".jpg")

    def test_non_image(self) -> None:
        self.assertIsNone(sniff_image_type(b"not an image"))


class FileStoreUploadTests(unittest.TestCase):
    def test_clipboard_png_without_extension_is_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(Path(tmp))
            meta = store.upload("p1", "clipboard", base64.b64encode(PNG_1X1).decode("ascii"))
            self.assertEqual(meta["mime"], "image/png")
            self.assertTrue(str(meta["name"]).endswith(".png"))
            self.assertTrue(store.is_image(meta))
            payload = store.image_payload("p1", [meta["id"]])
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["mime"], "image/png")

    def test_explicit_mime_kept_for_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(Path(tmp))
            meta = store.upload(
                "p1",
                "image.png",
                base64.b64encode(PNG_1X1).decode("ascii"),
                mime="image/png",
            )
            self.assertEqual(meta["mime"], "image/png")
            self.assertTrue(store.is_image(meta))


if __name__ == "__main__":
    unittest.main()
