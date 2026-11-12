"""Build an annotation-anchored ring pilot with existing Process Hub functions.

This is not a full workflow replay or a ground-truth intent dataset. It reuses
the production design extractors, Excel route loader and grinding rule engine.
No LLM, service startup, template mutation or CAD writeback is performed.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import logging
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


# Outer-ring parameter identities used in mogong.engine; not spatial bindings.
OUTER_RING_FIELDS = {
    "D": "outer_diameter",
    "D2": "inner_diameter",
    "De": "groove_diameter",
    "C": "planar_dimension",
    "ae": "groove_position",
    "R": "groove_radius",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def numeric(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def bounds(nominal: float, lower: float, upper: float) -> list[float]:
    if not all(numeric(v) for v in (nominal, lower, upper)) or lower > upper:
        raise ValueError("尺寸及上下偏差必须是有效数值且下偏差不大于上偏差")
    base = Decimal(str(nominal))
    return [
        float(base + Decimal(str(lower))),
        float(base + Decimal(str(upper))),
    ]


def evaluate_between(predicate: dict, value: float) -> dict:
    if predicate.get("operator") != "between_inclusive" or predicate.get("unit") != "mm":
        raise ValueError("不支持的谓词或单位")
    lo, hi = predicate["lower"], predicate["upper"]
    if not all(numeric(v) for v in (lo, hi, value)) or lo > hi:
        raise ValueError("谓词边界或待检查数值无效")
    return {"value_mm": value, "lower_mm": lo, "upper_mm": hi, "satisfied": lo <= value <= hi}


def source_handles(item: dict) -> list[str]:
    sources = [item.get("source") or {}]
    sources.extend(a.get("source") or {} for a in item.get("annotations") or [])
    return sorted({str(s["handle"]) for s in sources if s.get("handle")})


def build_feature_candidates(case_id: str, design: dict, config: dict) -> list[dict]:
    entities: dict[str, dict] = {}
    for entity in config["entities"]:
        handle = str(entity.get("handle") or "")
        if handle in entities:
            raise ValueError(f"源图实体 handle 重复：{handle}")
        if handle:
            entities[handle] = entity
    result = []
    for key, item in design.items():
        if not isinstance(item, dict) or not numeric(item.get("nominal")):
            continue
        handles = source_handles(item)
        missing = [h for h in handles if h not in entities]
        if missing:
            raise ValueError(f"参数来源在源图中不存在：{key} {missing}")
        gaps = []
        if not handles:
            gaps.append("提取结果只有位置或无来源，未建立唯一实体引用")
        constraints = []
        annotations = item.get("annotations") or [item]
        for index, annotation in enumerate(annotations):
            anchor = (annotation.get("source") or {}).get("handle")
            values = [annotation.get(k) for k in ("nominal", "lower", "upper")]
            if not all(numeric(v) for v in values):
                gaps.append(f"标注{index}缺少已解析的名义值或公差，不将缺失偏差补为0")
                continue
            if not anchor or anchor not in entities:
                gaps.append(f"标注{index}未唯一绑定源图实体，暂不发布可追溯谓词")
                continue
            nominal, lower, upper = values
            lo, hi = bounds(nominal, lower, upper)
            constraints.append({
                "constraint_id": f"{case_id}:{key}:{index}",
                "intent_type": "tolerance_keep",
                "constraint_type": "finished_dimension_range",
                "label_status": "rule_candidate_not_human_verified",
                "scope": "成品设计尺寸约束；不代表公差等级、实际制造合格或功能意图已验证",
                "predicate": {
                    "operator": "between_inclusive",
                    "variable": f"finished.{key}.{index}.measured_dimension",
                    "unit": "mm",
                    "lower": lo,
                    "upper": hi,
                },
                "evidence_handles": [str(anchor)],
                "parsed_annotation": copy.deepcopy(annotation),
                "validation": {
                    "nominal_self_check": evaluate_between(
                        {"operator": "between_inclusive", "unit": "mm", "lower": lo, "upper": hi},
                        nominal,
                    ),
                    "physical_measurement_available": False,
                },
            })
        result.append({
            "feature_id": f"{case_id}:{key}",
            "parameter_key": key,
            "feature_type_candidate": item.get("role"),
            "feature_group_status": "annotation_seed_only_geometry_group_not_annotated",
            "geometry_handles": [],
            "annotation_handles": handles,
            "annotation_entities": [entities[h] for h in handles],
            "design_parameter": copy.deepcopy(item),
            "intent_candidates": constraints,
            "gaps": gaps,
        })
    return result


class WarningCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno < logging.WARNING and not any(
            word in message for word in ("暂用", "回退", "缺少", "未从")
        ):
            return
        self.records.append({
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--extraction-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = args.project_root.resolve(strict=True)
    extraction_path = args.extraction_manifest.resolve(strict=True)
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    if extraction["status"] != "completed" or Path(extraction["project_root"]).resolve() != project:
        raise ValueError("提取未完成或提取器项目版本与本次不一致")
    source = Path(extraction["json_path"])
    original = Path(extraction["source_dwg"])
    if digest(source) != extraction["json_sha256"] or digest(original) != extraction["source_sha256"]:
        raise ValueError("原图或提取 JSON 与提取凭据不一致")
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已存在的结果：{output}")
    output.mkdir(parents=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.FileHandler(output / "generation.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    warnings = WarningCollector()
    logging.getLogger().addHandler(warnings)
    sys.path.insert(0, str(project))
    accessed_assets: set[Path] = set()

    def audit_files(event: str, event_args: tuple) -> None:
        if event != "open" or not event_args or not isinstance(event_args[0], str):
            return
        path = Path(event_args[0])
        if path.suffix.lower() not in {".json", ".xlsx", ".yml", ".yaml"}:
            return
        path = path.resolve()
        if project in path.parents:
            accessed_assets.add(path)

    sys.addaudithook(audit_files)
    from src.TQ.table_process.product_field_extraction import (
        extract_product_drawing_fields,
        normalize_product_drawing_fields,
    )
    from src.TQ.table_process.product_geometry_extraction import (
        build_design_part_params,
        detect_ring_product_type,
    )
    from src.TQ.process_flow.excel_process_flow import (
        PROCESS_SELECTION_EXCEL_PATH,
        load_excel_processes_for_part,
    )
    from src.TQ.mogong.context import infer_has_lapping, infer_precision_grade
    from src.TQ.mogong.engine import ProcessRuleEngine
    from src.TQ.mogong.Loop_Check import detect_grind_loop_from_context
    from src.TQ.mogong.process_match import resolve_mogong_process_type
    from src.TQ.mogong.rules_store import MogongRulesStore
    from src.TQ.mogong.turning_source import enrich_design_part_params_for_turning

    config = json.loads(source.read_text(encoding="utf-8-sig"))
    fields = normalize_product_drawing_fields(extract_product_drawing_fields(config))
    field_map = {f["field_name"]: f["value"] for f in fields}
    case_id = field_map.get("零件型号")
    if not case_id or case_id != original.stem:
        raise ValueError("源图提取图号与文件名不一致，须人工确认")
    ring_type = detect_ring_product_type(field_map)
    if ring_type != "外圈":
        raise ValueError("本试点只验证外圈参数对应关系，不自动套用到其它产品")
    if not field_map.get("材料牌号"):
        raise ValueError("源图材料缺失，拒绝依赖材料默认值")
    if case_id[0].upper() not in "GEDC" and not field_map.get("精度等级"):
        raise ValueError("精度等级无法从输入确定")
    design = build_design_part_params(config, ring_type=ring_type, field_map=field_map)
    save_json(output / "design_parameters.json", {"fields": fields, "parameters": design})
    route = load_excel_processes_for_part(case_id)
    features = build_feature_candidates(case_id, design, config)
    params = enrich_design_part_params_for_turning(
        design, design_config_path=str(source), field_map=field_map
    )
    params["field_map"] = field_map
    grade = infer_precision_grade(field_map)
    loop = asdict(detect_grind_loop_from_context(params, field_map=field_map))
    has_lapping = infer_has_lapping(route)
    has_fine_plane = any("细磨平面" in r["process_name_only"] for r in route)
    store = MogongRulesStore()
    engine = ProcessRuleEngine(store)
    process_results = []
    context_diagnostics = list(warnings.records)
    for row in route:
        name = resolve_mogong_process_type(row["process_name_only"], row)
        entry = {
            "route_row": row,
            "canonical_process": name,
            "status": "outside_grinding_pilot",
            "parameters": {},
        }
        rule = store.get_process_rule(name, design_part_params=params) if name else None
        if rule is not None:
            start = len(warnings.records)
            resolved = engine.resolve_process(
                name, params, grade=grade, has_lapping=has_lapping,
                has_fine_grind_plane=has_fine_plane,
            )
            values = {k: asdict(v) for k, v in resolved.items()}
            expected = {p["name"] for p in rule.get("parameters", [])}
            missing = sorted(expected - values.keys())
            entry.update(
                status="partial" if missing else ("computed" if values else "no_numeric_rule"),
                rule=copy.deepcopy(rule),
                parameters=values,
                missing_parameters=missing,
                warnings=warnings.records[start:],
            )
        process_results.append(entry)
    save_json(output / "process_parameters.json", {
        "scope": "现有工序表中的磨工规则计算；非全流程工艺卡生成",
        "context_diagnostics": context_diagnostics,
        "quality_status": "requires_review_of_context_fallbacks" if context_diagnostics else "not_human_verified",
        "grade": grade,
        "grade_source": "原项目精度规则解析图号/字段",
        "grind_loop": loop,
        "has_lapping": has_lapping,
        "has_fine_grind_plane": has_fine_plane,
        "processes": process_results,
        "engine_registry": engine.registry.snapshot(),
        "turning_context": params.get("turning_computed"),
    })
    by_field = {f["parameter_key"]: f for f in features}
    pairs = []
    for proc in process_results:
        for symbol, value in proc["parameters"].items():
            field = OUTER_RING_FIELDS.get(symbol)
            if field not in by_field or value["nominal_mm"] is None:
                continue
            feature = by_field[field]
            annotation = feature["design_parameter"]
            if not feature["intent_candidates"]:
                continue
            if annotation.get("annotations"):
                continue
            generated_bounds = bounds(
                value["nominal_mm"], value["lower_tolerance_mm"], value["upper_tolerance_mm"]
            )
            predicate = feature["intent_candidates"][0]["predicate"]
            direct = (
                value["source_type"] == "product_drawing"
                and value["evidence"] == f"design.{field}"
            )
            comparison = {
                "status": "not_applicable_without_stage_contract",
                "satisfied": None,
                "reason": "中间工序可保留加工余量；非设计尺寸直接抄入项不套用成品公差",
            }
            if direct:
                comparison = {
                    "status": "direct_copy_consistency_only",
                    "satisfied": all(
                        evaluate_between(predicate, number)["satisfied"]
                        for number in generated_bounds
                    ),
                    "reason": "仅检查生成参数区间包含于图样要求；同源一致性不是独立效果验证",
                }
            pairs.append({
                "case_id": case_id,
                "feature_id": feature["feature_id"],
                "feature_group_status": feature["feature_group_status"],
                "annotation_handles": feature["annotation_handles"],
                "design_parameter": annotation,
                "intent_candidate": feature["intent_candidates"][0],
                "process_no": proc["route_row"]["process_no"],
                "process_name": proc["route_row"]["process_name_only"],
                "process_parameter": value,
                "process_target_interval_mm": generated_bounds,
                "association": {
                    "status": "parameter_role_candidate_not_manual_feature_alignment",
                    "method": "外圈磨工引擎中的参数角色对应",
                    "parameter_symbol": symbol,
                    "design_field": field,
                    "rule_feature": proc["rule"].get("feature"),
                    "source_module": "src.TQ.mogong.engine",
                },
                "comparison": comparison,
                "human_reviewed": False,
                "context_diagnostics": context_diagnostics,
            })
    if not pairs:
        raise RuntimeError("未生成有标注证据的候选数据对")
    save_json(output / "feature_candidates.json", features)
    save_json(output / "pairs.json", pairs)
    with (output / "pairs.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        columns = [
            "case_id", "feature_id", "annotation_handles", "design_nominal_mm",
            "design_lower_mm", "design_upper_mm", "process_no", "process_name",
            "parameter", "process_nominal_mm", "process_lower_mm", "process_upper_mm",
            "parameter_source", "parameter_evidence", "check_status", "check_result",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for pair in pairs:
            predicate = pair["intent_candidate"]["predicate"]
            writer.writerow(dict(zip(columns, [
                case_id, pair["feature_id"], ";".join(pair["annotation_handles"]),
                pair["design_parameter"]["nominal"], predicate["lower"], predicate["upper"],
                pair["process_no"], pair["process_name"], pair["process_parameter"]["name"],
                pair["process_parameter"]["nominal_mm"], *pair["process_target_interval_mm"],
                pair["process_parameter"]["source_type"], pair["process_parameter"]["evidence"],
                pair["comparison"]["status"], pair["comparison"]["satisfied"],
            ])))
    imported_sources = []
    for module_name, module in sorted(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if module_name.startswith("src.") and filename:
            path = Path(filename).resolve()
            if project not in path.parents:
                raise RuntimeError(f"检测到跨版本模块混入：{path}")
            if path.is_file():
                imported_sources.append({"path": str(path), "sha256": digest(path)})
    save_json(output / "provenance.json", {
        "project_root": str(project),
        "extraction_manifest": str(extraction_path),
        "extraction_manifest_sha256": digest(extraction_path),
        "input_sha256": digest(source),
        "original_dwg_sha256": digest(original),
        "route_excel": str(PROCESS_SELECTION_EXCEL_PATH),
        "rules_file": str(store.rules_path),
        "assets": [
            {"path": str(p), "sha256": digest(p)}
            for p in sorted(accessed_assets) if p.is_file()
        ],
        "imported_sources": imported_sources,
        "generator": {"path": str(Path(__file__).resolve()), "sha256": digest(Path(__file__))},
        "command": sys.argv,
    })
    summary = {
        "status": "pilot_generated_requires_human_review",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_id": case_id,
        "source_entities": len(config["entities"]),
        "route_steps": len(route),
        "computed_processes": sum(p["status"] == "computed" for p in process_results),
        "partial_processes": sum(p["status"] == "partial" for p in process_results),
        "feature_candidates": len(features),
        "manually_annotated_feature_groups": 0,
        "intent_predicate_candidates": sum(len(f["intent_candidates"]) for f in features),
        "paired_design_parameters": len({p["feature_id"] for p in pairs}),
        "pair_rows": len(pairs),
        "direct_copy_checks": sum(p["comparison"]["satisfied"] is not None for p in pairs),
        "warnings": warnings.records,
        "limits": [
            "这是单图、本地确定性阶段的数据试点，不是完整 Flask/LLM/工艺卡出图流程验收",
            "参数角色候选不等于已标注的完整实体组；geometry_handles 尚为空",
            "意图只发布有来源标注和上下偏差的成品尺寸区间候选，不推断配合功能",
            "加工参数来自系统规则，不是工程师审核真值；刀具、转速等未生成",
            "同源约束与生成参数的一致性检查不能作为意图模型的独立准确率",
            "单图无新旧版本对，未生成 GlobalID、跨图匹配、变更传播或冲突结论",
            "未加载实际细车工序卡；原引擎存在平面尺寸回退，详见 context_diagnostics，不据此宣称完整工艺正确",
        ],
        "feature_gaps": {f["feature_id"]: f["gaps"] for f in features if f["gaps"]},
    }
    if digest(original) != extraction["source_sha256"] or digest(source) != extraction["json_sha256"]:
        raise RuntimeError("运行后源文件哈希不一致")
    save_json(output / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if isinstance(v, (str, int))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
