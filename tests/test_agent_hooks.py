from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos import agent_hook_service, agent_hooks
from backend.architectos.mcp_server import MemoryMCPServer, write_mcp_bind_file
from backend.architectos.models import Project
from backend.architectos.server import ArchitectOSHandler
from backend.architectos.service import ArchitectOSService


class HookNormalizationTests(unittest.TestCase):
    def test_claude_prompt_payload(self) -> None:
        turn = agent_hooks.normalize_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Remember: we deploy only from main.",
                "session_id": "s-1",
                "cwd": "/tmp/project",
            },
            client="claude",
            event="prompt",
        )
        assert turn is not None
        self.assertEqual(turn.event, agent_hooks.EVENT_PROMPT)
        self.assertEqual(turn.user_text, "Remember: we deploy only from main.")
        self.assertEqual(turn.assistant_text, "")
        self.assertEqual(turn.session_id, "s-1")
        self.assertEqual(turn.cwd, "/tmp/project")
        self.assertTrue(turn.can_inject)

    def test_codex_stop_uses_last_assistant_message(self) -> None:
        turn = agent_hooks.normalize_payload(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Switched the cache to Redis with a 15 minute TTL.",
                "cwd": "/tmp/project",
            },
            client="codex",
            event="response",
        )
        assert turn is not None
        self.assertEqual(turn.assistant_text, "Switched the cache to Redis with a 15 minute TTL.")
        self.assertEqual(turn.user_text, "")
        self.assertFalse(turn.can_inject)

    def test_cursor_event_inferred_from_payload_when_flag_missing(self) -> None:
        turn = agent_hooks.normalize_payload(
            {"hook_event_name": "beforeSubmitPrompt", "prompt": "Never commit secrets."},
            client="cursor",
        )
        assert turn is not None
        self.assertEqual(turn.event, agent_hooks.EVENT_PROMPT)
        self.assertFalse(turn.can_inject)

    def test_unknown_client_and_event_are_ignored(self) -> None:
        self.assertIsNone(agent_hooks.normalize_payload({"prompt": "hi"}, client="windsurf", event="prompt"))
        self.assertIsNone(agent_hooks.normalize_payload({"hook_event_name": "PreCompact"}, client="codex"))


_FILLER = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
    "quebec romeo sierra tango uniform victor whiskey xray yankee zulu amber basalt cobalt dune ember fjord"
).split()


def _seed(service: ArchitectOSService, project_id: str, count: int, word: str = "ledger") -> list[str]:
    """``count`` long, mutually distinct Lesson nodes that all match ``word``; returns their ids."""
    ids: list[str] = []
    for index in range(count):
        filler = " ".join(_FILLER[(index * 7 + step) % len(_FILLER)] + str(index) for step in range(40))
        node = service.repository.add_node(
            "Lesson",
            f"{word} rule {index}",
            "project",
            f"The {word} service must batch writes before replicating them. {filler}",
            project_id,
            None,
            0.8,
            {"source": "test"},
        )
        ids.append(node.id)
    return ids


class HookTimeoutTests(unittest.TestCase):
    def _timeouts(self, env: dict[str, str]) -> dict[str, float]:
        seen: dict[str, float] = {}
        events = iter(("session", "prompt", "response"))

        def refuse(address: tuple[str, int], timeout: float) -> object:
            seen[next(events)] = timeout
            raise ConnectionRefusedError("app closed")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**env, "ARCHITECTOS_HOOK_OFFLINE": "0"}), \
                mock.patch.object(socket, "create_connection", side_effect=refuse):
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )
            for raw_event, event, field in (
                ("SessionStart", "session", ""),
                ("UserPromptSubmit", "prompt", "prompt"),
                ("Stop", "response", "last_assistant_message"),
            ):
                payload = {"hook_event_name": raw_event, "cwd": str(root)}
                if field:
                    payload[field] = "How do we batch ledger writes?"
                agent_hooks.handle_capture(json.dumps(payload), client="claude", event=event, root=root)
        return seen

    def test_injecting_events_wait_two_seconds_and_capture_five(self) -> None:
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("ARCHITECTOS_HOOK_TIMEOUT", None)
            os.environ.pop("ARCHITECTOS_HOOK_INJECT_TIMEOUT", None)
            seen = self._timeouts({})
        self.assertEqual(seen, {"session": 2.0, "prompt": 2.0, "response": 5.0})

    def test_timeouts_are_overridable_per_kind(self) -> None:
        seen = self._timeouts({"ARCHITECTOS_HOOK_TIMEOUT": "7", "ARCHITECTOS_HOOK_INJECT_TIMEOUT": "0.5"})
        self.assertEqual(seen, {"session": 0.5, "prompt": 0.5, "response": 7.0})

    def test_a_trickling_reply_hits_one_deadline_for_the_whole_request(self) -> None:
        # The timeout bounds the whole exchange, not each socket operation: a reply
        # that keeps sending a byte at a time must not hold the agent past it.
        listener = socket.create_server(("127.0.0.1", 0))

        def trickle() -> None:
            conn, _ = listener.accept()
            with conn:
                conn.recv(65536)
                try:
                    for byte in b"HTTP/1.0 200 OK\r\n\r\n{}" + b" " * 40:
                        conn.sendall(bytes([byte]))
                        time.sleep(0.05)
                except OSError:
                    pass  # the hook gave up and closed

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        with listener:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                agent_hooks._exchange(f"http://127.0.0.1:{listener.getsockname()[1]}/api/memory/turn", "token", b"{}", 0.5)
            elapsed = time.monotonic() - started
            thread.join(timeout=5)
        self.assertLess(elapsed, 1.5)

    def test_turn_end_spools_a_turn_whose_connect_timed_out(self) -> None:
        # A connect that timed out never delivered the request: spooling it cannot
        # capture the turn twice, and not spooling it would lose the turn.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(socket, "create_connection", side_effect=TimeoutError("timed out")):
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )
            _hook(root, "Stop", cwd=root, last_assistant_message="Batch ledger writes per tenant.")
            spool = root / "data" / "hook_spool.jsonl"
            spooled = spool.read_text(encoding="utf-8").splitlines() if spool.exists() else []
        self.assertEqual(len(spooled), 1)

    def test_hook_pack_cuts_vector_search_at_800_ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 3)
            with mock.patch.object(service, "_vector_search_memory_timed", return_value=({}, 0.0, False)) as vector:
                service.capture_memory_turn("How do we batch ledger writes?", hook=True, capture=False)
                hook_timeout = vector.call_args.kwargs["timeout_s"]
                service.capture_memory_turn("How do we batch ledger writes?")
                default_timeout = vector.call_args.kwargs["timeout_s"]
        self.assertEqual(hook_timeout, 0.8)
        self.assertEqual(default_timeout, 2.5)

    def test_vector_timeout_is_a_request_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 2)
            with mock.patch.object(service, "_vector_search_memory_timed", return_value=({}, 0.0, False)) as vector:
                service.search_memory("ledger", project_id="architectos", vector_timeout_ms=300)
        self.assertEqual(vector.call_args.kwargs["timeout_s"], 0.3)


class HookBudgetTests(unittest.TestCase):
    QUESTION = "How do we batch ledger writes before replicating?"

    def test_hook_pack_fits_the_hook_budget_and_excerpt_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 12)
            full = service.context(self.QUESTION, project_id="architectos")
            hook = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
        self.assertGreater(len(full["context"]), 1600)
        injected = agent_hooks._injected_text(agent_hooks.render_stdout(
            agent_hooks.HookTurn(client="claude", event="prompt", raw_event="UserPromptSubmit"), hook
        ))
        self.assertLessEqual(len(injected), 1600, "prefix + pack stay within agent.hook_char_budget")
        self.assertLessEqual(agent_hooks.estimate_tokens_from_text(injected), 400)
        self.assertGreaterEqual(len(hook["hits"]), 1)
        lines = [line for line in hook["context"].splitlines() if line.startswith("- id=")]
        self.assertGreaterEqual(len(lines), 2)
        for line in lines[:-1]:  # the last line may be cut by the budget
            excerpt = line.split(": ", 1)[1].rsplit(" (evidence:", 1)[0]
            self.assertLessEqual(len(excerpt), 220)
        self.assertEqual(hook["atoms"], [])
        self.assertEqual(hook["reason"], "inject_only")

    def test_hook_char_budget_setting_is_read_and_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 12)
            service.repository.set_setting("agent", {"hook_char_budget": 2600})
            wide = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)["context"]
            service.repository.set_setting("agent", {"hook_char_budget": 300})
            settings = service.agent_hook_settings()
        self.assertGreater(len(wide), 1600)
        self.assertLessEqual(len(wide), 2600)
        self.assertEqual(settings["hook_char_budget"], 1301)  # the pack's 1200 floor + the hook prefix

    def test_injection_stays_within_the_smallest_hook_char_budget(self) -> None:
        # pack_memory_context never packs below its 1 200-character floor, so the
        # smallest allowed budget must leave room for that floor plus the hook prefix.
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 12)
            service.repository.set_setting("agent", {"hook_char_budget": 300})
            budget = service.agent_hook_settings()["hook_char_budget"]
            hook = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
        injected = agent_hooks._injected_text(agent_hooks.render_stdout(
            agent_hooks.HookTurn(client="claude", event="prompt", raw_event="UserPromptSubmit"), hook
        ))
        self.assertGreater(len(injected), budget - 200, "the pack fills the budget")
        self.assertLessEqual(len(injected), budget)

    def test_pack_without_hits_above_the_relevance_floor_is_not_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 4)
            service.repository.set_setting("agent", {"hook_min_score": 999})
            floored = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
            out = agent_hooks.render_stdout(
                agent_hooks.HookTurn(client="claude", event="prompt", raw_event="UserPromptSubmit"), floored
            )
            service.repository.set_setting("agent", {"hook_min_score": 0})
            open_floor = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
        self.assertEqual((floored["hits"], floored["context"], out), ([], "", ""))
        self.assertGreaterEqual(len(open_floor["hits"]), 1)

    def test_hook_prompt_asks_the_server_for_the_hook_pack(self) -> None:
        connection = mock.MagicMock()
        connection.__enter__.return_value.recv.return_value = b""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_OFFLINE": "0"}), \
                mock.patch.object(socket, "create_connection", return_value=connection):
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )
            agent_hooks.handle_capture(
                json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": self.QUESTION, "cwd": str(root)}),
                client="claude", event="prompt", root=root,
            )
        request = connection.__enter__.return_value.sendall.call_args.args[0]
        self.assertTrue(request.startswith(b"POST /api/memory/turn HTTP/1.0"))
        self.assertIs(json.loads(request.split(b"\r\n\r\n", 1)[1])["hook"], True)


