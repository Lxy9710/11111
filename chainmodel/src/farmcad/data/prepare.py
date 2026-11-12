from __future__ import annotations

import copy
import importlib
import math
import sys
from pathlib import Path
from typing import Any

from farmcad.data.schema import VERSION, LABEL_FIELDS, label, validate_design, validate_target
from farmcad.utils import file_hash, finite, read_json, require_keys, scaled, write_json

# Runtime `source_type` -> farmcad `update` operator.
#
# The outer-ring grinding engine and the cage rule engine use different
# vocabularies for the same physical relations. Both are mapped here so a
# cage record does not silently degrade to `unknown` and lose its supervision
# signal. `product_count_minus_one` is a derivation from a counted design
# feature, not a table lookup.
SOURCE_TYPE_TO_UPDATE = {
    # outer-ring grinding engine
    "product_drawing": "design_copy",
    "lookup_direct": "lookup",
    "lookup_allowance_calculate": "derive",
    "copy_from_other_process": "state_copy",
    # cage rule engine
    "product_drawing_with_tolerance": "design_copy",
    "product_drawing_plus_allowance": "derive",
    "product_drawing_face_chamfer": "derive",
    "product_drawing_ra": "design_copy",
    "product_drawing_display": "design_copy",
    "product_drawing_surface_roughness": "design_copy",
    "product_count_minus_one": "derive",
    "fixed": "lookup",
    "fixed_face_chamfer": "lookup",
}


def update_operator(source_type: str) -> str:
    """Map a runtime source type to the dataset's update operator."""

    if source_type.startswith("product_drawing_calculate"):
        return "derive"
    return SOURCE_TYPE_TO_UPDATE.get(source_type, "unknown")


# Operation semantics -> parameter role.
#
# A parameter's role cannot be read off the parameter alone. The same physical
# quantity is a machining target in the operation that produces it and a
# reference copy in the operation that merely records it. Binding the role by
# "the parameter maps to a design feature" is therefore wrong: it labels the
# outer diameter of a final inspection as a machining target, which teaches the
# model that inspection cuts material.
#
# The classes below are ordered, and the first match wins.
#
# - `inspection`: the operation measures, it does not cut.
# - `auxiliary`: the operation records a state or handles the part, but does
#   not change the machined dimensions.
# - `datum`: the operation establishes a reference. Parameters that are
#   consumed as the drilling or locating reference are datum uses; parameters
#   the operation itself produces are machining targets.
# - `cutting`: the operation removes material.
INSPECTION_OPERATIONS = {
    "终检", "中间检验", "首检", "荧光磁粉探伤", "磁粉探伤", "探伤",
    "原材料检验", "锻造检验", "热处理检验", "尺寸检验", "成品检验",
}

AUXILIARY_OPERATIONS = {
    "包装", "清洗", "镀银", "镀锌", "喷漆", "打标记", "打标",
    "光饰", "粗光饰", "精光饰", "抛光", "去毛刺", "去锐角", "去毛刺倒角",
    "退磁(粗磨后)", "退磁(修磨后)", "退磁", "酸洗", "上传数据",
}

# Operations that establish a reference. Their parameters can be datum uses.
DATUM_OPERATIONS = {
    "钻铆钉孔", "钻铣孔", "钻孔", "铆钉孔", "划线", "打中心", "找正",
}


def operation_role(operation: str, parameter: str) -> str | None:
    """The role a parameter plays in an operation.

    Returns `None` when the operation does not determine the role, which keeps
    the label unset rather than guessing.
    """

    name = str(operation or "").strip()
    if not name:
        return None
    if name in INSPECTION_OPERATIONS:
        return "inspection_target"
    if name in AUXILIARY_OPERATIONS:
        return "reference_copy"
    if name in DATUM_OPERATIONS:
        return "machining_target"
    if name in CUTTING_OPERATIONS:
        return "machining_target"
    return None


