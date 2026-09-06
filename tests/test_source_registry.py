"""Additional registry ingest and slug tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.service import ArchitectOSService
from architectos.source_registry import origin_slug_from_parts, unique_source_name


class SourceRegistryIngestTests(unittest.TestCase):
    def test_ingest_docs_and_adr_without_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "readme.md").write_text("# Hello\n\nDocs body.", encoding="utf-8")
            (root / "adr").mkdir()
            (root / "adr" / "0001-decision.md").write_text("# ADR 1\n\nWe chose SQLite.", encoding="utf-8")
            (root / "src").mkdir()
            for index in range(30):
                (root / "src" / f"module_{index}.py").write_text(f"# code {index}\n" + ("x" * 200), encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.create_project({"id": "demo", "name": "Demo", "root_path": str(root)})
            sources = service.list_project_sources(project["id"])
            docs = next(item for item in sources if item["kind"] == "docs")
            adr = next(item for item in sources if item["kind"] == "adr")
            result = service.ingest_memory({
                "project_id": project["id"],
                "sources": [docs["id"], adr["id"]],
                "limit": 0,
            })
            labels = {item.get("metadata", {}).get("source_name") or item.get("source_type") for item in result["candidates"]}
            kinds = {item.get("source_type") or item.get("metadata", {}).get("source_kind") for item in result["candidates"]}
            self.assertIn("adr", kinds)
            self.assertGreaterEqual(result["count"], 1)

    def test_custom_source_name_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"id": "demo2", "name": "Demo2", "root_path": str(tmp)})
            sources = service.list_project_sources(project["id"])
            docs = next(item for item in sources if item["kind"] == "docs")
            updated = service.update_source(docs["id"], {
                "project_id": project["id"],
                "kind": "docs",
                "name": "Platform wiki",
                "config": {**(docs.get("config") or {}), "name_customized": True},
            })
            self.assertEqual(updated["name"], "Platform wiki")

    def test_protocol_names_are_not_slugs(self) -> None:
        self.assertEqual(origin_slug_from_parts(driver="http", http_url="https://wiki.example.com"), "wiki")
        self.assertNotIn("http", origin_slug_from_parts(driver="http", http_url="https://wiki.example.com"))


    def test_unique_name_skips_redundant_kind_label(self) -> None:
        self.assertEqual(unique_source_name("ask", {"ask"}, kind="chat"), "ask (2)")
        self.assertEqual(unique_source_name("git", {"git"}, kind="git_history"), "git (2)")

    def test_default_source_titles(self) -> None:
        from architectos.source_registry import default_source_title
        self.assertEqual(default_source_title(driver="chat", kind="chat"), "Ask")
        self.assertEqual(default_source_title(driver="local_git", kind="git_history"), "Local Git")
        self.assertEqual(default_source_title(adapter="azure-boards", kind="issues"), "Azure Boards")
        self.assertEqual(default_source_title(adapter="github", kind="issues"), "GitHub Issues")
        self.assertEqual(default_source_title(adapter="github", kind="pull_requests"), "GitHub PRs")
        self.assertEqual(default_source_title(adapter="granola", kind="meetings"), "Granola")

    def test_default_source_descriptions(self) -> None:
        from architectos.source_registry import default_source_description
        ask = default_source_description(driver="chat", kind="chat")
        self.assertIn("Ask", ask)
        boards = default_source_description(adapter="azure-boards", kind="issues")
        self.assertIn("Azure DevOps", boards)
        self.assertTrue(default_source_description(adapter="granola", kind="meetings"))
        self.assertEqual(default_source_description(driver="http", kind="docs"), "")

    def test_seeded_and_preset_use_display_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            project = service.create_project({"name": "Titles", "root_path": str(root)})
            by_kind = {item["kind"]: item for item in service.list_project_sources(project["id"]) if item.get("config", {}).get("driver") in {"chat", "local_git"}}
            self.assertEqual(by_kind["chat"]["name"], "Ask")
            self.assertEqual(by_kind["git_history"]["name"], "Local Git")
            # Force-create github preset binding
            service.upsert_project_source({
                "project_id": project["id"],
                "kind": "issues",
                "reset_name": True,
                "config": {
                    "driver": "mcp",
                    "adapter": "github",
                    "mcp_server_id": "github",
                    "binding_key": "mcp:github:issues",
                    "name_customized": False,
                },
            })
            github = next(item for item in service.list_project_sources(project["id"]) if item.get("config", {}).get("binding_key") == "mcp:github:issues")
            self.assertEqual(github["name"], "GitHub Issues")

    def test_patch_cannot_move_source_across_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            path_a = root / "proj-a"
            path_b = root / "proj-b"
            path_a.mkdir()
            path_b.mkdir()
            a = service.create_project({"name": "A", "root_path": str(path_a)})
            b = service.create_project({"name": "B", "root_path": str(path_b)})
            self.assertNotEqual(a["id"], b["id"])
            sources_a = service.list_project_sources(a["id"])
            chat = next(item for item in sources_a if item["kind"] == "chat")
            service.update_source(chat["id"], {
                "project_id": b["id"],
                "kind": "chat",
                "config": {**(chat.get("config") or {}), "enabled": True},
            })
            moved = service.get_project_source(chat["id"])
            self.assertEqual(moved["project_id"], a["id"])
            chats_b = [item for item in service.list_project_sources(b["id"]) if item["kind"] == "chat"]
            self.assertEqual(len(chats_b), 1)
            self.assertNotEqual(chats_b[0]["id"], chat["id"])

    def test_dedupe_duplicate_binding_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"id": "proj-d", "name": "D", "root_path": str(tmp)})
            sources = service.list_project_sources(project["id"])
            chat = next(item for item in sources if item["kind"] == "chat")
            from architectos.models import Source, utc_now
            clone = Source.from_dict({
                **chat,
                "id": "source_orphan_chat_clone",
                "name": "ask Ask",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            })
            service.repository.upsert_source(clone)
            before = [item for item in service.repository.list_sources(project["id"]) if item.kind == "chat"]
            self.assertEqual(len(before), 2)
            after = service.list_project_sources(project["id"])
            chats = [item for item in after if item["kind"] == "chat"]
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0]["id"], chat["id"])

    def test_update_http_source_keeps_password_when_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"id": "http-edit", "name": "HTTP", "root_path": str(tmp)})
            created = service.create_source({
                "project_id": project["id"],
                "kind": "docs",
                "name": "Wiki",
                "config": {
                    "driver": "http",
                    "enabled": False,
                    "http": {
                        "url": "https://wiki.example.com/docs",
                        "method": "GET",
                        "auth": "bearer",
                        "bearer_token": "secret-token",
                    },
                },
            })
            updated = service.update_source(created["id"], {
                "project_id": project["id"],
                "kind": "docs",
                "name": "Wiki v2",
                "config": {
                    **(created.get("config") or {}),
                    "name_customized": True,
                    "http": {
                        "url": "https://wiki.example.com/v2",
                        "method": "GET",
                        "auth": "bearer",
                        "bearer_token": "secret-token",
                    },
                },
            })
            self.assertEqual(updated["name"], "Wiki v2")
            self.assertEqual((updated.get("config") or {}).get("http", {}).get("url"), "https://wiki.example.com/v2")
            self.assertEqual((updated.get("config") or {}).get("http", {}).get("bearer_token"), "secret-token")


if __name__ == "__main__":
    unittest.main()
