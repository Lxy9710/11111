"""What the FAXIN corpus actually contains, measured without assumptions.

Every earlier claim about this corpus was checked against the files, but
several of those checks keyed the drawings by their *file name* instead of
by the part they belong to, so they compared a part against itself and
reported stability that was not there. This module does the measurement
once, correctly, and reports the numbers that the rest of the work is
built on.

The corpus
----------
Six parts, seventy-five process drawings. The earlier count of "75 parts"
was the number of drawings, not of parts.

What is measured
----------------
1. How many drawings, parts, entities and dimensions there are.
2. Whether the drawn geometry changes between two steps of one part.
3. Whether the annotation structure (``group``) is stable between steps.
4. What the ``group`` name actually encodes.

The answer to (2) and (3) decides the whole approach, so they are measured
rather than assumed.
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"

#: The name of a group is ``serial_feature_level_label``, where the serial
#: is the drawing-local ordinal of the group, the feature and the level are
#: numbers, and the label is the Chinese description.
GROUP = re.compile(r"^(?P<serial>\d+)_(?P<feature>\d+)_(?P<level>\d+)_(?P<label>.*)$")

#: The entity types that carry the shape of the part.
SHAPE_TYPES = ("LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE",
               "ELLIPSE", "SPLINE")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def all_entities(data: dict) -> list[dict]:
    """Every entity of a drawing, top level and block level."""

    out = [e for e in (data.get("entities") or []) if isinstance(e, dict)]
    for block in (data.get("blocks") or {}).values():
        if not isinstance(block, dict):
            continue
        out.extend(e for e in (block.get("entities") or [])
                   if isinstance(e, dict))
    return out


def shape_signature(entity: dict) -> tuple | None:
    """What an entity looks like, ignoring where it sits."""

    kind = entity.get("type")
    if kind not in SHAPE_TYPES:
        return None
    if kind == "LINE":
        start, end = entity.get("start"), entity.get("end")
        if not start or not end:
            return ("LINE",)
        length = math.dist(start[:2], end[:2])
        angle = math.atan2(end[1] - start[1], end[0] - start[0]) % math.pi
        return ("LINE", round(length, 1), round(angle, 2))
    radius = entity.get("radius")
    return (kind, round(radius, 1) if isinstance(radius, (int, float)) else None)


def position_signature(entity: dict) -> tuple | None:
    """Where an entity sits, exactly."""

    kind = entity.get("type")
    if kind not in SHAPE_TYPES:
        return None
    points = []
    for key in ("start", "end", "center"):
        value = entity.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 2 \
                and isinstance(value[0], (int, float)):
            points.append((round(float(value[0]), 3), round(float(value[1]), 3)))
    if not points:
        return None
    return (kind, tuple(sorted(points)))


def group_names(data: dict) -> set[str]:
    """The annotation groups of a drawing, without the default bucket."""

    names = set()
    for entity in data.get("entities") or []:
        for name in entity.get("group") or []:
            if name != "default":
                names.add(name)
    return names


def feature_labels(data: dict) -> set[str]:
    """The feature labels of a drawing, with the drawing-local serial removed.

    The serial is the ordinal of the group *within one drawing*, so it
    changes whenever the drawing annotates a different set of features.
    The label is the Chinese description of the feature and is the part of
    the name that can carry across drawings.
    """

    labels = set()
    for name in group_names(data):
        match = GROUP.match(name)
        if match:
            labels.add(match.group("label"))
    return labels


def shape_multiset(data: dict) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for entity in all_entities(data):
        signature = shape_signature(entity)
        if signature:
            counter[signature] += 1
    return counter


def position_multiset(data: dict) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for entity in all_entities(data):
        signature = position_signature(entity)
        if signature:
            counter[signature] += 1
    return counter


def label_multiset(data: dict) -> collections.Counter:
    """The shape of every entity, per feature label."""

    counter: collections.Counter = collections.Counter()
    for entity in data.get("entities") or []:
        names = [n for n in (entity.get("group") or []) if n != "default"]
        signature = shape_signature(entity)
        if not signature:
            continue
        for name in names:
            match = GROUP.match(name)
            label = match.group("label") if match else name
            counter[(label, signature)] += 1
    return counter


def part_drawings() -> dict[str, list[Path]]:
    """The drawings of each part, in the order the route runs."""

    result: dict[str, list[Path]] = {}
    for folder in sorted(p for p in FAXIN.iterdir() if p.is_dir()):
        files = list(folder.glob("*.json"))
        # The route order is the trailing number of the file name.
        def order(path: Path) -> tuple[int, str]:
            match = re.search(r"(\d+)$", path.stem)
            return (int(match.group(1)) if match else 9999, path.stem)

        result[folder.name] = sorted(files, key=order)
    return result


def measure() -> dict:
    """The measured facts about the corpus."""

    drawings = part_drawings()
    parts = {}

    for part, files in drawings.items():
        loaded = [load(path) for path in files]
        shapes = [shape_multiset(data) for data in loaded]
        positions = [position_multiset(data) for data in loaded]
        labels = [label_multiset(data) for data in loaded]
        names = [group_names(data) for data in loaded]

        # The geometry is unchanged between two steps when the multiset of
        # shapes is the same. Comparing against the *first* step is enough
        # because equality is transitive.
        geometry_constant = all(c == shapes[0] for c in shapes)
        position_constant = all(c == positions[0] for c in positions)

        # The feature labels that survive every step of the route.
        common_labels = set.intersection(*[feature_labels(d) for d in loaded])
        union_labels = set.union(*[feature_labels(d) for d in loaded])

        # For a feature label that is present in several steps, is the
        # geometry drawn under it the same every time?
        per_label_stable = 0
        per_label_total = 0
        for label in common_labels:
            multisets = [c for c, l in zip(labels, feature_labels(loaded))
                         if label in l]
            per_label_total += 1
            if all(c == multisets[0] for c in multisets):
                per_label_stable += 1

        parts[part] = {
            "steps": len(files),
            "entities": sum(len(all_entities(d)) for d in loaded),
            "dimensions": sum(
                1 for d in loaded for e in all_entities(d)
                if e.get("type") == "DIMENSION"),
            "shape_constant_across_steps": geometry_constant,
            "position_constant_across_steps": position_constant,
            "group_names_constant": all(n == names[0] for n in names),
            "feature_labels_common": len(common_labels),
            "feature_labels_union": len(union_labels),
            "labels_with_stable_geometry": per_label_stable,
            "labels_measured": per_label_total,
        }

    designs = sorted(DESIGN.glob("*.json"))
    return {
        "parts": parts,
        "totals": {
            "parts": len(parts),
            "drawings": sum(p["steps"] for p in parts.values()),
            "design_drawings": len(designs),
            "entities": sum(p["entities"] for p in parts.values()),
            "dimensions": sum(p["dimensions"] for p in parts.values()),
        },
    }


def report() -> None:
    facts = measure()
    totals = facts["totals"]
    print(f"零件 {totals['parts']}  工序图 {totals['drawings']}  "
          f"名义设计图 {totals['design_drawings']}")
    print(f"实体 {totals['entities']}  尺寸标注 {totals['dimensions']}")
    print()
    header = (f"{'零件':<16s} {'工序':>4s} {'实体':>6s} {'尺寸':>4s} "
              f"{'几何不变':>8s} {'位置不变':>8s} {'分组不变':>8s} "
              f"{'共有特征':>8s} {'几何稳定特征':>10s}")
    print(header)
    for part, row in facts["parts"].items():
        print(f"{part:<16s} {row['steps']:>4d} {row['entities']:>6d} "
              f"{row['dimensions']:>4d} "
              f"{str(row['shape_constant_across_steps']):>8s} "
              f"{str(row['position_constant_across_steps']):>8s} "
              f"{str(row['group_names_constant']):>8s} "
              f"{row['feature_labels_common']:>8d} "
              f"{row['labels_with_stable_geometry']:>4d}/"
              f"{row['labels_measured']:<5d}")


if __name__ == "__main__":
    report()
