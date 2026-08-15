from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.graph_analysis import (
    compute_degrees,
    detect_communities,
    personalized_pagerank,
)
from backend.architectos.models import MemoryNode
from backend.architectos.service import ArchitectOSService

TRIANGLES = [
    ("a", "b"), ("b", "c"), ("a", "c"),  # cluster 1
    ("d", "e"), ("e", "f"), ("d", "f"),  # cluster 2 (disconnected)
]
NODE_IDS = ["a", "b", "c", "d", "e", "f"]


class GraphAnalysisUnitTests(unittest.TestCase):
    def test_degrees_count_both_ends_and_ignore_unknown(self) -> None:
        degrees = compute_degrees(NODE_IDS, TRIANGLES + [("a", "zzz"), ("a", "a")])
        self.assertEqual(degrees, {"a": 2, "b": 2, "c": 2, "d": 2, "e": 2, "f": 2})

    def test_communities_split_two_triangles(self) -> None:
        communities = detect_communities(NODE_IDS, TRIANGLES)
        self.assertEqual(communities["a"], communities["b"])
        self.assertEqual(communities["b"], communities["c"])
        self.assertEqual(communities["d"], communities["e"])
        self.assertEqual(communities["e"], communities["f"])
        self.assertNotEqual(communities["a"], communities["d"])
        # Renumbered by size desc, ties by smallest member id → {a,b,c} is 0.
        self.assertEqual(communities["a"], 0)
        self.assertEqual(communities["d"], 1)

    def test_communities_deterministic(self) -> None:
        first = detect_communities(NODE_IDS, TRIANGLES)
        second = detect_communities(list(reversed(NODE_IDS)), list(reversed(TRIANGLES)))
        self.assertEqual(first, second)

    def test_personalized_pagerank_favors_seed_component(self) -> None:
        ranks = personalized_pagerank(NODE_IDS, TRIANGLES, seeds=["a"])
        for reachable in ("a", "b", "c"):
            for unreachable in ("d", "e", "f"):
                self.assertGreater(ranks[reachable], ranks[unreachable])

    def test_pagerank_without_valid_seed_is_zero(self) -> None:
        ranks = personalized_pagerank(NODE_IDS, TRIANGLES, seeds=["missing"])
        self.assertTrue(all(value == 0.0 for value in ranks.values()))


class ServiceGraphAnnotationTests(unittest.TestCase):
    def test_graph_payload_carries_community_degree_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            nodes = []
            for index in range(4):
                node = service.add_memory({
                    "project_id": "architectos",
                    "label": f"Decision {index}",
                    "type": "Decision",
                    "scope": "project",
                    "text": f"Architectural decision number {index} about storage and search.",
                })
                nodes.append(node["id"])
            # Explicit relationship (EXTRACTED) + an inferred similarity link.
            service.repository.add_edge(nodes[0], nodes[1], "DEPENDS_ON", "project")
            inferred = service.repository.add_edge(nodes[2], nodes[3], "RELATED_TO", "project")

            payload = service.graph(project_id="architectos")

            for node in payload["nodes"]:
                self.assertIn("community", node)
                self.assertIn("degree", node)
            self.assertIn("communities", payload)

            prov = {
                (edge["source"], edge["target"], edge["type"]): edge.get("provenance")
                for edge in payload["edges"]
            }
            self.assertEqual(prov.get((nodes[0], nodes[1], "DEPENDS_ON")), "EXTRACTED")
            self.assertEqual(prov.get((nodes[2], nodes[3], "RELATED_TO")), "INFERRED")

            degree_by_id = {node["id"]: node["degree"] for node in payload["nodes"]}
            self.assertGreaterEqual(degree_by_id.get(nodes[0], 0), 1)


