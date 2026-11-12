from __future__ import annotations

import unittest

from farmcad.data.correspondence import (
    correspond,
    match_view_entities,
    match_views,
)
from farmcad.data.topology import Entity


def _line(a, b, handle="1"):
    return Entity(handle=handle, kind="LINE", layer="1", points=[a, b])


def _view(entities):
    return {"VIEW": entities}


class MatchViewTests(unittest.TestCase):
    def test_matches_two_views_of_the_same_part(self):
        before = _view([_line((0, 0), (10, 0), "a"), _line((10, 0), (10, 10), "b")])
        after = _view([_line((500, 500), (510, 500), "c"),
                       _line((510, 500), (510, 510), "d")])
        result = match_views(before, after)
        self.assertEqual(len(result["matches"]), 1)

    def test_does_not_match_views_that_share_nothing(self):
        before = _view([_line((0, 0), (10, 0), "a")])
        after = _view([_line((0, 0), (37, 11), "c"),
                       _line((37, 11), (52, 3), "d")])
        result = match_views(before, after)
        self.assertEqual(result["matches"], [])

    def test_reports_the_layout_offset(self):
        before = _view([_line((0, 0), (10, 0), "a")])
        after = _view([_line((100, 0), (110, 0), "c")])
        result = match_views(before, after)
        self.assertEqual(len(result["matches"]), 1)
        self.assertAlmostEqual(result["matches"][0]["offset"][0], 100.0)


class MatchViewEntityTests(unittest.TestCase):
    def test_an_unmoved_entity_has_no_residual(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((100, 0), (110, 0), "c")]
        result = match_view_entities(before, after, (100.0, 0.0))
        self.assertEqual(len(result["matches"]), 1)
        self.assertAlmostEqual(result["matches"][0]["residual_mm"], 0.0)
        self.assertFalse(result["matches"][0]["moved"])

    def test_a_moved_entity_reports_the_residual(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((100, 3), (110, 3), "c")]
        result = match_view_entities(before, after, (100.0, 0.0))
        self.assertEqual(len(result["matches"]), 1)
        self.assertAlmostEqual(result["matches"][0]["residual_mm"], 3.0)
        self.assertTrue(result["matches"][0]["moved"])

    def test_reports_added_and_removed_entities(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((100, 0), (110, 0), "c"),
                 _line((100, 5), (110, 5), "d")]
        result = match_view_entities(before, after, (100.0, 0.0))
        self.assertEqual(result["added"], ["d"])
        self.assertEqual(result["removed"], [])


class CorrespondTests(unittest.TestCase):
    def test_removes_the_layout_offset_from_the_residual(self):
        # The same view, drawn in a different place, with one entity
        # moved relative to the view.
        before = {
            "entities": [
                {"type": "LINE", "handle": "a", "layer": "1",
                 "start": [0.0, 0.0, 0.0], "end": [10.0, 0.0, 0.0]},
                {"type": "LINE", "handle": "b", "layer": "1",
                 "start": [0.0, 10.0, 0.0], "end": [10.0, 10.0, 0.0]},
            ],
            "blocks": {},
        }
        after = {
            "entities": [
                {"type": "LINE", "handle": "c", "layer": "1",
                 "start": [500.0, 500.0, 0.0], "end": [510.0, 500.0, 0.0]},
                {"type": "LINE", "handle": "d", "layer": "1",
                 "start": [500.0, 514.0, 0.0], "end": [510.0, 514.0, 0.0]},
            ],
            "blocks": {},
        }
        result = correspond(before, after)
        self.assertEqual(result["matched_entities"], 2)
        residuals = sorted(m["residual_mm"] for m in result["moved"])
        self.assertEqual(len(residuals), 1)
        self.assertAlmostEqual(residuals[0], 4.0, places=3)


if __name__ == "__main__":
    unittest.main()
