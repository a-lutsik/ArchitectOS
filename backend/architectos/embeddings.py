from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from hashlib import sha1
from typing import Any, Protocol

from .models import MemoryNode
from .ssl_util import urlopen

LOGGER = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional accel
    np = None  # type: ignore[assignment]
# Unicode-aware: ASCII-only tokenization dropped Cyrillic/other scripts from search.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Shared by lexical scoring and FTS so conversational wrappers do not drown
# the rare content terms that actually identify the answer.
QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "about", "from", "by",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has", "had",
    "what", "who", "when", "where", "why", "how", "which", "that", "this", "these", "those",
    "my", "me", "our", "your", "their", "its", "it", "as", "at", "into", "over", "under",
    "there", "here", "any", "some", "all", "just", "only", "also", "please", "can", "could",
    "would", "should", "will", "shall", "may", "might", "need", "needs", "want", "show", "tell",
    "find", "get", "give", "pull", "fetch", "open", "look", "see", "check", "list",
    "memory", "context", "note", "notes", "data", "details", "info", "information",
    "в", "во", "на", "по", "из", "от", "за", "к", "ко", "у", "о", "об", "обо", "со", "до", "при",
    "и", "а", "но", "или", "если", "что", "как", "кто", "где", "когда", "почему", "зачем",
    "какой", "какие", "какая", "какое", "это", "эта", "этот", "эти", "она", "он", "они", "мы", "вы",
    "про", "есть", "было", "были", "быть", "можно", "нужно", "только", "ещё", "еще", "уже", "ли",
    "мне", "мой", "моя", "мои", "наш", "ваш", "свой", "там", "тут", "здесь", "всё", "все",
    "говорила", "говорил", "говорили", "сказать", "сказал", "сказала",
    "памяти", "память", "памятью", "контекст", "контексте", "данные", "детали", "информацию",
    "подтяни", "подтянуть", "открой", "покажи", "найди", "дай", "скажи", "проверь",
})
# Small closed synonym bridges for retrieval only (not injected into prompts).
QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "uom": ("unit", "units", "power", "energy", "kwh", "mw", "conversion", "convert", "converted"),
    "баг": ("bug", "defect"),
    "баги": ("bug", "defect", "bugs"),
    "bug": ("defect", "баг"),
    "bugs": ("bug", "defect"),
    "defect": ("bug", "баг"),
}
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OLLAMA_EMBED_MODEL = "nomic-embed-text"
DEFAULT_GEMINI_EMBED_MODEL = "gemini-embedding-001"
DEFAULT_GEMINI_EMBED_DIMS = 768
DEFAULT_CLOUDFLARE_EMBED_MODEL = "@cf/baai/bge-m3"
DEFAULT_CLOUDFLARE_EMBED_DIMS = 1024
# On-device model used by LocalEmbeddingProvider (fastembed/sentence-transformers).
# Small, reliable, and downloaded once on first use; override via embedding_model.
DEFAULT_LOCAL_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_TEXT_CHAR_CAP = 12_000
GEMINI_EMBED_BASE = "https://generativelanguage.googleapis.com/v1beta"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