# Operations that remove material. A parameter of such an operation is the
# thing being machined, unless the operation is a reference or inspection
# operation, which are handled above.
CUTTING_OPERATIONS = {
    "粗磨外径", "细磨外径", "终磨外径", "修磨外径", "精磨外径",
    "粗磨外沟", "细磨外沟", "终磨外沟", "精研外沟", "磨外沟",
    "粗磨内径", "细磨内径", "终磨内径",
    "粗磨外圈内径", "细磨外圈内径", "终磨外圈内径",
    "均粗磨平面", "细磨平面", "终磨平面", "修磨平面", "均磨平面",
    "粗车第一平面及内径、倒角", "粗车第二平面及外径、倒角",
    "细车内径及倒角", "终车内径及倒角", "均车两平面及倒角",
    "车外沟", "车内沟", "细车Ⅰ", "细车Ⅱ", "切断", "倒铆钉孔角",
    "钻铆钉孔", "钻铣孔", "插销子", "拆销子", "倒角",
}


def parameter_role(operation: str, parameter: str, source_type: str,
                   bound_to_feature: bool) -> tuple[list[str] | None, str]:
    """Decide a parameter's role and record how the decision was made.

    The role depends on two things: what the operation *is*, and whether the
    operation *produced* the parameter or merely *consumed* it.

    A parameter the operation consumed was either a located reference or a
    recorded state. The two are distinguished by the operation's semantics:

    - a cutting operation that consumes a dimension positions its work against
      that dimension, so the dimension is a `datum_use`;
    - an auxiliary or inspection operation that consumes a dimension only
      records or measures against it, so the dimension is a `reference_copy`.

    This is why a role cannot be read off the parameter alone. The same
    physical quantity is a machining target in the operation that produces it,
    a datum use in the operation that locates its work against it, and a
    reference copy in the operation that merely records it.

    The second return value is the basis of the decision, and it decides the
    supervision level: a role derived from the operation semantics is
    deterministic and machine-checkable, so it is system level. A role that
    fell back to the feature binding is a candidate.
    """

    consumed = source_type in ("copy_from_other_process", "external_source")
    if consumed:
        if operation_role(operation, parameter) == "machining_target":
            return ["datum_use"], "consumed_by_cutting_operation"
        return ["reference_copy"], "consumed_by_non_cutting_operation"
    semantic = operation_role(operation, parameter)
    if semantic is not None:
        return [semantic], "operation_semantics"
    if bound_to_feature:
        return ["machining_target"], "feature_binding_heuristic"
    return None, ""


