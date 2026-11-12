from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from farmcad.data.dataset import Case, Vocabulary, load_cases
from farmcad.data.prepare import adapt_design, apply_annotations
from farmcad.data.schema import VERSION, validate_design, validate_target
from farmcad.utils import object_hash, write_json
from tests.fixtures import example


class DataTests(unittest.TestCase):
    def test_valid_contract(self):
        design, target = example()
        validate_design(design)
        validate_target(design, target)

    def test_inference_input_rejects_target_and_nested_group_labels(self):
        design, target = example()
        for key, value in (("target", target), ("processes", []), ("has_lapping", True)):
            altered = copy.deepcopy(design)
            altered[key] = value
            with self.assertRaises(ValueError):
                validate_design(altered)
        altered = copy.deepcopy(design)
        altered["features"][0]["geometry_handles"] = ["L"]
        with self.assertRaises(ValueError):
            validate_design(altered)

    def test_nonfinite_input_rejected(self):
        design, _ = example()
        design["entities"][0]["numeric"][0] = float("nan")
        with self.assertRaises(ValueError):
            validate_design(design)

    def test_raw_group_labels_do_not_enter_input(self):
        raw = {"entities": [{"type": "LINE", "handle": "L", "start": [0, 0], "end": [1, 1], "group": ["secret"]}]}
        first = adapt_design("example", raw, [], [])
        raw["entities"][0]["group"] = ["another-answer"]
        raw["entities"][0]["GlobalID"] = 123
        self.assertEqual(first, adapt_design("example", raw, [], []))
        self.assertNotIn("secret", str(first))

    def test_source_must_be_previous_operation_not_current_or_future(self):
        design, target = example()
        target["events"][1]["labels"]["sources"]["value"] = ["s1"]
        with self.assertRaisesRegex(ValueError, "future"):
            validate_target(design, target)
        design, target = example()
        extra = copy.deepcopy(target["events"][1])
        extra["id"] = "same-op"
        extra["labels"]["sources"]["value"] = ["s0"]
        target["events"].insert(2, extra)
        with self.assertRaisesRegex(ValueError, "precede"):
            validate_target(design, target)

    def test_specials_do_not_mix_with_entities(self):
        design, target = example()
        target["events"][1]["labels"]["features"]["value"] = ["NONE", "diameter"]
        with self.assertRaises(ValueError):
            validate_target(design, target)

    def test_missing_eos_and_parameter_outside_operation(self):
        design, target = example()
        target["events"].pop()
        with self.assertRaises(ValueError):
            validate_target(design, target)
        design, target = example()
        target["events"].pop(0)
        with self.assertRaises(ValueError):
            validate_target(design, target)

    def test_annotations_change_targets_only_and_promote_only_edited_fields(self):
        design, target = example()
        before = object_hash(design)
        target["reviewer"] = ""
        target["feature_members"] = {}
        for event in target["events"]:
            event["quality"] = "system"
            for annotation in event.get("labels", {}).values():
                annotation["quality"] = "system"
        annotations = {
            "schema_version": VERSION, "case_id": design["case_id"], "reviewer": "test-only",
            "feature_members": {"diameter": ["L"]},
            "event_overrides": {"s1": {"sources": ["s0"]}},
        }
        updated = apply_annotations(design, target, annotations)
        self.assertEqual(object_hash(design), before)
        self.assertEqual(updated["events"][4]["labels"]["sources"]["quality"], "gold")
        self.assertEqual(updated["events"][4]["labels"]["features"]["quality"], "system")
        self.assertEqual(target["events"][4]["labels"]["sources"]["quality"], "system")
        self.assertEqual(updated["feature_members"]["diameter"]["quality"], "gold")

    def test_empty_reviewer_and_unknown_manual_fields_fail(self):
        design, target = example()
        annotations = {"schema_version": VERSION, "case_id": design["case_id"], "reviewer": "",
                       "feature_members": {"diameter": ["L"]}, "event_overrides": {}}
        with self.assertRaises(ValueError):
            apply_annotations(design, target, annotations)
        annotations.update(reviewer="test", feature_members={}, event_overrides={"s0": {"made_up": True}})
        with self.assertRaises(ValueError):
            apply_annotations(design, target, annotations)

    def test_manual_parameter_coverage_controls_end_supervision(self):
        design, target = example()
        annotations = {
            "schema_version": VERSION, "case_id": "example", "reviewer": "synthetic",
            "feature_members": {}, "event_overrides": {"op0": {"parameters_complete": False}},
        }
        updated = apply_annotations(design, target, annotations)
        self.assertEqual(updated["events"][2]["quality"], "unknown")
        annotations["event_overrides"]["op0"]["parameters_complete"] = True
        self.assertEqual(apply_annotations(design, updated, annotations)["events"][2]["quality"], "gold")

    def test_family_and_duplicate_design_split_leakage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rows = []
            for index, split in enumerate(("train", "validation")):
                design, target = example(str(index))
                write_json(root / f"d{index}.json", design)
                write_json(root / f"t{index}.json", target)
                rows.append({"case_id": str(index), "family_id": "same", "split": split,
                             "design": f"d{index}.json", "target": f"t{index}.json"})
            path = root / "manifest.json"
            write_json(path, {"schema_version": VERSION, "cases": rows})
            with self.assertRaisesRegex(ValueError, "Family leakage"):
                load_cases(path)
            rows[1]["family_id"] = "other"
            write_json(path, {"schema_version": VERSION, "cases": rows})
            with self.assertRaisesRegex(ValueError, "Identical design"):
                load_cases(path)

    def test_single_case_requires_explicit_smoke(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            design, target = example()
            write_json(root / "design.json", design)
            write_json(root / "target.json", target)
            path = root / "manifest.json"
            write_json(path, {"schema_version": VERSION, "cases": [{
                "case_id": "example", "family_id": "family", "split": "train",
                "design": "design.json", "target": "target.json",
            }]})
            with self.assertRaisesRegex(ValueError, "validation"):
                load_cases(path)
            self.assertEqual(len(load_cases(path, smoke=True)), 1)

    def test_vocabulary_uses_train_only(self):
        design, target = example()
        other = copy.deepcopy(target)
        other["events"][0]["operation"] = "unseen_operation"
        vocabulary = Vocabulary.build([
            Case("a", "a", "train", design, target), Case("b", "b", "test", design, other),
        ])
        self.assertNotIn("unseen_operation", vocabulary.op_index)
