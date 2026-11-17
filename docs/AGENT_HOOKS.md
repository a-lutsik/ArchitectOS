# Agent Hooks: memory that does not wait for a tool call

MCP only fires when the model *decides* to call a tool. An agent that never calls
`memory_turn` therefore gets no memory at all, and nothing it learned is captured.
Client **hooks** close that gap: they are lifecycle events the IDE fires on its
own, so capture happens whether or not the model cooperates.

This is the third transport for the same memory engine, alongside the desktop app
and the MCP server. All three read and write one `data/architectos.db`.

## What each client can do

| Client | Config | Capture | Inject back |
| --- | --- | --- | --- |
| **Codex CLI** | `~/.codex/hooks.json` | yes (`UserPromptSubmit`, `Stop`) | yes — `additionalContext` on `SessionStart` / `UserPromptSubmit` |
| **Claude Code** | `~/.claude/settings.json` | yes (`UserPromptSubmit`, `Stop`) | yes — same fields |
| **Cursor** | `~/.cursor/hooks.json` | yes (`beforeSubmitPrompt`, `afterAgentResponse`) | **no** — Cursor hooks have no context-injection field |
| Everything else | – | – | use the [MCP server](MCP_MEMORY_SERVER.md) |

Cursor is capture-only by design, not by omission: its hook events do not accept
model-visible output. Keep the MCP memory server registered in Cursor so injection
still happens through `memory_turn` and the startup briefing, and let the hook take
care of capture so a silent agent still contributes facts.

Clients without a hook system (Copilot, Junie, Gemini CLI, Windsurf, Aider) keep
working exactly as before through MCP.

## Install

The app installs them for you: open **Setup → Agent hooks**, which lists every
client with its config file and install state, and press *Install for all* (or the
button on one card). It asks for confirmation showing the exact files it will
write, and *This project only* switches from the user-level config to the repo one.
The same panel removes them again.

The CLI does the same thing without the app:

```bash
python3 architectos_hook.py install --client all      # source checkout
architectos hook install --client all                 # pip install / python -m architectos
# Frozen desktop or MCP binary:
#   architectos-server hook install --client all
#   architectos-mcp hook doctor
```

Both paths share one installer (`GET /api/hooks`, `POST /api/hooks/install`,
`POST /api/hooks/uninstall` wrap the same code the CLI calls). It merges into
existing config files and never touches entries that are not ours — they are
recognized by `architectos_hook` in the command, or by the frozen
`hook capture --client` subcommand, so reinstalling is idempotent and
uninstalling leaves foreign hooks intact.

A packaged (frozen) build writes `"<binary>" hook capture ...` into the client
config, so it does not need `architectos_hook.py` next to the executable. From a
pip-installed package the same subcommand is `python -m architectos hook`.

Two client-specific steps the installer cannot do for you:

- **Codex** only runs hooks you have trusted. Open Codex, run `/hooks`, review the
  new entry and trust it. Also check that `[features].hooks` is not `false` in
  `~/.codex/config.toml`.
- **Cursor** reloads `hooks.json` on save; if hooks never fire, restart Cursor and
  check the Hooks output channel.

## What happens on each event

1. **Session start** (Codex, Claude Code) — injects the stable layer: pinned
   memories, constraints and long-term decisions. Same pack as `memory://briefing`.
2. **Prompt submit** — injects memory retrieved *for that question*, so the
   briefing is per-turn rather than once per session. An empty project injects
   nothing rather than spending tokens on an empty pack. With a `session_id` the
   question is only held in the session file (Cursor's `beforeSubmitPrompt` does
   not even call the app, since Cursor cannot inject); without one it is captured
   at once, as before.
3. **Turn end** — captures the held question together with the final assistant
   message in one `capture_memory_turn(user, assistant)` call: one capture and at
   most one extractor call per turn, and the extractor sees both sides. The
   question is removed from the session file as soon as it is taken and is never
   written to `hooks.log`. Cursor pairs `beforeSubmitPrompt` with
   `afterAgentResponse` the same way. A turn end with no answer text (the agent
   was stopped) still captures the question, and a question whose turn end never
   came is spooled when the next prompt for that project replaces it. Turn end in
   a subfolder the session has not seen pairs with the question of the project
   that folder belongs to.

Capture routes through `capture_memory_turn`, which is the same path MCP's
`memory_turn` uses, so the governance rules are identical:

- Raw dialogue is **never** stored — only extracted durable facts.
- What happens to a fact is the **memory capture policy** (Settings → Memory
  capture, `memory_capture.mode`): `auto` (default) writes facts above
  `min_confidence` to memory at once, undoable; `review` queues every fact in the
  **review queue** with a 7-day TTL; `off` captures nothing but explicit saves.
  Conflicts, changed values and duplicates found by the dedup gate wait for a
  person in every mode. See `docs/MEMORY_CAPTURE_RU.md`.
- Capture runs as the hook's project: it never rewrites another project's node. A
  fact that restates another project's active node is held (nothing is written);
  a similar but different fact becomes a new node linked by `possible_duplicate_of`.
  See `docs/PROJECT_ISOLATION_RU.md`.
- Each fact carries a kind — `user` (preferences about the person, stored as a
  global `Preference`), `feedback` (corrections and confirmed approaches),
  `project` (decisions the code does not state) or `reference` (where to look
  outside the repo) — and a confidence from `fact_signals`. In `auto`, project
  facts need `auto_project_min_confidence` (0.85) unless the person said
  "remember"; the other kinds need `min_confidence` (0.7).
- With a real provider enabled (`memory_capture.extractor`: `auto`), the facts
  come from one model call per turn that sees what memory already holds; the
  rule-based extractor is the offline path and the fallback on any failure.
- Secrets are redacted before anything is written.
- Chit-chat is filtered out by `evaluate_chat_turn` before any candidate is made.
- Cards land in the review queue with source **MCP** (`source_type=mcp`, label
  `MCP fact:`), not Ask. If the same fact is later finalized from an Ask dialog,
  ArchitectOS keeps the MCP card and does not add an Ask duplicate.

The project is resolved from the hook's working directory by the same rule as the
MCP server: a `.architectos.json` bind found walking up from that directory wins;
otherwise the registered project whose `root_path` contains it wins, and the most
deeply nested root wins for monorepos. No match falls back to the `architectos`
project.

MCP tools do **not** use that fallback. An unbound MCP folder errors, lists
projects, and asks the agent to call `memory_list_projects` then
`memory_use_project` or `project_create` (see [MCP_MEMORY_SERVER.md](MCP_MEMORY_SERVER.md)).

## Hook pack size

Prompt-submit injection uses a smaller pack than Ask's `context()`: a
1 600-character budget (≈ 400 tokens), 220-character node excerpts, at most four
stable-layer nodes, no open tasks or providers, no graph expansion (the ~2 hits
that fit the budget are direct matches; on the gold queries expansion changed none
of them and doubled the search time), and an 800 ms cap on vector search. Hits
scoring below the relevance floor are dropped, and a pack with no
hit left (and no stable layer due) is not injected at all. Both knobs live in the
`agent` setting:

| Setting | Default | Effect |
| --- | --- | --- |
| `agent.hook_char_budget` | `1600` | Character budget of the whole prompt injection (hook prefix + pack), clamped to 1 301–6 000: a pack never goes below 1 200 (its header alone is ~420), and the hook prefix comes on top |
| `agent.hook_stable_every_turns` | `20` | A session gets the stable layer again after this many prompts without it (`0` = only at session start) |
| `agent.hook_min_score` | `120` | Relevance floor for a hit. Static bonuses (scope, type, source, recency, lifecycle) plus an FTS rank alone reach ~115, so 120 asks for a real query-term match or a vector signal |

`POST /api/memory/turn` takes the same pack with `"hook": true`; `capture: false`
packs without capturing, `exclude_ids` and `include_stable` shape it, and
`vector_timeout_ms` overrides the vector-search timeout for that request. Its
`timing` adds `cache_ms`, the write of the offline briefing cache.

## Session state

When the client sends a `session_id` (Claude Code and Codex always do), the hook
keeps `data/hook_sessions/<digest>.json`: per project, a prompt counter, the turn
the stable layer was last injected and every node id injected so far. The project
is the one the app resolved for the folder, so one project's history never steers
another's pack, even under the same session id.

- Prompt submit sends the ids the session already holds as `exclude_ids`: each
  node is injected once per session, and the budget goes to new hits.
- The stable layer comes with `SessionStart`; on prompts it returns only after
  `agent.hook_stable_every_turns` prompts without it. A client without
  `SessionStart` (Cursor) gets it on the session's first prompt.
- `SessionStart` with `source` `clear` or `compact` opens a new context window, so
  the injected-id history of that project is forgotten.