class _RunningApp:
    """The app's HTTP handler on 127.0.0.1 with runtime.json pointing the hook at it."""

    def __init__(self, root: Path, service: ArchitectOSService) -> None:
        handler = type("SessionTestHandler", (ArchitectOSHandler,), {"log_message": lambda self, *args: None})
        handler.service = service
        self.runtime = root / "data" / "architectos.runtime.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.token = service.auth_token

    def __enter__(self) -> "_RunningApp":
        self.thread.start()
        self.runtime.write_text(
            json.dumps({"url": f"http://127.0.0.1:{self.server.server_port}", "auth_token": self.token}), encoding="utf-8"
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.runtime.unlink(missing_ok=True)
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


def _hook(root: Path, raw_event: str, *, cwd: Path, session: str = "", client: str = "claude", **fields: str) -> str:
    event = {"SessionStart": "session", "UserPromptSubmit": "prompt", "Stop": "response",
             "beforeSubmitPrompt": "prompt", "afterAgentResponse": "response"}[raw_event]
    payload: dict[str, str] = {"hook_event_name": raw_event, "cwd": str(cwd), **fields}
    if session:
        payload["session_id"] = session
    return agent_hooks.handle_capture(json.dumps(payload), client=client, event=event, root=root)


def _injected_ids(out: str) -> list[str]:
    if not out or out == "{}":
        return []
    return re.findall(r"^- id=(\S+)", json.loads(out)["hookSpecificOutput"]["additionalContext"], re.MULTILINE)


def _stable_section(out: str) -> bool:
    return bool(out) and "Stable memory" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _two_projects(root: Path, service: ArchitectOSService) -> tuple[Path, Path]:
    alpha, beta = root / "work" / "alpha", root / "work" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    service.repository.upsert_project(Project(id="alpha", name="Alpha", root_path=str(alpha)))
    service.repository.upsert_project(Project(id="beta", name="Beta", root_path=str(beta)))
    return alpha, beta


class HookServerModeTests(unittest.TestCase):
    QUESTION = "How do we batch ledger writes before replicating?"

    def test_hook_pack_skips_graph_expansion_other_callers_keep_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            _seed(service, "architectos", 4)
            expansion = service._pagerank_graph_expansion  # noqa: SLF001
            with mock.patch.object(service, "_pagerank_graph_expansion", wraps=expansion) as walk:
                hook = service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
                hook_walks = walk.call_count
                service.context(self.QUESTION, project_id="architectos")
                context_walks = walk.call_count - hook_walks
                service.capture_memory_turn(self.QUESTION, capture=False)
                mcp_walks = walk.call_count - hook_walks - context_walks
        self.assertEqual((hook_walks, context_walks, mcp_walks), (0, 1, 1))
        self.assertGreaterEqual(len(hook["hits"]), 1)
        self.assertIn("cache_ms", hook["timing"])

    def test_hook_mode_keeps_project_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            alpha_ids = _seed(service, "alpha", 2)
            beta_ids = _seed(service, "beta", 2)
            shared = service.repository.add_node(
                "Lesson", "ledger rule shared", "global", "The ledger service must batch writes everywhere.",
                None, None, 0.8, {"source": "test"},
            ).id
            # A cross-project edge must not carry a beta node into alpha's pack either.
            service.repository.add_edge(alpha_ids[0], beta_ids[0], "RELATED_TO", "project")
            with _RunningApp(root, service):
                out = _hook(root, "UserPromptSubmit", cwd=alpha, session="iso", prompt=self.QUESTION)
            cache = json.loads((root / "data" / agent_hooks.BRIEFING_CACHE).read_text(encoding="utf-8"))
        injected = set(_injected_ids(out))
        self.assertTrue(injected & set(alpha_ids))
        self.assertIn(shared, injected)
        self.assertFalse(injected & set(beta_ids))
        self.assertEqual(set(cache["projects"]), {"alpha"})
        cached = {item[0] for item in cache["projects"]["alpha"]["hits"]}
        self.assertFalse(cached & set(beta_ids))


class HookSessionTests(unittest.TestCase):
    QUESTIONS = (
        "How do we batch ledger writes?",
        "What does the ledger service replicate?",
        "Where does the ledger archive tier live?",
        "Which ledger rule covers batching?",
    )

    def test_stable_layer_stays_due_until_a_prompt_actually_shows_it(self) -> None:
        # The app timed out and the offline cache had no stable lines: nothing was
        # shown, so the next prompt must ask for the stable layer again.
        sent: list[dict[str, object]] = []

        def exchange(url: str, token: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
            if not sent:
                sent.append({})
                raise TimeoutError("timed out")
            sent.append(json.loads(body or b"{}"))
            return 200, json.dumps({"project_id": "architectos", "context": "", "hits": [], "stable": []}).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 8)
            # The app cached a hits-only pack: the offline answer has hits but no stable lines.
            service.capture_memory_turn(self.QUESTIONS[0], hook=True, capture=False, include_stable=False)
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )
            with mock.patch.object(agent_hooks, "_exchange", side_effect=exchange):
                first = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[0])
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[1])
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[2])
        self.assertTrue(_injected_ids(first))
        self.assertFalse(_stable_section(first))
        self.assertEqual([item.get("include_stable") for item in sent[1:]], [True, False])

    def test_a_session_never_gets_the_same_id_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 16)
            with _RunningApp(root, service):
                _hook(root, "SessionStart", cwd=root, session="s1", source="startup")
                turns = [_injected_ids(_hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=q)) for q in self.QUESTIONS]
        flat = [node_id for ids in turns for node_id in ids]
        self.assertTrue(turns[0] and turns[1], turns)
        self.assertEqual(len(flat), len(set(flat)), turns)

    def test_stable_layer_returns_only_after_n_prompts_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 12)
            for index in range(3):
                service.repository.add_node("Constraint", f"ledger constraint {index}", "project",
                                            f"Ledger writes must stay idempotent, rule {index}.", "architectos")
            service.repository.set_setting("agent", {"hook_stable_every_turns": 3})
            with _RunningApp(root, service):
                start = _hook(root, "SessionStart", cwd=root, session="s1", source="startup")
                prompts = [_hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=q) for q in self.QUESTIONS]
        self.assertTrue(_stable_section(start))
        self.assertEqual([_stable_section(out) for out in prompts], [False, False, True, False])

    def test_first_prompt_plays_session_start_for_clients_without_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 12)
            service.repository.add_node("Constraint", "ledger constraint", "project", "Ledger writes must stay idempotent.", "architectos")
            with _RunningApp(root, service):
                first = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[0])
                second = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[1])
        self.assertTrue(_stable_section(first))
        self.assertFalse(_stable_section(second))

    def test_compaction_start_lets_earlier_ids_back_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 6)
            with _RunningApp(root, service):
                first = _injected_ids(_hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[0]))
                again = _injected_ids(_hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[0]))
                _hook(root, "SessionStart", cwd=root, session="s1", source="compact")
                after = _injected_ids(_hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTIONS[0]))
        self.assertTrue(first)
        self.assertFalse(set(first) & set(again))
        self.assertTrue(set(first) & set(after))

    def test_without_session_id_every_prompt_gets_the_full_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 12)
            with _RunningApp(root, service):
                first = _injected_ids(_hook(root, "UserPromptSubmit", cwd=root, prompt=self.QUESTIONS[0]))
                second = _injected_ids(_hook(root, "UserPromptSubmit", cwd=root, prompt=self.QUESTIONS[0]))
            self.assertFalse((root / "data" / "hook_sessions").exists())
        self.assertTrue(first)
        self.assertEqual(set(first), set(second))

    def test_session_state_is_kept_per_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            alpha_ids = set(_seed(service, "alpha", 2))
            beta_ids = set(_seed(service, "beta", 2))
            shared = service.repository.add_node("Lesson", "ledger rule batch writes before replicating", "global",
                                                 "Every ledger service batches writes before replicating them.", None).id
            question = "Which ledger rule says to batch writes before replicating?"
            with _RunningApp(root, service):
                in_alpha = [set(_injected_ids(_hook(root, "UserPromptSubmit", cwd=alpha, session="same", prompt=question))) for _ in range(2)]
                in_beta = set(_injected_ids(_hook(root, "UserPromptSubmit", cwd=beta, session="same", prompt=question)))
            state = json.loads(next((root / "data" / "hook_sessions").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn(shared, in_alpha[0])
        self.assertNotIn(shared, in_alpha[1], "one project never sees an id twice")
        self.assertIn(shared, in_beta, "another project's history does not hide a node from this one")
        self.assertFalse(in_beta & alpha_ids)
        self.assertFalse((in_alpha[0] | in_alpha[1]) & beta_ids)
        self.assertEqual(set(state["projects"]), {"alpha", "beta"})
        self.assertFalse(set(state["projects"]["beta"]["seen"]) & alpha_ids)

    def test_unreadable_session_file_is_replaced_with_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 6)
            session = agent_hooks.HookSession.open(root, "broken")
            session.path.parent.mkdir(parents=True)
            session.path.write_text("{not json", encoding="utf-8")
            with _RunningApp(root, service), self.assertLogs("backend.architectos.agent_hooks", level="WARNING") as logs:
                out = _hook(root, "UserPromptSubmit", cwd=root, session="broken", prompt=self.QUESTIONS[0])
            state = json.loads(session.path.read_text(encoding="utf-8"))
        self.assertTrue(_injected_ids(out))
        self.assertIn("unreadable", "\n".join(logs.output))
        self.assertEqual(state["projects"]["architectos"]["turn"], 1)

    def test_stale_session_files_are_pruned_when_a_session_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "data" / "hook_sessions"
            folder.mkdir(parents=True)
            stale, fresh = folder / "stale.json", folder / "fresh.json"
            stale.write_text("{}", encoding="utf-8")
            fresh.write_text("{}", encoding="utf-8")
            old = time.time() - 4 * 24 * 3600
            os.utime(stale, (old, old))
            agent_hooks.HookSession.open(root, "new-session")
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())