# Catalog of supported embedding backends + models (for UI / settings).
EMBEDDING_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "cloudflare": {
        "label": "Cloudflare Workers AI (free tier)",
        "multilingual": True,
        "free_tier": True,
        "env_keys": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
        "models": [
            {
                "id": "@cf/baai/bge-m3",
                "label": "@cf/baai/bge-m3",
                "dims": [1024],
                "default_dims": 1024,
                "notes": "Multilingual BGE-M3 on Cloudflare edge; 10k Neurons/day free.",
            },
            {
                "id": "@cf/baai/bge-base-en-v1.5",
                "label": "@cf/baai/bge-base-en-v1.5",
                "dims": [768],
                "default_dims": 768,
                "notes": "English-focused; cheaper neurons than large.",
            },
            {
                "id": "@cf/baai/bge-small-en-v1.5",
                "label": "@cf/baai/bge-small-en-v1.5",
                "dims": [384],
                "default_dims": 384,
                "notes": "Small English embeddings.",
            },
        ],
    },
    "gemini": {
        "label": "Google Gemini (free tier)",
        "multilingual": True,
        "free_tier": True,
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "models": [
            {
                "id": "gemini-embedding-001",
                "label": "gemini-embedding-001",
                "dims": [768, 1536, 3072],
                "default_dims": 768,
                "notes": "Text-only, strong multilingual, free AI Studio quota.",
            },
            {
                "id": "gemini-embedding-2",
                "label": "gemini-embedding-2",
                "dims": [768, 1536, 3072],
                "default_dims": 768,
                "notes": "Multimodal + multilingual; reindex required when switching from 001.",
            },
        ],
    },
    "openai": {
        "label": "OpenAI",
        "multilingual": True,
        "free_tier": False,
        "env_keys": ["OPENAI_API_KEY"],
        "models": [
            {"id": "text-embedding-3-small", "label": "text-embedding-3-small", "dims": [1536], "default_dims": 1536},
            {"id": "text-embedding-3-large", "label": "text-embedding-3-large", "dims": [3072], "default_dims": 3072},
        ],
    },
    "azure-openai": {
        "label": "Azure OpenAI",
        "multilingual": True,
        "free_tier": False,
        "env_keys": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"],
        "models": [
            {"id": "text-embedding-3-small", "label": "deployment name (e.g. text-embedding-3-small)", "dims": [1536], "default_dims": 1536},
            {"id": "text-embedding-3-large", "label": "deployment name (e.g. text-embedding-3-large)", "dims": [3072], "default_dims": 3072},
        ],
    },
    "ollama": {
        "label": "Ollama (local)",
        "multilingual": True,
        "free_tier": True,
        "env_keys": ["OLLAMA_HOST", "OLLAMA_EMBED_MODEL"],
        "models": [
            {"id": "bge-m3", "label": "bge-m3", "dims": [1024], "default_dims": 1024},
            {"id": "nomic-embed-text", "label": "nomic-embed-text", "dims": [768], "default_dims": 768},
            {"id": "mxbai-embed-large", "label": "mxbai-embed-large", "dims": [1024], "default_dims": 1024},
        ],
    },
    "local": {
        "label": "Local model (on-device, no keys)",
        "multilingual": True,
        "free_tier": True,
        "env_keys": [],
        "requires_extra": "embeddings",
        "models": [
            {"id": "BAAI/bge-small-en-v1.5", "label": "bge-small-en-v1.5", "dims": [384], "default_dims": 384, "notes": "Small English; ~130MB one-time download."},
            {"id": "intfloat/multilingual-e5-small", "label": "multilingual-e5-small", "dims": [384], "default_dims": 384, "notes": "Multilingual (incl. Russian); use for cross-lingual code/notes."},
            {"id": "BAAI/bge-base-en-v1.5", "label": "bge-base-en-v1.5", "dims": [768], "default_dims": 768, "notes": "Stronger English; larger download."},
        ],
    },
    "hash": {
        "label": "Hash (offline fallback)",
        "multilingual": False,
        "free_tier": True,
        "env_keys": [],
        "models": [{"id": "hash-256", "label": "hash-256", "dims": [256], "default_dims": 256}],
    },
}


def tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def content_tokens(text: str) -> list[str]:
    """Tokenize a query and drop conversational stopwords."""
    return [token for token in tokens(text) if token not in QUERY_STOPWORDS and len(token) >= 2]


def expand_query_terms(query_terms: list[str]) -> list[str]:
    """Dedupe query terms and attach closed synonym bridges used by retrieval."""
    expanded: list[str] = []
    seen: set[str] = set()
    for term in query_terms:
        key = str(term or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        expanded.append(key)
        for synonym in QUERY_SYNONYMS.get(key, ()):
            syn = str(synonym or "").strip().lower()
            if not syn or syn in seen:
                continue
            seen.add(syn)
            expanded.append(syn)
    return expanded


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def l2_normalize(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 0:
        return list(vector)
    return [value / length for value in vector]


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob[: count * 4]))


def embedding_provider_catalog() -> list[dict[str, Any]]:
    return [
        {"id": provider_id, **dict(meta)}
        for provider_id, meta in EMBEDDING_PROVIDER_CATALOG.items()
    ]


def _env_api_key(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


class EmbeddingProvider(Protocol):
    provider_id: str
    model: str
    dimensions: int

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        ...


class HashEmbeddingProvider:
    """Offline fallback — no API key required."""

    provider_id = "hash"
    model = "hash-256"

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        del purpose
        vector = [0.0] * self.dimensions
        for token in tokens(text):
            digest = sha1(token.encode("utf-8")).hexdigest()
            index = int(digest[:8], 16) % self.dimensions
            sign = 1.0 if int(digest[8], 16) % 2 == 0 else -1.0
            vector[index] += sign
        return l2_normalize(vector)


class OpenAICompatibleEmbeddingProvider:
    """OpenAI or Azure OpenAI /openai/v1 embeddings endpoint."""

    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str,
        base_url: str,
        model: str,
        api_key_header: str = "Authorization",
        api_key_prefix: str = "Bearer ",
    ) -> None:
        self.provider_id = provider_id
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or DEFAULT_EMBEDDING_MODEL
        self.api_key_header = api_key_header
        self.api_key_prefix = api_key_prefix
        self.dimensions = 0  # set after first embed

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        del purpose
        payload = {"model": self.model, "input": (text or "")[:EMBED_TEXT_CHAR_CAP]}
        url = f"{self.base_url}/embeddings" if not self.base_url.endswith("/embeddings") else self.base_url
        headers = {
            self.api_key_header: f"{self.api_key_prefix}{self.api_key}".strip(),
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=20.0, validate=False) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Embeddings HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Embeddings request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Embeddings request timed out.") from exc
        parsed = json.loads(raw)
        data = parsed.get("data") or []
        if not data:
            raise RuntimeError("Embeddings response did not include data.")
        vector = [float(item) for item in (data[0].get("embedding") or [])]
        if not vector:
            raise RuntimeError("Embeddings response vector was empty.")
        self.dimensions = len(vector)
        return vector


