"""Faithful geometry extraction: a drawing as a reversible record.

The existing dataset keeps twelve numbers per entity, normalised against
the drawing's own extrema. That vector is enough for a network and useless
for a judge: the minima and the spans were thrown away, so nothing in the
record can say where an entity actually sits, what shape it has, or which
feature of the part it belongs to.

This module keeps the geometry. It reads the extracted AutoCAD JSON and
produces a record in which every entity carries

* its kind, its layer, the groups it belongs to;
* its coordinates in the drawing's own units, exactly as stored;
* the shape the coordinates imply: endpoints, centre, radius, angles,
  bounding box, length;
* the frame of the drawing, so that the normalised vector can be
  *inverted* back to real coordinates.

The frame is the part that makes the record reversible. The existing
normalisation is ``x = min + numeric * span``, so keeping ``min`` and
``span`` is enough to recover the coordinates to the last bit, and
:func:`invert_numeric` does that and is checked against the source in the
tests.

What the group field turns out to be
-----------------------------------
AutoCAD's extracted JSON carries a ``group`` field on every entity, and
the name encodes a hierarchy:

    serial_feature_level_label

The *feature* number is the identity of a feature **of the part**, and it
is the same number in every process drawing of that part, whatever the
drawing annotates. The *serial* is the ordinal of the group within one
drawing, and the *level* separates the drawing's main view from its
detail views. The label is the Chinese description of the feature.

Measured on the corpus: the feature numbers are contiguous per part
(1..32 for the largest), and the same feature number carries the same
feature in every step. That is the topological correspondence the process
chain needs, and it comes out of the drawing itself rather than out of a
heuristic that matches on measured length.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: The name of an annotation group.
GROUP = re.compile(r"^(?P<serial>\d+)_(?P<feature>\d+)_(?P<level>\d+)_(?P<label>.*)$")

#: The bucket every entity belongs to when it carries no annotation.
DEFAULT_GROUP = "default"

#: Entity types that carry the shape of the part.
SHAPE_TYPES = ("LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE",
               "ELLIPSE", "SPLINE")

#: Entity types that carry a measurement rather than a shape.
ANNOTATION_TYPES = ("DIMENSION", "MTEXT", "TEXT", "LEADER")

#: Proxy copies of entities AutoCAD could not represent natively. The
#: extractor writes both the real entity and its proxy, so every line and
#: arc appears twice at distance zero. They carry no geometry of their own.
PROXY_TYPES = ("HIDE_", "ACAD_PROXY_ENTITY")

#: The prefix of the blocks that hold a view of the part.
VIEW_BLOCK_PREFIX = "FCFEntity"

#: The fields that hold a list of points.
#:
#: A spline stores its shape in ``control_points`` and ``fit_points`` and
#: in no other field. Measured on the corpus, 166 splines in one drawing
#: and 468 in another carry no place on the sheet at all when these are
#: not read, which is 4% of the entities of those drawings.
POINT_LIST_KEYS = ("vertices", "points", "coordinates",
                   "fit_points", "control_points")

NUMBER = re.compile(r"-?\d+\.?\d*")


def is_proxy(entity: dict) -> bool:
    """Whether an entity is a proxy copy of another entity."""

    kind = str(entity.get("type", ""))
    return kind.startswith(PROXY_TYPES)


def as_point(value: Any) -> tuple[float, float] | None:
    """Pull a 2D point out of an entity field."""

    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if isinstance(value[0], (list, tuple)):
        if len(value[0]) < 2 or not isinstance(value[0][0], (int, float)):
            return None
        return (float(value[0][0]), float(value[0][1]))
    if not isinstance(value[0], (int, float)):
        return None
    return (float(value[0]), float(value[1]))


def entity_points(entity: dict) -> list[tuple[float, float]]:
    """Every point an entity exposes."""

    points = []
    for key in ("start", "end", "center", "position", "insertion_point",
                "insert", "start_point", "end_point", "ext_line1_point",
                "ext_line2_point", "chord_point", "angle_vertex"):
        point = as_point(entity.get(key))
        if point is not None:
            points.append(point)
    for key in POINT_LIST_KEYS:
        value = entity.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                point = as_point(item)
                if point is not None:
                    points.append(point)
    return points


def clean_text(text: Any) -> str:
    """The readable part of an AutoCAD text override.

    The stored text carries formatting codes (``\\C2;``), a tolerance
    placeholder (``<>``) and a font specification. Stripping them leaves
    the number and the unit, which is what the entity means.
    """

    if not text:
        return ""
    cleaned = re.sub(r"\\[A-Za-z][^;{}]*;", " ", str(text))
    cleaned = re.sub(r"[{}<>]", " ", cleaned)
    cleaned = cleaned.replace("%%c", "Φ").replace("%%d", "°")
    # The tolerance prefix (``%%p``) is not part of the value.
    cleaned = re.sub(r"%%p[0-9.]*", " ", cleaned)
    return " ".join(cleaned.split())


def stated_numbers(text: Any) -> list[float]:
    """The plain numbers in a dimension text override."""

    cleaned = re.sub(r"\\[A-Za-z][^;{}]*;", " ", str(text or ""))
    cleaned = re.sub(r"[{}<>]", " ", cleaned)
    return [float(m) for m in NUMBER.findall(cleaned)]


@dataclass
class Geometry:
    """The geometry of one entity, in the drawing's own units."""

    kind: str
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    center: tuple[float, float] | None = None
    radius: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    length: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.lower(),
            "start": list(self.start) if self.start else None,
            "end": list(self.end) if self.end else None,
            "center": list(self.center) if self.center else None,
            "radius": self.radius,
            "start_angle": self.start_angle,
            "end_angle": self.end_angle,
            "bbox": list(self.bbox) if self.bbox else None,
            "length": self.length,
        }


