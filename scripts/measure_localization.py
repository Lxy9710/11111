"""Measure how well the operations localise onto the nominal design.

This is the experiment that decides whether topology-aware geometric
localisation is well posed on this corpus. Every dimension an operation
states is matched to a dimension of the nominal design drawing, on the
measured length rather than on the position, with each drawing normalised
by its own plot scale.

The numbers that matter:

* the match rate, which says how much of an operation can be located;
* how many matches carry a different value, which is the material change
  the operation causes;
* how many nominal features the operation never mentions, which is what
  the operation deliberately leaves alone.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.geometry import merge_process_only_steps, plot_scale  # noqa: E402
from farmcad.data.localization import localise_route  # noqa: E402

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


print("=" * 104)
print("工序图 -> 名义设计图 的几何定位 (每张图按自身比例归一化)")
print("=" * 104)

summary = []
for folder in sorted(p for p in FAXIN.iterdir() if p.is_dir()):
    part = folder.name
    design_path = DESIGN / f"{part}.json"
    if not design_path.exists():
        print(f"\n  {part}: 设计图未抽取")
        continue
    design = load(design_path)
    design_scale = plot_scale(design)
    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])
    raw = [{"label": step_of(p, part)[1], "data": load(p)} for p in files]
    merged = merge_process_only_steps(raw)

    route = localise_route(design, merged)
    print(f"\n  {part}  设计图比例 {design_scale}  设计图尺寸 {route['steps'][0]['design_features'] if route['steps'] else 0} 个")
    for step in route["steps"]:
        print(f"    {step['operation'][:14]:<15s} 工序尺寸 {step['operation_features']:>3d}  "
              f"定位 {step['matched']:>3d} ({step['match_rate'] * 100:5.1f}%)  "
              f"值不同 {len(step['changed']):>2d}  "
              f"未提及名义特征 {len(step['unmatched_design']):>3d}")
    print(f"    -> 总体 {route['matched']}/{route['total_features']} "
          f"({route['match_rate'] * 100:.1f}%)  值不同 {route['changed_total']} 处")
    summary.append((part, route))

print()
print("=" * 104)
print("汇总")
print("=" * 104)
total = sum(r["total_features"] for _, r in summary)
matched = sum(r["matched"] for _, r in summary)
print(f"  零件 {len(summary)} 个")
print(f"  工序尺寸总数 {total}, 能定位到名义设计图 {matched} ({matched / max(1, total) * 100:.1f}%)")
print(f"  其中值不同(即该工序真正改动的尺寸) {sum(r['changed_total'] for _, r in summary)} 处")
for part, route in summary:
    print(f"    {part:<16s} {route['matched']:>3d}/{route['total_features']:<3d} "
          f"({route['match_rate'] * 100:5.1f}%)  值不同 {route['changed_total']:>2d}")