def point(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2 and all(finite(v) for v in value[:2]):
        return [float(value[0]), float(value[1])]
    return []


def entity_geometry(entity: dict) -> tuple[list[float], list[float], bool]:
    pts = []
    for key in ("start", "end", "center", "position", "insertion_point", "insert"):
        p = point(entity.get(key))
        if p:
            pts.append(p)
    for value in entity.get("points", []) or entity.get("vertices", []) or []:
        p = point(value)
        if p:
            pts.append(p)
    if not pts:
        return [0.0, 0.0], [0.0, 0.0], False
    lo = [min(p[i] for p in pts) for i in range(2)]
    hi = [max(p[i] for p in pts) for i in range(2)]
    return [(lo[i] + hi[i]) / 2 for i in range(2)], [hi[i] - lo[i] for i in range(2)], True


def adapt_design(case_id: str, raw: dict, candidates: list[dict], fields: list[dict]) -> dict:
    from farmcad.data.schema import ENTITY_TYPES

    originals = raw["entities"]
    geometry = [entity_geometry(e) for e in originals]
    valid_centers = [g[0] for g in geometry if g[2]]
    minima = [min((p[i] for p in valid_centers), default=0.0) for i in range(2)]
    spans = [max(max((p[i] for p in valid_centers), default=0.0) - minima[i], 1.0) for i in range(2)]
    entities, edges = [], set()
    for i, (entity, (center, extent, valid)) in enumerate(zip(originals, geometry)):
        kind = str(entity.get("type", "OTHER")).upper()
        start, end = point(entity.get("start")), point(entity.get("end"))
        length = math.dist(start, end) if start and end else 0.0
        angle = math.atan2(end[1] - start[1], end[0] - start[0]) if start and end else 0.0
        radius = entity.get("radius")
        measurement = entity.get("measurement")
        entities.append({
            "id": str(entity.get("handle") or f"entity-{i}"),
            "type": kind if kind in ENTITY_TYPES else "OTHER",
            "numeric": [
                (center[0] - minima[0]) / spans[0] if valid else 0.0,
                (center[1] - minima[1]) / spans[1] if valid else 0.0,
                extent[0] / spans[0], extent[1] / spans[1],
                scaled(length), scaled(radius) if finite(radius) else 0.0,
                math.sin(angle), math.cos(angle),
                scaled(measurement) if finite(measurement) else 0.0,
                float(finite(measurement)), float(valid), float(bool(start and end)),
            ],
            "text": str(entity.get("text") or entity.get("contents") or ""),
        })
        edges.add((i, i, 0))
    for i, (center, _, valid) in enumerate(geometry):
        if not valid:
            continue
        near = sorted(
            (j for j, g in enumerate(geometry) if g[2] and j != i),
            key=lambda j: (sum(((center[k] - geometry[j][0][k]) / spans[k]) ** 2 for k in range(2)), j),
        )[:4]
        for j in near:
            edges.add((i, j, 1))
    endpoints: dict[tuple[float, float], list[int]] = {}
    for i, entity in enumerate(originals):
        if str(entity.get("type", "")).upper() not in ("LINE", "ARC"):
            continue
        for key in ("start", "end"):
            p = point(entity.get(key))
            if p:
                endpoints.setdefault((round(p[0], 6), round(p[1], 6)), []).append(i)
    for indices in endpoints.values():
        for i in indices:
            for j in indices:
                if i != j:
                    edges.add((i, j, 2))
    features, requirements = [], []
    for feature in sorted(candidates, key=lambda f: f["parameter_key"]):
        fid = feature["parameter_key"]
        parameter = feature["design_parameter"]
        features.append({
            "id": fid,
            "text": str(feature["feature_type_candidate"] or "") + " " + str(parameter.get("display") or ""),
            "value": {
                "nominal": parameter.get("nominal"), "lower": parameter.get("lower"),
                "upper": parameter.get("upper"), "direct": None, "unit": "mm",
            },
            "anchor_entities": list(feature["annotation_handles"]),
        })
        for j, req in enumerate(feature["intent_candidates"]):
            requirements.append({
                "id": f"{fid}:requirement:{j}", "feature_id": fid,
                "text": req["constraint_type"],
                "lower": req["predicate"]["lower"], "upper": req["predicate"]["upper"],
                "evidence_entities": req["evidence_handles"],
            })
    # Route-derived flags, identifiers, group labels and state provenance never enter this object.
    context = "\n".join(f'{f["field_name"]}: {f["value"]}' for f in fields
                        if f["field_name"] in {"材料牌号", "产品名称", "技术要求", "精度等级"})
    design = {
        "schema_version": VERSION, "case_id": case_id, "entities": entities,
        "edges": [list(e) for e in sorted(edges)], "features": features,
        "requirements": requirements, "context": context,
    }
    validate_design(design)
    return design


def make_target(design: dict, graph: dict, candidates: list[dict]) -> dict:
    nodes = {n["id"]: n for n in graph["nodes"]}
    feature_ids = {f["feature_id"]: f["parameter_key"] for f in candidates}
    requirement_ids = {
        req["constraint_id"]: f'{f["parameter_key"]}:requirement:{j}'
        for f in candidates for j, req in enumerate(f["intent_candidates"])
    }
    outgoing: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["source"], []).append(edge)
    operations = sorted((n for n in nodes.values() if n["kind"] == "operation"), key=lambda n: n["route_index"])
    events = []
    for op in operations:
        complete = op["generation_status"] == "computed"
        events.append({
            "id": op["id"], "action": "OP", "quality": "system",
            "operation": op["name"], "parameters_complete": complete,
        })
        for state in nodes.values():
            if state["kind"] != "parameter_state" or state["operation_id"] != op["id"]:
                continue
            source_edges = outgoing.get(state["id"], [])
            sources = [e["target"] for e in source_edges if e["relation"] == "copies_parameter_from"]
            external = any(e["relation"] == "external_parameter_source" for e in source_edges)
            requirement_links = [requirement_ids[e["target"]] for e in source_edges
                                 if e["relation"] == "copies_finished_requirement"]
            raw = state["resolved_parameter"]
            source_type = raw["source_type"]
            update = (
                "state_copy" if sources else "external" if external else
                update_operator(source_type)
            )
            roles, role_basis = parameter_role(
                operation=op["name"],
                parameter=state["parameter"],
                source_type=source_type,
                bound_to_feature=state["role"] == "machining_target_candidate",
            )
            fid = feature_ids.get(state["feature_id"])
            # The quality of a feature binding depends on how it was established,
            # not on the fact that a binding exists.
            #
            # - `rule_declared_design_field`: the rule asset itself declares which
            #   design field a parameter belongs to. This is deterministic and
            #   machine-checkable, so it is system level.
            # - anything else (a symbol-to-field naming table, a heuristic): the
            #   binding is a candidate and must not drive the pointer head.
            binding_basis = str(state.get("binding_basis") or "")
            if fid is None:
                feature_label = label(None)
            elif binding_basis == "rule_declared_design_field":
                feature_label = label([fid], "system")
            else:
                feature_label = label([fid], "candidate")
            dimensional = raw.get("nominal_mm") is not None
            value = {
                "nominal": raw.get("nominal_mm"), "lower": raw.get("lower_tolerance_mm"),
                "upper": raw.get("upper_tolerance_mm"), "direct": raw.get("direct_value_mm"),
                "unit": "mm" if dimensional else "unknown",
            }
            events.append({
                "id": state["id"], "action": "PARAM", "quality": "system",
                "parameter": state["parameter"],
                "labels": {
                    "features": feature_label,
                    "requirements": label(requirement_links, "system") if requirement_links else label(None),
                    "sources": label(sources or ["EXTERNAL"], "system") if sources or external else label(None),
                    "roles": (label(roles, "system") if role_basis != "feature_binding_heuristic"
                              else label(roles, "candidate")) if roles else label(None),
                    "update": label(update, "system" if update != "unknown" else "unknown"),
                    "value": label(value, "system" if dimensional else "unknown"),
                },
            })
        events.append({
            "id": op["id"] + ":end", "action": "END_OP", "quality": "system" if complete else "unknown",
        })
    events.append({"id": graph["case_id"] + ":eos", "action": "EOS", "quality": "system"})
    target = {
        "schema_version": VERSION, "case_id": graph["case_id"], "reviewer": "",
        "events": events, "feature_members": {},
    }
    validate_target(design, target)
    return target


