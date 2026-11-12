from __future__ import annotations

from typing import Any

from farmcad.utils import finite, require_keys

VERSION = "farmcad-t2-v1"
ACTIONS = ("OP", "PARAM", "END_OP", "EOS")
ROLES = ("machining_target", "reference_copy", "inspection_target", "datum_use")
UPDATES = ("unknown", "design_copy", "state_copy", "external", "lookup", "derive")
SPECIALS = ("NONE", "UNKNOWN", "EXTERNAL")
QUALITIES = ("gold", "silver", "system", "candidate", "unknown")
VALUE_KEYS = ("nominal", "lower", "upper", "direct")
ENTITY_TYPES = ("LINE", "ARC", "CIRCLE", "DIMENSION", "TEXT", "MTEXT",
                "POLYLINE", "LWPOLYLINE", "ELLIPSE", "INSERT", "OTHER")
LABEL_FIELDS = ("features", "requirements", "sources", "roles", "update", "value")


def unknown_value() -> dict:
    return {**{k: None for k in VALUE_KEYS}, "unit": "unknown"}


def label(value: Any, quality: str = "unknown") -> dict:
    if quality not in QUALITIES:
        raise ValueError(f"Unknown label quality: {quality}")
    return {"value": value, "quality": quality}


def validate_value(value: dict) -> None:
    require_keys(value, set(VALUE_KEYS) | {"unit"}, set(VALUE_KEYS) | {"unit"}, "value")
    if value["unit"] not in ("mm", "um", "unknown"):
        raise ValueError("Unsupported value unit")
    for key in VALUE_KEYS:
        if value[key] is not None and not finite(value[key]):
            raise ValueError(f"Nonfinite value: {key}")
    if value["lower"] is not None and value["upper"] is not None and value["lower"] > value["upper"]:
        raise ValueError("Reversed tolerance")


def validate_design(design: dict) -> None:
    required = {"schema_version", "case_id", "entities", "edges", "features", "requirements", "context"}
    require_keys(design, required, required, "design input")
    if design["schema_version"] != VERSION:
        raise ValueError("Unsupported design schema")
    if not isinstance(design["case_id"], str) or not design["case_id"]:
        raise ValueError("Missing case identity")
    if not isinstance(design["context"], str):
        raise ValueError("Context must be observable design text, not process metadata")
    entities = {}
    for entity in design["entities"]:
        require_keys(entity, {"id", "type", "numeric", "text"}, {"id", "type", "numeric", "text"}, "entity")
        if entity["id"] in entities or entity["type"] not in ENTITY_TYPES:
            raise ValueError("Duplicate entity or unsupported type")
        if len(entity["numeric"]) != 12 or not all(finite(n) for n in entity["numeric"]):
            raise ValueError("Entity numeric vector must have 12 finite entries")
        if not isinstance(entity["text"], str):
            raise ValueError("Entity text must be a string")
        entities[entity["id"]] = entity
    if not entities:
        raise ValueError("Design has no entities")
    seen_edges = set()
    for source, target, kind in design["edges"]:
        key = (source, target, kind)
        if key in seen_edges or source not in range(len(entities)) or target not in range(len(entities)) or kind not in range(3):
            raise ValueError("Invalid or duplicate design edge")
        seen_edges.add(key)
    features = {}
    for feature in design["features"]:
        keys = {"id", "text", "value", "anchor_entities"}
        require_keys(feature, keys, keys, "feature seed")
        if feature["id"] in features or feature["id"] in SPECIALS:
            raise ValueError("Duplicate/reserved feature identity")
        validate_value(feature["value"])
        if any(i not in entities for i in feature["anchor_entities"]):
            raise ValueError("Feature anchor references missing entity")
        features[feature["id"]] = feature
    reqs = set()
    for requirement in design["requirements"]:
        keys = {"id", "feature_id", "text", "lower", "upper", "evidence_entities"}
        require_keys(requirement, keys, keys, "design requirement")
        if requirement["id"] in reqs or requirement["id"] in SPECIALS or requirement["feature_id"] not in features:
            raise ValueError("Invalid requirement identity or feature")
        if not all(finite(requirement[k]) for k in ("lower", "upper")) or requirement["lower"] > requirement["upper"]:
            raise ValueError("Invalid requirement interval")
        if any(i not in entities for i in requirement["evidence_entities"]):
            raise ValueError("Requirement evidence missing")
        reqs.add(requirement["id"])


