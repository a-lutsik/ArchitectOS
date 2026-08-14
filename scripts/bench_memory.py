"""Memory mechanism benchmark / metrics.

Part A: runs against a COPY of the real data/architectos.db (search bumps
lifecycle access stats, so we never touch the original).
Part B: synthetic scale test (fresh corpus of N nodes) to show latency scaling.

Default mode forces deterministic offline hash embeddings. `--real-vectors`
loads ./.env and keeps the provider configured in the DB (e.g. Cloudflare
bge-m3), so the bench measures the production hybrid path against the persisted
1024d vectors; only the ~dozens of bench queries hit the embedding API.

Usage:
    /usr/local/bin/python3.12 scripts/bench_memory.py [--nodes 3000] [--queries 24] [--real-vectors]

Before/after quality of the on-device local model (LocalEmbeddingProvider):
    1. Baseline (hash):   python3 scripts/bench_memory.py            # default forces hash
    2. Install backend:   pip install -e ".[embeddings]"            # fastembed
    3. Switch provider:   Settings -> Embeddings -> "Local model"   # (or set
       memory_retrieval.embedding_provider = "local" in the DB). The service
       reindexes the corpus with the local model on the provider change; wait
       until embedding coverage reads ~100%.
    4. Local vectors:     python3 scripts/bench_memory.py --real-vectors
   Because doc and query vectors must share the same model, comparing a local
   query embedding against a hash-indexed corpus is meaningless — always reindex
   before benchmarking a new provider. Part B (synthetic scale) is network-free
   and measures latency only, independent of provider quality.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.architectos.embeddings import HashEmbeddingProvider  # noqa: E402
from backend.architectos.service import ArchitectOSService  # noqa: E402


def load_env_file(path: Path) -> None:
    """Same semantics as the service env loader: never override existing vars."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def force_hash_embeddings(service: ArchitectOSService) -> None:
    """Deterministic offline mode even when the shell exports real API creds.

    The persisted DB setting (embedding_provider=cloudflare) wins over the
    MEMORY_EMBEDDING_PROVIDER env var, so the swap must happen on the instance.
    """
    provider = HashEmbeddingProvider()
    service.embedding_provider = provider
    service.memory_embeddings.provider = provider
    service.memory_embeddings.invalidate_vector_cache()


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return round(values[idx], 1)


def edge_audit(nodes: list, edges: list) -> dict:
    """Edge quality audit: degree spread, isolation, hub concentration.

    A few percent of nodes holding most edges means the auto-linker is wiring
    everything through hubs — neighbors stop being informative.
    """
    degree: Counter = Counter()
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1
    label_by_id = {n.id: str(n.label or "") for n in nodes}
    degrees = sorted(float(degree[n.id]) for n in nodes)
    top = degree.most_common(10)
    top_ids = {node_id for node_id, _ in top}
    hub_edges = sum(1 for edge in edges if edge.source in top_ids or edge.target in top_ids)
    return {
        "edges_per_node": round(len(edges) / max(1, len(nodes)), 2),
        "isolated_nodes": sum(1 for n in nodes if degree[n.id] == 0),
        "degree": {"p50": pct(degrees, 0.5), "p95": pct(degrees, 0.95), "max": int(degrees[-1]) if degrees else 0},
        "top10_edge_share_pct": round(100.0 * hub_edges / max(1, len(edges)), 1),
        "top_hubs": [
            {"label": label_by_id.get(node_id, node_id)[:80], "degree": deg}
            for node_id, deg in top[:5]
        ],
    }


def corpus_stats(service: ArchitectOSService) -> dict:
    repo = service.repository
    nodes = repo.list_nodes()
    by_status = Counter(n.status for n in nodes)
    by_type = Counter(n.type for n in nodes)
    by_scope = Counter(n.scope for n in nodes)
    by_source = Counter(str((n.metadata or {}).get("source") or (n.metadata or {}).get("source_type") or "?") for n in nodes)
    text_lens = sorted(len(n.text or "") for n in nodes)
    tiers = Counter(str((n.metadata or {}).get("memory_tier") or "none") for n in nodes)
    states = Counter(str((n.metadata or {}).get("lifecycle_state") or "none") for n in nodes)
    scores = [float((n.metadata or {}).get("memory_score") or 0) for n in nodes]
    dups = sum(1 for n in nodes if (n.metadata or {}).get("duplicate"))
    edges = repo.list_edges()
    edge_types = Counter(e.type for e in edges)
    counts = repo.count_memory_candidates_by_status(None)
    coverage = repo.memory_embedding_coverage(None)
    return {
        "nodes": len(nodes),
        "by_status": dict(by_status),
        "by_type": dict(by_type.most_common(12)),
        "by_scope": dict(by_scope),
        "by_source_top": dict(by_source.most_common(12)),
        "text_len_chars": {"p50": pct([float(v) for v in text_lens], 0.5), "p95": pct([float(v) for v in text_lens], 0.95), "max": text_lens[-1] if text_lens else 0},
        "lifecycle_tier": dict(tiers),
        "lifecycle_state": dict(states),
        "memory_score": {"p50": pct(scores, 0.5), "p95": pct(scores, 0.95)},
        "duplicates": dups,
        "edges": len(edges),
        "edge_types": dict(edge_types.most_common(10)),
        "edge_audit": edge_audit(nodes, edges),
        "candidates": counts,
        "embedding_coverage": coverage,
    }