def entity_geometry(entity: dict) -> Geometry:
    """The geometry of one entity, taken from the coordinates it stores.

    The kind is normalised to a small vocabulary, because the raw AutoCAD
    type names vary (``LWPOLYLINE`` and ``POLYLINE`` are the same thing
    for the purposes of a shape), and because the checks downstream should
    not have to know the file format.
    """

    raw = str(entity.get("type", "")).upper()
    kind = {
        "LINE": "line",
        "ARC": "arc",
        "CIRCLE": "circle",
        "POLYLINE": "polyline",
        "LWPOLYLINE": "polyline",
        "ELLIPSE": "ellipse",
        "SPLINE": "spline",
        "DIMENSION": "dim",
        "MTEXT": "text",
        "TEXT": "text",
        "LEADER": "leader",
        "HATCH": "hatch",
        "SOLID": "solid",
        "POINT": "point",
        "INSERT": "block",
        "INSERTBLOCK": "block",
    }.get(raw, "other")

    points = entity_points(entity)
    start = as_point(entity.get("start"))
    end = as_point(entity.get("end"))
    center = as_point(entity.get("center"))
    radius = entity.get("radius")
    radius = float(radius) if isinstance(radius, (int, float)) else None

    def angle(key: str) -> float | None:
        value = entity.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    start_angle = angle("start_angle")
    end_angle = angle("end_angle")

    bbox = None
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))

    length = None
    if kind == "line" and start and end:
        length = math.dist(start, end)
    elif kind == "circle" and radius is not None:
        length = 2 * math.pi * radius
    elif kind == "arc" and radius is not None and start_angle is not None and end_angle is not None:
        # The length of an arc is its radius times the angle it sweeps, not
        # the circumference of the whole circle.
        length = radius * ((end_angle - start_angle) % (2 * math.pi))

    # An arc bulges. The box of its two endpoints does not contain the
    # bulge, so for an arc the box is rebuilt from the angles it sweeps.
    if kind == "arc" and center and radius is not None and start_angle is not None and end_angle is not None:
        angles = [start_angle]
        sweep = (end_angle - start_angle) % (2 * math.pi)
        for quarter in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
            offset = (quarter - start_angle) % (2 * math.pi)
            if offset <= sweep:
                angles.append(start_angle + offset)
        angles.append(end_angle)
        xs = [center[0] + radius * math.cos(a) for a in angles]
        ys = [center[1] + radius * math.sin(a) for a in angles]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    elif kind == "circle" and center and radius is not None:
        bbox = (center[0] - radius, center[1] - radius,
                center[0] + radius, center[1] + radius)

    return Geometry(
        kind=kind,
        start=start,
        end=end,
        center=center,
        radius=radius,
        start_angle=start_angle,
        end_angle=end_angle,
        points=points,
        bbox=bbox,
        length=length,
    )


