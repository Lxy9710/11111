from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assemble_outer_dataset import proposed_family, proposed_split, undirected_components
from assign_splits import supervision_fingerprint
from build_outer_dataset import ROOT, identity_status, known_grade
from farmcad.data.dataset import load_cases
from farmcad.data.schema import validate_design
from farmcad.utils import read_json


class BatchPolicyTests(unittest.TestCase):
    def test_only_known_filename_prefix_is_normalized(self):
        self.assertEqual(identity_status("D207NQ.01", "D207NQ.01"), "exact")
        self.assertEqual(identity_status("（内部）D209NW1U1(H).01", "D209NW1U1(H).01"),
                         "internal_prefix_removed_requires_review")
        self.assertEqual(identity_status("D207NW1Y.56", "3D207NW1Y.56"), "mismatch_requires_review")
        self.assertEqual(identity_status("207.02", "E207N4QU.02 JP"), "mismatch_requires_review")

    def test_unknown_precision_does_not_use_upstream_default(self):
        self.assertFalse(known_grade({"零件型号": "3D1000095X3.01"}))
        self.assertFalse(known_grade({"零件型号": "unidentified"}))
        self.assertTrue(known_grade({"零件型号": "D207NQ.01"}))
        self.assertTrue(known_grade({"零件型号": "3D1000095X3.01", "精度等级": "D"}))

    def test_related_207_variants_stay_in_training_reservation(self):
        first = proposed_family("D207NQ.01")
        second = proposed_family("E207N4QU.01")
        self.assertEqual(first, second)
        self.assertEqual(proposed_split(first), "train")
        self.assertEqual(proposed_split(proposed_family("D209NW1U1(H).01")), "validation")
        self.assertEqual(proposed_split(proposed_family("D210NW1U(H).01")), "test")
        with self.assertRaises(ValueError):
            proposed_family("unidentified")

    def test_duplicate_components_join_across_different_families(self):
        rows = [
            {"sample_id": "a", "family_id": "F1", "drawing_sha256": "x", "design_fingerprint": "p"},
            {"sample_id": "b", "family_id": "F2", "drawing_sha256": "x", "design_fingerprint": "q"},
            {"sample_id": "c", "family_id": "F2", "drawing_sha256": "z", "design_fingerprint": "r"},
            {"sample_id": "d", "family_id": "F3", "drawing_sha256": "w", "design_fingerprint": "s"},
        ]
        groups = undirected_components(rows)
        self.assertEqual(groups["a"], groups["b"])
        self.assertEqual(groups["b"], groups["c"])
        self.assertNotEqual(groups["c"], groups["d"])
        self.assertEqual(groups, undirected_components(list(reversed(rows))))


DATASET = ROOT / "chainmodel" / "data" / "processed" / "outer-batch-v2"


@unittest.skipUnless((DATASET / "dataset_summary.json").is_file(), "Finalize the real batch before integration checks")
class GeneratedBatchTests(unittest.TestCase):
    def test_unlabelled_holdouts_are_not_fake_training_targets(self):
        cases = load_cases(DATASET / "manifest.ready.json", smoke=True)
        self.assertEqual([c.case_id for c in cases], ["D207NQ.01"])
        with self.assertRaisesRegex(ValueError, "validation"):
            load_cases(DATASET / "manifest.ready.json")
        pool = read_json(DATASET / "design_pool_manifest.json")
        for row in pool["cases"]:
            validate_design(read_json(DATASET / row["design"]))
            if row["split"] in ("validation", "test"):
                self.assertIsNone(row["target"])
        self.assertFalse(pool["family_review_complete"])

    def test_original_d207_pair_is_preserved_by_new_batch_path(self):
        cases = load_cases(DATASET / "manifest.ready.json", smoke=True)
        old = ROOT / "chainmodel" / "data" / "processed" / "d207nq01"
        self.assertEqual(cases[0].design, read_json(old / "design.json"))
        self.assertEqual(cases[0].target, read_json(old / "target.json"))

    def test_batch_never_promotes_candidates_to_gold(self):
        cases = load_cases(DATASET / "manifest.ready.json", smoke=True)
        self.assertEqual(len(cases[0].target["events"]), 169)
        for event in cases[0].target["events"]:
            self.assertNotEqual(event["quality"], "gold")
            for annotation in event.get("labels", {}).values():
                self.assertNotEqual(annotation["quality"], "gold")
        self.assertEqual(cases[0].target["feature_members"], {})

    def test_inventory_covers_all_drawings_and_expected_blockers(self):
        rows = read_json(DATASET / "audit" / "inventory.json")
        self.assertEqual(len(rows), 39)
        self.assertTrue(all(row["raw_json"] for row in rows))
        outer = [r for r in rows if r["product"] == "外圈"]
        self.assertEqual(len(outer), 7)
        self.assertEqual(sum(r["status"] == "eligible_outer_candidate" for r in outer), 1)
        self.assertTrue(any(r["status"] == "blocked_precision" for r in outer))
        self.assertTrue(any(r["status"] == "blocked_route" for r in outer))


class SupervisionFingerprintTests(unittest.TestCase):
    """留出案例必须在"监督了什么"上有差异，而不只是数值不同。"""

    def test_fingerprint_ignores_numeric_values(self):
        base = {"events": [
            {"id": "a", "action": "OP", "operation": "grind", "quality": "system"},
            {"id": "b", "action": "PARAM", "parameter": "D", "quality": "system",
             "labels": {
                 "features": {"value": ["diameter"], "quality": "system"},
                 "requirements": {"value": ["diameter:r0"], "quality": "system"},
                 "update": {"value": "derive", "quality": "system"},
                 "value": {"value": {"nominal": 10.0, "lower": 0.0, "upper": 0.1,
                                     "direct": None, "unit": "mm"}, "quality": "system"},
             }},
        ]}
        other = json.loads(json.dumps(base))
        other["events"][1]["labels"]["value"]["value"]["nominal"] = 99.9
        other["events"][0]["id"] = "z"
        self.assertEqual(supervision_fingerprint(base), supervision_fingerprint(other))

    def test_fingerprint_detects_different_bindings(self):
        base = {"events": [
            {"id": "a", "action": "OP", "operation": "grind", "quality": "system"},
            {"id": "b", "action": "PARAM", "parameter": "D", "quality": "system",
             "labels": {
                 "features": {"value": ["diameter"], "quality": "system"},
                 "requirements": {"value": ["diameter:r0"], "quality": "system"},
                 "update": {"value": "derive", "quality": "system"},
             }},
        ]}
        other = json.loads(json.dumps(base))
        other["events"][1]["labels"]["features"]["value"] = ["pocket_radius"]
        self.assertNotEqual(supervision_fingerprint(base), supervision_fingerprint(other))

    def test_current_batch_split_is_rejected_as_leaking(self):
        """当前 combined-batch-v1 的划分必须被新检查判定为泄漏。"""
        batch = ROOT / "chainmodel" / "data" / "processed" / "combined-batch-v1"
        manifest = read_json(batch / "manifest.json")
        by_split: dict[str, set[str]] = {}
        for case in manifest["cases"]:
            target = read_json(batch / case["target"])
            by_split.setdefault(supervision_fingerprint(target), set()).add(case["split"])
        leaking = {fp: sorted(s) for fp, s in by_split.items() if len(s) > 1}
        self.assertTrue(leaking,
                        "combined-batch-v1 is expected to share one supervision pattern "
                        "across splits; if this now passes, the dataset was rebuilt and "
                        "this test should be updated.")


if __name__ == "__main__":
    unittest.main()