def search_bench(service: ArchitectOSService, queries: list[str]) -> dict:
    totals, fts_ms_list, vec_ms_list = [], [], []
    modes = Counter()
    top_scores, hit_counts = [], []
    fts_hit_counts, vec_hit_counts = [], []
    for query in queries:
        started = time.perf_counter()
        result = service.search_memory(query, project_id=None, limit=8)
        totals.append((time.perf_counter() - started) * 1000.0)
        retrieval = result.get("retrieval") or {}
        modes[retrieval.get("mode") or "?"] += 1
        vec_ms_list.append(float(retrieval.get("vector_ms") or 0.0))
        fts_hit_counts.append(int(retrieval.get("fts_hits") or 0))
        vec_hit_counts.append(int(retrieval.get("vector_hits") or 0))
        hits = result.get("hits") or []
        hit_counts.append(len(hits))
        if hits:
            top_scores.append(float(hits[0].get("score") or 0.0))
    return {
        "queries": len(queries),
        "total_ms": {"p50": pct(totals, 0.5), "p95": pct(totals, 0.95), "max": round(max(totals, default=0.0), 1)},
        "vector_ms": {"p50": pct(vec_ms_list, 0.5), "p95": pct(vec_ms_list, 0.95)},
        "modes": dict(modes),
        "fts_hits_avg": round(statistics.mean(fts_hit_counts), 1) if fts_hit_counts else 0,
        "vector_hits_avg": round(statistics.mean(vec_hit_counts), 1) if vec_hit_counts else 0,
        "hits_avg": round(statistics.mean(hit_counts), 1) if hit_counts else 0,
        "empty_results": sum(1 for c in hit_counts if c == 0),
        "top_score": {"p50": pct(top_scores, 0.5), "p95": pct(top_scores, 0.95)},
    }


def relevance_bench(service: ArchitectOSService, count: int) -> dict:
    """Retrieval quality: query = leading words of a node's label, expected hit = that node.

    Reports hit@1, hit@8, MRR and zero-hit rate over the sampled nodes.
    """
    nodes = [n for n in service.repository.list_nodes() if n.status == "active" and len((n.label or "").split()) >= 3]
    rng = random.Random(13)
    picked = rng.sample(nodes, min(count, len(nodes))) if nodes else []
    hit1 = hit8 = zero = 0
    ranks: list[float] = []
    for node in picked:
        query = " ".join((node.label or "").split()[:5])
        hits = service.search_memory(query, project_id=None, limit=8).get("hits") or []
        ids = [str((h.get("node") or {}).get("id") or h.get("id") or "") for h in hits]
        if not ids:
            zero += 1
        if node.id in ids:
            rank = ids.index(node.id) + 1
            ranks.append(1.0 / rank)
            if rank == 1:
                hit1 += 1
            hit8 += 1
    total = max(1, len(picked))
    return {
        "samples": len(picked),
        "hit@1": round(hit1 / total, 3),
        "hit@8": round(hit8 / total, 3),
        "mrr": round(sum(ranks) / total, 3),
        "zero_result_rate": round(zero / total, 3),
    }


def gold_eval(service: ArchitectOSService, gold: list[dict]) -> dict:
    """Quality on real user-style queries (scripts/bench_gold_queries.json).

    Unlike relevance_bench (label self-retrieval), queries here are written the
    way a user would ask — cross-lingual, paraphrased, scenario-style — and a hit
    counts when the expected label substring appears in the returned labels.
    """
    hit1 = hit8 = zero = 0
    ranks: list[float] = []
    misses: list[dict] = []
    for item in gold:
        query = str(item.get("query") or "")
        expect = str(item.get("expect") or "").lower()
        if not query or not expect:
            continue
        hits = service.search_memory(query, project_id=None, limit=8).get("hits") or []
        labels = [str((h.get("node") or {}).get("label") or "").lower() for h in hits]
        if not labels:
            zero += 1
        rank = next((index + 1 for index, label in enumerate(labels) if expect in label), 0)
        if rank:
            ranks.append(1.0 / rank)
            if rank == 1:
                hit1 += 1
            hit8 += 1
        else:
            misses.append({"query": query, "expect": expect, "top_labels": [label[:70] for label in labels[:3]]})
    total = max(1, len(gold))
    return {
        "queries": len(gold),
        "hit@1": round(hit1 / total, 3),
        "hit@8": round(hit8 / total, 3),
        "mrr": round(sum(ranks) / total, 3),
        "zero_result_rate": round(zero / total, 3),
        "misses": misses,
    }