@dataclass
class GroupRef:
    """A reference from an entity to an annotation group."""

    serial: int
    feature: int
    level: int
    label: str

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "feature": self.feature,
            "level": self.level,
            "label": self.label,
        }


def parse_group(name: str) -> GroupRef | None:
    """Parse an annotation group name."""

    match = GROUP.match(name)
    if not match:
        return None
    return GroupRef(
        serial=int(match.group("serial")),
        feature=int(match.group("feature")),
        level=int(match.group("level")),
        label=match.group("label"),
    )


def feature_name(label: str) -> str:
    """The name of the feature a group label describes.

    A label names a feature and then lists the dimensions that annotate
    it, each as ``_<index><kind>``. The index is a part-level identifier
    for that dimension, and the kind says what it measures. The name of
    the feature is the text before the first of those.
    """

    match = re.search(r"_\d+", label)
    return label[:match.start()] if match else label


def dimension_spec(label: str) -> list[dict]:
    """The dimensions a group label declares.

    A label such as ``外圆_6直径_5粗糙度`` declares two dimensions of the
    outer circle: dimension 6 is its diameter and dimension 5 is its
    roughness. The numbers are the part-level dimension identifiers, so
    the same dimension keeps its number across every drawing of the part.

    The index and the kind share a token: the token starts with the number
    and the rest of the token is what it measures.
    """

    out = []
    for token in label.split("_")[1:]:
        match = re.match(r"^(\d+)(.+)$", token)
        if match:
            out.append({"index": int(match.group(1)), "kind": match.group(2)})
    return out


@dataclass
class ExtractedEntity:
    """One entity of a drawing, with everything that is known about it."""

    id: str
    type: str
    layer: str | None
    handle: str | None
    text: str
    groups: list[GroupRef]
    geometry: Geometry
    block: str | None
    measurement: float | None
    stated_value: float | None
    dimension_kind: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "layer": self.layer,
            "handle": self.handle,
            "text": self.text,
            "groups": [g.to_dict() for g in self.groups],
            "geometry": self.geometry.to_dict(),
            "block": self.block,
            "measurement": self.measurement,
            "stated_value": self.stated_value,
            "dimension_kind": self.dimension_kind,
        }


def frame_of(entities: list[dict]) -> dict:
    """The frame of a drawing: the extent the normalised vector refers to.

    The existing dataset normalises an entity against the minimum and the
    span of the drawing, so keeping the two makes the vector reversible.
    The frame is computed the same way, over the entities that have a
    centre.
    """

    centres = []
    for entity in entities:
        if is_proxy(entity):
            continue
        points = entity_points(entity)
        if not points:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        centres.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))

    if not centres:
        return {"min": [0.0, 0.0], "span": [1.0, 1.0], "unit": "mm", "scale": None}
    minima = [min(c[i] for c in centres) for i in range(2)]
    spans = [max(max(c[i] for c in centres) - minima[i], 1.0) for i in range(2)]
    return {"min": minima, "span": spans, "unit": "mm", "scale": None}


def invert_numeric(numeric: list[float], frame: dict) -> tuple[float, float]:
    """Recover the centre of an entity from its normalised vector.

    The normalisation used by the dataset is
    ``x = min + numeric * span``, so the inverse is exact and the frame
    is what makes the vector useful to a judge.
    """

    minimum = frame["min"]
    span = frame["span"]
    return (minimum[0] + numeric[0] * span[0],
            minimum[1] + numeric[1] * span[1])


def normalize_point(point: tuple[float, float], frame: dict) -> tuple[float, float]:
    """The normalised form of a point, as the dataset writes it."""

    minimum = frame["min"]
    span = frame["span"]
    return ((point[0] - minimum[0]) / span[0],
            (point[1] - minimum[1]) / span[1])


def collect_raw_entities(data: dict) -> list[tuple[str | None, dict]]:
    """Every entity of a drawing, with the block it came from.

    The block matters: the blocks named ``FCFEntity<N>`` are the views of
    the part, and an entity that belongs to a view belongs to that view
    and to no other. The top level holds whatever did not belong to a
    block.
    """

    out: list[tuple[str | None, dict]] = []
    for entity in data.get("entities") or []:
        if isinstance(entity, dict):
            out.append((None, entity))
    for name, block in (data.get("blocks") or {}).items():
        if not isinstance(block, dict):
            continue
        for entity in block.get("entities") or []:
            if isinstance(entity, dict):
                out.append((name, entity))
    return out


