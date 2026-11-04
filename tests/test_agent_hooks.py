from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.architectos import agent_hooks
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

        def fake_request(url: str, token: str, *, payload: object, timeout: float, root: object = None) -> tuple[dict[str, object], str]:
            seen["session" if "briefing" in url else ("response" if payload and payload.get("assistant_text") else "prompt")] = timeout  # type: ignore[union-attr]
            return {"context": "", "hits": []}, ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env), \
                mock.patch.object(agent_hooks, "_request_json", side_effect=fake_request):
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
        self.assertEqual(settings["hook_char_budget"], 1200)

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
        sent: list[dict[str, object]] = []

        def fake_request(url: str, token: str, *, payload: dict[str, object], timeout: float, root: object = None) -> tuple[dict[str, object], str]:
            sent.append(payload)
            return {"context": "", "hits": []}, ""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agent_hooks, "_request_json", side_effect=fake_request):
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "architectos.runtime.json").write_text(
                json.dumps({"url": "http://127.0.0.1:9", "auth_token": "token"}), encoding="utf-8"
            )
            agent_hooks.handle_capture(
                json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": self.QUESTION, "cwd": str(root)}),
                client="claude", event="prompt", root=root,
            )
        self.assertIs(sent[0]["hook"], True)


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


class HookSessionTests(unittest.TestCase):
    QUESTIONS = (
        "How do we batch ledger writes?",
        "What does the ledger service replicate?",
        "Where does the ledger archive tier live?",
        "Which ledger rule covers batching?",
    )

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
        self.assertEqual([(item["project_id"], item["user_text"], item["assistant_text"]) for item in spooled],
                         [("architectos", self.QUESTION, "Batch them per tenant.")])

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
