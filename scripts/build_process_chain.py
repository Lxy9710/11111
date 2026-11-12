"""Build a reviewable provenance graph from a verified Process Hub pilot replay."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from build_pilot_pair import OUTER_RING_FIELDS, bounds, digest, numeric, save_json


SCHEMA_VERSION = "process-chain-0.1"
INPUT_FILES = (
    "feature_candidates.json", "process_parameters.json",
    "design_parameters.json", "summary.json", "provenance.json",
)
VALUE_FIELDS = (
    "nominal_mm", "upper_tolerance_mm", "lower_tolerance_mm", "direct_value_mm",
)
PRIMARY_FIELDS = {
    "平面": {"C"}, "外径": {"D"}, "外圈内径": {"D2"},
    "外沟": {"De", "R", "ae"},
}
DESIGN_CALCULATIONS = {
    "lookup_allowance_calculate", "product_drawing_calculate_rough_c",
    "product_drawing_calculate_r", "product_drawing_calculate_ae",
    "product_drawing_calculate_de",
}
LINEAGE_RELATIONS = {"copies_parameter_from", "external_parameter_source", "reads_design_value"}


def ref(artifact: str, *parts: object) -> dict[str, str]:
    tokens = [str(p).replace("~", "~0").replace("/", "~1") for p in parts]
    return {"artifact": artifact, "pointer": "/" + "/".join(tokens) if tokens else ""}


def resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer:
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer: {pointer}")
    value = document
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def copy_source_symbol(value: dict[str, Any], definition: dict[str, Any]) -> str:
    process = value.get("copied_from_process")
    evidence = value.get("evidence") or ""
    if process and evidence.startswith(process + "."):
        tail = evidence[len(process) + 1:]
        suffix = "(一次循环)"
        if tail.endswith(suffix):
            tail = tail[:-len(suffix)]
        # A turning expression is not an exact registry key.
        if tail and not any(c in tail for c in "= .()"):
            return tail
    return str((definition.get("copy_from") or {}).get("parameter") or value["name"])


def parameter_value(value: dict[str, Any]) -> dict[str, Any]:
    for field in VALUE_FIELDS:
        number = value.get(field)
        if number is not None and not numeric(number):
            raise ValueError(f"Invalid numeric field {field}: {number!r}")
    nominal, direct = value.get("nominal_mm"), value.get("direct_value_mm")
    if nominal is None and direct is None:
        raise ValueError("Resolved parameter has no numeric value")
    interval = None
    if nominal is not None:
        lo, hi = value.get("lower_tolerance_mm"), value.get("upper_tolerance_mm")
        if lo is not None and hi is not None:
            interval = bounds(nominal, lo, hi)
    return {
        "raw_engine_fields": {k: value.get(k) for k in VALUE_FIELDS},
        "target_interval_mm": interval,
        "unit": "mm" if nominal is not None else None,
        "unit_status": "dimensional_nominal" if nominal is not None else "not_normalized",
    }


def load_verified_inputs(source: Path) -> dict[str, Any]:
    docs = {name: json.loads((source / name).read_text(encoding="utf-8-sig")) for name in INPUT_FILES}
    provenance = docs["provenance.json"]
    project = Path(provenance["project_root"]).resolve()
    manifest_path = Path(provenance["extraction_manifest"])
    if digest(manifest_path) != provenance["extraction_manifest_sha256"]:
        raise ValueError("Extraction manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if Path(manifest["project_root"]).resolve() != project:
        raise ValueError("Extraction and generation project roots differ")
    for path, expected in (
        (Path(manifest["json_path"]), provenance["input_sha256"]),
        (Path(manifest["source_dwg"]), provenance["original_dwg_sha256"]),
    ):
        if digest(path) != expected:
            raise ValueError(f"Drawing/extraction hash mismatch: {path}")
    for entry in provenance["assets"] + provenance["imported_sources"]:
        path = Path(entry["path"]).resolve()
        if project not in path.parents or digest(path) != entry["sha256"]:
            raise ValueError(f"Source asset changed or outside project: {path}")
    generator = provenance["generator"]
    if digest(Path(generator["path"])) != generator["sha256"]:
        raise ValueError("Pilot generator changed after replay")
    docs["source_entities.json"] = json.loads(
        Path(manifest["json_path"]).read_text(encoding="utf-8-sig")
    )
    docs["extraction_manifest.json"] = manifest
    return docs


def build_graph(docs: dict[str, Any]) -> dict[str, Any]:
    features = docs["feature_candidates.json"]
    process_doc = docs["process_parameters.json"]
    case_id = docs["summary.json"]["case_id"]
    design_fields = {
        item["field_name"]: item["value"] for item in docs["design_parameters.json"]["fields"]
    }
    if design_fields.get("零件型号") != case_id or design_fields.get("产品名称") != "外圈":
        raise ValueError("Only verified outer-ring pilots with matching case identity are supported")
    graph: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id,
        "status": "system_generated_requires_review",
        "scope": "planned_parameter_records_not_measured_workpiece_states",
        "nodes": [], "edges": [], "review_tasks": [],
    }
    nodes: dict[str, dict[str, Any]] = {}
    edge_keys: set[tuple[str, str, str]] = set()

    def node(node_id: str, kind: str, sources: list[dict[str, str]], **attrs: Any) -> str:
        if node_id in nodes:
            raise ValueError(f"Duplicate node: {node_id}")
        item = {
            "id": node_id, "kind": kind, "source_refs": sources,
            "human_reviewed": False, **attrs,
        }
        nodes[node_id] = item
        graph["nodes"].append(item)
        return node_id

    def edge(source: str, target: str, relation: str, basis: str,
             sources: list[dict[str, str]], **attrs: Any) -> None:
        key = (source, target, relation)
        if key in edge_keys:
            raise ValueError(f"Duplicate edge: {key}")
        edge_keys.add(key)
        graph["edges"].append({
            "id": f"{case_id}:edge:{len(graph['edges'])}",
            "source": source, "target": target, "relation": relation,
            "basis": basis, "source_refs": sources, "human_reviewed": False, **attrs,
        })

    def review(target: str, issue: str, reason: str, priority: str = "medium") -> None:
        graph["review_tasks"].append({
            "task_id": f"{case_id}:review:{len(graph['review_tasks'])}",
            "target_id": target, "issue": issue, "reason": reason, "priority": priority,
            "status": "pending", "reviewer": "", "decision": "", "notes": "",
        })

    entity_lookup = {}
    for i, entity in enumerate(docs["source_entities.json"]["entities"]):
        handle = entity.get("handle")
        if handle:
            if handle in entity_lookup:
                raise ValueError(f"Duplicate source handle: {handle}")
            entity_lookup[handle] = i
    feature_ids: dict[str, str] = {}
    requirements: dict[str, list[str]] = {}
    feature_sources: dict[str, dict[str, str]] = {}
    for i, feature in enumerate(features):
        field = feature["parameter_key"]
        if field in feature_ids:
            raise ValueError(f"Duplicate parameter-role seed: {field}")
        sources = [ref("feature_candidates.json", i)]
        fid = node(
            feature["feature_id"], "feature_seed", sources,
            parameter_key=field, feature_type_candidate=feature["feature_type_candidate"],
            identity_scope="case_local_parameter_role_not_physical_GlobalID",
            geometry_handles=feature["geometry_handles"],
            annotation_handles=feature["annotation_handles"],
            design_parameter=feature["design_parameter"], gaps=feature["gaps"],
        )
        feature_ids[field] = fid
        feature_sources[field] = sources[0]
        requirements[fid] = []
        if not feature["geometry_handles"]:
            review(fid, "geometry_group_missing", "Annotate complete geometry membership and physical feature identity.", "high")
        for gap in feature["gaps"]:
            review(fid, "design_evidence_gap", gap, "high")
        for handle in feature["annotation_handles"]:
            if handle not in entity_lookup:
                raise ValueError(f"Annotation handle not in source: {handle}")
            eid = f"{case_id}:evidence:{handle}"
            if eid not in nodes:
                index = entity_lookup[handle]
                node(eid, "annotation_evidence", [ref("source_entities.json", "entities", index)],
                     handle=handle, entity=docs["source_entities.json"]["entities"][index])
            edge(fid, eid, "anchored_by", "extractor_handle", sources)
        for j, requirement in enumerate(feature["intent_candidates"]):
            rid = node(
                requirement["constraint_id"], "design_requirement",
                [ref("feature_candidates.json", i, "intent_candidates", j)],
                requirement_type=requirement["constraint_type"],
                predicate=requirement["predicate"],
                scope="finished_dimension_only_not_functional_intent_or_measured_quality",
                legacy_intent_label=requirement["intent_type"],
            )
            requirements[fid].append(rid)
            edge(rid, fid, "constrains", "parsed_annotation_candidate", sources)
            for handle in requirement["evidence_handles"]:
                edge(rid, f"{case_id}:evidence:{handle}", "supported_by",
                     "extractor_handle", sources)
            review(rid, "requirement_confirmation", "Confirm object binding, value, scope and evidence; not independent ground truth.")

    context_id = node(
        f"{case_id}:context", "process_context", [ref("process_parameters.json")],
        grade=process_doc["grade"], grind_loop=process_doc["grind_loop"],
        has_lapping=process_doc["has_lapping"],
        has_fine_grind_plane=process_doc["has_fine_grind_plane"],
        context_diagnostics=process_doc["context_diagnostics"],
    )
    for diagnostic in process_doc["context_diagnostics"]:
        review(context_id, "upstream_context_diagnostic", diagnostic["message"], "high")
    for index, item in enumerate(docs["design_parameters.json"]["fields"]):
        if item["field_name"] not in {"材料牌号", "技术要求"}:
            continue
        sources = [ref("design_parameters.json", "fields", index)]
        handles = item.get("value_cell_ids") or []
        for handle in handles:
            if handle not in entity_lookup:
                raise ValueError(f"Document evidence handle not in source: {handle}")
            sources.append(ref("source_entities.json", "entities", entity_lookup[handle]))
        evidence_id = node(
            f"{case_id}:document:{index}", "document_evidence", sources,
            field_name=item["field_name"], text=item["value"], annotation_handles=handles,
            interpretation_status="raw_document_evidence_not_feature_bound",
        )
        edge(context_id, evidence_id, "document_context", "raw_document_not_causal_proof", sources)
        if item["field_name"] == "技术要求":
            review(evidence_id, "technical_requirement_scope_unresolved",
                   "Segment the original requirements and annotate feature/operation scope; do not infer applicability from keywords.", "high")

    latest_registry: dict[tuple[str, str], str] = {}
    previous_feature_record: dict[str, str] = {}
    previous_operation = None
    for route_index, operation in enumerate(process_doc["processes"]):
        row = operation["route_row"]
        base = ("processes", route_index)
        sources = [ref("process_parameters.json", *base)]
        op_id = node(
            f"{case_id}:operation:{row['process_no']}", "operation", sources,
            process_no=row["process_no"], route_index=route_index,
            name=row["process_name_only"], canonical_name=operation["canonical_process"],
            process_type=row["process_type"], generation_status=operation["status"],
        )
        if previous_operation is not None:
            edge(previous_operation, op_id, "route_next", "excel_order_not_causal_dependency", sources)
        previous_operation = op_id
        if not operation["parameters"]:
            review(op_id, "operation_parameters_unavailable",
                   f"Preserved route step; generation status: {operation['status']}. Do not infer no effect.")
            continue
        for missing in operation.get("missing_parameters", []):
            review(op_id, "expected_parameter_missing", missing, "high")
        for warning in operation.get("warnings", []):
            review(op_id, "parameter_generation_warning", warning["message"], "high")
        rule = operation["rule"]
        definitions = {d["name"]: d for d in rule["parameters"]}
        rule_id = node(
            f"{op_id}:rule", "rule_evidence",
            [ref("process_parameters.json", *base, "rule")],
            declared_feature=rule.get("feature"), definition=rule,
        )
        unbound = []
        pending_registry = []
        for symbol, value in operation["parameters"].items():
            if value["name"] != symbol or symbol not in definitions:
                raise ValueError(f"Parameter key/definition mismatch: {op_id}.{symbol}")
            definition = definitions[symbol]
            psource = ref("process_parameters.json", *base, "parameters", symbol)
            stype = value["source_type"]
            field = OUTER_RING_FIELDS.get(symbol)
            fid = feature_ids.get(field) if field else None
            role = "unbound_process_parameter"
            if stype == "copy_from_other_process":
                role = "reference_copy_not_machining_evidence"
            elif fid and symbol in PRIMARY_FIELDS.get(rule.get("feature"), set()):
                role = "machining_target_candidate"
            elif fid:
                role = "unresolved_feature_role"
            sid = node(
                f"{case_id}:state:{row['process_no']}:{symbol}", "parameter_state", [psource],
                operation_id=op_id, route_index=route_index, feature_id=fid, parameter=symbol,
                record_kind="planned_parameter_snapshot_not_observed_workpiece",
                role=role, value=parameter_value(value), resolved_parameter=value,
                declared_source_type=definition["source_type"], actual_source_type=stype,
                requires_inspection=definition.get("requires_inspection"),
                requirement_ids=requirements.get(fid, []),
                lineage_status="not_resolved", finished_check="not_applicable",
            )
            edge(op_id, sid, "records_parameter", "system_output", [psource])
            edge(sid, rule_id, "resolved_under_rule", "rule_snapshot_not_full_execution_trace", sources)
            edge(sid, context_id, "generated_in_context", "system_context", [psource])
            if fid:
                edge(sid, fid, "parameter_of", "outer_ring_symbol_role_candidate", [psource, feature_sources[field]])
                previous = previous_feature_record.get(fid)
                if previous is not None:
                    edge(previous, sid, "next_recorded_state",
                         "chronological_observation_not_causal_or_machining_transition", [psource])
                previous_feature_record[fid] = sid
            else:
                unbound.append(symbol)
            if nodes[sid]["value"]["unit_status"] == "not_normalized":
                review(sid, "direct_value_unit_unverified",
                       "Keep raw engine fields; direct_value_mm is not a universal mm unit (e.g. Ra display convention).")
            if role == "unresolved_feature_role":
                review(sid, "operation_role_unresolved", "Parameter identity does not prove that this operation machines the feature.")
            if stype == "copy_from_other_process":
                source_process = value.get("copied_from_process")
                if not source_process:
                    raise ValueError(f"Copy source missing: {sid}")
                source_symbol = copy_source_symbol(value, definition)
                parent = latest_registry.get((source_process, source_symbol))
                same_value = parent is not None and all(
                    nodes[parent]["resolved_parameter"].get(k) == value.get(k) for k in VALUE_FIELDS
                )
                if same_value:
                    edge(sid, parent, "copies_parameter_from",
                         "runtime_named_source_latest_prior_record_and_equal_value", [psource],
                         source_parameter=source_symbol)
                    nodes[sid]["lineage_status"] = "resolved_prior_parameter_record"
                else:
                    reason = "no_prior_route_record" if parent is None else "prior_record_value_mismatch"
                    external_id = node(
                        f"{sid}:external", "external_source", [psource],
                        source_process=source_process, source_parameter=source_symbol,
                        source_evidence=value.get("evidence"), resolution_gap=reason,
                        candidate_record_id=parent, route_index=None,
                    )
                    edge(sid, external_id, "external_parameter_source",
                         "runtime_source_named_but_stage_unresolved", [psource])
                    nodes[sid]["lineage_status"] = "unresolved_stage_reference"
                    review(sid, reason, f"Locate source {source_process}.{source_symbol}; do not attach to a guessed operation.", "high")
            elif fid and (
                stype in DESIGN_CALCULATIONS
                or (stype == "product_drawing" and value.get("evidence") == f"design.{field}")
            ):
                edge(sid, fid, "reads_design_value", "engine_primary_design_field", [psource, feature_sources[field]])
                nodes[sid]["lineage_status"] = (
                    "direct_design_field" if stype == "product_drawing" else "primary_design_input_only"
                )
                if stype != "product_drawing":
                    review(sid, "derivation_inputs_incomplete",
                           "Primary design value and rule are linked; complete executed formula/table inputs require annotation or instrumentation.")
                if stype == "product_drawing":
                    interval = nodes[sid]["value"]["target_interval_mm"]
                    reqs = requirements[fid]
                    if interval is not None and len(reqs) == 1:
                        predicate = nodes[reqs[0]]["predicate"]
                        if not (predicate["lower"] <= interval[0] <= interval[1] <= predicate["upper"]):
                            raise ValueError(f"Direct design-copy interval contradicts source requirement: {sid}")
                        nodes[sid]["finished_check"] = "same_source_interval_consistent_not_independent_validation"
                        edge(sid, reqs[0], "copies_finished_requirement",
                             "direct_design_copy_only", [psource])
                    else:
                        nodes[sid]["finished_check"] = "unknown_missing_or_ambiguous_requirement"
                        review(sid, "finished_requirement_unavailable",
                               "Engine defaults do not complete a missing design tolerance or evidence handle.", "high")
            elif stype == "lookup_direct":
                nodes[sid]["lineage_status"] = "rule_lookup_without_exact_table_cell"
            else:
                review(sid, "source_semantics_unresolved", f"Retained runtime source: {stype}; no inferred design or causal edge.", "high")
            if stype in {"lookup_direct", "lookup_allowance_calculate", "product_drawing_calculate_rough_c"}:
                lookup_symbol = (definition.get("lookup") or {}).get("dimension_key") or rule.get("dimension_key")
                lookup_field = OUTER_RING_FIELDS.get(lookup_symbol)
                if lookup_field in feature_ids:
                    edge(sid, feature_ids[lookup_field], "declared_lookup_key",
                         "rule_declared_selector_not_complete_runtime_dependency", sources)
            pending_registry.append((operation["canonical_process"], symbol, sid))
        if unbound:
            review(op_id, "parameter_feature_binding_missing",
                   "Bind non-dimension parameters to physical features and requirements: " + ", ".join(unbound))
        for canonical, symbol, sid in pending_registry:
            if canonical:
                latest_registry[(canonical, symbol)] = sid
    validate_graph(graph, docs)
    return graph


def validate_graph(graph: dict[str, Any], docs: dict[str, Any]) -> None:
    json.dumps(graph, allow_nan=False)
    nodes = {n["id"]: n for n in graph["nodes"]}
    if len(nodes) != len(graph["nodes"]):
        raise ValueError("Duplicate node IDs")
    seen = set()
    for item in graph["nodes"] + graph["edges"]:
        if item["human_reviewed"]:
            raise ValueError("Generated data must not claim human review")
        if not item["source_refs"]:
            raise ValueError("Missing source reference")
        for source in item["source_refs"]:
            resolve_pointer(docs[source["artifact"]], source["pointer"])
    for e in graph["edges"]:
        key = (e["source"], e["target"], e["relation"])
        if key in seen or e["source"] not in nodes or e["target"] not in nodes:
            raise ValueError(f"Duplicate/dangling edge: {key}")
        seen.add(key)
        source, target = nodes[e["source"]], nodes[e["target"]]
        if e["relation"] == "copies_parameter_from":
            if source["kind"] != "parameter_state" or target["kind"] != "parameter_state":
                raise ValueError("Copy pointers must reference parameter states")
            if target["route_index"] >= source["route_index"]:
                raise ValueError("Copy pointer references same or future operation")
            if any(source["resolved_parameter"].get(k) != target["resolved_parameter"].get(k) for k in VALUE_FIELDS):
                raise ValueError("Copy value mismatch")
        if e["relation"] in {"route_next", "next_recorded_state"}:
            if source["route_index"] >= target["route_index"]:
                raise ValueError("Invalid chronological edge")
    for task in graph["review_tasks"]:
        if task["target_id"] not in nodes or task["status"] != "pending":
            raise ValueError("Invalid review task")


def source_paths(graph: dict[str, Any], state_id: str) -> list[list[str]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    if state_id not in nodes:
        raise ValueError(f"Unknown state: {state_id}")
    adjacency: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        if edge["relation"] in LINEAGE_RELATIONS:
            adjacency.setdefault(edge["source"], []).append(edge["target"])

    def walk(current: str, path: list[str]) -> list[list[str]]:
        if current in path:
            raise ValueError("Cycle in provenance pointers")
        extended = path + [current]
        targets = adjacency.get(current, [])
        if not targets:
            return [extended]
        return [p for target in targets for p in walk(target, extended)]

    return walk(state_id, [])


def chain_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge)
    rows = []
    for state in graph["nodes"]:
        if state["kind"] != "parameter_state" or not state["feature_id"]:
            continue
        operation = nodes[state["operation_id"]]
        raw = state["resolved_parameter"]
        parents = [e["target"] for e in outgoing.get(state["id"], []) if e["relation"] in LINEAGE_RELATIONS]
        rows.append({
            "case_id": graph["case_id"], "feature_id": state["feature_id"],
            "route_index": state["route_index"], "process_no": operation["process_no"],
            "process_name": operation["name"], "state_id": state["id"],
            "parameter": state["parameter"], "role": state["role"],
            "nominal_mm": raw.get("nominal_mm"),
            "target_interval_mm": state["value"]["target_interval_mm"],
            "actual_source_type": state["actual_source_type"],
            "declared_source_type": state["declared_source_type"],
            "source_evidence": raw.get("evidence"), "source_node_ids": parents,
            "source_paths": source_paths(graph, state["id"]),
            "requirement_ids": state["requirement_ids"], "lineage_status": state["lineage_status"],
            "finished_check": state["finished_check"], "human_reviewed": False,
        })
    return sorted(rows, key=lambda r: (r["feature_id"], r["route_index"], r["parameter"]))


def summarize(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    copies = [e for e in graph["edges"] if e["relation"] == "copies_parameter_from"]
    states = [n for n in graph["nodes"] if n["kind"] == "parameter_state"]
    return {
        "schema_version": SCHEMA_VERSION, "case_id": graph["case_id"], "status": graph["status"],
        "node_counts": dict(Counter(n["kind"] for n in graph["nodes"])),
        "edge_counts": dict(Counter(e["relation"] for e in graph["edges"])),
        "parameter_roles": dict(Counter(n["role"] for n in states)),
        "lineage_status_counts": dict(Counter(n["lineage_status"] for n in states)),
        "feature_chain_rows": len(chain_rows(graph)),
        "review_tasks": len(graph["review_tasks"]),
        "review_issue_counts": dict(Counter(t["issue"] for t in graph["review_tasks"])),
        "maximum_verified_copy_route_gap": max(
            (nodes[e["source"]]["route_index"] - nodes[e["target"]]["route_index"] for e in copies),
            default=0,
        ),
        "human_verified_edges": 0, "training_ready": False,
        "limits": [
            "Parameter-role seeds are not manually grouped physical features or cross-drawing GlobalIDs.",
            "States are generated planning records, not measured or proven achieved workpiece states.",
            "Copied values and chronological edges do not prove physical machining or causal transitions.",
            "Source pointers cover declared runtime provenance, not all formula/table/fixture dependencies.",
            "Missing operations, external turning sources, units and evidence remain explicit review tasks.",
            "Only explicit finished-dimension candidates are present; no functional intent labels are invented.",
            "No A/B version alignment, impact ground truth, or model training is included.",
        ],
    }


def save_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                k: json.dumps(row[k], ensure_ascii=False) if isinstance(row.get(k), (dict, list)) else row.get(k)
                for k in columns
            })


def portable_path(path: Path) -> str:
    """Render a path relative to the project root when it lies inside it.

    Provenance is a record of *what* was read, not of where the checkout
    happened to live. Embedding absolute paths makes the artifact differ
    byte-for-byte after the project is moved, which breaks replay
    verification even though every content artifact is identical. Paths
    outside the project keep their absolute form, because they refer to
    external assets that genuinely have no project-relative location.
    """
    resolved = Path(path).resolve()
    root = Path(__file__).resolve().parents[1]
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-results", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source, output = args.source_results.resolve(), args.output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite: {output}")
    docs = load_verified_inputs(source)
    graph = build_graph(docs)
    rows = chain_rows(graph)
    summary = summarize(graph)
    output.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, handlers=[
        logging.StreamHandler(), logging.FileHandler(output / "generation.log", encoding="utf-8"),
    ])
    for task in graph["review_tasks"]:
        logging.warning("%s [%s]: %s", task["issue"], task["target_id"], task["reason"])
    save_json(output / "association_graph.json", graph)
    save_json(output / "feature_chains.json", rows)
    save_csv(output / "feature_chains.csv", rows, list(rows[0]) if rows else ["case_id", "feature_id"])
    save_csv(output / "nodes.csv", graph["nodes"],
             ["id", "kind", "feature_id", "parameter_key", "operation_id", "route_index", "role", "source_refs"])
    save_csv(output / "edges.csv", graph["edges"],
             ["id", "source", "target", "relation", "basis", "human_reviewed", "source_refs"])
    save_csv(output / "review_tasks.csv", graph["review_tasks"],
             ["task_id", "target_id", "issue", "reason", "priority", "status", "reviewer", "decision", "notes"])
    save_json(output / "provenance.json", {
        "inputs": [{"path": portable_path(source / name), "sha256": digest(source / name)}
                   for name in INPUT_FILES],
        "upstream_provenance": docs["provenance.json"],
        "generator": {"path": portable_path(Path(__file__).resolve()), "sha256": digest(Path(__file__))},
        "helper": {"path": portable_path(Path(__file__).with_name("build_pilot_pair.py")),
                   "sha256": digest(Path(__file__).with_name("build_pilot_pair.py"))},
    })
    save_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
