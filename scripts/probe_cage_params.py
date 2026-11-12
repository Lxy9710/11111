"""Probe what the cage platform can resolve for one design drawing.

Reads the extracted CAD JSON, builds the cage design parameters, resolves the
fixed cage route through the cage rule engine, and reports what each operation
yields. Nothing is written back to CAD; this only establishes what supervision
signal is actually available.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    project = args.project_root.resolve(strict=True)
    sys.path.insert(0, str(project))

    from src.BJ.table_process.product_field_extraction import (
        extract_product_drawing_fields,
        normalize_product_drawing_fields,
    )
    from src.BJ.table_process.product_geometry_extraction import (
        build_cage_design_params,
    )
    from src.BJ.cage.engine import CageProcessRuleEngine
    from src.BJ.cage.rules_store import get_default_cage_rules_store

    raw = json.loads(args.json.read_text(encoding="utf-8-sig"))
    fields = normalize_product_drawing_fields(extract_product_drawing_fields(raw))
    field_map = {f["field_name"]: f["value"] for f in fields}

    params = build_cage_design_params(raw, part_type="保持架", field_map=field_map)
    engine = CageProcessRuleEngine(get_default_cage_rules_store())

    report = {
        "json": str(args.json),
        "case_id": field_map.get("零件型号"),
        "product": field_map.get("产品名称"),
        "material": field_map.get("材料牌号"),
        "design_part_params": params,
        "processes": [],
    }
    for name in ["总图", "粗车第一平面及内径、倒角", "粗磨外径", "终磨外径",
                 "终车内径及倒角", "钻铆钉孔", "钻铣孔", "光饰", "终检", "镀银", "包装"]:
        try:
            resolved = engine.resolve_process(name, params)
        except Exception as exc:
            report["processes"].append({"name": name, "error": str(exc)})
            continue
        report["processes"].append({
            "name": name,
            "parameters": {
                key: {
                    "nominal_mm": value.nominal_mm,
                    "direct_value_mm": value.direct_value_mm,
                    "lower_tolerance_mm": value.lower_tolerance_mm,
                    "upper_tolerance_mm": value.upper_tolerance_mm,
                    "source_type": value.source_type,
                    "evidence": value.evidence,
                }
                for key, value in resolved.items()
            },
        })

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text[:4000])


if __name__ == "__main__":
    main()
