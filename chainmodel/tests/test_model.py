from __future__ import annotations

import copy
import unittest

import torch

from farmcad.data.schema import SPECIALS
from farmcad.models.pointer import pointer_targets, select_pointers
from farmcad.training.losses import DEFAULT_QUALITY_WEIGHTS, training_loss
from farmcad.training.metrics import align_operations, evaluate_case
from tests.fixtures import example, tiny_model


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def setUp(self):
        torch.manual_seed(17)
        self.design, self.target = example()
        self.model = tiny_model(self.design, self.target)

    def test_all_heads_receive_finite_supervised_gradients(self):
        loss, report = training_loss(self.model, self.design, self.target)
        loss.backward()
        for name in ("features", "requirements", "sources", "roles", "update", "group", "unit"):
            self.assertGreater(report["supervised_counts"][name], 0)
        gradients = [p.grad for p in self.model.parameters() if p.grad is not None]
        self.assertTrue(gradients)
        self.assertTrue(all(torch.isfinite(g).all() for g in gradients))
        self.assertGreater(float(self.model.source_pointer.query.weight.grad.abs().sum()), 0)

    def test_future_target_changes_do_not_change_past_logits(self):
        self.model.eval()
        _, before = self.model.teacher_forced(self.design, self.target)
        other = copy.deepcopy(self.target)
        other["events"][4]["labels"]["value"]["value"]["nominal"] = 11
        _, after = self.model.teacher_forced(self.design, other)
        for i in range(5):
            for name in before[i][0].heads:
                self.assertTrue(torch.equal(before[i][0].heads[name], after[i][0].heads[name]), name)

    def test_source_memory_grows_and_masks_current_operation(self):
        encoded, trajectory = self.model.teacher_forced(self.design, self.target)
        self.assertEqual(trajectory[1][0].source_ids, [])
        self.assertEqual(trajectory[2][0].source_ids, ["s0"])
        self.assertFalse(trajectory[2][0].source_valid[0])
        self.assertTrue((trajectory[2][0].heads["sources"][:, len(SPECIALS)] < -1e8).all())
        self.assertTrue(trajectory[4][0].source_valid[0])
        self.assertEqual(encoded.feature_ids, ["diameter"])

    def test_unknown_labels_are_not_false_negative_supervision(self):
        target = copy.deepcopy(self.target)
        for event in target["events"]:
            if event["action"] == "PARAM":
                event["labels"]["features"] = {"value": None, "quality": "unknown"}
        _, report = training_loss(self.model, self.design, target)
        self.assertNotIn("features", report["supervised_counts"])
        weights = dict(DEFAULT_QUALITY_WEIGHTS, unknown=1)
        with self.assertRaises(ValueError):
            training_loss(self.model, self.design, target, quality_weights=weights)

    def test_candidate_weight_zero_masks_feature_pointer_loss(self):
        target = copy.deepcopy(self.target)
        for event in target["events"]:
            if event["action"] == "PARAM":
                event["labels"]["features"]["quality"] = "candidate"
        _, report = training_loss(self.model, self.design, target)
        self.assertNotIn("features", report["supervised_counts"])

    def test_empty_feature_and_requirement_candidate_sets(self):
        design, target = copy.deepcopy(self.design), copy.deepcopy(self.target)
        design["features"], design["requirements"], target["feature_members"] = [], [], {}
        for event in target["events"]:
            if event["action"] == "PARAM":
                for name in ("features", "requirements"):
                    event["labels"][name] = {"value": None, "quality": "unknown"}
        loss, _ = training_loss(self.model, design, target)
        self.assertTrue(torch.isfinite(loss))

    def test_limits_are_explicit_not_silently_truncated(self):
        self.model.config.max_events = 1
        with self.assertRaisesRegex(ValueError, "max_events"):
            self.model.teacher_forced(self.design, self.target)
        self.model.eval()
        result = self.model.generate(self.design)
        self.assertEqual(result["termination"], "max_events")
        self.assertFalse(result["structurally_complete"])
        self.assertEqual(result["events"][0]["action"], "OP")
        self.model.config.max_entities = 1
        with self.assertRaisesRegex(ValueError, "max_entities"):
            self.model.generate(self.design)

    def test_free_rollout_has_no_future_or_same_operation_sources(self):
        self.model.eval()
        with torch.no_grad():
            self.model.action_head.weight.zero_()
            self.model.action_head.bias.copy_(torch.tensor([4.0, 5.0, -5.0, -5.0]))
        self.model.config.max_parameters = 1
        result = self.model.generate(self.design)
        states, op = {}, -1
        for event in result["events"]:
            if event["action"] == "OP":
                op += 1
            elif event["action"] == "PARAM":
                for source in event["source_state_ids"]:
                    if source not in SPECIALS:
                        self.assertIn(source, states)
                        self.assertLess(states[source], op)
                states[event["id"]] = op
        self.assertGreater(len(states), 1)
        self.assertFalse(result["engineering_verified"])

    def test_inference_requires_eval_and_design_only(self):
        with self.assertRaises(RuntimeError):
            self.model.generate(self.design)
        self.model.eval()
        with self.assertRaises(ValueError):
            self.model.generate({**self.design, "target": self.target})

    def test_multi_pointer_limits_and_duplicate_suppression(self):
        with self.assertRaises(ValueError):
            pointer_targets(["a", "b"], ["a", "b"], slots=1)
        logits = torch.tensor([[0., -1., -1., 5., 2.], [0., -1., -1., 5., 4.], [5., 0., 0., 0., 0.]])
        self.assertEqual(select_pointers(logits, ["a", "b"]), ["a", "b"])

    def test_copy_resolution_preserves_missing_tolerance(self):
        design = copy.deepcopy(self.design)
        design["features"][0]["value"]["lower"] = None
        encoded = self.model.encode(design)
        state = self.model.initial_state(encoded)
        value, status = self.model.resolve_value(design, state, ["diameter"], [], "design_copy")
        self.assertIsNone(value["lower"])
        self.assertIn("copied_design", status)
        value, status = self.model.resolve_value(design, state, [], [], "derive")
        self.assertEqual(value["unit"], "unknown")
        self.assertEqual(status, "requires_rule_execution_or_review")

    def test_no_gold_reports_null_not_perfect_metrics(self):
        target = copy.deepcopy(self.target)
        for event in target["events"]:
            event["quality"] = "system"
            for annotation in event.get("labels", {}).values():
                annotation["quality"] = "system"
        self.model.eval()
        report, _ = evaluate_case(self.model, self.design, target, {"gold"})
        self.assertIsNone(report["route_normalized_edit_distance"])
        self.assertEqual(report["label_metrics"], {})
        self.assertIsNone(report["source_span_metrics"]["10+"]["accuracy"])

    def test_route_alignment_handles_repeated_operations(self):
        distance, mapping = align_operations(["a", "b", "a"], ["a", "x", "b", "a"])
        self.assertEqual(distance, 1)
        self.assertEqual(mapping, {2: 3, 1: 2, 0: 0})

    def test_tiny_overfit_learns_route_eos_and_source_pointer(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.01)
        first = None
        for _ in range(150):
            optimizer.zero_grad()
            loss, _ = training_loss(self.model, self.design, self.target)
            if first is None:
                first = float(loss.item())
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            optimizer.step()
        self.assertLess(float(loss.item()), first * 0.01)
        self.model.eval()
        prediction = self.model.generate(self.design)
        self.assertTrue(prediction["structurally_complete"])
        self.assertEqual([e["action"] for e in prediction["events"]],
                         ["OP", "PARAM", "END_OP", "OP", "PARAM", "END_OP", "EOS"])
        self.assertEqual([e["operation"] for e in prediction["events"] if e["action"] == "OP"],
                         ["grind", "inspect"])
        states = [e for e in prediction["events"] if e["action"] == "PARAM"]
        self.assertEqual(states[1]["source_state_ids"], [states[0]["id"]])
