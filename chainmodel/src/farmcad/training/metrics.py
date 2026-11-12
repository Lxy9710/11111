from __future__ import annotations

from collections import defaultdict

from farmcad.data.schema import SPECIALS
from farmcad.models.model import FARMModel


def align_operations(predicted: list[str], reference: list[str]) -> tuple[int, dict[int, int]]:
    dp = [[0] * (len(reference) + 1) for _ in range(len(predicted) + 1)]
    for i in range(len(predicted) + 1):
        dp[i][0] = i
    for j in range(len(reference) + 1):
        dp[0][j] = j
    for i in range(1, len(predicted) + 1):
        for j in range(1, len(reference) + 1):
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1,
                           dp[i - 1][j - 1] + int(predicted[i - 1] != reference[j - 1]))
    i, j = len(predicted), len(reference)
    mapping = {}
    while i or j:
        if i and j and dp[i][j] == dp[i - 1][j - 1] + int(predicted[i - 1] != reference[j - 1]):
            if predicted[i - 1] == reference[j - 1]:
                mapping[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif i and dp[i][j] == dp[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return dp[-1][-1], mapping


def operation_records(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        if event["action"] == "OP":
            result.append({"event": event, "parameters": []})
        elif event["action"] == "PARAM":
            if not result:
                raise ValueError("Parameter precedes generated operation")
            result[-1]["parameters"].append(event)
    return result


def evaluate_case(model: FARMModel, design: dict, target: dict, qualities: set[str]) -> tuple[dict, dict]:
    # Only the design object reaches the decoder; alignment happens after free rollout.
    prediction = model.generate(design)
    pred_ops, ref_ops = operation_records(prediction["events"]), operation_records(target["events"])
    distance, op_mapping = align_operations(
        [o["event"]["operation"] for o in pred_ops], [o["event"]["operation"] for o in ref_ops],
    )
    state_mapping, paired = {}, {}
    for pred_index, ref_index in op_mapping.items():
        buckets = defaultdict(list)
        for state in ref_ops[ref_index]["parameters"]:
            buckets[state["parameter"]].append(state)
        for state in pred_ops[pred_index]["parameters"]:
            if buckets[state["parameter"]]:
                reference = buckets[state["parameter"]].pop(0)
                state_mapping[state["id"]] = reference["id"]
                paired[reference["id"]] = state
    counts = defaultdict(lambda: {"correct": 0, "total": 0})
    spans = {name: {"correct": 0, "total": 0} for name in ("1", "2-4", "5-9", "10+")}
    ref_positions = {p["id"]: i for i, op in enumerate(ref_ops) for p in op["parameters"]}
    numeric_errors = []
    for op_index, op in enumerate(ref_ops):
        for state in op["parameters"]:
            predicted = paired.get(state["id"])
            if state["quality"] in qualities:
                counts["parameter_recall"]["total"] += 1
                counts["parameter_recall"]["correct"] += int(predicted is not None)
            for name, field in (("features", "feature_ids"), ("requirements", "requirement_ids"),
                                ("sources", "source_state_ids"), ("roles", "roles"), ("update", "update")):
                annotation = state["labels"][name]
                if annotation["quality"] not in qualities or annotation["value"] is None:
                    continue
                expected = annotation["value"]
                actual = predicted[field] if predicted else None
                if name == "sources" and actual is not None:
                    actual = [v if v in SPECIALS else state_mapping.get(v, "UNMATCHED:" + v) for v in actual]
                correct = actual == expected if name == "update" else actual is not None and set(actual) == set(expected)
                counts[name]["total"] += 1
                counts[name]["correct"] += int(correct)
                if name == "sources":
                    distances = [op_index - ref_positions[v] for v in expected if v in ref_positions]
                    if distances:
                        gap = max(distances)
                        bucket = "1" if gap == 1 else "2-4" if gap <= 4 else "5-9" if gap <= 9 else "10+"
                        spans[bucket]["total"] += 1
                        spans[bucket]["correct"] += int(correct)
            value = state["labels"]["value"]
            if predicted and value["quality"] in qualities and value["value"] and value["value"]["unit"] != "unknown":
                if predicted["value"]["unit"] == value["value"]["unit"]:
                    for key in ("nominal", "lower", "upper", "direct"):
                        a, b = predicted["value"][key], value["value"][key]
                        if a is not None and b is not None:
                            numeric_errors.append(abs(a - b))
    has_route_labels = all(op["event"]["quality"] in qualities for op in ref_ops)
    scores = {name: {**value, "accuracy": value["correct"] / value["total"] if value["total"] else None}
              for name, value in counts.items()}
    return {
        "case_id": design["case_id"], "label_qualities": sorted(qualities),
        "predicted_operations": len(pred_ops), "reference_operations": len(ref_ops),
        "route_edit_distance": distance if has_route_labels else None,
        "route_normalized_edit_distance": distance / max(len(pred_ops), len(ref_ops), 1) if has_route_labels else None,
        "route_metric_status": "available" if has_route_labels else "reference_route_not_in_requested_quality",
        "parameter_precision": None,
        "parameter_precision_status": "not_reported_without_exhaustive_parameter_annotations",
        "matched_parameter_states": len(paired), "label_metrics": scores,
        "source_span_metrics": {
            name: {**value, "accuracy": value["correct"] / value["total"] if value["total"] else None}
            for name, value in spans.items()
        },
        "materialized_value_mae": sum(numeric_errors) / len(numeric_errors) if numeric_errors else None,
        "value_comparisons": len(numeric_errors),
        "structurally_complete": prediction["structurally_complete"],
        "termination": prediction["termination"], "limits_reached": prediction["limits_reached"],
        "evaluation_note": "Reference-aligned rollout; numeric proposals excluded. Weak labels are not independent engineering truth.",
    }, prediction
