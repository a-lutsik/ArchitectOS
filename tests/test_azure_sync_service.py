from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.mcp import MCPError, MCPServerConfig
from backend.architectos.service import ArchitectOSService


def _enabled_azure_server(server_id: str) -> MCPServerConfig:
    return MCPServerConfig.from_dict({
        "id": server_id,
        "label": server_id,
        "command": ["npx", "-y", "@azure-devops/mcp", "ExampleOrg"],
        "enabled": True,
        "env": {"ado_mcp_project": "E-AI"},
    })


def _work_item(item_id: int, wi_type: str, title: str, relations: list[dict[str, Any]] | None = None, **fields: Any) -> dict[str, Any]:
    """Build a work item payload shaped like the Azure DevOps MCP get_item result."""
    merged = {
        "System.Id": item_id,
        "System.WorkItemType": wi_type,
        "System.Title": title,
        "System.State": "Active",
        "System.IterationPath": "E-AI\\Dev v7.7",
        "System.AreaPath": "E-AI",
        **fields,
    }
    item: dict[str, Any] = {
        "id": item_id,
        "fields": merged,
        "url": f"https://dev.azure.com/ExampleOrg/E-AI/_workitems/edit/{item_id}",
    }
    if relations is not None:
        item["relations"] = relations
    return item


class _FakeAzureBoardsMCP:
    """Speaks the @azure-devops/mcp tool surface that call_ado_tool drives.

    The production code never talks HTTP directly from the mixin — every call
    goes through ``mcp_manager.call_tool``, so faking that boundary is enough.
    """

    def __init__(
        self,
        work_items: list[dict[str, Any]],
        comments: dict[int, list[dict[str, Any]]] | None = None,
        *,
        fail_get_item: bool = False,
        envelope_comments: bool = False,
    ) -> None:
        self.work_items = work_items
        self.comments = comments or {}
        self.fail_get_item = fail_get_item
        self.envelope_comments = envelope_comments
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get_server(self, server_id: str) -> MCPServerConfig | None:
        if server_id != "azure-devops":
            return None
        return _enabled_azure_server(server_id)

    def call_tool(self, server_id: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((server_id, tool, args))
        if server_id != "azure-devops":
            raise MCPError(f"unexpected server {server_id}")
        action = str(args.get("action") or "")

        def _text(payload: Any) -> dict[str, Any]:
            return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}

        if tool == "wit_query" and action == "wiql":
            return _text({"workItems": [{"id": item["id"]} for item in self.work_items]})
        if tool == "search_workitem":
            return _text({"results": []})
        if tool == "wit_work_item" and action == "get":
            if self.fail_get_item:
                return {"result": {"content": [{"type": "text", "text": "TF401232: work item does not exist"}], "isError": True}}
            item_id = int(args.get("id") or 0)
            item = next((entry for entry in self.work_items if int(entry["id"]) == item_id), None)
            if item is None:
                return {"result": {"content": [{"type": "text", "text": f"TF401232: work item {item_id} does not exist"}], "isError": True}}
            return _text(item)
        if tool == "wit_work_item" and action == "list_comments":
            item_id = int(args.get("workItemId") or 0)
            comments_payload: dict[str, Any] = {"comments": self.comments.get(item_id, [])}
            if self.envelope_comments:
                # Standard MCP tool-result envelope: the JSON rides inside a text content part.
                return _text(comments_payload)
            return {"result": comments_payload}
        raise MCPError(f"unexpected tool {tool} ({action or 'no action'})")

    def close_session(self, server_id: str) -> None:
        return None


