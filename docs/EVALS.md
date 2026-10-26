# Retrieval evals

ArchitectOS lives or dies on one question: *does the agent get the right
memory, and only the right memory?* `evals/` answers it with numbers instead of
"looks fine to me".

| Path | What it is |
|---|---|
| `evals/retrieval/corpus.json` | Public fixture corpus: a fictional energy platform (`gridflow`), a neighbouring project (`billing-svc`), team-wide shared rules, distractors, one superseded decision and four nodes carrying secrets. Graph edges included. |
| `evals/retrieval/queries.json` | 35 queries with **graded** relevance (2 = the answer, 1 = useful context) and `forbidden` nodes that must never come back. |
| `evals/retrieval_eval.py` | Runner: builds a fresh workspace through the production write path (`add_memory`, `create_graph_edge`), runs every query in each retrieval mode, prints a Markdown report. |
| `evals/retrieval/thresholds.json` | Ratchet floors for the CI gate. |
| `evals/retrieval/BASELINE.md` | The report the floors were set from. |
| `tests/test_retrieval_eval.py` | CI gate (runs inside the normal `unittest discover`). |

`scripts/bench_memory.py` stays the tool for measuring *your own*
`data/architectos.db` (latency at scale, corpus health, private gold queries).
The eval here is the reproducible, shareable counterpart.

## Running

```bash
python3 -m evals.retrieval_eval                         # hash embeddings (deterministic, offline)
python3 -m evals.retrieval_eval --show-misses           # + every query that missed
python3 -m evals.retrieval_eval --modes hybrid,context  # subset of modes
python3 -m evals.retrieval_eval --provider local        # on-device model, needs pip install -e ".[embeddings]"
python3 -m evals.retrieval_eval --provider configured   # provider from ./.env (e.g. Cloudflare bge-m3)
python3 -m evals.retrieval_eval --json out.json --md out.md
```

A full run takes ~10 s with hash embeddings.

## Modes (ablation)

Each mode runs on its own fresh corpus so lifecycle access bumps never leak
between modes. Channels are switched off on the instance only; production code
is untouched.

| Mode | What runs |
|---|---|
| `lexical` | FTS5/BM25 only (vector search disabled) |
| `vector` | embeddings only (FTS disabled) |
| `hybrid` | production `search_memory` (FTS + vector, RRF) |
| `hybrid_graph` | `search_memory(expand_graph=True)` — personalized PageRank expansion |
| `context` | `service.context()` — the pack an agent actually receives |

## Metrics

Ranking (queries with at least one relevant node, noise excluded):

- **answer@1** — the top hit is a grade-2 node.
- **recall@5 / recall@10** — share of relevant (grade ≥ 1) nodes retrieved.
- **MRR** — reciprocal rank of the first grade-2 node.
- **nDCG@10** — graded, gain `2^grade − 1`.
- **context recall** (`context` mode) — share of grade-2 nodes whose `id=` line is in the packed context.

Safety (strict):

- **forbidden returned** — a superseded fact or another project's memory in hits *or in the pack*.
- **noise FP rate** — off-topic queries that return anything.
- **secret leaks** — a secret value surviving redaction in storage, hits or the pack.
- **over budget** — packs larger than `DEFAULT_CONTEXT_CHAR_BUDGET` (+5%).

Query categories: `exact`, `paraphrase`, `cross_lingual` (RU query → EN
memory), `multi_hop` (answer linked only through the graph), `temporal`
(superseded decisions), `isolation` (other project), `shared` (shared scope must
be visible), `noise`.

## Baseline (hash embeddings)

| mode | recall@10 | nDCG@10 | MRR |
|---|---|---|---|
| lexical | 0.55 | 0.59 | 0.61 |
| vector | 0.43 | 0.52 | 0.58 |
| hybrid | 0.56 | 0.60 | 0.62 |
| hybrid_graph | 0.73 | 0.68 | 0.63 |

Context recall 0.93; zero safety violations except the known redaction gaps
below. Full table: [`evals/retrieval/BASELINE.md`](../evals/retrieval/BASELINE.md).

What it says:

1. **Graph expansion carries the system.** `multi_hop` recall goes 0.46 → 1.00
   and `exact` 0.69 → 1.00 with `expand_graph`; supporting lessons rarely share
   words with the question.
2. **Cross-lingual is 0.00 with hash vectors** — expected: the hash embedder is
   lexical, not semantic. This category exists to measure real models; run
   `--provider local` / `--provider configured` to get the number that matters.
3. **A query worded like a superseded decision returns nothing** (`tm-02`):
   search hides the old fact correctly but does not redirect to its successor.
   Candidate improvement: follow `superseded_by` and surface the replacing node.

## Findings this eval surfaced

- **Fixed — superseded decisions leaked into agent context.** `search_memory`
  hid nodes with `invalid_at`, but `list_stable_memory` /
  `list_rule_layer_memory` (the always-on layer of `context()` and
  `memory_briefing()`) did not, so a decision retired via `SUPERSEDES` was still
  handed to agents as a governing rule. Both now apply the same bi-temporal
  filter; `SupersededStableLayerTests` is the regression test.
- **Open — redaction gaps** (`known_gap: true` in `corpus.json`): GitHub tokens
  (`ghp_…`), AWS access key ids (`AKIA…`) and passwords inside connection URIs
  (`postgres://user:pass@host`) are stored verbatim. `password=` assignments and
  Bearer tokens are redacted. When the redactor learns a format,
  `test_known_secret_gaps_are_still_gaps` fails and tells you to drop the flag —
  from then on a regression is a hard failure.

## CI gate and the ratchet

`tests/test_retrieval_eval.py` runs the whole eval once (hash embeddings) and checks:

- quality floors from `thresholds.json` (~0.03 below baseline);
- zero forbidden results, zero noise hits, zero over-budget packs, zero
  unexpected secret leaks;
- metric math (`score_ranking`) on hand-computed cases.

When a change improves retrieval, raise the floors in `thresholds.json` in the
same PR. Never lower a floor to get a change through — look at
`--show-misses` instead.

## Extending

- **New query:** add it to `queries.json` with graded `relevant` keys and, if it
  matters, `forbidden` keys. Grade 2 only for nodes that *answer* the question.
- **New behaviour to protect** (e.g. interface scope, `as_of` queries): add the
  nodes to `corpus.json`, a category of queries, and a strict safety assertion
  in the test if it is a correctness/safety property rather than a quality one.
- **Real-vector baseline:** commit a second report produced with
  `--provider local` so quality changes from the model are visible separately
  from ranking changes.

Next layer, not covered yet: answer-level evals for Ask (faithfulness to the
retrieved pack, citation correctness) with an LLM judge calibrated on a small
hand-labelled set.
