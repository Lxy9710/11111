"""Build the geometric state dataset: the chain that carries entities.

The parameter chain says what number each operation sets. This builds the
counterpart: for every operation of a part, the features it touches, the
entities that belong to them, the values it states, and whether it kept or
changed each one.

What the corpus makes possible
------------------------------
The extracted AutoCAD JSON carries a ``group`` field, and the group name
encodes a hierarchy ``serial_feature_level_label``. The *feature* number
is the identity of a feature **of the part**: it is the same number in
every process drawing of that part, whatever that drawing annotates, and
the numbers of a part are contiguous.

That gives the correspondence for free. The earlier approach matched
entities between two drawings by what they measured, and had to correct
for the fact that each drawing is laid out independently. The feature
number is exact, and it does not move.

The chain this module produces
-------------------------------
For every operation:

    states     the features the operation draws, with the geometry it
               draws them with
    values     the dimensions the operation states, with the value in mm
    kept       the features whose value the operation repeats
    changed    the features whose value the operation alters
    introduced the features the operation draws that no other step draws

The last group is what makes the chain usable for a modification: a
feature that appears only in one step is a feature that step created or
revealed, and the geometry of that feature, together with the features
around it, is what a modification has to stay consistent with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from farmcad.data.faithful import (
    extract_drawing,
    extract_entities,
    extract_features,
)
from farmcad.data.geometry import plot_scale

#: The name of an annotation group.
GROUP = re.compile(r"^(?P<serial>\d+)_(?P<feature>\d+)_(?P<level>\d+)_(?P<label>.*)$")

#: A value change smaller than this is rounding, not a geometric change.
#: A drawing is quoted to a fixed number of decimals, so the same feature
#: is annotated slightly differently in two layouts.
ROUNDING_TOLERANCE = 0.05

#: The name of the dataset.
SCHEMA_VERSION = "farmcad-geo-state-v1"


def _significant(before: float, after: float) -> bool:
    """Whether two values differ by more than the rounding tolerance."""

    return abs(after - before) > max(ROUNDING_TOLERANCE,
                                     0.001 * abs(before))


def _geometry_of(state: dict) -> dict:
    """The part of a feature's state that describes its geometry.

    The stated values are excluded, because the question here is whether
    the *shape* changed, not whether the dimension was restated.
    """

    geometry = state.get("geometry") or {}
    return {
        "lines": geometry.get("lines"),
        "arcs": geometry.get("arcs"),
        "circles": geometry.get("circles"),
        "radius_span": geometry.get("radius_span"),
        "length_span": geometry.get("length_span"),
    }


def _feature_states(data: dict) -> dict[int, dict]:
    """The features of a drawing, with the values their dimensions state."""

    entities = extract_entities(data)
    features = {f["id"]: dict(f) for f in extract_features(data)}

    for entity in entities:
        if entity.type != "DIMENSION":
            continue
        value = entity.stated_value
        if value is None:
            continue
        for group in entity.groups:
            feature = features.get(group.feature)
            if feature is None:
                continue
            feature.setdefault("stated", []).append({
                "handle": entity.handle,
                "value_mm": value,
                "measurement": entity.measurement,
                "kind": entity.dimension_kind,
                "level": group.level,
            })
    return features


def _feature_geometry(data: dict) -> dict[int, dict]:
    """The geometry each feature is drawn with.

    The geometry is summarised rather than kept in full: a feature is
    characterised by the shapes it is drawn with, so that two drawings
    can be compared without matching entity by entity.
    """

    entities = extract_entities(data)
    geometry: dict[int, dict] = {}
    for entity in entities:
        shape = entity.geometry
        for group in entity.groups:
            record = geometry.setdefault(group.feature, {
                "lines": 0, "arcs": 0, "circles": 0, "other": 0,
                "radii": [], "lengths": [], "bbox": None,
            })
            if shape.kind == "line":
                record["lines"] += 1
            elif shape.kind == "arc":
                record["arcs"] += 1
            elif shape.kind == "circle":
                record["circles"] += 1
            else:
                record["other"] += 1
            if shape.radius is not None:
                record["radii"].append(round(shape.radius, 3))
            if shape.length is not None:
                record["lengths"].append(round(shape.length, 3))
            if shape.bbox:
                box = shape.bbox
                if record["bbox"] is None:
                    record["bbox"] = list(box)
                else:
                    current = record["bbox"]
                    record["bbox"] = [
                        min(current[0], box[0]), min(current[1], box[1]),
                        max(current[2], box[2]), max(current[3], box[3]),
                    ]

    for record in geometry.values():
        record["radii"] = sorted(record["radii"])
        record["lengths"] = sorted(record["lengths"])
        if record["radii"]:
            record["radius_span"] = [record["radii"][0], record["radii"][-1]]
        else:
            record["radius_span"] = None
        if record["lengths"]:
            record["length_span"] = [record["lengths"][0], record["lengths"][-1]]
        else:
            record["length_span"] = None
    return geometry


def build_step(path: Path, part: str, label: str, index: int) -> dict:
    """The geometric state of one operation."""

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    features = _feature_states(data)
    geometry = _feature_geometry(data)
    scale = plot_scale(data)

    states = []
    for fid in sorted(features):
        feature = features[fid]
        record = geometry.get(fid, {})
        states.append({
            "feature": fid,
            "name": feature["name"],
            "labels": feature["labels"],
            "levels": feature["levels"],
            "entity_count": len(feature["entity_ids"]),
            "stated": feature.get("stated", []),
            "values_mm": sorted({round(s["value_mm"], 4)
                                 for s in feature.get("stated", [])}),
            "geometry": {
                "lines": record.get("lines", 0),
                "arcs": record.get("arcs", 0),
                "circles": record.get("circles", 0),
                "radius_span": record.get("radius_span"),
                "length_span": record.get("length_span"),
                "bbox": record.get("bbox"),
            },
        })

    return {
        "step": index,
        "operation": label,
        "source": path.name,
        "scale": scale,
        "features": states,
        "feature_ids": sorted(features),
    }


def build_case(part: str, files: list[tuple[Path, str]]) -> dict:
    """The geometric state chain of one part."""

    steps = [build_step(path, part, label, index)
             for index, (path, label) in enumerate(files)]

    # Which features the whole route touches, and which features only one
    # step draws. A feature that appears in a single step was created or
    # revealed by that step.
    occurrences: dict[int, list[int]] = {}
    for step in steps:
        for fid in step["feature_ids"]:
            occurrences.setdefault(fid, []).append(step["step"])

    for step in steps:
        for state in step["features"]:
            fid = state["feature"]
            seen = occurrences[fid]
            state["steps_seeing_it"] = seen
            state["introduced"] = len(seen) == 1
            state["persistent"] = len(seen) > 1

    # The chain between consecutive steps: what a step kept, what it
    # changed, and what it introduced.
    #
    # The comparison is against the last step that actually drew the
    # feature, not against the immediately preceding step. A heat
    # treatment or a cleaning step annotates nothing, and treating it as
    # the predecessor would make every feature look new again.
    last_drawn: dict[int, dict] = {}
    for index, step in enumerate(steps):
        previous = last_drawn
        current = {s["feature"]: s for s in step["features"]}
        step["kept"] = []
        step["changed"] = []
        step["geometry_changed"] = []
        step["redrawn"] = []
        for fid, state in current.items():
            before = previous.get(fid)
            if before is None:
                continue
            before_values = before["values_mm"]
            after_values = state["values_mm"]
            # A feature whose value did not move is kept, even if the
            # drawing rounded it differently. A feature whose value moved
            # is changed. There is no third case: a difference smaller
            # than the rounding tolerance is not a change, and a
            # difference larger than it is.
            if not any(_significant(b, a)
                       for b in before_values for a in after_values):
                step["kept"].append(fid)
            else:
                step["changed"].append(fid)

            # Whether the geometry itself changed, which is a different
            # question from whether the stated value changed: an
            # operation can redraw a feature without restating its
            # dimension, and it can restate a dimension without moving
            # the geometry.
            if _geometry_of(before) != _geometry_of(state):
                step["geometry_changed"].append(fid)
            if before["entity_count"] != state["entity_count"]:
                step["redrawn"].append(fid)

        for fid, state in current.items():
            last_drawn[fid] = state

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": part,
        "steps": steps,
        "feature_registry": {
            str(fid): {
                "steps": sorted(seen),
                "persistent": len(seen) > 1,
            }
            for fid, seen in sorted(occurrences.items())
        },
        "summary": {
            "steps": len(steps),
            "features": len(occurrences),
            "persistent_features": sum(1 for s in occurrences.values()
                                       if len(s) > 1),
            "step_introduced": sum(1 for step in steps
                                   for s in step["features"] if s["introduced"]),
            "changes": sum(len(step["changed"]) for step in steps),
            "geometry_changes": sum(len(step["geometry_changed"])
                                    for step in steps),
            "kept": sum(len(step["kept"]) for step in steps),
        },
    }


def check_chain(case: dict) -> list[dict]:
    """Check the chain for contradictions.

    Three contradictions are decidable from the chain itself.

    *A feature that changes in both directions.* A feature whose value
    goes down and then back up was changed twice, which no single pass of
    a route does. The rounding tolerance keeps a rounded value from
    counting as a reversal.

    *A feature whose geometry grows.* A material removing operation
    cannot make a feature bigger. When the drawn geometry of a feature
    grows and no operation between the two states added material, the
    chain contradicts itself.

    *A feature that is introduced twice.* A feature drawn only in one
    step is created by that step; a feature drawn only in one step in two
    different steps is two features that happen to share a number, which
    the corpus shows does happen, and the chain has to say so rather than
    treat it as one feature.
    """

    issues = []
    steps = case["steps"]

    # The state each feature was last drawn in, and the step that drew it.
    last_drawn: dict[int, dict] = {}
    last_step: dict[int, int] = {}

    for index, step in enumerate(steps):
        for fid in step["changed"]:
            before = last_drawn.get(fid)
            after = next((s for s in step["features"]
                          if s["feature"] == fid), None)
            if not before or not after:
                continue
            directions = []
            for a in before["values_mm"]:
                for b in after["values_mm"]:
                    if _significant(a, b):
                        directions.append(1 if b > a else -1)
            if not directions:
                continue
            direction = directions[0]
            # Look ahead: does a later step move it back?
            for later in steps[index + 1:]:
                state = next((s for s in later["features"]
                              if s["feature"] == fid), None)
                if not state:
                    continue
                back = any(direction * (1 if b > a else -1) < 0
                           for a in before["values_mm"]
                           for b in state["values_mm"]
                           if _significant(a, b))
                if back:
                    issues.append({
                        "kind": "direction_reversal",
                        "feature": fid,
                        "step": step["step"],
                        "operation": step["operation"],
                        "detail": f"特征 {fid} 在 {step['operation']} 之后反向变化",
                    })
                break

        for fid, state in {s["feature"]: s for s in step["features"]}.items():
            last_drawn[fid] = state
            last_step[fid] = index

    # A feature that is introduced by two different steps shares its
    # number with two features, and the chain has to report that.
    introduced_at: dict[int, list[int]] = {}
    for index, step in enumerate(steps):
        for state in step["features"]:
            if state["introduced"]:
                introduced_at.setdefault(state["feature"], []).append(index)
    for fid, where in introduced_at.items():
        if len(where) > 1:
            issues.append({
                "kind": "shared_feature_number",
                "feature": fid,
                "steps": where,
                "detail": f"特征号 {fid} 在工序 "
                          f"{[steps[i]['operation'] for i in where]} 中各自独立出现",
            })

    return issues
