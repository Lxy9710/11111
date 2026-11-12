"""Does the layout-corrected correspondence find a meaningful displacement?

The raw displacement between two process drawings is explained entirely by
the offset of the drawings' centres, because each drawing is laid out
independently. This removes that offset through the view correspondence
and measures what is left.

The question is whether the residual is small and consistent, which would
say the correspondence is right and the drawings agree about the
geometry, or large and scattered, which would say the correspondence is
wrong.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.correspondence import correspond  # noqa: E402
from farmcad.data.geometry import merge_process_only_steps  # noqa: E402

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


for folder in sorted(p for p in FAXIN.iterdir() if p.is_dir()):
    part = folder.name
    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])
    raw = [{"label": step_of(p, part)[1], "data": load(p)} for p in files]
    merged = merge_process_only_steps(raw)
    if len(merged) < 2:
        continue

    print(f"\n=== {part}")
    for i in range(len(merged) - 1):
        result = correspond(merged[i]["data"], merged[i + 1]["data"])
        residuals = [m["residual_mm"] for m in result["moved"]]
        all_residuals = []
        for entry in result["entities"]:
            all_residuals.extend(m["residual_mm"] for m in entry["result"]["matches"])
        view_matches = len(result["views"]["matches"])
        unmatched_after = len(result["views"]["unmatched_after"])
        print(f"  {merged[i]['label'][:13]:<14s} -> {merged[i+1]['label'][:13]:<14s} "
              f"视图配对 {view_matches} (未配 {unmatched_after})  "
              f"实体配对 {result['matched_entities']}  "
              f"新增 {len(result['added'])}  移除 {len(result['removed'])}  "
              f"残余位移>阈 {len(result['moved'])}")
        if all_residuals:
            all_residuals.sort()
            print(f"      残余位移: 中位 {all_residuals[len(all_residuals) // 2]:.3f}mm  "
                  f"90分位 {all_residuals[int(len(all_residuals) * 0.9)]:.3f}mm  "
                  f"最大 {max(all_residuals):.3f}mm")
        if residuals:
            residuals.sort()
            print(f"      判定为移动的: 中位 {residuals[len(residuals) // 2]:.3f}mm  "
                  f"最大 {max(residuals):.3f}mm")
