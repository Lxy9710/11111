"""Check that every supervised head actually receives supervision.

A label that is present but sits at a quality with weight zero is not
supervision. The dataset can be full of correct labels that the loss never
sees, and the only way to know is to ask the loss itself.

This reports the supervised counts per head, split by quality, so it is
visible which heads are learning from which supervision level.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

from farmcad.training.losses import training_loss  # noqa: E402
from farmcad.training.trainer import build_model  # noqa: E402
from farmcad.data.dataset import Vocabulary, load_cases  # noqa: E402
from farmcad.models.model import ModelConfig  # noqa: E402


def main() -> None:
    dataset = Path(sys.argv[1] if len(sys.argv) > 1
                   else ROOT / "chainmodel" / "data" / "processed" / "combined-batch-v1")
    manifest = dataset / "manifest.json"
    cases = load_cases(manifest)
    vocabulary = Vocabulary.build(cases)
    model = build_model("gru", ModelConfig(), vocabulary)

    by_head = defaultdict(Counter)
    for case in cases:
        loss, report = training_loss(model, case.design, case.target)
        for key, value in report["supervised_counts"].items():
            by_head[key]["total"] += value

    print(json.dumps({k: dict(v) for k, v in sorted(by_head.items())},
                     ensure_ascii=False, indent=2))

    # The per-quality breakdown comes from the target itself, because the loss
    # report only counts the supervised items.
    quality = defaultdict(Counter)
    for case in cases:
        for event in case.target["events"]:
            if event["action"] != "PARAM":
                continue
            for field, label in event["labels"].items():
                quality[field][label["quality"]] += 1
    print()
    print("label quality per field:")
    print(json.dumps({k: dict(v) for k, v in sorted(quality.items())},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
