"""Build the geometric state dataset from the FAXIN corpus.

The parameter chain says what number each operation sets. This builds the
counterpart: the entities each operation draws, the features they belong
to, the values the operation states, and whether it kept or changed each
one.

The correspondence between the steps of one part comes from the annotation
groups in the drawing itself, so it is exact rather than inferred.

Usage:
    python scripts/build_geo_state.py
    python scripts/build_geo_state.py --part 2798Z-01-03 --output chainmodel/data/geo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.corpus_audit import part_drawings  # noqa: E402
from farmcad.data.geo_state import build_case, check_chain  # noqa: E402

GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"

#: The route order of a drawing is the number its file name ends with.
STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")


def step_of(path: Path, part: str) -> tuple[int, str]:
    """The route position and the operation name of a drawing."""

    match = STEP.match(path.stem)
    if match:
        return int(match.group("number")), path.stem[len(part):].lstrip("-")
    return 9999, path.stem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", action="append", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    drawings = part_drawings()
    if args.part:
        wanted = set(args.part)
        drawings = {k: v for k, v in drawings.items() if k in wanted}
    if not drawings:
        print("没有可处理的零件")
        return 1

    output = args.output or (BASE / "chainmodel" / "data" / "geo")
    output.mkdir(parents=True, exist_ok=True)

    for part, files in sorted(drawings.items()):
        ordered = sorted(
            ((path, step_of(path, part)[1]) for path in files),
            key=lambda item: step_of(item[0], part)[0],
        )
        case = build_case(part, ordered)
        issues = check_chain(case)
        case["consistency_issues"] = issues
        summary = case["summary"]

        print(f"{part:<16s} 工序 {summary['steps']:>2d}  "
              f"特征 {summary['features']:>3d}  "
              f"跨工序特征 {summary['persistent_features']:>3d}  "
              f"单工序引入 {summary['step_introduced']:>3d}  "
              f"值改动 {summary['changes']:>3d}  "
              f"几何改动 {summary['geometry_changes']:>3d}  "
              f"保持 {summary['kept']:>3d}  "
              f"矛盾 {len(issues):>2d}")
        for issue in issues[:3]:
            print(f"      [{issue['kind']}] {issue['detail']}")

        target = output / f"{part.replace('/', '__')}.json"
        target.write_text(json.dumps(case, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    split = {"train": sorted(drawings)[:max(1, len(drawings) // 2)],
             "validation": sorted(drawings)[max(1, len(drawings) // 2):]}
    (output / "split.json").write_text(
        json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n训练集: {split['train']}")
    print(f"验证集: {split['validation']}")
    print(f"已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
