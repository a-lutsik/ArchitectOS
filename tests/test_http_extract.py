"""HTTP source extract + request helpers."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from architectos.source_remote import (
    _http_method,
    detect_json_paths,
    extract_json_items,
    fetch_http_probe,
    ingest_http_candidates,
    resolve_dot_path,
)


class HttpExtractTests(unittest.TestCase):
    def test_resolve_dot_path(self) -> None:
        data = {"data": {"items": [{"title": "A"}]}}
        self.assertEqual(resolve_dot_path(data, "data.items")[0]["title"], "A")
        self.assertEqual(resolve_dot_path(data, ""), data)
        with self.assertRaises(KeyError):
            resolve_dot_path(data, "missing.path")

    def test_extract_json_items_with_fields(self) -> None:
        payload = {
            "data": {
                "items": [
                    {"title": "One", "body": "Hello", "meta": {"id": 1}, "noise": {"x": 1}},
                    {"title": "Two", "body": "World", "summary": "S"},
                ]
            }
        }
        rows = extract_json_items(
            payload,
            {"items_path": "data.items", "label_field": "title", "text_fields": ["body", "summary"]},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["label"], "One")
        self.assertEqual(rows[0]["text"], "Hello")
        self.assertIn("World", rows[1]["text"])
        self.assertIn("S", rows[1]["text"])
        self.assertNotIn("noise", rows[0]["text"])

    def test_extract_without_text_fields_keeps_scalars_only(self) -> None:
        payload = {"items": [{"title": "T", "nested": {"a": 1}, "ok": "yes"}]}
        rows = extract_json_items(payload, {"items_path": "items", "label_field": "title"})
        self.assertEqual(rows[0]["label"], "T")
        self.assertIn("ok: yes", rows[0]["text"])
        self.assertNotIn("nested", rows[0]["text"])

    def test_detect_json_paths(self) -> None:
        paths = detect_json_paths({"data": {"items": [{"title": "x", "body": "y"}]}})
        self.assertTrue(any(p.startswith("data") for p in paths))
        self.assertTrue(any("title" in p for p in paths))

    def test_http_method_validation(self) -> None:
        self.assertEqual(_http_method({"method": "post"}), "POST")
        with self.assertRaises(ValueError):
            _http_method({"method": "TRACE"})


class HttpRequestTests(unittest.TestCase):
    def test_probe_json_preview_and_candidates(self) -> None:
        payload = {"items": [{"title": "Alpha", "body": "Text alpha here"}]}
        body = json.dumps(payload).encode("utf-8")

        class _Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, _n: int) -> bytes:
                return body

            def getcode(self) -> int:
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("architectos.source_remote.validate_outbound_url", return_value="https://example.com/api"), \
             patch("architectos.source_remote.outbound_policy") as policy, \
             patch("architectos.source_remote.urlopen", return_value=_Resp()):
            policy.return_value.__enter__ = MagicMock(return_value=None)
            policy.return_value.__exit__ = MagicMock(return_value=False)
            result = fetch_http_probe({
                "url": "https://example.com/api",
                "method": "GET",
                "extract": {"mode": "json", "items_path": "items", "label_field": "title", "text_fields": ["body"]},
            })
        self.assertTrue(result["ok"])
        self.assertIn("items", result.get("json_preview") or "")
        self.assertTrue(result.get("detected_paths"))
        self.assertEqual(result["preview_candidates"][0]["label"], "probe: Alpha")
        self.assertIn("Text alpha", result["preview_candidates"][0]["text"])

    def test_ingest_uses_method_and_body(self) -> None:
        captured: dict = {}
        payload = {"items": [{"title": "P", "body": "posted body text"}]}
        body = json.dumps(payload).encode("utf-8")

        class _Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, _n: int) -> bytes:
                return body

            def getcode(self) -> int:
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):  # noqa: ARG001
            captured["method"] = req.get_method()
            captured["data"] = req.data
            return _Resp()

        with patch("architectos.source_remote.validate_outbound_url", return_value="https://example.com/search"), \
             patch("architectos.source_remote.outbound_policy") as policy, \
             patch("architectos.source_remote.urlopen", side_effect=fake_urlopen):
            policy.return_value.__enter__ = MagicMock(return_value=None)
            policy.return_value.__exit__ = MagicMock(return_value=False)
            candidates = ingest_http_candidates(
                "proj",
                {
                    "id": "src1",
                    "name": "API",
                    "kind": "docs",
                    "config": {
                        "http": {
                            "url": "https://example.com/search",
                            "method": "POST",
                            "body": '{"q":"x"}',
                            "extract": {
                                "mode": "json",
                                "items_path": "items",
                                "label_field": "title",
                                "text_fields": ["body"],
                            },
                        }
                    },
                },
                stable_id=lambda *parts: "id:" + ":".join(map(str, parts)),
            )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], b'{"q":"x"}')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["label"], "API: P")
        self.assertIn("posted body", candidates[0]["text"])


if __name__ == "__main__":
    unittest.main()
