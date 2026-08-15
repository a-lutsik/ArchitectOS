from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.code_graph import CodeGraphIngestor, parse_javascript, parse_python
from backend.architectos.service import ArchitectOSService


class ParseUnitTests(unittest.TestCase):
    def test_python_symbols_qualnames_and_containment(self) -> None:
        source = (
            "import os\n"
            "from .helpers import thing\n"
            "\n"
            "def top_level(a, b):\n"
            "    '''A documented top-level function.'''\n"
            "    return a + b\n"
            "\n"
            "class Widget:\n"
            "    def render(self, ctx):\n"
            "        return ctx\n"
        )
        parsed = parse_python(source)
        by_qual = {sym.qualname: sym for sym in parsed.symbols}
        self.assertIn("top_level", by_qual)
        self.assertIn("Widget", by_qual)
        self.assertIn("Widget.render", by_qual)
        self.assertEqual(by_qual["top_level"].kind, "Function")
        self.assertEqual(by_qual["Widget"].kind, "Class")
        self.assertEqual(by_qual["Widget.render"].kind, "Method")
        self.assertEqual(by_qual["Widget.render"].parent, "Widget")
        self.assertIn("documented", by_qual["top_level"].doc)
        # imports: absolute `os` and relative `.helpers`.
        self.assertIn("os", parsed.imports)
        self.assertIn(".helpers", parsed.imports)

    def test_javascript_symbols_and_imports(self) -> None:
        source = (
            "import { render } from './widget';\n"
            "const helper = () => 1;\n"
            "export function main() { return render(); }\n"
        )
        parsed = parse_javascript(source)
        names = {sym.name for sym in parsed.symbols}
        self.assertIn("helper", names)
        self.assertIn("main", names)
        self.assertIn("./widget", parsed.imports)


class IngestFilesTests(unittest.TestCase):
    def _service(self, tmp: str) -> ArchitectOSService:
        return ArchitectOSService(Path(tmp))

    def test_symbols_and_defines_edges_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            file_node = service.repository.add_node(
                "Artifact", "pkg/mod.py", "project",
                "Code file pkg/mod.py structure:\n\ndef alpha(): pass",
                "architectos", confidence=0.72,
                metadata={"source": "project_scan", "path": "pkg/mod.py"},
            )
            source = "def alpha(x):\n    '''Alpha does things.'''\n    return x\n\nclass Beta:\n    def go(self):\n        return 1\n"
            result = service.code_graph.ingest_files(
                [(file_node, "pkg/mod.py", ".py", source)], "architectos"
            )
            self.assertEqual(result["files"], 1)
            self.assertGreaterEqual(result["symbols"], 3)

            symbols = [n for n in service.repository.list_nodes() if n.type == "Symbol"]
            labels = {n.label for n in symbols}
            self.assertIn("pkg/mod.py::alpha", labels)
            self.assertIn("pkg/mod.py::Beta", labels)
            self.assertIn("pkg/mod.py::Beta.go", labels)

            edges = service.repository.list_edges_touching([file_node.id])
            defines = [e for e in edges if e.type == "DEFINES" and e.source == file_node.id]
            self.assertGreaterEqual(len(defines), 3)
            for edge in defines:
                self.assertEqual((edge.metadata or {}).get("origin"), "code_graph")

            # CONTAINS edge Beta -> Beta.go
            beta = next(n for n in symbols if n.label == "pkg/mod.py::Beta")
            beta_go = next(n for n in symbols if n.label == "pkg/mod.py::Beta.go")
            contains = [e for e in service.repository.list_edges_touching([beta.id])
                        if e.type == "CONTAINS" and e.source == beta.id and e.target == beta_go.id]
            self.assertEqual(len(contains), 1)

    def test_python_import_edge_between_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            a = service.repository.add_node(
                "Artifact", "pkg/a.py", "project", "file a", "architectos",
                metadata={"source": "project_scan", "path": "pkg/a.py"},
            )
            b = service.repository.add_node(
                "Artifact", "pkg/b.py", "project", "file b", "architectos",
                metadata={"source": "project_scan", "path": "pkg/b.py"},
            )
            entries = [
                (a, "pkg/a.py", ".py", "from .b import helper\n\ndef use():\n    return helper()\n"),
                (b, "pkg/b.py", ".py", "def helper():\n    return 1\n"),
            ]
            service.code_graph.ingest_files(entries, "architectos")
            imports = [e for e in service.repository.list_edges_touching([a.id])
                       if e.type == "IMPORTS" and e.source == a.id and e.target == b.id]
            self.assertEqual(len(imports), 1)
            self.assertEqual((imports[0].metadata or {}).get("origin"), "code_graph")

    def test_reingest_prunes_removed_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            node = service.repository.add_node(
                "Artifact", "m.py", "project", "file m", "architectos",
                metadata={"source": "project_scan", "path": "m.py"},
            )
            service.code_graph.ingest_files(
                [(node, "m.py", ".py", "def one():\n    return 1\n\ndef two():\n    return 2\n")],
                "architectos",
            )
            active = {n.label for n in service.repository.list_nodes() if n.type == "Symbol" and n.status == "active"}
            self.assertIn("m.py::one", active)
            self.assertIn("m.py::two", active)

            # Re-scan without `two` -> it should be archived.
            service.code_graph.ingest_files(
                [(node, "m.py", ".py", "def one():\n    return 1\n")],
                "architectos",
            )
            active = {n.label for n in service.repository.list_nodes() if n.type == "Symbol" and n.status == "active"}
            self.assertIn("m.py::one", active)
            self.assertNotIn("m.py::two", active)


