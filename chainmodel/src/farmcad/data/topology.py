"""Entity-level topology: the interference and misalignment criteria.

The dimension-level work says which *feature* an operation changes. It
does not say whether the geometry that carries the feature is still a legal
solid after the change, and it cannot say whether two features ended up in
the same place. Those are the failure modes that a drawing inspection
misses and a process chain has to rule out.

This module works at the level of the drawn entities: the lines, arcs and
curves that make up a view, and the loops they form.

What can be checked
-------------------
A drawing is a set of views, and each view is a set of entities. Three
things are decidable from them:

*Overlap*
    Two entities that occupy the same place in the same view are
    interfering: the drawing asserts two things about the same piece of
    the part.

*Displacement*
    The same view's geometry, redrawn after a step, has to stay in the
    same place. A feature that moves between two drawings of the same
    part, without a step that says it moved, is misaligned.

*Legality*
    A view that draws a closed loop must have a consistent orientation
    and no self-intersection, or the solid it describes does not exist.

The correspondence between two views is established through the nominal
design, in the same way the dimension-level localisation is: by what the
geometry measures, not by where it sits on the sheet.

The honest limitation
---------------------
A drawing is a projection. A view cannot prove that the solid is legal;
it can only prove that the drawing is self-consistent. So these checks
are *necessary* conditions, and they are stated as such. The chain that
passes them is not thereby correct; the chain that fails them is wrong.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .geometry import (
    collect_entities,
    dimension_orientation,
    dimension_span,
    plot_scale,
)

#: Two entities closer than this in real millimetres are interfering.
#: The tolerance is set by the coarsest thing that has to be told apart,
#: which on this corpus is the gap between adjacent machined features.
OVERLAP_TOLERANCE_MM = 0.5

#: How far a feature may drift between two drawings of the same part
#: before the shift counts as a misalignment rather than redrawing.
DRIFT_TOLERANCE_MM = 1.0


@dataclass
class Entity:
    """One drawn entity, in real millimetres."""

    handle: str | None
    kind: str
    layer: str | None
    points: list[tuple[float, float]] = field(default_factory=list)
    radius: float | None = None
    center: tuple[float, float] | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        if not self.points:
            return None
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    def polyline(self, samples: int = 32) -> list[tuple[float, float]]:
        """The entity as a dense polyline, for geometric comparison.

        A line is its own polyline; a curve is sampled around it. Two
        entities that lie on top of each other share a stretch of these
        points, and two entities that merely cross share none.
        """

        if self.kind == "LINE" and len(self.points) >= 2:
            a, b = self.points[0], self.points[1]
            return [(a[0] + (b[0] - a[0]) * i / samples,
                     a[1] + (b[1] - a[1]) * i / samples)
                    for i in range(samples + 1)]
        if self.kind in ("ARC", "CIRCLE") and self.center and self.radius:
            cx, cy = self.center
            radius = self.radius
            return [(cx + radius * math.cos(2 * math.pi * i / samples),
                     cy + radius * math.sin(2 * math.pi * i / samples))
                    for i in range(samples)]
        return list(self.points)


def _as_point(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if not isinstance(value[0], (int, float)):
        return None
    return (float(value[0]), float(value[1]))


#: Entity types that carry a proxy copy of an entity AutoCAD could not
#: represent natively. The extractor writes both the real entity and its
#: proxy, so every line and arc appears twice, and the two copies sit at
#: distance zero from each other. They have to be dropped before any
#: geometric check, or every entity interferes with its own proxy.
PROXY_TYPES = ("HIDE_", "ACAD_PROXY_ENTITY")

#: The proxy form of a block reference.
PROXY_BLOCK_TYPES = ("INSERTBLOCK",)

#: The prefix of the blocks that hold a view of the part. AutoCAD names
#: them ``FCFEntity<N>`` on this corpus, and each one is a single view:
#: the blocks are disjoint in space, so a view is separable.
VIEW_BLOCK_PREFIX = "FCFEntity"

#: The prefix of the blocks that hold hatching, which is a material
#: indication rather than a view of the part.
HATCH_BLOCK_PREFIX = "HATCH"

#: The prefix of the blocks that hold labels.
LABEL_BLOCK_PREFIX = "LABEL"


def _is_proxy(entity: dict) -> bool:
    kind = str(entity.get("type", ""))
    if kind.startswith(PROXY_TYPES):
        return True
    if kind in PROXY_BLOCK_TYPES:
        return True
    return False


def split_views(data: dict) -> dict:
    """Separate a drawing into its views.

    A drawing is not one picture but several: the part's front view, its
    section, the tool path, the blank outline, each laid out side by side.
    Entities from different views overlap in the projection without
    overlapping on the part, so a check that ignores the views measures
    the layout rather than the geometry.

    On this corpus the views are the blocks named ``FCFEntity<N>``, and
    they are disjoint in space. The top level of the drawing holds
    whatever did not belong to a block.

    The result maps a view name to its entities, and records which of the
    views could be separated.
    """

    views: dict[str, list[dict]] = {}
    for name, block in (data.get("blocks") or {}).items():
        if not isinstance(block, dict):
            continue
        entities = [e for e in (block.get("entities") or [])
                    if isinstance(e, dict)]
        if name.startswith(VIEW_BLOCK_PREFIX):
            views[name] = entities
    top = [e for e in (data.get("entities") or [])
           if isinstance(e, dict) and e.get("type") != "INSERT"]
    views["_TOP"] = top
    return views


def collect_entities_mm(data: dict, scale: float | None = None,
                        drop_proxies: bool = True) -> list[Entity]:
    """The drawn entities of a drawing, in real millimetres.

    The drawing units are divided by the plot scale so that two drawings
    of the same part, plotted at different scales, are comparable. The
    scale is only good up to a factor, so the result is comparable up to
    the same factor; where that matters, the nominal design resolves it.

    The proxy copies are dropped by default. They duplicate every real
    entity, so leaving them in makes every entity interfere with its own
    proxy and the interference check measures nothing.
    """

    if scale is None:
        scale = plot_scale(data)
    factor = scale if scale else 1.0
    out = []
    for entity in collect_entities(data):
        kind = entity.get("type")
        if kind not in ("LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE",
                        "ELLIPSE", "SPLINE"):
            continue
        if drop_proxies and _is_proxy(entity):
            continue
        points = []
        for key in ("start", "end", "center"):
            point = _as_point(entity.get(key))
            if point is not None:
                points.append((point[0] / factor, point[1] / factor))
        for key in ("vertices", "points"):
            value = entity.get(key)
            if isinstance(value, (list, tuple)):
                for item in value:
                    point = _as_point(item)
                    if point is not None:
                        points.append((point[0] / factor, point[1] / factor))
        radius = entity.get("radius")
        center = _as_point(entity.get("center"))
        out.append(Entity(
            handle=entity.get("handle"),
            kind=kind,
            layer=entity.get("layer"),
            points=points,
            radius=float(radius) / factor if isinstance(radius, (int, float)) else None,
            center=(center[0] / factor, center[1] / factor) if center else None,
        ))
    return out


def _segment_distance(a: tuple[float, float], b: tuple[float, float],
                      c: tuple[float, float], d: tuple[float, float]) -> float:
    """The distance between two line segments."""

    def point_segment_distance(p, q, r):
        pq = (q[0] - p[0], q[1] - p[1])
        pr = (r[0] - p[0], r[1] - p[1])
        length = pq[0] * pq[0] + pq[1] * pq[1]
        if length == 0:
            return math.dist(p, r)
        t = max(0.0, min(1.0, (pr[0] * pq[0] + pr[1] * pq[1]) / length))
        projection = (p[0] + t * pq[0], p[1] + t * pq[1])
        return math.dist(projection, r)

    d1 = point_segment_distance(a, b, c)
    d2 = point_segment_distance(a, b, d)
    d3 = point_segment_distance(c, d, a)
    d4 = point_segment_distance(c, d, b)
    return min(d1, d2, d3, d4)


def _shared_length(a: Entity, b: Entity, tolerance: float) -> float:
    """How much of the two entities lie on top of each other.

    This is the measure that separates a conflict from a crossing. A
    circle and a line that cross share no length; a circle drawn twice at
    the same radius share all of it. The shared length is measured by
    walking one entity's polyline and summing the stretches of it that
    lie on the other.
    """

    samples = a.polyline()
    if not samples:
        return 0.0

    # The step between the samples bounds how precisely the shared length
    # can be measured, so it is the resolution of the answer.
    step = 0.0
    for i in range(len(samples) - 1):
        step += math.dist(samples[i], samples[i + 1])
    resolution = step / max(1, len(samples) - 1)
    if resolution <= 0:
        return 0.0

    shared = 0.0
    for i in range(len(samples) - 1):
        p, q = samples[i], samples[i + 1]
        if _segment_on_entity(p, q, b, tolerance):
            shared += math.dist(p, q)
    return shared


def _segment_on_entity(p: tuple[float, float], q: tuple[float, float],
                       entity: Entity, tolerance: float) -> bool:
    """Whether a short segment lies along an entity.

    The segment has to lie along the entity, not merely touch it: a
    segment that meets a curve at one point is a crossing, and a segment
    that ends on it is a junction.
    """

    if entity.kind == "LINE" and len(entity.points) >= 2:
        a, b = entity.points[0], entity.points[1]
        midpoint = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        if _segment_distance(a, b, p, q) > tolerance:
            return False
        # The segment has to run along the line, not across it.
        direction = (b[0] - a[0], b[1] - a[1])
        length = math.hypot(*direction)
        if length < 1e-9:
            return False
        unit = (direction[0] / length, direction[1] / length)
        normal = (-unit[1], unit[0])
        offset = abs((midpoint[0] - a[0]) * normal[0]
                     + (midpoint[1] - a[1]) * normal[1])
        if offset > tolerance:
            return False
        segment_direction = (q[0] - p[0], q[1] - p[1])
        segment_length = math.hypot(*segment_direction)
        if segment_length < 1e-9:
            return False
        alignment = abs((segment_direction[0] * unit[0]
                         + segment_direction[1] * unit[1]) / segment_length)
        return alignment > 0.99

    if entity.kind in ("ARC", "CIRCLE") and entity.center and entity.radius:
        midpoint = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        offset = abs(math.dist(midpoint, entity.center) - entity.radius)
        if offset > tolerance:
            return False
        # The segment has to be tangential, not radial: a chord that
        # crosses the circle meets it at two points and shares no length.
        radial = (midpoint[0] - entity.center[0],
                  midpoint[1] - entity.center[1])
        segment_direction = (q[0] - p[0], q[1] - p[1])
        segment_length = math.hypot(*segment_direction)
        radial_length = math.hypot(*radial)
        if segment_length < 1e-9 or radial_length < 1e-9:
            return False
        alignment = abs((segment_direction[0] * radial[0]
                         + segment_direction[1] * radial[1])
                        / (segment_length * radial_length))
        return alignment < 0.01

    return False


def _segment_overlap_length(a: tuple[float, float], b: tuple[float, float],
                            c: tuple[float, float], d: tuple[float, float],
                            tolerance: float) -> float:
    """How much two line segments lie on top of each other.

    Two entities that *touch* at a point are normal in a technical
    drawing: a rectangle is four lines meeting at four corners. Two
    entities that lie on top of each other over a length are not: the
    drawing is asserting two things about the same stretch of the part.

    So the criterion is the length of the shared stretch, not the distance
    between the entities.
    """

    d1 = (b[0] - a[0], b[1] - a[1])
    d2 = (d[0] - c[0], d[1] - c[1])
    denominator = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denominator) < 1e-12:
        # Parallel. They overlap if they are on the same line and their
        # projections onto that line overlap.
        length_a = math.hypot(*d1)
        if length_a < 1e-9:
            return 0.0
        normal = (-d1[1] / length_a, d1[0] / length_a)
        offset = abs((c[0] - a[0]) * normal[0] + (c[1] - a[1]) * normal[1])
        if offset > tolerance:
            return 0.0
        unit = (d1[0] / length_a, d1[1] / length_a)
        proj_a = (a[0] * unit[0] + a[1] * unit[1])
        proj_b = (b[0] * unit[0] + b[1] * unit[1])
        proj_c = (c[0] * unit[0] + c[1] * unit[1])
        proj_d = (d[0] * unit[0] + d[1] * unit[1])
        low = max(min(proj_a, proj_b), min(proj_c, proj_d))
        high = min(max(proj_a, proj_b), max(proj_c, proj_d))
        return max(0.0, high - low)

    t = ((c[0] - a[0]) * d2[1] - (c[1] - a[1]) * d2[0]) / denominator
    u = ((c[0] - a[0]) * d1[1] - (c[1] - a[1]) * d1[0]) / denominator
    if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
        return 0.0
    point = (a[0] + t * d1[0], a[1] + t * d1[1])
    other = (c[0] + u * d2[0], c[1] + u * d2[1])
    if math.dist(point, other) > tolerance:
        return 0.0
    # They cross rather than lie on top of each other: a crossing is not
    # an interference.
    return 0.0


def _sample_points(entity: Entity, samples: int = 24) -> list[tuple[float, float]]:
    """Points along an entity, densely enough to detect an overlap."""

    if entity.kind == "LINE" and len(entity.points) >= 2:
        a, b = entity.points[0], entity.points[1]
        return [(a[0] + (b[0] - a[0]) * i / samples,
                 a[1] + (b[1] - a[1]) * i / samples)
                for i in range(samples + 1)]
    if entity.kind in ("ARC", "CIRCLE") and entity.center and entity.radius:
        cx, cy = entity.center
        radius = entity.radius
        return [(cx + radius * math.cos(2 * math.pi * i / samples),
                 cy + radius * math.sin(2 * math.pi * i / samples))
                for i in range(samples)]
    return list(entity.points)


def _entity_distance(a: Entity, b: Entity) -> float:
    """The distance between two entities, in millimetres."""

    if a.kind == "LINE" and b.kind == "LINE" and len(a.points) >= 2 and len(b.points) >= 2:
        return _segment_distance(a.points[0], a.points[1], b.points[0], b.points[1])
    if a.center and b.center:
        if a.radius is not None and b.radius is not None:
            distance = math.dist(a.center, b.center)
            return abs(distance - abs(a.radius - b.radius))
        return math.dist(a.center, b.center)
    if a.points and b.points:
        return min(math.dist(p, q) for p in a.points for q in b.points)
    return float("inf")


#: The fraction of the shorter entity that has to be covered by the longer
#: one before the pair counts as an interference.
#:
#: The value is set from the corpus. Classifying the reported pairs by how
#: much of the shorter entity the longer one covers gives:
#:
#:   809 pairs  covered at least 95%  the same geometry drawn twice, once
#:                                   as an individual line and once as
#:                                   part of a polyline
#:   173 pairs  covered 50-95%     a line and the polyline that groups
#:                                   it, sharing most of their length
#:   237 pairs  covered below 50%   and 237 whose covered fraction is
#:                                   below a tenth, with an overlap of
#:                                   under a millimetre
#:
#: The last group is the one that matters, and its overlaps are of the
#: order of a tenth of a millimetre: they are the junctions where two
#: lines meet, and the overlap is a rounding artefact of the coordinates
#: rather than a shared stretch of the part. A pair has to share a
#: meaningful fraction of the shorter entity before it is a conflict.
COVERAGE_RATIO = 0.5


def _entity_length(entity: Entity) -> float:
    """The length of an entity, in millimetres."""

    if entity.kind == "LINE" and len(entity.points) >= 2:
        return math.dist(entity.points[0], entity.points[1])
    if entity.radius is not None:
        return 2 * math.pi * entity.radius
    if entity.points:
        return sum(math.dist(entity.points[i], entity.points[i + 1])
                   for i in range(len(entity.points) - 1))
    return 0.0


def find_interferences(entities: list[Entity],
                       tolerance: float = OVERLAP_TOLERANCE_MM,
                       coverage_ratio: float = COVERAGE_RATIO) -> list[dict]:
    """Pairs of entities that lie on top of each other.

    Two entities that merely touch at a point are normal in a technical
    drawing, and are not reported. Two entities that share a stretch of
    positive length are asserting two things about the same stretch of
    the part, and that is an interference.

    Two further conditions narrow it down, because a drawing legitimately
    contains the same geometry in more than one form:

    * entities that are geometrically identical are not an interference,
      since a drawing may draw the same line twice;
    * entities on different layers are not an interference, since the
      drawing is entitled to draw different things in the same place.

    A pair also has to share a meaningful fraction of the shorter entity.
    A junction where two lines meet shares a fraction of a millimetre,
    and that is a rounding artefact of the coordinates rather than a
    conflict.
    """

    clashes = []
    for a, b in _candidate_pairs(entities, tolerance):
        if a.handle is not None and a.handle == b.handle:
            continue
        if a.layer is not None and b.layer is not None and a.layer != b.layer:
            continue
        if _identical_geometry(a, b, tolerance):
            continue

        length = _shared_length(a, b, tolerance)
        if length <= tolerance:
            continue
        shorter = min(_entity_length(a), _entity_length(b))
        if shorter <= 0:
            continue
        if length / shorter < coverage_ratio:
            continue

        clashes.append({
            "kind": "interference",
            "a": a.handle,
            "b": b.handle,
            "a_type": a.kind,
            "b_type": b.kind,
            "a_layer": a.layer,
            "b_layer": b.layer,
            "overlap_mm": round(length, 4),
            "coverage": round(length / shorter, 4),
        })
    return clashes


def _candidate_pairs(entities: list[Entity], tolerance: float):
    """The pairs of entities that are close enough to overlap.

    A grid over the sheet means only the entities in neighbouring cells
    are compared, which turns the check from quadratic in the entity
    count into roughly linear.
    """

    cell = max(tolerance * 4.0, 1.0)
    grid: dict[tuple, list[int]] = defaultdict(list)
    boxes = []
    for index, entity in enumerate(entities):
        box = entity.bbox
        if box is None:
            boxes.append(None)
            continue
        boxes.append(box)
        x0 = int(math.floor(box[0] / cell))
        x1 = int(math.floor(box[2] / cell))
        y0 = int(math.floor(box[1] / cell))
        y1 = int(math.floor(box[3] / cell))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                grid[(gx, gy)].append(index)

    seen = set()
    for members in grid.values():
        if len(members) < 2:
            continue
        for position, i in enumerate(members):
            for j in members[position + 1:]:
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                yield entities[key[0]], entities[key[1]]


def _identical_geometry(a: Entity, b: Entity, tolerance: float) -> bool:
    """Whether two entities are the same geometry drawn twice.

    The comparison is on the shape and not on the position, because the
    same line may be drawn at the same place (a duplicated outline) or as
    a segment of a longer line (a construction line under an outline).
    """

    if a.kind == "LINE" and len(a.points) >= 2 and len(b.points) >= 2:
        length_a = math.dist(a.points[0], a.points[1])
        length_b = math.dist(b.points[0], b.points[1])
        if abs(length_a - length_b) > tolerance:
            return False
        angle_a = math.atan2(a.points[1][1] - a.points[0][1],
                             a.points[1][0] - a.points[0][0]) % math.pi
        angle_b = math.atan2(b.points[1][1] - b.points[0][1],
                             b.points[1][0] - b.points[0][0]) % math.pi
        delta = abs(angle_a - angle_b)
        if min(delta, abs(delta - math.pi)) > 0.02:
            return False
        # Same length and same direction: the same line, whether it is
        # drawn in the same place or is a piece of a longer one.
        return True

    # A line and a polyline: the same geometry if the line lies entirely
    # along the polyline. This is how a drawing carries a contour that is
    # both an individual line and part of a grouped polyline.
    if "LINE" in (a.kind, b.kind) and "POLYLINE" in (a.kind, b.kind):
        line, poly = (a, b) if a.kind == "LINE" else (b, a)
        if len(line.points) < 2:
            return False
        poly_points = poly.polyline()
        if not poly_points:
            return False
        for point in line.polyline():
            if not _point_on_polyline(point, poly_points, tolerance):
                return False
        return True

    if a.kind != b.kind:
        return False
    if a.radius is not None and b.radius is not None:
        return abs(a.radius - b.radius) <= tolerance
    return False


def _point_on_polyline(point: tuple[float, float],
                       polyline: list[tuple[float, float]],
                       tolerance: float) -> bool:
    """Whether a point lies on a polyline."""

    for i in range(len(polyline) - 1):
        if _segment_distance(polyline[i], polyline[i + 1], point, point) <= tolerance:
            return True
    return False


def _point_on_entity(point: tuple[float, float], entity: Entity,
                     tolerance: float) -> bool:
    """Whether a point lies on an entity, away from its ends."""

    if entity.kind == "LINE" and len(entity.points) >= 2:
        a, b = entity.points[0], entity.points[1]
        if math.dist(point, a) <= tolerance or math.dist(point, b) <= tolerance:
            return False
        return _segment_distance(a, b, point, point) <= tolerance
    if entity.kind in ("ARC", "CIRCLE") and entity.center and entity.radius:
        return abs(math.dist(point, entity.center) - entity.radius) <= tolerance
    return False


def find_loops(entities: list[Entity]) -> list[list[tuple[float, float]]]:
    """The closed contours formed by the line entities.

    A view of a part is a set of closed contours. Following the lines
    from end to end recovers them.

    The walk is strict about the geometry: a contour is only a contour if
    its segments meet at their endpoints. Two lines that cross in the
    middle of both are not the same point, so the walk does not continue
    through them, which is what keeps the result from being a tangle of
    accidental chains.
    """

    lines = [e for e in entities if e.kind == "LINE" and len(e.points) >= 2]
    if not lines:
        return []

    def key(point):
        return (round(point[0], 3), round(point[1], 3))

    adjacency: dict[tuple, list[int]] = defaultdict(list)
    for index, line in enumerate(lines):
        for point in (line.points[0], line.points[1]):
            adjacency[key(point)].append(index)

    loops = []
    visited = set()
    for start in range(len(lines)):
        if start in visited:
            continue
        line = lines[start]
        visited.add(start)
        loop = [line.points[0], line.points[1]]
        endpoint = line.points[1]
        closed = False
        for _ in range(len(lines)):
            candidates = [i for i in adjacency.get(key(endpoint), [])
                          if i not in visited]
            if not candidates:
                break
            nxt = candidates[0]
            visited.add(nxt)
            nxt_line = lines[nxt]
            endpoint = nxt_line.points[1]
            if key(endpoint) == key(loop[0]):
                closed = True
                break
            loop.append(endpoint)
        if closed and len(loop) >= 4:
            loops.append(loop)
    return loops


def check_loop_legality(loop: list[tuple[float, float]]) -> dict:
    """Whether a contour is a legal closed region.

    A contour that crosses itself does not describe a region, so the solid
    it stands for does not exist. The test is the standard segment
    intersection test, excluding the shared endpoints of adjacent
    segments.
    """

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def on_segment(p, q, r):
        return (min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
                and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9)

    count = len(loop)
    intersections = []
    for i in range(count):
        p1, q1 = loop[i], loop[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or (j + 1) % count == i or (i + 1) % count == j:
                continue
            p2, q2 = loop[j], loop[(j + 1) % count]
            d1 = cross(p1, q1, p2)
            d2 = cross(p1, q1, q2)
            d3 = cross(p2, q2, p1)
            d4 = cross(p2, q2, q1)
            if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
                intersections.append((i, j))
            elif (abs(d1) < 1e-9 and on_segment(p1, p2, q1)) or \
                 (abs(d2) < 1e-9 and on_segment(p1, q2, q1)) or \
                 (abs(d3) < 1e-9 and on_segment(p2, p1, q2)) or \
                 (abs(d4) < 1e-9 and on_segment(p2, q1, q2)):
                intersections.append((i, j))

    area = 0.0
    for i in range(count):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % count]
        area += x1 * y2 - x2 * y1
    area /= 2.0

    return {
        "vertices": count,
        "self_intersections": len(intersections),
        "area": round(abs(area), 6),
        "closed": True,
        "legal": len(intersections) == 0 and abs(area) > 1e-9,
    }


def feature_signature(entity: Entity) -> tuple:
    """What an entity measures, in a form that survives relayout.

    The length of the entity and the direction it runs in identify the
    geometry, while its position on the sheet does not.
    """

    if entity.kind == "LINE" and len(entity.points) >= 2:
        a, b = entity.points[0], entity.points[1]
        length = math.dist(a, b)
        angle = round(math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi, 2)
        return ("LINE", round(length, 1), angle)
    if entity.kind in ("ARC", "CIRCLE") and entity.radius is not None:
        return (entity.kind, round(entity.radius, 1))
    if entity.points:
        return (entity.kind, round(entity.radius or 0.0, 1))
    return (entity.kind,)


def match_entities(before: list[Entity], after: list[Entity],
                   tolerance: float = 1.0) -> dict:
    """Match the entities of two drawings of the same part.

    A matched entity that moved is a displacement, and an unmatched entity
    is something the step added or removed.
    """

    before_index: dict[tuple, list[int]] = defaultdict(list)
    for index, entity in enumerate(before):
        before_index[feature_signature(entity)].append(index)

    used = set()
    matched = []
    for entity in after:
        signature = feature_signature(entity)
        candidates = [i for i in before_index.get(signature, [])
                      if i not in used]
        if not candidates:
            continue
        chosen = candidates[0]
        used.add(chosen)
        old = before[chosen]
        displacement = None
        if old.points and entity.points:
            displacement = min(math.dist(p, q) for p in old.points
                               for q in entity.points)
        matched.append({
            "before": old.handle,
            "after": entity.handle,
            "signature": signature,
            "displacement_mm": (round(displacement, 4)
                                if displacement is not None else None),
            "drifted": (displacement is not None and displacement > tolerance),
        })

    added = [e.handle for i, e in enumerate(after)
             if not any(m["after"] == e.handle for m in matched)]
    removed = [e.handle for i, e in enumerate(before) if i not in used]

    return {
        "matched": matched,
        "added": added,
        "removed": removed,
        "drifted": [m for m in matched if m["drifted"]],
        "jaccard": len(matched) / max(1, len(before) + len(after) - len(matched)),
    }


def check_views(data: dict, scale: float | None = None) -> dict:
    """Run the entity-level checks on each view of a drawing separately.

    This is the meaningful form of the check: two entities that lie on top
    of each other in different views are two views of the same feature, not
    a conflict, so the check has to run within a view.
    """

    if scale is None:
        scale = plot_scale(data)
    factor = scale if scale else 1.0

    report = {"views": {}, "interferences": [], "illegal_loops": []}
    for name, entities in split_views(data).items():
        if not entities:
            continue
        mm = []
        for entity in entities:
            if entity.get("type") not in ("LINE", "ARC", "CIRCLE", "POLYLINE",
                                          "LWPOLYLINE", "ELLIPSE", "SPLINE"):
                continue
            if _is_proxy(entity):
                continue
            points = []
            for key in ("start", "end", "center"):
                point = _as_point(entity.get(key))
                if point is not None:
                    points.append((point[0] / factor, point[1] / factor))
            for key in ("vertices", "points"):
                value = entity.get(key)
                if isinstance(value, (list, tuple)):
                    for item in value:
                        point = _as_point(item)
                        if point is not None:
                            points.append((point[0] / factor, point[1] / factor))
            radius = entity.get("radius")
            center = _as_point(entity.get("center"))
            mm.append(Entity(
                handle=entity.get("handle"),
                kind=entity.get("type"),
                layer=entity.get("layer"),
                points=points,
                radius=float(radius) / factor if isinstance(radius, (int, float)) else None,
                center=(center[0] / factor, center[1] / factor) if center else None,
            ))

        clashes = find_interferences(mm)
        loops = find_loops(mm)
        legal = 0
        for loop in loops:
            if check_loop_legality(loop)["legal"]:
                legal += 1
        report["views"][name] = {
            "entities": len(mm),
            "interferences": len(clashes),
            "loops": len(loops),
            "legal_loops": legal,
            "clean": not clashes and legal == len(loops),
        }
        for clash in clashes:
            clash = dict(clash)
            clash["view"] = name
            report["interferences"].append(clash)
    return report


def _entities_of(entities: list[dict], factor: float) -> list[Entity]:
    """The entities of one view, in real millimetres."""

    mm = []
    for entity in entities:
        if entity.get("type") not in ("LINE", "ARC", "CIRCLE", "POLYLINE",
                                      "LWPOLYLINE", "ELLIPSE", "SPLINE"):
            continue
        if _is_proxy(entity):
            continue
        points = []
        for key in ("start", "end", "center"):
            point = _as_point(entity.get(key))
            if point is not None:
                points.append((point[0] / factor, point[1] / factor))
        for key in ("vertices", "points"):
            value = entity.get(key)
            if isinstance(value, (list, tuple)):
                for item in value:
                    point = _as_point(item)
                    if point is not None:
                        points.append((point[0] / factor, point[1] / factor))
        radius = entity.get("radius")
        center = _as_point(entity.get("center"))
        mm.append(Entity(
            handle=entity.get("handle"),
            kind=entity.get("type"),
            layer=entity.get("layer"),
            points=points,
            radius=(float(radius) / factor
                    if isinstance(radius, (int, float)) else None),
            center=((center[0] / factor, center[1] / factor)
                    if center else None),
        ))
    return mm


def check_geometry(design: dict, steps: list[dict]) -> dict:
    """Run the entity-level checks over a route.

    ``design`` is the nominal drawing and ``steps`` the operation drawings.
    The report says whether the geometry of the route is self-consistent:
    no two entities claim the same place, no contour crosses itself, and
    no feature drifts without a step that says it moved.

    The checks run per view. A drawing is a set of views laid out side by
    side, and entities from different views overlap in the projection
    without overlapping on the part, so a check that ignores the views
    measures the layout rather than the geometry.

    The drift check compares the same view in two consecutive steps, each
    normalised by its own plot scale. It is a *candidate* drift rather
    than a fault, because the operation drawings are laid out
    independently: the same feature is drawn in a different place in each
    of them, so a raw displacement measures the layout and not the part.
    """

    steps_views = []
    for step in steps:
        scale = plot_scale(step["data"])
        factor = scale if scale else 1.0
        by_view = {name: _entities_of(entities, factor)
                   for name, entities in split_views(step["data"]).items()}
        steps_views.append((step["label"], by_view))

    interferences = []
    illegal_loops = []
    for label, by_view in steps_views:
        for name, entities in by_view.items():
            for clash in find_interferences(entities):
                clash = dict(clash)
                clash["operation"] = label
                clash["view"] = name
                interferences.append(clash)
            for index, loop in enumerate(find_loops(entities)):
                report = check_loop_legality(loop)
                if not report["legal"]:
                    illegal_loops.append({
                        "operation": label,
                        "view": name,
                        "loop": index,
                        **report,
                    })

    displacements = []
    for (before_label, before_views), (after_label, after_views) in zip(
            steps_views, steps_views[1:]):
        for name in set(before_views) & set(after_views):
            before = before_views[name]
            after = after_views[name]
            if not before or not after:
                continue
            result = match_entities(before, after, tolerance=DRIFT_TOLERANCE_MM)
            for drift in result["drifted"]:
                drift = dict(drift)
                drift["view"] = name
                drift["from_operation"] = before_label
                drift["to_operation"] = after_label
                displacements.append(drift)

    return {
        "interferences": interferences,
        "illegal_loops": illegal_loops,
        "displacements": displacements,
        "clean": not (interferences or illegal_loops),
    }
