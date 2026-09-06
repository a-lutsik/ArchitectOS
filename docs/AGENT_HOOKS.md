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
2. **Prompt submit** — captures the user message and injects memory retrieved *for
   that question*, so the briefing is per-turn rather than once per session. An
   empty project injects nothing rather than spending tokens on an empty pack.
3. **Turn end** — captures the final assistant message.

Capture routes through `capture_memory_turn`, which is the same path MCP's
`memory_turn` uses, so the governance rules are identical:

- Raw dialogue is **never** stored — only extracted durable facts.
- Low-risk `Lesson` facts auto-write to short-term memory.
- `Decision`, `Constraint` and `Requirement` facts go to the **review queue** with
  a 7-day TTL and need a human to promote them.
- Secrets are redacted before anything is written.
- Chit-chat is filtered out by `evaluate_chat_turn` before any candidate is made.
- Cards land in the review queue with source **MCP** (`source_type=mcp`, label
  `MCP fact:`), not Ask. If the same fact is later finalized from an Ask dialog,
  ArchitectOS keeps the MCP card and does not add an Ask duplicate.

The project is resolved from the hook's working directory: the registered project
whose `root_path` contains that directory wins, and the most deeply nested root
wins for monorepos. No match falls back to the `architectos` project.

## Transport

The hook prefers the running app: it reads `data/architectos.runtime.json` for the
port and auth token and calls `POST /api/memory/turn` (or
`GET /api/memory/briefing`). If the app is not running it opens the store directly,
so hooks work with the desktop app closed.

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
| `ARCHITECTOS_HOOK_TIMEOUT` | HTTP timeout in seconds (default `5`) |
| `ARCHITECTOS_HOOK_OFFLINE=0` | Do not fall back to opening the store directly |
| `ARCHITECTOS_HOOK_DEBUG=1` | Append one line per event to `data/hooks.log` |

## Manual check

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"Remember: we deploy only from main.","cwd":"'$PWD'"}' \
  | python3 architectos_hook.py capture --client codex --event prompt
```

Silence means the turn was captured but there was nothing worth injecting yet.
Run it a second time on a related question and the stored memory comes back as
`additionalContext`. Facts that landed in review appear in the app under memory candidates with the
**MCP** chip.
