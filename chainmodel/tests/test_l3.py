from __future__ import annotations

import unittest

from farmcad.data.geometric_chain import (
    ChainEntry,
    build_chain,
    chain_coverage,
    score_chain,
)
from farmcad.data.l3_model import L3Model, build_pairs, evaluate, operation_features


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


class OperationFeatureTests(unittest.TestCase):
    def test_classifies_a_removing_operation(self):
        features = operation_features("数控车5")
        self.assertEqual(features["removing"], 1.0)
        self.assertEqual(features["adding"], 0.0)
        self.assertEqual(features["process_only"], 0.0)

    def test_classifies_a_process_only_operation(self):
        features = operation_features("热处理25")
        self.assertEqual(features["process_only"], 1.0)
        self.assertEqual(features["removing"], 0.0)


class BuildPairsTests(unittest.TestCase):
    def test_pairs_nominal_features_with_what_the_operation_says(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        operation = _drawing([
            _dimension("\\C2;95", 285.0, "o1"),
        ])
        pairs = build_pairs(design, operation)
        by_handle = {p.nominal_handle: p for p in pairs}
        self.assertEqual(len(pairs), 2)
        self.assertTrue(by_handle["d1"].stated)
        self.assertAlmostEqual(by_handle["d1"].delta_mm, -5.0)
        self.assertFalse(by_handle["d2"].stated)
        self.assertIsNone(by_handle["d2"].delta_mm)


class L3ModelTests(unittest.TestCase):
    def test_predicts_no_change_when_the_corpus_shows_none(self):
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        operation = _drawing([_dimension("\\C2;100", 300.0, "o1")])
        model = L3Model().fit([(design, "数控车5", operation)])
        prediction = model.predict(design, "数控车5", operation)
        self.assertEqual(len(prediction), 1)
        self.assertAlmostEqual(prediction[0]["predicted_value_mm"], 100.0)

    def test_the_model_cannot_infer_an_unseen_change(self):
        # The corpus shows no change for this operation kind, so the model
        # predicts none. This is the negative result: the value has to be
        # carried by the chain, not inferred.
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        operation = _drawing([_dimension("\\C2;90", 270.0, "o1")])
        model = L3Model().fit([(design, "数控车5", operation)])
        prediction = model.predict(design, "数控车5", operation)
        self.assertAlmostEqual(prediction[0]["predicted_value_mm"], 100.0)

    def test_evaluates_against_the_real_drawing(self):
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        operation = _drawing([_dimension("\\C2;100", 300.0, "o1")])
        model = L3Model().fit([(design, "数控车5", operation)])
        score = evaluate(model, [(design, "数控车5", operation)])
        self.assertEqual(score["matched"], 1)
        self.assertEqual(score["precision"], 1.0)


class GeometricChainTests(unittest.TestCase):
    def test_the_chain_carries_the_features_an_operation_touches(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        steps = [{"label": "数控车5", "data": _drawing([
            _dimension("\\C2;95", 285.0, "o1"),
        ])}]
        chain = build_chain(design, steps, "part")
        self.assertEqual(len(chain.operations), 1)
        entries = chain.operations[0].entries
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].nominal_handle, "d1")
        self.assertAlmostEqual(entries[0].stated_value_mm, 95.0)
        self.assertAlmostEqual(entries[0].delta_mm, -5.0)

    def test_the_chain_records_an_introduced_feature(self):
        design = _drawing([_dimension("\\C2;100", 300.0, "d1")])
        steps = [{"label": "数控铣7", "data": _drawing([
            _dimension("\\C2;100", 300.0, "o1"),
            _dimension("\\C2;3", 9.0, "o2"),
        ])}]
        chain = build_chain(design, steps, "part")
        entries = chain.operations[0].entries
        introduced = [e for e in entries if e.introduced]
        self.assertEqual(len(introduced), 1)
        self.assertAlmostEqual(introduced[0].stated_value_mm, 3.0)

    def test_the_chain_reproduces_the_drawing_it_was_built_from(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        steps = [{"label": "数控车5", "data": _drawing([
            _dimension("\\C2;95", 285.0, "o1"),
            _dimension("\\C2;50", 150.0, "o2"),
        ])}]
        chain = build_chain(design, steps, "part")
        score = score_chain(chain, steps)
        self.assertEqual(score["matched"], 2)
        self.assertEqual(score["precision"], 1.0)
        self.assertEqual(score["recall"], 1.0)

    def test_the_chain_covers_the_whole_drawing(self):
        design = _drawing([
            _dimension("\\C2;100", 300.0, "d1"),
            _dimension("\\C2;50", 150.0, "d2"),
        ])
        steps = [{"label": "数控车5", "data": _drawing([
            _dimension("\\C2;95", 285.0, "o1"),
            _dimension("\\C2;50", 150.0, "o2"),
        ])}]
        chain = build_chain(design, steps, "part")
        coverage = chain_coverage(chain, steps)
        self.assertEqual(coverage["covered"], 2)
        self.assertEqual(coverage["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