class HookCaptureOnStopTests(unittest.TestCase):
    PROMPT = "Remember: QuartzDeployGate must approve every release from main."
    ANSWER = "Understood, QuartzDeployGate approves each release before it ships from main."

    def _calls(self, service: ArchitectOSService) -> mock.MagicMock:
        return mock.patch.object(service, "capture_memory_turn", wraps=service.capture_memory_turn)

    @staticmethod
    def _captures(spy: mock.MagicMock) -> list[tuple[str, str]]:
        return [(call.args[0], call.args[1]) for call in spy.call_args_list if call.kwargs.get("capture", True)]

    def _requests(self, root: Path, steps: list[tuple[str, Path, dict[str, str]]]) -> list[dict[str, object]]:
        """POST bodies the hook sent for ``steps`` (raw event, cwd, fields) against a stub app."""
        sent: list[dict[str, object]] = []

        def exchange(url: str, token: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
            sent.append(json.loads(body or b"{}"))
            return 200, json.dumps({"project_id": "architectos", "context": "", "hits": [], "stable": []}).encode("utf-8")

        (root / "data").mkdir(exist_ok=True)
        (root / "data" / "architectos.runtime.json").write_text(
            json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
        )
        with mock.patch.object(agent_hooks, "_exchange", side_effect=exchange):
            for raw_event, cwd, fields in steps:
                _hook(root, raw_event, cwd=cwd, session="s1", **fields)
        return sent

    @staticmethod
    def _spooled(root: Path) -> list[tuple[str, str]]:
        spool = root / "data" / "hook_spool.jsonl"
        lines = spool.read_text(encoding="utf-8").splitlines() if spool.exists() else []
        return [(json.loads(line)["user_text"], json.loads(line)["assistant_text"]) for line in lines]

    def test_session_changes_wait_for_the_session_lock(self) -> None:
        # Another hook of the same session holding its lock blocks this one's change.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            lock = agent_hooks.HookSession._path(root, "s1").with_suffix(".lock")  # noqa: SLF001
            done = threading.Event()

            def prompt() -> None:
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.PROMPT)
                done.set()

            with mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_OFFLINE": "0"}), agent_hooks.file_lock(lock):
                thread = threading.Thread(target=prompt, daemon=True)
                thread.start()
                blocked = not done.wait(0.5)
            thread.join(timeout=10)
            saved = bool(list((root / "data" / "hook_sessions").glob("*.json")))
        self.assertTrue(blocked)
        self.assertTrue(saved)

    def test_next_prompt_during_a_slow_turn_end_neither_doubles_nor_loses_a_question(self) -> None:
        # The next prompt's hook runs while turn end still waits for the app: the first
        # question is captured once (by turn end), and the second stays held.
        first, second = self.PROMPT, "Which team owns QuartzDeployGate?"
        sent: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )

            def exchange(url: str, token: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
                request = json.loads(body or b"{}")
                sent.append(request)
                if request.get("capture") and request.get("assistant_text") == self.ANSWER:
                    _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=second)  # arrives meanwhile
                return 200, json.dumps({"project_id": "architectos", "context": "", "hits": [], "stable": []}).encode("utf-8")

            with mock.patch.object(agent_hooks, "_exchange", side_effect=exchange):
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=first)
                _hook(root, "Stop", cwd=root, session="s1", last_assistant_message=self.ANSWER)
                _hook(root, "Stop", cwd=root, session="s1", last_assistant_message="Team Quartz owns it.")
            spooled = self._spooled(root)
        captures = [(item["user_text"], item["assistant_text"]) for item in sent if item.get("capture")]
        self.assertEqual(captures, [(first, self.ANSWER), (second, "Team Quartz owns it.")])
        self.assertEqual(spooled, [])

    def test_turn_end_pairs_the_question_after_the_agent_changed_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "packages" / "billing"
            nested.mkdir(parents=True)
            sent = self._requests(root, [
                ("UserPromptSubmit", root, {"prompt": self.PROMPT}),
                ("Stop", nested, {"last_assistant_message": self.ANSWER}),
            ])
        captures = [(item["user_text"], item["assistant_text"]) for item in sent if item.get("capture")]
        self.assertEqual(captures, [(self.PROMPT, self.ANSWER)])

    def test_interrupted_turn_still_captures_its_question(self) -> None:
        # Turn end with no answer text (the user stopped the agent) still captures the
        # question; a prompt whose turn never ended is spooled before the next one
        # replaces it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sent = self._requests(root, [
                ("UserPromptSubmit", root, {"prompt": self.PROMPT}),
                ("Stop", root, {"last_assistant_message": ""}),
                ("UserPromptSubmit", root, {"prompt": "First unanswered question about QuartzDeployGate?"}),
                ("UserPromptSubmit", root, {"prompt": "Second question about QuartzDeployGate?"}),
            ])
            spooled = self._spooled(root)
        captures = [(item["user_text"], item["assistant_text"]) for item in sent if item.get("capture")]
        self.assertEqual(captures, [(self.PROMPT, "")])
        self.assertEqual(spooled, [("First unanswered question about QuartzDeployGate?", "")])

    def test_turn_end_spools_the_turn_when_the_app_answers_with_an_error(self) -> None:
        # The question leaves the session file before the send; an app error (500 from
        # "database is locked", a cut reply) must not lose the turn.
        for status, body in ((500, b'{"error": "database is locked"}'), (200, b"")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data").mkdir()
                (root / "data" / "architectos.runtime.json").write_text(
                    json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
                )
                with mock.patch.object(agent_hooks, "_exchange", return_value=(status, body)):
                    _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.PROMPT)
                    _hook(root, "Stop", cwd=root, session="s1", last_assistant_message=self.ANSWER)
                spool = root / "data" / "hook_spool.jsonl"
                spooled = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines()] if spool.exists() else []
                self.assertEqual([(item["user_text"], item["assistant_text"]) for item in spooled], [(self.PROMPT, self.ANSWER)])

    def test_prompt_only_injects_and_turn_end_captures_question_with_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            with _RunningApp(root, service), self._calls(service) as spy:
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.PROMPT)
                _hook(root, "Stop", cwd=root, session="s1", last_assistant_message=self.ANSWER)
            stats = service.memory_capture_stats(days=1, project_id="architectos")["agent_turns"]
        self.assertEqual(self._captures(spy), [(self.PROMPT, self.ANSWER)])
        self.assertEqual((stats["prompt"]["count"], stats["response"]["count"]), (1, 1))

    def test_question_leaves_the_session_file_once_captured_and_never_reaches_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": "1"}):
            root = Path(tmp)
            service = ArchitectOSService(root)
            with _RunningApp(root, service):
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.PROMPT)
                session_file = next((root / "data" / "hook_sessions").glob("*.json"))
                held = session_file.read_text(encoding="utf-8")
                _hook(root, "Stop", cwd=root, session="s1", last_assistant_message=self.ANSWER)
                after = session_file.read_text(encoding="utf-8")
            log = (root / "data" / "hooks.log").read_text(encoding="utf-8")
        self.assertIn("QuartzDeployGate", held)
        self.assertNotIn("QuartzDeployGate", after)
        self.assertNotIn("QuartzDeployGate", log)

    def test_cursor_pairs_before_submit_with_after_agent_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            with _RunningApp(root, service), self._calls(service) as spy:
                prompt_out = _hook(root, "beforeSubmitPrompt", cwd=root, session="c1", client="cursor", prompt=self.PROMPT)
                calls_after_prompt = spy.call_count
                _hook(root, "afterAgentResponse", cwd=root, session="c1", client="cursor", text=self.ANSWER)
        self.assertEqual((prompt_out, calls_after_prompt), ("", 0))
        self.assertEqual(self._captures(spy), [(self.PROMPT, self.ANSWER)])

    def test_without_session_each_event_still_captures_its_own_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            with _RunningApp(root, service), self._calls(service) as spy:
                _hook(root, "UserPromptSubmit", cwd=root, prompt=self.PROMPT)
                _hook(root, "Stop", cwd=root, last_assistant_message=self.ANSWER)
        self.assertEqual(self._captures(spy), [(self.PROMPT, ""), ("", self.ANSWER)])

    def test_held_question_is_paired_only_within_its_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            with _RunningApp(root, service), self._calls(service) as spy:
                _hook(root, "SessionStart", cwd=alpha, session="same")
                _hook(root, "SessionStart", cwd=beta, session="same")
                _hook(root, "UserPromptSubmit", cwd=alpha, session="same", prompt=self.PROMPT)
                _hook(root, "Stop", cwd=beta, session="same", last_assistant_message="Beta answer about invoices.")
                _hook(root, "Stop", cwd=alpha, session="same", last_assistant_message=self.ANSWER)
        captured = [(call.args[0], call.kwargs.get("cwd")) for call in spy.call_args_list if call.kwargs.get("capture", True)]
        self.assertEqual(captured, [("", str(beta)), (self.PROMPT, str(alpha))])


