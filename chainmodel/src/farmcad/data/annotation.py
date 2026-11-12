"""Human annotation round-trip: worksheet export and strict re-import.

The engineer's edits happen outside this repository, in a spreadsheet and in the
upstream ProcessCAD frontend. The only thing this module guarantees is that
those edits arrive back as *gold* supervision without silently changing the
data they annotate.

Three properties make the transfer safe:

1. **Binding.** A worksheet carries the SHA256 of the exact `design.json` and
   `target.json` it was generated from. An import against changed inputs is
   refused, so a stale worksheet cannot be applied to a newer case.
2. **Addressing.** Every row is keyed by the event id that already exists in
   `target.json`. Unknown ids are refused rather than created.
3. **Scope.** The engineer states, per row, which part of the annotation they
   actually confirmed. Only the confirmed fields are promoted to `gold`; the
   rest keep their original supervision level. A field the engineer did not
   touch is never promoted.

The three-state absence semantics of `farmcad-t2-v1` are preserved on import:
`[]` or `NONE` means "confirmed absent", `UNKNOWN` means "confirmed uncertain",
and an empty cell means "not reviewed by the engineer".
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from farmcad.data.schema import (
    LABEL_FIELDS,
    ROLES,
    SPECIALS,
    UPDATES,
    VERSION,
    validate_target,
)
from farmcad.utils import file_hash, write_json

POINTER_FIELDS = ("features", "requirements", "sources")
LABEL_FIELDS_ORDER = ("features", "requirements", "sources", "roles", "update", "value")

# Action vocabulary for the `action` column.
ACTION_ALL = "confirm"       # the whole row is correct as generated
ACTION_VALUE = "value"       # only the numeric value is confirmed
ACTION_POINTER = "pointer"   # only the object/role/update labels are confirmed
ACTION_SKIP = "skip"         # nothing is confirmed; the row keeps its level
ACTIONS = (ACTION_ALL, ACTION_VALUE, ACTION_POINTER, ACTION_SKIP)

# Value columns. `lower`/`upper` are relative deviations, exactly as in the
# target value label, not absolute interval end points.
VALUE_COLUMNS = ("value_nominal", "value_lower_dev", "value_upper_dev",
                 "value_direct", "value_unit")

PARAM_SHEET_COLUMNS = (
    ("event_id", "事件ID（勿改）"),
    ("operation", "工序"),
    ("parameter", "参数"),
    ("action", "确认范围(confirm/value/pointer/skip)"),
    ("features", "特征ID(分号分隔)"),
    ("requirements", "要求ID(分号分隔)"),
    ("sources", "来源事件ID(分号分隔)"),
    ("roles", "作用(machining_target/reference_copy/inspection_target/datum_use)"),
    ("update", "更新算子(unknown/design_copy/state_copy/external/lookup/derive)"),
    ("value_nominal", "名义值"),
    ("value_lower_dev", "下偏差"),
    ("value_upper_dev", "上偏差"),
    ("value_direct", "直接值"),
    ("value_unit", "单位(mm/um/unknown)"),
    ("current_features", "当前特征"),
    ("current_requirements", "当前要求"),
    ("current_sources", "当前来源"),
    ("current_roles", "当前作用"),
    ("current_update", "当前算子"),
    ("current_value", "当前数值"),
    ("note", "备注"),
)

GEOMETRY_SHEET_COLUMNS = (
    ("feature_id", "特征ID（勿改）"),
    ("feature_text", "特征说明"),
    ("action", "确认范围(confirm/skip)"),
    ("members", "成员实体ID(分号分隔，完整集合)"),
    ("current_members", "当前成员"),
    ("note", "备注"),
)

WORKSHEET_INSTRUCTIONS = """# 标注说明

## 你要做什么

每个案例一张标注表，`params.csv` 是一行一个加工参数。表里已经填好了系统
自动生成的结果（`current_*` 列）。你要做的是**核对并纠正**它们。

## 每行填什么

只改 `action` 列和需要纠正的列，其余列保持不动。

`action` 列四个取值：

