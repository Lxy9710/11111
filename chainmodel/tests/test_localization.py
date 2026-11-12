from __future__ import annotations

import unittest

from farmcad.data.localization import (
    dimension_features,
    localise_onto_design,
    localise_route,
    match_features,
    stated_value,
)
from farmcad.data.synthesis import (
    SynthesisedDimension,
    score_synthesis,
    synthesise_step,
)


def _dimension(text, measurement=None, handle="1", start=(0, 0), end=(0, 0)):
    entity = {
        "type": "DIMENSION",
        "text": text,
        "handle": handle,
        "ext_line1_point": list(start) + [0.0],
        "ext_line2_point": list(end) + [0.0],
    }
    if measurement is not None:
        entity["measurement"] = measurement
    return entity


def _drawing(entities):
    return {"entities": entities, "blocks": {}, "groups": [], "layers": {}}


class StatedValueTests(unittest.TestCase):
    def test_reads_the_leading_plain_number(self):
        self.assertEqual(stated_value({"text": "\\C2;100"}), 100.0)
        self.assertEqual(stated_value({"text": " 24.5 "}), 24.5)
        self.assertIsNone(stated_value({"text": ""}))
        self.assertIsNone(stated_value({}))

    def test_ignores_a_tolerance_suffix(self):
        self.assertEqual(stated_value({"text": "\\C2;25<>\\C1;0.01"}), 25.0)


class DimensionFeatureTests(unittest.TestCase):
    def test_records_the_stated_value(self):
        features = dimension_features(_drawing([
            _dimension("\\C2;100", 300.0, "a"),
            _dimension("\\C2;50", 150.0, "b"),
        ]))
        self.assertEqual(len(features), 2)
        self.assertEqual(features[0]["value_mm"], 100.0)
        self.assertEqual(features[0]["stored"], 300.0)

    def test_flags_a_dimension_that_does_not_annotate_geometry(self):
        # The stored length is 300 but the extension points span 30, so the
        # annotation does not describe the feature it sits on.
        features = dimension_features(_drawing([
            _dimension("\\C2;100", 300.0, "a", (0, 0), (30, 0)),
        ]))
        self.assertFalse(features[0]["annotates_geometry"])

    def test_accepts_a_dimension_that_annotates_geometry(self):
        features = dimension_features(_drawing([
            _dimension("\\C2;100", 300.0, "a", (0, 0), (300, 0)),
        ]))
        self.assertTrue(features[0]["annotates_geometry"])

    def test_skips_dimensions_without_a_stated_value(self):
        features = dimension_features(_drawing([
            _dimension("", 300.0, "a"),
        ]))
        self.assertEqual(features, [])


class MatchFeatureTests(unittest.TestCase):
    def test_matches_on_the_stated_value(self):
        source = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 900.0, "b")]))
        matches = match_features(source, target)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source_handle"], "a")
        self.assertEqual(matches[0]["target_handle"], "b")
        self.assertAlmostEqual(matches[0]["delta_mm"], 0.0)

    def test_a_difference_inside_the_change_window_still_matches(self):
        # The operation states an intermediate value of a feature it
        # changed, so it matches, but not identically.
        source = dimension_features(_drawing([_dimension("\\C2;96", 288.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "b")]))
        matches = match_features(source, target)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_kind"], "changed")

    def test_a_difference_beyond_the_change_window_does_not_match(self):
        source = dimension_features(_drawing([_dimension("\\C2;50", 150.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "b")]))
        self.assertEqual(match_features(source, target), [])

    def test_matches_a_target_at_most_once(self):
        source = dimension_features(_drawing([
            _dimension("\\C2;100", 300.0, "a"),
            _dimension("\\C2;100", 300.0, "b"),
        ]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "c")]))
        matches = match_features(source, target)
        self.assertEqual(len(matches), 1)

    def test_reports_a_value_difference(self):
        # A feature the operation changed is stated with an intermediate
        # value, so it still matches, but as a change rather than as an
        # identity.
        source = dimension_features(_drawing([_dimension("\\C2;95", 285.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "b")]))
        matches = match_features(source, target)
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0]["delta_mm"], -5.0)
        self.assertEqual(matches[0]["match_kind"], "changed")

    def test_an_unchanged_feature_matches_identically(self):
        source = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "b")]))
        matches = match_features(source, target)
        self.assertEqual(matches[0]["match_kind"], "identical")

    def test_a_value_beyond_the_change_window_does_not_match(self):
        source = dimension_features(_drawing([_dimension("\\C2;50", 150.0, "a")]))
        target = dimension_features(_drawing([_dimension("\\C2;100", 300.0, "b")]))
        self.assertEqual(match_features(source, target), [])


