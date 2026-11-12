from __future__ import annotations

import torch

from farmcad.data.schema import ROLES, SPECIALS, UPDATES, validate_target
from farmcad.models.local import LocalPredictor
from farmcad.models.pointer import select_pointers


FIELDS = ("parameter", "features", "requirements", "sources", "roles", "update")


def _empty_counts() -> dict:
    return {key: {"evaluated": 0, "exact": 0} for key in FIELDS}


def _score(counts: dict, predictions: dict, event: dict, qualities: set[str]) -> None:
    annotations = {**event["labels"], "parameter": {"value": event["parameter"], "quality": event["quality"]}}
    for key, prediction in predictions.items():
        annotation = annotations[key]
        if annotation["quality"] not in qualities or annotation["value"] is None:
            continue
        expected = annotation["value"]
        if isinstance(expected, list):
            expected = [] if expected == [SPECIALS[0]] else expected
            equal = set(prediction) == set(expected)
        else:
            equal = prediction == expected
        counts[key]["evaluated"] += 1
        counts[key]["exact"] += int(equal)


def _predictions(model: LocalPredictor, encoded, result, event: dict) -> dict:
    predictions = {
        "parameter": model.vocabulary.parameters[int(result.heads["parameter"].argmax().item())],
        "roles": [r for r, p in zip(ROLES, result.heads["roles"].sigmoid().tolist())
                  if p >= model.config.role_threshold],
        "update": UPDATES[int(result.heads["update"].argmax().item())],
    }
    for key, ids in (("features", encoded.feature_ids),
                     ("requirements", encoded.requirement_ids),
                     ("sources", result.source_ids)):
        predictions[key] = select_pointers(result.heads[key], ids)
    return predictions


def _summarise(case_id: str, counts: dict, qualities: set[str], mode: str) -> dict:
    return {
        "case_id": case_id,
        "scope": "known_operation_parameter_queries_and_gold_history_not_route_rollout",
        "label_qualities": sorted(qualities),
        "evaluation_mode": mode,
        "fields": {key: {**row, "accuracy": row["exact"] / row["evaluated"] if row["evaluated"] else None}
                   for key, row in counts.items()},
        "engineering_verified": False,
    }


@torch.no_grad()
def evaluate_local(model: LocalPredictor, design: dict, target: dict, qualities: set[str],
                   mode: str = "teacher_forced") -> dict:
    """评估局部预测头。

    mode:
      ``teacher_forced``  查询向量中注入金标参数。指标是**上界**，
                         因为金标参数参与了 query 构造，不能当作模型能力。
      ``self_predicted``  查询向量中注入**模型自己**预测的参数。
                         这才是部署时可用的口径。
    """
    model.eval()
    if mode not in ("teacher_forced", "self_predicted"):
        raise ValueError(f"Unknown evaluation mode: {mode}")

    validate_target(design, target)
    encoded = model.encode(design)

    counts = _empty_counts()
    history: list[dict] = []
    operation, operation_index = "", -1

    for event in target["events"]:
        if event["action"] == "OP":
            operation, operation_index = event["operation"], operation_index + 1
            continue
        if event["action"] != "PARAM":
            continue

        if mode == "teacher_forced":
            query_parameter = event["parameter"]
        else:
            # 用模型自己的预测驱动 query，模拟真实推理
            probe = model.step(encoded, operation, event["parameter"], history, operation_index)
            query_parameter = model.vocabulary.parameters[
                int(probe.heads["parameter"].argmax().item())]

        result = model.step(encoded, operation, query_parameter, history, operation_index)
        _score(counts, _predictions(model, encoded, result, event), event, qualities)
        history.append({**event, "operation": operation, "operation_index": operation_index})

    return _summarise(design["case_id"], counts, qualities, mode)
