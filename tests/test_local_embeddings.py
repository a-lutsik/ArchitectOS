from __future__ import annotations

import os
import unittest
from unittest import mock

from backend.architectos import embeddings as emb


class _FakeFastEmbed:
    """fastembed-style encoder: .embed(list[str]) -> iterator of vectors."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(self, texts: list[str]):
        for _ in texts:
            yield list(self._vector)


class _FakeSentenceTransformer:
    """sentence-transformers-style encoder: .encode(str) -> vector."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def encode(self, text: str, normalize_embeddings: bool = True):
        return list(self._vector)


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # CI pins MEMORY_EMBEDDING_PROVIDER=hash for determinism; that operator
        # override must not leak into these provider-selection tests.
        patcher = mock.patch.dict(os.environ, {"MEMORY_EMBEDDING_PROVIDER": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_falls_back_to_hash_when_no_local_backend(self) -> None:
        with mock.patch.object(emb, "local_embeddings_available", return_value=False):
            provider = emb.build_embedding_provider({"embedding_provider": "auto"})
        self.assertEqual(provider.provider_id, "hash")

    def test_auto_prefers_local_over_hash_when_available(self) -> None:
        # No cloud keys / ollama host in the test env, so the chain reaches _local().
        with mock.patch.object(emb, "local_embeddings_available", return_value=True):
            provider = emb.build_embedding_provider({"embedding_provider": "auto"})
        self.assertEqual(provider.provider_id, "local")
        # Construction must stay lazy: no encoder loaded, no download.
        self.assertIsNone(provider._encoder)
        self.assertEqual(provider.dimensions, 0)

    def test_explicit_local_mode_falls_back_when_unavailable(self) -> None:
        with mock.patch.object(emb, "local_embeddings_available", return_value=False):
            provider = emb.build_embedding_provider({"embedding_provider": "local"})
        self.assertEqual(provider.provider_id, "hash")

    def test_explicit_local_aliases_select_local_when_available(self) -> None:
        for alias in ("local", "fastembed", "sentence-transformers", "st", "onnx"):
            with self.subTest(alias=alias):
                with mock.patch.object(emb, "local_embeddings_available", return_value=True):
                    provider = emb.build_embedding_provider({"embedding_provider": alias})
                self.assertEqual(provider.provider_id, "local")

    def test_available_helper_is_false_without_optional_libs(self) -> None:
        # The extra is not installed in CI/dev by default.
        self.assertFalse(emb.local_embeddings_available())

    def test_catalog_exposes_local_before_hash(self) -> None:
        ids = [entry["id"] for entry in emb.embedding_provider_catalog()]
        self.assertIn("local", ids)
        self.assertIn("hash", ids)
        self.assertLess(ids.index("local"), ids.index("hash"))
        local_entry = next(entry for entry in emb.embedding_provider_catalog() if entry["id"] == "local")
        self.assertEqual(local_entry.get("requires_extra"), "embeddings")
        self.assertTrue(local_entry.get("models"))


class LocalEmbedTests(unittest.TestCase):
    def test_construction_is_cheap_and_uses_default_model(self) -> None:
        provider = emb.LocalEmbeddingProvider()
        self.assertEqual(provider.model, emb.DEFAULT_LOCAL_EMBED_MODEL)
        self.assertEqual(provider.dimensions, 0)
        self.assertIsNone(provider._encoder)

    def test_custom_model_is_respected(self) -> None:
        provider = emb.LocalEmbeddingProvider(model="intfloat/multilingual-e5-small")
        self.assertEqual(provider.model, "intfloat/multilingual-e5-small")

    def test_embed_fastembed_backend_normalizes_and_sets_dims(self) -> None:
        provider = emb.LocalEmbeddingProvider()
        provider._encoder = _FakeFastEmbed([3.0, 4.0, 0.0])
        provider._backend = "fastembed"
        vector = provider.embed("hello world")
        self.assertEqual(provider.dimensions, 3)
        # 3-4-0 normalizes to 0.6-0.8-0.
        self.assertAlmostEqual(vector[0], 0.6, places=6)
        self.assertAlmostEqual(vector[1], 0.8, places=6)
        self.assertAlmostEqual(sum(v * v for v in vector), 1.0, places=6)

    def test_embed_sentence_transformers_backend(self) -> None:
        provider = emb.LocalEmbeddingProvider(model="x")
        provider._encoder = _FakeSentenceTransformer([0.0, 1.0, 0.0])
        provider._backend = "sentence-transformers"
        vector = provider.embed("hi")
        self.assertEqual(vector, [0.0, 1.0, 0.0])
        self.assertEqual(provider.dimensions, 3)

    def test_embed_without_backend_raises_clear_error(self) -> None:
        provider = emb.LocalEmbeddingProvider()
        # No optional libs installed -> _ensure_encoder must raise a helpful error.
        with self.assertRaises(RuntimeError):
            provider.embed("boom")


if __name__ == "__main__":
    unittest.main()