class _FailingAzureGitMCP:
    """Every tool call reports an unreachable Azure DevOps host via isError."""

    def get_server(self, server_id: str) -> MCPServerConfig | None:
        if server_id not in {"azure-devops", "azure-devops-git"}:
            return None
        return _enabled_azure_server(server_id)

    def call_tool(self, server_id: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        return {
            "result": {
                "content": [{"type": "text", "text": "Error listing repositories: tunneling socket could not be established, statusCode=403"}],
                "isError": True,
            }
        }

    def close_session(self, server_id: str) -> None:
        return None


class _FakeTeamsGraphClient:
    def __init__(self, meetings: list[dict[str, Any]]) -> None:
        self.meetings = meetings

    def collect_meetings(self, *, limit: int = 12, lookback_days: Any = None, include_transcripts: Any = None, include_ai_insights: Any = None) -> list[dict[str, Any]]:
        return self.meetings[:limit]


def _make_service(tmp: str) -> ArchitectOSService:
    return ArchitectOSService(Path(tmp))


class AzureBoardsSyncTests(unittest.TestCase):
    def test_boards_import_writes_nodes_with_mapped_types(self) -> None:
        work_items = [
            _work_item(
                101,
                "Bug",
                "Crash on empty chart data",
                **{
                    "System.AssignedTo": {"displayName": "Ada", "uniqueName": "ada@example.com"},
                    "System.Tags": "sprint-7.7; crash",
                    "System.Description": "<p>Reproduce with an empty series.</p>",
                    "System.CommentCount": 1,
                },
                relations=[{
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": "https://dev.azure.com/ExampleOrg/E-AI/_apis/wit/workItems/100",
                    "attributes": {"name": "Parent feature"},
                }],
            ),
            _work_item(102, "User Story", "Import board memories", **{"System.CommentCount": 0}),
        ]
        comments = {
            101: [{"id": 7, "text": "Confirmed on the staging slot.", "createdBy": {"displayName": "Grace"}, "createdDate": "2026-07-01T10:00:00Z"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            service.mcp_manager = _FakeAzureBoardsMCP(work_items, comments)  # type: ignore[assignment]
            result = service._import_azure_boards_to_memory("architectos", 5, {"ado_project": "E-AI"})

            self.assertEqual(result["count"], 2)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["chunk_nodes"], 1)
            stored = {
                str(dict(node.metadata or {}).get("work_item_id") or ""): node
                for node in service.repository.list_nodes()
                if dict(node.metadata or {}).get("source") == "azure_boards"
                and str(dict(node.metadata or {}).get("chunk_role") or "") in {"", "main"}
            }
            self.assertEqual(set(stored), {"101", "102"})
            # ADO_BOARD_MEMORY_TYPES: bug -> Constraint, user story -> Requirement.
            bug = stored["101"]
            self.assertEqual(bug.type, "Constraint")
            self.assertEqual(bug.label, "ADO Bug #101: Crash on empty chart data")
            self.assertEqual(bug.project_id, "architectos")
            self.assertEqual(bug.metadata["source_type"], "azure-boards")
            self.assertEqual(bug.metadata["work_item_type"], "Bug")
            self.assertEqual(bug.metadata["work_item_state"], "Active")
            self.assertEqual(bug.metadata["assigned_to"], "Ada <ada@example.com>")
            self.assertEqual(bug.metadata["iteration_path"], "E-AI\\Dev v7.7")
            self.assertEqual(bug.metadata["tags"], "sprint-7.7; crash")
            self.assertEqual(bug.metadata["ado_project"], "E-AI")
            self.assertEqual(bug.metadata["relations"][0]["work_item_id"], "100")
            self.assertEqual(bug.metadata["relations"][0]["link_type"], "parent")
            self.assertIn("Crash on empty chart data", bug.text)
            self.assertIn("Reproduce with an empty series.", bug.text)
            story = stored["102"]
            self.assertEqual(story.type, "Requirement")
            self.assertEqual(story.label, "ADO User Story #102: Import board memories")

            # The single comment becomes a linked chunk node behind HAS_COMMENT.
            comment_nodes = [
                node for node in service.repository.list_nodes()
                if dict(node.metadata or {}).get("chunk_role") == "comment"
                and str(dict(node.metadata or {}).get("parent_work_item_id") or "") == "101"
            ]
            self.assertEqual(len(comment_nodes), 1)
            self.assertIn("Confirmed on the staging slot.", comment_nodes[0].text)
            edges = service.repository.list_edges()
            self.assertTrue(any(edge.type == "HAS_COMMENT" and edge.source == bug.id and edge.target == comment_nodes[0].id for edge in edges))

    def test_boards_comments_in_standard_envelope_yield_single_chunk(self) -> None:
        # Regression: list_comments in the standard MCP envelope (JSON inside a
        # text content part) must not double-count — the raw envelope dict is not
        # itself a comment.
        work_items = [
            _work_item(
                101,
                "Bug",
                "Crash on empty chart data",
                **{
                    "System.Description": "<p>Reproduce with an empty series.</p>",
                    "System.CommentCount": 1,
                },
            ),
        ]
        comments = {
            101: [{"id": 7, "text": "Confirmed on the staging slot.", "createdBy": {"displayName": "Grace"}, "createdDate": "2026-07-01T10:00:00Z"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            service.mcp_manager = _FakeAzureBoardsMCP(work_items, comments, envelope_comments=True)  # type: ignore[assignment]
            result = service._import_azure_boards_to_memory("architectos", 5, {"ado_project": "E-AI"})

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["chunk_nodes"], 1)
            main = next(
                node for node in service.repository.list_nodes()
                if str(dict(node.metadata or {}).get("work_item_id") or "") == "101"
                and dict(node.metadata or {}).get("chunk_role") == "main"
            )
            stored_comments = list(main.metadata.get("comments") or [])
            self.assertEqual(len(stored_comments), 1)
            self.assertEqual(stored_comments[0]["author"], "Grace")
            self.assertEqual(stored_comments[0]["text"], "Confirmed on the staging slot.")

            comment_nodes = [
                node for node in service.repository.list_nodes()
                if dict(node.metadata or {}).get("chunk_role") == "comment"
            ]
            self.assertEqual(len(comment_nodes), 1)
            chunk = comment_nodes[0]
            self.assertEqual(chunk.metadata["comment_author"], "Grace")
            self.assertIn("Confirmed on the staging slot.", chunk.text)
            self.assertNotIn("unknown", chunk.text)

    def test_boards_import_twice_updates_in_place(self) -> None:
        work_items = [_work_item(101, "Task", "Publish docker image", **{"System.CommentCount": 0})]
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            service.mcp_manager = _FakeAzureBoardsMCP(work_items)  # type: ignore[assignment]
            first = service._import_azure_boards_to_memory("architectos", 5, {"ado_project": "E-AI"})
            self.assertEqual(first["updated"], 0)
            node_id = first["imported"][0]["id"]

            again = service._import_azure_boards_to_memory("architectos", 5, {"ado_project": "E-AI"})
            self.assertEqual(again["count"], 1)
            self.assertEqual(again["updated"], 1)
            self.assertEqual(again["imported"][0]["id"], node_id)
            main_nodes = [
                node for node in service.repository.list_nodes()
                if node.status == "active"
                and str(dict(node.metadata or {}).get("work_item_id") or "") == "101"
            ]
            self.assertEqual(len(main_nodes), 1)

    def test_boards_item_fetch_failure_skips_item_without_failing_run(self) -> None:
        work_items = [_work_item(101, "Bug", "Will not load", **{"System.CommentCount": 0})]
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            service.mcp_manager = _FakeAzureBoardsMCP(work_items, fail_get_item=True)  # type: ignore[assignment]
            result = service._import_azure_boards_to_memory("architectos", 5, {"ado_project": "E-AI"})
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["imported"], [])
            self.assertEqual(service.repository.list_nodes(), [])

    def test_azure_git_host_failure_surfaces_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            service.mcp_manager = _FailingAzureGitMCP()  # type: ignore[assignment]
            # Direct import raises a plain MCPError carrying the host message.
            with self.assertRaises(MCPError) as raised:
                service._import_azure_git_to_memory("architectos", 5, {"ado_project": "E-AI"})
            self.assertIn("tunneling socket", str(raised.exception))

            # Through the ingest facade the same failure becomes a warning, not a traceback.
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-git"],
                "limit": 5,
                "ado_project": "E-AI",
                "ingest_mode": "memory",
            })
            self.assertEqual(result["azure_git_count"], 0)
            self.assertEqual(service.repository.list_nodes(), [])
            self.assertTrue(any("Azure Git MCP skipped" in warning and "tunneling socket" in warning for warning in result["warnings"]))

    def test_missing_azure_configuration_skips_with_clear_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            servers = service._load_mcp_servers()
            for server in servers:
                server["enabled"] = False
            service._save_mcp_servers(servers)

            with self.assertRaises(ValueError) as boards_error:
                service._ingest_azure_boards_candidates("architectos", 5, {})
            self.assertIn("Enable Azure DevOps MCP server", str(boards_error.exception))
            with self.assertRaises(ValueError) as wiki_error:
                service._import_azure_wiki_to_memory("architectos", 5, {})
            self.assertIn("Enable Azure DevOps MCP server", str(wiki_error.exception))
            with self.assertRaises(ValueError) as git_error:
                service._azure_git_mcp_server_id()
            self.assertIn("Enable Azure DevOps", str(git_error.exception))

            # The facade converts the misconfiguration into per-source warnings.
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards", "azure-git", "azure-wiki"],
                "limit": 5,
                "ingest_mode": "memory",
            })
            self.assertEqual(result["boards_count"], 0)
            self.assertEqual(result["azure_git_count"], 0)
            self.assertEqual(result["wiki_count"], 0)
            self.assertEqual(result["memory_written"], 0)
            self.assertTrue(any("Azure Boards skipped" in warning for warning in result["warnings"]))
            self.assertTrue(any("Azure Git skipped" in warning for warning in result["warnings"]))
            self.assertTrue(any("Azure Wiki skipped" in warning for warning in result["warnings"]))

    def test_teams_meetings_import_via_injected_client(self) -> None:
        meetings = [
            {
                "meeting_id": "mtg-1",
                "subject": "Sprint 7.7 planning",
                "start": "2026-07-10T10:00:00.0000000",
                "end": "2026-07-10T11:00:00.0000000",
                "organizer": "Ada",
                "attendees": ["Ada", "Grace"],
                "insights_text": "Notes:\n- Lock the release window",
                "transcript_text": "Ada: ship it Friday.\nGrace: agreed.",
                "action_items": ["Grace: update the runbook"],
                "has_insights": True,
                "has_transcript": True,
            },
            # No insights and no transcript: nothing worth remembering, must be skipped.
            {"meeting_id": "mtg-2", "subject": "Empty standup"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            payload = {"_teams_graph_client": _FakeTeamsGraphClient(meetings)}
            result = service._import_teams_meetings_to_memory("architectos", 5, payload)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["updated"], 0)
            node = service.repository.get_node(result["imported"][0]["id"])
            self.assertIsNotNone(node)
            assert node is not None
            self.assertEqual(node.type, "Meeting")
            self.assertEqual(node.label, "Teams: Sprint 7.7 planning")
            self.assertEqual(node.metadata["source"], "teams_graph")
            self.assertEqual(node.metadata["source_type"], "teams-meetings")
            self.assertEqual(node.metadata["teams_meeting_id"], "mtg-1")
            self.assertEqual(node.metadata["attendees"], ["Ada", "Grace"])
            self.assertIn("AI Insights", node.text)
            self.assertIn("Transcript:", node.text)
            self.assertIn("ship it Friday", node.text)

            again = service._import_teams_meetings_to_memory("architectos", 5, {"_teams_graph_client": _FakeTeamsGraphClient(meetings)})
            self.assertEqual(again["count"], 1)
            self.assertEqual(again["updated"], 1)
            self.assertEqual(again["imported"][0]["id"], node.id)
            meeting_nodes = [item for item in service.repository.list_nodes() if item.type == "Meeting"]
            self.assertEqual(len(meeting_nodes), 1)


if __name__ == "__main__":
    unittest.main()
