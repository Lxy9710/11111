from __future__ import annotations

import unittest

from farmcad.data.geometry import (
    cascade_trace,
    collect_entities,
    contour_radius,
    dimension_span,
    dimension_truth,
    has_part_contour,
    is_machining_step,
    merge_process_only_steps,
    pair_across_steps,
    pair_geometry_across_steps,
    plot_scale,
    profile_delta,
    radial_profile,
    track_contour,
)


def _line(start, end, handle="1"):
    return {"type": "LINE", "start": list(start) + [0.0],
            "end": list(end) + [0.0], "handle": handle}


def _arc(center, radius, handle="1"):
    return {"type": "ARC", "center": list(center) + [0.0],
            "radius": radius, "handle": handle}


def _dimension(first, second, text, measurement, handle="1"):
    return {
        "type": "DIMENSION",
        "dim_type": "AcDbRotatedDimension",
        "ext_line1_point": list(first) + [0.0],
        "ext_line2_point": list(second) + [0.0],
        "text": text,
        "measurement": measurement,
        "handle": handle,
    }


def _drawing(entities, blocks=None):
    return {
        "entities": entities,
        "blocks": blocks or {},
        "groups": ["default"],
        "layers": {},
    }


class CollectEntitiesTests(unittest.TestCase):
    def test_includes_block_contents(self):
        data = _drawing([_line((0, 0), (1, 0))], {
            "_U0": {"entities": [_arc((5, 5), 12.0)]},
            "_U1": {"entities": [_line((2, 2), (3, 3))]},
        })
        entities = collect_entities(data)
        self.assertEqual(len(entities), 3)
        self.assertEqual({e["type"] for e in entities}, {"LINE", "ARC"})

    def test_tolerates_malformed_input(self):
        data = {"entities": [None, "x", {"type": "LINE"}], "blocks": {"a": "bad"}}
        entities = collect_entities(data)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "LINE")


class DimensionTruthTests(unittest.TestCase):
    def test_span_measures_between_extension_points(self):
        dim = _dimension((0, 0), (30, 0), "", 30.0)
        self.assertEqual(dimension_span(dim), 30.0)

    def test_truth_recovers_millimetres(self):
        data = _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (150, 0), "\\C2;50", 150.0, "b"),
        ])
        entries = dimension_truth(data)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["truth_mm"], 100.0)
        self.assertEqual(entries[0]["scale"], 3.0)
        self.assertTrue(entries[0]["measurement_matches_span"])

    def test_truth_marks_span_disagreement(self):
        data = _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (150, 0), "\\C2;50", 150.0, "b"),
            _dimension((0, 0), (30, 0), "\\C2;10", 999.0, "c"),
        ])
        entries = dimension_truth(data)
        self.assertFalse(entries[-1]["measurement_matches_span"])


class PlotScaleTests(unittest.TestCase):
    def test_uses_the_mode_of_measurements(self):
        data = _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (150, 0), "\\C2;50", 150.0, "b"),
            _dimension((0, 0), (450, 0), "\\C2;150", 450.0, "c"),
        ])
        self.assertEqual(plot_scale(data), 3.0)

    def test_ignores_tiny_displayed_values(self):
        data = _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (150, 0), "\\C2;0.05", 150.0, "b"),
        ])
        self.assertEqual(plot_scale(data), 3.0)

    def test_is_none_without_a_clear_majority(self):
        data = _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (100, 0), "\\C2;70", 100.0, "b"),
        ])
        self.assertIsNone(plot_scale(data))

    def test_the_scale_comes_from_measurement_not_the_span(self):
        # The extension points carry an arrow offset, so the span only
        # approximates the measured length. The scale must follow the
        # measurement.
        data = _drawing([
            _dimension((0, 0), (299.0, 0), "\\C2;100", 300.0, "a"),
            _dimension((0, 0), (149.0, 0), "\\C2;50", 150.0, "b"),
        ])
        self.assertEqual(plot_scale(data), 3.0)