class HookCacheInvalidationTests(unittest.TestCase):
    """The offline briefing cache follows the store: what changed or left a project leaves the cache."""

    QUESTION = "How do we batch ledger writes?"

    def _cached(self, root: Path) -> tuple[ArchitectOSService, str, list[str]]:
        """A service whose cache holds the stable layer and a hits pack; (service, stable id, hit ids)."""
        service = ArchitectOSService(root)
        hit_ids = _seed(service, "architectos", 8)
        stable = service.repository.add_node("Constraint", "ledger constraint", "project", "Ledger writes must stay idempotent.", "architectos")
        service.hook_briefing(cwd=str(root))
        service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
        return service, stable.id, hit_ids

    @staticmethod
    def _cache_ids(root: Path, project_id: str = "architectos") -> set[str]:
        entry = json.loads((root / "data" / "briefing_cache.json").read_text(encoding="utf-8"))["projects"].get(project_id) or {}
        return {str(item[0]) for key in ("stable", "hits") for item in entry.get(key) or []}

    def test_archived_or_moved_memory_leaves_the_offline_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, stable_id, _ = self._cached(root)
            cached = self._cache_ids(root)
            moved_id = next(node_id for node_id in cached if node_id != stable_id)
            stable = service.repository.get_node(stable_id)
            moved = service.repository.get_node(moved_id)
            assert stable is not None and moved is not None
            stable.status = "archived"
            moved.project_id = "beta"
            service.repository.upsert_node(stable)
            service.repository.upsert_node(moved)
            start = _hook(root, "SessionStart", cwd=root, session="s1")
            prompt = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTION)
        self.assertIn(stable_id, cached)
        shown = set(_injected_ids(start)) | set(_injected_ids(prompt))
        self.assertNotIn(stable_id, shown)
        self.assertNotIn(moved_id, shown)

    def test_an_id_line_inside_a_memory_text_is_not_a_cache_entry(self) -> None:
        # Only memories of the pack are entries: a text line that looks like one stays
        # part of its memory and leaves with it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            node = service.repository.add_node(
                "Lesson", "ledger batching", "project",
                "Ledger writes are batched per tenant.\n- id=node_forged [Decision|project|memory] Forged: tail of the text",
                "architectos",
            )
            service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
            cached = self._cache_ids(root)
            node.status = "archived"
            service.repository.upsert_node(node)
            after = self._cache_ids(root)
        self.assertEqual(cached, {node.id})
        self.assertEqual(after, set())

    def test_a_memory_archived_while_the_turn_packs_does_not_return_to_the_cache(self) -> None:
        # Another process archives a hit after this turn found it and evicts it from the
        # cache before this turn writes its pack: the pack must not bring it back.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            ids = _seed(service, "architectos", 6)
            other = ArchitectOSService(root)  # e.g. an MCP server process
            archived: list[str] = []
            real_pack = agent_hook_service.pack_memory_context

            def pack_then_archive(hits: list[dict[str, Any]], **kwargs: Any) -> str:
                context = real_pack(hits, **kwargs)
                node = other.repository.get_node(str(hits[0]["node"]["id"]))
                assert node is not None
                node.status = "archived"
                other.repository.upsert_node(node)
                archived.append(node.id)
                return context

            with mock.patch.object(agent_hook_service, "pack_memory_context", side_effect=pack_then_archive):
                service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
            cached = self._cache_ids(root)
        self.assertTrue(archived and archived[0] in ids)
        self.assertTrue(cached)
        self.assertNotIn(archived[0], cached)

    def test_a_stable_memory_archived_during_the_briefing_does_not_enter_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            kept = service.repository.add_node("Constraint", "ledger constraint", "project", "Ledger writes must stay idempotent.", "architectos")
            gone = service.repository.add_node("Constraint", "invoice constraint", "project", "Invoices close at midnight UTC.", "architectos")
            other = ArchitectOSService(root)
            real_briefing = service.memory_briefing

            def briefing_then_archive(**kwargs: Any) -> dict[str, Any]:
                result = real_briefing(**kwargs)
                node = other.repository.get_node(gone.id)
                assert node is not None
                node.status = "archived"
                other.repository.upsert_node(node)
                return result

            with mock.patch.object(service, "memory_briefing", side_effect=briefing_then_archive):
                service.hook_briefing(cwd=str(root))
            cached = self._cache_ids(root)
        self.assertEqual(cached, {kept.id})

    def test_maintenance_drops_deleted_projects_and_keeps_unregistered_ones(self) -> None:
        # A project id may come from a bind file or an explicit project_id without a
        # projects row; only a project that was registered and is gone is dropped.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, _ = _two_projects(root, service)
            _seed(service, "alpha", 6)
            _seed(service, "ghost", 6)
            service.capture_memory_turn(self.QUESTION, cwd=str(alpha), hook=True, capture=False, include_stable=False)
            service.capture_memory_turn(self.QUESTION, project_id="ghost", hook=True, capture=False, include_stable=False)
            before = set(json.loads((root / "data" / "briefing_cache.json").read_text(encoding="utf-8"))["projects"])
            with service.repository._connect() as conn:  # noqa: SLF001 - a project removed outside the app's API
                conn.execute("DELETE FROM projects WHERE id = 'alpha'")
            service.hook_maintenance()
            after = set(json.loads((root / "data" / "briefing_cache.json").read_text(encoding="utf-8"))["projects"])
        self.assertEqual(before, {"alpha", "ghost"})
        self.assertEqual(after, {"ghost"})

    def test_clearing_a_source_drops_only_its_memories_from_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            project_id = service.create_project({"id": "wipe-docs", "name": "Wipe Docs", "root_path": str(root)})["id"]
            sources = {item["kind"]: item["id"] for item in service.list_project_sources(project_id)}
            texts = {
                "docs": "Ledger writes are batched per tenant before replication.",
                "adr": "How we batch ledger writes: the nightly job flushes invoices in groups of 500.",
            }
            added = {
                kind: service.add_memory({
                    "project_id": project_id, "label": f"Ledger rule from {kind}", "type": "Lesson", "scope": "project",
                    "text": text, "source_id": sources[kind],
                })["id"]
                for kind, text in texts.items()
            }
            service.repository.set_setting("agent", {"hook_min_score": 0})  # both memories in the pack
            service.capture_memory_turn(self.QUESTION, project_id=project_id, hook=True, capture=False, include_stable=False)
            before = self._cache_ids(root, project_id)
            service.clear_source_memory(sources["docs"], confirm=True)
            after = self._cache_ids(root, project_id)
        self.assertEqual(before, set(added.values()))
        self.assertEqual(after, {added["adr"]})

    def test_cache_writes_wait_for_the_cross_process_lock(self) -> None:
        # Another process (an MCP server) holding the cache lock blocks this writer.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 4)
            path = root / "data" / agent_hooks.BRIEFING_CACHE
            done = threading.Event()

            def write() -> None:
                service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
                done.set()

            with agent_hooks.file_lock(path.with_name(f"{agent_hooks.BRIEFING_CACHE}.lock")):
                thread = threading.Thread(target=write, daemon=True)
                thread.start()
                blocked = not done.wait(0.5)
                written_while_held = path.exists()
            thread.join(timeout=10)
            written = path.exists()
        self.assertTrue(blocked)
        self.assertFalse(written_while_held)
        self.assertTrue(written)

    def test_a_prompt_that_shows_the_stable_layer_renews_its_cached_lines(self) -> None:
        # Only SessionStart rewrote the stable lines; a prompt that got the stable layer
        # from the store re-checks the cached lines and renews their age.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, stable_id, _ = self._cached(root)
            path = root / "data" / "briefing_cache.json"
            cache = json.loads(path.read_text(encoding="utf-8"))
            cache["projects"]["architectos"]["stable_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text(json.dumps(cache), encoding="utf-8")
            service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=True)
            start = _hook(root, "SessionStart", cwd=root, session="s1")
        self.assertIn(stable_id, _injected_ids(start))

    def test_access_telemetry_keeps_the_cached_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, _, _ = self._cached(root)
            cached = self._cache_ids(root)
            nodes = [node for node in (service.repository.get_node(node_id) for node_id in cached) if node is not None]
            service.memory_lifecycle.refresh_nodes([node.id for node in nodes])  # the access bump of a search
            for node in nodes:
                node.metadata = {**node.metadata, "access_count": 99, "memory_score": 0.9}
                service.repository.upsert_node(node)  # a metadata-only rewrite
            after = self._cache_ids(root)
        self.assertGreaterEqual(len(cached), 2)
        self.assertEqual(after, cached)

    def test_wiping_a_project_drops_its_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service, _, _ = self._cached(root)
            before = self._cache_ids(root)
            service.repository.upsert_project(Project(id="architectos", name="ArchitectOS", root_path=str(root)))
            service.wipe_project_memory("architectos", confirm=True)
            after = self._cache_ids(root)
            prompt = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTION)
        self.assertTrue(before)
        self.assertEqual(after, set())
        self.assertEqual(_injected_ids(prompt), [])

    def test_cache_ttl_counts_from_each_projects_own_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            _seed(service, "alpha", 6)
            _seed(service, "beta", 6)
            service.capture_memory_turn(self.QUESTION, cwd=str(alpha), hook=True, capture=False, include_stable=False)
            path = root / "data" / "briefing_cache.json"
            cache = json.loads(path.read_text(encoding="utf-8"))
            stale = "2000-01-01T00:00:00+00:00"
            cache["projects"]["alpha"] = {**cache["projects"]["alpha"], "updated_at": stale, "stable_at": stale, "hits_at": stale}
            path.write_text(json.dumps(cache), encoding="utf-8")
            # A later write for another project must not renew alpha's lines.
            service.capture_memory_turn(self.QUESTION, cwd=str(beta), hook=True, capture=False, include_stable=False)
            from_alpha = _hook(root, "UserPromptSubmit", cwd=alpha, session="a", prompt=self.QUESTION)
            from_beta = _hook(root, "UserPromptSubmit", cwd=beta, session="b", prompt=self.QUESTION)
        self.assertEqual(_injected_ids(from_alpha), [])
        self.assertTrue(_injected_ids(from_beta))


