"""Tests for the annotation batch preparation script.

The batch script writes one directory per case. A drawing number may contain
a path separator, and a case id may contain characters that are not valid in a
directory name, so the naming rule is pinned down here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "scripts"))

from prepare_annotation_batch import safe_name  # noqa: E402


class SafeNameTest(unittest.TestCase):

    def test_a_slash_in_the_drawing_number_is_flattened(self) -> None:
        # `6006FBC1/P4YA6.66` is a real drawing number. Using it verbatim
        # would create a nested directory and put the sheet in the wrong place.
        name = safe_name("6006FBC1/P4YA6.66")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertEqual(name, "6006FBC1__P4YA6.66")

    def test_a_backslash_is_flattened_too(self) -> None:
        self.assertNotIn("\\", safe_name("A\\B"))

    def test_an_ordinary_id_is_unchanged(self) -> None:
        self.assertEqual(safe_name("C180506K1NW1R3U(H).56"), "C180506K1NW1R3U(H).56")

    def test_a_dot_name_is_refused(self) -> None:
        for bad in (".", "..", ""):
            with self.assertRaises(ValueError):
                safe_name(bad)


if __name__ == "__main__":
    unittest.main()