class PairingTests(unittest.TestCase):
    def test_matches_by_measured_geometry(self):
        before = _drawing([_dimension((0, 0), (300, 0), "\\C2;100", 300.0)])
        after = _drawing([_dimension((500, 400), (800, 400), "\\C2;100", 300.0)])
        result = pair_across_steps(before, after)
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["changed"], [])

    def test_detects_a_value_change(self):
        before = _drawing([_dimension((0, 0), (300, 0), "\\C2;100", 300.0)])
        after = _drawing([_dimension((0, 0), (300, 0), "\\C2;90", 270.0)])
        result = pair_across_steps(before, after)
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0]["before_value"], 100.0)
        self.assertEqual(result["changed"][0]["after_value"], 90.0)

    def test_reports_additions_and_removals(self):
        before = _drawing([_dimension((0, 0), (300, 0), "\\C2;100", 300.0)])
        after = _drawing([_dimension((0, 0), (150, 0), "\\C2;50", 150.0)])
        result = pair_across_steps(before, after)
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(result["pairs"], [])

    def test_matches_across_different_plot_scales(self):
        # The same feature drawn at two scales: the spans differ by the
        # ratio of the scales, but the measured lengths agree, so the pair
        # must be found and reported as unchanged.
        before = _drawing([_dimension((0, 0), (300, 0), "\\C2;100", 300.0)])
        after = _drawing([_dimension((0, 0), (200, 0), "\\C2;100", 200.0)])
        result = pair_across_steps(before, after)
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["changed"], [])

    def test_tolerates_drafting_slop_in_the_span(self):
        before = _drawing([_dimension((0, 0), (300.0, 0), "\\C2;100", 300.0)])
        after = _drawing([_dimension((0, 0), (300.2, 0), "\\C2;100", 300.0)])
        result = pair_across_steps(before, after)
        self.assertEqual(len(result["pairs"]), 1)

    def test_geometry_pairing_counts_shape_changes(self):
        before = _drawing([_line((0, 0), (10, 0)), _arc((5, 5), 3.0)])
        after = _drawing([_line((0, 0), (10, 0)), _arc((5, 5), 4.0)])
        result = pair_geometry_across_steps(before, after)
        self.assertEqual(result["common"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertTrue(result["changed"])


class ContourTests(unittest.TestCase):
    def test_radius_and_detection(self):
        drawing = _drawing([_arc((0, 0), 72.5)])
        self.assertEqual(contour_radius(drawing), 72.5)
        self.assertTrue(has_part_contour(drawing))

    def test_small_curves_are_tooling_not_the_part(self):
        drawing = _drawing([_arc((0, 0), 4.5)])
        self.assertFalse(has_part_contour(drawing))

    def test_track_reports_deltas_only_between_drawn_steps(self):
        steps = [
            {"label": "数控车5", "data": _drawing([_arc((0, 0), 72.5)])},
            {"label": "热处理25", "data": _drawing([_arc((0, 0), 4.5)])},
            {"label": "精车10", "data": _drawing([_arc((0, 0), 32.8)])},
        ]
        trajectory = track_contour(steps)
        self.assertIsNone(trajectory[0]["delta"])
        self.assertFalse(trajectory[1]["has_contour"])
        self.assertIsNone(trajectory[1]["delta"])
        self.assertAlmostEqual(trajectory[2]["delta"], 32.8 - 72.5)


class RadialProfileTests(unittest.TestCase):
    def test_normalises_by_the_plot_scale(self):
        # A radius of 90 at a scale of 3 is a 30 mm feature.
        drawing = _drawing([_arc((0, 0), 90.0)])
        profile = radial_profile(drawing, 3.0)
        self.assertEqual(profile["max"], 30.0)
        self.assertEqual(profile["distinct"], 1)

    def test_counts_repeated_radii(self):
        drawing = _drawing([
            _arc((0, 0), 90.0, "a"), _arc((10, 0), 90.0, "b"),
            _arc((20, 0), 45.0, "c"),
        ])
        profile = radial_profile(drawing, 3.0)
        self.assertEqual(profile["radii"], {30.0: 2, 15.0: 1})
        self.assertEqual(profile["total"], 3)

    def test_ignores_small_curves(self):
        drawing = _drawing([_arc((0, 0), 4.5)])
        self.assertEqual(radial_profile(drawing, 3.0)["distinct"], 0)

    def test_profile_delta_separates_removed_from_added(self):
        before = radial_profile(_drawing([
            _arc((0, 0), 90.0), _arc((0, 0), 45.0)]), 3.0)
        after = radial_profile(_drawing([
            _arc((0, 0), 90.0), _arc((0, 0), 60.0)]), 3.0)
        delta = profile_delta(before, after)
        self.assertEqual(list(delta["removed_radii"]), [15.0])
        self.assertEqual(list(delta["added_radii"]), [20.0])
        self.assertEqual(list(delta["common_radii"]), [30.0])
        self.assertTrue(delta["changed"])


class StepMergingTests(unittest.TestCase):
    def test_separates_process_markers(self):
        self.assertTrue(is_machining_step("数控车5"))
        self.assertTrue(is_machining_step("精车10"))
        self.assertTrue(is_machining_step("外磨35"))
        self.assertFalse(is_machining_step("热处理25"))
        self.assertFalse(is_machining_step("表面处理60"))
        self.assertFalse(is_machining_step("磁粉检测40"))

    def test_collapses_markers(self):
        steps = [
            {"label": "数控车5", "data": _drawing([_arc((0, 0), 72.5)])},
            {"label": "热处理25", "data": _drawing([_arc((0, 0), 72.5)])},
            {"label": "表面处理60", "data": _drawing([_arc((0, 0), 72.5)])},
            {"label": "精车10", "data": _drawing([_arc((0, 0), 32.8)])},
        ]
        merged = merge_process_only_steps(steps)
        self.assertEqual([s["label"] for s in merged], ["数控车5", "精车10"])
        self.assertEqual(merged[0]["route"], ["数控车5", "热处理25", "表面处理60"])
        self.assertEqual(merged[1]["route"], ["精车10"])
        self.assertTrue(merged[0]["machining"])
        self.assertTrue(merged[1]["machining"])

    def test_keeps_a_leading_marker(self):
        steps = [
            {"label": "下料0", "data": _drawing([])},
            {"label": "数控车5", "data": _drawing([_arc((0, 0), 72.5)])},
        ]
        merged = merge_process_only_steps(steps)
        self.assertEqual([s["label"] for s in merged], ["下料0", "数控车5"])


class CascadeTraceTests(unittest.TestCase):
    def test_finds_a_decreasing_dimension(self):
        steps = [
            {"label": "数控车5", "data": _drawing([
                _dimension((0, 0), (300, 0), "\\C2;100", 300.0)])},
            {"label": "外磨35", "data": _drawing([
                _dimension((0, 0), (300, 0), "\\C2;95", 285.0)])},
        ]
        traces = cascade_trace(steps)
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0]["decreasing"])
        self.assertEqual([t["value"] for t in traces[0]["trajectory"]],
                         [100.0, 95.0])

    def test_needs_two_steps_to_appear(self):
        steps = [{"label": "数控车5", "data": _drawing([
            _dimension((0, 0), (300, 0), "\\C2;100", 300.0)])}]
        self.assertEqual(cascade_trace(steps), [])

    def test_flags_a_non_monotone_run(self):
        steps = [
            {"label": "数控车5", "data": _drawing([
                _dimension((0, 0), (300, 0), "\\C2;8.5", 300.0)])},
            {"label": "外磨30", "data": _drawing([
                _dimension((0, 0), (300, 0), "\\C2;8.2", 300.0)])},
            {"label": "热处理35", "data": _drawing([
                _dimension((0, 0), (300, 0), "\\C2;8.25", 300.0)])},
        ]
        traces = cascade_trace(steps)
        self.assertTrue(traces[0]["changed"])
        self.assertFalse(traces[0]["decreasing"])


if __name__ == "__main__":
    unittest.main()