class CallsAndImpactTests(unittest.TestCase):
    def _seed(self, service: ArchitectOSService) -> None:
        helpers = service.repository.add_node(
            "Artifact", "pkg/helpers.py", "project", "file helpers", "architectos",
            metadata={"source": "project_scan", "path": "pkg/helpers.py"},
        )
        main = service.repository.add_node(
            "Artifact", "pkg/main.py", "project", "file main", "architectos",
            metadata={"source": "project_scan", "path": "pkg/main.py"},
        )
        entries = [
            (helpers, "pkg/helpers.py", ".py", "def helper():\n    '''Reusable.'''\n    return 1\n"),
            (main, "pkg/main.py", ".py",
             "from .helpers import helper\n\nclass App:\n    def run(self):\n        return helper()\n"),
        ]
        self._result = service.code_graph.ingest_files(entries, "architectos")

    def test_calls_edge_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self._seed(service)
            self.assertGreaterEqual(self._result["calls"], 1)
            calls = [e for e in service.repository.list_edges() if e.type == "CALLS"]
            self.assertTrue(calls)
            for edge in calls:
                self.assertEqual((edge.metadata or {}).get("origin"), "code_graph")
                self.assertTrue((edge.metadata or {}).get("inferred"))

    def test_code_neighbors_lists_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self._seed(service)
            neighbors = service.code_neighbors("helper", "architectos")
            self.assertTrue(neighbors["found"])
            caller_labels = {c["label"] for c in neighbors["callers"]}
            self.assertIn("pkg/main.py::App.run", caller_labels)
            self.assertEqual((neighbors["defined_in"] or {}).get("label"), "pkg/helpers.py")

    def test_code_impact_reports_callers_and_importers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self._seed(service)
            impact = service.code_impact("helper", depth=3, project_id="architectos")
            self.assertTrue(impact["found"])
            impacted = {item["label"]: item for item in impact["impacted"]}
            self.assertIn("pkg/main.py::App.run", impacted)
            self.assertEqual(impacted["pkg/main.py::App.run"]["via"], "CALLS")
            self.assertIn("pkg/main.py", impacted)
            self.assertEqual(impacted["pkg/main.py"]["via"], "IMPORTS")

    def test_reingest_prunes_stale_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self._seed(service)
            self.assertTrue([e for e in service.repository.list_edges() if e.type == "CALLS"])
            # App.run no longer calls helper.
            main = next(
                n for n in service.repository.list_nodes()
                if n.type == "Artifact" and (n.metadata or {}).get("path") == "pkg/main.py"
            )
            service.code_graph.ingest_files(
                [(main, "pkg/main.py", ".py", "class App:\n    def run(self):\n        return 0\n")],
                "architectos",
            )
            remaining = [e for e in service.repository.list_edges() if e.type == "CALLS" and e.status == "active"]
            self.assertEqual(remaining, [])


class ScanIntegrationTests(unittest.TestCase):
    def test_scan_project_builds_code_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "proj"
            pkg = project_root / "pkg"
            pkg.mkdir(parents=True)
            app_root.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "helpers.py").write_text("def helper():\n    '''Reusable helper.'''\n    return 1\n", encoding="utf-8")
            (pkg / "main.py").write_text(
                "from .helpers import helper\n\nclass App:\n    def run(self):\n        return helper()\n",
                encoding="utf-8",
            )

            service = ArchitectOSService(app_root)
            created = service.create_project({"name": "Proj", "root_path": str(project_root)})
            scan = service.scan_project({"project_id": created["id"], "limit": 20})

            self.assertIsNotNone(scan.get("code_graph"))
            self.assertGreater(scan["code_graph"]["symbols"], 0)

            symbols = [n for n in service.repository.list_nodes() if n.type == "Symbol"]
            labels = {n.label for n in symbols}
            self.assertIn("pkg/main.py::App", labels)
            self.assertIn("pkg/main.py::App.run", labels)
            self.assertIn("pkg/helpers.py::helper", labels)

            # main.py should IMPORTS helpers.py.
            main_node = next(
                n for n in service.repository.list_nodes()
                if n.type == "Artifact" and (n.metadata or {}).get("path") == "pkg/main.py"
            )
            helpers_node = next(
                n for n in service.repository.list_nodes()
                if n.type == "Artifact" and (n.metadata or {}).get("path") == "pkg/helpers.py"
            )
            imports = [e for e in service.repository.list_edges_touching([main_node.id])
                       if e.type == "IMPORTS" and e.source == main_node.id and e.target == helpers_node.id]
            self.assertEqual(len(imports), 1)


if __name__ == "__main__":
    unittest.main()