- Files untouched for 3 days are deleted when a new session file is created.
- Without a `session_id` nothing is kept and every prompt gets the full hook pack.

## Transport

The hook prefers the running app: it reads `data/architectos.runtime.json` for the
port and auth token and calls `POST /api/memory/turn` (or
`GET /api/memory/briefing`). Each agent turn starts a new interpreter, so the hook
keeps its imports to a few stdlib modules and speaks HTTP/1.0 over a plain socket
to the app's loopback server rather than through `urllib`. The registered
`capture --client X --event Y` command line is read without `argparse`, and the
hook command never loads `logging`: its own log lines go straight to
`data/hooks.log` with `ARCHITECTOS_HOOK_DEBUG=1` and are dropped otherwise.

If the app is not running (or does not answer in time) the hook never opens the
store, so an event costs a few milliseconds instead of a service cold start:

- **Injection** comes from `data/briefing_cache.json`. The app rewrites it on
  every briefing (the stable layer) and every hook pack (the latest hits) per
  project, together with the project roots, the hook settings and the default
  project. The hook resolves the folder's project the way the app does
  (`.architectos.json` bind, then the deepest root, then the default project)
  and injects only that project's cached lines, still minus what the session
  already holds.
- **Capture** is appended to `data/hook_spool.jsonl`, one record per turn with
  the project resolved for the folder. The app replays the spool through
  `capture_memory_turn` into each record's own project when it starts and once a
  minute (the `chat-session-idle` loop); a record that keeps failing is retried
  three times and then dropped. The spool stops growing at 5 MB. A capture whose
  reply timed out after the whole request was sent is *not* spooled: the app
  already has it. A connect or send that timed out never reached the app and is
  spooled. The timeout bounds the whole request, not each socket operation. One the app
  answered with an error (HTTP 4xx/5xx, a cut reply) is spooled, since the app did
  not store it. The spool keeps the raw question and answer until the app replays
  them through `capture_memory_turn`, which redacts secrets as for a live turn; like
  the session files and the briefing cache it is readable by its owner only (0600).
- Until the app runs again, spooled facts are not in memory. `doctor` shows how
  many records wait and how old the oldest one is.

`ARCHITECTOS_HOOK_OFFLINE=0` turns the offline path off: with the app closed the
hook says nothing and spools nothing.

## Failure behaviour

A memory hook must never break the agent, so every failure is silent: unparsable
stdin, an unreachable server, a missing project — all exit `0` with no output. The
one exception is Codex `Stop`, which requires valid JSON on stdout, so it always
prints `{}`.

## Environment variables

| Variable | Effect |
| --- | --- |
| `ARCHITECTOS_ROOT` | Which root's `data/architectos.db` to use |
| `ARCHITECTOS_HOOK_DISABLED=1` | Turn all hooks into no-ops without editing configs |
| `ARCHITECTOS_HOOK_INJECT_TIMEOUT` | HTTP timeout in seconds for events that inject (`SessionStart`, prompt submit; default `2`) |
| `ARCHITECTOS_HOOK_TIMEOUT` | HTTP timeout in seconds for capture on turn end (default `5`) |
| `ARCHITECTOS_HOOK_OFFLINE=0` | With the app closed, stay silent: no cached injection, no capture spool |
| `ARCHITECTOS_HOOK_DEBUG=1` | Append one line per event to `data/hooks.log` (see below) |

With `ARCHITECTOS_HOOK_DEBUG=1` each handled event adds one line with counts and
timings only, never prompt or answer text:

```text
2026-11-04T10:00:00+00:00 event=prompt client=claude transport=http ms=312.4 tokens=1003 hits=10 project=architectos queued=0 auto=1
```

`transport` is `http` (app running), `offline` (briefing cache and spool),
`deferred` (Cursor prompt held for turn end) or `none`; `ms` covers the event
inside the hook process, not the interpreter start;
`tokens` estimates the injected context (0 when the hook stayed silent). The
command-line hook also appends its own log lines there (offline cache and spool
decisions, skipped injections, timeouts), again counts only.

## Manual check

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"Remember: we deploy only from main.","cwd":"'$PWD'"}' \
  | python3 architectos_hook.py capture --client codex --event prompt
```

Silence means the turn was captured but there was nothing worth injecting yet.
Run it a second time on a related question and the stored memory comes back as
`additionalContext`. Facts that landed in review appear in the app under memory candidates with the
**MCP** chip.
