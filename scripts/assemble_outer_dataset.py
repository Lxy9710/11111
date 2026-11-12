"""Publish ready weak-label cases separately from reserved, unlabelled design splits."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from build_outer_dataset import ROOT, field_map
from build_pilot_pair import build_feature_candidates
from build_process_chain import save_csv
from farmcad.data.dataset import Vocabulary, load_cases
from farmcad.data.prepare import adapt_design
from farmcad.data.schema import VERSION
from farmcad.utils import object_hash, read_json, write_json


def proposed_family(case_id: str) -> str:
    match = re.search(r"[A-Za-z](\d+)", case_id)
    if not match:
        raise ValueError(f"Cannot propose a numeric series group: {case_id}")
    return "outer-series-" + match.group(1)


def proposed_split(family: str) -> str:
    # This is a collection reservation, not a certified physical-family split.
    if family == "outer-series-209":
        return "validation"
    if family == "outer-series-210":
        return "test"
    return "train"


def undirected_components(rows: list[dict]) -> dict[str, str]:
    parents = {r["sample_id"]: r["sample_id"] for r in rows}

    def find(identity):
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity

    seen = {}
    for row in rows:
        for key in ("family_id", "drawing_sha256", "design_fingerprint"):
            value = (key, row[key])
            if value in seen:
                left, right = find(row["sample_id"]), find(seen[value])
                parents[max(left, right)] = min(left, right)
            else:
                seen[value] = row["sample_id"]
    return {key: find(key) for key in parents}


def assemble(audit_path: Path, batch: Path, project: Path) -> dict:
    if (batch / "dataset_summary.json").exists():
        raise FileExistsError("Dataset was already finalized; create a new version")
    inventory = read_json(audit_path)
    generated = {r["sample_id"]: r for r in read_json(batch / "generation_results.json") if r["status"] == "generated"}
    catalog, features, requirements, operations, parameter_rows = [], [], [], [], []
    for row in inventory:
        if row["product"] != "外圈" or not row["raw_json"]:
            continue
        raw = read_json(Path(row["raw_json"]))
        fields, fm = field_map(raw, project)
        from src.TQ.table_process.product_geometry_extraction import build_design_part_params
        params = build_design_part_params(raw, ring_type="外圈", field_map=fm)
        candidates = build_feature_candidates(row["case_id"], params, raw)
        design = adapt_design(row["case_id"], raw, candidates, fields)
        destination = batch / "design_pool" / row["sample_id"]
        write_json(destination / "design.json", design)
        write_json(destination / "source.json", row)
        write_json(destination / "feature_candidates.json", candidates)
        write_json(destination / "annotations.template.json", {
            "schema_version": VERSION, "case_id": row["case_id"], "reviewer": "",
            "feature_members": {}, "event_overrides": {},
        })
        if row["sample_id"] not in generated and row["route_matches"]:
            import pandas as pd
            for index, match in enumerate(row["route_matches"]):
                frame = pd.read_excel(match["path"]).fillna("")
                column = next(c for c in frame.columns if str(c).strip() == "部件型号（图号）")
                selected = frame[frame[column].astype(str).str.strip().str.upper() == row["case_id"].upper()]
                if selected.empty:
                    raise ValueError("Previously audited route candidate disappeared")
                route_path = destination / "unapproved_route_candidates" / f"candidate-{index}.csv"
                route_path.parent.mkdir(parents=True, exist_ok=True)
                selected.to_csv(route_path, index=False, encoding="utf-8-sig")
                write_json(route_path.with_suffix(".provenance.json"), {
                    **match, "status": "exact_id_in_alternative_table_requires_engineer_approval",
                    "case_id": row["case_id"], "used_as_training_target": False,
                })
        family = proposed_family(row["case_id"])
        case = {
            "sample_id": row["sample_id"], "case_id": row["case_id"], "family_id": family,
            "family_status": "numeric_series_heuristic_requires_expert_confirmation",
            "split": proposed_split(family), "split_status": "reserved_not_gold_evaluation_ready",
            "design": str((destination / "design.json").relative_to(batch)).replace("\\", "/"),
            "target": None, "readiness": row["status"],
            "drawing_sha256": row["source_dwg_sha256"],
            "design_fingerprint": object_hash({k: v for k, v in design.items() if k != "case_id"}),
            "entity_count": len(design["entities"]), "features": len(design["features"]),
            "requirements": len(design["requirements"]), "operations": 0, "parameters": 0,
        }
        target = None
        if row["sample_id"] in generated:
            case_dir = batch / "cases" / row["sample_id"]
            if read_json(case_dir / "design.json") != design:
                raise ValueError("Independent design input does not equal paired input")
            target = read_json(case_dir / "target.json")
            case.update(target=str((case_dir / "target.json").relative_to(batch)).replace("\\", "/"),
                        readiness="system_weak_pair_ready_smoke_only",
                        operations=sum(e["action"] == "OP" for e in target["events"]),
                        parameters=sum(e["action"] == "PARAM" for e in target["events"]))
        catalog.append(case)
        for feature in design["features"]:
            features.append({
                "sample_id": row["sample_id"], "case_id": row["case_id"], "feature_id": feature["id"],
                "candidate_text": feature["text"], "annotation_handles": feature["anchor_entities"],
                "nominal": feature["value"]["nominal"], "lower_deviation": feature["value"]["lower"],
                "upper_deviation": feature["value"]["upper"], "unit": feature["value"]["unit"],
                "physical_object_id": "", "geometry_handles_complete": "", "group_is_complete": "",
                "reviewer": "", "decision": "", "notes": "",
            })
        for requirement in design["requirements"]:
            requirements.append({
                "sample_id": row["sample_id"], "case_id": row["case_id"], **requirement,
                "scope": "finished_dimension_candidate", "confirmed_feature_id": "",
                "reviewer": "", "decision": "", "notes": "",
            })
        if target is None:
            operations.append({
                "sample_id": row["sample_id"], "case_id": row["case_id"], "event_id": "",
                "process_no": "", "operation": "", "parameters_complete": "",
                "source_status": "missing_route_no_invented_rows", "reviewer": "", "decision": "", "notes": "",
            })
        else:
            current = ""
            for event in target["events"]:
                if event["action"] == "OP":
                    current = event["operation"]
                    operations.append({
                        "sample_id": row["sample_id"], "case_id": row["case_id"], "event_id": event["id"],
                        "process_no": event["id"].rsplit(":", 1)[-1], "operation": current,
                        "parameters_complete": event["parameters_complete"], "source_status": event["quality"],
                        "reviewer": "", "decision": "", "notes": "",
                    })
                elif event["action"] == "PARAM":
                    parameter_rows.append({
                        "sample_id": row["sample_id"], "case_id": row["case_id"],
                        "event_id": event["id"], "operation": current, "parameter": event["parameter"],
                        **{key: annotation["value"] for key, annotation in event["labels"].items()},
                        "field_qualities": {key: a["quality"] for key, a in event["labels"].items()},
                        "reviewer": "", "decision": "", "notes": "",
                    })
    groups = undirected_components(catalog)
    component_splits = {}
    for row in catalog:
        component = groups[row["sample_id"]]
        prior = component_splits.setdefault(component, row["split"])
        if prior != row["split"]:
            raise ValueError("Duplicate/family component crosses proposed split; expert reassignment needed")
        row["leakage_component"] = component
    manifest = {"schema_version": VERSION, "cases": [
        {key: row[key] for key in ("case_id", "family_id", "split", "design", "target")}
        for row in catalog if row["target"] is not None
    ]}
    write_json(batch / "manifest.ready.json", manifest)
    cases = load_cases(batch / "manifest.ready.json", smoke=True)
    vocabulary = Vocabulary.build(cases)
    write_json(batch / "design_pool_manifest.json", {
        "schema_version": "farmcad-design-pool-v1", "purpose": "collection_and_review_not_training_loader_input",
        "family_review_complete": False, "cases": catalog,
    })
    for split in ("train", "validation", "test"):
        write_json(batch / "reserved_splits" / f"{split}.json", {
            "schema_version": "farmcad-design-pool-v1", "split": split, "targets_may_be_missing": True,
            "family_review_complete": False, "cases": [r for r in catalog if r["split"] == split],
        })
    reviews = batch / "review"
    for name, rows in (("features.csv", features), ("requirements.csv", requirements),
                       ("routes.csv", operations), ("parameters.csv", parameter_rows)):
        reviews.mkdir(parents=True, exist_ok=True)
        save_csv(reviews / name, rows, list(rows[0]) if rows else ["case_id", "reviewer", "decision"])
    case_reviews = []
    lookup = {r["sample_id"]: r for r in catalog}
    for row in inventory:
        proposed = lookup.get(row["sample_id"], {})
        case_reviews.append({
            "sample_id": row["sample_id"], "case_id": row["case_id"], "product": row["product"],
            "source_relative": row["source_relative"], "readiness": row["status"],
            "reason": row["reason"], "identity_status": row["identity_status"],
            "route_matches": row["route_matches"], "suggested_family": proposed.get("family_id", ""),
            "reserved_split": proposed.get("split", ""), "confirmed_case_id": "", "confirmed_family_id": "",
            "confirmed_precision_grade": "", "approved_route_source": "",
            "reviewer": "", "decision": "", "notes": "",
        })
    save_csv(reviews / "cases.csv", case_reviews, list(case_reviews[0]))
    write_json(batch / "audit" / "inventory.json", inventory)
    write_json(batch / "publication.json", {
        "assembler": str(Path(__file__).resolve()), "audit_source": str(audit_path.resolve()),
        "input_target_contract": VERSION, "raw_labels_are_not_gold": True,
        "heldout_designs_have_no_implicit_target": True,
    })
    for name in ("inventory.csv", "inventory_summary.json", "route_catalog.json"):
        source = audit_path.parent / name
        (batch / "audit" / name).write_bytes(source.read_bytes())
    summary = {
        "schema_version": "farmcad-dataset-build-v1", "status": "partial_labels_require_human_completion",
        "input_drawings": len(inventory),
        "products": dict(Counter(r["product"] or "unparsed" for r in inventory)),
        "inventory_statuses": dict(Counter(r["status"] for r in inventory)),
        "outer_designs": len(catalog),
        "reserved_design_counts": dict(Counter(r["split"] for r in catalog)),
        "ready_pairs_by_split": {split: sum(c.split == split for c in cases) for split in ("train", "validation", "test")},
        "reserved_case_ids": {split: [r["case_id"] for r in catalog if r["split"] == split] for split in ("train", "validation", "test")},
        "outer_entities": sum(r["entity_count"] for r in catalog), "feature_candidates": len(features),
        "requirement_candidates": len(requirements),
        "ready_operations": sum(r["operations"] for r in catalog),
        "ready_parameters": sum(r["parameters"] for r in catalog),
        "ready_vocabulary": vocabulary.coverage(cases),
        "case_review_rows": len(case_reviews), "feature_review_rows": len(features),
        "requirement_review_rows": len(requirements), "route_review_rows": len(operations),
        "parameter_review_rows": len(parameter_rows), "human_gold_fields": 0,
        "can_run_smoke_training": True, "can_run_formal_training": False,
        "can_report_validation_or_test_accuracy": False,
        "missing_labels_not_replaced_with_d207_route": True,
        "family_assignment": "provisional_conservative_numeric_series_grouping_not_expert_truth",
    }
    write_json(batch / "dataset_summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(assemble(args.audit, args.dataset, args.project_root.resolve()), ensure_ascii=False))
