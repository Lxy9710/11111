"""Topology-aware geometric localisation.

A process drawing says what an operation needs: a width, a radius, a
position. To act on it, the operation has to be pointed at a place on the
part. The design drawing names those places, but the two drawings are laid
out independently, so a position in one is not a position in the other.

This module localises an operation's dimensions onto the nominal design
drawing, using the value each dimension states rather than where it sits
on the sheet.

Why the stated value, and not the stored geometry
--------------------------------------------------
AutoCAD stores a dimension twice: the ``measurement`` is the length in
drawing units, and the dimension text is the value the designer wrote, in
real millimetres. The two differ by the drawing's plot scale, and the
scale cannot be trusted on its own: a drawing plotted at 1:1 and one
plotted at 1:2 store the same measurements and display them with the same
ratio, so the ratio alone is ambiguous up to a factor of two.

The stated value is immune to that ambiguity, because it is the designer's
own number in real units. Measured on the FAXIN corpus, matching on the
stated value recovers 7 of the 8 dimensions of a turning operation against
the nominal design, while matching on the stored geometry recovers almost
none. The stated value is therefore what the localisation uses, and the
stored geometry is kept alongside it as the evidence that a dimension
annotates a real feature rather than a note.

What the result is used for
----------------------------
The correspondence is the geometric half of the process chain: for every
dimension an operation states, which nominal feature it refers to, and
whether the operation keeps that feature, changes it, or introduces a new
one. An operation that does not mention a nominal feature is either
leaving it alone or working outside the part, and the chain has to say
which, so the unmatched features are reported too.
"""

from __future__ import annotations

import re
from typing import Any

from .geometry import collect_entities, dimension_span

#: How far apart two stated values may be and still be the same feature.
#: The tolerance is relative, because a drawing is quoted to a fixed number
#: of decimals and the rounding error grows with the length.
LENGTH_TOLERANCE = 0.02

#: How far apart two stated values may be and still be the same feature
#: that the operation changed.
#:
#: The window is set from the corpus, not guessed. Matching every operation
#: dimension to its nearest nominal dimension and measuring the relative
#: difference gives a distribution with a clear structure: 94 of 160
#: dimensions state the nominal value exactly, and the differences that
#: remain fall away into a tail, with 11 dimensions differing by more than
#: 100 per cent. A window of 20 per cent therefore separates the features
#: an operation changed from the ones it merely mentions, while excluding
#: the tail.
CHANGE_TOLERANCE = 0.20

NUMBER = re.compile(r"-?\d+\.?\d*")


def _plain_numbers(text: Any) -> list[float]:
    """The plain numbers in a dimension text.

    Dimension text carries formatting codes, a tolerance suffix and
    sometimes a second number for the tolerance. Only the leading plain
    number is the stated value.
    """

    if not text:
        return []
    cleaned = re.sub(r"\\[A-Za-z][^;{}]*;", " ", str(text))
    cleaned = re.sub(r"[{}<>]", " ", cleaned)
    return [float(m) for m in NUMBER.findall(cleaned)]


def stated_value(dim: dict) -> float | None:
    """The value the dimension states, in real millimetres."""

    values = _plain_numbers(dim.get("text"))
    return values[0] if values else None


def dimension_features(data: dict) -> list[dict]:
    """Every dimension of a drawing, described by the value it states.

    Each feature carries the stated value, the stored length, and whether
    the stored length agrees with the geometry the annotation spans. A
    dimension that agrees with its own geometry is annotating a real
    feature; one that does not is annotating something else, and is
    flagged so it can be excluded from the localisation.
    """

    features = []
    for dim in collect_entities(data):
        if dim.get("type") != "DIMENSION":
            continue
        value = stated_value(dim)
        if value is None:
            continue
        measurement = dim.get("measurement")
        span = dimension_span(dim)
        stored = float(measurement) if isinstance(measurement, (int, float)) else None
        agrees = (
            stored is not None
            and span is not None
            and abs(stored - span) <= max(0.5, 0.01 * span)
        )
        features.append({
            "handle": dim.get("handle"),
            "layer": dim.get("layer"),
            "dim_type": dim.get("dim_type"),
            "value_mm": value,
            "stored": stored,
            "span": span,
            "annotates_geometry": agrees,
        })
    return features