class OllamaEmbeddingProvider:
    provider_id = "ollama"

    def __init__(self, host: str, model: str) -> None:
        self.host = host.rstrip("/")
        self.model = model.strip() or DEFAULT_OLLAMA_EMBED_MODEL
        self.dimensions = 0

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        del purpose
        url = f"{self.host}/api/embeddings"
        payload = {"model": self.model, "prompt": (text or "")[:EMBED_TEXT_CHAR_CAP]}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=60.0, validate=False) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Ollama embeddings HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama embeddings failed: {exc.reason}") from exc
        parsed = json.loads(raw)
        vector = [float(item) for item in (parsed.get("embedding") or [])]
        if not vector:
            raise RuntimeError("Ollama embeddings response was empty.")
        self.dimensions = len(vector)
        return vector


def local_embeddings_available() -> bool:
    """True when an on-device embedding backend can be imported (no download)."""
    return (
        importlib.util.find_spec("fastembed") is not None
        or importlib.util.find_spec("sentence_transformers") is not None
    )


class LocalEmbeddingProvider:
    """On-device embeddings via fastembed (preferred) or sentence-transformers.

    Zero-config real vectors: install the optional ``[embeddings]`` extra and the
    model is selected automatically instead of the weak hash fallback. The model
    is downloaded once on first use and cached; construction stays cheap so
    provider selection never blocks or hits the network.
    """

    provider_id = "local"

    def __init__(self, model: str = "") -> None:
        self.model = (model or "").strip() or DEFAULT_LOCAL_EMBED_MODEL
        self.dimensions = 0  # set after first embed
        self._backend = ""  # "fastembed" | "sentence-transformers"
        self._encoder: Any = None
        self._lock = threading.Lock()

    def _ensure_encoder(self) -> None:
        if self._encoder is not None:
            return
        with self._lock:
            if self._encoder is not None:
                return
            # Prefer fastembed (ONNX, no torch); fall back to sentence-transformers.
            if importlib.util.find_spec("fastembed") is not None:
                try:
                    from fastembed import TextEmbedding

                    self._encoder = TextEmbedding(model_name=self.model)
                    self._backend = "fastembed"
                    return
                except Exception as exc:  # noqa: BLE001 - try the other backend
                    LOGGER.warning("fastembed init failed for %s: %s", self.model, exc)
            if importlib.util.find_spec("sentence_transformers") is not None:
                try:
                    from sentence_transformers import SentenceTransformer

                    self._encoder = SentenceTransformer(self.model)
                    self._backend = "sentence-transformers"
                    return
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("sentence-transformers init failed for %s: %s", self.model, exc)
            raise RuntimeError("No local embedding backend available (install the 'embeddings' extra).")

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        del purpose
        self._ensure_encoder()
        clean = (text or "")[:EMBED_TEXT_CHAR_CAP]
        if self._backend == "fastembed":
            vectors = list(self._encoder.embed([clean]))
            raw = vectors[0] if vectors else []
        else:
            raw = self._encoder.encode(clean, normalize_embeddings=True)
        vector = [float(value) for value in (raw.tolist() if hasattr(raw, "tolist") else list(raw))]
        if not vector:
            raise RuntimeError("Local embedding produced an empty vector.")
        self.dimensions = len(vector)
        # Normalize so cosine over the persisted matrix is consistent across backends.
        return l2_normalize(vector)