| 取值 | 含义 |
|---|---|
| `skip` | 这一行我不核对（**默认值，保持不动**） |
| `confirm` | 整行都对，参数、数值、来源、作用全部确认 |
| `value` | 只确认数值（名义值、偏差、单位），其余不表态 |
| `pointer` | 只确认关联关系（特征、要求、来源、作用、算子），数值不表态 |

## 空格的含义（重要）

关联类字段（特征/要求/来源/作用/算子）：

- 填了 ID 列表 = 确认就是这样
- 填 `NONE` = **确认这里确实没有**（比如这道工序不复制任何前序参数）
- 留空 = 我对这一项**没有表态**，不会影响训练

数值字段（名义值/偏差/单位）：当 `action` 含 `value` 时，这一行的数值是**整体**
被确认的，所以留空表示"没有这个分量"（比如确实没有上偏差）。

## 数值里填什么

`value_lower_dev` / `value_upper_dev` 填的是**相对偏差**，不是绝对值。
比如图纸写 `Φ52.6 0.15/0`，就填 `value_nominal=52.6`、`value_lower_dev=0.15`、
`value_upper_dev=0`。单位填 `mm`。

## 三个容易出错的地方

1. **来源必须是更早的工序**。比如第 110 道工序引用第 100 道的参数是可以的，
   反过来会被拒绝并报错。
2. **别把编号抄错**。`features` 里的 ID 必须和 `params.csv` 里其他行、
   或 `design.json` 的 `features` 对得上，写错会直接报错，不会静默忽略。
3. **确认范围要老实**。`confirm` 表示整行都对；如果只是数值对，请填 `value`。
   标错范围会让训练权重虚高。

## 几何成员表