class MultiHopExpansionTests(unittest.TestCase):
    # Deliberately unrelated vocabulary per node so the auto-linker adds no
    # similarity edges — the only edges are the explicit chain we create, keeping
    # hop distances exact.
    def _chain_service(self, tmp: str) -> tuple[ArchitectOSService, list[str]]:
        service = ArchitectOSService(Path(tmp))
        texts = [
            ("Alpha anchor", "Zephyr alpha anchor unique marker keyword"),
            ("Bravo node", "Bravo caching subsystem session cookie eviction"),
            ("Charlie node", "Charlie deployment pipeline release artifact rollout"),
            ("Delta node", "Delta localization glossary translation locale"),
        ]
        ids = [
            service.add_memory({
                "project_id": "architectos", "label": label, "type": "Decision",
                "scope": "project", "text": text,
            })["id"]
            for label, text in texts
        ]
        for source, target in zip(ids, ids[1:], strict=False):
            service.repository.add_edge(source, target, "DEPENDS_ON", "project")
        return service, ids

    def test_pagerank_expansion_reaches_two_hops_not_three(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, ids = self._chain_service(tmp)
            anchor, one_hop, two_hop, three_hop = ids
            _edges, neighbor_order = service._pagerank_graph_expansion(seeds=[anchor], limit=8)
            neighbors = set(neighbor_order)
            self.assertIn(one_hop, neighbors)   # 1 hop
            self.assertIn(two_hop, neighbors)   # 2 hops — the multi-hop upgrade
            self.assertNotIn(three_hop, neighbors)  # 3 hops — beyond GRAPH_EXPANSION_HOPS
            self.assertNotIn(anchor, neighbors)  # seeds are never returned as neighbors

    def test_expansion_surfaces_two_hop_neighbor_in_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, ids = self._chain_service(tmp)
            anchor, one_hop, two_hop, _three_hop = ids
            result = service.search_memory(
                "zephyr alpha anchor unique", project_id="architectos", limit=8, expand_graph=True,
            )
            surfaced = {hit["node"]["id"] for hit in result["hits"]}
            self.assertIn(anchor, surfaced)
            self.assertIn(two_hop, surfaced)  # reachable only via multi-hop expansion
            self.assertGreaterEqual(result["retrieval"].get("graph_neighbors", 0), 1)

    def test_interactive_search_does_not_walk_graph(self) -> None:
        # Without expand_graph the graph is not walked at all (interactive latency).
        with tempfile.TemporaryDirectory() as tmp:
            service, ids = self._chain_service(tmp)
            anchor, one_hop, _two, _three = ids
            result = service.search_memory("zephyr alpha anchor unique", project_id="architectos", limit=8)
            surfaced = {hit["node"]["id"] for hit in result["hits"]}
            self.assertIn(anchor, surfaced)
            self.assertNotIn(one_hop, surfaced)


class TemporalSupersedeTests(unittest.TestCase):
    OLD_TS = "2026-01-01T00:00:00+00:00"
    NEW_TS = "2026-06-01T00:00:00+00:00"
    BEFORE = "2026-03-01T00:00:00+00:00"
    AFTER = "2026-07-01T00:00:00+00:00"

    def _fixture(self, tmp: str) -> tuple[ArchitectOSService, str, str]:
        service = ArchitectOSService(Path(tmp))
        old_id = service.add_memory({
            "project_id": "architectos", "label": "Old limit", "type": "Constraint",
            "scope": "project", "text": "Quarterlimit budget cap is five thousand euros.",
        })["id"]
        new_id = service.add_memory({
            "project_id": "architectos", "label": "New limit", "type": "Constraint",
            "scope": "project", "text": "Quarterlimit budget cap is nine thousand euros.",
        })["id"]
        # Pin deterministic timestamps so as_of windows are exact.
        old_node = service.repository.get_node(old_id)
        old_node.created_at = self.OLD_TS
        service.repository.upsert_node(old_node)
        new_node = service.repository.get_node(new_id)
        new_node.created_at = self.NEW_TS
        service.repository.upsert_node(new_node)
        # New fact supersedes the old one.
        service.create_graph_edge({"source": new_id, "target": old_id, "type": "SUPERSEDES"})
        return service, old_id, new_id

    def test_supersede_stamps_invalid_at_on_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, old_id, new_id = self._fixture(tmp)
            old_node = service.repository.get_node(old_id)
            self.assertEqual(old_node.metadata.get("invalid_at"), self.NEW_TS)
            self.assertEqual(old_node.metadata.get("superseded_by"), new_id)

    def test_live_search_hides_superseded_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, old_id, new_id = self._fixture(tmp)
            ids = {hit["node"]["id"] for hit in service.search_memory("quarterlimit budget cap", project_id="architectos", limit=8)["hits"]}
            self.assertIn(new_id, ids)
            self.assertNotIn(old_id, ids)

    def test_as_of_before_shows_only_old_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, old_id, new_id = self._fixture(tmp)
            ids = {hit["node"]["id"] for hit in service.search_memory("quarterlimit budget cap", project_id="architectos", limit=8, as_of=self.BEFORE)["hits"]}
            self.assertIn(old_id, ids)     # was true back then
            self.assertNotIn(new_id, ids)  # not created yet

    def test_as_of_after_shows_only_new_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, old_id, new_id = self._fixture(tmp)
            ids = {hit["node"]["id"] for hit in service.search_memory("quarterlimit budget cap", project_id="architectos", limit=8, as_of=self.AFTER)["hits"]}
            self.assertIn(new_id, ids)
            self.assertNotIn(old_id, ids)   # already superseded by then


class IngestDedupTests(unittest.TestCase):
    def _count(self, service: ArchitectOSService, label: str) -> int:
        return sum(
            1 for node in service.repository.list_nodes()
            if node.status == "active" and node.label == label
        )

    def test_exact_restatement_updates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            payload = {
                "project_id": "architectos", "label": "Zephyr rule", "type": "Decision",
                "scope": "project", "text": "Zephyr deployment uses blue green rollout strategy.",
            }
            first = service.add_memory(dict(payload))
            second = service.add_memory(dict(payload, confidence=0.95))
            self.assertTrue(second.get("dedup_merged"))
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(self._count(service, "Zephyr rule"), 1)
            self.assertEqual(second["metadata"].get("dedup_hits"), 1)
            self.assertGreaterEqual(second["confidence"], 0.95)

    def test_changed_value_is_not_merged(self) -> None:
        # Distinct labels so node identity (label-derived) does not collide; the
        # value change alone must not fold the two facts together.
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            old = service.add_memory({
                "project_id": "architectos", "label": "Zephyr cap original", "type": "Constraint",
                "scope": "project", "text": "Zephyr quarterlimit budget cap is five thousand euros.",
            })
            new = service.add_memory({
                "project_id": "architectos", "label": "Zephyr cap revised", "type": "Constraint",
                "scope": "project", "text": "Zephyr quarterlimit budget cap is nine thousand euros.",
            })
            self.assertFalse(new.get("dedup_merged"))
            self.assertNotEqual(new["id"], old["id"])
            ids = {node.id for node in service.repository.list_nodes() if node.label in {"Zephyr cap original", "Zephyr cap revised"}}
            self.assertEqual(len(ids), 2)

    def test_near_duplicate_gets_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            base = service.add_memory({
                "project_id": "architectos", "label": "Cache note", "type": "Decision",
                "scope": "project", "text": "Alpha caching layer uses redis backing store sessions",
            })
            similar = service.add_memory({
                "project_id": "architectos", "label": "Cache note", "type": "Decision",
                "scope": "project", "text": "Alpha caching layer uses redis backing store sessions cookies",
            })
            self.assertFalse(similar.get("dedup_merged"))
            self.assertEqual(similar["metadata"].get("possible_duplicate_of"), base["id"])

    def test_dedup_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            payload = {
                "project_id": "architectos", "label": "Zephyr rule", "type": "Decision",
                "scope": "project", "text": "Zephyr deployment uses blue green rollout strategy.",
            }
            service.add_memory(dict(payload))
            second = service.add_memory(dict(payload, dedup=False))
            # dedup disabled → raw create/overwrite path, no dedup bookkeeping.
            self.assertFalse(second.get("dedup_merged"))
            self.assertNotIn("dedup_hits", second["metadata"])


class CommunitySummaryTests(unittest.TestCase):
    def _clustered_service(self, tmp: str) -> tuple[ArchitectOSService, dict[str, str]]:
        service = ArchitectOSService(Path(tmp))
        cache = [
            ("Cache A", "Redis cache eviction policy alpha lru marker"),
            ("Cache B", "Redis cache eviction policy beta ttl marker"),
            ("Cache C", "Redis cache eviction policy gamma cold marker"),
        ]
        deploy = [
            ("Deploy A", "Kubernetes deployment rollout alpha canary release"),
            ("Deploy B", "Kubernetes deployment rollout beta bluegreen release"),
            ("Deploy C", "Kubernetes deployment rollout gamma staging release"),
        ]
        ids: dict[str, str] = {}
        for label, text in cache + deploy:
            ids[label] = service.add_memory({
                "project_id": "architectos", "label": label, "type": "Concept",
                "scope": "project", "text": text,
            })["id"]
        for a, b in [("Cache A", "Cache B"), ("Cache B", "Cache C"), ("Cache A", "Cache C")]:
            service.repository.add_edge(ids[a], ids[b], "DEPENDS_ON", "project")
        for a, b in [("Deploy A", "Deploy B"), ("Deploy B", "Deploy C"), ("Deploy A", "Deploy C")]:
            service.repository.add_edge(ids[a], ids[b], "DEPENDS_ON", "project")
        return service, ids

    def test_summaries_have_structure_and_size_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self._clustered_service(tmp)
            themes = service.community_summaries()["communities"]
            self.assertTrue(themes)
            sizes = [t["size"] for t in themes]
            self.assertEqual(sizes, sorted(sizes, reverse=True))
            for theme in themes:
                for key in ("id", "label", "size", "keywords", "members", "summary", "score"):
                    self.assertIn(key, theme)
                self.assertGreaterEqual(theme["size"], 2)
                self.assertTrue(theme["members"])

    def test_query_ranks_relevant_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self._clustered_service(tmp)
            themes = service.community_summaries(query="redis cache eviction")["communities"]
            self.assertTrue(themes)
            self.assertTrue(all(t["score"] > 0 for t in themes))
            self.assertTrue(any("redis" in t["keywords"] for t in themes))

    def test_search_dual_level_attaches_communities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self._clustered_service(tmp)
            result = service.search_memory("redis cache eviction policy marker", include_communities=True, limit=6)
            self.assertIn("communities", result)
            self.assertIsInstance(result["communities"], list)
            hit_ids = {hit["node"]["id"] for hit in result["hits"]}
            for community in result["communities"]:
                self.assertTrue(set(community["hit_ids"]) <= hit_ids)
                self.assertGreaterEqual(community["size"], 2)

    def test_search_without_flag_has_no_communities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self._clustered_service(tmp)
            result = service.search_memory("redis cache eviction policy marker", limit=6)
            self.assertNotIn("communities", result)


class NoiseArtifactPurgeTests(unittest.TestCase):
    def _artifact(self, service: ArchitectOSService, label: str, path: str, ext: str) -> str:
        return service.add_memory({
            "project_id": "architectos", "label": label, "type": "Artifact",
            "scope": "project", "text": f"Imported {path}",
            "source": "project_scan",
            "metadata": {"source": "project_scan", "path": path, "extension": ext},
        })["id"]

    def test_purge_flags_log_and_scratch_dir_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            junk_log = self._artifact(
                service, "Code: .playwright-mcp/console.log",
                ".playwright-mcp/console-2026-07-01T08-13-16.log", ".log",
            )
            junk_lock = self._artifact(
                service, "Code: package-lock.json", "frontend/package-lock.json", ".json",
            )
            keeper = service.add_memory({
                "project_id": "architectos", "label": "Auth decision", "type": "Decision",
                "scope": "project", "text": "JWT rotation happens every thirty minutes.",
                "source": "adr",
            })["id"]

            preview = service.purge_noise_nodes(dry_run=True)
            flagged = {item["id"] for item in preview["items"]}
            self.assertIn(junk_log, flagged)
            self.assertIn(junk_lock, flagged)
            self.assertNotIn(keeper, flagged)
            # dry-run must not mutate anything
            self.assertEqual(service.repository.get_node(junk_log).status, "active")

            result = service.purge_noise_nodes()
            self.assertEqual(result["purged"], 2)
            self.assertEqual(service.repository.get_node(junk_log).status, "archived")
            self.assertEqual(
                service.repository.get_node(junk_log).metadata.get("archived_reason"),
                "noise_artifact",
            )
            self.assertEqual(service.repository.get_node(keeper).status, "active")

    def test_pinned_artifact_is_never_purged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node_id = self._artifact(service, "Code: debug.log", "logs/debug.log", ".log")
            node = service.repository.get_node(node_id)
            node.metadata["pinned"] = True
            service.repository.upsert_node(node)
            result = service.purge_noise_nodes()
            self.assertEqual(result["purged"], 0)
            self.assertEqual(service.repository.get_node(node_id).status, "active")

    def test_noise_file_name_classifier(self) -> None:
        self.assertTrue(ArchitectOSService._is_noise_file_name("console.log"))
        self.assertTrue(ArchitectOSService._is_noise_file_name("yarn.lock"))
        self.assertTrue(ArchitectOSService._is_noise_file_name("bundle.min.js"))
        self.assertTrue(ArchitectOSService._is_noise_file_name(".DS_Store"))
        self.assertFalse(ArchitectOSService._is_noise_file_name("service.py"))
        self.assertFalse(ArchitectOSService._is_noise_file_name("README.md"))


class RelevanceFloorTests(unittest.TestCase):
    def test_weak_tail_dropped_below_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos", "label": "Zephyr rollout strategy decision",
                "type": "Decision", "scope": "project",
                "text": "Zephyr rollout strategy uses blue green canary staged deployment.",
                "source": "adr",
            })
            # A loosely related node sharing one weak token, propped up by static bonuses.
            service.add_memory({
                "project_id": "architectos", "label": "Finance planning note",
                "type": "Lesson", "scope": "project",
                "text": "Quarterly finance planning strategy session for an unrelated team.",
                "source": "chat",
            })
            strict = service.search_memory(
                "zephyr rollout strategy blue green", project_id="architectos",
                limit=8, relevance_floor=0.5,
            )
            loose = service.search_memory(
                "zephyr rollout strategy blue green", project_id="architectos",
                limit=8, relevance_floor=0.0,
            )
            strict_labels = [hit["node"]["label"] for hit in strict["hits"]]
            self.assertLessEqual(len(strict["hits"]), len(loose["hits"]))
            self.assertTrue(strict["hits"])  # top hit always survives
            self.assertEqual(strict_labels[0], "Zephyr rollout strategy decision")
            # The weak, single-token match must not survive a strict floor.
            self.assertNotIn("Finance planning note", strict_labels)
            self.assertIn("relevance_floor", strict["retrieval"])
            self.assertGreaterEqual(strict["retrieval"].get("dropped_low_relevance", 0), 0)

    def test_min_score_filters_below_absolute_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos", "label": "Payment retry policy",
                "type": "Decision", "scope": "project",
                "text": "Payment retry policy attempts three times with backoff.",
                "source": "adr",
            })
            huge = service.search_memory(
                "payment retry policy", project_id="architectos", limit=8, min_score=100000.0,
            )
            # Absolute floor above every real score keeps only the guaranteed top hit.
            self.assertLessEqual(len(huge["hits"]), 1)
            self.assertEqual(huge["retrieval"].get("min_score"), 100000.0)


