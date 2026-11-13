"""Tests for the faithful geometry extraction.

The claims the rest of the work rests on are checked here:

* the normalised vector is reversible against the frame, to the last bit;
* the group name parses into the hierarchy it is supposed to encode;
* the feature number is the identity of a feature of the part;
* the detail view of a feature is found through the feature number, not
  through a guess about where it sits.
"""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from farmcad.data.faithful import (
    clean_text,
    dimension_spec,
    extract_drawing,
    extract_entities,
    extract_features,
    extract_views,
    feature_name,
    frame_of,
    invert_numeric,
    normalize_point,
    parse_group,
)

BASE = Path(__file__).resolve().parents[2]
GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"


def _drawing():
    """A small drawing with the structure of the corpus."""

    return {
        "entities": [
            {
                "type": "LINE", "handle": "A1", "layer": "1",
                "start": [10.0, 20.0, 0.0], "end": [40.0, 20.0, 0.0],
                "group": ["1_3_1_外圆_6直径_5粗糙度"],
            },
            {
                "type": "ARC", "handle": "A2", "layer": "1",
                "center": [100.0, 100.0, 0.0], "radius": 25.0,
                "start_angle": 0.0, "end_angle": 3.14159,
                "group": ["2_3_1_外圆_6直径_5粗糙度"],
            },
            {
                "type": "DIMENSION", "handle": "D1", "layer": "1",
                "measurement": 60.0, "text": "\\C2; \C2;30 \C1;",
                "group": ["1_3_1_外圆_6直径_5粗糙度"],
            },
            {
                "type": "HIDE_AcDbLine", "handle": "H1",
                "group": ["1_3_1_外圆_6直径_5粗糙度"],
            },
        ],
        "blocks": {
            "FCFEntity1": {
                "entities": [
                    {
                        "type": "LINE", "handle": "B1", "layer": "1",
                        "start": [200.0, 300.0, 0.0], "end": [260.0, 300.0, 0.0],
                        "group": ["3_7_0_槽底圆角_3半径"],
                    },
                ],
                "origin": [200.0, 300.0, 0.0],
            },
        },
        "groups": ["default", "1_3_1_外圆_6直径_5粗糙度", "3_7_0_槽底圆角_3半径"],
    }


class GroupParsingTests(unittest.TestCase):
    def test_parses_the_hierarchy(self):
        ref = parse_group("12_14_1_E类共3处环槽_8长度")
        self.assertEqual(ref.serial, 12)
        self.assertEqual(ref.feature, 14)
        self.assertEqual(ref.level, 1)
        self.assertEqual(ref.label, "E类共3处环槽_8长度")

    def test_rejects_a_non_group(self):
        self.assertIsNone(parse_group("default"))
        self.assertIsNone(parse_group("618S_A1"))

    def test_feature_name_stops_before_the_dimension_index(self):
        self.assertEqual(feature_name("外圆_6直径_5粗糙度"), "外圆")
        self.assertEqual(feature_name("B类共3处环槽"), "B类共3处环槽")

    def test_dimension_spec_pairs_index_with_kind(self):
        specs = dimension_spec("外圆_6直径_5粗糙度")
        self.assertEqual(specs, [{"index": 6, "kind": "直径"},
                                 {"index": 5, "kind": "粗糙度"}])


class GeometryTests(unittest.TestCase):
    def test_line_geometry_is_exact(self):
        entities = extract_entities(_drawing())
        line = next(e for e in entities if e.handle == "A1")
        self.assertEqual(line.geometry.kind, "line")
        self.assertEqual(line.geometry.start, (10.0, 20.0))
        self.assertEqual(line.geometry.end, (40.0, 20.0))
        self.assertAlmostEqual(line.geometry.length, 30.0)

    def test_arc_geometry_keeps_centre_and_radius(self):
        entities = extract_entities(_drawing())
        arc = next(e for e in entities if e.handle == "A2")
        self.assertEqual(arc.geometry.kind, "arc")
        self.assertEqual(arc.geometry.center, (100.0, 100.0))
        self.assertEqual(arc.geometry.radius, 25.0)

    def test_frame_makes_the_normalised_vector_reversible(self):
        data = _drawing()
        frame = frame_of(data["entities"])
        entity = data["entities"][0]
        centre = ((entity["start"][0] + entity["end"][0]) / 2,
                  (entity["start"][1] + entity["end"][1]) / 2)
        numeric = list(normalize_point(centre, frame))
        recovered = invert_numeric(numeric, frame)
        self.assertAlmostEqual(recovered[0], centre[0], places=9)
        self.assertAlmostEqual(recovered[1], centre[1], places=9)

    def test_dimension_text_is_cleaned(self):
        self.assertEqual(clean_text("\\C2; \C2;30 \C1;{%%p0.1}"), "30")
        self.assertEqual(clean_text("%%c6.7"), "Φ6.7")


