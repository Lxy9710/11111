"""Round-trip tests for the human annotation worksheets.

The contract under test: an engineer edits a worksheet outside this repository,
and the import must promote exactly the fields they confirmed, refuse anything
that does not match the bound inputs, and never silently invent supervision.
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from farmcad.data.annotation import (
    ACTION_ALL,
    ACTION_POINTER,
    ACTION_SKIP,
    ACTION_VALUE,
    AnnotationError,
    export_worksheets,
    import_worksheets,
)


def write_case(root: Path) -> Path:
    """A minimal but schema-valid two-operation case."""

    case = root / "case"
    case.mkdir(parents=True)
    design = {
        "schema_version": "farmcad-t2-v1",
        "case_id": "CASE1",
        "entities": [
            {"id": "E1", "type": "LINE", "numeric": [0.0] * 12, "text": "outer"},
            {"id": "E2", "type": "ARC", "numeric": [1.0] * 12, "text": "groove"},
        ],
        "edges": [[0, 0, 0], [1, 1, 0]],
        "features": [
            {"id": "Dc", "text": "outer diameter 40",
             "value": {"nominal": 40.0, "lower": 0.0, "upper": 0.05, "direct": None, "unit": "mm"},
             "anchor_entities": ["E1"]},
        ],
        "requirements": [
            {"id": "Dc:requirement:0", "feature_id": "Dc", "text": "tolerance_keep",
             "lower": 40.0, "upper": 40.05, "evidence_entities": ["E1"]},
        ],
        "context": "material: 40Cr",
    }
    target = {
        "schema_version": "farmcad-t2-v1",
        "case_id": "CASE1",
        "reviewer": "",
        "events": [
            {"id": "CASE1:operation:100", "action": "OP", "quality": "system",
             "operation": "grind", "parameters_complete": True},
            {"id": "CASE1:state:100:Dc", "action": "PARAM", "quality": "system",
             "parameter": "Dc", "labels": {
                 "features": {"value": ["Dc"], "quality": "system"},
                 "requirements": {"value": ["Dc:requirement:0"], "quality": "system"},
                 "sources": {"value": None, "quality": "system"},
                 "roles": {"value": ["machining_target"], "quality": "candidate"},
                 "update": {"value": "derive", "quality": "system"},
                 "value": {"value": {"nominal": 40.02, "lower": 0.0, "upper": 0.0,
                                     "direct": None, "unit": "mm"}, "quality": "system"},
             }},
            {"id": "CASE1:operation:100:end", "action": "END_OP", "quality": "system"},
            {"id": "CASE1:operation:110", "action": "OP", "quality": "system",
             "operation": "polish", "parameters_complete": True},
            {"id": "CASE1:state:110:Dc", "action": "PARAM", "quality": "system",
             "parameter": "Dc", "labels": {
                 "features": {"value": ["Dc"], "quality": "system"},
                 "requirements": {"value": None, "quality": "system"},
                 "sources": {"value": ["CASE1:state:100:Dc"], "quality": "system"},
                 "roles": {"value": ["reference_copy"], "quality": "system"},
                 "update": {"value": "state_copy", "quality": "system"},
                 "value": {"value": {"nominal": 40.02, "lower": 0.0, "upper": 0.0,
                                     "direct": None, "unit": "mm"}, "quality": "system"},
             }},
            {"id": "CASE1:operation:110:end", "action": "END_OP", "quality": "system"},
            {"id": "CASE1:eos", "action": "EOS", "quality": "system"},
        ],
        "feature_members": {},
    }
    (case / "design.json").write_text(json.dumps(design), encoding="utf-8")
    (case / "target.json").write_text(json.dumps(target), encoding="utf-8")
    return case


def read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class AnnotationRoundTripTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.case = write_case(self.root)
        self.sheets = self.root / "sheets"
        export_worksheets(self.case, self.sheets)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_export_writes_both_worksheets_and_binding(self) -> None:
        self.assertTrue((self.sheets / "params.csv").exists())
        self.assertTrue((self.sheets / "geometry.csv").exists())
        binding = json.loads((self.sheets / "binding.json").read_text(encoding="utf-8"))
        self.assertEqual(binding["case_id"], "CASE1")
        self.assertEqual(len(binding["design_sha256"]), 64)

    def test_export_lists_every_parameter_event(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        self.assertEqual([r["event_id"] for r in rows],
                         ["CASE1:state:100:Dc", "CASE1:state:110:Dc"])
        self.assertEqual(rows[0]["current_features"], "Dc")
        self.assertEqual(rows[0]["current_value"], "nominal=40.02;lower=0.0;upper=0.0;unit=mm")

    def test_skip_leaves_supervision_untouched(self) -> None:
        out = self.root / "out"
        report = import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        self.assertEqual(report["promoted_fields"], 0)
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        self.assertEqual(event["labels"]["value"]["quality"], "system")
        self.assertEqual(event["labels"]["roles"]["quality"], "candidate")

    def test_confirm_promotes_every_field_to_gold(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_ALL
        rows[0]["value_nominal"] = "40.03"
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        report = import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        # `sources` was generated as null, so the sheet cell is empty and the
        # engineer made no statement about it. It keeps its level.
        self.assertEqual(report["promoted_fields"], 5)
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        for name in ("features", "requirements", "roles", "update", "value"):
            self.assertEqual(event["labels"][name]["quality"], "gold", name)
        self.assertEqual(event["labels"]["value"]["value"]["nominal"], 40.03)
        self.assertEqual(event["labels"]["sources"]["quality"], "system")

    def test_value_action_promotes_only_the_value(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_VALUE
        rows[0]["value_nominal"] = "39.9"
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        report = import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        self.assertEqual(report["promoted_fields"], 1)
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        self.assertEqual(event["labels"]["value"]["quality"], "gold")
        self.assertEqual(event["labels"]["features"]["quality"], "system")
        self.assertEqual(event["labels"]["roles"]["quality"], "candidate")

    def test_pointer_action_promotes_labels_but_not_value(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["roles"] = "inspection_target"
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        self.assertEqual(event["labels"]["roles"]["quality"], "gold")
        self.assertEqual(event["labels"]["roles"]["value"], ["inspection_target"])
        self.assertEqual(event["labels"]["value"]["quality"], "system")

    def test_empty_pointer_cell_is_not_a_confirmation(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_ALL
        rows[0]["features"] = ""
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        self.assertEqual(event["labels"]["features"]["quality"], "system")

    def test_empty_pointer_list_means_confirmed_absent(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["requirements"] = "NONE"
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        event = target["events"][1]
        self.assertEqual(event["labels"]["requirements"]["quality"], "gold")
        self.assertEqual(event["labels"]["requirements"]["value"], ["NONE"])

    def test_stale_worksheet_is_refused(self) -> None:
        target = json.loads((self.case / "target.json").read_text(encoding="utf-8"))
        target["events"][1]["parameter"] = "Dchanged"
        (self.case / "target.json").write_text(json.dumps(target), encoding="utf-8")
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_unknown_event_id_is_refused(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["event_id"] = "CASE1:state:999:ghost"
        rows[0]["action"] = ACTION_ALL
        write_rows(self.sheets / "params.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_unknown_pointer_target_is_refused(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["features"] = "NoSuchFeature"
        write_rows(self.sheets / "params.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_future_source_is_refused(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["sources"] = "CASE1:state:110:Dc"
        write_rows(self.sheets / "params.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_special_pointer_cannot_mix_with_objects(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["features"] = "Dc;UNKNOWN"
        write_rows(self.sheets / "params.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_unknown_role_is_refused(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_POINTER
        rows[0]["roles"] = "invented_role"
        write_rows(self.sheets / "params.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_reviewer_is_required(self) -> None:
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="")

    def test_reviewer_mismatch_is_refused(self) -> None:
        binding = json.loads((self.sheets / "binding.json").read_text(encoding="utf-8"))
        binding["reviewer"] = "Someone"
        (self.sheets / "binding.json").write_text(json.dumps(binding), encoding="utf-8")
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Other")

    def test_source_case_directory_is_not_modified(self) -> None:
        before = (self.case / "target.json").read_text(encoding="utf-8")
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_ALL
        write_rows(self.sheets / "params.csv", rows)
        import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")
        self.assertEqual((self.case / "target.json").read_text(encoding="utf-8"), before)

    def test_geometry_sheet_promotes_feature_members(self) -> None:
        rows = read_rows(self.sheets / "geometry.csv")
        rows[0]["action"] = ACTION_ALL
        rows[0]["members"] = "E1;E2"
        write_rows(self.sheets / "geometry.csv", rows)
        out = self.root / "out"
        report = import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        self.assertEqual(report["gold_feature_members"], 1)
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        self.assertEqual(target["feature_members"]["Dc"]["quality"], "gold")
        self.assertEqual(target["feature_members"]["Dc"]["value"], ["E1", "E2"])

    def test_geometry_sheet_rejects_unknown_entity(self) -> None:
        rows = read_rows(self.sheets / "geometry.csv")
        rows[0]["action"] = ACTION_ALL
        rows[0]["members"] = "E9"
        write_rows(self.sheets / "geometry.csv", rows)
        with self.assertRaises(AnnotationError):
            import_worksheets(self.case, self.sheets, self.root / "out", reviewer="Eng")

    def test_review_log_records_every_promotion(self) -> None:
        rows = read_rows(self.sheets / "params.csv")
        rows[0]["action"] = ACTION_VALUE
        write_rows(self.sheets / "params.csv", rows)
        out = self.root / "out"
        import_worksheets(self.case, self.sheets, out, reviewer="Eng")
        log = json.loads((out / "review_log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["reviewer"], "Eng")
        self.assertEqual(log["promoted_count"], 1)
        self.assertEqual(log["promoted_fields"][0]["event_id"], "CASE1:state:100:Dc")


if __name__ == "__main__":
    unittest.main()