def sample_queries(service: ArchitectOSService, count: int) -> list[str]:
    nodes = [n for n in service.repository.list_nodes() if n.status == "active" and len((n.label or "").split()) >= 2]
    rng = random.Random(42)
    picked = rng.sample(nodes, min(count, len(nodes)))
    queries = [" ".join((n.label or "").split()[:5]) for n in picked]
    queries += [
        "архитектура аутентификации",
        "token rotation policy",
        "какие решения приняли на встрече",
        "deployment rollback procedure",
    ]
    return queries


def synthetic_scale(node_count: int, query_count: int) -> dict:
    rng = random.Random(7)
    words = "auth token cache deploy session pipeline vector memory graph search embed queue lifecycle decay archive promote scope project interface decision constraint lesson".split()
    with tempfile.TemporaryDirectory() as tmp:
        service = ArchitectOSService(Path(tmp))
        t0 = time.perf_counter()
        for i in range(node_count):
            label = " ".join(rng.choices(words, k=3)) + f" {i}"
            text = " ".join(rng.choices(words, k=30))
            service.repository.add_node("Lesson", label, "project", text, "bench", None, 0.7, {"source": "bench"})
        ingest_ms = (time.perf_counter() - t0) * 1000.0
        queries = [" ".join(rng.choices(words, k=3)) for _ in range(query_count)]
        bench = search_bench(service, queries)
        bench["nodes"] = node_count
        bench["ingest_ms_total"] = round(ingest_ms, 1)
        bench["ingest_ms_per_node"] = round(ingest_ms / max(1, node_count), 2)
        return bench


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=3000)
    parser.add_argument("--queries", type=int, default=24)
    parser.add_argument("--skip-real", action="store_true")
    parser.add_argument(
        "--real-vectors",
        action="store_true",
        help="Use the DB-configured embedding provider (loads ./.env) instead of forcing hash — "
        "measures the production hybrid path against persisted vectors; bench queries hit the embedding API.",
    )
    args = parser.parse_args()

    if args.real_vectors:
        os.environ.pop("MEMORY_EMBEDDING_PROVIDER", None)
        load_env_file(Path(".env"))
    else:
        os.environ["MEMORY_EMBEDDING_PROVIDER"] = "hash"

    report: dict = {}

    if not args.skip_real:
        real_db = Path("data/architectos.db")
        if real_db.exists():
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                data_dir.mkdir()
                shutil.copy2(real_db, data_dir / "architectos.db")
                t0 = time.perf_counter()
                service = ArchitectOSService(Path(tmp))
                if not args.real_vectors:
                    force_hash_embeddings(service)
                report["real"] = {"startup_s": round(time.perf_counter() - t0, 2)}
                report["real"]["embedding_mode"] = "real-vectors" if args.real_vectors else "hash-forced"
                report["real"]["provider"] = service.memory_embeddings.provider_info()
                report["real"]["corpus"] = corpus_stats(service)
                t0 = time.perf_counter()
                service.memory_embeddings._load_vector_cache()  # noqa: SLF001
                report["real"]["vector_cache_cold_load_s"] = round(time.perf_counter() - t0, 2)
                cache = service.memory_embeddings._vector_cache  # noqa: SLF001
                report["real"]["vector_cache"] = {
                    "rows": len(cache) if cache else 0,
                    "dims": cache.dims if cache else 0,
                    "ram_mb_estimate": round((len(cache) * cache.dims * 4) / 1e6, 1) if cache else 0,
                }
                queries = sample_queries(service, args.queries)
                report["real"]["search"] = search_bench(service, queries)
                report["real"]["relevance"] = relevance_bench(service, args.queries)
                gold_path = Path(__file__).resolve().parent / "bench_gold_queries.json"
                gold = json.loads(gold_path.read_text(encoding="utf-8")) if gold_path.is_file() else []
                report["real"]["gold_eval"] = gold_eval(service, gold) if gold else {"error": "bench_gold_queries.json missing"}
                report["real"]["queries_sample"] = queries[:6]
                t0 = time.perf_counter()
                decay = service.memory_lifecycle.run_decay(None, dry_run=True)
                report["real"]["decay_dry_run"] = {
                    "changed": decay.get("changed"),
                    "elapsed_s": round(time.perf_counter() - t0, 2),
                    "actions": dict(Counter(item["action"] for item in decay.get("items") or [])),
                }
        else:
            report["real"] = {"error": "data/architectos.db not found"}

    # Synthetic corpora always use hash embeddings: they are generated offline
    # and must stay network-free regardless of --real-vectors.
    os.environ["MEMORY_EMBEDDING_PROVIDER"] = "hash"
    report["synthetic"] = {
        "scale_1k": synthetic_scale(1000, args.queries),
        f"scale_{args.nodes // 1000}k": synthetic_scale(args.nodes, args.queries),
    }

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