class HookOfflineTests(unittest.TestCase):
    """The app is closed: no runtime.json, the hook must not open the store."""

    QUESTION = "How do we batch ledger writes?"

    def _no_store(self) -> mock._patch:  # type: ignore[type-arg]
        return mock.patch("backend.architectos.service.ArchitectOSService.__init__", side_effect=AssertionError("store opened"))

    def test_offline_events_use_the_cache_and_spool_without_opening_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 8)
            service.repository.add_node("Constraint", "ledger constraint", "project", "Ledger writes must stay idempotent.", "architectos")
            service.hook_briefing(cwd=str(root))
            service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
            with self._no_store():
                start = _hook(root, "SessionStart", cwd=root, session="s1")
                first = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTION)
                second = _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTION)
                stop = _hook(root, "Stop", cwd=root, session="s1", last_assistant_message="Batch them per tenant.")
            spooled = [json.loads(line) for line in (root / "data" / "hook_spool.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(_stable_section(start))
        self.assertTrue(_injected_ids(first))
        self.assertFalse(_stable_section(first), "the stable layer came with SessionStart")
        self.assertFalse(set(_injected_ids(first)) & set(_injected_ids(second)))
        self.assertEqual(stop, "")
        # The first prompt's turn never ended: the second prompt spools its question.
        self.assertEqual([(item["project_id"], item["user_text"], item["assistant_text"]) for item in spooled],
                         [("architectos", self.QUESTION, ""), ("architectos", self.QUESTION, "Batch them per tenant.")])

    def test_offline_briefing_keeps_every_multiline_fact(self) -> None:
        facts = ("Ledger writes must stay idempotent.", "Invoices close at midnight UTC.", "Refunds need two approvals.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            for index, fact in enumerate(facts):
                text = f"Durable fact captured from an MCP agent turn (not a full transcript).\n\n{fact}"
                service.repository.add_node("Constraint", f"MCP fact {index}", "project", text, "architectos")
            service.hook_briefing(cwd=str(root))
            with self._no_store():
                start = _hook(root, "SessionStart", cwd=root, session="s1")
        context = json.loads(start)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(len(_injected_ids(start)), 3)
        for fact in facts:
            self.assertIn(fact, context)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_hook_files_are_private_to_the_owner(self) -> None:
        # Session files hold the pending question, the spool holds question and answer,
        # the briefing cache holds memory lines: owner-only, like the database.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 4)
            service.hook_briefing(cwd=str(root))
            previous = os.umask(0o022)
            try:
                _hook(root, "UserPromptSubmit", cwd=root, session="s1", prompt=self.QUESTION)
                _hook(root, "Stop", cwd=root, session="s1", last_assistant_message="Batch them per tenant.")
                service.capture_memory_turn(self.QUESTION, hook=True, capture=False, include_stable=False)
            finally:
                os.umask(previous)
            data = root / "data"
            files = [data / "briefing_cache.json", data / "hook_spool.jsonl", *(data / "hook_sessions").glob("*.json")]
            modes = {path.name: oct(path.stat().st_mode & 0o777) for path in files}
        self.assertEqual(len(modes), 3)
        self.assertEqual(set(modes.values()), {"0o600"}, modes)

    def test_spool_keeps_records_with_unicode_line_separators(self) -> None:
        # json.dumps(ensure_ascii=False) leaves U+2028, U+2029 and U+0085 raw, and
        # str.splitlines() breaks on them; only "\n" ends a spool record.
        texts = ("Ledger rule\u2028one", "Ledger rule\u2029two", "Ledger rule\x85three")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            for text in texts:
                agent_hooks.spool_turn(root, {"project_id": "architectos", "user_text": text, "assistant_text": "Noted."})
            status = agent_hooks.spool_status(root)
            with mock.patch.object(service, "capture_memory_turn") as capture:
                stats = service.drain_hook_spool()
        self.assertEqual(status["records"], 3)
        self.assertEqual((stats["records"], stats["captured"], stats["dropped"]), (3, 3, 0))
        self.assertEqual([call.args[0] for call in capture.call_args_list], list(texts))

    def test_app_replays_the_spool_into_each_records_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            service.hook_maintenance()
            with self._no_store():
                for cwd, word in ((alpha, "AlphaQuartzRule"), (beta, "BetaQuartzRule")):
                    _hook(root, "UserPromptSubmit", cwd=cwd, session="same", prompt=f"Remember: {word} must guard every release.")
                    _hook(root, "Stop", cwd=cwd, session="same", last_assistant_message=f"Noted: {word} guards every release.")
            with mock.patch.object(service, "capture_memory_turn", wraps=service.capture_memory_turn) as spy:
                drained = service.drain_hook_spool()
            alpha_hits = json.dumps(service.search_memory("AlphaQuartzRule BetaQuartzRule", project_id="alpha")["hits"])
            beta_hits = json.dumps(service.search_memory("AlphaQuartzRule BetaQuartzRule", project_id="beta")["hits"])
            leftover = list((root / "data").glob("hook_spool*"))
        self.assertEqual(drained["captured"], 2)
        self.assertEqual(sorted(call.kwargs["project_id"] for call in spy.call_args_list), ["alpha", "beta"])
        self.assertIn("AlphaQuartzRule", alpha_hits)
        self.assertNotIn("BetaQuartzRule", alpha_hits)
        self.assertIn("BetaQuartzRule", beta_hits)
        self.assertNotIn("AlphaQuartzRule", beta_hits)
        self.assertEqual(leftover, [])

    def test_offline_injection_comes_only_from_the_folders_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            alpha, beta = _two_projects(root, service)
            alpha_ids = set(_seed(service, "alpha", 4))
            beta_ids = set(_seed(service, "beta", 4))
            bound = root / "work" / "elsewhere"
            bound.mkdir()
            write_mcp_bind_file(bound, "beta", "Beta")
            for project_id in ("alpha", "beta"):
                service.capture_memory_turn(self.QUESTION, project_id=project_id, hook=True, capture=False)
            with self._no_store():
                in_alpha = set(_injected_ids(_hook(root, "UserPromptSubmit", cwd=alpha, session="same", prompt=self.QUESTION)))
                in_beta = set(_injected_ids(_hook(root, "UserPromptSubmit", cwd=beta, session="same", prompt=self.QUESTION)))
                in_bound = set(_injected_ids(_hook(root, "UserPromptSubmit", cwd=bound, prompt=self.QUESTION)))
        self.assertTrue(in_alpha and in_alpha <= alpha_ids, in_alpha)
        self.assertTrue(in_beta and in_beta <= beta_ids, in_beta)
        self.assertTrue(in_bound and in_bound <= beta_ids, in_bound)

    def test_offline_zero_stays_silent_and_spools_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_OFFLINE": "0"}):
            root = Path(tmp)
            service = ArchitectOSService(root)
            _seed(service, "architectos", 4)
            service.capture_memory_turn(self.QUESTION, hook=True, capture=False)
            prompt = _hook(root, "UserPromptSubmit", cwd=root, prompt=self.QUESTION)
            stop = _hook(root, "Stop", cwd=root, client="codex", last_assistant_message="Batch them.")
            self.assertFalse((root / "data" / "hook_spool.jsonl").exists())
        self.assertEqual((prompt, stop), ("", "{}"))

    def test_capture_spools_when_the_app_is_unreachable_but_not_when_it_timed_out(self) -> None:
        spooled: dict[str, bool] = {}
        for failure in ("unreachable", "timeout"):
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(agent_hooks, "_request_json", return_value=(None, failure)):
                root = Path(tmp)
                (root / "data").mkdir()
                (root / "data" / "architectos.runtime.json").write_text(
                    json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
                )
                _hook(root, "Stop", cwd=root, last_assistant_message="Batch ledger writes per tenant.")
                spooled[failure] = (root / "data" / "hook_spool.jsonl").exists()
        self.assertEqual(spooled, {"unreachable": True, "timeout": False})

    def test_failing_spool_records_are_retried_then_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            agent_hooks.spool_turn(root, {"ts": "2026-11-04T00:00:00+00:00", "project_id": "architectos",
                                          "cwd": str(root), "client": "claude", "user_text": "", "assistant_text": "Batch it."})
            with mock.patch.object(service, "capture_memory_turn", side_effect=RuntimeError("db locked")), \
                    self.assertLogs("architectos.service", level="ERROR"):
                rounds = [service.drain_hook_spool() for _ in range(3)]
            left = (root / "data" / "hook_spool.jsonl").exists()
        self.assertEqual([item["failed"] for item in rounds], [1, 1, 0])
        self.assertEqual(rounds[-1]["dropped"], 1)
        self.assertFalse(left)

    def test_app_start_drains_the_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            agent_hooks.spool_turn(root, {"ts": "2026-11-04T00:00:00+00:00", "project_id": "architectos", "cwd": str(root),
                                          "client": "claude", "user_text": "Remember: OnyxGate signs releases.",
                                          "assistant_text": "Noted, OnyxGate signs every release."})
            service._chat_session_stop.set()
            service._chat_session_idle_loop(initial_delay_s=0, interval_s=0)
            found = json.dumps(service.search_memory("OnyxGate", project_id="architectos")["hits"])
            self.assertFalse((root / "data" / "hook_spool.jsonl").exists())
        self.assertIn("OnyxGate", found)

    def test_doctor_reports_the_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_ROOT": tmp}):
            root = Path(tmp)
            for index in range(2):
                agent_hooks.spool_turn(root, {"ts": "2026-11-04T00:00:00+00:00", "project_id": "architectos",
                                              "user_text": f"q{index}", "assistant_text": "a"})
            offline = agent_hooks.status(project_root=root)["offline"]
        self.assertEqual(offline["spool"]["records"], 2)
        self.assertGreater(offline["spool"]["bytes"], 0)
        self.assertGreater(offline["spool"]["oldest_age_s"], 0)


class HookRenderTests(unittest.TestCase):
    def test_injecting_client_returns_additional_context(self) -> None:
        turn = agent_hooks.HookTurn(client="codex", event=agent_hooks.EVENT_PROMPT, raw_event="UserPromptSubmit")
        rendered = json.loads(agent_hooks.render_stdout(
            turn,
            {"context": "Constraint: no secrets in git.", "hits": [{"node": {"id": "n1"}}]},
        ))
        specific = rendered["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertIn("no secrets in git", specific["additionalContext"])
        self.assertIn("not user text", specific["additionalContext"])

    def test_empty_pack_is_not_injected(self) -> None:
        turn = agent_hooks.HookTurn(client="codex", event=agent_hooks.EVENT_PROMPT, raw_event="UserPromptSubmit")
        self.assertEqual(agent_hooks.render_stdout(turn, {"context": "header only", "hits": []}), "")

    def test_cursor_stays_silent(self) -> None:
        turn = agent_hooks.HookTurn(client="cursor", event=agent_hooks.EVENT_PROMPT)
        self.assertEqual(
            agent_hooks.render_stdout(turn, {"context": "Constraint: no secrets.", "hits": [{"node": {}}]}),
            "",
        )

    def test_codex_stop_returns_valid_json_even_with_no_context(self) -> None:
        turn = agent_hooks.HookTurn(client="codex", event=agent_hooks.EVENT_RESPONSE, raw_event="Stop")
        self.assertEqual(json.loads(agent_hooks.render_stdout(turn, None)), {})

    def test_codex_stop_returns_valid_json_when_payload_is_unusable(self) -> None:
        for raw in ("", "not json", '{"hook_event_name":"Stop"}'):
            with self.subTest(raw=raw):
                out = agent_hooks.handle_capture(raw, client="codex", event=agent_hooks.EVENT_RESPONSE)
                self.assertEqual(json.loads(out), {})

    def test_long_context_is_truncated(self) -> None:
        turn = agent_hooks.HookTurn(client="claude", event=agent_hooks.EVENT_PROMPT, raw_event="UserPromptSubmit")
        rendered = json.loads(agent_hooks.render_stdout(
            turn,
            {"context": "x" * 9000, "hits": [{"node": {"id": "n1"}}]},
        ))
        body = rendered["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(body), agent_hooks._INJECT_MAX_CHARS + len("\n[truncated]"))
        self.assertTrue(body.endswith("[truncated]"))


class HookCaptureTests(unittest.TestCase):
    def test_capture_writes_turn_into_memory_then_injects_it_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            first = agent_hooks.handle_capture(
                json.dumps({
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Remember: UniqueBirchFormatter is the required Python formatter in this repo.",
                    "cwd": str(root),
                }),
                client="codex",
                event="prompt",
                root=root,
            )
            self.assertEqual(first, "", "an empty project must not spend tokens on an empty pack")

            # The app was closed: the turn waits in the spool until the app replays it.
            self.assertEqual(service.drain_hook_spool()["captured"], 1)
            found = service.search_memory("UniqueBirchFormatter", project_id="architectos", limit=5)
            self.assertIn("UniqueBirchFormatter", json.dumps(found.get("hits") or []))

            with _RunningApp(root, service):
                second = agent_hooks.handle_capture(
                    json.dumps({
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Which formatter should I use, UniqueBirchFormatter or something else?",
                        "cwd": str(root),
                    }),
                    client="codex",
                    event="prompt",
                    root=root,
                )
            injected = json.loads(second)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("UniqueBirchFormatter", injected)

    def test_blank_text_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = agent_hooks.handle_capture(
                json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "   "}),
                client="claude",
                event="prompt",
                root=root,
            )
            self.assertEqual(out, "")

    def test_broken_stdin_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                agent_hooks.handle_capture("not json at all", client="cursor", event="prompt", root=Path(tmp)),
                "",
            )

    def test_disable_switch_short_circuits(self) -> None:
        previous = os.environ.get("ARCHITECTOS_HOOK_DISABLED")
        os.environ["ARCHITECTOS_HOOK_DISABLED"] = "1"
        try:
            out = agent_hooks.handle_capture(
                json.dumps({"prompt": "Remember: this must not be stored."}),
                client="claude",
                event="prompt",
            )
        finally:
            if previous is None:
                os.environ.pop("ARCHITECTOS_HOOK_DISABLED", None)
            else:
                os.environ["ARCHITECTOS_HOOK_DISABLED"] = previous
        self.assertEqual(out, "")


class HookProjectResolutionTests(unittest.TestCase):
    def test_cwd_inside_project_root_selects_that_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            workspace = root / "workspaces" / "shop"
            (workspace / "src").mkdir(parents=True)
            service.repository.upsert_project(Project(id="shop", name="Shop", root_path=str(workspace)))

            self.assertEqual(service.project_id_for_path(str(workspace / "src")), "shop")
            self.assertEqual(service.project_id_for_path(str(workspace)), "shop")

    def test_nested_project_root_wins_over_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            outer = root / "workspaces" / "mono"
            inner = outer / "packages" / "api"
            inner.mkdir(parents=True)
            service.repository.upsert_project(Project(id="mono", name="Mono", root_path=str(outer)))
            service.repository.upsert_project(Project(id="api", name="Api", root_path=str(inner)))

            self.assertEqual(service.project_id_for_path(str(inner / "src")), "api")
            self.assertEqual(service.project_id_for_path(str(outer / "docs")), "mono")

    def test_bind_file_wins_over_project_root_for_hook_and_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            workspace = root / "workspaces" / "shop"
            (workspace / "src").mkdir(parents=True)
            service.repository.upsert_project(Project(id="shop", name="Shop", root_path=str(workspace)))
            service.repository.upsert_project(Project(id="bound", name="Bound"))
            write_mcp_bind_file(workspace, "bound", "Bound")
            cwd = str(workspace / "src")

            prompt = agent_hooks.run_turn(
                agent_hooks.HookTurn(client="claude", event=agent_hooks.EVENT_PROMPT, user_text="How do we deploy?", cwd=cwd),
                root=root,
            )
            session = agent_hooks.run_turn(
                agent_hooks.HookTurn(client="claude", event=agent_hooks.EVENT_SESSION, cwd=cwd),
                root=root,
            )

            self.assertEqual(MemoryMCPServer(root, cwd=cwd).project_id, "bound")
            self.assertEqual((prompt or {}).get("project_id"), "bound")
            self.assertEqual((session or {}).get("project_id"), "bound")

    def test_unknown_path_resolves_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self.assertIsNone(service.project_id_for_path("/definitely/not/a/project/root"))
            self.assertIsNone(service.project_id_for_path(""))


class HookInstallTests(unittest.TestCase):
    def _script(self, root: Path) -> Path:
        script = root / "architectos_hook.py"
        script.write_text("# test entry\n", encoding="utf-8")
        return script

    def test_install_is_idempotent_per_client(self) -> None:
        for client in agent_hooks.CLIENTS:
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = self._script(root)
                path = agent_hooks.config_path(client, project_root=root)
                for _ in range(3):
                    agent_hooks.install(client, project_root=root, script=script)
                config = json.loads(path.read_text(encoding="utf-8"))
                serialized = json.dumps(config)
                self.assertEqual(serialized.count(agent_hooks.MARKER), len(agent_hooks._REGISTRATIONS[client]))

    def test_install_preserves_foreign_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self._script(root)
            path = agent_hooks.config_path("claude", project_root=root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "permissions": {"allow": ["Bash"]},
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool.sh"}]}]},
            }), encoding="utf-8")

            agent_hooks.install("claude", project_root=root, script=script)
            config = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(config["permissions"], {"allow": ["Bash"]})
            commands = [
                item.get("command")
                for entry in config["hooks"]["UserPromptSubmit"]
                for item in entry.get("hooks", [])
            ]
            self.assertIn("other-tool.sh", commands)
            self.assertTrue(any(agent_hooks.MARKER in str(command) for command in commands))

    def test_uninstall_removes_only_our_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self._script(root)
            path = agent_hooks.config_path("cursor", project_root=root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "version": 1,
                "hooks": {"beforeSubmitPrompt": [{"command": "keep-me.sh"}]},
            }), encoding="utf-8")

            agent_hooks.install("cursor", project_root=root, script=script)
            agent_hooks.install("cursor", project_root=root, script=script, remove=True)
            config = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(config["hooks"]["beforeSubmitPrompt"], [{"command": "keep-me.sh"}])
            self.assertNotIn(agent_hooks.MARKER, json.dumps(config))

    def test_cursor_registers_prompt_and_response_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self._script(root)
            agent_hooks.install("cursor", project_root=root, script=script)
            config = json.loads(agent_hooks.config_path("cursor", project_root=root).read_text(encoding="utf-8"))
            self.assertEqual(config["version"], 1)
            self.assertIn("beforeSubmitPrompt", config["hooks"])
            self.assertIn("afterAgentResponse", config["hooks"])
            command = config["hooks"]["beforeSubmitPrompt"][-1]["command"]
            self.assertIn("--client cursor", command)
            self.assertIn("--event prompt", command)

    def test_missing_entry_script_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                agent_hooks.install("codex", project_root=root, script=root / "nope.py")

    def test_codex_install_mentions_trusting_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = agent_hooks.install("codex", project_root=root, script=self._script(root))
            self.assertIn("/hooks", str(result.get("note")))


