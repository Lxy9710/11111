from __future__ import annotations

import unittest

from farmcad.data.topology import (
    Entity,
    check_loop_legality,
    feature_signature,
    find_interferences,
    find_loops,
    match_entities,
)


def _line(a, b, handle="1", layer="1"):
    return Entity(handle=handle, kind="LINE", layer=layer,
                  points=[a, b])


def _arc(center, radius, handle="1", layer="1"):
    return Entity(handle=handle, kind="ARC", layer=layer,
                  points=[center], radius=radius, center=center)


class InterferenceTests(unittest.TestCase):
    def test_two_lines_that_touch_at_a_point_do_not_interfere(self):
        # A rectangle's corners are lines meeting at a point, which is
        # normal in a drawing.
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((10, 0), (10, 10), "b"),
        ]
        self.assertEqual(find_interferences(entities), [])

    def test_two_lines_that_lie_on_top_of_each_other_interfere(self):
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((2, 0), (8, 0), "b"),
        ]
        clashes = find_interferences(entities)
        self.assertEqual(len(clashes), 1)
        self.assertGreater(clashes[0]["overlap_mm"], 0)

    def test_identical_lines_are_not_an_interference(self):
        # The same line drawn twice is a duplicate, not a conflict.
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((0, 0), (10, 0), "b"),
        ]
        self.assertEqual(find_interferences(entities), [])

    def test_lines_on_different_layers_do_not_interfere(self):
        entities = [
            _line((0, 0), (10, 0), "a", layer="1"),
            _line((2, 0), (8, 0), "b", layer="2"),
        ]
        self.assertEqual(find_interferences(entities), [])

    def test_parallel_lines_at_a_distance_do_not_interfere(self):
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((0, 3), (10, 3), "b"),
        ]
        self.assertEqual(find_interferences(entities), [])


class LoopTests(unittest.TestCase):
    def test_finds_a_square(self):
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((10, 0), (10, 10), "b"),
            _line((10, 10), (0, 10), "c"),
            _line((0, 10), (0, 0), "d"),
        ]
        loops = find_loops(entities)
        self.assertEqual(len(loops), 1)
        report = check_loop_legality(loops[0])
        self.assertTrue(report["legal"])
        self.assertEqual(report["self_intersections"], 0)
        self.assertAlmostEqual(report["area"], 100.0)

    def test_an_open_chain_is_not_a_loop(self):
        entities = [
            _line((0, 0), (10, 0), "a"),
            _line((10, 0), (10, 10), "b"),
        ]
        self.assertEqual(find_loops(entities), [])

    def test_a_self_crossing_contour_is_illegal(self):
        # A bow tie: the contour crosses itself, so it does not describe a
        # region.
        entities = [
            _line((0, 0), (10, 10), "a"),
            _line((10, 10), (10, 0), "b"),
            _line((10, 0), (0, 10), "c"),
            _line((0, 10), (0, 0), "d"),
        ]
        loops = find_loops(entities)
        self.assertEqual(len(loops), 1)
        report = check_loop_legality(loops[0])
        self.assertGreater(report["self_intersections"], 0)
        self.assertFalse(report["legal"])

    def test_a_zero_area_contour_is_illegal(self):
        loop = [(0, 0), (10, 0), (5, 0)]
        report = check_loop_legality(loop)
        self.assertFalse(report["legal"])


class SignatureTests(unittest.TestCase):
    def test_a_line_is_identified_by_its_length_and_direction(self):
        first = _line((0, 0), (10, 0), "a")
        second = _line((500, 500), (510, 500), "b")
        self.assertEqual(feature_signature(first), feature_signature(second))

    def test_lines_of_different_length_have_different_signatures(self):
        first = _line((0, 0), (10, 0), "a")
        second = _line((0, 0), (12, 0), "b")
        self.assertNotEqual(feature_signature(first), feature_signature(second))

    def test_a_circle_is_identified_by_its_radius(self):
        first = _arc((0, 0), 30.0, "a")
        second = _arc((100, 100), 30.0, "b")
        self.assertEqual(feature_signature(first), feature_signature(second))


class MatchEntityTests(unittest.TestCase):
    def test_matches_a_line_that_moved(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((0, 5), (10, 5), "b")]
        result = match_entities(before, after)
        self.assertEqual(len(result["matched"]), 1)
        self.assertAlmostEqual(result["matched"][0]["displacement_mm"], 5.0)
        self.assertTrue(result["matched"][0]["drifted"])

    def test_a_stable_line_does_not_drift(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((0, 0.1), (10, 0.1), "b")]
        result = match_entities(before, after)
        self.assertEqual(len(result["matched"]), 1)
        self.assertFalse(result["matched"][0]["drifted"])

    def test_reports_what_was_added_and_removed(self):
        before = [_line((0, 0), (10, 0), "a")]
        after = [_line((0, 0), (10, 0), "b"), _line((0, 5), (10, 5), "c")]
        result = match_entities(before, after)
        self.assertEqual(result["added"], ["c"])
        self.assertEqual(result["removed"], [])


if __name__ == "__main__":
    unittest.main()
