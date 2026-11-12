from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import torch

from farmcad.data.schema import VERSION
from farmcad.models.model import ModelConfig
from farmcad.training.trainer import TrainingConfig, device_for, load_checkpoint, train
from farmcad.utils import read_json, write_json
from tests.fixtures import example


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        design, target = example()
        write_json(self.root / "design.json", design)
        write_json(self.root / "target.json", target)
        self.manifest = self.root / "manifest.json"
        write_json(self.manifest, {"schema_version": VERSION, "cases": [{
            "case_id": "example", "family_id": "family", "split": "train",
            "design": "design.json", "target": "target.json",
        }]})
        self.config = self.root / "config.json"
        write_json(self.config, {
            "model": asdict(ModelConfig(hidden=16, graph_layers=1, dropout=0, text_bytes=32,
                                       max_events=24, max_operations=4, max_parameters=3)),
            "training": asdict(TrainingConfig(epochs=2, learning_rate=0.003, num_threads=1, max_updates=2)),
        })

    def tearDown(self):
        self.temporary.cleanup()

    def test_train_checkpoint_predict_and_refuse_overwrite(self):
        output = self.root / "run"
        report = train(self.manifest, self.config, output, smoke=True)
        self.assertEqual(report["new_updates"], 2)
        self.assertFalse(report["generalization_claim"])
        model, metadata, _ = load_checkpoint(output / "checkpoints" / "last")
        design = read_json(self.root / "design.json")
        first = model.generate(design)
        restored, _, _ = load_checkpoint(output / "checkpoints" / "last")
        self.assertEqual(first, restored.generate(design))
        self.assertEqual(metadata["task"], "T2")
        self.assertIsNone(report["best_validation_loss"])
        with self.assertRaises(FileExistsError):
            train(self.manifest, self.config, output, smoke=True)

    def test_resume_matches_uninterrupted_cpu_training(self):
        full = self.root / "full"
        partial = self.root / "partial"
        resumed = self.root / "resumed"
        train(self.manifest, self.config, full, smoke=True)
        train(self.manifest, self.config, partial, smoke=True, max_updates=1)
        train(self.manifest, self.config, resumed, smoke=True, resume=partial / "checkpoints" / "last")
        a, _, _ = load_checkpoint(full / "checkpoints" / "last")
        b, _, _ = load_checkpoint(resumed / "checkpoints" / "last")
        for name, value in a.state_dict().items():
            self.assertTrue(torch.equal(value, b.state_dict()[name]), name)

    def test_resume_rejects_changed_targets(self):
        partial = self.root / "partial"
        train(self.manifest, self.config, partial, smoke=True, max_updates=1)
        target = read_json(self.root / "target.json")
        target["events"][1]["labels"]["value"]["value"]["nominal"] = 11
        write_json(self.root / "target.json", target)
        with self.assertRaisesRegex(ValueError, "dataset"):
            train(self.manifest, self.config, self.root / "other", smoke=True,
                  resume=partial / "checkpoints" / "last")

    def test_checkpoint_hash_is_checked_before_loading(self):
        output = self.root / "run"
        train(self.manifest, self.config, output, smoke=True, max_updates=1)
        checkpoint = output / "checkpoints" / "last"
        with (checkpoint / "weights.pt").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_checkpoint(checkpoint)

    def test_real_validation_path_saves_best_and_rollout(self):
        design, target = example("validation")
        design["entities"][0]["numeric"][0] = 0.5
        write_json(self.root / "validation_design.json", design)
        write_json(self.root / "validation_target.json", target)
        manifest = read_json(self.manifest)
        manifest["cases"].append({
            "case_id": "validation", "family_id": "independent", "split": "validation",
            "design": "validation_design.json", "target": "validation_target.json",
        })
        write_json(self.manifest, manifest)
        output = self.root / "validated"
        report = train(self.manifest, self.config, output)
        self.assertIsNotNone(report["best_validation_loss"])
        self.assertTrue((output / "checkpoints" / "best" / "metadata.json").is_file())
        validation = read_json(output / "validation.json")
        self.assertEqual(len(validation["rollout_cases"]), 1)
        self.assertIn("termination", validation["rollout_cases"][0])

    def test_bad_config_and_unavailable_device_fail_explicitly(self):
        for config in ({"learning_rate": -1}, {"epochs": 0}, {"misspelled": 1}):
            with self.assertRaises(ValueError):
                TrainingConfig.from_dict(config)
        with self.assertRaises(ValueError):
            ModelConfig.from_dict({"pointer_slots": 0})
        if not torch.cuda.is_available():
            with self.assertRaises(RuntimeError):
                device_for("cuda")


if __name__ == "__main__":
    unittest.main()