class HookStatusTests(unittest.TestCase):
    def test_status_reports_clients_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = agent_hooks.status(project_root=Path(tmp))
            self.assertIn("clients", report)
            self.assertIn("server", report)
            self.assertEqual({item["client"] for item in report["clients"]}, set(agent_hooks.CLIENTS))

    def test_status_marks_injecting_clients_and_installed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "architectos_hook.py").write_text("# test entry\n", encoding="utf-8")
            agent_hooks.install("codex", project_root=root, script=root / "architectos_hook.py")
            rows = {
                item["client"]: item
                for item in agent_hooks.status(project_root=root)["clients"]
                if item["scope"] == "project"
            }
            self.assertTrue(rows["codex"]["installed"])
            self.assertTrue(rows["codex"]["injects"])
            self.assertFalse(rows["cursor"]["installed"])
            self.assertFalse(rows["cursor"]["injects"], "Cursor hooks cannot inject context back")

    def test_expand_clients_accepts_lists_strings_and_all(self) -> None:
        self.assertEqual(agent_hooks.expand_clients("all"), list(agent_hooks.CLIENTS))
        self.assertEqual(agent_hooks.expand_clients(None), list(agent_hooks.CLIENTS))
        self.assertEqual(agent_hooks.expand_clients("codex,claude"), ["codex", "claude"])
        self.assertEqual(agent_hooks.expand_clients(["codex", "codex"]), ["codex"])
        with self.assertRaises(ValueError):
            agent_hooks.expand_clients(["windsurf"])