def apply_annotations(design: dict, target: dict, annotations: dict) -> dict:
    keys = {"schema_version", "case_id", "reviewer", "feature_members", "event_overrides"}
    require_keys(annotations, keys, keys, "annotations")
    if annotations["case_id"] != design["case_id"] or annotations["schema_version"] != VERSION:
        raise ValueError("Annotation case/schema mismatch")
    result = copy.deepcopy(target)
    has_edits = bool(annotations["feature_members"] or annotations["event_overrides"])
    if has_edits and not annotations["reviewer"].strip():
        raise ValueError("Manual annotations require reviewer identity")
    result["reviewer"] = annotations["reviewer"]
    for fid, members in annotations["feature_members"].items():
        result["feature_members"][fid] = label(members, "gold")
    events = {e["id"]: e for e in result["events"]}
    for event_id, values in annotations["event_overrides"].items():
        if event_id not in events:
            raise ValueError(f"Unknown annotated event: {event_id}")
        event = events[event_id]
        if event["action"] == "PARAM":
            require_keys(values, set(LABEL_FIELDS), set(), "parameter annotations")
            for name, value in values.items():
                event["labels"][name] = label(value, "gold")
        elif event["action"] == "OP":
            require_keys(values, {"operation", "parameters_complete"}, set(), "operation annotations")
            event.update(values)
            event["quality"] = "gold"
            if "parameters_complete" in values:
                start = result["events"].index(event)
                end = next(e for e in result["events"][start + 1:] if e["action"] == "END_OP")
                end["quality"] = "gold" if values["parameters_complete"] else "unknown"
        else:
            raise ValueError("Annotate OP/PARAM, not grammar delimiters")
    validate_target(design, result)
    return result


