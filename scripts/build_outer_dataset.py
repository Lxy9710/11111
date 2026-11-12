"""Batch outer-ring weak supervision, with explicit exclusions and human-review exports."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from build_pilot_pair import WarningCollector, build_feature_candidates, digest, save_json
from build_process_chain import build_graph, chain_rows, save_csv, summarize, validate_graph

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))
from farmcad.data.prepare import adapt_design, make_target
from farmcad.data.schema import VERSION
from farmcad.utils import object_hash, read_json, write_json


def normalize_filename(stem: str) -> str:
    return re.sub(r"^(?:（内部）|\(内部\))", "", stem).strip()


def identity_status(stem: str, case_id: str) -> str:
    if stem == case_id:
        return "exact"
    if normalize_filename(stem) == case_id:
        return "internal_prefix_removed_requires_review"
    return "mismatch_requires_review"


def known_grade(fields: dict) -> bool:
    return any(str(fields.get(key, "")).upper() in {"G", "E", "D", "C"}
               for key in ("精度等级", "产品等级", "等级", "精度")) or bool(
                   re.match(r"^[GEDC]\d", str(fields.get("零件型号", "")).upper()))


def field_map(raw: dict, project: Path) -> tuple[list[dict], dict]:
    sys.path.insert(0, str(project))
    from src.TQ.table_process.product_field_extraction import extract_product_drawing_fields, normalize_product_drawing_fields
    fields = normalize_product_drawing_fields(extract_product_drawing_fields(raw))
    return fields, {f["field_name"]: f["value"] for f in fields}


def resolve_route(
    case_id: str,
    design_parameters: dict,
    fields: dict,
    project: Path,
) -> tuple[list[dict], dict]:
    """解析路线：优先按图号精确查表，查不到则回退到类别默认路线模板。

    两条来源的证据强度不同，回退时在来源记录里显式标注，
    下游必须把模板路线当作 system 级而非 gold。
    """
    from farmcad.data.route_source import RouteResolver
    from farmcad.data.route_templates import CATEGORY_OUTER, signature_from_design

    def excel_loader(part_number: str):
        from src.TQ.process_flow.excel_process_flow import load_excel_processes_for_part
        try:
            return load_excel_processes_for_part(part_number)
        except Exception:
            return []

    resolver = RouteResolver(excel_loader=excel_loader)
    signature = signature_from_design(CATEGORY_OUTER, design_parameters, fields)
    resolved = resolver.resolve(case_id, CATEGORY_OUTER, signature)
    for warning in resolved.warnings:
        logging.warning("路线来源降级：%s", warning)
    return resolved.rows, resolved.to_dict()


def route_catalog(project: Path) -> list[dict]:
    import pandas as pd

    result = []
    default = project / "dataset" / "TQ" / "excel" / "工序选择.xlsx"
    for path in sorted((project / "dataset").rglob("工序选择*.xlsx")):
        frame = pd.read_excel(path).fillna("")
        column = next((c for c in frame.columns if str(c).strip() == "部件型号（图号）"), None)
        if column is None:
            raise ValueError(f"Route table lacks drawing-number column: {path}")
        counts = Counter(str(v).strip().upper() for v in frame[column])
        counts.pop("", None)
        counts.pop("0", None)
        result.append({
            "path": str(path.resolve()), "sha256": digest(path), "rows": len(frame),
            "is_active_outer_table": path.resolve() == default.resolve(), "part_counts": dict(counts),
        })
    return result


def inventory(drawings: Path, batch: Path, project: Path, output: Path) -> list[dict]:
    catalog = route_catalog(project)
    manifests = {}
    for path in [ROOT / "pilot" / "D207NQ.01" / "source" / "extraction_manifest.json"] + sorted(batch.glob("*/source/extraction_manifest.json")):
        man = read_json(path)
        if man["status"] == "completed":
            if digest(Path(man["json_path"])) != man["json_sha256"] or digest(Path(man["source_dwg"])) != man["source_sha256"]:
                raise ValueError(f"Extraction hash mismatch: {path}")
            manifests[str(Path(man["source_dwg"]).resolve())] = (path, man)
    rows = []
    for index, drawing in enumerate(sorted(drawings.rglob("*.dwg"))):
        row = {
            "sample_id": f"sample-{index:02d}", "source_dwg": str(drawing.resolve()),
            "source_dwg_sha256": digest(drawing), "source_relative": str(drawing.relative_to(drawings)),
            "case_id": "", "product": "", "parsed_product": "", "material": "", "entity_count": 0,
            "identity_status": "unparsed", "raw_json": "", "raw_json_sha256": "",
            "extraction_manifest": "", "extraction_status": "missing_json",
            "status": "needs_extraction", "reason": "", "grade_evidence_available": False,
            "route_matches": [],
            "entity_content_sha256": "",
        }
        existing = manifests.get(str(drawing.resolve()))
        raw_path = Path(existing[1]["json_path"]) if existing else drawing.with_suffix(".json")
        if not raw_path.exists():
            row["reason"] = "No extracted JSON; original DWG is not a tensor input."
            rows.append(row)
            continue
        row.update(raw_json=str(raw_path.resolve()), raw_json_sha256=digest(raw_path),
                   extraction_status="verified_private_extraction" if existing else "preexisting_json_pair_not_reextracted",
                   extraction_manifest=str(existing[0].resolve()) if existing else "")
        try:
            raw = read_json(raw_path)
            fields, fm = field_map(raw, project)
            case_id = str(fm.get("零件型号") or "")
            parsed_product = str(fm.get("产品名称") or "")
            product = "内外圈混合工艺参考" if "内外圈" in drawing.stem else parsed_product
            row.update(case_id=case_id, product=product, parsed_product=parsed_product, material=fm.get("材料牌号") or "",
                       entity_count=len(raw["entities"]), identity_status=identity_status(drawing.stem, case_id),
                       grade_evidence_available=known_grade(fm))
            entity_input = adapt_design(case_id or row["sample_id"], raw, [], fields)
            row["entity_content_sha256"] = object_hash(sorted(
                ({k: v for k, v in e.items() if k != "id"} for e in entity_input["entities"]),
                key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=True),
            ))
            row["route_matches"] = [
                {"path": table["path"], "sha256": table["sha256"],
                 "rows": table["part_counts"][case_id.upper()], "active_outer_table": table["is_active_outer_table"]}
                for table in catalog if case_id.upper() in table["part_counts"]
            ]
            write_json(output / "fields" / f'{row["sample_id"]}.json', fields)
            if product != "外圈":
                row.update(status="deferred_product_adapter", reason="Inner-ring/cage/mixed drawing is not compatible with the outer-ring state adapter.")
            elif not case_id or row["identity_status"] == "mismatch_requires_review":
                row.update(status="blocked_identity", reason="Drawing title and filename need human resolution.")
            elif not row["material"]:
                row.update(status="blocked_material", reason="No design material; do not use an implicit default.")
            elif not row["grade_evidence_available"]:
                row.update(status="blocked_precision", reason="Upstream grade resolver would fall back to D; expert evidence is required.")
            elif not existing:
                row.update(status="blocked_extraction_provenance", reason="Outer training inputs require verified DWG extraction provenance.")
            elif not any(m["active_outer_table"] for m in row["route_matches"]):
                row.update(status="blocked_route", reason="No exact part-number route in active table; alternative matches, if any, need explicit review.")
            else:
                row.update(status="eligible_outer_candidate", reason="System-generated weak labels still require expert review.")
        except (ValueError, KeyError, TypeError) as exc:
            logging.exception("Audit failed for %s", drawing)
            row.update(status="audit_failed", reason=str(exc))
        rows.append(row)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "inventory.json", rows)
    write_json(output / "route_catalog.json", catalog)
    save_csv(output / "inventory.csv", rows, list(rows[0]))
    write_json(output / "inventory_summary.json", {
        "drawings": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
        "products": dict(Counter(r["product"] or "unparsed" for r in rows)),
        "source_jsons": sum(bool(r["raw_json"]) for r in rows),
        "human_verified": 0,
        "duplicate_entity_content_groups": [
            [r["sample_id"] for r in rows if r["entity_content_sha256"] == signature]
            for signature, count in Counter(r["entity_content_sha256"] for r in rows if r["entity_content_sha256"]).items()
            if count > 1
        ],
    })
    return rows


def generate_case(row: dict, project: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    if row["status"] != "eligible_outer_candidate":
        raise ValueError("Cannot generate a blocked/deferred sample")
    output.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, handlers=[
        logging.FileHandler(output / "generation.log", encoding="utf-8"), logging.StreamHandler(),
    ])
    collector = WarningCollector()
    logging.getLogger().addHandler(collector)
    accessed = set()

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], str):
            path = Path(args[0])
            if path.suffix.lower() in {".json", ".xlsx", ".yml", ".yaml", ".csv"}:
                resolved = path.resolve()
                if project in resolved.parents and resolved.is_file():
                    accessed.add(resolved)
    sys.addaudithook(audit)
    raw = read_json(Path(row["raw_json"]))
    fields, fm = field_map(raw, project)
    if fm.get("零件型号") != row["case_id"] or fm.get("产品名称") != "外圈" or not known_grade(fm):
        raise ValueError("Audited design identity/product/grade changed")
    from src.TQ.table_process.product_geometry_extraction import build_design_part_params
    from src.TQ.mogong.context import infer_has_lapping, infer_precision_grade
    from src.TQ.mogong.engine import ProcessRuleEngine
    from src.TQ.mogong.Loop_Check import detect_grind_loop_from_context
    from src.TQ.mogong.process_match import resolve_mogong_process_type
    from src.TQ.mogong.rules_store import MogongRulesStore
    from src.TQ.mogong.turning_source import enrich_design_part_params_for_turning

    design_parameters = build_design_part_params(raw, ring_type="外圈", field_map=fm)
    candidates = build_feature_candidates(row["case_id"], design_parameters, raw)
    if not candidates:
        raise ValueError("No supported design attribute candidates")
    route, route_source = resolve_route(row["case_id"], design_parameters, fm, project)
    if not route:
        raise ValueError("No route; not replacing it with another part's template")
    params = enrich_design_part_params_for_turning(design_parameters, design_config_path=row["raw_json"], field_map=fm)
    params["field_map"] = fm
    grade = infer_precision_grade(fm)
    loop = asdict(detect_grind_loop_from_context(params, field_map=fm))
    has_lapping = infer_has_lapping(route)
    has_plane = any("细磨平面" in r["process_name_only"] for r in route)
    store = MogongRulesStore()
    engine = ProcessRuleEngine(store)
    processes = []
    context_diagnostics = list(collector.records)
    for route_row in route:
        name = resolve_mogong_process_type(route_row["process_name_only"], route_row)
        entry = {"route_row": route_row, "canonical_process": name,
                 "status": "outside_grinding_pilot", "parameters": {}}
        rule = store.get_process_rule(name, design_part_params=params) if name else None
        if rule is not None:
            start = len(collector.records)
            try:
                values = {k: asdict(v) for k, v in engine.resolve_process(
                    name, params, grade=grade, has_lapping=has_lapping, has_fine_grind_plane=has_plane,
                ).items()}
                missing = sorted({p["name"] for p in rule.get("parameters", [])} - set(values))
                entry.update(status="partial" if missing else ("computed" if values else "no_numeric_rule"),
                             rule=copy.deepcopy(rule), parameters=values, missing_parameters=missing,
                             warnings=collector.records[start:])
            except (ValueError, KeyError, RuntimeError) as exc:
                logging.exception("Operation generation failed: %s", route_row)
                entry.update(status="generation_failed", error=str(exc), warnings=collector.records[start:])
        processes.append(entry)
    process_doc = {
        "grade": grade, "grind_loop": loop, "has_lapping": has_lapping,
        "has_fine_grind_plane": has_plane, "context_diagnostics": context_diagnostics,
        "processes": processes, "engine_registry": engine.registry.snapshot(),
        "turning_context": params.get("turning_computed"),
        "route_source": route_source,
        "scope": "system_generated_grinding_parameters_not_full_manufacturing_truth",
    }
    docs = {
        "feature_candidates.json": candidates, "design_parameters.json": {"fields": fields, "parameters": design_parameters},
        "process_parameters.json": process_doc, "summary.json": {"case_id": row["case_id"]},
        "source_entities.json": raw,
    }
    graph = build_graph(docs)
    validate_graph(graph, docs)
    design = adapt_design(row["case_id"], raw, candidates, fields)
    target = make_target(design, graph, candidates)
    if not any(e["action"] == "PARAM" for e in target["events"]):
        raise ValueError("Route has no generated parameters; retain only in audit pool")
    source = output / "source"
    for name, data in docs.items():
        write_json(source / name, data)
    write_json(source / "association_graph.json", graph)
    write_json(output / "design.json", design)
    write_json(output / "target.json", target)
    write_json(output / "annotations.template.json", {
        "schema_version": VERSION, "case_id": row["case_id"], "reviewer": "",
        "feature_members": {}, "event_overrides": {},
    })
    save_csv(output / "review_tasks.csv", graph["review_tasks"],
             ["task_id", "target_id", "issue", "reason", "priority", "status", "reviewer", "decision", "notes"])
    chain = chain_rows(graph)
    save_csv(output / "feature_chains.csv", chain, list(chain[0]) if chain else ["case_id"])
    imported = set()
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name.startswith("src.") and filename:
            path = Path(filename).resolve()
            if project not in path.parents:
                raise ValueError(f"Cross-project import: {path}")
            if path.is_file():
                imported.add(path)
    provenance = {
        "sample": row, "project_root": str(project),
        "source_type": "verified_design_extraction_plus_system_weak_labels",
        "generator": {"path": str(Path(__file__).resolve()), "sha256": digest(Path(__file__))},
        "helpers": [{"path": str(p.resolve()), "sha256": digest(p)} for p in (
            Path(__file__).with_name("build_pilot_pair.py"), Path(__file__).with_name("build_process_chain.py"),
            ROOT / "chainmodel" / "src" / "farmcad" / "data" / "prepare.py",
        )],
        "assets": [{"path": str(p), "sha256": digest(p)} for p in sorted(accessed)],
        "imported_sources": [{"path": str(p), "sha256": digest(p)} for p in sorted(imported)],
        "design_sha256": digest(output / "design.json"), "target_sha256": digest(output / "target.json"),
    }
    if digest(Path(row["source_dwg"])) != row["source_dwg_sha256"] or digest(Path(row["raw_json"])) != row["raw_json_sha256"]:
        raise ValueError("Source changed during generation")
    write_json(output / "provenance.json", provenance)
    summary = summarize(graph)
    summary.update(sample_id=row["sample_id"], entity_count=len(design["entities"]), target_events=len(target["events"]),
                   design_hash=object_hash({k: v for k, v in design.items() if k != "case_id"}),
                   operation_errors=[p for p in processes if p["status"] == "generation_failed"])
    write_json(output / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("case_id", "entity_count", "target_events", "review_tasks")}, ensure_ascii=True))


def build_batch(audit_path: Path, project: Path, output: Path, resume: bool = False) -> None:
    if output.exists() and not resume:
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=resume)
    rows = read_json(audit_path)
    results_path = output / "generation_results.json"
    results = read_json(results_path) if results_path.exists() else []
    for row in rows:
        if row["status"] != "eligible_outer_candidate":
            continue
        destination = output / "cases" / row["sample_id"]
        if destination.exists():
            provenance = read_json(destination / "provenance.json")
            read_json(destination / "summary.json")
            for key in ("case_id", "source_dwg_sha256", "raw_json_sha256"):
                if provenance["sample"][key] != row[key]:
                    raise ValueError("Existing case does not match current audit")
            if digest(destination / "design.json") != provenance["design_sha256"] or digest(destination / "target.json") != provenance["target_sha256"]:
                raise ValueError("Existing generated case was modified")
            if not any(r["sample_id"] == row["sample_id"] and r["status"] == "generated" for r in results):
                raise ValueError("Case exists without a successful generation record")
            continue
        log = output / f'{row["sample_id"]}.log'
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([
                sys.executable, str(Path(__file__)), "worker", "--audit", str(audit_path),
                "--sample-id", row["sample_id"], "--project-root", str(project), "--output", str(destination),
            ], stdout=stream, stderr=subprocess.STDOUT)
        entry = {"sample_id": row["sample_id"], "case_id": row["case_id"],
                 "exit_code": result.returncode, "case_directory": str(destination),
                 "status": "generated" if result.returncode == 0 else "failed_see_log", "log": str(log)}
        results.append(entry)
        write_json(output / "generation_results.json", results)
        print(json.dumps(entry, ensure_ascii=True), flush=True)
        if result.returncode:
            logging.error("Case generation failed: %s; preserved log %s", row["case_id"], log)
    write_json(output / "generation_summary.json", {
        "status": "partial_failure" if any(r["exit_code"] for r in results) else "eligible_cases_generated",
        "generated": sum(r["status"] == "generated" for r in results),
        "failed": sum(r["status"] != "generated" for r in results),
        "excluded_in_audit": sum(r["status"] != "eligible_outer_candidate" for r in rows),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--drawings", type=Path, required=True)
    audit.add_argument("--batch", type=Path, required=True)
    audit.add_argument("--project-root", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    for name in ("build", "worker"):
        command = sub.add_parser(name)
        command.add_argument("--audit", type=Path, required=True)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "worker":
            command.add_argument("--sample-id", required=True)
        else:
            command.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    project = args.project_root.resolve()
    if args.command == "audit":
        if args.output.exists():
            raise FileExistsError(args.output)
        inventory(args.drawings, args.batch, project, args.output)
    elif args.command == "build":
        build_batch(args.audit, project, args.output, args.resume)
    else:
        row = next(r for r in read_json(args.audit) if r["sample_id"] == args.sample_id)
        generate_case(row, project, args.output)


if __name__ == "__main__":
    main()
