"""Batch-probe cage design drawings: params + fixed route + rule resolution.

For each drawing under a test-data root this script reports, per drawing:
- the identity fields extracted from the title block,
- which cage parameters resolved (with their source handle and tolerance),
- how many operations of the fixed cage route produced parameters,
- which operations the cage rule engine does not support.

It writes one consolidated JSON report so the same script can later be reused
as a pre-flight check before a real dataset build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def classify(stem: str) -> str:
    upper = stem.strip().upper()
    if upper.endswith(".56") or upper.endswith(".66"):
        return "cage"
    if upper.endswith(".01"):
        return "outer_ring"
    if upper.endswith(".02"):
        return "inner_ring"
    return "undetermined"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
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

    engine = CageProcessRuleEngine(get_default_cage_rules_store())
    report = []
    for path in sorted(args.root.rglob("*.json")):
        category = classify(path.stem)
        if category != "cage":
            continue
        entry = {"file": str(path), "stem": path.stem, "category": category}
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            fields = normalize_product_drawing_fields(extract_product_drawing_fields(raw))
            field_map = {f["field_name"]: f["value"] for f in fields}
            entry["case_id"] = field_map.get("零件型号")
            entry["product"] = field_map.get("产品名称")
            entry["material"] = field_map.get("材料牌号")
            params = build_cage_design_params(raw, part_type="保持架", field_map=field_map)
        except Exception as exc:
            entry["error"] = f"extract: {exc}"
            report.append(entry)
            continue

        entry["pocket_shape"] = params.get("pocket_shape")
        entry["product_kind"] = params.get("product_kind")
        entry["rivet_hole_count"] = params.get("rivet_hole_count")
        entry["params"] = {
            key: {
                "nominal": value.get("nominal"),
                "lower": value.get("lower"),
                "upper": value.get("upper"),
                "display": value.get("display"),
                "handle": (value.get("source") or {}).get("handle"),
            }
            for key, value in params.items()
            if isinstance(value, dict) and value.get("nominal") is not None
        }

        resolved_ops = {}
        unsupported = []
        for name in ["粗磨外径", "终磨外径", "终车内径及倒角", "均车两平面及倒角",
                     "细车内径及倒角", "切断", "倒铆钉孔角", "拆销子"]:
            try:
                resolved = engine.resolve_process(name, params)
            except Exception:
                unsupported.append(name)
                continue
            resolved_ops[name] = {
                key: {
                    "nominal_mm": value.nominal_mm,
                    "direct_value_mm": value.direct_value_mm,
                    "lower_tolerance_mm": value.lower_tolerance_mm,
                    "upper_tolerance_mm": value.upper_tolerance_mm,
                    "source_type": value.source_type,
                    "evidence": value.evidence,
                }
                for key, value in resolved.items()
            }
        entry["resolved_operations"] = resolved_ops
        entry["unsupported_operations"] = unsupported
        entry["resolved_parameter_count"] = sum(len(v) for v in resolved_ops.values())
        report.append(entry)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for entry in report:
        if "error" in entry:
            print(f'FAIL {entry["stem"]:28s} {entry["error"][:90]}')
            continue
        print(f'OK   {entry["case_id"]:26s} {str(entry.get("pocket_shape")):8s} '
              f'rivets={entry.get("rivet_hole_count")} params={len(entry["params"]):2d} '
              f'op_params={entry["resolved_parameter_count"]:2d} '
              f'unsupported={len(entry["unsupported_operations"])}')


if __name__ == "__main__":
    main()
