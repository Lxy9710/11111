"""Annotation assistance: put the local model's predictions next to the labels.

An engineer reviewing a worksheet has to answer, for every parameter, three
questions: which feature does it belong to, what role does it play, and how is
it updated. The local model can answer those three questions with a score, but
only when it is asked the way it was trained: given a design, an operation, a
parameter, and the records that preceded it.

This module replays a case's target as that history and records the model's
answer for every parameter. The answers are written into the worksheet as
`suggest_*` columns, next to the `current_*` columns that hold the dataset's
existing label.

The suggestion is never applied automatically. The engineer still fills in the
editable columns; the suggestion exists so that the rows worth looking at are
obvious, and so that a disagreement between the model and the dataset is
visible instead of buried.

Numeric values are deliberately not suggested. A `lookup` or `derive` value
requires the rule engine to execute; the local model proposes numbers that
were never checked against the rules, and offering them as a suggestion would
invite an engineer to confirm a number nobody computed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from farmcad.data.annotation import (
    ACTION_SKIP,
    PARAM_SHEET_COLUMNS,
    AnnotationError,
    export_worksheets,
    write_worksheet,
)
from farmcad.data.schema import ROLES, UPDATES
from farmcad.models.pointer import select_pointers
from farmcad.utils import file_hash, write_json

# The suggestion columns are appended after the editable and current columns so
# that the engineer reads "what is it now / what does the model say" as one
# pair per field.
SUGGEST_COLUMNS = (
    ("suggest_features", "模型建议特征"),
    ("suggest_feature_slots", "模型特征分槽判定"),
    ("suggest_requirements", "模型建议要求"),
    ("suggest_requirement_slots", "模型要求分槽判定"),
    ("suggest_sources", "模型建议来源"),
    ("suggest_roles", "模型建议作用"),
    ("suggest_role_scores", "模型作用分数"),
    ("suggest_update", "模型建议算子"),
    ("suggest_update_score", "模型算子分数"),
    ("agree_features", "特征一致"),
    ("agree_roles", "作用一致"),
    ("agree_update", "算子一致"),
    ("review_priority", "复核优先级(gap/high/medium/low)"),
    ("model_confidence", "模型置信度(未校准)"),
)


def _scores_for_selection(logits, ids: list[str]) -> dict[str, float]:
    """The probability of each addressable id, keyed by id, per slot.

    A slot's probability distribution covers the three special columns plus the
    candidates. The specials are not addressable ids, so they are reported
    under their own name.

    A slot may hold fewer columns than the candidate list if the pointer was
    given an empty candidate set, so the index is clamped to the available
    columns instead of being assumed.
    """

    probabilities = logits.softmax(-1).tolist()
    specials = ["NONE", "UNKNOWN", "EXTERNAL"]
    out: dict[str, float] = {}
    for row in probabilities:
        order = sorted(range(len(row)), key=lambda i: row[i], reverse=True)
        for index in order[:3]:
            if index < len(specials):
                name = specials[index]
            else:
                candidate_index = index - len(specials)
                if candidate_index >= len(ids):
                    continue
                name = ids[candidate_index]
            if name not in out or row[index] > out[name]:
                out[name] = row[index]
    return out


def _slot_reading(logits, ids: list[str]) -> list[dict]:
    """What each pointer slot actually decided.

    The pointer has several slots, and each one picks independently. Merging
    the slots into one probability table hides the structure that matters:
    a slot that says `NONE` and a slot that says `outer_diameter` are
    different decisions, and the engineer needs to see which slot said what.

    Each entry reports the slot's own winner and the probability it gave it.
    """

    probabilities = logits.softmax(-1).tolist()
    specials = ["NONE", "UNKNOWN", "EXTERNAL"]
    readings = []
    for slot, row in enumerate(probabilities):
        index = max(range(len(row)), key=lambda i: row[i])
        if index < len(specials):
            name = specials[index]
        else:
            candidate_index = index - len(specials)
            if candidate_index >= len(ids):
                name = "<out-of-range>"
            else:
                name = ids[candidate_index]
        readings.append({"slot": slot, "choice": name, "probability": round(row[index], 4)})
    return readings


def _format_slots(readings: list[dict]) -> str:
    return ";".join(f"槽{r['slot']}={r['choice']}({r['probability']:.3f})" for r in readings)


def _format_scores(scores: dict[str, float], limit: int = 3) -> str:
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    return ";".join(f"{name}={value:.3f}" for name, value in ordered)


def _split_scores(text: str) -> set[str]:
    return {part.split("=")[0].strip() for part in text.split(";") if part.strip()}


def _agree(model_text: str, current_text: str) -> bool:
    """Compare two semicolon-separated id sets."""

    def norm(text: str) -> set[str]:
        return {item.strip() for item in text.split(";") if item.strip()}

    return norm(model_text) == norm(current_text)


def _priority(agree_features: bool, agree_roles: bool, agree_update: bool,
              role_confidence: float, label_is_empty: bool) -> str:
    """How much this row deserves the engineer's attention.

    Three kinds of row need different amounts of work.

    A **gap** is a row where the dataset has no feature binding and the model
    proposes one. The model is not contradicting anything; it is filling in a
    blank the rules could not fill. This is the highest-value row for the
    engineer, because it converts an unsupervised row into a supervised one.

    A **conflict** is a row where the dataset has a binding and the model
    proposes a different one. Either the label or the model is wrong, and both
    need to be checked. A confident conflict is the most expensive of all.

    A row the model and the dataset agree on is the cheapest kind of review.
    """

    disagreements = sum(not flag for flag in (agree_features, agree_roles, agree_update))
    if disagreements == 0:
        return "low"
    if label_is_empty:
        # The dataset is silent, so there is no contradiction to resolve.
        # The engineer's job is to judge a proposal, which is easier than
        # arbitrating a conflict.
        return "gap"
    if role_confidence >= 0.8:
        return "high"
    return "medium"


def replay_predictions(model, design: dict, target: dict) -> list[dict]:
    """Run the local model over every PARAM event of a case.

    The target's own event order supplies the history, exactly as during
    training, so the model sees the same kind of context it was trained on.
    """

    encoded = model.encode(design)
    history: list[dict] = []
    operation, operation_index = "", -1
    rows: list[dict] = []

    for event in target["events"]:
        if event["action"] == "OP":
            operation, operation_index = event["operation"], operation_index + 1
            continue
        if event["action"] != "PARAM":
            continue

        result = model.step(encoded, operation, event["parameter"], history, operation_index)
        heads = result.heads

        feature_ids = select_pointers(heads["features"], encoded.feature_ids)
        requirement_ids = select_pointers(heads["requirements"], encoded.requirement_ids)
        source_ids = select_pointers(heads["sources"], result.source_ids)

        role_probabilities = dict(zip(ROLES, heads["roles"].sigmoid().tolist()))
        threshold = model.config.role_threshold
        suggested_roles = [role for role in ROLES if role_probabilities[role] >= threshold]
        if not suggested_roles:
            suggested_roles = [max(role_probabilities, key=role_probabilities.get)]

        update_probabilities = dict(zip(UPDATES, heads["update"].softmax(-1).tolist()))
        suggested_update = max(update_probabilities, key=update_probabilities.get)

        rows.append({
            "event_id": event["id"],
            "operation": operation,
            "parameter": event["parameter"],
            "suggest_features": ";".join(feature_ids),
            "suggest_feature_slots": _format_slots(
                _slot_reading(heads["features"], encoded.feature_ids)),
            "suggest_requirements": ";".join(requirement_ids),
            "suggest_requirement_slots": _format_slots(
                _slot_reading(heads["requirements"], encoded.requirement_ids)),
            "suggest_sources": ";".join(source_ids),
            "suggest_roles": ";".join(suggested_roles),
            "suggest_role_scores": _format_scores(role_probabilities, limit=len(ROLES)),
            "suggest_update": suggested_update,
            "suggest_update_score": f"{update_probabilities[suggested_update]:.3f}",
            "role_confidence": f"{role_probabilities.get(suggested_roles[0], 0.0):.3f}",
        })
        history.append({**event, "operation": operation, "operation_index": operation_index})

    return rows


def annotate_assist(case_dir: Path, checkpoint: Path, output_dir: Path) -> dict:
    """Write a worksheet that carries the local model's suggestions.

    If `output_dir` already holds a worksheet, the suggestions are merged into
    it and the engineer's existing decisions are preserved. Otherwise a fresh
    worksheet is exported first.
    """

    import torch

    from farmcad.training.trainer import load_checkpoint

    design_path = case_dir / "design.json"
    target_path = case_dir / "target.json"
    if not design_path.exists() or not target_path.exists():
        raise AnnotationError(f"Case directory lacks design.json/target.json: {case_dir}")
    design = json.loads(design_path.read_text(encoding="utf-8-sig"))
    target = json.loads(target_path.read_text(encoding="utf-8-sig"))

    # The architecture is checked before the weights are loaded. A route
    # checkpoint does not answer per-parameter questions, and loading it would
    # fail with a weight-shape error that says nothing about the real problem.
    metadata_path = checkpoint / "metadata.json"
    if not metadata_path.exists():
        raise AnnotationError(f"Checkpoint lacks metadata.json: {checkpoint}")
    architecture = json.loads(metadata_path.read_text(encoding="utf-8-sig")).get("architecture")
    if architecture != "local":
        raise AnnotationError(
            f"Annotation assistance needs a local checkpoint (architecture=local); "
            f"this one is {architecture!r}.")

    torch.set_num_threads(1)
    model, metadata, _ = load_checkpoint(checkpoint, "cpu")
    from farmcad.models.local import LocalPredictor
    if not isinstance(model, LocalPredictor):
        raise AnnotationError(
            "Annotation assistance needs a local checkpoint (architecture=local); "
            "a route checkpoint does not answer per-parameter questions.")
    model.eval()

    if not (output_dir / "params.csv").exists():
        export_worksheets(case_dir, output_dir)

    suggestions = {row["event_id"]: row for row in replay_predictions(model, design, target)}

    import csv
    with (output_dir / "params.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AnnotationError(f"Empty worksheet: {output_dir / 'params.csv'}")

    headers = [(c, t) for c, t in PARAM_SHEET_COLUMNS] + [(c, t) for c, t in SUGGEST_COLUMNS]

    high = 0
    gaps = 0
    for row in rows:
        suggestion = suggestions.get(row["event_id"])
        if suggestion is None:
            continue
        agree_features = _agree(suggestion["suggest_features"], row.get("current_features", ""))
        agree_roles = _agree(suggestion["suggest_roles"], row.get("current_roles", ""))
        agree_update = _agree(suggestion["suggest_update"], row.get("current_update", ""))
        confidence = float(suggestion["role_confidence"])
        label_is_empty = not row.get("current_features", "").strip()
        priority = _priority(agree_features, agree_roles, agree_update, confidence, label_is_empty)
        if priority == "gap":
            gaps += 1
        elif priority == "high":
            high += 1
        row.update({
            "suggest_features": suggestion["suggest_features"],
            "suggest_feature_slots": suggestion["suggest_feature_slots"],
            "suggest_requirements": suggestion["suggest_requirements"],
            "suggest_requirement_slots": suggestion["suggest_requirement_slots"],
            "suggest_sources": suggestion["suggest_sources"],
            "suggest_roles": suggestion["suggest_roles"],
            "suggest_role_scores": suggestion["suggest_role_scores"],
            "suggest_update": suggestion["suggest_update"],
            "suggest_update_score": suggestion["suggest_update_score"],
            "agree_features": "是" if agree_features else "否",
            "agree_roles": "是" if agree_roles else "否",
            "agree_update": "是" if agree_update else "否",
            "review_priority": priority,
            "model_confidence": f"{confidence:.3f}",
        })

    write_worksheet(output_dir / "params.csv", rows, headers)

    binding_path = output_dir / "binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8-sig")) if binding_path.exists() else {}
    binding["assist"] = {
        "checkpoint": str(checkpoint.resolve()),
        "weights_sha256": metadata.get("weights_sha256"),
        "checkpoint_smoke": metadata.get("smoke"),
        "architecture": metadata.get("architecture"),
        "scores_calibrated": False,
        "numeric_values_not_suggested": (
            "Lookup and derive values require the rule engine; the local model "
            "proposes numbers that were never executed, so no value is suggested."),
    }
    binding.setdefault("design_sha256", file_hash(design_path))
    binding.setdefault("target_sha256", file_hash(target_path))
    write_json(binding_path, binding)

    return {
        "case_id": design["case_id"],
        "rows": len(rows),
        "gap_rows": gaps,
        "high_priority_rows": high,
        "checkpoint": str(checkpoint.resolve()),
        "output": str(output_dir.resolve()),
    }
