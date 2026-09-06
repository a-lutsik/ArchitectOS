from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.architectos import agent_hooks
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
            ArchitectOSService(root)  # initialise the database
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

            found = ArchitectOSService(root).search_memory("UniqueBirchFormatter", project_id="architectos", limit=5)
            self.assertIn("UniqueBirchFormatter", json.dumps(found.get("hits") or []))

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


if __name__ == "__main__":
    unittest.main()
