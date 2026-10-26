"""Reproducible retrieval evals for ArchitectOS memory.

Builds a fresh, throwaway workspace from a public fixture corpus
(``evals/retrieval/corpus.json``), runs graded queries
(``evals/retrieval/queries.json``) through the real service code paths and
reports standard IR metrics plus safety checks.

Unlike ``scripts/bench_memory.py`` (which measures a private
``data/architectos.db``), everything here is checked in, so the numbers are
reproducible on any machine and can gate CI.

Retrieval modes (each runs on its own fresh corpus, so lifecycle access bumps
from one mode never leak into another):

- ``lexical``      FTS5/BM25 only (vector search disabled)
- ``vector``       embeddings only (FTS disabled)
- ``hybrid``       production ``search_memory`` (FTS + vector, RRF)
- ``hybrid_graph`` ``search_memory(expand_graph=True)`` (PageRank expansion)
- ``context``      ``service.context()`` — the pack an agent actually receives

Metrics (per mode, overall and per category):

- ``answer@1``   top hit is a grade-2 ("the answer") node
- ``recall@5`` / ``recall@10``   share of relevant (grade >= 1) nodes retrieved
- ``mrr``        reciprocal rank of the first grade-2 node
- ``ndcg@10``    graded nDCG with gain 2^grade - 1
- ``context_recall`` (context mode)  share of grade-2 nodes present in the pack

Safety checks: superseded / other-project nodes returned (``forbidden``),
noise queries returning anything, secrets surviving redaction (in storage, in
hits, in packs), pack size over budget.

Usage::

    python -m evals.retrieval_eval                     # hash embeddings, all modes
    python -m evals.retrieval_eval --provider local    # on-device model (needs .[embeddings])
    python -m evals.retrieval_eval --provider configured  # provider from ./.env (e.g. Cloudflare)
    python -m evals.retrieval_eval --md report.md --json report.json --show-misses
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL_DIR = Path(__file__).resolve().parent / "retrieval"
CORPUS_PATH = EVAL_DIR / "corpus.json"
QUERIES_PATH = EVAL_DIR / "queries.json"

MODES = ("lexical", "vector", "hybrid", "hybrid_graph", "context")
RANKED_CATEGORIES_EXCLUDED = {"noise"}
SEARCH_LIMIT = 10


# --------------------------------------------------------------------------- metrics


def _ndcg(ranked_keys: list[str | None], relevant: dict[str, int], k: int) -> float:
    def gain(grade: int) -> float:
        return float(2**grade - 1)

    dcg = 0.0
    for index, key in enumerate(ranked_keys[:k]):
        grade = relevant.get(key or "", 0)
        if grade:
            dcg += gain(grade) / math.log2(index + 2)
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(gain(grade) / math.log2(index + 2) for index, grade in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def score_ranking(ranked_keys: list[str | None], relevant: dict[str, int]) -> dict[str, float]:
    """IR metrics for one query. ``ranked_keys`` holds corpus keys (None = non-corpus node)."""
    rel = {key for key, grade in relevant.items() if grade >= 1}
    answers = {key for key, grade in relevant.items() if grade >= 2}

    def recall_at(k: int) -> float:
        if not rel:
            return 0.0
        return len(rel & {key for key in ranked_keys[:k] if key}) / len(rel)

    first_answer = next((index + 1 for index, key in enumerate(ranked_keys) if key in answers), 0)
    return {
        "answer@1": 1.0 if ranked_keys and ranked_keys[0] in answers else 0.0,
        "recall@5": recall_at(5),
        "recall@10": recall_at(10),
        "mrr": 1.0 / first_answer if first_answer else 0.0,
        "ndcg@10": _ndcg(ranked_keys, relevant, 10),
    }


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _mean(values: list[float]) -> float:
    return round(statistics.mean(values), 3) if values else 0.0


# --------------------------------------------------------------------------- corpus


@dataclass
class LoadedCorpus:
    service: Any
    main_project: str
    key_to_id: dict[str, str]
    id_to_key: dict[str, str]
    secrets: list[dict[str, Any]]
    tmp: tempfile.TemporaryDirectory = field(repr=False)

    def close(self) -> None:
        self.tmp.cleanup()


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _configure_provider_env(provider: str) -> None:
    if provider == "hash":
        os.environ["MEMORY_EMBEDDING_PROVIDER"] = "hash"
    elif provider == "local":
        os.environ["MEMORY_EMBEDDING_PROVIDER"] = "local"
    elif provider == "configured":
        os.environ.pop("MEMORY_EMBEDDING_PROVIDER", None)
        env_file = ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                item = line.strip()
                if not item or item.startswith("#") or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    else:
        raise ValueError(f"unknown provider: {provider}")


def build_corpus(corpus: dict[str, Any], provider: str = "hash") -> LoadedCorpus:
    """Load the fixture into a fresh temp workspace through the production write path."""
    _configure_provider_env(provider)
    from backend.architectos.service import ArchitectOSService

    tmp = tempfile.TemporaryDirectory(prefix="aos-eval-")
    service = ArchitectOSService(Path(tmp.name))
    if provider == "hash":
        # A persisted DB setting can override the env var; pin the instance too.
        from backend.architectos.embeddings import HashEmbeddingProvider

        hash_provider = HashEmbeddingProvider()
        service.embedding_provider = hash_provider
        service.memory_embeddings.provider = hash_provider
        service.memory_embeddings.invalidate_vector_cache()

    main_project = str(corpus["main_project"])
    key_to_id: dict[str, str] = {}
    for spec in corpus["nodes"]:
        created = service.add_memory({
            "project_id": spec.get("project") or main_project,
            "scope": spec.get("scope") or "project",
            "type": spec["type"],
            "label": spec["label"],
            "text": spec["text"],
            "source": spec.get("source") or "eval",
            "dedup": False,
        })
        key_to_id[spec["key"]] = str(created["id"])
    for edge in corpus.get("edges") or []:
        service.create_graph_edge({
            "source": key_to_id[edge["source"]],
            "target": key_to_id[edge["target"]],
            "type": edge["type"],
            "confidence": float(edge.get("confidence") or 0.8),
        })
    return LoadedCorpus(
        service=service,
        main_project=main_project,
        key_to_id=key_to_id,
        id_to_key={node_id: key for key, node_id in key_to_id.items()},
        secrets=list(corpus.get("secrets") or []),
        tmp=tmp,
    )


def _apply_mode(loaded: LoadedCorpus, mode: str) -> None:
    """Ablate one retrieval channel on this instance only (no production code changes)."""
    service = loaded.service
    if mode == "lexical":
        service.memory_embeddings.enabled = lambda: False
    elif mode == "vector":
        service.repository.search_memory_fts = lambda *args, **kwargs: []


# --------------------------------------------------------------------------- run


def _hit_ids(hits: list[dict[str, Any]]) -> list[str]:
    return [str((hit.get("node") or {}).get("id") or hit.get("id") or "") for hit in hits]


def _secret_leaks_in(text: str, secrets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [secret for secret in secrets if secret["value"] in text]


def run_mode(corpus: dict[str, Any], queries: list[dict[str, Any]], mode: str, provider: str) -> dict[str, Any]:
    loaded = build_corpus(corpus, provider)
    try:
        _apply_mode(loaded, mode)
        service = loaded.service
        per_query: list[dict[str, Any]] = []
        latencies: list[float] = []
        for item in queries:
            project_id = item.get("project") or loaded.main_project
            started = time.perf_counter()
            if mode == "context":
                result = service.context(item["query"], project_id=project_id, limit=8)
                pack = str(result.get("context") or "")
            else:
                result = service.search_memory(
                    item["query"],
                    project_id=project_id,
                    limit=SEARCH_LIMIT,
                    expand_graph=(mode == "hybrid_graph"),
                )
                pack = ""
            latencies.append((time.perf_counter() - started) * 1000.0)

            hits = result.get("hits") or []
            ids = _hit_ids(hits)
            ranked_keys = [loaded.id_to_key.get(node_id) for node_id in ids]
            relevant: dict[str, int] = dict(item.get("relevant") or {})
            forbidden = set(item.get("forbidden") or [])
            row: dict[str, Any] = {
                "id": item["id"],
                "category": item["category"],
                "query": item["query"],
                "returned": [key or f"<{node_id[:14]}>" for key, node_id in zip(ranked_keys, ids, strict=True)],
                "forbidden_returned": sorted(forbidden & {key for key in ranked_keys if key}),
                "retrieval_mode": (result.get("retrieval") or {}).get("mode"),
            }
            if relevant:
                row["metrics"] = score_ranking(ranked_keys, relevant)
            if item["category"] == "noise":
                row["noise_hits"] = sum(1 for key in ranked_keys if key)
            if mode == "context":
                answers = [key for key, grade in relevant.items() if grade >= 2]
                in_pack = [key for key in answers if f"id={loaded.key_to_id[key]}" in pack]
                row["context_recall"] = (len(in_pack) / len(answers)) if answers else None
                row["pack_chars"] = len(pack)
                row["forbidden_in_pack"] = sorted(key for key in forbidden if f"id={loaded.key_to_id[key]}" in pack)
            serialized = json.dumps(hits, ensure_ascii=False) + pack
            row["secret_leaks"] = [secret["node"] for secret in _secret_leaks_in(serialized, loaded.secrets)]
            per_query.append(row)

        storage_leaks = []
        for secret in loaded.secrets:
            node = service.repository.get_node(loaded.key_to_id[secret["node"]])
            stored = f"{getattr(node, 'label', '')}\n{getattr(node, 'text', '')}" if node else ""
            if secret["value"] in stored:
                storage_leaks.append({"node": secret["node"], "kind": secret["kind"], "known_gap": bool(secret.get("known_gap"))})

        return _summarize(mode, per_query, latencies, storage_leaks, loaded)
    finally:
        loaded.close()


def _summarize(
    mode: str,
    per_query: list[dict[str, Any]],
    latencies: list[float],
    storage_leaks: list[dict[str, Any]],
    loaded: LoadedCorpus,
) -> dict[str, Any]:
    ranked = [row for row in per_query if row.get("metrics") and row["category"] not in RANKED_CATEGORIES_EXCLUDED]
    metric_names = ("answer@1", "recall@5", "recall@10", "mrr", "ndcg@10")

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
        out = {name: _mean([row["metrics"][name] for row in rows]) for name in metric_names}
        out["queries"] = len(rows)
        return out

    categories: dict[str, dict[str, float]] = {}
    for category in sorted({row["category"] for row in ranked}):
        categories[category] = aggregate([row for row in ranked if row["category"] == category])

    forbidden = [
        {"query": row["id"], "returned": row["forbidden_returned"]}
        for row in per_query
        if row["forbidden_returned"] or row.get("forbidden_in_pack")
    ]
    noise_rows = [row for row in per_query if row["category"] == "noise"]
    query_leaks = sorted({node for row in per_query for node in row["secret_leaks"]})
    known_gap_nodes = {secret["node"] for secret in loaded.secrets if secret.get("known_gap")}
    summary: dict[str, Any] = {
        "mode": mode,
        "overall": aggregate(ranked),
        "by_category": categories,
        "safety": {
            "forbidden_violations": forbidden,
            "noise_false_positive_rate": _mean([1.0 if row["noise_hits"] else 0.0 for row in noise_rows]),
            "secret_storage_leaks": storage_leaks,
            "secret_query_leaks": query_leaks,
            "unexpected_secret_leaks": sorted(
                {leak["node"] for leak in storage_leaks if not leak["known_gap"]}
                | {node for node in query_leaks if node not in known_gap_nodes}
            ),
        },
        "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95)},
        "queries": per_query,
    }
    if mode == "context":
        recalls = [row["context_recall"] for row in per_query if row.get("context_recall") is not None]
        sizes = [float(row["pack_chars"]) for row in per_query]
        from backend.architectos.search import DEFAULT_CONTEXT_CHAR_BUDGET

        summary["context"] = {
            "context_recall": _mean(recalls),
            "pack_chars": {"p50": _pct(sizes, 0.5), "max": max(sizes, default=0.0)},
            "budget_chars": DEFAULT_CONTEXT_CHAR_BUDGET,
            "over_budget": sum(1 for size in sizes if size > DEFAULT_CONTEXT_CHAR_BUDGET * 1.05),
        }
    return summary


def run_eval(
    provider: str = "hash",
    modes: tuple[str, ...] = MODES,
    corpus_path: Path = CORPUS_PATH,
    queries_path: Path = QUERIES_PATH,
) -> dict[str, Any]:
    corpus = load_fixture(corpus_path)
    queries = load_fixture(queries_path)["queries"]
    started = time.perf_counter()
    results = {mode: run_mode(corpus, queries, mode, provider) for mode in modes}
    return {
        "provider": provider,
        "corpus_nodes": len(corpus["nodes"]),
        "queries": len(queries),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "modes": results,
    }


# --------------------------------------------------------------------------- report


def render_markdown(report: dict[str, Any], show_misses: bool = False) -> str:
    modes = report["modes"]
    lines = [
        "# ArchitectOS retrieval eval",
        "",
        f"Provider: `{report['provider']}` · corpus: {report['corpus_nodes']} nodes · queries: {report['queries']}",
        "",
        "## Overall (ranked queries)",
        "",
        "| mode | answer@1 | recall@5 | recall@10 | MRR | nDCG@10 | p50 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for mode, data in modes.items():
        o = data["overall"]
        lines.append(
            f"| {mode} | {o['answer@1']:.2f} | {o['recall@5']:.2f} | {o['recall@10']:.2f} | {o['mrr']:.2f} | {o['ndcg@10']:.2f} | {data['latency_ms']['p50']} |"
        )
    categories = sorted({cat for data in modes.values() for cat in data["by_category"]})
    lines += ["", "## Recall@10 by category", "", "| category | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    for cat in categories:
        cells = [f"{modes[m]['by_category'].get(cat, {}).get('recall@10', 0):.2f}" for m in modes]
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    if "context" in modes:
        ctx = modes["context"]["context"]
        lines += [
            "",
            "## Context pack",
            "",
            f"- context recall (grade-2 nodes present in the pack): **{ctx['context_recall']:.2f}**",
            f"- pack size p50 / max: {ctx['pack_chars']['p50']:.0f} / {ctx['pack_chars']['max']:.0f} chars (budget {ctx['budget_chars']})",
            f"- packs over budget: {ctx['over_budget']}",
        ]
    lines += ["", "## Safety", "", "| mode | forbidden returned | noise FP rate | secret leaks (unexpected) | secret leaks (known gaps) |", "|---|---|---|---|---|"]
    for mode, data in modes.items():
        s = data["safety"]
        known = sorted({leak["node"] for leak in s["secret_storage_leaks"] if leak["known_gap"]})
        lines.append(
            f"| {mode} | {len(s['forbidden_violations'])} | {s['noise_false_positive_rate']:.2f} | "
            f"{', '.join(s['unexpected_secret_leaks']) or '0'} | {', '.join(known) or '0'} |"
        )
    if show_misses:
        lines += ["", "## Misses (recall@10 < 1 or forbidden returned)", ""]
        for mode, data in modes.items():
            misses = [
                row for row in data["queries"]
                if (row.get("metrics") and row["metrics"]["recall@10"] < 1.0) or row["forbidden_returned"] or row.get("noise_hits")
            ]
            if not misses:
                continue
            lines.append(f"### {mode}")
            for row in misses:
                recall = row.get("metrics", {}).get("recall@10")
                tag = f"recall@10={recall:.2f}" if recall is not None else "no-relevant"
                extra = f" forbidden={row['forbidden_returned']}" if row["forbidden_returned"] else ""
                lines.append(f"- `{row['id']}` {tag}{extra} — “{row['query']}” → {row['returned'][:5]}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=("hash", "local", "configured"), default="hash")
    parser.add_argument("--modes", default=",".join(MODES), help=f"comma list from {','.join(MODES)}")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--md", type=Path, help="write the Markdown summary")
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args(argv)
    modes = tuple(mode.strip() for mode in args.modes.split(",") if mode.strip())
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        parser.error(f"unknown modes: {unknown}")
    report = run_eval(provider=args.provider, modes=modes)
    markdown = render_markdown(report, show_misses=args.show_misses)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        args.md.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
