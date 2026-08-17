from __future__ import annotations

import logging
import math
import os
import re
import struct
import threading
import time
from collections import OrderedDict
from hashlib import sha1
from typing import Any

from .embedding_providers import (
    CLOUDFLARE_API_BASE,
    DEFAULT_CLOUDFLARE_EMBED_DIMS,
    DEFAULT_CLOUDFLARE_EMBED_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_GEMINI_EMBED_DIMS,
    DEFAULT_GEMINI_EMBED_MODEL,
    DEFAULT_LOCAL_EMBED_MODEL,
    DEFAULT_OLLAMA_EMBED_MODEL,
    EMBED_TEXT_CHAR_CAP,
    EMBEDDING_PROVIDER_CATALOG,
    GEMINI_EMBED_BASE,
    CloudflareEmbeddingProvider,
    EmbeddingProvider,
    GeminiEmbeddingProvider,
    HashEmbeddingProvider,
    LocalEmbeddingProvider,
    OllamaEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    _env_api_key,
    build_embedding_provider,
    default_memory_retrieval_settings,
    embedding_provider_catalog,
    local_embeddings_available,
)
from .models import MemoryNode

# Keep provider symbols importable from ``embeddings`` after the split.
__all__ = [
    "CLOUDFLARE_API_BASE",
    "DEFAULT_CLOUDFLARE_EMBED_DIMS",
    "DEFAULT_CLOUDFLARE_EMBED_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_GEMINI_EMBED_DIMS",
    "DEFAULT_GEMINI_EMBED_MODEL",
    "DEFAULT_LOCAL_EMBED_MODEL",
    "DEFAULT_OLLAMA_EMBED_MODEL",
    "EMBEDDING_PROVIDER_CATALOG",
    "EMBED_TEXT_CHAR_CAP",
    "GEMINI_EMBED_BASE",
    "QUERY_STOPWORDS",
    "QUERY_SYNONYMS",
    "TOKEN_RE",
    "CloudflareEmbeddingProvider",
    "EmbeddingProvider",
    "GeminiEmbeddingProvider",
    "HashEmbeddingProvider",
    "LocalEmbeddingProvider",
    "MemoryEmbeddingEngine",
    "OllamaEmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "build_embedding_provider",
    "content_tokens",
    "cosine",
    "default_memory_retrieval_settings",
    "embedding_provider_catalog",
    "expand_query_terms",
    "l2_normalize",
    "local_embeddings_available",
    "node_embedding_text",
    "pack_vector",
    "tokens",
    "unpack_vector",
]

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
