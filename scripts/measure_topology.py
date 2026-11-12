"""Measure the entity-level topology checks on the corpus.

The entity-level checks answer the question the dimension-level work
cannot: whether the geometry of a route is self-consistent.

The check runs per view. A drawing is a set of views laid out side by
side, and entities from different views overlap in the projection without
overlapping on the part. On this corpus the views are the ``FCFEntity<N>``
blocks, and they are disjoint in space.

What fires and how often says which of the criteria is discriminating. A
check that fires on every view is measuring the draughting, not the
process.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.geometry import merge_process_only_steps  # noqa: E402
from farmcad.data.topology import (  # noqa: E402
    check_geometry,
    check_views,
)

GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"
STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def step_of(path, part):
    match = STEP.match(path.stem)
    if match:
        return int(match.group("number")), path.stem[len(part):].lstrip("-")
    return 9999, path.stem


print("=" * 100)
print("名义设计图: 按视图的实体级检查")
print("=" * 100)
print(f"{'零件':<16s} {'视图':>5s} {'实体':>6s} {'干涉对':>7s} {'轮廓':>5s} {'合法':>5s}")
print("-" * 100)

totals = {"views": 0, "interferences": 0, "illegal_loops": 0, "dirty_views": 0}
for path in sorted(DESIGN.glob("*.json")):
    report = check_views(load(path))
    views = report["views"]
    entity_total = sum(v["entities"] for v in views.values())
    clash_total = sum(v["interferences"] for v in views.values())
    loop_total = sum(v["loops"] for v in views.values())
    legal_total = sum(v["legal_loops"] for v in views.values())
    dirty = sum(1 for v in views.values() if not v["clean"])
    totals["views"] += len(views)
    totals["interferences"] += clash_total
    totals["illegal_loops"] += loop_total - legal_total
    totals["dirty_views"] += dirty
    print(f"{path.stem[:15]:<16s} {len(views):>5d} {entity_total:>6d} "
          f"{clash_total:>7d} {loop_total:>5d} {legal_total:>5d}"
          + (f"   <- {dirty} 个视图有问题" if dirty else ""))

print()
print("=" * 100)
print("工序路线: 视图级与路线级检查")
print("=" * 100)
route_totals = {"interference": 0, "illegal_loops": 0, "displacement": 0}
for folder in sorted(p for p in FAXIN.iterdir() if p.is_dir()):
    part = folder.name
    design_path = DESIGN / f"{part}.json"
    if not design_path.exists():
        continue
    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])
    raw = [{"label": step_of(p, part)[1], "data": load(p)} for p in files]
    merged = merge_process_only_steps(raw)

    step_clashes = 0
    step_loops = 0
    for step in merged:
        report = check_views(step["data"])
        step_clashes += len(report["interferences"])
        for view in report["views"].values():
            step_loops += view["loops"] - view["legal_loops"]

    report = check_geometry(load(design_path), merged)
    route_totals["interference"] += step_clashes
    route_totals["illegal_loops"] += step_loops
    route_totals["displacement"] += len(report["displacements"])
    print(f"\n  {part}  工序视图干涉 {step_clashes:>4d}  "
          f"非法轮廓 {step_loops:>4d}  "
          f"路线错位 {len(report['displacements']):>4d}")
    for clash in report["interferences"][:3]:
        print(f"      [干涉] {clash['operation']:<13s} {clash['a_type']}x"
              f"{clash['b_type']} 重叠 {clash['overlap_mm']}mm "
              f"层 {clash['a_layer']}/{clash['b_layer']}")
    for drift in report["displacements"][:3]:
        print(f"      [错位] {drift['from_operation']:<13s} -> "
              f"{drift['to_operation']:<13s} 残余位移 {drift['residual_mm']}mm "
              f"视图 {drift.get('before_view')}")

print()
print("=" * 100)
print("汇总")
print("=" * 100)
print(f"  19 张设计图: 视图 {totals['views']} 个, 干涉 {totals['interferences']} 对, "
      f"非法轮廓 {totals['illegal_loops']} 个, 有问题的视图 {totals['dirty_views']} 个")
for key, value in route_totals.items():
    print(f"  6 个零件路线: {key:<16s} {value}")