`geometry.csv` 是特征和图纸实体的对应关系。这里的 `members` 要填**完整的**
成员集合（不是"新增的那几个"）。留空表示不表态。
"""



class AnnotationError(ValueError):
    """A worksheet that cannot be imported without losing meaning."""


# ---------------------------------------------------------------- export ---


def _current_value_text(value: dict | None) -> str:
    if not value:
        return ""
    parts = []
    for key in ("nominal", "lower", "upper", "direct"):
        item = value.get(key)
        if item is not None:
            parts.append(f"{key}={item}")
    if value.get("unit") not in (None, "unknown"):
        parts.append(f"unit={value['unit']}")
    return ";".join(parts)


def _join(values: Iterable[Any] | None) -> str:
    if not values:
        return ""
    return ";".join(str(v) for v in values)


def export_param_sheet(design: dict, target: dict) -> list[dict]:
    """One row per PARAM event, with the generated annotation as reference."""

    # A PARAM event id is `<part>:state:<process_no>:<symbol>` and its OP event
    # id is `<part>:operation:<process_no>`, so the process number is the join.
    operation_of: dict[str, str] = {}
    for event in target["events"]:
        if event["action"] == "OP":
            operation_of[event["id"]] = event["operation"]

    def operation_name(event_id: str) -> str:
        parts = event_id.split(":")
        if len(parts) >= 4 and parts[1] == "state":
            return operation_of.get(f"{parts[0]}:operation:{parts[2]}", "")
        return ""

    rows = []
    for event in target["events"]:
        if event["action"] != "PARAM":
            continue
        labels = event["labels"]
        rows.append({
            "event_id": event["id"],
            "operation": operation_name(event["id"]),
            "parameter": event["parameter"],
            "action": ACTION_SKIP,
            "features": _join(labels["features"]["value"]),
            "requirements": _join(labels["requirements"]["value"]),
            "sources": _join(labels["sources"]["value"]),
            "roles": _join(labels["roles"]["value"]),
            "update": labels["update"]["value"] or "",
            "value_nominal": "" if labels["value"]["value"] is None else labels["value"]["value"].get("nominal", ""),
            "value_lower_dev": "" if labels["value"]["value"] is None else labels["value"]["value"].get("lower", ""),
            "value_upper_dev": "" if labels["value"]["value"] is None else labels["value"]["value"].get("upper", ""),
            "value_direct": "" if labels["value"]["value"] is None else labels["value"]["value"].get("direct", ""),
            "value_unit": "" if labels["value"]["value"] is None else labels["value"]["value"].get("unit", ""),
            "current_features": _join(labels["features"]["value"]),
            "current_requirements": _join(labels["requirements"]["value"]),
            "current_sources": _join(labels["sources"]["value"]),
            "current_roles": _join(labels["roles"]["value"]),
            "current_update": labels["update"]["value"] or "",
            "current_value": _current_value_text(labels["value"]["value"]),
            "note": "",
        })
    return rows


def export_geometry_sheet(design: dict, target: dict) -> list[dict]:
    """One row per design feature, for the geometry membership annotation."""

    members = target.get("feature_members") or {}
    rows = []
    for feature in design["features"]:
        current = (members.get(feature["id"]) or {}).get("value") or []
        rows.append({
            "feature_id": feature["id"],
            "feature_text": feature["text"],
            "action": ACTION_SKIP,
            "members": _join(current),
            "current_members": _join(current),
            "note": "",
        })
    return rows


def write_worksheet(path: Path, rows: list[dict], columns) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[c for c, _ in columns])
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c, _ in columns})


def export_worksheets(case_dir: Path, output_dir: Path) -> dict:
    """Write `params.csv` and `geometry.csv` bound to the current inputs."""

    design_path = case_dir / "design.json"
    target_path = case_dir / "target.json"
    if not design_path.exists() or not target_path.exists():
        raise AnnotationError(f"Case directory lacks design.json/target.json: {case_dir}")
    design = json.loads(design_path.read_text(encoding="utf-8-sig"))
    target = json.loads(target_path.read_text(encoding="utf-8-sig"))
    validate_target(design, target)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_worksheet(output_dir / "params.csv", export_param_sheet(design, target), PARAM_SHEET_COLUMNS)
    write_worksheet(output_dir / "geometry.csv", export_geometry_sheet(design, target), GEOMETRY_SHEET_COLUMNS)
    (output_dir / "README.md").write_text(WORKSHEET_INSTRUCTIONS, encoding="utf-8")
    binding = {
        "schema_version": VERSION,
        "case_id": design["case_id"],
        "design_sha256": file_hash(design_path),
        "target_sha256": file_hash(target_path),
        "reviewer": "",
        "reviewed_at": "",
        "notes": "",
    }
    write_json(output_dir / "binding.json", binding)
    return {
        "case_id": design["case_id"],
        "param_rows": len(export_param_sheet(design, target)),
        "feature_rows": len(design["features"]),
        "output": str(output_dir.resolve()),
    }


# ---------------------------------------------------------------- import ---


def _read_csv(path: Path, required: set[str]) -> list[dict]:
    if not path.exists():
        raise AnnotationError(f"Missing worksheet: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise AnnotationError(f"Empty worksheet: {path}")
        missing = required - set(reader.fieldnames)
        if missing:
            raise AnnotationError(f"Worksheet lacks columns {sorted(missing)}: {path}")
        return [{(k or "").strip(): (v if v is None else v.strip()) for k, v in row.items()}
                for row in reader]


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _number(text: str, field: str, event_id: str):
    if text == "":
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise AnnotationError(f"{event_id}: {field} is not a number: {text!r}") from exc


def _pointer(text: str, field: str, event_id: str) -> list[str] | None:
    if text == "":
        return None
    items = _split(text)
    if not items:
        return []
    if any(item in SPECIALS for item in items):
        if len(items) != 1:
            raise AnnotationError(
                f"{event_id}: {field} mixes a special pointer with concrete ids: {items}")
    return items


def _roles(text: str, event_id: str) -> list[str] | None:
    if text == "":
        return None
    items = _split(text)
    bad = [item for item in items if item not in ROLES]
    if bad:
        raise AnnotationError(f"{event_id}: unknown role {bad}; allowed {list(ROLES)}")
    if len(items) != len(set(items)):
        raise AnnotationError(f"{event_id}: duplicate roles {items}")
    return items


def _update(text: str, event_id: str) -> str | None:
    if text == "":
        return None
    if text not in UPDATES:
        raise AnnotationError(f"{event_id}: unknown update operator {text!r}; allowed {list(UPDATES)}")
    return text


def _value(texts: dict, event_id: str, fallback: dict | None) -> dict | None:
    unit = texts.get("value_unit", "")
    if unit == "":
        unit = (fallback or {}).get("unit", "unknown")
    if unit not in ("mm", "um", "unknown"):
        raise AnnotationError(f"{event_id}: value_unit must be mm, um or unknown, got {unit!r}")
    numeric = {
        "nominal": _number(texts.get("value_nominal", ""), "nominal", event_id),
        "lower": _number(texts.get("value_lower_dev", ""), "lower", event_id),
        "upper": _number(texts.get("value_upper_dev", ""), "upper", event_id),
        "direct": _number(texts.get("value_direct", ""), "direct", event_id),
    }
    if all(item is None for item in numeric.values()) and unit == "unknown" and fallback is None:
        return None
    if numeric["lower"] is not None and numeric["upper"] is not None and numeric["lower"] > numeric["upper"]:
        raise AnnotationError(f"{event_id}: lower deviation is larger than upper deviation")
    return {**numeric, "unit": unit}


def _check_pointer_targets(design: dict, target: dict, event_id: str, field: str,
                           values: list[str] | None) -> None:
    if values is None:
        return
    if field == "features":
        allowed = {f["id"] for f in design["features"]}
    elif field == "requirements":
        allowed = {r["id"] for r in design["requirements"]}
    else:
        allowed = {e["id"] for e in target["events"] if e["action"] == "PARAM"}
    unknown = [v for v in values if v not in allowed and v not in SPECIALS]
    if unknown:
        raise AnnotationError(f"{event_id}: {field} points at unknown ids {unknown}")


def _check_source_precedence(target: dict, event_id: str, values: list[str] | None) -> None:
    """A source must be a PARAM event that strictly precedes the current operation."""

    if not values:
        return
    order: dict[str, int] = {}
    operation_index = -1
    for event in target["events"]:
        if event["action"] == "OP":
            operation_index += 1
        elif event["action"] == "PARAM":
            order[event["id"]] = operation_index
    if event_id not in order:
        raise AnnotationError(f"{event_id}: not a PARAM event")
    current = order[event_id]
    for value in values:
        if value in SPECIALS:
            continue
        if value not in order:
            raise AnnotationError(f"{event_id}: source {value} is not a PARAM event")
        if order[value] >= current:
            raise AnnotationError(
                f"{event_id}: source {value} does not precede the current operation")


def import_worksheets(case_dir: Path, worksheet_dir: Path, output_dir: Path,
                       reviewer: str = "") -> dict:
    """Merge reviewed worksheets into a new gold-labelled case directory.

    The source case directory is never modified. The result is written to
    `output_dir` together with a `review_log.json` that lists every field that
    was promoted to `gold`.
    """

    design_path = case_dir / "design.json"
    target_path = case_dir / "target.json"
    design = json.loads(design_path.read_text(encoding="utf-8-sig"))
    target = json.loads(target_path.read_text(encoding="utf-8-sig"))
    validate_target(design, target)

    binding_path = worksheet_dir / "binding.json"
    if not binding_path.exists():
        raise AnnotationError(f"Missing binding.json: {binding_path}")
    binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
    if binding.get("case_id") != design["case_id"]:
        raise AnnotationError(
            f"Worksheet belongs to {binding.get('case_id')}, case is {design['case_id']}")
    if binding.get("design_sha256") != file_hash(design_path):
        raise AnnotationError("design.json changed after the worksheet was generated")
    if binding.get("target_sha256") != file_hash(target_path):
        raise AnnotationError("target.json changed after the worksheet was generated")

    who = (reviewer or binding.get("reviewer") or "").strip()
    if not who:
        raise AnnotationError("A reviewer identity is required to record gold supervision")
    if binding.get("reviewer") and binding["reviewer"].strip() != who:
        raise AnnotationError(
            f"Reviewer mismatch: binding says {binding['reviewer']!r}, import says {who!r}")

    param_rows = _read_csv(worksheet_dir / "params.csv", {"event_id", "action"})
    events = {e["id"]: e for e in target["events"]}

    promoted: list[dict] = []
    result = json.loads(json.dumps(target))
    result["reviewer"] = who

    for row in param_rows:
        event_id = row["event_id"]
        action = (row.get("action") or "").lower()
        if action == "":
            continue
        if action not in ACTIONS:
            raise AnnotationError(f"{event_id}: unknown action {action!r}; allowed {list(ACTIONS)}")
        if action == ACTION_SKIP:
            continue
        if event_id not in events:
            raise AnnotationError(f"{event_id}: no such event in target.json")
        event = events[event_id]
        if event["action"] != "PARAM":
            raise AnnotationError(f"{event_id}: only PARAM events can be annotated")
        labels = event["labels"]
        if action == ACTION_ALL:
            fields = set(LABEL_FIELDS)
        elif action == ACTION_VALUE:
            fields = {"value"}
        else:
            fields = set(LABEL_FIELDS) - {"value"}

        new_labels = dict(labels)
        for field in fields:
            if field in POINTER_FIELDS:
                values = _pointer(row.get(field, ""), field, event_id)
                if values is None:
                    continue
                _check_pointer_targets(design, target, event_id, field, values)
                if field == "sources":
                    _check_source_precedence(target, event_id, values)
            elif field == "roles":
                values = _roles(row.get(field, ""), event_id)
                if values is None:
                    continue
            elif field == "update":
                values = _update(row.get(field, ""), event_id)
                if values is None:
                    continue
            else:
                values = _value(row, event_id, labels["value"]["value"])
                if values is None:
                    continue
            new_labels[field] = {"value": values, "quality": "gold"}
            promoted.append({"event_id": event_id, "field": field,
                             "value": values, "action": action})
        event = dict(event)
        event["labels"] = new_labels
        result["events"] = [event if e["id"] == event_id else e for e in result["events"]]

    geometry_rows = _read_csv(worksheet_dir / "geometry.csv", {"feature_id", "action"}) if (worksheet_dir / "geometry.csv").exists() else []
    entity_ids = {e["id"] for e in design["entities"]}
    feature_ids = {f["id"] for f in design["features"]}
    for row in geometry_rows:
        feature_id = row.get("feature_id", "")
        action = (row.get("action") or "").lower()
        if action == "":
            continue
        if action not in (ACTION_ALL, ACTION_SKIP):
            raise AnnotationError(f"{feature_id}: unknown action {action!r}")
        if action == ACTION_SKIP:
            continue
        if feature_id not in feature_ids:
            raise AnnotationError(f"{feature_id}: no such design feature")
        members = _split(row.get("members", ""))
        unknown = [m for m in members if m not in entity_ids]
        if unknown:
            raise AnnotationError(f"{feature_id}: unknown entity ids {unknown}")
        result["feature_members"][feature_id] = {"value": members, "quality": "gold"}
        promoted.append({"event_id": feature_id, "field": "feature_members",
                         "value": members, "action": action})

    validate_target(design, result)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "design.json", design)
    write_json(output_dir / "target.json", result)
    write_json(output_dir / "annotations.template.json", {
        "schema_version": VERSION, "case_id": design["case_id"], "reviewer": who,
        "feature_members": {}, "event_overrides": {},
    })
    write_json(output_dir / "review_log.json", {
        "schema_version": VERSION,
        "case_id": design["case_id"],
        "reviewer": who,
        "reviewed_at": binding.get("reviewed_at", ""),
        "design_sha256": binding.get("design_sha256"),
        "target_sha256": binding.get("target_sha256"),
        "promoted_fields": promoted,
        "promoted_count": len(promoted),
        "notes": binding.get("notes", ""),
    })

    gold_events = sum(1 for e in result["events"]
                      if e["action"] == "PARAM" and e["labels"]["value"]["quality"] == "gold")
    return {
        "case_id": design["case_id"],
        "reviewer": who,
        "promoted_fields": len(promoted),
        "gold_parameter_events": gold_events,
        "gold_feature_members": len(result["feature_members"]),
        "output": str(output_dir.resolve()),
    }