class HookServiceTests(unittest.TestCase):
    """The desktop app installs hooks through the service, never the CLI."""

    def test_install_and_uninstall_through_service_for_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            config = agent_hooks.config_path("codex", project_root=Path(tmp))

            installed = service.configure_agent_hooks({"clients": ["codex"], "scope": "project"})
            self.assertEqual(installed["scope"], "project")
            self.assertTrue(config.exists())
            row = next(item for item in installed["status"]["clients"] if item["client"] == "codex" and item["scope"] == "project")
            self.assertTrue(row["installed"])

            removed = service.configure_agent_hooks({"clients": ["codex"], "scope": "project"}, remove=True)
            row = next(item for item in removed["status"]["clients"] if item["client"] == "codex" and item["scope"] == "project")
            self.assertFalse(row["installed"])
            self.assertNotIn(agent_hooks.MARKER, config.read_text(encoding="utf-8"))

    def test_scope_defaults_to_user_without_touching_the_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch.object(agent_hooks, "install", return_value={"client": "codex", "removed": False, "events": []}) as installer:
                service.configure_agent_hooks({"clients": ["codex"]})
            self.assertIsNone(installer.call_args.kwargs["project_root"], "user scope must not pin a project root")

    def test_bad_scope_and_unknown_client_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with self.assertRaises(ValueError):
                service.configure_agent_hooks({"clients": ["codex"], "scope": "global"})
            with self.assertRaises(ValueError):
                service.configure_agent_hooks({"clients": ["windsurf"], "scope": "project"})

    def test_missing_entry_script_is_reported_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch.object(agent_hooks, "hook_entry_available", return_value=False):
                self.assertFalse(service.agent_hook_status()["entry_script_exists"])
                with self.assertRaises(ValueError) as caught:
                    service.configure_agent_hooks({"clients": ["codex"], "scope": "project"})
            self.assertIn("cannot install hooks", str(caught.exception))

    def test_frozen_hook_command_uses_the_binary_not_the_python_script(self) -> None:
        fake_bin = Path("/Applications/ArchitectOS.app/Contents/MacOS/architectos-server")
        with mock.patch.object(agent_hooks, "is_frozen", return_value=True), mock.patch.object(sys, "executable", str(fake_bin)):
            command = agent_hooks.hook_command("codex", "prompt")
        self.assertIn(str(fake_bin), command)
        self.assertIn("hook capture --client codex --event prompt", command)
        self.assertNotIn("architectos_hook.py", command)
        self.assertTrue(agent_hooks._is_ours_command(command))

    def test_module_invocation_is_used_when_the_repo_script_is_absent(self) -> None:
        with mock.patch.object(agent_hooks, "is_frozen", return_value=False), mock.patch.object(agent_hooks, "entry_script", return_value=Path("/nope/architectos_hook.py")):
            command = agent_hooks.hook_command("cursor", "response")
        self.assertIn("-m architectos hook capture --client cursor --event response", command)
        self.assertTrue(agent_hooks._is_ours_command(command))