def extract_entities(data: dict) -> list[ExtractedEntity]:
    """Every entity of a drawing, with its geometry and its groups."""

    raw = collect_raw_entities(data)
    entities = []
    for index, (block, entity) in enumerate(raw):
        groups = []
        for name in entity.get("group") or []:
            parsed = parse_group(name)
            if parsed:
                groups.append(parsed)
        kind = str(entity.get("type", "")).upper()
        text = clean_text(entity.get("text") or entity.get("contents") or "")
        measurement = entity.get("measurement")
        measurement = float(measurement) if isinstance(measurement, (int, float)) else None
        values = stated_numbers(entity.get("text"))
        dimension_kind = None
        if kind == "DIMENSION" and groups:
            specs = dimension_spec(groups[0].label)
            dimension_kind = specs[0]["kind"] if specs else None
        entities.append(ExtractedEntity(
            id=str(entity.get("handle") or f"{block or 'TOP'}:{index}"),
            type=kind if kind else "OTHER",
            layer=entity.get("layer"),
            handle=entity.get("handle"),
            text=text,
            groups=groups,
            geometry=entity_geometry(entity),
            block=block,
            measurement=measurement,
            stated_value=values[0] if values else None,
            dimension_kind=dimension_kind,
        ))
    return entities


def split_by_picture(entities: list["ExtractedEntity"], gap_ratio: float = 0.01) -> list[list[int]]:
    """Group the entities into the pictures they form on the sheet.

    A drawing is several pictures laid out side by side. The pictures are
    separated by a gap that is much wider than the gap between the
    entities inside one picture, so the views can be recovered from the
    geometry alone.

    This matters because the blocks are not the views. Measured on the
    corpus, the blocks named ``FCFEntity<N>`` hold 3,661 entities out of
    45,818, which is 8% of the geometry, and the rest of it sits in the
    top level together with everything else. A view split that leaves 90%
    of the geometry in one bucket has not separated the views; it has only
    separated the blocks.

    The split is a union over boxes that overlap, so two entities belong
    to the same picture when their boxes touch. The gap that decides
    whether two pictures are the same one is a fraction of the sheet,
    because a drawing may be plotted at any scale.
    """

    boxes = []
    for index, entity in enumerate(entities):
        # The annotation is not part of a picture's geometry: a dimension's
        # text is written away from the shape it measures, so grouping it
        # by position would pull unrelated pictures together. It is
        # attached to the nearest picture afterwards.
        if entity.type in ("DIMENSION", "MTEXT", "TEXT", "LEADER"):
            continue
        if entity.geometry.bbox is not None:
            boxes.append((index, entity.geometry.bbox))
    if not boxes:
        return []

    xs = [b[1][0] for b in boxes] + [b[1][2] for b in boxes]
    ys = [b[1][1] for b in boxes] + [b[1][3] for b in boxes]
    gap = gap_ratio * max(max(xs) - min(xs), max(ys) - min(ys))

    parent = list(range(len(boxes)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # The boxes are swept along the x axis, so a box only has to be
    # compared with the boxes that start after it.
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][1][0])
    for position, i in enumerate(order):
        box_i = boxes[i][1]
        for j in order[position + 1:]:
            box_j = boxes[j][1]
            if box_j[0] > box_i[2] + gap:
                break
            if (box_i[0] - gap <= box_j[2] and box_j[0] - gap <= box_i[2]
                    and box_i[1] - gap <= box_j[3] and box_j[1] - gap <= box_i[3]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for position, (index, _) in enumerate(boxes):
        groups.setdefault(find(position), []).append(index)

    # The groups are named by their position on the sheet, so that the
    # name of a view does not depend on the order of the entities.
    # ``boxes`` is indexed by position within the box list, while
    # ``members`` holds entity indices, so the position is recovered.
    position_of = {index: position for position, (index, _) in enumerate(boxes)}
    ordered = sorted(groups.values(),
                     key=lambda members: (min(boxes[position_of[m]][1][0] for m in members),
                                          min(boxes[position_of[m]][1][1] for m in members)))
    return ordered


def extract_views(data: dict) -> list[dict]:
    """The views of a drawing.

    A drawing is not one picture but several: the main view of the part,
    its section, the detail views of the grooves, the tool path, each laid
    out side by side.

    Two things are recorded. The *block* a entity came from, which is what
    the drawing itself states, and the *picture* it belongs to, which is
    recovered from the geometry. The two are not the same thing: the
    blocks named ``FCFEntity<N>`` hold 8% of the geometry of the corpus,
    and the pictures are recovered from the sheet.

    The level of the annotation group says whether an entity belongs to a
    main view or to a detail view, and the groups that name the same
    feature at different levels are the main view and its detail.
    """

    entities = extract_entities(data)
    views: dict[str, dict] = {}
    for entity in entities:
        name = entity.block if entity.block and entity.block.startswith(VIEW_BLOCK_PREFIX) else "_TOP"
        view = views.setdefault(name, {
            "name": name,
            "entity_ids": [],
            "bbox": None,
            "features": set(),
            "levels": set(),
        })
        view["entity_ids"].append(entity.id)
        for group in entity.groups:
            view["features"].add(group.feature)
            view["levels"].add(group.level)
        box = entity.geometry.bbox
        if box:
            if view["bbox"] is None:
                view["bbox"] = list(box)
            else:
                current = view["bbox"]
                view["bbox"] = [
                    min(current[0], box[0]), min(current[1], box[1]),
                    max(current[2], box[2]), max(current[3], box[3]),
                ]

    out = []
    for name in sorted(views):
        view = views[name]
        out.append({
            "name": name,
            "entity_ids": view["entity_ids"],
            "bbox": view["bbox"],
            "features": sorted(view["features"]),
            "levels": sorted(view["levels"]),
            "is_detail": name == "_TOP" and any(l > 0 for l in view["levels"]),
        })
    return out


def extract_features(data: dict) -> list[dict]:
    """The features of a drawing, described by the annotation groups.

    A feature is identified by its number, and the number is the same in
    every drawing of the part. The feature records the labels it was
    given, the dimensions it declares, and the entities that belong to it.
    """

    entities = extract_entities(data)
    features: dict[int, dict] = {}
    for entity in entities:
        for group in entity.groups:
            feature = features.setdefault(group.feature, {
                "id": group.feature,
                "serial": group.serial,
                "levels": set(),
                "labels": set(),
                "entity_ids": set(),
                "dimensions": [],
            })
            feature["levels"].add(group.level)
            feature["labels"].add(group.label)
            feature["entity_ids"].add(entity.id)

    out = []
    for key in sorted(features):
        feature = features[key]
        specs: dict[int, dict] = {}
        for label in feature["labels"]:
            for spec in dimension_spec(label):
                specs.setdefault(spec["index"], spec)
        feature["dimensions"] = [specs[i] for i in sorted(specs)]
        out.append({
            "id": feature["id"],
            "serial": feature["serial"],
            "name": feature_name(sorted(feature["labels"])[0]),
            "labels": sorted(feature["labels"]),
            "levels": sorted(feature["levels"]),
            "entity_ids": sorted(feature["entity_ids"]),
            "dimensions": feature["dimensions"],
        })
    return out


def extract_relations(data: dict) -> list[dict]:
    """The relations between the features of a drawing.

    Two relations are recoverable from the drawing itself.

    *A detail view belongs to a main view.* The detail view of a groove
    and the main view that shows the groove are the same feature at two
    levels, and the group names of the two carry the same feature number.
    The relation is therefore read off the feature number, and it is
    exact.

    *A dimension annotates a feature.* The dimension's extension points
    sit on the geometry it measures, and the group it belongs to names
    the feature. The relation is exact wherever the dimension declares a
    feature and its points fall inside that feature's extent.
    """

    entities = {e.id: e for e in extract_entities(data)}
    features = {f["id"]: f for f in extract_features(data)}
    relations: list[dict] = []

    # A dimension annotates the feature it is grouped with.
    for entity in entities.values():
        if entity.type != "DIMENSION":
            continue
        for group in entity.groups:
            if group.feature not in features:
                continue
            relations.append({
                "relation": "annotation_of",
                "source": entity.id,
                "target": group.feature,
                "level": group.level,
                "evidence": "group_membership",
            })

    # A block reference belongs to the feature it is grouped with.
    for entity in entities.values():
        if entity.type != "INSERTBLOCK":
            continue
        for group in entity.groups:
            if group.feature not in features:
                continue
            relations.append({
                "relation": "block_ref",
                "source": entity.id,
                "target": group.feature,
                "level": group.level,
                "evidence": "group_membership",
            })

    # The entities of a feature are the members of that feature.
    for feature in features.values():
        for entity_id in feature["entity_ids"]:
            relations.append({
                "relation": "member_of",
                "source": entity_id,
                "target": feature["id"],
                "level": 0,
                "evidence": "group_membership",
            })

    # Two groups that name the same feature at different levels are a
    # main view and its detail view.
    by_feature: dict[int, list[dict]] = {}
    for feature in features.values():
        for level in feature["levels"]:
            by_feature.setdefault(feature["id"], []).append({"level": level})

    return relations


def extract_drawing(data: dict, case_id: str, source: str) -> dict:
    """The full faithful record of one drawing."""

    entities = extract_entities(data)
    raw = collect_raw_entities(data)

    # The frame is computed over the top level of the drawing only,
    # because that is the scope the dataset normalised against. Measured
    # against the shipped cases, a frame built over the blocks as well
    # shifts the minimum by up to 31 mm and the span by 31 mm, which
    # would place every entity of the dataset in the wrong spot.
    frame = frame_of([entity for entity in (data.get("entities") or [])
                      if isinstance(entity, dict)])
    # The frame of the whole drawing, blocks included, is kept beside it:
    # it is the extent of the sheet, which is what the geometry of the
    # views has to be measured against.
    frame_all = frame_of([entity for _, entity in raw])

    # The plot scale is recovered from the dimensions themselves.
    from .geometry import plot_scale
    scale = plot_scale(data)
    frame["scale"] = scale
    frame_all["scale"] = scale

    views = extract_views(data)

    # The pictures the geometry forms on the sheet, which are not the
    # blocks. The annotation is attached to the nearest picture, because a
    # dimension's text is written away from the shape it measures.
    pictures = _pictures_of(entities)

    return {
        "schema_version": "farmcad-geom-v1",
        "case_id": case_id,
        "source": source,
        "frame": frame,
        "frame_all": frame_all,
        "views": views,
        "pictures": pictures,
        "features": extract_features(data),
        "entities": [entity.to_dict() for entity in entities],
        "relations": extract_relations(data),
        "summary": {
            "entities": len(entities),
            "views": len(views),
            "pictures": len(pictures),
            "features": len(extract_features(data)),
            "dimensions": sum(1 for e in entities if e.type == "DIMENSION"),
        },
    }


def _pictures_of(entities: list["ExtractedEntity"]) -> list[dict]:
    """The pictures the sheet holds, with the annotation attached to them.

    A picture is a group of geometry that lies together. The annotation
    is not part of any picture by position — a dimension's text is written
    away from the shape it measures — so it joins the nearest picture.
    """

    groups = split_by_picture(entities)
    pictures = []
    for number, members in enumerate(groups, start=1):
        boxes = [entities[i].geometry.bbox for i in members]
        boxes = [b for b in boxes if b is not None]
        pictures.append({
            "name": f"P{number:02d}",
            "entity_ids": [entities[i].id for i in members],
            "bbox": ([min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes)]
                     if boxes else None),
        })

    # The annotation joins the nearest picture, measured by the centre of
    # the picture rather than by the nearest single entity.
    annotation = [e for e in entities
                  if e.type in ("DIMENSION", "MTEXT", "TEXT", "LEADER")]
    for entity in annotation:
        anchor = None
        if entity.geometry.bbox is not None:
            anchor = ((entity.geometry.bbox[0] + entity.geometry.bbox[2]) / 2.0,
                      (entity.geometry.bbox[1] + entity.geometry.bbox[3]) / 2.0)
        if anchor is None or not pictures:
            continue
        best = None
        for picture in pictures:
            box = picture["bbox"]
            if box is None:
                continue
            distance = math.dist(anchor, ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0))
            if best is None or distance < best[0]:
                best = (distance, picture["name"])
        if best:
            picture["entity_ids"].append(entity.id)
    return pictures