def validate_target(design: dict, target: dict) -> None:
    keys = {"schema_version", "case_id", "reviewer", "events", "feature_members"}
    require_keys(target, keys, keys, "training target")
    if target["schema_version"] != VERSION or target["case_id"] != design["case_id"]:
        raise ValueError("Target/design identity mismatch")
    feature_ids = {f["id"] for f in design["features"]}
    requirement_ids = {r["id"] for r in design["requirements"]}
    entity_ids = {e["id"] for e in design["entities"]}
    states: dict[str, int] = {}
    seen_ids = set()
    in_operation = False
    operation_index = -1
    ended = False
    for event in target["events"]:
        action = event["action"]
        common = {"id", "action", "quality"}
        allowed = common | ({"operation", "parameters_complete"} if action == "OP" else set())
        if action == "PARAM":
            allowed |= {"parameter", "labels"}
        require_keys(event, allowed, allowed, "target event")
        if event["id"] in seen_ids or event["id"] in SPECIALS or ended or action not in ACTIONS or event["quality"] not in QUALITIES:
            raise ValueError("Invalid event identity, action, quality or event after EOS")
        if event["quality"] == "gold" and not target["reviewer"].strip():
            raise ValueError("Gold supervision requires a named reviewer")
        seen_ids.add(event["id"])
        if action == "OP":
            if in_operation or not isinstance(event["operation"], str) or not event["operation"] or not isinstance(event["parameters_complete"], bool):
                raise ValueError("OP must open a new operation")
            in_operation = True
            operation_index += 1
        elif action == "END_OP":
            if not in_operation:
                raise ValueError("END_OP without operation")
            in_operation = False
        elif action == "EOS":
            if in_operation or operation_index < 0:
                raise ValueError("EOS before a complete operation")
            ended = True
        else:
            if not in_operation or not event["parameter"]:
                raise ValueError("PARAM outside operation or unnamed parameter")
            require_keys(event["labels"], set(LABEL_FIELDS), set(LABEL_FIELDS), "labels")
            for name, annotation in event["labels"].items():
                require_keys(annotation, {"value", "quality"}, {"value", "quality"}, name)
                value, quality = annotation["value"], annotation["quality"]
                if quality not in QUALITIES or (quality == "gold" and not target["reviewer"].strip()):
                    raise ValueError("Invalid supervision quality/reviewer")
                if name in ("features", "requirements", "sources"):
                    choices = feature_ids if name == "features" else requirement_ids if name == "requirements" else set(states)
                    if value is not None:
                        if not isinstance(value, list) or len(value) != len(set(value)):
                            raise ValueError("Pointer targets must be a unique list or null")
                        if any(v not in choices and v not in SPECIALS for v in value):
                            raise ValueError(f"Unknown/future pointer target in {name}: {value}")
                        if any(v in SPECIALS for v in value) and len(value) != 1:
                            raise ValueError("Special pointers cannot mix with concrete objects")
                        if name == "sources" and any(v in states and states[v] >= operation_index for v in value):
                            raise ValueError("Source state must precede current operation")
                elif name == "roles":
                    if value is not None and (not isinstance(value, list) or len(value) != len(set(value)) or any(v not in ROLES for v in value)):
                        raise ValueError("Invalid role labels")
                elif name == "update":
                    if value is not None and value not in UPDATES:
                        raise ValueError("Invalid update operator")
                elif value is not None:
                    validate_value(value)
            states[event["id"]] = operation_index
    if not ended:
        raise ValueError("Target missing EOS")
    for fid, annotation in target["feature_members"].items():
        if fid not in feature_ids:
            raise ValueError("Geometry labels reference missing feature")
        require_keys(annotation, {"value", "quality"}, {"value", "quality"}, "feature members")
        if annotation["quality"] not in QUALITIES:
            raise ValueError("Unknown geometry label quality")
        if annotation["quality"] == "gold" and not target["reviewer"].strip():
            raise ValueError("Gold geometry supervision requires reviewer")
        if not isinstance(annotation["value"], list) or any(e not in entity_ids for e in annotation["value"]):
            raise ValueError("Geometry labels reference missing entities")
