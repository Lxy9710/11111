# -*- coding: utf-8 -*-
"""端到端验证：真实 blocked_route 图号走通「Excel 查不到 -> 类别默认路线」。

用 D208N4Q.01（活动表无精确行）与 C110K1QT2U(H).01（全网无精确行），
验证解析器能落到类别模板并产出结构合法的工序行。
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "NOVbcj" / "smartCAD" / "CAD_Process_Hub"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

from farmcad.data.route_source import RouteResolver
from farmcad.data.route_templates import CATEGORY_OUTER, signature_from_design
from src.TQ.table_process.product_field_extraction import extract_product_drawing_fields, normalize_product_drawing_fields
from src.TQ.table_process.product_geometry_extraction import build_design_part_params


def excel_loader(part_number: str):
    from src.TQ.process_flow.excel_process_flow import load_excel_processes_for_part
    try:
        return load_excel_processes_for_part(part_number)
    except Exception as exc:
        print(f"   [excel miss] {part_number}: {type(exc).__name__}: {exc}")
        return []


resolver = RouteResolver(excel_loader=excel_loader)

design_dir = PROJECT / "dataset" / "config" / "外圈" / "D208N4Q.01"
print("外圈工艺目录存在:", design_dir.exists())

cands = sorted(design_dir.glob("*.json"))
src = cands[0]
print("使用源图:", src.name)

config = json.loads(src.read_text(encoding="utf-8-sig"))
fields = normalize_product_drawing_fields(extract_product_drawing_fields(config))
fm = {f["field_name"]: f["value"] for f in fields}
print("零件型号:", fm.get("零件型号"))

params = build_design_part_params(config, ring_type="外圈", field_map=fm)
print("设计参数键:", sorted(params.keys()))

sig = signature_from_design(CATEGORY_OUTER, params, fm)
print("\n特征签名:", json.dumps(sig.to_dict(), ensure_ascii=False, indent=2))

for part in ("D208N4Q.01", "C110K1QT2U(H).01"):
    print(f"\n===== resolve {part}")
    try:
        resolved = resolver.resolve(part, CATEGORY_OUTER, sig)
    except Exception as exc:
        print("   失败:", exc)
        continue
    print("   source =", resolved.source)
    for w in resolved.warnings:
        print("   warn:", w)
    print(f"   生成 {len(resolved.rows)} 道:")
    for r in resolved.rows:
        print(f"      {r['process_no']:>4}  {r['process_name_only']}")
