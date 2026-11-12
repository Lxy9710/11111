from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from farmcad.data.dataset import load_cases
from farmcad.utils import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "processed" / "d207nq01" / "manifest.json"
CHECKPOINT = ROOT / "runs" / "smoke_d207nq01" / "checkpoints" / "last"


@unittest.skipUnless(MANIFEST.is_file(), "Prepare the local D207NQ pilot to run integration checks")
class PilotTests(unittest.TestCase):
    def test_real_route_and_parameter_events_preserved_without_fake_gold(self):
        case = load_cases(MANIFEST, smoke=True)[0]
        self.assertEqual(len(case.design["entities"]), 428)
        self.assertEqual(len(case.target["events"]), 169)
        self.assertEqual(sum(e["action"] == "OP" for e in case.target["events"]), 32)
        self.assertEqual(sum(e["action"] == "PARAM" for e in case.target["events"]), 104)
        self.assertEqual(case.target["feature_members"], {})
        self.assertEqual(case.target["reviewer"], "")
        self.assertNotIn("has_lapping", case.design)
        labels = [a for e in case.target["events"] for a in e.get("labels", {}).values()]
        self.assertFalse(any(a["quality"] == "gold" for a in labels))
        self.assertEqual(sum(e["action"] == "END_OP" and e["quality"] == "unknown" for e in case.target["events"]), 13)

    @unittest.skipUnless((CHECKPOINT / "metadata.json").is_file(), "Run the smoke experiment before CLI prediction checks")
    def test_standalone_input_predicts_without_target_or_manifest(self):
        case = load_cases(MANIFEST, smoke=True)[0]
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            write_json(directory / "only_design.json", case.design)
            result = subprocess.run([
                sys.executable, str(ROOT / "run.py"), "predict",
                "--checkpoint", str(CHECKPOINT),
                "--design", str(directory / "only_design.json"),
                "--output", str(directory / "prediction.json"), "--max-events", "8",
            ], cwd=str(directory), capture_output=True, text=True, encoding="utf-8",
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(result.returncode, 0, result.stderr)
            prediction = read_json(directory / "prediction.json")
            self.assertEqual(prediction["task"], "T2")
            self.assertFalse(prediction["engineering_verified"])
            self.assertLessEqual(len(prediction["events"]), 8)
            self.assertFalse((directory / "target.json").exists())
            self.assertFalse((directory / "manifest.json").exists())
