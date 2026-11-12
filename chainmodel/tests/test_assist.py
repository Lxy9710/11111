"""Tests for the annotation assistance worksheet.

The assist worksheet is what the engineer actually reads, so its contract has
to hold: the editable columns must survive, the suggestion columns must be
populated, and the agreement flags must describe reality.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import torch

from farmcad.data.annotation import AnnotationError, import_worksheets
from farmcad.data.assist import (
    SUGGEST_COLUMNS,
    _agree,
    _format_scores,
    _format_slots,
    _priority,
    _scores_for_selection,
    _slot_reading,
    annotate_assist,
)
from farmcad.data.dataset import Vocabulary
from farmcad.models.local import LocalPredictor
from farmcad.models.model import ModelConfig
from farmcad.training.trainer import save_checkpoint


class AgreementTest(unittest.TestCase):

    def test_identical_sets_agree(self) -> None:
        self.assertTrue(_agree("Dc", "Dc"))
        self.assertTrue(_agree("Dc;Bc", "Bc;Dc"))

    def test_different_sets_disagree(self) -> None:
        self.assertFalse(_agree("Dc", "Bc"))
        self.assertFalse(_agree("Dc", "Dc;Bc"))

    def test_empty_against_empty_agrees(self) -> None:
        self.assertTrue(_agree("", ""))

    def test_empty_against_value_disagrees(self) -> None:
        self.assertFalse(_agree("", "Dc"))


class PriorityTest(unittest.TestCase):

    def test_full_agreement_is_low_priority(self) -> None:
        self.assertEqual(_priority(True, True, True, 0.9, False), "low")

    def test_confident_disagreement_is_high_priority(self) -> None:
        self.assertEqual(_priority(False, True, True, 0.85, False), "high")
        self.assertEqual(_priority(False, False, False, 0.9, False), "high")

    def test_uncertain_disagreement_is_medium_priority(self) -> None:
        self.assertEqual(_priority(False, True, True, 0.5, False), "medium")

    def test_an_empty_label_is_a_gap_not_a_conflict(self) -> None:
        # The dataset has no binding and the model proposes one. Nothing is
        # being contradicted, so this is cheaper to resolve than a conflict.
        self.assertEqual(_priority(False, True, True, 0.9, True), "gap")


class SlotReadingTest(unittest.TestCase):

    def test_each_slot_reports_its_own_winner(self) -> None:
        # Slot 0 prefers NONE, slot 1 prefers the candidate.
        logits = torch.tensor([[5.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 9.0]])
        readings = _slot_reading(logits, ["X"])
        self.assertEqual(readings[0]["choice"], "NONE")
        self.assertEqual(readings[1]["choice"], "X")
        self.assertGreater(readings[1]["probability"], readings[0]["probability"])

    def test_slot_reading_survives_a_short_candidate_list(self) -> None:
        logits = torch.tensor([[0.0, 0.0, 0.0]])
        readings = _slot_reading(logits, ["X"])
        self.assertEqual(readings[0]["choice"], "NONE")

    def test_formatted_slots_name_the_slot(self) -> None:
        readings = [{"slot": 0, "choice": "NONE", "probability": 0.9}]
        self.assertEqual(_format_slots(readings), "槽0=NONE(0.900)")


class ScoreFormatTest(unittest.TestCase):

    def test_scores_are_sorted_and_bounded(self) -> None:
        text = _format_scores({"a": 0.1, "b": 0.9, "c": 0.5}, limit=2)
        self.assertEqual(text, "b=0.900;c=0.500")

    def test_selection_scores_report_the_top_columns(self) -> None:
        # Column 0 is NONE, column 3 is the candidate "X". The candidate must
        # outrank NONE when the pointer is confident.
        logits = torch.tensor([[0.0, 0.0, 0.0, 10.0, 1.0]])
        scores = _scores_for_selection(logits, ["X"])
        self.assertIn("X", scores)
        self.assertIn("NONE", scores)
        self.assertGreater(scores["X"], scores["NONE"])

    def test_selection_scores_survive_a_short_candidate_list(self) -> None:
        # A slot with fewer columns than candidates must not raise.
        logits = torch.tensor([[0.0, 0.0, 0.0]])
        scores = _scores_for_selection(logits, ["X"])
        self.assertIn("NONE", scores)


class AssistWorksheetTest(unittest.TestCase):

    def _case(self, root: Path) -> Path:
        case = root / "case"
        case.mkdir()
        design = {
            "schema_version": "farmcad-t2-v1",
            "case_id": "TEST.01",
            "context": "材料牌号: GCr15",
            "entities": [
                {"id": "E1", "type": "LINE", "numeric": [0.0] * 12, "text": ""},
                {"id": "E2", "type": "ARC", "numeric": [0.5] * 12, "text": ""},
            ],
            "edges": [],
            "features": [
                {"id": "D", "text": "D Φ50", "value": {"nominal": 50.0, "lower": -0.01,
                                                       "upper": 0.0, "direct": None, "unit": "mm"},
                 "anchor_entities": ["E1"]},
            ],
            "requirements": [
                {"id": "D:requirement:0", "feature_id": "D", "text": "finished_dimension_range",
                 "lower": 49.99, "upper": 50.0, "evidence_entities": ["E1"]},
            ],
        }
        target = {
            "schema_version": "farmcad-t2-v1",
            "case_id": "TEST.01",
            "reviewer": "",
            "events": [
                {"id": "TEST.01:operation:10", "action": "OP", "quality": "system",
                 "operation": "粗磨外径", "parameters_complete": True},
                {"id": "TEST.01:state:10:D", "action": "PARAM", "quality": "system",
                 "parameter": "D",
                 "labels": {
                     "features": {"value": ["D"], "quality": "system"},
                     "requirements": {"value": ["D:requirement:0"], "quality": "system"},
                     "sources": {"value": None, "quality": "unknown"},
                     "roles": {"value": ["machining_target"], "quality": "system"},
                     "update": {"value": "design_copy", "quality": "system"},
                     "value": {"value": {"nominal": 50.0, "lower": -0.01, "upper": 0.0,
                                         "direct": None, "unit": "mm"}, "quality": "system"},
                 }},
                {"id": "TEST.01:operation:10:end", "action": "END_OP", "quality": "system"},
                {"id": "TEST.01:operation:20", "action": "OP", "quality": "system",
                 "operation": "终检", "parameters_complete": True},
                {"id": "TEST.01:state:20:D", "action": "PARAM", "quality": "system",
                 "parameter": "D",
                 "labels": {
                     "features": {"value": ["D"], "quality": "system"},
                     "requirements": {"value": ["D:requirement:0"], "quality": "system"},
                     "sources": {"value": None, "quality": "unknown"},
                     "roles": {"value": ["inspection_target"], "quality": "system"},
                     "update": {"value": "design_copy", "quality": "system"},
                     "value": {"value": {"nominal": 50.0, "lower": -0.01, "upper": 0.0,
                                         "direct": None, "unit": "mm"}, "quality": "system"},
                 }},
                {"id": "TEST.01:operation:20:end", "action": "END_OP", "quality": "system"},
                {"id": "TEST.01:eos", "action": "EOS", "quality": "system"},
            ],
            "feature_members": {},
        }
        (case / "design.json").write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
        (case / "target.json").write_text(json.dumps(target, ensure_ascii=False), encoding="utf-8")
        return case

    def _checkpoint(self, root: Path, case: Path) -> Path:
        design = json.loads((case / "design.json").read_text(encoding="utf-8"))
        target = json.loads((case / "target.json").read_text(encoding="utf-8"))
        config = ModelConfig(hidden=16, graph_layers=1, text_bytes=64)
        vocabulary = Vocabulary(["<PAD>", "<UNK>", "粗磨外径", "终检"],
                                 ["<PAD>", "<UNK>", "D"])
        model = LocalPredictor(config, vocabulary)
        optimizer = torch.optim.AdamW(model.parameters(), lr=0.001)
        path = root / "ckpt"
        save_checkpoint(path, model, optimizer, {"smoke": True})
        return path

    def test_assist_populates_suggestion_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self._case(root)
            checkpoint = self._checkpoint(root, case)
            out = root / "sheet"
            result = annotate_assist(case, checkpoint, out)
            self.assertEqual(result["rows"], 2)
            rows = list(csv.DictReader((out / "params.csv").open(encoding="utf-8-sig", newline="")))
            self.assertEqual(len(rows), 2)
            for column, _ in SUGGEST_COLUMNS:
                self.assertIn(column, rows[0])
            # A suggestion column may legitimately be empty: the pointer may
            # have selected NONE, and the first parameter of a case has no
            # prior record to point at. The score columns and the agreement
            # flags must always be filled, because they carry the model's
            # reasoning even when its answer is "nothing".
            for column in ("suggest_feature_slots", "suggest_requirement_slots",
                           "suggest_role_scores", "suggest_update_score",
                           "agree_features", "agree_roles", "agree_update",
                           "review_priority", "model_confidence"):
                self.assertNotEqual(rows[0][column], "", column)

    def test_assist_preserves_engineer_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self._case(root)
            checkpoint = self._checkpoint(root, case)
            out = root / "sheet"
            annotate_assist(case, checkpoint, out)
            path = out / "params.csv"
            rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
            rows[0]["action"] = "confirm"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            annotate_assist(case, checkpoint, out)
            again = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
            self.assertEqual(again[0]["action"], "confirm")

    def test_assist_rejects_a_route_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self._case(root)
            checkpoint = self._checkpoint(root, case)
            metadata = json.loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
            metadata["architecture"] = "gru"
            (checkpoint / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(AnnotationError):
                annotate_assist(case, checkpoint, root / "sheet")

    def test_assist_worksheet_still_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = self._case(root)
            checkpoint = self._checkpoint(root, case)
            out = root / "sheet"
            annotate_assist(case, checkpoint, out)
            path = out / "params.csv"
            rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
            rows[0]["action"] = "confirm"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            binding = out / "binding.json"
            data = json.loads(binding.read_text(encoding="utf-8"))
            data["reviewer"] = "王工"
            binding.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = import_worksheets(case, out, root / "gold", "王工")
            self.assertGreater(result["promoted_fields"], 0)


if __name__ == "__main__":
    unittest.main()
