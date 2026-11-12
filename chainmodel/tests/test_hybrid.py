from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import torch

from farmcad.data.dataset import Case, Vocabulary
from farmcad.data.schema import VERSION
from farmcad.memory.store import MemoryStore, dump, model_records
from farmcad.models.local import LocalPredictor
from farmcad.models.model import ModelConfig
from farmcad.reasoning.backend import QwenPlaceholder
from farmcad.reasoning.runtime import HybridConfig, HybridSession, initialize, open_session
from farmcad.training.local_metrics import evaluate_local
from farmcad.training.losses import training_loss
from farmcad.training.trainer import load_checkpoint, train
from farmcad.utils import read_json, write_json
from tests.fixtures import example


def parameter(source: str = None, role: str = "machining_target") -> dict:
    return {
        "parameter": "D", "feature_ids": ["diameter"], "requirement_ids": ["diameter:r0"],
        "source_state_ids": [source] if source else [], "roles": [role],
        "update": "state_copy" if source else "design_copy",
    }


class HybridTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        torch.manual_seed(17)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.design, self.target = example()
        self.model_config = ModelConfig(hidden=16, graph_layers=1, dropout=0, text_bytes=32)
        vocabulary = Vocabulary.build([Case("example", "family", "train", self.design, self.target)])
        self.model = LocalPredictor(self.model_config, vocabulary).eval()
        self.path = self.root / "memory.sqlite"
        self.store = MemoryStore.create(self.path, self.design)
        self.store.set_meta("small_model", {"smoke": True, "status": "synthetic_untrained_test"})
        self.store.set_meta("perception", {})
        self.config = HybridConfig(model_path=str(self.root / "not-downloaded"))
        self.session = HybridSession(self.store, self.model, self.config)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def decision(self, action: str, payload: dict) -> dict:
        request = self.session.pending()
        return {**{k: request[k] for k in ("schema_version", "plan_id", "request_id", "revision")},
                "action": action, "payload": payload}

    def first_state(self) -> str:
        return self.store.commit_operation(0, "grind", [parameter()])["state_ids"][0]

    def test_memory_persists_exact_facts_not_only_vectors(self):
        identity = self.first_state()
        with MemoryStore(self.path) as restored:
            self.assertEqual(restored.record("state", identity)["value"], self.design["features"][0]["value"])
            self.assertEqual(restored.meta("revision"), 1)
            self.assertEqual(restored.meta("design"), self.design)
            self.assertFalse(restored.record("state", identity)["engineering_verified"])
        with self.assertRaises(FileExistsError):
            MemoryStore.create(self.path, self.design)

    def test_fact_store_import_does_not_require_neural_models(self):
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import farmcad.memory.store; assert 'torch' not in sys.modules"],
            env=dict(os.environ, PYTHONPATH=str(source)), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflicting_state_writes_rejected_without_partial_changes(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.store.commit_operation(0, "grind", [parameter(), parameter()])
        self.assertEqual(self.store.states(), [])
        self.assertEqual(self.store.meta("revision"), 0)

    def test_long_chain_repeated_names_and_observation_do_not_overwrite(self):
        first = self.first_state()
        latest = first
        for revision in range(1, 12):
            latest = self.store.commit_operation(revision, "grind", [parameter(first)])["state_ids"][0]
        observed = self.store.commit_operation(12, "inspect", [parameter(latest, "inspection_target")])["state_ids"][0]
        self.assertEqual(len(self.store.states("diameter")), 13)
        self.assertEqual(self.store.current_states("diameter"), {"D": latest})
        self.assertEqual(self.store.record("state", latest)["source_state_ids"], [first])
        self.assertEqual(self.store.record("state", observed)["kind"], "planned_observation")
        self.assertNotEqual(observed, latest)

    def test_invalid_batch_is_atomic_and_a_stale_revision_fails(self):
        first = self.first_state()
        invalid = parameter("missing-or-future")
        with self.assertRaisesRegex(ValueError, "Unknown state"):
            self.store.commit_operation(1, "inspect", [parameter(first), invalid])
        self.assertEqual(self.store.meta("revision"), 1)
        self.assertEqual(len(self.store.operations()), 1)
        self.assertEqual(len(self.store.states()), 1)
        with self.assertRaisesRegex(ValueError, "revision"):
            self.store.commit_operation(0, "grind", [])

    def test_copy_compatibility_and_requirements_are_checked(self):
        first = self.first_state()
        wrong = parameter(first)
        wrong["parameter"] = "Ra"
        with self.assertRaisesRegex(ValueError, "matching"):
            self.store.commit_operation(1, "inspect", [wrong])
        wrong = parameter()
        wrong["feature_ids"] = []
        with self.assertRaisesRegex(ValueError, "Requirement"):
            self.store.commit_operation(1, "grind", [wrong])
        wrong = parameter()
        wrong["value"] = {"nominal": 12345}
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.store.commit_operation(1, "grind", [wrong])

    def test_lookup_and_derive_cannot_fabricate_executed_values(self):
        for revision, update in enumerate(("lookup", "derive", "unknown", "external")):
            p = parameter()
            p["update"] = update
            identity = self.store.commit_operation(revision, "grind", [p])["state_ids"][0]
            record = self.store.record("state", identity)
            self.assertIsNone(record["value"]["nominal"])
            self.assertEqual(record["value"]["unit"], "unknown")
            self.assertEqual(record["value_status"], "requires_rule_execution_or_review")

    def test_close_is_not_engineering_success_and_freezes_branch(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.close_plan(0)
        self.first_state()
        report = self.store.close_plan(1)
        self.assertFalse(report["engineering_verified"])
        self.assertEqual(report["unverified_requirement_ids"], ["diameter:r0"])
        with self.assertRaisesRegex(ValueError, "closed"):
            self.store.commit_operation(2, "inspect", [])

    def test_independent_local_model_has_no_recurrent_route_decoder(self):
        self.assertFalse(any(isinstance(m, (torch.nn.GRU, torch.nn.GRUCell, torch.nn.LSTM)) for m in self.model.modules()))
        self.model.train()
        loss, report = training_loss(self.model, self.design, self.target)
        loss.backward()
        self.assertTrue(torch.isfinite(loss))
        self.assertNotIn("action", report["components"])
        self.assertNotIn("operation", report["components"])
        self.assertGreater(float(self.model.addressing.sources.query.weight.grad.abs().sum()), 0)

    def test_local_gold_history_has_no_future_leakage(self):
        altered = copy.deepcopy(self.target)
        altered["events"][4]["labels"]["value"]["value"]["nominal"] = 100
        _, before = self.model.teacher_forced(self.design, self.target)
        _, after = self.model.teacher_forced(self.design, altered)
        self.assertTrue(torch.equal(before[0][0].heads["features"], after[0][0].heads["features"]))
        self.assertEqual(before[0][0].source_ids, [])
        self.assertEqual(before[1][0].source_ids, ["s0"])
        first = self.first_state()
        records = model_records(self.store.states())
        encoded = self.model.encode(self.design)
        same = self.model.step(encoded, "grind", "D", records, 0)
        self.assertTrue(torch.all(same.heads["sources"][:, 3:] < -1e8))
        prior = self.model.predict(self.design, "inspect", "D", records, 1)
        self.assertEqual(prior["addresses"]["sources"]["candidate_ids"], [first])

    def test_local_overfit_learns_address_and_update(self):
        self.model.train()
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.01)
        initial = None
        for _ in range(100):
            optimizer.zero_grad()
            loss, _ = training_loss(self.model, self.design, self.target)
            initial = float(loss.item()) if initial is None else initial
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1)
            optimizer.step()
        self.assertLess(float(loss.item()), initial * 0.03)
        metrics = evaluate_local(self.model, self.design, self.target, {"gold"})
        for field in ("features", "requirements", "sources", "update"):
            self.assertEqual(metrics["fields"][field]["accuracy"], 1.0, field)

    def test_no_gold_local_metrics_are_null(self):
        target = copy.deepcopy(self.target)
        for event in target["events"]:
            event["quality"] = "system"
            for annotation in event.get("labels", {}).values():
                annotation["quality"] = "system"
        metrics = evaluate_local(self.model, self.design, target, {"gold"})
        self.assertTrue(all(row["accuracy"] is None for row in metrics["fields"].values()))

    def test_parameter_logits_vary_with_emitted_parameters(self):
        """同一工序内，参数 logits 必须随"已发出参数"变化。

        否则参数头被锁死：每道工序只能预测一个参数，准确率上限 = 工序数/参数数。
        """
        encoded = self.model.encode(self.design)
        op = self.target["events"][0]["operation"]
        empty = self.model.step(encoded, op, "D", [], 0)
        after_first = self.model.step(encoded, op, "D", [self._emitted("D", op, 0)], 0)
        self.assertFalse(
            torch.allclose(empty.heads["parameter"], after_first.heads["parameter"]),
            "parameter logits are constant within an operation: the parameter head is capped.")

    def _emitted(self, parameter: str, operation: str, operation_index: int) -> dict:
        return {
            "id": "emitted:0", "parameter": parameter, "operation": operation,
            "operation_index": operation_index,
            "labels": {
                "features": {"value": ["diameter"], "quality": "gold"},
                "requirements": {"value": ["diameter:r0"], "quality": "gold"},
                "sources": {"value": [], "quality": "unknown"},
                "roles": {"value": ["machining_target"], "quality": "gold"},
                "update": {"value": "design_copy", "quality": "gold"},
                "value": {"value": {"nominal": 10.0, "lower": -0.1, "upper": 0.1,
                                    "direct": None, "unit": "mm"}, "quality": "gold"},
            },
        }

    def test_self_predicted_mode_does_not_inject_gold_parameter(self):
        """self_predicted 口径下，查询用的参数来自模型自身预测，而非金标。"""
        forced = evaluate_local(self.model, self.design, self.target, {"gold"}, "teacher_forced")
        predicted = evaluate_local(self.model, self.design, self.target, {"gold"}, "self_predicted")
        self.assertEqual(forced["evaluation_mode"], "teacher_forced")
        self.assertEqual(predicted["evaluation_mode"], "self_predicted")
        for field in ("parameter", "features", "requirements", "update"):
            self.assertIn(field, forced["fields"])
            self.assertIn(field, predicted["fields"])

    def test_evaluate_local_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            evaluate_local(self.model, self.design, self.target, {"gold"}, "leaky")

    def test_placeholder_never_loads_or_pretends_to_reason(self):
        backend = QwenPlaceholder("user-label", str(self.root / "missing"))
        self.assertEqual(backend.request({}).status, "awaiting_llm")
        self.assertIsNone(backend.request({}).decision)
        self.assertFalse(backend.describe()["weights_loaded"])
        self.assertFalse((self.root / "missing").exists())
        self.session.append_request({"status": "test"})
        response = self.session.respond(self.decision("list", {"collection": "features", "offset": 0}))
        self.assertEqual(response["status"], "awaiting_llm")
        self.assertEqual(self.store.operations(), [])

    def test_query_predict_commit_and_finish_feedback_loop(self):
        packet = self.session.append_request({"status": "test"})
        self.assertNotIn("entities", packet)
        result = self.session.respond(self.decision("predict_local", {"operation": "grind", "parameter": "D", "feature_id": "diameter"}))
        self.assertEqual(result["request"]["tool_result"]["status"], "local_candidates_not_verified")
        self.session.respond(self.decision("commit_operation", {"operation": "grind", "parameters": [parameter()]}))
        first = self.store.states()[0]["id"]
        result = self.session.respond(self.decision("query_states", {"feature_id": "diameter", "offset": 0}))
        self.assertEqual(result["request"]["tool_result"]["items"][0]["id"], first)
        self.session.respond(self.decision("commit_operation", {"operation": "inspect", "parameters": [parameter(first, "inspection_target")]}))
        result = self.session.respond(self.decision("finish", {}))
        self.assertEqual(result["status"], "proposal_closed_pending_review")
        self.assertFalse(result["engineering_verified"])

    def test_bad_response_is_audited_and_can_be_corrected(self):
        self.session.append_request({"status": "test"})
        decision = self.decision("commit_operation", {"operation": "grind", "parameters": [parameter()]})
        bad = {**decision, "plan_id": "different-branch"}
        with self.assertRaisesRegex(ValueError, "plan_id"):
            self.session.respond(bad)
        self.assertEqual(self.store.operations(), [])
        self.assertEqual(self.session.pending()["request_id"], 0)
        self.session.respond(decision)
        with self.assertRaisesRegex(ValueError, "request_id"):
            self.session.respond(decision)
        self.assertEqual(len(self.store.operations()), 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM audit WHERE kind='rejected_decision'").fetchone()[0], 2)

    def test_bad_tool_field_types_are_explicitly_rejected(self):
        self.session.append_request({"status": "test"})
        with self.assertRaisesRegex(ValueError, "strings"):
            self.session.respond(self.decision("predict_local", {"operation": [], "parameter": "D", "feature_id": "diameter"}))
        self.assertEqual(self.store.operations(), [])

    def test_actual_context_budget_and_atomic_rollback(self):
        first = self.session.append_request({"status": "test"})
        self.assertLessEqual(len(dump(first)), self.config.max_context_chars)
        self.config.max_context_chars = 10
        with self.assertRaisesRegex(ValueError, "budget"):
            self.session.respond(self.decision("commit_operation", {"operation": "grind", "parameters": [parameter()]}))
        self.assertEqual(self.store.operations(), [])
        self.assertEqual(self.store.meta("revision"), 0)
        self.assertEqual(self.session.pending()["request_id"], 0)

    def test_step_budget_stops_incomplete_not_fake_eos(self):
        self.config.max_steps = 1
        self.session.append_request({"status": "test"})
        result = self.session.respond(self.decision("list", {"collection": "features", "offset": 0}))
        self.assertEqual(result["status"], "max_steps_pending_review")
        self.assertFalse(self.store.meta("closed"))
        with self.assertRaisesRegex(ValueError, "No pending"):
            self.session.pending()

    def test_checkpoint_resume_and_design_only_hybrid_initialization(self):
        write_json(self.root / "design.json", self.design)
        write_json(self.root / "target.json", self.target)
        manifest = self.root / "manifest.json"
        write_json(manifest, {"schema_version": VERSION, "cases": [{
            "case_id": "example", "family_id": "family", "split": "train",
            "design": "design.json", "target": "target.json",
        }]})
        config = self.root / "train.json"
        write_json(config, {"architecture": "local", "model": asdict(self.model_config),
                            "training": {"epochs": 2, "max_updates": 2, "num_threads": 1}})
        full, partial, resumed = [self.root / name for name in ("full", "partial", "resumed")]
        train(manifest, config, full, smoke=True)
        train(manifest, config, partial, smoke=True, max_updates=1)
        train(manifest, config, resumed, smoke=True, resume=partial / "checkpoints" / "last")
        a, _, _ = load_checkpoint(full / "checkpoints" / "last")
        b, _, _ = load_checkpoint(resumed / "checkpoints" / "last")
        self.assertIsInstance(a, LocalPredictor)
        for key, value in a.state_dict().items():
            self.assertTrue(torch.equal(value, b.state_dict()[key]), key)
        config_json = self.root / "hybrid.json"
        write_json(config_json, asdict(self.config))
        output = self.root / "hybrid"
        with self.assertRaisesRegex(ValueError, "explicit --smoke"):
            initialize(self.root / "design.json", full / "checkpoints" / "last", config_json, output)
        # No training target or manifest is available to inference.
        (self.root / "target.json").unlink()
        manifest.unlink()
        result = initialize(self.root / "design.json", full / "checkpoints" / "last", config_json, output, smoke=True)
        self.assertEqual(result["status"], "awaiting_llm")
        session = open_session(output)
        with session.store:
            self.assertEqual(session.store.states(), [])
            self.assertEqual(session.store.meta("design"), self.design)
            self.assertEqual(session.pending(), result["request"])
        self.assertTrue((output / "local_perception.json").is_file())
        self.assertFalse((self.root / "not-downloaded").exists())