class HookEndpointTests(unittest.TestCase):
    def test_hook_endpoints_report_and_change_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))

            class TestHandler(ArchitectOSHandler):
                pass

            TestHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            headers = {"X-ArchitectOS-Token": service.auth_token, "Content-Type": "application/json"}

            def call(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
                request = urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(body).encode("utf-8") if body is not None else None,
                    method="POST" if body is not None else "GET",
                    headers=headers,
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                def project_row(status: dict[str, object]) -> dict[str, object]:
                    rows = status["clients"]  # type: ignore[index]
                    return next(item for item in rows if item["client"] == "claude" and item["scope"] == "project")

                self.assertFalse(project_row(call("/api/hooks"))["installed"])
                installed = call("/api/hooks/install", {"clients": ["claude"], "scope": "project"})
                self.assertTrue(project_row(installed["status"])["installed"])  # type: ignore[index]
                self.assertTrue(project_row(call("/api/hooks"))["installed"])
                removed = call("/api/hooks/uninstall", {"clients": ["claude"], "scope": "project"})
                self.assertFalse(project_row(removed["status"])["installed"])  # type: ignore[index]
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()


class HookTimingLogTests(unittest.TestCase):
    LINE = re.compile(
        r"^\S+ event=prompt client=codex transport=(?P<transport>http|offline) ms=\d+\.\d "
        r"tokens=(?P<tokens>\d+) hits=(?P<hits>\d+) project=architectos queued=\d+ auto=\d+$"
    )
    PROMPT = "Remember: UniqueBirchFormatter is the required Python formatter in this repo."

    def _capture(self, root: Path, prompt: str) -> str:
        return agent_hooks.handle_capture(
            json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(root)}),
            client="codex",
            event="prompt",
            root=root,
        )

    def _log_lines(self, root: Path) -> list[str]:
        return (root / "data" / "hooks.log").read_text(encoding="utf-8").splitlines()

    def test_debug_log_records_one_offline_timing_line_without_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": "1"}):
            root = Path(tmp)
            service = ArchitectOSService(root)
            service.hook_maintenance()  # the app ran once: the cache knows the projects
            self._capture(root, self.PROMPT)
            service.drain_hook_spool()
            question = "Which formatter should I use, UniqueBirchFormatter or something else?"
            service.capture_memory_turn(question, hook=True, capture=False)  # the app caches the pack
            out = self._capture(root, question)
            lines = self._log_lines(root)
        self.assertEqual(len(lines), 2)
        first, second = (self.LINE.match(line) for line in lines)
        assert first is not None and second is not None, lines
        self.assertEqual((first["transport"], first["tokens"]), ("offline", "0"))
        injected = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(int(second["tokens"]), max(1, round(len(injected) / 4)))
        self.assertGreaterEqual(int(second["hits"]), 1)
        self.assertNotIn("UniqueBirchFormatter", "\n".join(lines))

    def test_debug_log_names_the_http_transport_when_the_app_is_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": "1"}):
            root = Path(tmp)
            service = ArchitectOSService(root)

            class TestHandler(ArchitectOSHandler):
                pass

            TestHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                (root / "data" / "architectos.runtime.json").write_text(
                    json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "auth_token": service.auth_token}),
                    encoding="utf-8",
                )
                self._capture(root, self.PROMPT)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
            lines = self._log_lines(root)
        self.assertEqual(len(lines), 1)
        match = self.LINE.match(lines[0])
        assert match is not None, lines
        self.assertEqual(match["transport"], "http")

    # Every agent event starts a fresh interpreter: argparse (with the shutil, bz2
    # and lzma its help formatter pulls in), logging and the idna codec cost each
    # turn ~25 ms, and the hook process needs none of them.
    HOOK_PROCESS = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(agent_hooks.__file__).resolve().parents[1])!r})\n"
        "from architectos.agent_hooks import main\n"
        "main(['capture', '--client', 'claude', '--event', 'prompt'])\n"
        "print()\n"
        "print(sorted(n for n in ('argparse', 'logging', 'shutil', 'encodings.idna') if n in sys.modules))\n"
    )

    def _hook_process(self, root: str, *, offline: str) -> tuple[str, str, str]:
        """(stdout, modules the hook loaded, hooks.log) of one prompt event run as the hook command."""
        env = {**os.environ, "ARCHITECTOS_ROOT": root, "ARCHITECTOS_HOOK_DEBUG": "1", "ARCHITECTOS_HOOK_OFFLINE": offline}
        proc = subprocess.run(
            [sys.executable, "-c", self.HOOK_PROCESS],
            input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": self.PROMPT, "cwd": root}),
            capture_output=True, text=True, env=env, check=True, timeout=60,
        )
        out, modules = proc.stdout.rsplit("\n", 2)[:2]
        return out, modules, (Path(root) / "data" / "hooks.log").read_text(encoding="utf-8")

    def test_injecting_prompt_turn_loads_no_cli_or_logging_modules(self) -> None:
        pack = {"context": "- id=node_a [Decision] Hooks talk to the app over a socket.", "hits": [{"id": "node_a"}]}
        listener = socket.create_server(("127.0.0.1", 0))

        def reply() -> None:
            conn, _ = listener.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(re.search(rb"Content-Length: (\d+)", head)[1])
                while len(body) < length:
                    body += conn.recv(65536)
                conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" + json.dumps(pack).encode("utf-8"))

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp, listener:
            (Path(tmp) / "data").mkdir()
            (Path(tmp) / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "auth_token": "token"}), encoding="utf-8"
            )
            out, modules, log = self._hook_process(tmp, offline="0")
            thread.join(timeout=5)
        self.assertIn("Hooks talk to the app", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertIn("transport=http", log)
        self.assertEqual(modules, "[]")

    def test_offline_turn_logs_to_the_debug_file_without_logging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, modules, log = self._hook_process(tmp, offline="1")
        self.assertEqual(out, "")
        self.assertIn(" INFO offline hook: kind=prompt project=- cached=False", log)
        self.assertIn(" INFO injection skipped: kind=prompt transport=offline", log)
        self.assertIn("transport=offline", log)
        self.assertNotIn(self.PROMPT, log)
        self.assertEqual(modules, "[]")

    def test_debug_log_stays_off_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": ""}):
            root = Path(tmp)
            ArchitectOSService(root)
            self._capture(root, self.PROMPT)
            self.assertFalse((root / "data" / "hooks.log").exists())

    def test_silent_events_log_zero_injected_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": "1"}):
            root = Path(tmp)
            ArchitectOSService(root)
            self._capture(root, self.PROMPT)
            stop = agent_hooks.handle_capture(
                json.dumps({"hook_event_name": "Stop", "last_assistant_message": self.PROMPT, "cwd": str(root)}),
                client="codex",
                event="response",
                root=root,
            )
            cursor = agent_hooks.handle_capture(
                json.dumps({"hook_event_name": "beforeSubmitPrompt", "prompt": "Which Python formatter do we use?", "cwd": str(root)}),
                client="cursor",
                event="prompt",
                root=root,
            )
            lines = self._log_lines(root)[1:]
        self.assertEqual((stop, cursor), ("{}", ""))
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r" event=response client=codex transport=offline ms=\d+\.\d tokens=0 hits=\d+ ")
        self.assertRegex(lines[1], r" event=prompt client=cursor transport=offline ms=\d+\.\d tokens=0 hits=\d+ ")

    def test_failure_lines_land_under_the_given_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ARCHITECTOS_HOOK_DEBUG": "1"}):
            root = Path(tmp)
            ArchitectOSService(root)
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:1", "auth_token": "token"}),
                encoding="utf-8",
            )
            agent_hooks.handle_capture('{"hook_event_name": "PreCompact"}', client="codex", root=root)
            self._capture(root, self.PROMPT)
            log = "\n".join(self._log_lines(root))
        self.assertIn("ignored event client=codex", log)
        self.assertIn("http failed:", log)
        self.assertIn("transport=offline", log)


if __name__ == "__main__":
    unittest.main()
