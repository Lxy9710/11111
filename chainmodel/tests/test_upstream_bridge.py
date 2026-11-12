"""Tests for the upstream ProcessCAD frontend bridge.

The bridge is the only path by which an edit made in the web frontend reaches
the dataset as gold supervision. These tests use a synthetic upstream run that
mimics the platform's on-disk layout, so they do not depend on a live platform
installation.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from farmcad.data.annotation import export_worksheets, import_worksheets

REPO = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO / "scripts" / "bridge_upstream_edits.py"

_spec = importlib.util.spec_from_file_location("bridge_upstream_edits", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

PART = "C180506K1NW1R3U(H).56"


def write_case(root: Path) -> Path:
    case = root / "case"
    case.mkdir(parents=True)
    design = {
        "schema_version": "farmcad-t2-v1",
        "case_id": PART,
        "entities": [
            {"id": "E1", "type": "LINE", "numeric": [0.0] * 12, "text": "outer"},
            {"id": "E2", "type": "ARC", "numeric": [1.0] * 12, "text": "groove"},
        ],
        "edges": [[0, 0, 0], [1, 1, 0]],
        "features": [
            {"id": "Dc", "text": "outer diameter",
             "value": {"nominal": 52.6, "lower": 0.0, "upper": 0.15, "direct": None, "unit": "mm"},
             "anchor_entities": ["E1"]},
            {"id": "Bc", "text": "width",
             "value": {"nominal": 18.5, "lower": 0.0, "upper": 0.15, "direct": None, "unit": "mm"},
             "anchor_entities": ["E2"]},
        ],
        "requirements": [
            {"id": "Dc:requirement:0", "feature_id": "Dc", "text": "tolerance_keep",
             "lower": 52.6, "upper": 52.75, "evidence_entities": ["E1"]},
        ],
        "context": "material: 40Cr",
    }
    target = {
        "schema_version": "farmcad-t2-v1",
        "case_id": PART,
        "reviewer": "",
        "events": [
            {"id": f"{PART}:operation:105", "action": "OP", "quality": "system",
             "operation": "粗车第二平面及外径、倒角", "parameters_complete": True},
            {"id": f"{PART}:state:105:Dc", "action": "PARAM", "quality": "system",
             "parameter": "Dc", "labels": {
                 "features": {"value": ["Dc"], "quality": "system"},
                 "requirements": {"value": ["Dc:requirement:0"], "quality": "system"},
                 "sources": {"value": None, "quality": "system"},
                 "roles": {"value": ["machining_target"], "quality": "candidate"},
                 "update": {"value": "design_copy", "quality": "system"},
                 "value": {"value": {"nominal": 52.6, "lower": 0.0, "upper": 0.15,
                                     "direct": None, "unit": "mm"}, "quality": "system"},
             }},
            {"id": f"{PART}:state:105:Bc", "action": "PARAM", "quality": "system",
             "parameter": "Bc", "labels": {
                 "features": {"value": ["Bc"], "quality": "system"},
                 "requirements": {"value": None, "quality": "system"},
                 "sources": {"value": None, "quality": "system"},
                 "roles": {"value": ["machining_target"], "quality": "candidate"},
                 "update": {"value": "design_copy", "quality": "system"},
                 "value": {"value": {"nominal": 18.5, "lower": 0.0, "upper": 0.15,
                                     "direct": None, "unit": "mm"}, "quality": "system"},
             }},
            {"id": f"{PART}:operation:105:end", "action": "END_OP", "quality": "system"},
            {"id": f"{PART}:eos", "action": "EOS", "quality": "system"},
        ],
        "feature_members": {},
    }
    (case / "design.json").write_text(json.dumps(design), encoding="utf-8")
    (case / "target.json").write_text(json.dumps(target), encoding="utf-8")
    return case


def write_upstream(root: Path, edits: list[dict]) -> tuple[Path, Path]:
    """A synthetic upstream run: task snapshot plus working cards."""

    task = root / "upstream" / "output" / "_task_runtime" / "TASK1"
    task.mkdir(parents=True)
    (task / "task_snapshot.json").write_text(json.dumps({
        "payload": {
            "task_id": "TASK1",
            "result": {"manual_table_edits": {"applied_count": len(edits),
                                              "applied_edits": edits}},
        },
    }, ensure_ascii=False), encoding="utf-8")

    modifier = root / "upstream" / "temp" / "modifier" / "TASK1"
    modifier.mkdir(parents=True)
    card = modifier / f"{PART}_105_粗车第二平面及外径、倒角.json"
    card.write_text(json.dumps({
        "cage_patch": {
            "粗车第二平面及外径、倒角": {
                "parameters": {
                    "Dc": {"nominal": 52.6, "lower": 0.0, "upper": 0.15},
                    "Bc": {"nominal": 18.5, "lower": 0.0, "upper": 0.15},
                },
            },
        },
    }, ensure_ascii=False), encoding="utf-8")
    return task, modifier


class UpstreamBridgeTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.case = write_case(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_engineer_edit_becomes_a_value_row(self) -> None:
        task, modifier = write_upstream(self.root, [{
            "field_name": "Dc", "process_name": "粗车第二平面及外径、倒角",
            "old_text": "φ52.6", "new_text": "φ52.65",
            "table_id": "T1", "target_cell": "r02c03", "handle": "2F3",
        }])
        rows = bridge.build_rows(self.case, modifier, [task])
        edited = [r for r in rows if r["action"] == "value"]
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]["event_id"], f"{PART}:state:105:Dc")
        self.assertEqual(edited[0]["value_nominal"], 52.65)

    def test_untouched_card_values_stay_skipped(self) -> None:
        task, modifier = write_upstream(self.root, [{
            "field_name": "Dc", "process_name": "粗车第二平面及外径、倒角",
            "old_text": "φ52.6", "new_text": "φ52.65",
            "table_id": "T1", "target_cell": "r02c03", "handle": "2F3",
        }])
        rows = bridge.build_rows(self.case, modifier, [task])
        others = [r for r in rows if r["event_id"].endswith(":Bc")]
        self.assertEqual(len(others), 1)
        self.assertEqual(others[0]["action"], "skip")

    def test_no_engineer_edit_yields_no_promotions(self) -> None:
        task, modifier = write_upstream(self.root, [])
        rows = bridge.build_rows(self.case, modifier, [task])
        self.assertEqual([r for r in rows if r["action"] == "value"], [])

    def test_merge_does_not_override_a_worksheet_decision(self) -> None:
        sheets = self.root / "sheets"
        export_worksheets(self.case, sheets)
        params = sheets / "params.csv"
        with params.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row["action"] = "confirm" if row["event_id"].endswith(":Dc") else "skip"
        with params.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        task, modifier = write_upstream(self.root, [{
            "field_name": "Dc", "process_name": "粗车第二平面及外径、倒角",
            "old_text": "φ52.6", "new_text": "φ52.65",
            "table_id": "T1", "target_cell": "r02c03", "handle": "2F3",
        }])
        bridged = bridge.build_rows(self.case, modifier, [task])
        bridge.merge_into_worksheet(params, bridged)

        with params.open("r", encoding="utf-8-sig", newline="") as handle:
            after = {r["event_id"]: r for r in csv.DictReader(handle)}
        dc = after[f"{PART}:state:105:Dc"]
        # The engineer already confirmed the row, so the bridged value must not
        # overwrite the decision they made in the worksheet.
        self.assertEqual(dc["action"], "confirm")
        self.assertEqual(dc["value_nominal"], "52.6")

    def test_full_chain_produces_exactly_one_gold_value(self) -> None:
        sheets = self.root / "sheets"
        export_worksheets(self.case, sheets)
        task, modifier = write_upstream(self.root, [{
            "field_name": "Dc", "process_name": "粗车第二平面及外径、倒角",
            "old_text": "φ52.6", "new_text": "φ52.65",
            "table_id": "T1", "target_cell": "r02c03", "handle": "2F3",
        }])
        bridged = bridge.build_rows(self.case, modifier, [task])
        bridge.merge_into_worksheet(sheets / "params.csv", bridged)

        binding = json.loads((sheets / "binding.json").read_text(encoding="utf-8"))
        binding["reviewer"] = "李工"
        (sheets / "binding.json").write_text(
            json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")

        out = self.root / "out"
        report = import_worksheets(self.case, sheets, out, reviewer="李工")
        self.assertEqual(report["promoted_fields"], 1)
        target = json.loads((out / "target.json").read_text(encoding="utf-8"))
        events = {e["id"]: e for e in target["events"]}
        dc = events[f"{PART}:state:105:Dc"]
        self.assertEqual(dc["labels"]["value"]["quality"], "gold")
        self.assertEqual(dc["labels"]["value"]["value"]["nominal"], 52.65)
        bc = events[f"{PART}:state:105:Bc"]
        self.assertEqual(bc["labels"]["value"]["quality"], "system")


if __name__ == "__main__":
    unittest.main()