class GraphFetchScopeTests(unittest.TestCase):
    """The scoped SQL fetch must not truncate the set a filter searches over."""

    NEEDLE = "zetatronic"

    def _service_with_many_nodes(self, tmp: str, count: int = 900) -> ArchitectOSService:
        service = ArchitectOSService(Path(tmp))
        # Written straight to the repository: add_memory() would run the auto-linker
        # for every node, which is far too slow for a corpus this size.
        for index in range(count):
            # Oldest node holds the needle, so any recency-ordered LIMIT hides it.
            marker = self.NEEDLE if index == 0 else "filler"
            stamp = f"2026-01-{(index % 28) + 1:02d}T{(index % 24):02d}:00:00+00:00"
            service.repository.upsert_node(
                MemoryNode(
                    id=f"node-{index:04d}",
                    type="Decision",
                    label=f"{marker} decision {index}",
                    scope="project",
                    text=f"{marker} body text for node {index}",
                    project_id="architectos",
                    created_at=stamp,
                    updated_at=stamp,
                    # Pre-seeded so annotate_nodes() skips its upsert and leaves the
                    # timestamps (and therefore the recency ordering) exactly as set.
                    metadata={"memory_tier": "short_term"},
                )
            )
        return service

    def test_search_finds_old_node_beyond_the_fetch_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_many_nodes(tmp)
            payload = service.graph(project_id="architectos", limit=100, search=self.NEEDLE)
            labels = [node["label"] for node in payload["nodes"]]
            self.assertTrue(
                any(self.NEEDLE in label for label in labels),
                "graph search must scan every active node, not just the newest page",
            )

    def test_type_filter_is_not_starved_by_the_fetch_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_many_nodes(tmp, count=900)
            service.repository.upsert_node(
                MemoryNode(
                    id="rare-risk",
                    type="Risk",
                    label="Rare risk node",
                    scope="project",
                    text="A single Risk among many Decisions.",
                    project_id="architectos",
                    created_at="2025-01-01T00:00:00+00:00",
                    updated_at="2025-01-01T00:00:00+00:00",
                    metadata={"memory_tier": "short_term"},
                )
            )
            payload = service.graph(project_id="architectos", limit=100, node_type="Risk")
            self.assertIn("rare-risk", {node["id"] for node in payload["nodes"]})

    def test_narrowing_filter_keeps_every_dropdown_option(self) -> None:
        # Filters are pushed into SQL; the facet lists must still describe the
        # whole store, or the UI dropdowns collapse to the current selection.
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_many_nodes(tmp, count=20)
            service.repository.upsert_node(
                MemoryNode(
                    id="a-risk",
                    type="Risk",
                    label="A risk",
                    scope="global",
                    text="Risk body.",
                    project_id="architectos",
                    metadata={"memory_tier": "short_term"},
                )
            )
            payload = service.graph(project_id="architectos", limit=100, node_type="Risk")
            self.assertEqual({node["id"] for node in payload["nodes"]}, {"a-risk"})
            self.assertIn("Decision", payload["types"])
            self.assertIn("Risk", payload["types"])
            self.assertIn("project", payload["scopes"])

    def test_non_ascii_search_still_matches(self) -> None:
        # Payloads are stored with ensure_ascii=True, so a SQL LIKE prefilter can
        # never match Cyrillic; the query has to fall back to the Python scan.
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_many_nodes(tmp, count=20)
            service.repository.upsert_node(
                MemoryNode(
                    id="cyrillic-node",
                    type="Decision",
                    label="Кириллическое решение",
                    scope="project",
                    text="Текст на кириллице.",
                    project_id="architectos",
                    metadata={"memory_tier": "short_term"},
                )
            )
            payload = service.graph(project_id="architectos", limit=100, search="Кириллическое")
            self.assertIn("cyrillic-node", {node["id"] for node in payload["nodes"]})

    def test_multi_word_search_spanning_two_fields_still_matches(self) -> None:
        # "decision zetatronic" spans label -> text only in the joined haystack,
        # never in the JSON payload, so the prefilter must not be used.
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.upsert_node(
                MemoryNode(
                    id="spanning",
                    type="Decision",
                    label="Rollout decision",
                    scope="project",
                    text="zetatronic rollout body",
                    project_id="architectos",
                    metadata={"memory_tier": "short_term"},
                )
            )
            payload = service.graph(project_id="architectos", limit=100, search="decision zetatronic")
            self.assertIn("spanning", {node["id"] for node in payload["nodes"]})


if __name__ == "__main__":
    unittest.main()
