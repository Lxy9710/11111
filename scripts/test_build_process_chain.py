from __future__ import annotations

import copy
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_process_chain as chain
from build_pilot_pair import build_feature_candidates, digest


def fixture() -> dict:
    config = {"entities": [{"handle": "A", "type": "DIMENSION"}]}
    design = {"outer_diameter": {
        "role": "diameter", "nominal": 72, "upper": 0, "lower": -0.009,
        "source": {"handle": "A"},
    }}
    return {
        "feature_candidates.json": build_feature_candidates("test", design, config),
        "source_entities.json": config,
        "design_parameters.json": {
            "fields": [
                {"field_name": "零件型号", "value": "test"},
                {"field_name": "产品名称", "value": "外圈"},
            ],
            "parameters": design,
        },
        "summary.json": {"case_id": "test"},
        "process_parameters.json": {
            "grade": "D", "grind_loop": {"kind": "二次循环"},
            "has_lapping": True, "has_fine_grind_plane": True,
            "context_diagnostics": [], "processes": [],
        },
    }


def operation(number: str, canonical: str, source_type: str = "product_drawing",
              source_process: str = "", nominal: float = 72) -> dict:
    return {
        "route_row": {"process_no": number, "process_name_only": canonical, "process_type": "grind"},
        "canonical_process": canonical, "status": "computed",
        "rule": {
            "feature": "外径", "dimension_key": "D",
            "parameters": [{
                "name": "D", "source_type": source_type,
                "requires_inspection": source_type != "copy_from_other_process",
                "copy_from": {"process": source_process, "parameter": "D"},
            }],
        },
        "parameters": {"D": {
            "name": "D", "nominal_mm": nominal, "upper_tolerance_mm": 0,
            "lower_tolerance_mm": -0.009, "direct_value_mm": None,
            "source_type": source_type,
            "evidence": source_process + ".D" if source_process else "design.outer_diameter",
            "copied_from_process": source_process or None, "extra": {},
        }},
    }


def graph_for(*operations: dict) -> tuple[dict, dict]:
    docs = fixture()
    docs["process_parameters.json"]["processes"] = list(operations)
    return chain.build_graph(docs), docs


