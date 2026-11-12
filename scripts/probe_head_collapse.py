"""Probe a trained model for heads that collapsed to a constant.

A head can be trained, reach a low loss, and still have learned one constant
answer, if that was the only positive signal it ever saw. Nothing in the loss
reveals this: the loss goes down, the validation loss goes down, and a
teacher-forced accuracy of 1.0 is reported for a head that has learned to
ignore its input.

This is not hypothetical. The roles head of the first local checkpoint
predicted `reference_copy` for every operation, because every
`machining_target` label sat at the `candidate` quality, whose training weight
is zero. The model was correct about the data it was given and useless for the
question it was asked.

For each head this reports:

- the distribution of the ground truth it was trained on;
- the distribution of what the model actually predicts;
- the prediction entropy, and whether the model collapses onto one answer.

A head whose prediction distribution is much narrower than the label
distribution is collapsing, whatever the loss says.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

import torch  # noqa: E402

from farmcad.data.dataset import Vocabulary, load_cases  # noqa: E402
from farmcad.data.schema import ROLES, UPDATES  # noqa: E402
from farmcad.models.local import LocalPredictor  # noqa: E402
from farmcad.models.model import ModelConfig  # noqa: E402
from farmcad.models.pointer import select_pointers  # noqa: E402
from farmcad.utils import read_json  # noqa: E402


def _entropy(counts: Counter) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    import math
    return -sum((n / total) * math.log(n / total) for n in counts.values() if n)


def training_coverage(cases, field: str, classes) -> dict:
    """How much training signal each class actually received.

    A class whose labels all sit at a quality with weight zero never reaches
    the loss. The head then learns whatever the remaining classes teach, and
    the loss gives no hint that the class was never supervised.

    This reconstructs the coverage from the dataset and the loss's own quality
    weights, so it reports what the gradient saw, not what the labels say.
    """

    from farmcad.training.losses import DEFAULT_QUALITY_WEIGHTS

    counts: Counter = Counter()
    weighted: Counter = Counter()
    for case in cases:
        for event in case.target["events"]:
            if event["action"] != "PARAM":
                continue
            annotation = event["labels"][field]
            if not annotation["value"]:
                continue
            # A pointer or role field holds a list; an update field holds a
            # single operator name. Both are normalised to a list here.
            items = annotation["value"]
            if isinstance(items, str):
                items = [items]
            for item in items:
                counts[item] += 1
                if DEFAULT_QUALITY_WEIGHTS[annotation["quality"]] > 0:
                    weighted[item] += 1
    unsupervised = {c: counts.get(c, 0) - weighted.get(c, 0) for c in classes}
    return {
        "label_counts": {c: counts.get(c, 0) for c in classes},
        "supervised_counts": {c: weighted.get(c, 0) for c in classes},
        "never_supervised_counts": unsupervised,
        "classes_without_signal": [c for c in classes if weighted.get(c, 0) == 0],
    }


def probe_roles(model: LocalPredictor, cases) -> dict:
    """Compare the roles label distribution with the roles prediction.

    The roles head is multi-label: each role gets an independent score and the
    deployment rule keeps every role at or above the threshold. Reading it as
    an argmax would be wrong, because an independent sigmoid per role does not
    make the roles compete; the highest-scoring role is not "the" role.

    Two failure modes are distinguished:

    - **No signal**: the class never reached the loss, because all of its
      labels sat at a quality with weight zero.
    - **No discrimination**: the head received the signal but fires the class
      regardless of the operation.
    """

    label_counts: Counter = Counter()
    predicted_counts: Counter = Counter()
    per_operation: dict[str, Counter] = {}
    correct: Counter = Counter()
    total: Counter = Counter()
    predicted_when_absent: Counter = Counter()
    absent: Counter = Counter()
    exact = 0
    examples = 0

    for case in cases:
        encoded = model.encode(case.design)
        history: list[dict] = []
        operation, operation_index = "", -1
        for event in case.target["events"]:
            if event["action"] == "OP":
                operation, operation_index = event["operation"], operation_index + 1
                continue
            if event["action"] != "PARAM":
                continue
            result = model.step(encoded, operation, event["parameter"], history, operation_index)
            probabilities = result.heads["roles"].sigmoid().tolist()
            fired = [ROLES[i] for i, p in enumerate(probabilities)
                     if p >= model.config.role_threshold]
            truth = event["labels"]["roles"]["value"] or []
            for role in fired:
                predicted_counts[role] += 1
            per_operation.setdefault(operation, Counter()).update(fired)
            for role in ROLES:
                if role in truth:
                    total[role] += 1
                    if role in fired:
                        correct[role] += 1
                else:
                    absent[role] += 1
                    if role in fired:
                        predicted_when_absent[role] += 1
            for role in truth:
                label_counts[role] += 1
            if truth:
                examples += 1
                if set(fired) == set(truth):
                    exact += 1
            history.append({**event, "operation": operation, "operation_index": operation_index})

    per_class = {}
    for role in ROLES:
        hits = correct.get(role, 0)
        seen = total.get(role, 0)
        false_positive = predicted_when_absent.get(role, 0)
        absent_total = absent.get(role, 0)
        per_class[role] = {
            "recall": round(hits / seen, 4) if seen else None,
            "false_positive_rate": round(false_positive / absent_total, 4) if absent_total else None,
            "support": seen,
        }

    label_entropy = _entropy(label_counts)
    prediction_entropy = _entropy(predicted_counts)
    distinct_answers = len({tuple(c) for c in per_operation.values()})
    return {
        "reading": "thresholded multi-label, not argmax",
        "threshold": model.config.role_threshold,
        "exact_set_accuracy": round(exact / examples, 4) if examples else None,
        "label_distribution": dict(label_counts),
        "prediction_distribution": dict(predicted_counts),
        "label_entropy": round(label_entropy, 4),
        "prediction_entropy": round(prediction_entropy, 4),
        "entropy_ratio": round(prediction_entropy / label_entropy, 4) if label_entropy else None,
        "distinct_role_sets_across_operations": distinct_answers,
        "operations_probed": len(per_operation),
        "per_class": per_class,
        "per_operation": {k: dict(v) for k, v in sorted(per_operation.items())},
    }


def probe_update(model: LocalPredictor, cases) -> dict:
    label_counts: Counter = Counter()
    predicted_counts: Counter = Counter()
    for case in cases:
        encoded = model.encode(case.design)
        history: list[dict] = []
        operation, operation_index = "", -1
        for event in case.target["events"]:
            if event["action"] == "OP":
                operation, operation_index = event["operation"], operation_index + 1
                continue
            if event["action"] != "PARAM":
                continue
            result = model.step(encoded, operation, event["parameter"], history, operation_index)
            predicted_counts[UPDATES[int(result.heads["update"].argmax().item())]] += 1
            annotation = event["labels"]["update"]
            if annotation["value"]:
                label_counts[annotation["value"]] += 1
            history.append({**event, "operation": operation, "operation_index": operation_index})
    label_entropy = _entropy(label_counts)
    prediction_entropy = _entropy(predicted_counts)
    return {
        "label_distribution": dict(label_counts),
        "prediction_distribution": dict(predicted_counts),
        "label_entropy": round(label_entropy, 4),
        "prediction_entropy": round(prediction_entropy, 4),
        "entropy_ratio": round(prediction_entropy / label_entropy, 4) if label_entropy else None,
        "collapsed": (prediction_entropy < 0.2 * label_entropy) if label_entropy else None,
    }


def probe_features(model: LocalPredictor, cases) -> dict:
    """How often does the model point at a feature, and how often at nothing?"""

    label_counts: Counter = Counter()
    predicted_counts: Counter = Counter()
    for case in cases:
        encoded = model.encode(case.design)
        history: list[dict] = []
        operation, operation_index = "", -1
        for event in case.target["events"]:
            if event["action"] == "OP":
                operation, operation_index = event["operation"], operation_index + 1
                continue
            if event["action"] != "PARAM":
                continue
            result = model.step(encoded, operation, event["parameter"], history, operation_index)
            selected = select_pointers(result.heads["features"], encoded.feature_ids)
            predicted_counts[";".join(selected) if selected else "<none>"] += 1
            annotation = event["labels"]["features"]
            if annotation["value"]:
                label_counts[";".join(annotation["value"])] += 1
            history.append({**event, "operation": operation, "operation_index": operation_index})
    label_entropy = _entropy(label_counts)
    prediction_entropy = _entropy(predicted_counts)
    return {
        "distinct_label_bindings": len(label_counts),
        "distinct_predictions": len(predicted_counts),
        "label_entropy": round(label_entropy, 4),
        "prediction_entropy": round(prediction_entropy, 4),
        "entropy_ratio": round(prediction_entropy / label_entropy, 4) if label_entropy else None,
        "collapsed": (prediction_entropy < 0.2 * label_entropy) if label_entropy else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--dataset", type=Path,
                        default=ROOT / "chainmodel" / "data" / "processed" / "combined-batch-v1")
    parser.add_argument("--split", default="train",
                        choices=("train", "validation", "test", "all"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    metadata = read_json(args.checkpoint / "metadata.json")
    if metadata.get("architecture") != "local":
        raise SystemExit(f"Only a local checkpoint can be probed; got {metadata.get('architecture')!r}")
    config = ModelConfig.from_dict(metadata["model_config"])
    vocabulary = Vocabulary(**metadata["vocabulary"])
    model = LocalPredictor(config, vocabulary)
    payload = torch.load(args.checkpoint / "weights.pt", map_location="cpu", weights_only=True)
    model.load_state_dict(payload["model"])
    model.eval()

    cases = load_cases(args.dataset / "manifest.json")
    if args.split != "all":
        cases = [c for c in cases if c.split == args.split]

    report = {
        "checkpoint": str(args.checkpoint.resolve()),
        "weights_sha256": metadata.get("weights_sha256"),
        "split": args.split,
        "cases": len(cases),
        "training_coverage": {
            "roles": training_coverage(cases, "roles", ROLES),
            "update": training_coverage(cases, "update", UPDATES),
        },
        "roles": probe_roles(model, cases),
        "update": probe_update(model, cases),
        "features": probe_features(model, cases),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
