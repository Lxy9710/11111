import importlib.util
import csv
import json
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "build_pilot_pair", Path(__file__).with_name("build_pilot_pair.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PilotPredicateTests(unittest.TestCase):
    def test_exact_decimal_bounds(self):
        self.assertEqual(module.bounds(60.2, 0, 0.06), [60.2, 60.26])
        self.assertEqual(module.bounds(72, -0.009, 0), [71.991, 72.0])

    def test_inclusive_and_outside(self):
        p = {"operator": "between_inclusive", "unit": "mm", "lower": 60.2, "upper": 60.26}
        for value in (60.2, 60.23, 60.26):
            self.assertTrue(module.evaluate_between(p, value)["satisfied"])
        for value in (60.199999, 60.260001):
            self.assertFalse(module.evaluate_between(p, value)["satisfied"])

    def test_invalid_bounds_and_values(self):
        for triple in ((1, 0.1, 0), (1, None, 0), (True, 0, 1), (float("nan"), 0, 1)):
            with self.assertRaises(ValueError):
                module.bounds(*triple)
        p = {"operator": "between_inclusive", "unit": "mm", "lower": 1, "upper": 2}
        for value in (None, float("inf"), True):
            with self.assertRaises(ValueError):
                module.evaluate_between(p, value)
        with self.assertRaises(ValueError):
            module.evaluate_between(dict(p, unit="inch"), 1)

    def test_missing_tolerance_not_fabricated(self):
        design = {"width": {"nominal": 17, "lower": None, "upper": None, "source": {"handle": "A"}}}
        config = {"entities": [{"handle": "A", "type": "DIMENSION"}]}
        result = module.build_feature_candidates("test", design, config)[0]
        self.assertEqual(result["intent_candidates"], [])
        self.assertTrue(result["gaps"])
        self.assertEqual(result["geometry_handles"], [])

    def test_missing_handle_rejected(self):
        design = {"width": {"nominal": 17, "lower": 0, "upper": 0.1, "source": {"handle": "A"}}}
        with self.assertRaises(ValueError):
            module.build_feature_candidates("test", design, {"entities": []})

    def test_position_only_not_promoted_to_traceable_intent(self):
        design = {"radius": {"nominal": 5, "lower": 0, "upper": 0.1, "source": {"position": [1, 2]}}}
        result = module.build_feature_candidates("test", design, {"entities": []})[0]
        self.assertEqual(result["intent_candidates"], [])


class GeneratedPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1] / "pilot" / "D207NQ.01"
        cls.output = cls.root / "results"
        cls.manifest = json.loads((cls.root / "source" / "extraction_manifest.json").read_text(encoding="utf-8"))
        cls.source = json.loads(Path(cls.manifest["json_path"]).read_text(encoding="utf-8"))
        cls.pairs = json.loads((cls.output / "pairs.json").read_text(encoding="utf-8"))
        cls.processes = json.loads((cls.output / "process_parameters.json").read_text(encoding="utf-8"))
        cls.features = json.loads((cls.output / "feature_candidates.json").read_text(encoding="utf-8"))

    def test_original_and_export_unchanged(self):
        self.assertEqual(module.digest(Path(self.manifest["source_dwg"])), self.manifest["source_sha256"])
        self.assertEqual(module.digest(Path(self.manifest["json_path"])), self.manifest["json_sha256"])
        self.assertTrue(self.manifest["autocad_released"])

    def test_every_pair_has_existing_evidence_and_real_route_parameter(self):
        entities = {e["handle"]: e for e in self.source["entities"]}
        route = {p["route_row"]["process_no"]: p for p in self.processes["processes"]}
        keys = set()
        for pair in self.pairs:
            identity = (pair["feature_id"], pair["process_no"], pair["process_parameter"]["name"])
            self.assertNotIn(identity, keys)
            keys.add(identity)
            for handle in pair["annotation_handles"]:
                self.assertIn(handle, entities)
            self.assertTrue(pair["annotation_handles"])
            proc = route[pair["process_no"]]
            self.assertEqual(proc["route_row"]["process_name_only"], pair["process_name"])
            self.assertEqual(proc["parameters"][pair["process_parameter"]["name"]], pair["process_parameter"])
            self.assertFalse(pair["human_reviewed"])

    def test_inner_diameter_pair_has_allowance_and_finished_tolerance(self):
        pairs = {
            p["process_no"]: p for p in self.pairs
            if p["feature_id"].endswith(":inner_diameter")
        }
        rough, finished = pairs["320"], pairs["390"]
        self.assertEqual(rough["annotation_handles"], ["21B4E"])
        self.assertEqual(rough["process_target_interval_mm"], [60.15, 60.17])
        self.assertIsNone(rough["comparison"]["satisfied"])
        self.assertEqual(finished["process_target_interval_mm"], [60.2, 60.26])
        self.assertTrue(finished["comparison"]["satisfied"])
        self.assertEqual(finished["comparison"]["status"], "direct_copy_consistency_only")

    def test_missing_data_and_fallbacks_remain_visible(self):
        features = {f["parameter_key"]: f for f in self.features}
        self.assertEqual(features["planar_dimension"]["intent_candidates"], [])
        self.assertEqual(features["groove_radius"]["intent_candidates"], [])
        self.assertTrue(self.processes["context_diagnostics"])
        self.assertEqual(self.processes["grind_loop"]["kind"], "二次循环")
        self.assertEqual(self.processes["grind_loop"]["material"], "Cr4Mo4V-YB688")
        for feature in self.features:
            self.assertEqual(feature["geometry_handles"], [])

    def test_counts_csv_and_provenance_match_persisted_data(self):
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        provenance = json.loads((self.output / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["pair_rows"], len(self.pairs))
        self.assertEqual(summary["route_steps"], len(self.processes["processes"]))
        self.assertEqual(summary["feature_candidates"], len(self.features))
        with (self.output / "pairs.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), len(self.pairs))
        project = Path(self.manifest["project_root"]).resolve()
        for entry in provenance["assets"] + provenance["imported_sources"]:
            path = Path(entry["path"]).resolve()
            self.assertIn(project, path.parents)
            self.assertEqual(module.digest(path), entry["sha256"])
        self.assertEqual(module.digest(Path(provenance["generator"]["path"])), provenance["generator"]["sha256"])


if __name__ == "__main__":
    unittest.main()