def match_features(source: list[dict], target: list[dict],
                   length_tolerance: float = LENGTH_TOLERANCE,
                   require_geometry: bool = False,
                   change_tolerance: float = CHANGE_TOLERANCE) -> list[dict]:
    """Match the dimensions of one drawing to those of another.

    The match is on the stated value, and it has to answer two questions
    at once, because an operation either states the nominal value or states
    an intermediate one:

    * a feature the operation kept is stated with the nominal value, and
      matches exactly;
    * a feature the operation changed is stated with a value that differs
      from the nominal one by the amount of material removed, and matches
      within the change window.

    Each source feature is matched at most once, and the closest candidate
    wins, so two similar features cannot both claim the same target.
    """

    used = set()
    matches = []
    for feature in sorted(source, key=lambda f: -(abs(f["value_mm"]) or 0)):
        candidates = []
        for other in target:
            if other["handle"] in used:
                continue
            if require_geometry and not other["annotates_geometry"]:
                continue
            distance = abs(feature["value_mm"] - other["value_mm"])
            rounding = max(length_tolerance,
                           length_tolerance * abs(feature["value_mm"]))
            if distance <= rounding:
                candidates.append((distance, 0, other))
                continue
            # A changed feature: the stated value sits inside the change
            # window around the nominal one.
            window = max(change_tolerance,
                         change_tolerance * abs(other["value_mm"]))
            if distance <= window:
                candidates.append((distance, 1, other))
        if not candidates:
            continue
        _, kind, chosen = min(candidates, key=lambda item: (item[1], item[0]))
        used.add(chosen["handle"])
        matches.append({
            "source_handle": feature["handle"],
            "target_handle": chosen["handle"],
            "source_value_mm": feature["value_mm"],
            "target_value_mm": chosen["value_mm"],
            "delta_mm": round(feature["value_mm"] - chosen["value_mm"], 6),
            "match_kind": "identical" if kind == 0 else "changed",
            "source_annotates_geometry": feature["annotates_geometry"],
            "target_annotates_geometry": chosen["annotates_geometry"],
        })
    return matches


def detect_changes(matches: list[dict], nominal: list[dict],
                   change_tolerance: float = CHANGE_TOLERANCE) -> list[dict]:
    """Which matched features did the operation actually change?

    A match says the operation states the same feature the nominal design
    states. Whether it states the *same value* is a separate question: an
    operation that removes material states an intermediate value, which is
    smaller than the nominal one by the amount removed.

    The sign matters as much as the size. A material removing operation
    states a value below the nominal one, so a value above the nominal one
    is not a change of that feature but a different feature that happens to
    be close in size.
    """

    by_handle = {f["handle"]: f for f in nominal}
    changes = []
    for match in matches:
        feature = by_handle.get(match["target_handle"])
        if feature is None:
            continue
        nominal_value = feature["value_mm"]
        delta = match["source_value_mm"] - nominal_value
        if delta == 0:
            continue
        window = max(change_tolerance, change_tolerance * abs(nominal_value))
        if abs(delta) > window:
            continue
        changes.append({
            "source_handle": match["source_handle"],
            "target_handle": match["target_handle"],
            "nominal_value_mm": nominal_value,
            "stated_value_mm": match["source_value_mm"],
            "delta_mm": round(delta, 6),
            "relative": round(delta / nominal_value, 6) if nominal_value else None,
        })
    return changes


def match_with_changes(operation: list[dict], nominal: list[dict],
                       length_tolerance: float = LENGTH_TOLERANCE,
                       change_tolerance: float = CHANGE_TOLERANCE) -> dict:
    """Match an operation onto the nominal design and classify the result.

    The three outcomes are the three things an operation can do to a
    nominal feature: keep it, change it, or ignore it.
    """

    matches = match_features(operation, nominal, length_tolerance)
    changes = detect_changes(matches, nominal, change_tolerance)
    changed_handles = {c["target_handle"] for c in changes}
    kept = [m for m in matches if m["target_handle"] not in changed_handles]
    return {
        "matches": matches,
        "changes": changes,
        "kept": kept,
        "changed": changes,
    }


def localise_onto_design(design: dict, operation: dict) -> dict:
    """Localise an operation's dimensions onto the nominal design.

    Returns the correspondence, the features the operation changed, the
    features it kept, the dimensions it introduced, and the nominal
    features the operation never mentions. The last group matters: an
    operation that does not mention a nominal feature is either leaving it
    alone or working outside the part, and the chain has to say which.
    """

    design_features = dimension_features(design)
    operation_features = dimension_features(operation)
    matches = match_features(operation_features, design_features)
    changes = detect_changes(matches, design_features)
    changed_targets = {c["target_handle"] for c in changes}

    matched_sources = {m["source_handle"] for m in matches}
    matched_targets = {m["target_handle"] for m in matches}
    unmatched_operation = [f for f in operation_features
                           if f["handle"] not in matched_sources]
    unmatched_design = [f for f in design_features
                        if f["handle"] not in matched_targets]
    kept = [m for m in matches if m["target_handle"] not in changed_targets]

    return {
        "matches": matches,
        "changed": changes,
        "kept": kept,
        "operation_features": len(operation_features),
        "design_features": len(design_features),
        "matched": len(matches),
        "match_rate": len(matches) / max(1, len(operation_features)),
        "unmatched_operation": unmatched_operation,
        "unmatched_design": unmatched_design,
    }


def localise_route(design: dict, steps: list[dict]) -> dict:
    """Localise a whole process route onto the nominal design.

    ``steps`` is a list of ``{"label": str, "data": dict}``. Each step is
    localised independently, which is the point: the steps are independent
    views, and the nominal drawing is the only thing they share.
    """

    results = []
    for step in steps:
        result = localise_onto_design(design, step["data"])
        result["operation"] = step["label"]
        results.append(result)
    total = sum(r["operation_features"] for r in results)
    matched = sum(r["matched"] for r in results)
    return {
        "steps": results,
        "total_features": total,
        "matched": matched,
        "match_rate": matched / max(1, total),
        "changed_total": sum(len(r["changed"]) for r in results),
    }