def resolve_recorded_path(path: str, roots: list[Path] = None) -> Path:
    """Resolve a path recorded in a provenance document.

    Provenance stores project-relative paths for artifacts inside the project
    so that the record survives relocation. Paths outside the project are kept
    absolute, because they refer to external assets that have no project
    relative location.

    The candidate roots are tried in order and the first one that actually
    contains the file wins, so that the document stays valid regardless of
    which directory the caller was invoked from.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if roots is None:
        roots = [Path(__file__).resolve().parents[4], Path(__file__).resolve().parents[3]]
    for root in roots:
        resolved = root / candidate
        if resolved.exists():
            return resolved
    return roots[0] / candidate


def prepare_pilot(pilot: Path, output: Path, family_id: str, annotations: Path = None) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite dataset: {output}")
    if not family_id.strip():
        raise ValueError("Explicit part/drawing family ID is required")
    graph_path = pilot / "process_chain" / "association_graph.json"
    provenance = read_json(pilot / "process_chain" / "provenance.json")
    for entry in provenance["inputs"]:
        if file_hash(resolve_recorded_path(entry["path"])) != entry["sha256"]:
            raise ValueError(f"Pilot input changed: {entry['path']}")
    verification = read_json(pilot / "process_chain" / "verification.json")
    expected = next(e["sha256"] for e in verification["outputs"] if e["file"] == graph_path.name)
    if file_hash(graph_path) != expected:
        raise ValueError("Association graph hash mismatch")
    extraction = read_json(pilot / "source" / "extraction_manifest.json")
    raw_path = Path(extraction["json_path"])
    if file_hash(raw_path) != extraction["json_sha256"]:
        raise ValueError("Extracted CAD JSON changed")
    candidates = read_json(pilot / "chain_source" / "feature_candidates.json")
    fields = read_json(pilot / "chain_source" / "design_parameters.json")["fields"]
    graph = read_json(graph_path)
    design = adapt_design(graph["case_id"], read_json(raw_path), candidates, fields)
    target = make_target(design, graph, candidates)
    if annotations:
        target = apply_annotations(design, target, read_json(annotations))
    output.mkdir(parents=True)
    write_json(output / "design.json", design)
    write_json(output / "target.json", target)
    write_json(output / "annotations.template.json", {
        "schema_version": VERSION, "case_id": design["case_id"], "reviewer": "",
        "feature_members": {}, "event_overrides": {},
    })
    manifest = {"schema_version": VERSION, "cases": [{
        "case_id": design["case_id"], "family_id": family_id, "split": "train",
        "design": "design.json", "target": "target.json",
    }]}
    write_json(output / "manifest.json", manifest)
    write_json(output / "provenance.json", {
        "pilot": str(pilot.resolve()), "association_graph_sha256": file_hash(graph_path),
        "raw_design_sha256": file_hash(raw_path),
        "annotations": str(annotations.resolve()) if annotations else None,
        "annotations_sha256": file_hash(annotations) if annotations else None,
        "design_sha256": file_hash(output / "design.json"),
        "target_sha256": file_hash(output / "target.json"),
        "scope": "single_case_pipeline_only_not_a_train_validation_test_split",
    })
    return {"case_id": design["case_id"], "entities": len(design["entities"]),
            "feature_seeds": len(design["features"]), "events": len(target["events"])}


def prepare_raw_design(raw_path: Path, project: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite design: {output}")
    project = project.resolve()
    sys.path.insert(0, str(project))
    research_scripts = Path(__file__).resolve().parents[4] / "scripts"
    sys.path.insert(0, str(research_scripts))
    pilot_adapter = importlib.import_module("build_pilot_pair")
    field_adapter = importlib.import_module("src.TQ.table_process.product_field_extraction")
    geometry_adapter = importlib.import_module("src.TQ.table_process.product_geometry_extraction")
    if Path(pilot_adapter.__file__).resolve() != research_scripts / "build_pilot_pair.py":
        raise ValueError("Wrong research-side design adapter imported")

    for module_name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if module_name.startswith("src.TQ") and filename and project not in Path(filename).resolve().parents:
            raise ValueError(f"Cross-project import detected: {module_name}")
    raw = read_json(raw_path)
    fields = field_adapter.normalize_product_drawing_fields(field_adapter.extract_product_drawing_fields(raw))
    field_map = {f["field_name"]: f["value"] for f in fields}
    case_id = field_map.get("零件型号")
    if not case_id or geometry_adapter.detect_ring_product_type(field_map) != "外圈":
        raise ValueError("Raw adapter currently requires an identifiable outer-ring design")
    params = geometry_adapter.build_design_part_params(raw, ring_type="外圈", field_map=field_map)
    candidates = pilot_adapter.build_feature_candidates(case_id, params, raw)
    write_json(output, adapt_design(case_id, raw, candidates, fields))