class GeminiEmbeddingProvider:
    """Google AI Studio / Gemini API embeddings (free tier, multilingual)."""

    provider_id = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_EMBED_MODEL,
        dimensions: int = DEFAULT_GEMINI_EMBED_DIMS,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = self._normalize_model(model)
        self.requested_dimensions = max(128, min(int(dimensions or DEFAULT_GEMINI_EMBED_DIMS), 3072))
        self.base_url = (base_url or GEMINI_EMBED_BASE).rstrip("/")
        self.dimensions = 0

    @staticmethod
    def _normalize_model(model: str) -> str:
        raw = str(model or DEFAULT_GEMINI_EMBED_MODEL).strip()
        if raw.startswith("models/"):
            raw = raw[len("models/") :]
        return raw or DEFAULT_GEMINI_EMBED_MODEL

    def _prepare_text(self, text: str, *, purpose: str) -> str:
        body = (text or "")[:EMBED_TEXT_CHAR_CAP]
        if self.model.startswith("gemini-embedding-2"):
            if purpose == "query":
                return f"task: search result | query: {body}"
            # Document side for asymmetric retrieval.
            title = "none"
            first_line = body.splitlines()[0].strip() if body else ""
            if first_line and len(first_line) < 120:
                title = first_line[:120]
            return f"title: {title} | text: {body}"
        return body

    def _task_type(self, purpose: str) -> str | None:
        # taskType is only for gemini-embedding-001 (not embedding-2).
        if not self.model.startswith("gemini-embedding-001"):
            return None
        if purpose == "query":
            return "RETRIEVAL_QUERY"
        return "RETRIEVAL_DOCUMENT"

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        model_path = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"{self.base_url}/{model_path}:embedContent"
        payload: dict[str, Any] = {
            "model": model_path,
            "content": {"parts": [{"text": self._prepare_text(text, purpose=purpose)}]},
            "outputDimensionality": self.requested_dimensions,
        }
        task_type = self._task_type(purpose)
        if task_type:
            payload["taskType"] = task_type
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
        )
        try:
            with urlopen(request, timeout=30.0, validate=False) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Gemini embeddings HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini embeddings failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Gemini embeddings request timed out.") from exc
        parsed = json.loads(raw)
        embedding = parsed.get("embedding") or {}
        values = embedding.get("values") if isinstance(embedding, dict) else None
        if values is None and isinstance(parsed.get("embeddings"), list) and parsed["embeddings"]:
            first = parsed["embeddings"][0] or {}
            values = first.get("values") if isinstance(first, dict) else None
        vector = [float(item) for item in (values or [])]
        if not vector:
            raise RuntimeError("Gemini embeddings response vector was empty.")
        # gemini-embedding-001 requires manual L2 normalize when truncating dims.
        if self.model.startswith("gemini-embedding-001") and len(vector) != 3072:
            vector = l2_normalize(vector)
        self.dimensions = len(vector)
        return vector


class CloudflareEmbeddingProvider:
    """Cloudflare Workers AI embeddings (bge-m3 etc.), free Neurons quota."""

    provider_id = "cloudflare"

    def __init__(
        self,
        *,
        api_token: str,
        account_id: str,
        model: str = DEFAULT_CLOUDFLARE_EMBED_MODEL,
    ) -> None:
        self.api_token = api_token.strip()
        self.account_id = account_id.strip()
        self.model = (model or DEFAULT_CLOUDFLARE_EMBED_MODEL).strip()
        if not self.model.startswith("@"):
            # Allow shorthand "bge-m3" / "baai/bge-m3"
            if "/" in self.model:
                self.model = f"@cf/{self.model.lstrip('/')}"
            else:
                self.model = f"@cf/baai/{self.model}"
        # Known dims for the default model so provider_info and the vector cache
        # can filter mixed legacy rows; 0 for custom models (dims learned on load).
        self.dimensions = DEFAULT_CLOUDFLARE_EMBED_DIMS if self.model == DEFAULT_CLOUDFLARE_EMBED_MODEL else 0

    def embed(self, text: str, *, purpose: str = "document") -> list[float]:
        del purpose
        if not self.api_token:
            raise RuntimeError("CLOUDFLARE_API_TOKEN is not set.")
        if not self.account_id:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is not set.")
        url = f"{CLOUDFLARE_API_BASE}/accounts/{self.account_id}/ai/run/{self.model}"
        payload = {"text": (text or "")[:EMBED_TEXT_CHAR_CAP]}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30.0, validate=False) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Cloudflare embeddings HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cloudflare embeddings failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Cloudflare embeddings request timed out.") from exc
        parsed = json.loads(raw)
        if parsed.get("success") is False:
            errors = parsed.get("errors") or []
            raise RuntimeError(f"Cloudflare embeddings error: {errors or parsed}")
        result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
        data = result.get("data") if isinstance(result, dict) else None
        vector: list[float] = []
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, list):
                vector = [float(item) for item in first]
            elif isinstance(first, (int, float)):
                vector = [float(item) for item in data]
        if not vector and isinstance(result, dict) and isinstance(result.get("shape"), list):
            # Some responses nest under result differently; fall through.
            pass
        if not vector:
            raise RuntimeError("Cloudflare embeddings response vector was empty.")
        self.dimensions = len(vector)
        return vector