class ChainUnitTests(unittest.TestCase):
    def test_json_pointer_escapes(self):
        reference = chain.ref("x", "a/b~c", 0)
        self.assertEqual(chain.resolve_pointer({"a/b~c": [4]}, reference["pointer"]), 4)
        with self.assertRaises(ValueError):
            chain.resolve_pointer({}, "invalid")

    def test_missing_annotation_handle_rejected(self):
        docs = fixture()
        docs["source_entities.json"]["entities"] = []
        with self.assertRaisesRegex(ValueError, "Annotation handle"):
            chain.build_graph(docs)

    def test_repeated_process_uses_latest_prior_instance(self):
        graph, _ = graph_for(
            operation("10", "finish"),
            operation("20", "finish"),
            operation("30", "check", "copy_from_other_process", "finish"),
        )
        edges = [e for e in graph["edges"] if e["relation"] == "copies_parameter_from"]
        self.assertEqual([(e["source"], e["target"]) for e in edges],
                         [("test:state:30:D", "test:state:20:D")])

    def test_future_named_source_stays_unresolved(self):
        graph, _ = graph_for(
            operation("10", "reference", "copy_from_other_process", "finish"),
            operation("20", "finish"),
        )
        self.assertFalse(any(e["relation"] == "copies_parameter_from" for e in graph["edges"]))
        self.assertTrue(any(t["issue"] == "no_prior_route_record" for t in graph["review_tasks"]))

    def test_copy_mismatch_not_silently_attached(self):
        graph, _ = graph_for(
            operation("10", "finish"),
            operation("20", "reference", "copy_from_other_process", "finish", nominal=73),
        )
        self.assertFalse(any(e["relation"] == "copies_parameter_from" for e in graph["edges"]))
        self.assertTrue(any(t["issue"] == "prior_record_value_mismatch" for t in graph["review_tasks"]))

    def test_reference_copy_not_machining_target(self):
        graph, _ = graph_for(operation("10", "reference", "copy_from_other_process", "turning"))
        state = next(n for n in graph["nodes"] if n["kind"] == "parameter_state")
        self.assertEqual(state["role"], "reference_copy_not_machining_evidence")
        self.assertEqual(state["finished_check"], "not_applicable")

    def test_missing_requirement_not_recreated_from_engine_defaults(self):
        docs = fixture()
        docs["feature_candidates.json"][0]["intent_candidates"] = []
        docs["feature_candidates.json"][0]["design_parameter"]["upper"] = None
        docs["process_parameters.json"]["processes"] = [operation("10", "finish")]
        graph = chain.build_graph(docs)
        self.assertFalse(any(n["kind"] == "design_requirement" for n in graph["nodes"]))
        state = next(n for n in graph["nodes"] if n["kind"] == "parameter_state")
        self.assertEqual(state["finished_check"], "unknown_missing_or_ambiguous_requirement")

    def test_actual_source_wins_over_declared_rule(self):
        op = operation("10", "finish")
        op["rule"]["parameters"][0]["source_type"] = "lookup_allowance_calculate"
        graph, _ = graph_for(op)
        state = next(n for n in graph["nodes"] if n["kind"] == "parameter_state")
        self.assertEqual(state["lineage_status"], "direct_design_field")
        self.assertEqual(state["declared_source_type"], "lookup_allowance_calculate")
        self.assertTrue(any(e["relation"] == "copies_finished_requirement" for e in graph["edges"]))

    def test_finished_copy_contradiction_rejected(self):
        with self.assertRaisesRegex(ValueError, "contradicts"):
            graph_for(operation("10", "finish", nominal=73))

    def test_nonfinite_and_missing_numbers_rejected(self):
        for number in (float("nan"), float("inf"), True, None):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    chain.parameter_value({
                        "nominal_mm": number, "direct_value_mm": None,
                        "lower_tolerance_mm": 0, "upper_tolerance_mm": 0,
                    })

    def test_direct_display_value_does_not_claim_mm(self):
        value = chain.parameter_value({"nominal_mm": None, "direct_value_mm": 0.4})
        self.assertIsNone(value["unit"])
        self.assertEqual(value["unit_status"], "not_normalized")
        self.assertEqual(value["raw_engine_fields"]["direct_value_mm"], 0.4)

    def test_unavailable_operation_preserved_without_fake_state(self):
        op = operation("10", "inspection")
        op.update(parameters={}, status="outside_grinding_pilot", canonical_process=None)
        graph, _ = graph_for(op)
        self.assertEqual(sum(n["kind"] == "operation" for n in graph["nodes"]), 1)
        self.assertFalse(any(n["kind"] == "parameter_state" for n in graph["nodes"]))
        self.assertTrue(any(t["issue"] == "operation_parameters_unavailable" for t in graph["review_tasks"]))

    def test_duplicate_operation_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate node"):
            graph_for(operation("10", "finish"), operation("10", "finish"))

    def test_dangling_edge_and_false_gold_rejected(self):
        graph, docs = graph_for(operation("10", "finish"))
        bad = copy.deepcopy(graph)
        bad["edges"][0]["target"] = "missing"
        with self.assertRaises(ValueError):
            chain.validate_graph(bad, docs)
        bad = copy.deepcopy(graph)
        bad["nodes"][0]["human_reviewed"] = True
        with self.assertRaises(ValueError):
            chain.validate_graph(bad, docs)

    def test_source_paths_are_explicit_multihop_references(self):
        graph, _ = graph_for(
            operation("10", "finish"),
            operation("20", "check1", "copy_from_other_process", "finish"),
            operation("30", "check2", "copy_from_other_process", "check1"),
        )
        self.assertEqual(chain.source_paths(graph, "test:state:30:D"), [[
            "test:state:30:D", "test:state:20:D", "test:state:10:D", "test:outer_diameter",
        ]])
        with self.assertRaises(ValueError):
            chain.source_paths(graph, "missing")

    def test_copy_override_symbol_from_actual_evidence(self):
        value = {"name": "D", "copied_from_process": "stage", "evidence": "stage.D2(一次循环)"}
        self.assertEqual(chain.copy_source_symbol(value, {"copy_from": {"parameter": "D"}}), "D2")

    def test_deterministic_and_nonmutating(self):
        docs = fixture()
        docs["process_parameters.json"]["processes"] = [operation("10", "finish")]
        before = copy.deepcopy(docs)
        first = chain.build_graph(docs)
        second = chain.build_graph(docs)
        self.assertEqual(first, second)
        self.assertEqual(docs, before)

    def test_csv_keeps_nested_pointer_lists(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chain.csv"
            chain.save_csv(path, [{"ids": ["x", "y"]}], ["ids"])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(json.loads(next(csv.DictReader(stream))["ids"]), ["x", "y"])


class RealChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1] / "pilot" / "D207NQ.01"
        cls.docs = chain.load_verified_inputs(cls.root / "chain_source")
        cls.graph = chain.build_graph(cls.docs)
        cls.nodes = {n["id"]: n for n in cls.graph["nodes"]}

    def test_every_generated_parameter_and_route_step_preserved(self):
        processes = self.docs["process_parameters.json"]["processes"]
        self.assertEqual(sum(n["kind"] == "operation" for n in self.nodes.values()), len(processes))
        self.assertEqual(
            sum(n["kind"] == "parameter_state" for n in self.nodes.values()),
            sum(len(p["parameters"]) for p in processes),
        )
        for op in processes:
            for symbol, value in op["parameters"].items():
                sid = f"D207NQ.01:state:{op['route_row']['process_no']}:{symbol}"
                self.assertEqual(self.nodes[sid]["resolved_parameter"], value)

    def test_inner_diameter_real_intervals_and_override(self):
        rough = self.nodes["D207NQ.01:state:320:D2"]
        final = self.nodes["D207NQ.01:state:390:D2"]
        self.assertEqual(rough["value"]["target_interval_mm"], [60.15, 60.17])
        self.assertEqual(rough["finished_check"], "not_applicable")
        self.assertEqual(final["value"]["target_interval_mm"], [60.2, 60.26])
        self.assertEqual(final["declared_source_type"], "lookup_allowance_calculate")
        self.assertEqual(final["actual_source_type"], "product_drawing")

    def test_reference_dimensions_and_long_range_latest_copy(self):
        self.assertEqual(self.nodes["D207NQ.01:state:300:D"]["role"], "reference_copy_not_machining_evidence")
        edges = {(e["source"], e["target"]) for e in self.graph["edges"] if e["relation"] == "copies_parameter_from"}
        self.assertIn(("D207NQ.01:state:425:D", "D207NQ.01:state:415:D"), edges)
        self.assertNotIn(("D207NQ.01:state:425:D", "D207NQ.01:state:360:D"), edges)
        self.assertGreater(chain.summarize(self.graph)["maximum_verified_copy_route_gap"], 1)

    def test_missing_geometry_units_and_turning_sources_are_not_hidden(self):
        issues = {t["issue"] for t in self.graph["review_tasks"]}
        self.assertTrue({"geometry_group_missing", "direct_value_unit_unverified",
                         "no_prior_route_record", "upstream_context_diagnostic"}.issubset(issues))
        self.assertFalse(chain.summarize(self.graph)["training_ready"])
        self.assertFalse(any(n["human_reviewed"] for n in self.nodes.values()))

    def test_replay_preserved_original_pair_data(self):
        for name in ("design_parameters.json", "process_parameters.json", "feature_candidates.json", "pairs.json", "pairs.csv"):
            self.assertEqual(digest(self.root / "results" / name), digest(self.root / "chain_source" / name))

    def test_technical_requirement_evidence_retained_without_invented_binding(self):
        documents = [n for n in self.nodes.values() if n["kind"] == "document_evidence"]
        note = next(n for n in documents if n["field_name"] == "技术要求")
        self.assertEqual(note["annotation_handles"], ["21B16"])
        self.assertIn("非装配倒角", note["text"])
        self.assertFalse(any(
            e["relation"] == "constrains" and e["source"] == note["id"]
            for e in self.graph["edges"]
        ))

    def test_persisted_artifacts_match_graph_and_provenance(self):
        output = self.root / "process_chain"
        persisted = json.loads((output / "association_graph.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, self.graph)
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8")),
                         chain.summarize(self.graph))
        provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
        for entry in provenance["inputs"] + [provenance["generator"], provenance["helper"]]:
            self.assertEqual(digest(Path(entry["path"])), entry["sha256"])
        for filename, expected in (
            ("nodes.csv", len(self.graph["nodes"])),
            ("edges.csv", len(self.graph["edges"])),
            ("feature_chains.csv", len(chain.chain_rows(self.graph))),
            ("review_tasks.csv", len(self.graph["review_tasks"])),
        ):
            with (output / filename).open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), expected)

    def test_cli_replay_is_byte_identical_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "replay"
            args = [
                sys.executable, str(Path(chain.__file__).resolve()),
                "--source-results", str(self.root / "chain_source"), "--output", str(output),
            ]
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            names = (
                "association_graph.json", "feature_chains.json", "feature_chains.csv",
                "nodes.csv", "edges.csv", "review_tasks.csv", "summary.json", "provenance.json",
            )
            before = {name: digest(output / name) for name in names}
            for name in names:
                self.assertEqual(before[name], digest(self.root / "process_chain" / name))
            second = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("FileExistsError", second.stderr)
            self.assertEqual(before, {name: digest(output / name) for name in names})


if __name__ == "__main__":
    unittest.main()
