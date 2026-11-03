"""Agent integration benchmark: hook events, MCP memory tools and injection size.

Builds a synthetic memory of N nodes in a temporary root (never data/architectos.db)
and measures, per corpus size:

- service cold start and ``context()`` per turn (with its search / stable / pack split);
- MCP ``memory_turn`` (question and fact turns) and ``memory_search``;
- hook events ``SessionStart`` / ``UserPromptSubmit`` / ``Stop`` run as real
  ``architectos_hook.py`` subprocesses, over HTTP (app running: an in-process
  ArchitectOSHandler on 127.0.0.1) and offline (app closed: the hook opens the
  store itself). Wall time includes the interpreter start; ``in_process_ms`` and
  the injected tokens come from the hook's own ``data/hooks.log`` line.

Default mode is deterministic offline hash embeddings. ``--real-provider`` loads
./.env and keeps the configured embedding provider; the synthetic corpus is then
embedded through that provider too, so keep ``--sizes`` small.

Usage:
    python3 scripts/bench_agent.py [--sizes 500,2000,15000] [--queries 20] [--hook-runs 5] [--out bench_agent.json]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bench_memory import load_env_file, pct  # noqa: E402
from backend.architectos.mcp_server import MemoryMCPServer  # noqa: E402
from backend.architectos.server import ArchitectOSHandler  # noqa: E402
from backend.architectos.service import ArchitectOSService  # noqa: E402

PROJECT = "architectos"
WORDS = (
    "auth token cache deploy session pipeline vector memory graph search embed queue lifecycle "
    "decay archive promote scope project interface decision constraint lesson router billing "
    "invoice tenant gateway retry timeout schema migration index replica backup audit"
).split()
# Roughly the type mix of a real project memory: the stable layer (Decision /
# Constraint / Requirement) is what context() re-reads on every turn.
NODE_TYPES = ("Lesson",) * 9 + ("Decision",) * 4 + ("Constraint",) * 2 + ("Requirement",) * 2 + ("Concept",) * 3
HOOK_EVENTS = (("SessionStart", "session"), ("UserPromptSubmit", "prompt"), ("Stop", "response"))


def stats(values: list[float]) -> dict[str, float]:
    return {"p50": pct(values, 0.5), "p95": pct(values, 0.95), "mean": round(statistics.mean(values), 1) if values else 0.0}


def build_corpus(service: ArchitectOSService, size: int, rng: random.Random) -> float:
    """Insert ``size`` nodes in batched transactions; returns ms per node."""
    started = time.perf_counter()
    for offset in range(0, size, 1000):
        with service.repository.transaction():
            for index in range(offset, min(size, offset + 1000)):
                node_type = rng.choice(NODE_TYPES)
                label = f"{node_type} {' '.join(rng.choices(WORDS, k=4))} {index}"
                text = " ".join(rng.choices(WORDS, k=36)) + "."
                service.repository.add_node(node_type, label, "project", text, PROJECT, None, 0.75, {"source": "bench"})
    return round((time.perf_counter() - started) * 1000.0 / max(1, size), 2)


def question(rng: random.Random) -> str:
    return f"How do we handle {' '.join(rng.choices(WORDS, k=3))} in this project?"


def fact(rng: random.Random, index: int) -> str:
    first, second, third = rng.sample(WORDS, 3)
    return f"Architecture decision: always route {first} {second} through the {third} service (bench case {index})."


def bench_service(service: ArchitectOSService, root: Path, queries: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    ArchitectOSService(root)
    startup_ms = (time.perf_counter() - started) * 1000.0
    totals: list[float] = []
    split: dict[str, list[float]] = {"search_ms": [], "stable_ms": [], "pack_ms": []}
    tokens: list[float] = []
    for query in queries:
        started = time.perf_counter()
        packed = service.context(query, project_id=PROJECT)
        totals.append((time.perf_counter() - started) * 1000.0)
        for key, values in split.items():
            values.append(float(packed["timing"][key]))
        tokens.append(float(packed["token_economy"]["packed_tokens"]))
    return {
        "startup_ms": round(startup_ms, 1),
        "context_ms": stats(totals),
        "context_split_ms": {key: stats(values) for key, values in split.items()},
        "context_pack_tokens": stats(tokens),
    }


def bench_mcp(service: ArchitectOSService, root: Path, queries: list[str], facts: list[str]) -> dict[str, Any]:
    server = MemoryMCPServer(root, project_id=PROJECT)
    server._service = service  # noqa: SLF001 - reuse the warm store, measure the tool path only

    def timed(name: str, arguments: dict[str, Any]) -> float:
        started = time.perf_counter()
        result = server._call_tool({"name": name, "arguments": arguments})  # noqa: SLF001
        if result.get("isError"):
            raise RuntimeError(f"{name} failed: {result}")
        return (time.perf_counter() - started) * 1000.0

    return {
        "memory_turn_question_ms": stats([timed("memory_turn", {"user_text": query}) for query in queries]),
        "memory_turn_fact_ms": stats([timed("memory_turn", {"user_text": text}) for text in facts]),
        "memory_search_ms": stats([timed("memory_search", {"query": query}) for query in queries]),
    }


def hook_payload(raw_event: str, root: Path, rng: random.Random, index: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"hook_event_name": raw_event, "cwd": str(root), "session_id": "bench"}
    if raw_event == "UserPromptSubmit":
        payload["prompt"] = question(rng)
    elif raw_event == "Stop":
        payload["last_assistant_message"] = fact(rng, 10_000 + index)
    return payload


def bench_hooks(root: Path, env: dict[str, str], runs: int, rng: random.Random) -> dict[str, Any]:
    """Run each hook event ``runs`` times as a subprocess and pair it with its log line."""
    log = root / "data" / "hooks.log"
    report: dict[str, Any] = {}
    for raw_event, event in HOOK_EVENTS:
        wall: list[float] = []
        inner: list[float] = []
        tokens: list[float] = []
        hits: list[float] = []
        transports: set[str] = set()
        for index in range(runs):
            before = log.stat().st_size if log.exists() else 0
            started = time.perf_counter()
            subprocess.run(
                [sys.executable, str(REPO / "architectos_hook.py"), "capture", "--client", "claude", "--event", event],
                input=json.dumps(hook_payload(raw_event, root, rng, index)),
                capture_output=True,
                text=True,
                env=env,
                cwd=root,
                check=True,
                timeout=120,
            )
            wall.append((time.perf_counter() - started) * 1000.0)
            with log.open("r", encoding="utf-8") as handle:
                handle.seek(before)
                lines = [line for line in handle.read().splitlines() if " event=" in line]
            fields = dict(item.split("=", 1) for item in lines[-1].split()[1:]) if lines else {}
            transports.add(fields.get("transport", "missing"))
            inner.append(float(fields.get("ms", 0.0)))
            tokens.append(float(fields.get("tokens", 0)))
            hits.append(float(fields.get("hits", 0)))
        report[raw_event] = {
            "transport": ",".join(sorted(transports)),
            "wall_ms": stats(wall),
            "in_process_ms": stats(inner),
            "inject_tokens": stats(tokens),
            "hits": stats(hits),
        }
    return report


def bench_hooks_http(service: ArchitectOSService, root: Path, env: dict[str, str], runs: int, rng: random.Random) -> dict[str, Any]:
    class BenchHandler(ArchitectOSHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

    BenchHandler.service = service
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), BenchHandler)
    except OSError as exc:
        return {"skipped": f"cannot listen on 127.0.0.1 ({exc}); run outside a sandbox that blocks local ports"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = root / "data" / "architectos.runtime.json"
    runtime.write_text(
        json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "auth_token": service.auth_token}),
        encoding="utf-8",
    )
    try:
        return bench_hooks(root, env, runs, rng)
    finally:
        runtime.unlink(missing_ok=True)
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def bench_size(size: int, args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(size)
    with tempfile.TemporaryDirectory(prefix="architectos-bench-") as tmp:
        root = Path(tmp)
        service = ArchitectOSService(root)
        print(f"[bench] {size} nodes: building corpus", file=sys.stderr, flush=True)
        ingest_ms = build_corpus(service, size, rng)
        queries = [question(rng) for _ in range(args.queries)]
        facts = [fact(rng, index) for index in range(args.queries)]
        report: dict[str, Any] = {"nodes": size, "ingest_ms_per_node": ingest_ms}
        print(f"[bench] {size} nodes: service and MCP", file=sys.stderr, flush=True)
        report.update(bench_service(service, root, queries))
        report["mcp"] = bench_mcp(service, root, queries, facts)
        env = {**os.environ, "ARCHITECTOS_ROOT": str(root), "ARCHITECTOS_HOOK_DEBUG": "1", "ARCHITECTOS_HOOK_OFFLINE": "1"}
        print(f"[bench] {size} nodes: hooks over HTTP", file=sys.stderr, flush=True)
        report["hooks_http"] = bench_hooks_http(service, root, env, args.hook_runs, rng)
        print(f"[bench] {size} nodes: hooks offline", file=sys.stderr, flush=True)
        report["hooks_offline"] = bench_hooks(root, env, args.hook_runs, rng)
        report["capture_stats"] = service.memory_capture_stats(days=1, project_id=PROJECT)["agent_turns"]
        return report


def _p50(report: dict[str, Any], transport: str, event: str) -> str:
    block = report.get(transport) or {}
    if "skipped" in block:
        return "skipped"
    return f"{block[event]['wall_ms']['p50']:.0f} ({block[event]['in_process_ms']['p50']:.0f}) ms"


def print_table(sizes: dict[str, dict[str, Any]]) -> None:
    header = (
        "| Nodes | Cold start | context() p50 / p95 | Inject tokens | memory_turn q / fact p50 | memory_search p50 "
        "| Prompt hook HTTP | Prompt hook offline | SessionStart offline | Stop offline |"
    )
    print(header)
    print("|" + " --- |" * header.count(" | ") + " --- |")
    for report in sizes.values():
        mcp = report["mcp"]
        prompt_offline = report["hooks_offline"]["UserPromptSubmit"]
        print(
            f"| {report['nodes']} | {report['startup_ms']:.0f} ms "
            f"| {report['context_ms']['p50']:.0f} / {report['context_ms']['p95']:.0f} ms "
            f"| {prompt_offline['inject_tokens']['mean']:.0f} "
            f"| {mcp['memory_turn_question_ms']['p50']:.0f} / {mcp['memory_turn_fact_ms']['p50']:.0f} ms "
            f"| {mcp['memory_search_ms']['p50']:.0f} ms "
            f"| {_p50(report, 'hooks_http', 'UserPromptSubmit')} "
            f"| {_p50(report, 'hooks_offline', 'UserPromptSubmit')} "
            f"| {_p50(report, 'hooks_offline', 'SessionStart')} "
            f"| {_p50(report, 'hooks_offline', 'Stop')} |"
        )
    print("Hook cells: subprocess wall p50 (in-process p50 from hooks.log).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sizes", default="500,2000,15000", help="Comma-separated corpus sizes.")
    parser.add_argument("--queries", type=int, default=20, help="Turns per service / MCP measurement.")
    parser.add_argument("--hook-runs", type=int, default=5, help="Subprocess runs per hook event and transport.")
    parser.add_argument("--out", default="-", help="JSON report path ('-' prints it after the table).")
    parser.add_argument(
        "--real-provider",
        action="store_true",
        help="Use the embedding provider from ./.env instead of hash; the corpus is embedded through it (network).",
    )
    args = parser.parse_args()

    if args.real_provider:
        os.environ.pop("MEMORY_EMBEDDING_PROVIDER", None)
        load_env_file(REPO / ".env")
    else:
        os.environ["MEMORY_EMBEDDING_PROVIDER"] = "hash"

    sizes = [int(item) for item in args.sizes.split(",") if item.strip()]
    print(f"[bench] start: sizes={sizes} queries={args.queries} hook_runs={args.hook_runs} "
          f"embeddings={'real' if args.real_provider else 'hash'}", file=sys.stderr, flush=True)
    started = time.perf_counter()
    results = {str(size): bench_size(size, args) for size in sizes}
    report = {
        "meta": {
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": f"{platform.system()} {platform.mac_ver()[0] or platform.release()} {platform.machine()}",
            "python": platform.python_version(),
            "embeddings": os.environ.get("MEMORY_EMBEDDING_PROVIDER") or "configured",
            "queries": args.queries,
            "hook_runs": args.hook_runs,
            "elapsed_s": round(time.perf_counter() - started, 1),
        },
        "sizes": results,
    }
    print_table(results)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out == "-":
        print(payload)
    else:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"JSON report: {args.out}")
    print(f"[bench] done in {report['meta']['elapsed_s']} s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