def default_memory_retrieval_settings() -> dict[str, Any]:
    return {
        "embeddings_enabled": True,
        "embedding_provider": "auto",
        "embedding_model": "",
        "embedding_dimensions": DEFAULT_CLOUDFLARE_EMBED_DIMS,
        "vector_pool": 64,
        "vector_min_score": 0.22,
        "vector_query_timeout_ms": 2500,
        # Relevance floor for ranked search: a hit is dropped when its score falls
        # below this fraction of the best real hit's score. Trims the weak tail on
        # noise/near-empty queries (where static scope/type/source bonuses inflate
        # otherwise-irrelevant nodes) without touching strong, well-matched hits.
        "search_relevance_floor": 0.28,
        "reindex_on_startup": True,
    }


def build_embedding_provider(settings: dict[str, Any] | None = None) -> EmbeddingProvider:
    settings = dict(settings or {})
    # An explicit saved provider (non-auto) always wins. Otherwise a
    # MEMORY_EMBEDDING_PROVIDER env override takes precedence over the "auto"
    # default so operators/CI can pin a backend without editing settings.
    configured_mode = str(settings.get("embedding_provider") or "").strip().lower()
    env_mode = str(os.environ.get("MEMORY_EMBEDDING_PROVIDER") or "").strip().lower()
    if configured_mode and configured_mode != "auto":
        mode = configured_mode
    else:
        mode = env_mode or configured_mode or "auto"
    model = str(settings.get("embedding_model") or os.environ.get("MEMORY_EMBEDDING_MODEL") or "").strip()
    try:
        dims = int(settings.get("embedding_dimensions") or os.environ.get("MEMORY_EMBEDDING_DIMENSIONS") or DEFAULT_CLOUDFLARE_EMBED_DIMS)
    except (TypeError, ValueError):
        dims = DEFAULT_CLOUDFLARE_EMBED_DIMS

    def _cloudflare() -> CloudflareEmbeddingProvider | None:
        token = _env_api_key("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN")
        account = str(os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID") or "").strip()
        if not token or not account:
            return None
        return CloudflareEmbeddingProvider(
            api_token=token,
            account_id=account,
            model=model or str(os.environ.get("CLOUDFLARE_EMBED_MODEL") or DEFAULT_CLOUDFLARE_EMBED_MODEL),
        )

    def _gemini() -> GeminiEmbeddingProvider | None:
        key = _env_api_key("GEMINI_API_KEY", "GOOGLE_API_KEY")
        if not key:
            return None
        return GeminiEmbeddingProvider(
            api_key=key,
            model=model or str(os.environ.get("GEMINI_EMBED_MODEL") or DEFAULT_GEMINI_EMBED_MODEL),
            dimensions=dims,
            base_url=str(os.environ.get("GEMINI_API_BASE") or "").strip() or None,
        )

    def _openai() -> OpenAICompatibleEmbeddingProvider | None:
        key = _env_api_key("OPENAI_API_KEY")
        if not key:
            return None
        base = str(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        return OpenAICompatibleEmbeddingProvider(
            provider_id="openai",
            api_key=key,
            base_url=base,
            model=model or DEFAULT_EMBEDDING_MODEL,
        )

    def _azure() -> OpenAICompatibleEmbeddingProvider | None:
        key = _env_api_key("AZURE_OPENAI_API_KEY")
        endpoint = str(os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip()
        if not key or not endpoint:
            return None
        return OpenAICompatibleEmbeddingProvider(
            provider_id="azure-openai",
            api_key=key,
            base_url=endpoint,
            model=model
            or str(os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or DEFAULT_EMBEDDING_MODEL),
            api_key_header="api-key",
            api_key_prefix="",
        )

    def _ollama() -> OllamaEmbeddingProvider | None:
        host = str(os.environ.get("OLLAMA_HOST") or os.environ.get("OLLAMA_BASE_URL") or "").strip()
        if not host:
            return None
        return OllamaEmbeddingProvider(
            host=host if host.startswith("http") else f"http://{host}",
            model=model or str(os.environ.get("OLLAMA_EMBED_MODEL") or DEFAULT_OLLAMA_EMBED_MODEL),
        )

    def _local() -> LocalEmbeddingProvider | None:
        # On-device real vectors when the optional extra is installed. Selected in
        # `auto` ahead of the hash fallback so a fresh install gets real quality
        # without any API keys. Construction is cheap; the model loads lazily.
        if not local_embeddings_available():
            return None
        return LocalEmbeddingProvider(model=model or str(os.environ.get("MEMORY_LOCAL_EMBED_MODEL") or ""))

    if mode in {"", "auto"}:
        # Prefer free multilingual cloud, then paid, then a local model, then the
        # offline hash fallback (weak — only when nothing else is available).
        return _cloudflare() or _gemini() or _azure() or _openai() or _ollama() or _local() or HashEmbeddingProvider()

    if mode == "hash":
        return HashEmbeddingProvider()
    if mode in {"cloudflare", "cf", "workers-ai"}:
        return _cloudflare() or HashEmbeddingProvider()
    if mode in {"gemini", "google", "google-gemini"}:
        provider = _gemini()
        return provider or HashEmbeddingProvider()
    if mode in {"openai"}:
        return _openai() or HashEmbeddingProvider()
    if mode in {"azure", "azure-openai"}:
        return _azure() or HashEmbeddingProvider()
    if mode == "ollama":
        host = str(os.environ.get("OLLAMA_HOST") or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").strip()
        if not host.startswith("http"):
            host = f"http://{host}"
        return OllamaEmbeddingProvider(host=host, model=model or DEFAULT_OLLAMA_EMBED_MODEL)
    if mode in {"local", "fastembed", "sentence-transformers", "st", "onnx"}:
        return _local() or HashEmbeddingProvider()
    return HashEmbeddingProvider()


def node_embedding_text(node: MemoryNode) -> str:
    return f"{node.label or ''}\n{node.text or ''}".strip()


class _VectorIndex:
    """In-RAM cosine index. Prefer a float32 matrix when numpy is available."""

    __slots__ = ("node_ids", "scopes", "projects", "dims", "matrix", "vectors")

    def __init__(
        self,
        *,
        node_ids: list[str],
        scopes: list[str],
        projects: list[str],
        dims: int,
        matrix: Any = None,
        vectors: list[tuple[float, ...]] | None = None,
    ) -> None:
        self.node_ids = node_ids
        self.scopes = scopes
        self.projects = projects
        self.dims = dims
        self.matrix = matrix
        self.vectors = vectors or []

    def __len__(self) -> int:
        return len(self.node_ids)


class MemoryEmbeddingEngine:
    """Persisted vector index for semantic memory retrieval."""

    def __init__(self, repository, provider: EmbeddingProvider | None = None) -> None:
        self.repository = repository
        self.provider = provider or HashEmbeddingProvider()
        self._cache_lock = threading.Lock()
        self._vector_cache: _VectorIndex | None = None
        # Short-lived LRU for query embeddings: repeated chat/search queries
        # must not hit the embedding API every time.
        self._query_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()

    def _embed_query_cached(self, query: str, *, ttl_s: float = 300.0, maxsize: int = 32) -> list[float]:
        key = sha1(
            f"{getattr(self.provider, 'provider_id', '')}|{getattr(self.provider, 'model', '')}|{query}".encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        with self._cache_lock:
            entry = self._query_cache.get(key)
            if entry and now - entry[0] < ttl_s:
                self._query_cache.move_to_end(key)
                return list(entry[1])
        vector = self.provider.embed(query, purpose="query")
        with self._cache_lock:
            self._query_cache[key] = (now, list(vector))
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > maxsize:
                self._query_cache.popitem(last=False)
        return vector

    def settings(self) -> dict[str, Any]:
        configured = dict(self.repository.get_setting("memory_retrieval") or {})
        merged = default_memory_retrieval_settings()
        merged.update({key: configured[key] for key in merged if key in configured})
        merged["embeddings_enabled"] = bool(merged.get("embeddings_enabled", True))
        merged["vector_pool"] = max(8, min(int(merged.get("vector_pool") or 64), 200))
        merged["vector_min_score"] = float(merged.get("vector_min_score") or 0.22)
        try:
            raw_floor: Any = merged.get("search_relevance_floor")
            merged["search_relevance_floor"] = max(0.0, min(float(raw_floor), 0.9))
        except (TypeError, ValueError):
            merged["search_relevance_floor"] = 0.28
        try:
            merged["vector_query_timeout_ms"] = max(50, min(int(merged.get("vector_query_timeout_ms") or 2500), 5000))
        except (TypeError, ValueError):
            merged["vector_query_timeout_ms"] = 2500
        try:
            merged["embedding_dimensions"] = max(128, min(int(merged.get("embedding_dimensions") or DEFAULT_GEMINI_EMBED_DIMS), 3072))
        except (TypeError, ValueError):
            merged["embedding_dimensions"] = DEFAULT_GEMINI_EMBED_DIMS
        return merged

    def enabled(self) -> bool:
        return bool(self.settings().get("embeddings_enabled"))

    def provider_info(self) -> dict[str, Any]:
        dims = int(getattr(self.provider, "dimensions", 0) or 0)
        return {
            "provider": getattr(self.provider, "provider_id", "unknown"),
            "model": getattr(self.provider, "model", ""),
            "dimensions": dims,
            "enabled": self.enabled(),
            "numpy": bool(np is not None),
        }

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        settings = self.settings()
        coverage = {}
        try:
            coverage = self.repository.memory_embedding_coverage(project_id)
        except Exception as exc:  # pragma: no cover - defensive for older DBs
            LOGGER.warning("embedding coverage unavailable: %s", exc)
            coverage = {"active_nodes": 0, "indexed": 0, "missing": 0, "coverage_pct": 0.0, "by_provider": []}
        return {
            **self.provider_info(),
            "settings": settings,
            "coverage": coverage,
            "catalog": embedding_provider_catalog(),
            "env": {
                "cloudflare_token": bool(_env_api_key("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN")),
                "cloudflare_account": bool(str(os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID") or "").strip()),
                "gemini_key": bool(_env_api_key("GEMINI_API_KEY", "GOOGLE_API_KEY")),
                "openai_key": bool(_env_api_key("OPENAI_API_KEY")),
                "azure_key": bool(_env_api_key("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT")),
                "ollama_host": bool(str(os.environ.get("OLLAMA_HOST") or os.environ.get("OLLAMA_BASE_URL") or "").strip()),
            },
        }

    def invalidate_vector_cache(self) -> None:
        with self._cache_lock:
            self._vector_cache = None

    def _update_vector_cache(self, *, node_id: str, scope: str, project_id: str, vector: list[float]) -> bool:
        """Apply one freshly indexed row to the live cache. False → caller must invalidate."""
        with self._cache_lock:
            index = self._vector_cache
            if index is None or index.dims != len(vector):
                return False
            if node_id in index.node_ids:
                position = index.node_ids.index(node_id)
                index.scopes[position] = scope
                index.projects[position] = project_id
                if index.matrix is not None and np is not None:
                    index.matrix[position] = np.asarray(vector, dtype=np.float32)
                else:
                    index.vectors[position] = tuple(vector)
                return True
            index.node_ids.append(node_id)
            index.scopes.append(scope)
            index.projects.append(project_id)
            if index.matrix is not None and np is not None:
                index.matrix = np.vstack([index.matrix, np.asarray(vector, dtype=np.float32)])
            else:
                index.vectors.append(tuple(vector))
            return True

    def _load_vector_cache(self) -> _VectorIndex:
        with self._cache_lock:
            if self._vector_cache is not None:
                return self._vector_cache
        rows = self.repository.list_memory_embedding_rows()
        # Prefer the active provider dimensionality so mixed legacy rows are ignored.
        preferred_dims = int(getattr(self.provider, "dimensions", 0) or 0)
        node_ids: list[str] = []
        scopes: list[str] = []
        projects: list[str] = []
        vectors: list[list[float]] = []
        dims = preferred_dims
        for row in rows:
            vector = unpack_vector(row.get("vector") or b"")
            if not vector:
                continue
            row_dims = int(row.get("dimensions") or len(vector))
            if preferred_dims and row_dims != preferred_dims:
                continue
            if not dims:
                dims = row_dims
            if row_dims != dims or len(vector) != dims:
                continue
            node_ids.append(str(row.get("node_id") or ""))
            scopes.append(str(row.get("scope") or ""))
            projects.append(str(row.get("project_id") or ""))
            vectors.append(list(vector))
        if np is not None and vectors:
            matrix = np.asarray(vectors, dtype=np.float32)
            index = _VectorIndex(
                node_ids=node_ids,
                scopes=scopes,
                projects=projects,
                dims=dims,
                matrix=matrix,
            )
        else:
            index = _VectorIndex(
                node_ids=node_ids,
                scopes=scopes,
                projects=projects,
                dims=dims,
                vectors=[tuple(v) for v in vectors],
            )
        with self._cache_lock:
            self._vector_cache = index
            return index

    def index_node(self, node: MemoryNode, *, invalidate: bool = True) -> bool:
        if not self.enabled():
            return False
        if node.status != "active":
            self.repository.delete_memory_embedding(node.id)
            if invalidate:
                self.invalidate_vector_cache()
            return False
        text = node_embedding_text(node)
        if len(text) < 4:
            return False
        try:
            vector = self.provider.embed(text, purpose="document")
        except Exception as exc:
            LOGGER.warning("memory embedding index failed for %s: %s", node.id, exc)
            return False
        self.repository.upsert_memory_embedding(
            node_id=node.id,
            scope=node.scope,
            project_id=node.project_id,
            status=node.status,
            provider_id=str(getattr(self.provider, "provider_id", "hash")),
            model=str(getattr(self.provider, "model", "")),
            dimensions=len(vector),
            vector=vector,
        )
        if invalidate:
            # Cheap path: patch the live in-RAM index instead of reloading every vector.
            if not self._update_vector_cache(
                node_id=node.id,
                scope=node.scope or "",
                project_id=str(node.project_id or ""),
                vector=vector,
            ):
                self.invalidate_vector_cache()
        return True

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 64,
        min_score: float | None = None,
    ) -> list[str]:
        return [node_id for node_id, _ in self.search_scored(
            query,
            project_id=project_id,
            scope=scope,
            limit=limit,
            min_score=min_score,
        )]

    @staticmethod
    def _passes_scope_filter(
        node_scope: str,
        node_project: str,
        *,
        project_id: str | None,
        scope: str | None,
    ) -> bool:
        if scope:
            if node_scope != scope:
                return False
            if scope in {"project", "interface"} and project_id and node_project != project_id:
                return False
            return True
        if project_id:
            if node_scope not in {"shared", "global"} and node_project and node_project != project_id:
                return False
        return True

    def search_scored(
        self,
        query: str,
        *,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 64,
        min_score: float | None = None,
    ) -> list[tuple[str, float]]:
        if not self.enabled():
            return []
        query = str(query or "").strip()
        if not query:
            return []
        settings = self.settings()
        try:
            query_vector = self._embed_query_cached(query)
        except Exception as exc:
            LOGGER.warning("memory embedding query failed: %s", exc)
            return []
        threshold = float(min_score if min_score is not None else settings["vector_min_score"])
        limit = max(1, min(int(limit or 64), 200))
        dims = len(query_vector)
        if dims <= 0:
            return []
        index = self._load_vector_cache()
        if not len(index) or index.dims != dims:
            return []

        if index.matrix is not None and np is not None:
            q = np.asarray(query_vector, dtype=np.float32)
            q_norm = float(np.linalg.norm(q)) or 1.0
            scores = index.matrix @ (q / q_norm)
            # Build eligibility mask without scanning scores twice.
            if scope or project_id:
                eligible = [
                    i for i, (node_scope, node_project) in enumerate(zip(index.scopes, index.projects, strict=True))
                    if self._passes_scope_filter(node_scope, node_project, project_id=project_id, scope=scope)
                ]
                if not eligible:
                    return []
                eligible_arr = np.asarray(eligible, dtype=np.int32)
                sub = scores[eligible_arr]
                keep = sub >= threshold
                if not bool(np.any(keep)):
                    return []
                cand = eligible_arr[keep]
                cand_scores = sub[keep]
            else:
                keep = scores >= threshold
                if not bool(np.any(keep)):
                    return []
                cand = np.flatnonzero(keep)
                cand_scores = scores[cand]
            if cand_scores.size > limit:
                top = np.argpartition(-cand_scores, limit - 1)[:limit]
                cand = cand[top]
                cand_scores = cand_scores[top]
            order = np.argsort(-cand_scores)
            return [(index.node_ids[int(cand[i])], float(cand_scores[i])) for i in order]

        # Pure-Python fallback (no numpy).
        q_norm = math.sqrt(sum(v * v for v in query_vector)) or 1.0
        ranked: list[tuple[str, float]] = []
        for node_id, node_scope, node_project, vector in zip(
            index.node_ids, index.scopes, index.projects, index.vectors, strict=True
        ):
            if len(vector) != dims:
                continue
            if not self._passes_scope_filter(node_scope, node_project, project_id=project_id, scope=scope):
                continue
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True)) / q_norm
            if score >= threshold:
                ranked.append((node_id, float(score)))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:limit]

    def rebuild_all(self) -> dict[str, Any]:
        indexed = 0
        skipped = 0
        errors = 0
        for node in self.repository.list_nodes():
            if node.status != "active":
                self.repository.delete_memory_embedding(node.id)
                skipped += 1
                continue
            if self.index_node(node, invalidate=False):
                indexed += 1
            else:
                errors += 1
        self.invalidate_vector_cache()
        return {"indexed": indexed, "skipped": skipped, "errors": errors, **self.provider_info()}

    def ensure_indexed(self) -> dict[str, Any]:
        """Index active nodes that have no embedding row yet."""
        if not self.enabled():
            return {"indexed": 0, "pending": 0}
        missing = self.repository.list_nodes_missing_embeddings()
        indexed = 0
        for node in missing:
            if self.index_node(node, invalidate=False):
                indexed += 1
        if indexed:
            self.invalidate_vector_cache()
        return {"indexed": indexed, "pending": max(0, len(missing) - indexed), **self.provider_info()}
