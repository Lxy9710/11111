from __future__ import annotations

from collections import defaultdict
from typing import Union

import torch
from torch import Tensor
from torch.nn import functional as F

from farmcad.data.schema import ACTIONS, QUALITIES, ROLES, SPECIALS, UPDATES, VALUE_KEYS
from farmcad.models.model import FARMModel
from farmcad.models.local import LocalPredictor
from farmcad.models.pointer import pointer_targets
from farmcad.utils import finite, scaled


DEFAULT_QUALITY_WEIGHTS = {"gold": 1.0, "silver": 0.5, "system": 0.2, "candidate": 0.0, "unknown": 0.0}
DEFAULT_LOSS_WEIGHTS = {
    "action": 1.0, "operation": 1.0, "parameter": 1.0, "features": 1.0,
    "requirements": 1.0, "sources": 1.0, "roles": 0.5, "update": 0.5,
    "value": 0.1, "value_mask": 0.1, "unit": 0.1, "group": 1.0,
}


def validate_weights(qualities: dict, losses: dict) -> None:
    if set(qualities) != set(QUALITIES) or set(losses) != set(DEFAULT_LOSS_WEIGHTS):
        raise ValueError("Quality/loss weights must specify the documented keys exactly")
    if any(not finite(v) or v < 0 for v in list(qualities.values()) + list(losses.values())):
        raise ValueError("Weights must be finite and nonnegative")
    if qualities["unknown"] != 0 or qualities["gold"] <= 0:
        raise ValueError("Unknown supervision must be masked and gold weight positive")
    if not any(losses.values()):
        raise ValueError("At least one loss weight must be positive")


def training_loss(model: Union[FARMModel, LocalPredictor], design: dict, target: dict,
                  quality_weights: dict = None, loss_weights: dict = None) -> tuple[Tensor, dict]:
    quality_weights = DEFAULT_QUALITY_WEIGHTS if quality_weights is None else quality_weights
    loss_weights = DEFAULT_LOSS_WEIGHTS if loss_weights is None else loss_weights
    validate_weights(quality_weights, loss_weights)
    encoded, trajectory = model.teacher_forced(design, target)
    terms: dict[str, list[Tensor]] = defaultdict(list)

    def add(name: str, loss: Tensor, quality: str) -> None:
        weight = quality_weights[quality]
        if weight > 0:
            terms[name].append(loss * weight)

    for step, event in trajectory:
        h = step.heads
        target_index = lambda value: torch.tensor([value], dtype=torch.long, device=step.hidden.device)
        if isinstance(model, FARMModel):
            add("action", F.cross_entropy(h["action"].unsqueeze(0), target_index(ACTIONS.index(event["action"]))), event["quality"])
        if event["action"] == "OP":
            index = model.vocabulary.op_index.get(event["operation"], 1)
            if index >= 2:
                add("operation", F.cross_entropy(h["operation"].unsqueeze(0), target_index(index)), event["quality"])
        if event["action"] != "PARAM":
            continue
        index = model.vocabulary.param_index.get(event["parameter"], 1)
        if index >= 2:
            add("parameter", F.cross_entropy(h["parameter"].unsqueeze(0), target_index(index)), event["quality"])
        annotations = event["labels"]
        for name, ids in (("features", encoded.feature_ids), ("requirements", encoded.requirement_ids), ("sources", step.source_ids)):
            annotation = annotations[name]
            if annotation["value"] is None or quality_weights[annotation["quality"]] == 0:
                continue
            indices = pointer_targets(annotation["value"], ids, model.config.pointer_slots)
            count = len(annotation["value"])
            if count and annotation["value"][0] in SPECIALS:
                count = 0
            count = min(model.config.pointer_slots, count + 1)
            if any(h[name][slot, idx].item() <= -1e8 for slot, idx in enumerate(indices[:count])):
                raise ValueError("Supervised pointer points to a masked state")
            targets = torch.tensor(indices[:count], dtype=torch.long, device=h[name].device)
            add(name, F.cross_entropy(h[name][:count], targets), annotation["quality"])
        roles = annotations["roles"]
        if roles["value"] is not None:
            expected = h["roles"].new_tensor([float(role in roles["value"]) for role in ROLES])
            add("roles", F.binary_cross_entropy_with_logits(h["roles"], expected), roles["quality"])
        update = annotations["update"]
        if update["value"] is not None:
            add("update", F.cross_entropy(h["update"].unsqueeze(0), target_index(UPDATES.index(update["value"]))), update["quality"])
        value = annotations["value"]
        if value["value"] is not None and value["value"]["unit"] in ("mm", "um"):
            numbers = [value["value"][key] for key in VALUE_KEYS]
            mask = torch.tensor([number is not None for number in numbers], dtype=torch.bool, device=h["value"].device)
            if mask.any():
                expected = h["value"].new_tensor([scaled(number) if number is not None else 0.0 for number in numbers])
                add("value", F.smooth_l1_loss(h["value"][mask], expected[mask]), value["quality"])
            add("value_mask", F.binary_cross_entropy_with_logits(h["value_mask"], mask.float()), value["quality"])
            add("unit", F.cross_entropy(h["unit"].unsqueeze(0), target_index(("mm", "um").index(value["value"]["unit"]))), value["quality"])
    for fid, annotation in target["feature_members"].items():
        expected = encoded.membership_logits.new_tensor([float(e["id"] in annotation["value"]) for e in design["entities"]])
        logits = encoded.membership_logits[encoded.feature_ids.index(fid)]
        probabilities = logits.sigmoid()
        dice = 1 - (2 * (probabilities * expected).sum() + 1) / (probabilities.sum() + expected.sum() + 1)
        add("group", F.binary_cross_entropy_with_logits(logits, expected) + dice, annotation["quality"])
    if not terms:
        raise ValueError("Case has no usable supervised labels")
    means = {name: torch.stack(values).mean() for name, values in terms.items()}
    total = sum(means[name] * loss_weights[name] for name in means)
    if not isinstance(total, Tensor) or not torch.isfinite(total):
        raise ValueError("Nonfinite training loss")
    report = {
        "loss": float(total.detach().item()),
        "components": {name: float(value.detach().item()) for name, value in means.items()},
        "supervised_counts": {name: len(values) for name, values in terms.items()},
        "input_diagnostics": encoded.diagnostics,
    }
    return total, report
