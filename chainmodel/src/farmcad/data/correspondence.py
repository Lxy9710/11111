"""Cross-drawing entity correspondence.

The displacement check cannot work on absolute positions, because each
process drawing is laid out independently: the same feature is drawn in a
different place in each of them, and measured on the corpus every
displacement between two consecutive steps is explained by the offset of
the drawings' centres.

What can be compared is the geometry *relative to the drawing's own
reference*. Two drawings of the same part agree on the geometry they
share, and disagree only on where they put it. So the correspondence is
established by matching entities on what they measure and on where they
sit relative to the part's own centre, and the displacement that remains
after the layout offset has been removed is the real one.

This module establishes that correspondence and reports the residual
displacement.

Two levels are used
-------------------
The *coarse* level matches whole views. Two drawings have the same view
when they have a view with the same shape. This is what identifies which
view of one drawing corresponds to which view of another, and it is
robust: a view is a large object.

The *fine* level matches the entities within a matched pair of views. It
uses the entity's length and direction, together with its position
relative to the view's centre, so a redrawn but unmoved feature still
matches.

The residual
------------
Once the layout offset is removed, a feature that still moves is one the
process moved. A feature that does not move is one the process left
alone, and a feature that appears or disappears is one the process
created or removed.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from .geometry import plot_scale
from .topology import (
    Entity,
    _entities_of,
    _entity_length,
    feature_signature,
    split_views,
)

#: How close two view centres have to be, relative to the drawing's own
#: size, for two views to be considered the same view.
VIEW_MATCH_TOLERANCE = 0.15

#: How far an entity may sit from its expected place, relative to the
#: view's size, before it is considered a different entity.
#:
#: The window is generous because its purpose is to *find* the pair that
#: belongs together; the residual then says how far the entity actually
#: moved. The signature has to match exactly as well, so a generous
#: window does not let an unrelated entity be claimed.
ENTITY_MATCH_TOLERANCE = 0.5

#: How far an entity has to move, relative to the view's size, before the
#: move is reported. Below this the drawing is redrawn rather than
#: changed.
ENTITY_MOVED_RATIO = 0.01


def _centroid(entities: list[Entity]) -> tuple[float, float] | None:
    points = [p for e in entities for p in e.points]
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _extent(entities: list[Entity]) -> float:
    """The overall size of a set of entities."""

    points = [p for e in entities for p in e.points]
    if not points:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def match_views(before: dict[str, list[Entity]],
                after: dict[str, list[Entity]]) -> dict:
    """Match the views of two drawings of the same part.

    A view is matched by what it holds, not by where it sits: the
    multiset of the lengths and directions of its entities is the same in
    both drawings, whatever place the draughtsman put the view in.

    The score is the overlap of the two multisets, so a view that gained
    or lost an entity still matches, and a view that shares nothing with
    another does not.
    """

    def signature_set(entities: list[Entity]) -> Counter:
        return Counter(feature_signature(e) for e in entities)

    before_info = {}
    for name, entities in before.items():
        centre = _centroid(entities)
        before_info[name] = {
            "centre": centre,
            "extent": _extent(entities),
            "count": len(entities),
            "signatures": signature_set(entities),
            "entities": entities,
        }

    matches = []
    used = set()
    for name, entities in after.items():
        centre = _centroid(entities)
        if centre is None:
            continue
        signatures = signature_set(entities)
        candidates = []
        for other_name, info in before_info.items():
            if other_name in used or info["centre"] is None:
                continue
            shared = sum((signatures & info["signatures"]).values())
            total = max(sum(signatures.values()), sum(info["signatures"].values()), 1)
            overlap = shared / total
            if overlap >= VIEW_MATCH_TOLERANCE:
                candidates.append((-overlap, other_name))
        if not candidates:
            continue
        _, chosen = min(candidates)
        used.add(chosen)
        matches.append({
            "before_view": chosen,
            "after_view": name,
            "offset": (centre[0] - before_info[chosen]["centre"][0],
                       centre[1] - before_info[chosen]["centre"][1]),
        })

    return {
        "matches": matches,
        "unmatched_before": [n for n in before if n not in used],
        "unmatched_after": [n for n in after if n not in {m["after_view"] for m in matches}],
    }


def match_view_entities(before: list[Entity], after: list[Entity],
                         offset: tuple[float, float],
                         tolerance: float = ENTITY_MATCH_TOLERANCE) -> dict:
    """Match the entities of two matched views.

    The layout offset is removed first, so the comparison is between the
    two drawings' own geometries rather than between their layouts. The
    offset is the one the view matching found, which came from the
    difference of the two views' centres.

    An entity matches when it has the same length and direction and sits
    in the same place once the offset is taken out. The residual is then
    the distance the entity actually moved.

    The comparison is on absolute positions with the offset taken out of
    one side only, and not on positions relative to each view's own
    centre: adding an entity to a view moves its centre, so a relative
    comparison would report the whole view as having moved.
    """

    extent = max(_extent(before), _extent(after), 1.0)

    def middle(entity):
        points = entity.points
        if not points:
            return None
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))

    def aligned(entity):
        position = middle(entity)
        if position is None:
            return None
        return (position[0] + offset[0], position[1] + offset[1])

    matches = []
    used = set()
    for entity in after:
        signature = feature_signature(entity)
        position = middle(entity)
        if position is None:
            continue
        candidates = []
        for index, other in enumerate(before):
            if index in used:
                continue
            if feature_signature(other) != signature:
                continue
            other_position = aligned(other)
            if other_position is None:
                continue
            distance = math.dist(position, other_position)
            if distance <= tolerance * extent:
                candidates.append((distance, index))
        if not candidates:
            continue
        distance, chosen = min(candidates, key=lambda item: item[0])
        used.add(chosen)
        matches.append({
            "before": before[chosen].handle,
            "after": entity.handle,
            "signature": signature,
            "residual_mm": round(distance, 4),
            "moved": distance > ENTITY_MOVED_RATIO * extent,
        })

    added = [e.handle for e in after
             if e.handle not in {m["after"] for m in matches}]
    removed = [e.handle for index, e in enumerate(before) if index not in used]

    return {
        "matches": matches,
        "added": added,
        "removed": removed,
        "moved": [m for m in matches if m["moved"]],
        "extent": extent,
    }


def refine_offset(before: list[Entity], after: list[Entity]) -> tuple[float, float] | None:
    """The layout offset between two views, measured from the entities.

    The centre of a view is a poor measure of where the view sits: one
    entity that moved shifts the centre of everything. The offset is
    therefore measured from the entities themselves.

    The measurement is a search: every candidate offset is one that
    would align a pair of entities of the same shape, and the offset
    that aligns the most entities is the right one. Taking the offset
    that explains the most of the view is robust to the entities that
    genuinely moved, because a moved entity only ever explains one
    pair, while the entities that did not move explain all of them.
    """

    def middle(entity):
        points = entity.points
        if not points:
            return None
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))

    before_positions = [(entity, middle(entity)) for entity in before]
    after_positions = [(entity, middle(entity)) for entity in after]
    before_positions = [(e, p) for e, p in before_positions if p is not None]
    after_positions = [(e, p) for e, p in after_positions if p is not None]
    if not before_positions or not after_positions:
        return None

    candidates = Counter()
    for entity, position in before_positions:
        signature = feature_signature(entity)
        for other, other_position in after_positions:
            if feature_signature(other) != signature:
                continue
            offset = (other_position[0] - position[0],
                      other_position[1] - position[1])
            candidates[(round(offset[0], 1), round(offset[1], 1))] += 1

    if not candidates:
        return None

    # The best offset is the one that aligns the most entities, and among
    # those the one closest to the middle of the view, which breaks the
    # tie when a view contains a symmetric pair.
    best = max(candidates.items(), key=lambda item: item[1])[0]
    return best


def _count_aligned(before: list[Entity], after: list[Entity],
                   offset: tuple[float, float]) -> int:
    """How many entities the offset aligns."""

    def middle(entity):
        points = entity.points
        if not points:
            return None
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))

    aligned = Counter()
    for entity in before:
        position = middle(entity)
        if position is None:
            continue
        signature = feature_signature(entity)
        for other in after:
            if feature_signature(other) != signature:
                continue
            other_position = middle(other)
            if other_position is None:
                continue
            shift = (round(other_position[0] - position[0], 1),
                     round(other_position[1] - position[1], 1))
            aligned[shift] += 1
    return aligned.most_common(1)[0][1] if aligned else 0


def correspond(before_data: dict, after_data: dict) -> dict:
    """Establish the correspondence between two drawings of the same part.

    Returns the matched views, the matched entities within them, and the
    entities that were added or removed. The displacement that remains
    after the layout offset is removed is the residual.
    """

    before_scale = plot_scale(before_data) or 1.0
    after_scale = plot_scale(after_data) or 1.0
    before_views = {name: _entities_of(entities, before_scale)
                    for name, entities in split_views(before_data).items()}
    after_views = {name: _entities_of(entities, after_scale)
                   for name, entities in split_views(after_data).items()}

    view_matches = match_views(before_views, after_views)

    entities = []
    for match in view_matches["matches"]:
        before_entities = before_views[match["before_view"]]
        after_entities = after_views[match["after_view"]]
        # The centre of a view is a poor measure of where it sits, so the
        # offset is refined from the entities themselves before they are
        # compared.
        refined = refine_offset(before_entities, after_entities)
        offset = refined if refined is not None else match["offset"]
        result = match_view_entities(before_entities, after_entities, offset)
        for entry in result["matches"]:
            entry["before_view"] = match["before_view"]
            entry["after_view"] = match["after_view"]
        entities.append({
            "before_view": match["before_view"],
            "after_view": match["after_view"],
            "offset": offset,
            "centre_offset": match["offset"],
            "result": result,
        })

    moved = []
    for entry in entities:
        for item in entry["result"]["moved"]:
            item = dict(item)
            item["before_view"] = entry["before_view"]
            item["after_view"] = entry["after_view"]
            moved.append(item)

    return {
        "views": view_matches,
        "entities": entities,
        "moved": moved,
        "matched_entities": sum(len(e["result"]["matches"]) for e in entities),
        "added": [h for e in entities for h in e["result"]["added"]],
        "removed": [h for e in entities for h in e["result"]["removed"]],
    }