class LocaliseTests(unittest.TestCase):
    def test_reports_the_correspondence(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        operation = _drawing([
            _dimension("\\C2;95", 285.0, "o1"),
            _dimension("\\C2;50", 150.0, "o2"),
        ])
        result = localise_onto_design(design, operation)
        self.assertEqual(result["matched"], 2)
        # The 95 states the changed value of the 100, the 50 is unchanged.
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(len(result["kept"]), 1)

    def test_reports_the_nominal_features_the_operation_never_mentions(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        operation = _drawing([_dimension("\\C2;100", 300.0, "o1")])
        result = localise_onto_design(design, operation)
        self.assertEqual(len(result["unmatched_design"]), 1)
        self.assertEqual(result["unmatched_design"][0]["handle"], "d2")

    def test_reports_the_dimensions_the_operation_introduces(self):
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        operation = _drawing([
            _dimension("\\C2;100", 300.0, "o1"),
            _dimension("\\C2;3", 9.0, "o2"),
        ])
        result = localise_onto_design(design, operation)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(result["unmatched_operation"]), 1)
        self.assertEqual(result["unmatched_operation"][0]["value_mm"], 3.0)

    def test_localises_a_whole_route(self):
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        steps = [
            {"label": "数控车5", "data": _drawing([
                _dimension("\\C2;95", 285.0, "o1")])},
            {"label": "精车10", "data": _drawing([
                _dimension("\\C2;100", 300.0, "o2")])},
        ]
        route = localise_route(design, steps)
        self.assertEqual(route["total_features"], 2)
        self.assertEqual(route["matched"], 2)
        self.assertEqual(route["changed_total"], 1)


class SynthesisTests(unittest.TestCase):
    def test_a_parameter_on_a_nominal_feature_restates_it(self):
        nominal = [{"handle": "d1", "value_mm": 100.0}]
        step = synthesise_step("数控车5", [
            {"parameter": "外径", "features": ["d1"],
             "value": {"nominal": 100.0}, "update": "design_copy"},
        ], nominal, {"d1": nominal[0]})
        self.assertEqual(len(step.dimensions), 1)
        self.assertEqual(step.dimensions[0].value_mm, 100.0)
        self.assertEqual(step.dimensions[0].origin, "nominal")

    def test_a_parameter_that_changes_the_feature_states_the_new_value(self):
        nominal = [{"handle": "d1", "value_mm": 100.0}]
        step = synthesise_step("外磨30", [
            {"parameter": "外径", "features": ["d1"],
             "value": {"nominal": 95.0}, "update": "lookup"},
        ], nominal, {"d1": nominal[0]})
        self.assertEqual(step.dimensions[0].value_mm, 95.0)
        self.assertEqual(step.dimensions[0].origin, "modified")
        self.assertEqual(step.dimensions[0].evidence["delta"], -5.0)

    def test_a_parameter_without_a_feature_introduces_one(self):
        step = synthesise_step("数控铣7", [
            {"parameter": "槽宽", "features": [],
             "value": {"nominal": 3.0}, "update": "lookup"},
        ], [], {})
        self.assertEqual(len(step.dimensions), 1)
        self.assertEqual(step.dimensions[0].origin, "new")
        self.assertIsNone(step.dimensions[0].source_feature)

    def test_scoring_a_perfect_prediction(self):
        actual = [{"value_mm": 100.0, "handle": "a"}]
        predicted = [SynthesisedDimension(value_mm=100.0)]
        score = score_synthesis(predicted, actual)
        self.assertEqual(score["matched"], 1)
        self.assertEqual(score["precision"], 1.0)
        self.assertEqual(score["recall"], 1.0)

    def test_scoring_a_missing_dimension(self):
        actual = [{"value_mm": 100.0, "handle": "a"},
                  {"value_mm": 50.0, "handle": "b"}]
        predicted = [SynthesisedDimension(value_mm=100.0)]
        score = score_synthesis(predicted, actual)
        self.assertEqual(score["matched"], 1)
        self.assertEqual(score["precision"], 1.0)
        self.assertEqual(score["recall"], 0.5)

    def test_scoring_a_wrong_value(self):
        actual = [{"value_mm": 100.0, "handle": "a"}]
        predicted = [SynthesisedDimension(value_mm=80.0)]
        score = score_synthesis(predicted, actual)
        self.assertEqual(score["matched"], 0)
        self.assertEqual(score["f1"], 0.0)


if __name__ == "__main__":
    unittest.main()