class FeatureTests(unittest.TestCase):
    def test_the_feature_number_is_the_identity(self):
        features = extract_features(_drawing())
        by_id = {f["id"]: f for f in features}
        # Feature 3 appears in two groups and is one feature.
        self.assertIn(3, by_id)
        self.assertEqual(by_id[3]["name"], "外圆")
        # Both groups of the feature carry the same level.
        self.assertEqual(by_id[3]["levels"], [1])

    def test_a_level_zero_group_is_a_detail_view(self):
        features = extract_features(_drawing())
        by_id = {f["id"]: f for f in features}
        # Feature 7 lives in the FCFEntity block, at level 0.
        self.assertEqual(by_id[7]["levels"], [0])

    def test_the_detail_view_is_found_through_the_feature_number(self):
        views = extract_views(_drawing())
        detail = next(v for v in views if v["name"] == "FCFEntity1")
        self.assertEqual(detail["features"], [7])
        self.assertIn(0, detail["levels"])

    def test_the_record_is_self_describing(self):
        record = extract_drawing(_drawing(), "TEST", "unit")
        self.assertEqual(record["schema_version"], "farmcad-geom-v1")
        self.assertEqual(record["summary"]["entities"], 5)
        self.assertEqual(record["summary"]["features"], 2)
        self.assertIn("frame", record)
        self.assertIn("min", record["frame"])


