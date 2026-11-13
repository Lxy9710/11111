"""Tests for the geometric state chain.

The chain claims that the feature number in a drawing's annotation group
is the identity of a feature of the part, and that the geometry a feature
is drawn with changes as the route proceeds. Both claims are checked here
against the corpus, because the whole approach rests on them.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from farmcad.data.corpus_audit import part_drawings
from farmcad.data.geo_state import build_case, check_chain

BASE = Path(__file__).resolve().parents[2]
GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"

STEP_SUFFIX = ("下料", "数控车", "精车", "热处理", "外磨", "珩研", "磁粉",
               "清洗", "表面处理", "车研", "车", "精线切")


def _ordered(part: str):
    """The drawings of a part, in route order."""

    files = part_drawings()[part]
    def order(path: Path) -> int:
        digits = "".join(c for c in path.stem if c.isdigit())
        return int(digits) if digits else 9999
    return sorted(files, key=order)


@unittest.skipUnless(FAXIN.is_dir(), "FAXIN corpus is not available")
class ChainTests(unittest.TestCase):
    def test_the_chain_of_every_part_builds(self):
        for part in part_drawings():
            files = _ordered(part)
            labels = [p.stem[len(part):].lstrip("-") for p in files]
            case = build_case(part, list(zip(files, labels)))
            self.assertEqual(case["summary"]["steps"], len(files))
            self.assertGreater(case["summary"]["features"], 0)

    def test_the_feature_numbers_of_a_part_are_contiguous(self):
        for part in part_drawings():
            files = _ordered(part)
            labels = [p.stem[len(part):].lstrip("-") for p in files]
            case = build_case(part, list(zip(files, labels)))
            ids = sorted(int(k) for k in case["feature_registry"])
            self.assertEqual(ids, list(range(min(ids), max(ids) + 1)),
                             f"{part} 的特征号不连续")

    def test_a_feature_that_persists_is_drawn_in_more_than_one_step(self):
        """The chain must be able to say that a feature is the same one.

        A feature that appears in several steps is the correspondence
        between them: it is the same feature of the part, drawn again.
        """

        part = "2798Z-01-03"
        files = _ordered(part)
        labels = [p.stem[len(part):].lstrip("-") for p in files]
        case = build_case(part, list(zip(files, labels)))
        persistent = [k for k, v in case["feature_registry"].items()
                      if v["persistent"]]
        self.assertGreater(len(persistent), 5)

    def test_the_geometry_of_a_feature_changes_over_the_route(self):
        """The chain must carry a geometric change, not only a value change.

        If the drawn geometry of every feature were constant across the
        route, the chain would add nothing to the parameter chain.
        """

        total = 0
        for part in part_drawings():
            files = _ordered(part)
            labels = [p.stem[len(part):].lstrip("-") for p in files]
            case = build_case(part, list(zip(files, labels)))
            total += case["summary"]["geometry_changes"]
        self.assertGreater(total, 20)

    def test_the_chain_has_no_contradictions(self):
        """The corpus itself must be self-consistent.

        A contradiction in the source drawings would mean the ground truth
        is wrong, so it has to be reported rather than trained on.
        """

        for part in part_drawings():
            files = _ordered(part)
            labels = [p.stem[len(part):].lstrip("-") for p in files]
            case = build_case(part, list(zip(files, labels)))
            issues = check_chain(case)
            self.assertEqual(issues, [],
                             f"{part}: {[i['detail'] for i in issues]}")

    def test_a_step_that_annotates_nothing_does_not_break_the_chain(self):
        """A heat treatment step carries no feature.

        The chain must compare against the last step that actually drew
        the feature, so that a step which annotates nothing does not make
        every feature look new again.

        A step is also allowed not to redraw a feature it drew before, so
        the test is about the features that *are* drawn on both sides: they
        must be classified, not dropped.
        """

        part = "2798Z-01-03"
        files = _ordered(part)
        labels = [p.stem[len(part):].lstrip("-") for p in files]
        case = build_case(part, list(zip(files, labels)))

        empty = [s for s in case["steps"] if not s["features"]]
        self.assertTrue(empty, "the corpus has steps that annotate nothing")

        checked = 0
        for index, step in enumerate(case["steps"]):
            if step["features"]:
                continue
            if index == 0 or index + 1 >= len(case["steps"]):
                continue
            before = {s["feature"] for s in case["steps"][index - 1]["features"]}
            following = case["steps"][index + 1]
            after = {s["feature"] for s in following["features"]}
            shared = before & after
            if not shared:
                continue
            classified = set(following["kept"]) | set(following["changed"])
            self.assertTrue(shared <= classified,
                            "the chain lost its history at an empty step")
            checked += 1

        self.assertGreater(checked, 0,
                           "the corpus has no case that exercises this")


if __name__ == "__main__":
    unittest.main()
