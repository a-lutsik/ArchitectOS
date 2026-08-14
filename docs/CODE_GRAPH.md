# Code Graph

ArchitectOS persists a lightweight **code-structure graph** alongside its memory
graph. On top of the coarse per-file `Artifact` nodes produced by scanning, it
extracts **symbols** (functions / methods / classes) and the **relationships**
between them, and writes both into the same `memory_nodes` / `memory_edges`
store. This gives the existing graph analytics (communities, PageRank, dual-level
retrieval) a real code substrate to work on, and lets agents answer questions
like *"who calls this function?"* and *"what breaks if I change it?"*.

Implementation: `backend/architectos/code_graph.py` (`CodeGraphIngestor`).

## What gets stored

### Nodes

| type | meaning | label | text (embedded when selected) |
|------|---------|-------|-------------------------------|
| `Symbol` | a function, method, or class | `path::qualname` | `Kind qualname(signature)` + docstring |

`Symbol` metadata: `source="code_graph"`, `cg_file`, `path`, `symbol` (qualname),
`kind` (`Function`/`Method`/`Class`), `line`, `language`, `cg_embed`.

The file-level `Artifact` node is kept as the anchor and is the source of
`DEFINES` / `IMPORTS` edges.

### Edges

All code edges carry `metadata.origin = "code_graph"`.

| edge | from → to | provenance | derived from |
|------|-----------|------------|--------------|
| `DEFINES` | file `Artifact` → `Symbol` | EXTRACTED | AST/regex |
| `CONTAINS` | class `Symbol` → method `Symbol` | EXTRACTED | AST nesting |
| `IMPORTS` | file `Artifact` → imported file `Artifact` | EXTRACTED | import resolver |
| `CALLS` | caller `Symbol` → callee `Symbol` | INFERRED | call-site heuristic |

`CALLS` is best-effort and follows a **precision-over-recall** rule: a call name
resolves only when it is unique within the file, or unique across the project;
ambiguous names are skipped. In the UI, INFERRED edges render dashed/faint.

## How extraction works

- **Offline, no language server, no network.** Python uses the standard-library
  `ast`; JavaScript/TypeScript use the regex symbol patterns from `lsp.py`.
- **Two passes.** First all symbols and `DEFINES`/`CONTAINS`/`IMPORTS` edges are
  written; then a second pass resolves `CALLS` once every symbol id is known
  (so cross-file calls resolve).
- **Idempotent.** Node and edge ids are content-stable, so re-scanning a file
  does not duplicate. Symbols and edges that disappear from a re-scanned file are
  archived / deleted (`_prune_stale`).
- **Import resolution.** Python dotted and relative imports are matched against
  the project's known module paths; JS/TS relative specifiers (`./`, `../`,
  `index.*`) are resolved to files. Bare/external specifiers are skipped.

## Configuration

Stored under the `code_graph` setting (all optional; defaults shown):

```json
{
  "enabled": true,
  "languages": ["python", "javascript", "typescript"],
  "embed_symbols": "selective",
  "max_symbols_per_file": 300,
  "min_doc_chars_for_embed": 40
}
```

- **`enabled`** — turn code-graph ingest on/off during `scan_project`.
- **`embed_symbols`** — `off` | `selective` | `all`. `selective` embeds only
  documented symbols, classes, and public top-level symbols; everything else is
  still found via full-text search on its name. Symbols not marked for embedding
  are skipped by the embedding worker (`metadata.cg_embed`), keeping token cost
  bounded even though symbols outnumber files 10–50×.
- **`max_symbols_per_file`** — cap to keep pathological files tidy.

Because the schema is unchanged, existing databases pick up the code graph on the
next `scan` / `reindex` — no migration required.

## Querying (MCP tools)

- **`code_neighbors`** — for a symbol (by id, `path::qualname`, qualname, or bare
  name): its callers, callees, and defining file.
- **`code_impact`** — the blast radius of changing a symbol: everything that
  transitively calls it (reverse `CALLS` up to a depth) plus files that import
  its defining file.

Both are also available as service methods (`ArchitectOSService.code_neighbors`,
`code_impact`) and the symbols participate in ordinary `memory_search`.

## UI

In the memory graph, `Symbol` nodes form a **Code** group (cyan), are drawn
smaller than knowledge nodes (classes slightly larger than functions), and code
edges are tinted by type (`DEFINES`/`CONTAINS` cyan, `IMPORTS` teal, `CALLS`
violet + dashed as INFERRED). The node detail panel shows the symbol kind,
`path:line`, and caller/callee counts, and lists linked symbols as neighbor
cards.

## Limitations

- `CALLS` is name-based, not type-resolved: overloads and same-named methods on
  different classes are intentionally skipped rather than guessed.
- Only Python/JS/TS are parsed today (offline). Other languages still get
  file-level `Artifact` nodes but no symbols.
- Import resolution is heuristic and project-local; third-party imports are not
  linked.
