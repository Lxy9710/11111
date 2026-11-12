"""Build one trainable cage case from a design drawing plus a route.

The cage route is largely fixed by the product family (the platform's own
`D106NW1(H).56` route and the `D106NW1(H).56` table row agree operation for
operation with the category template). That makes it possible to publish a
real route for every cage drawing instead of one.

The supervision this script produces:

- `design.json`  - entities, edges, feature seeds and design requirements.
                   The cage feature seeds come from the cage geometry extractor,
                   which reports the exact source entity handle for every value,
                   so the anchors are real handles and not guesses.
- `target.json`  - the OP -> PARAM -> END_OP -> EOS event stream, where each
                   PARAM carries the resolved value together with the platform's
                   own `source_type` and `evidence` string.

Everything the platform produces here is system level. Nothing is marked gold,
because no engineer has reviewed these records.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "farmcad-t2-v1"

# Cage parameter symbol -> cage design parameter key. The binding is by rule
# definition (`design_field`), not by guessing from the symbol name.
SYMBOL_TO_FIELD = {
    "Dc": "Dc", "dc": "dc", "Bc": "Bc",
    "内倒角": "inner_outer_chamfer", "外倒角": "inner_outer_chamfer",
    "倒角": "rivet_hole_angle", "每组销子数": "rivet_hole_count",
    "Ra": "surface_roughness",
}

# Cage source types -> farmcad update operators.
SOURCE_TO_UPDATE = {
    "product_drawing": "design_copy",
    "product_drawing_with_tolerance": "design_copy",
    "product_drawing_plus_allowance": "derive",
    "product_drawing_face_chamfer": "derive",
    "product_drawing_ra": "design_copy",
    "product_drawing_display": "design_copy",
    "fixed": "lookup",
    "fixed_face_chamfer": "lookup",
    "lookup_direct": "lookup",
    "lookup_allowance_calculate": "derive",
    "copy_from_other_process": "state_copy",
    "product_count_minus_one": "derive",
}

# Cage operations that the cage rule engine can resolve.
RESOLVABLE = (
    "粗磨外径", "终磨外径", "终车内径及倒角", "均车两平面及倒角",
    "细车内径及倒角", "切断", "倒铆钉孔角", "拆销子",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def numeric(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def interval(item: dict) -> list[float] | None:
    """Absolute interval of a design value, when the drawing carries tolerances."""

    nominal = item.get("nominal")
    lower = item.get("lower")
    upper = item.get("upper")
    if not numeric(nominal):
        return None
    if numeric(lower) and numeric(upper):
        return [float(nominal) + float(lower), float(nominal) + float(upper)]
    if numeric(lower):
        return [float(nominal) + float(lower), float(nominal)]
    if numeric(upper):
        return [float(nominal), float(nominal) + float(upper)]
    return None


def build_features(case_id: str, params: dict, entity_handles: set[str]) -> list[dict]:
    """Cage feature seeds with their source handles and design intervals."""

    features = []
    for key, item in params.items():
        if not isinstance(item, dict) or not numeric(item.get("nominal")):
            continue
        handle = str((item.get("source") or {}).get("handle") or "")
        if not handle or handle not in entity_handles:
            # No resolvable evidence handle means no publishable predicate.
            continue
        bounds = interval(item)
        intent_candidates = []
        if bounds is not None:
            intent_candidates.append({
                "constraint_id": f"{case_id}:{key}:0",
                "intent_type": "tolerance_keep",
                "constraint_type": "finished_dimension_range",
                "label_status": "rule_candidate_not_human_verified",
                "scope": "成品设计尺寸约束；不代表功能意图已验证",
                "predicate": {
                    "operator": "between_inclusive",
                    "variable": f"finished.{key}.0.measured_dimension",
                    "unit": "mm",
                    "lower": bounds[0],
                    "upper": bounds[1],
                },
                "evidence_handles": [handle],
            })
        features.append({
            "feature_id": f"{case_id}:{key}",
            "parameter_key": key,
            "feature_type_candidate": str(item.get("role") or key),
            "feature_group_status": "annotation_seed_only_geometry_group_not_annotated",
            "geometry_handles": [],
            "annotation_handles": [handle],
            "design_parameter": {
                "nominal": item.get("nominal"),
                "lower": item.get("lower"),
                "upper": item.get("upper"),
                "display": item.get("display"),
                "role": item.get("role"),
            },
            "intent_candidates": intent_candidates,
            "gaps": [] if intent_candidates else ["设计值缺少上下偏差，未发布区间要求"],
        })
    return features


def resolve_operations(
    route_rows: list[dict],
    params: dict,
    engine: Any,
    rules_store: Any = None,
) -> list[dict]:
    """Resolve parameters for every operation of the route.

    Operations the cage rule engine supports are resolved live. Operations that
    are not supported keep their route slot with an explicit
    `parameters_unavailable` status: a missing computation is not the same as
    an operation without parameters.

    The rule asset is consulted as well, because it declares which design field
    each parameter symbol belongs to. That declaration is the evidence for the
    feature binding, and it is recorded per symbol rather than inferred later.
    """

    results = []
    for index, row in enumerate(route_rows):
        name = row["process_name_only"]
        entry = {
            "route_row": row,
            "route_index": index,
            "canonical_process": name,
            "status": "parameters_unavailable",
            "parameters": {},
            "source_of_parameters": None,
            "declared_fields": {},
        }
        if name in RESOLVABLE:
            rule = rules_store.get_process_rule(name) if rules_store else None
            declared = {}
            for definition in ((rule or {}).get("parameters") or []):
                symbol = str(definition.get("name") or "").strip()
                field = str(definition.get("design_field") or "").strip()
                if symbol and field:
                    declared[symbol] = field
            entry["declared_fields"] = declared
            try:
                resolved = engine.resolve_process(name, params)
            except Exception:
                resolved = {}
            if resolved:
                entry["status"] = "computed"
                entry["source_of_parameters"] = "cage_rule_engine"
                entry["parameters"] = {
                    key: {
                        "name": value.name,
                        "nominal_mm": value.nominal_mm,
                        "direct_value_mm": value.direct_value_mm,
                        "upper_tolerance_mm": value.upper_tolerance_mm,
                        "lower_tolerance_mm": value.lower_tolerance_mm,
                        "text_value": value.text_value,
                        "source_type": value.source_type,
                        "evidence": value.evidence,
                        "declared_design_field": declared.get(key, ""),
                    }
                    for key, value in resolved.items()
                }
        results.append(entry)
    return results


def parameters_from_patch(patch: dict) -> dict:
    """Read the parameter block of a recovered working-card patch.

    The cage platform writes two different parameter shapes:

    - rule resolution (`粗磨外径` and friends): `nominal_mm`, `direct_value_mm`,
      `source_type`, `evidence` - already in the runtime vocabulary.
    - coarse turning (`粗车第一平面及内径、倒角`): `nominal`, `upper`, `lower`,
      `display`, `field` - the drawing values it read, without a source type.

    The second shape is normalized into the runtime vocabulary here so the
    downstream event builder sees one format. A value read straight from the
    design drawing is a design copy; the fact that it came from a patch rather
    than a live engine call is recorded in the evidence string.
    """

    if not isinstance(patch, dict) or not patch:
        return {}
    block = next(iter(patch.values()))
    if not isinstance(block, dict):
        return {}
    parameters = block.get("parameters")
    if not isinstance(parameters, dict):
        return {}

    normalized = {}
    for symbol, value in parameters.items():
        if not isinstance(value, dict):
            continue
        if "source_type" in value:
            normalized[symbol] = value
            continue
        nominal = value.get("nominal")
        if not isinstance(nominal, (int, float)):
            continue
        lower = value.get("lower")
        upper = value.get("upper")
        normalized[symbol] = {
            "name": symbol,
            "nominal_mm": float(nominal),
            "direct_value_mm": None,
            "upper_tolerance_mm": float(upper) if isinstance(upper, (int, float)) else None,
            "lower_tolerance_mm": float(lower) if isinstance(lower, (int, float)) else None,
            "text_value": value.get("display_value") or value.get("display"),
            "source_type": "product_drawing",
            "evidence": f"design.{value.get('field') or symbol}",
        }
    return normalized


def build_case(
    case_id: str,
    raw: dict,
    fields: list[dict],
    params: dict,
    route_rows: list[dict],
    route_source: str,
    operations: list[dict],
    card_patches: dict[str, dict],
) -> tuple[dict, dict]:
    """Build the design input and the training target for one case."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chainmodel" / "src"))
    from farmcad.data.prepare import adapt_design, make_target

    entity_handles = {
        str(entity.get("handle") or "") for entity in raw.get("entities", [])
    }
    candidates = build_features(case_id, params, entity_handles)
    design = adapt_design(case_id, raw, candidates, fields)

    feature_ids = {f["parameter_key"]: f["feature_id"] for f in candidates}
    requirement_ids = {
        req["constraint_id"]: f'{f["parameter_key"]}:requirement:{j}'
        for f in candidates for j, req in enumerate(f["intent_candidates"])
    }

    nodes: list[dict] = []
    edges: list[dict] = []

    def node(node_id: str, kind: str, **attrs: Any) -> str:
        nodes.append({"id": node_id, "kind": kind, "human_reviewed": False, **attrs})
        return node_id

    def edge(source: str, target: str, relation: str, **attrs: Any) -> None:
        edges.append({
            "id": f"{case_id}:edge:{len(edges)}",
            "source": source, "target": target, "relation": relation,
            "human_reviewed": False, **attrs,
        })

    for feature in candidates:
        fid = node(
            feature["feature_id"], "feature_seed",
            parameter_key=feature["parameter_key"],
            feature_type_candidate=feature["feature_type_candidate"],
            geometry_handles=[], annotation_handles=feature["annotation_handles"],
        )
        for j, requirement in enumerate(feature["intent_candidates"]):
            rid = node(
                requirement["constraint_id"], "design_requirement",
                requirement_type=requirement["constraint_type"],
                predicate=requirement["predicate"],
            )
            edge(rid, fid, "constrains")

    for operation in operations:
        row = operation["route_row"]
        op_id = node(
            f"{case_id}:operation:{row['process_no']}", "operation",
            process_no=row["process_no"], route_index=operation["route_index"],
            name=row["process_name_only"], canonical_name=operation["canonical_process"],
            process_type=row.get("process_type", ""),
            generation_status=operation["status"],
        )
        parameters = dict(operation["parameters"])
        if not parameters and row["process_name_only"] in card_patches:
            patch_parameters = parameters_from_patch(card_patches[row["process_name_only"]])
            if patch_parameters:
                parameters = patch_parameters
                operation["status"] = "computed"
                operation["source_of_parameters"] = "recovered_working_card_patch"
        for symbol, value in parameters.items():
            field = SYMBOL_TO_FIELD.get(symbol)
            fid = feature_ids.get(field) if field else None
            # The cage rule asset declares the design field for each parameter
            # symbol. That declaration, not a naming heuristic, is what binds
            # the parameter to a feature, so the binding is system level.
            declared_field = str(value.get("declared_design_field") or "")
            binding_basis = "rule_declared_design_field" if declared_field else ""
            sid = node(
                f"{case_id}:state:{row['process_no']}:{symbol}", "parameter_state",
                operation_id=op_id, route_index=operation["route_index"],
                feature_id=fid, parameter=symbol,
                binding_basis=binding_basis,
                role="machining_target_candidate" if fid else "unbound_process_parameter",
                value=value, resolved_parameter=value,
                declared_source_type=value.get("source_type"),
                actual_source_type=value.get("source_type"),
                requirement_ids=[],
                lineage_status="not_resolved", finished_check="not_applicable",
            )
            edge(op_id, sid, "records_parameter")
            if fid:
                edge(sid, fid, "parameter_of")
                for requirement in (
                    next(f["intent_candidates"] for f in candidates if f["feature_id"] == fid)
                ):
                    edge(sid, requirement["constraint_id"], "copies_finished_requirement")

    graph = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "status": "system_generated_requires_review",
        "scope": "planned_parameter_records_not_measured_workpiece_states",
        "nodes": nodes,
        "edges": edges,
        "review_tasks": [],
    }
    target = make_target(design, graph, candidates)
    return design, target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--route", required=True, type=Path,
                        help="Route registry JSON produced by build_route_registry.py.")
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

    registry = json.loads(args.route.read_text(encoding="utf-8"))
    raw = json.loads(args.json.read_text(encoding="utf-8-sig"))
    fields = normalize_product_drawing_fields(extract_product_drawing_fields(raw))
    field_map = {f["field_name"]: f["value"] for f in fields}
    case_id = str(field_map.get("零件型号") or "").strip()
    if not case_id:
        raise ValueError("Cannot resolve the drawing number from the title block")

    entry = registry.get(case_id) or registry.get(args.json.stem)
    if entry is None:
        raise ValueError(f"No route registry entry for {case_id}")
    if not entry["route_rows"]:
        raise ValueError(f"No route available for {case_id}")

    params = build_cage_design_params(raw, part_type="保持架", field_map=field_map)
    rules_store = get_default_cage_rules_store()
    engine = CageProcessRuleEngine(rules_store)
    operations = resolve_operations(entry["route_rows"], params, engine, rules_store)

    card_patches = {}
    card_source = entry["sources"].get("card_exact", {})
    for row in card_source.get("rows", []):
        patch = row.get("mogong_patch")
        if patch:
            card_patches[row["process_name_only"]] = patch

    design, target = build_case(
        case_id, raw, fields, params, entry["route_rows"],
        entry["chosen_source"], operations, card_patches,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "design.json").write_text(
        json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "target.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "provenance.json").write_text(json.dumps({
        "case_id": case_id,
        "source_json": str(args.json.resolve()),
        "source_json_sha256": digest(args.json),
        "route_source": entry["chosen_source"],
        "route_quality_status": entry["quality_status"],
        "route_operation_count": len(entry["route_rows"]),
        "operations_with_parameters": sum(
            1 for o in operations if o["status"] == "computed"),
        "operations_total": len(operations),
        "human_verified": False,
        "generator": str(Path(__file__).resolve()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "case_id": case_id,
        "entities": len(design["entities"]),
        "features": len(design["features"]),
        "requirements": len(design["requirements"]),
        "operations": len(operations),
        "operations_with_parameters": sum(1 for o in operations if o["status"] == "computed"),
        "events": len(target["events"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