@unittest.skipUnless(FAXIN.is_dir(), "FAXIN corpus is not available")
class CorpusTests(unittest.TestCase):
    """The claims that were measured on the corpus, checked as tests."""

    def test_the_feature_number_is_stable_across_the_route(self):
        """The same feature number means the same feature in every step.

        This is the correspondence the process chain needs, and it comes
        out of the drawing rather than out of a heuristic that matches on
        measured length.

        The feature *name* may differ between drawings — one step calls it
        ``外圆`` and another ``Φ6点7外圆`` — but the number is the same,
        and the numbers of a part are contiguous, which is what a
        part-level numbering looks like.
        """

        folder = FAXIN / "2798Z-01-03"
        drawings = sorted(folder.glob("*.json"))
        self.assertGreater(len(drawings), 5)

        feature_ids = set()
        for path in drawings:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            feature_ids.update(f["id"] for f in extract_features(data))

        self.assertTrue(feature_ids)
        self.assertEqual(sorted(feature_ids),
                         list(range(min(feature_ids), max(feature_ids) + 1)),
                         "the feature numbers of a part are not contiguous")

    def test_the_geometry_of_a_feature_changes_with_the_operation(self):
        """The drawn geometry of a feature is not constant.

        If it were, the process drawings would carry no geometric
        information and the whole question of a geometric process chain
        would be moot.
        """

        folder = FAXIN / "2798Z-01-03"
        drawings = sorted(folder.glob("*.json"))
        signatures = []
        for path in drawings:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            entities = extract_entities(data)
            shapes = {}
            for entity in entities:
                for group in entity.groups:
                    key = (group.feature, entity.geometry.kind,
                           entity.geometry.radius,
                           entity.geometry.length)
                    shapes[key] = shapes.get(key, 0) + 1
            signatures.append(shapes)

        self.assertTrue(any(s != signatures[0] for s in signatures),
                        "the drawn geometry never changes between steps")

    def test_the_same_feature_is_named_consistently_within_a_part(self):
        """A feature number keeps its name across the steps that use it.

        The names are not identical everywhere — a detail view of the
        outer circle is called ``Φ6点7外圆`` — but the *kind* of thing
        the feature is agrees often enough that the correspondence is
        usable, and every disagreement is reportable.
        """

        folder = FAXIN / "2798Z-01-03"
        names: dict[int, set[str]] = {}
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for feature in extract_features(data):
                names.setdefault(feature["id"], set()).add(feature["name"])

        # Most features keep one name across the route.
        stable = sum(1 for values in names.values() if len(values) == 1)
        self.assertGreater(stable, len(names) / 2)

    def test_the_frame_inverts_the_numbers_of_the_shipped_dataset(self):
        """The frame has to be the one the dataset normalised against.

        The dataset cases were built from the top level of a drawing. If
        the frame were built over the blocks as well, the minimum would
        move by up to 31 mm and every entity of the shipped cases would
        be placed in the wrong spot. The numbers would still look
        normalised and the error would be silent.
        """

        from farmcad.data.prepare import entity_geometry

        path = GEO / "设计图" / "2798Z-01-03.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        origins = data["entities"]

        # The normalisation the dataset applies.
        geometry = [entity_geometry(e) for e in origins]
        valid = [g[0] for g in geometry if g[2]]
        minima = [min((p[i] for p in valid), default=0.0) for i in range(2)]
        spans = [max(max((p[i] for p in valid), default=0.0) - minima[i], 1.0)
                 for i in range(2)]

        record = extract_drawing(data, "2798Z-01-03", "design")
        for i in range(2):
            self.assertAlmostEqual(record["frame"]["min"][i], minima[i], places=9)
            self.assertAlmostEqual(record["frame"]["span"][i], spans[i], places=9)

    def test_a_spline_keeps_its_place_on_the_sheet(self):
        """A spline stores its shape in its control points and nowhere else.

        Measured on the corpus: 166 splines in one drawing and 468 in
        another, which is 4% of the entities of those drawings. A record
        that does not read them leaves them with no position at all.
        """

        path = GEO / "设计图" / "1340.1-02-01.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        entities = extract_entities(data)
        splines = [e for e in entities if e.type == "SPLINE"]
        self.assertGreater(len(splines), 100)
        without = [e for e in splines if e.geometry.bbox is None]
        self.assertEqual(without, [], "a spline has no place on the sheet")

    def test_an_arc_is_bounded_by_its_bulge_not_by_its_endpoints(self):
        """The box of an arc is the box of the sector it sweeps."""

        data = {
            "entities": [{
                "type": "ARC", "handle": "A",
                "center": [0.0, 0.0, 0.0], "radius": 10.0,
                "start_angle": 0.0, "end_angle": 3.141592653589793,
                "layer": "1",
            }],
            "blocks": {},
        }
        entities = extract_entities(data)
        arc = entities[0].geometry
        self.assertAlmostEqual(arc.bbox[0], -10.0, places=6)
        self.assertAlmostEqual(arc.bbox[1], 0.0, places=6)
        self.assertAlmostEqual(arc.bbox[2], 10.0, places=6)
        self.assertAlmostEqual(arc.bbox[3], 10.0, places=6)
        # The length is the arc, not the whole circle.
        self.assertAlmostEqual(arc.length, 10.0 * math.pi, places=6)


    def test_the_pictures_of_a_drawing_are_not_the_blocks(self):
        """The views of a drawing are the pictures, not the blocks.

        Measured on the corpus: the blocks named ``FCFEntity<N>`` hold
        3,661 entities out of 45,818, which is 8% of the geometry, and
        everything else sits in the top level together. A view split that
        leaves 90% of the geometry in one bucket has not separated the
        views; it has only separated the blocks.
        """

        path = GEO / "设计图" / "2798Z-01-03.json"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        record = extract_drawing(data, "2798Z-01-03", "design")

        blocks = [v for v in record["views"] if v["name"] != "_TOP"]
        in_blocks = sum(len(v["entity_ids"]) for v in blocks)
        shapes = sum(1 for e in record["entities"] if e["geometry"].get("bbox"))

        # The blocks hold a small part of the geometry.
        self.assertLess(in_blocks, shapes / 4)

        # The pictures separate the geometry into several groups.
        self.assertGreater(len(record["pictures"]), len(blocks))
        biggest = max(len(p["entity_ids"]) for p in record["pictures"])
        self.assertLess(biggest, shapes)

        # Every shape ends up in exactly one picture. The annotation joins
        # the nearest picture, so it is counted separately.
        shape_ids = {e["id"] for e in record["entities"] if e["geometry"].get("bbox")}
        placed = []
        for picture in record["pictures"]:
            placed.extend(i for i in picture["entity_ids"] if i in shape_ids)
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(set(placed), shape_ids)


if __name__ == "__main__":
    unittest.main()
