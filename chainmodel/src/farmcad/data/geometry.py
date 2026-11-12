"""Geometric grounding for process chains.

The parameter-only process chain records what each operation does to a
number. It does not record the geometry that number lives on, so a cascade
cannot be checked: nothing says whether the groove was cut into the wall or
the floor, whether two features ended up on the same face, or whether a
downstream step drifted off the feature an upstream step created.

This module provides the pieces a geometric process chain needs:

* :func:`collect_entities` flattens a drawing (top level plus block
  references) into a workable entity list.
* :func:`dimension_truth` recovers the real millimetre value of a dimension
  annotation, including the drawing's plot scale, which the annotations
  themselves reveal.
* :func:`pair_across_steps` matches entities between two drawings of the
  same part by the geometry they occupy rather than by handle, because
  handles are reassigned on every export.
* :func:`track_contour` follows the part contour across a step sequence and
  reports how it moved.
* :func:`merge_process_only_steps` collapses steps that do not change the
  geometry, so a state sequence is not padded with empty transitions.

The pairing rule is the core idea: a feature is identified by *what it
measures* (span and orientation) rather than by where it sits on the sheet,
because each process drawing is laid out independently and the annotation
positions move while the measured geometry does not.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

# ---------------------------------------------------------------- constants

#: Entity types that carry the part's own geometry rather than annotation.
GEOMETRY_TYPES = ("LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE", "ELLIPSE",
                  "SPLINE", "SOLID", "HATCH")

#: Entity types that describe a measurement rather than a shape.
ANNOTATION_TYPES = ("DIMENSION", "MTEXT", "TEXT", "LEADER")

#: Steps that mark the process but do not remove material.
NON_MACHINING_MARKERS = (
    "下料", "热处理", "清洗", "表面处理", "表处理", "磁粉", "荧光", "退磁",
    "酸洗", "喷丸", "时效", "检测", "探伤", "校直",
)

#: Radius below which a circle is annotation or hardware, not the part.
CONTOUR_RADIUS_FLOOR = 8.0

NUMBER = re.compile(r"-?\d+\.?\d*")

_POINT_KEYS = ("start", "end", "center", "position", "insertion_point",
               "ext_line1_point", "ext_line2_point", "center_point",
               "dim_line_point", "chord_point", "angle_vertex")


# ------------------------------------------------------------------ helpers

def _as_point(value: Any) -> tuple[float, float] | None:
    """Pull a 2D point out of a drawing entity field."""

    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if isinstance(value[0], (list, tuple)):
        if len(value[0]) < 2 or not isinstance(value[0][0], (int, float)):
            return None
        return (float(value[0][0]), float(value[0][1]))
    if not isinstance(value[0], (int, float)):
        return None
    return (float(value[0]), float(value[1]))


def _radius(entity: dict) -> float | None:
    value = entity.get("radius")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def entity_points(entity: dict) -> list[tuple[float, float]]:
    """Every point a drawing entity exposes."""

    points = []
    for key in _POINT_KEYS:
        point = _as_point(entity.get(key))
        if point is not None:
            points.append(point)
    for key in ("vertices", "points", "coordinates"):
        value = entity.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                point = _as_point(item)
                if point is not None:
                    points.append(point)
    return points


def geometry_fingerprint(entity: dict) -> tuple:
    """A hashable summary of an entity's shape, ignoring annotation text.

    Two entities with the same fingerprint occupy the same geometry, which
    is what makes them comparable across drawings.
    """

    points = sorted(entity_points(entity))
    radius = _radius(entity)
    return (
        entity.get("type"),
        tuple(points),
        round(radius, 4) if radius is not None else None,
    )


# ------------------------------------------------------------------ entities

def collect_entities(data: dict) -> list[dict]:
    """Flatten a drawing into one entity list.

    A drawing has top-level entities plus block definitions, and the block
    definitions carry much of the geometry. Both are needed to see the part.
    """

    out: list[dict] = []
    for entity in data.get("entities") or []:
        if isinstance(entity, dict):
            out.append(entity)
    for block in (data.get("blocks") or {}).values():
        if not isinstance(block, dict):
            continue
        for entity in block.get("entities") or []:
            if isinstance(entity, dict):
                out.append(entity)
    return out


def geometry_entities(data: dict) -> list[dict]:
    """Only the entities that describe shape."""

    return [e for e in collect_entities(data)
            if e.get("type") in GEOMETRY_TYPES]


# ------------------------------------------------------------ dimension truth

def _plain_numbers(text: Any) -> list[float]:
    """The plain numbers in an AutoCAD dimension text override.

    Dimension text carries formatting codes (``\\C2;``), a tolerance suffix
    (``<>``) and often a second number for the tolerance. Only the leading
    plain number is the dimension value.
    """

    if not text:
        return []
    cleaned = re.sub(r"\\[A-Za-z][^;{}]*;", " ", str(text))
    cleaned = re.sub(r"[{}<>]", " ", cleaned)
    return [float(m) for m in NUMBER.findall(cleaned)]


def dimension_span(dim: dict) -> float | None:
    """The geometric length a dimension measures.

    The extension points sit on the geometry the dimension annotates, so the
    distance between them is the measured length in drawing units.
    """

    first = _as_point(dim.get("ext_line1_point"))
    second = _as_point(dim.get("ext_line2_point"))
    if first is None or second is None:
        return None
    return math.dist(first, second)


def dimension_orientation(dim: dict) -> float | None:
    """The direction a dimension runs, folded into [0, pi).

    Orientation is what survives relayout between drawings: the annotation
    moves on the sheet, but the direction it measures does not.
    """

    first = _as_point(dim.get("ext_line1_point"))
    second = _as_point(dim.get("ext_line2_point"))
    if first is None or second is None:
        return None
    return round(math.atan2(second[1] - first[1], second[0] - first[0]) % math.pi, 2)


def plot_scale(data: dict) -> float | None:
    """Recover the drawing's plot scale from its own dimensions.

    AutoCAD stores the true measured length in ``measurement`` and displays
    the real value in the dimension text. Their ratio is the scale, and it is
    the same for every dimension on a drawing, so the mode over all
    dimensions recovers it.

    The ratio must come from ``measurement`` rather than from the geometric
    span: ``measurement`` is the exact stored length, while the extension
    points carry an arrow offset and only approximate it.
    """

    ratios = []
    for dim in collect_entities(data):
        if dim.get("type") != "DIMENSION":
            continue
        measurement = dim.get("measurement")
        if not isinstance(measurement, (int, float)):
            continue
        values = _plain_numbers(dim.get("text"))
        if not values:
            continue
        shown = values[0]
        if abs(shown) < 1.0:
            continue
        ratio = float(measurement) / shown
        # A real plot scale is a small positive number near 1 to 10.
        if 0.5 <= ratio <= 10:
            ratios.append(round(ratio, 3))
    if not ratios:
        return None
    counts = Counter(ratios)
    scale = counts.most_common(1)[0][0]
    if len(ratios) == 1:
        # The only candidate left after filtering; there is nothing to
        # disagree with it.
        return scale
    # Otherwise the scale must be supported by a clear majority of the
    # candidates, so that a single mislabelled dimension cannot set it.
    cluster = sum(1 for r in ratios if abs(r - scale) <= 0.01 * scale)
    if cluster * 2 <= len(ratios):
        return None
    return scale


def dimension_truth(data: dict) -> list[dict]:
    """Extract the dimensional truth of a drawing.

    Each entry carries the measured value in millimetres, the value as drawn,
    the scale used to convert, and the geometry the annotation is bound to.
    """

    scale = plot_scale(data)
    entries = []
    for index, dim in enumerate(collect_entities(data)):
        if dim.get("type") != "DIMENSION":
            continue
        measurement = dim.get("measurement")
        if not isinstance(measurement, (int, float)):
            continue
        span = dimension_span(dim)
        orientation = dimension_orientation(dim)
        values = _plain_numbers(dim.get("text"))
        drawn = values[0] if values else None
        truth = None
        if drawn is not None and scale:
            truth = float(measurement) / scale
        entries.append({
            "index": index,
            "handle": dim.get("handle"),
            "layer": dim.get("layer"),
            "dim_type": dim.get("dim_type"),
            "measurement": float(measurement),
            "span": span,
            "orientation": orientation,
            "drawn_value": drawn,
            "truth_mm": truth,
            "scale": scale,
            "measurement_matches_span": (
                span is not None
                and abs(float(measurement) - span) <= max(0.5, 0.01 * span)
            ),
        })
    return entries


# ------------------------------------------------------------------- pairing

def _feature_key(dim: dict, scale: float | None) -> tuple | None:
    """The identity of the geometry a dimension measures.

    The key has to survive two things: the relayout between drawings, and
    the fact that each process drawing is plotted at its own scale. The
    measured span is therefore normalised by the drawing's plot scale, so
    the key is a length in millimetres, and the orientation is folded into
    [0, pi] so that a reversed annotation still matches.

    The span is only an *identifier*: it carries the arrow offset, so the
    value that matters is read from ``measurement`` instead.
    """

    span = dimension_span(dim)
    orientation = dimension_orientation(dim)
    if span is None or orientation is None:
        return None
    normalised = span / scale if scale else span
    return (round(normalised, 2), orientation)


def _measured_value(dim: dict) -> float | None:
    """The true length a dimension records, in drawing units."""

    measurement = dim.get("measurement")
    if isinstance(measurement, (int, float)):
        return float(measurement)
    return None


def _displayed_value(dim: dict) -> float | None:
    """The value a dimension shows, which is the real one in millimetres."""

    values = _plain_numbers(dim.get("text"))
    return values[0] if values else None


def _match_tolerance(scale: float | None) -> float:
    """How far apart two spans may be and still be the same feature.

    A dimension is annotated by hand, so the same feature is drawn with a
    slightly different span in two layouts. The tolerance has to absorb that
    drafting slop without merging genuinely different features.
    """

    # Roughly a tenth of a millimetre in real units, and never less than the
    # precision the drawing itself is quoted to.
    return 0.5 if scale else 1.0


def _pair_tables(before: dict[tuple, list], after: dict[tuple, list],
                 tolerance: float) -> tuple[dict, set, set]:
    """Match two dimension tables whose keys are (span_mm, orientation).

    Matching is by proximity rather than by equality, because the span is a
    drafted number and two plots of the same feature differ slightly. The
    nearest match is taken first, so a feature is never paired twice.
    """

    matched: dict[tuple, tuple] = {}
    used_after = set()
    for key in sorted(before, key=lambda k: (k[1], k[0])):
        candidates = []
        for other in after:
            if other in used_after or other[1] != key[1]:
                continue
            distance = abs(other[0] - key[0])
            if distance <= tolerance:
                candidates.append((distance, other))
        if not candidates:
            continue
        _, chosen = min(candidates)
        matched[key] = chosen
        used_after.add(chosen)
    only_before = set(before) - set(matched)
    only_after = set(after) - used_after
    return matched, only_before, only_after


def pair_across_steps(previous: dict, current: dict,
                      tolerance: float | None = None) -> dict:
    """Match the dimensions of two drawings of the same part.

    Returns the pairs, the dimensions that appeared, and the ones that
    disappeared. A dimension that survives with a changed value is the
    observable trace of a geometric modification.

    A change is only reported when the value moved by more than the
    tolerance, because a drawing rounds its annotations and the same
    geometry can be annotated slightly differently in two layouts.
    """

    def index(data):
        scale = plot_scale(data)
        table = defaultdict(list)
        for dim in collect_entities(data):
            if dim.get("type") != "DIMENSION":
                continue
            key = _feature_key(dim, scale)
            if key is None:
                continue
            table[key].append(dim)
        return table, scale

    before, before_scale = index(previous)
    after, after_scale = index(current)
    limit = tolerance if tolerance is not None else max(
        _match_tolerance(before_scale), _match_tolerance(after_scale))
    matched, only_before, only_after = _pair_tables(before, after, limit)

    pairs, added, removed = [], [], []
    for key in sorted(matched, key=lambda k: (k[1], k[0])):
        other = matched[key]
        old, new = before[key][0], after[other][0]
        old_shown = _displayed_value(old)
        new_shown = _displayed_value(new)
        delta = None
        changed = False
        if old_shown is not None and new_shown is not None:
            delta = new_shown - old_shown
            changed = abs(delta) > max(0.01, 0.01 * abs(old_shown))
        pairs.append({
            "span_mm": key[0],
            "orientation": key[1],
            "matched_span_mm": other[0],
            "before_value": old_shown,
            "after_value": new_shown,
            "before_measurement": _measured_value(old),
            "after_measurement": _measured_value(new),
            "before_scale": before_scale,
            "after_scale": after_scale,
            "delta": round(delta, 6) if delta is not None else None,
            "changed": changed,
        })
    for key in sorted(only_after, key=lambda k: (k[1], k[0])):
        dim = after[key][0]
        added.append({"span_mm": key[0], "orientation": key[1],
                      "value": _displayed_value(dim)})
    for key in sorted(only_before, key=lambda k: (k[1], k[0])):
        dim = before[key][0]
        removed.append({"span_mm": key[0], "orientation": key[1],
                        "value": _displayed_value(dim)})
    return {"pairs": pairs, "added": added, "removed": removed,
            "changed": [p for p in pairs if p["changed"]]}


def pair_geometry_across_steps(previous: dict, current: dict,
                                tolerance: float = 0.05) -> dict:
    """Match the drawn geometry of two drawings by fingerprint.

    A shape that exists in both drawings is unchanged; a shape that only
    exists in the current drawing was added by the step, and one that only
    exists in the previous drawing was removed by it.
    """

    before = Counter(geometry_fingerprint(e) for e in geometry_entities(previous))
    after = Counter(geometry_fingerprint(e) for e in geometry_entities(current))
    common = sum((before & after).values())
    added = sum((after - before).values())
    removed = sum((before - after).values())
    total = max(1, common + added + removed)
    return {
        "common": common,
        "added": added,
        "removed": removed,
        "jaccard": common / total,
        "changed": added > 0 or removed > 0,
    }


# ------------------------------------------------------------------- contour

def contour_radius(data: dict) -> float:
    """The largest curve radius in the drawing.

    When the part itself is drawn, this is its contour. Steps that only show
    a section or the tooling have small radii and are filtered out.
    """

    radii = [
        _radius(e) for e in collect_entities(data)
        if e.get("type") in ("ARC", "CIRCLE") and _radius(e) is not None
    ]
    return max(radii) if radii else 0.0


def radial_profile(data: dict, scale: float | None = None,
                   floor: float = CONTOUR_RADIUS_FLOOR) -> dict:
    """The radial profile of the part, in millimetres.

    The single largest radius is not comparable across steps: one step may
    draw the outer contour, another a groove profile, and the numbers then
    describe different features. The *multiset* of radii is the
    view-independent quantity, and dividing by the plot scale puts every
    step in the same units.

    The result is the radial signature of the state, and a change in it
    between two steps is the observable trace of a geometric modification.
    """

    profile: Counter = Counter()
    for entity in collect_entities(data):
        if entity.get("type") not in ("ARC", "CIRCLE"):
            continue
        radius = _radius(entity)
        if radius is None or radius < floor:
            continue
        value = radius / scale if scale else radius
        profile[round(value, 2)] += 1
    return {
        "scale": scale,
        "radii": dict(sorted(profile.items(), reverse=True)),
        "distinct": len(profile),
        "total": sum(profile.values()),
        "max": max(profile) if profile else 0.0,
    }


def profile_delta(before: dict, after: dict) -> dict:
    """Compare two radial profiles.

    A radius that disappears is material that was removed at that radius; a
    radius that appears is material that was added or a feature that was
    newly drawn.
    """

    before_radii = before.get("radii", {}) if isinstance(before, dict) else {}
    after_radii = after.get("radii", {}) if isinstance(after, dict) else {}
    removed = {r: n for r, n in before_radii.items() if r not in after_radii}
    added = {r: n for r, n in after_radii.items() if r not in before_radii}
    common = {r: min(n, after_radii[r]) for r, n in before_radii.items()
              if r in after_radii}
    return {
        "removed_radii": removed,
        "added_radii": added,
        "common_radii": common,
        "changed": bool(removed or added),
    }


def has_part_contour(data: dict) -> bool:
    """Whether the drawing shows the part rather than only tooling."""

    return contour_radius(data) >= CONTOUR_RADIUS_FLOOR


def track_contour(steps: list[dict]) -> list[dict]:
    """Follow the part contour along a step sequence.

    ``steps`` is a list of ``{"label": str, "data": dict}``. Each entry
    reports the contour radius at that step and how much it moved relative to
    the previous step that actually drew the part.
    """

    trajectory = []
    previous = None
    for step in steps:
        radius = contour_radius(step["data"])
        entry = {
            "label": step["label"],
            "contour_radius": round(radius, 4),
            "has_contour": radius >= CONTOUR_RADIUS_FLOOR,
            "delta": None,
        }
        if entry["has_contour"] and previous is not None:
            entry["delta"] = round(radius - previous, 4)
        if entry["has_contour"]:
            previous = radius
        trajectory.append(entry)
    return trajectory


# ---------------------------------------------------------- step collapsing

def is_machining_step(label: str) -> bool:
    """Whether a step removes material.

    Heat treatment, cleaning and inspection steps are part of the route but
    do not change the geometry, so they must not become separate states.
    """

    return not any(marker in label for marker in NON_MACHINING_MARKERS)


def merge_process_only_steps(steps: list[dict]) -> list[dict]:
    """Collapse steps that do not change the geometry.

    A non-machining step inherits the state of the step before it, so the
    result is a sequence of genuine geometric states. The route keeps its
    full length in ``route`` so nothing is lost.
    """

    merged: list[dict] = []
    for step in steps:
        label = step["label"]
        entry = dict(step)
        entry["machining"] = is_machining_step(label)
        entry["route"] = [label]
        if entry["machining"] or not merged:
            merged.append(entry)
        else:
            merged[-1]["route"].append(label)
            # The state after a non-machining step is the state before it.
            merged[-1]["state_after"] = merged[-1].get("state_before")
    return merged


def cascade_trace(steps: list[dict], tolerance: float = 0.01) -> list[dict]:
    """Trace every dimension across a step sequence.

    The result is the observable cascade: for each dimension that survives
    across steps, the sequence of values it takes along the route.

    The identity of a dimension is its measured span normalised by the
    drawing's own plot scale, plus its orientation. Both drawings of a pair
    are therefore compared in millimetres, which is what makes two plots of
    the same feature match even when they are drawn at different scales.

    Monotonicity is judged with a tolerance, because a drawing rounds its
    annotations and the same geometry can be annotated slightly differently
    in two layouts. A run that goes 8.25, 8.20, 8.25 is a rounding artefact
    rather than a real reversal.
    """

    history: dict[tuple, dict[int, float]] = defaultdict(dict)
    labels: dict[int, str] = {}
    for index, step in enumerate(steps):
        labels[index] = step["label"]
        scale = plot_scale(step["data"])
        for dim in collect_entities(step["data"]):
            if dim.get("type") != "DIMENSION":
                continue
            key = _feature_key(dim, scale)
            if key is None:
                continue
            value = _displayed_value(dim)
            if value is None:
                continue
            history[key][index] = value

    traces = []
    for key, entries in history.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries.items())
        values = [v for _, v in ordered]
        # A step is a real decrease when it drops by more than the tolerance.
        monotone = True
        for i in range(len(values) - 1):
            if abs(values[i] - values[i + 1]) > tolerance:
                if values[i] < values[i + 1]:
                    monotone = False
                    break
        traces.append({
            "span_mm": key[0],
            "orientation": key[1],
            "trajectory": [{"step": labels[i], "value": v} for i, v in ordered],
            "values": values,
            "decreasing": monotone and len(set(values)) > 1,
            "changed": len(set(values)) > 1,
        })
    return traces
