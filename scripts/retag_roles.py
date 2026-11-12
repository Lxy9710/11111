"""Retag the `roles` label of an existing dataset with the current operation semantics.

The dataset stores the generated `design.json` and `target.json` per case. When
a rule that shapes the target changes - for example the operation semantics
that decide whether a parameter is machined, copied, inspected or used as a
datum - the stored targets are stale. Rebuilding them from scratch would mean
re-running the whole extraction and route pipeline, and would discard the
engineer's gold labels.

This script instead recomputes only the affected label. It reads the stored
target, re-derives the role from the operation name and the parameter's
provenance, and rewrites the `roles` label with the matching supervision level.
Everything else in the target is left byte-identical.

A label that an engineer confirmed as gold is never overwritten. The engineer
decides, not the rule.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chainmodel" / "src"))

from farmcad.data.prepare import parameter_role  # noqa: E402
from farmcad.data.schema import validate_target  # noqa: E402


def retag_target(target: dict) -> tuple[dict, list[dict]]:
    """Recompute the `roles` label of every PARAM event."""

    operation_of: dict[str, str] = {}
    for event in target["events"]:
        if event["action"] == "OP":
            operation_of[event["id"]] = event["operation"]

    def operation_name(event_id: str) -> str:
        parts = event_id.split(":")
        if len(parts) >= 4 and parts[1] == "state":
            return operation_of.get(f"{parts[0]}:operation:{parts[2]}", "")
        return ""

    changes: list[dict] = []
    result = json.loads(json.dumps(target))
    for event in result["events"]:
        if event["action"] != "PARAM":
            continue
        current = event["labels"]["roles"]
        if current["quality"] == "gold":
            # A human decision outranks a rule.
            continue
        operation = operation_name(event["id"])
        source_type = ""
        update = event["labels"]["update"]["value"]
        if update == "state_copy":
            source_type = "copy_from_other_process"
        elif update == "external":
            source_type = "external_source"
        bound = bool(event["labels"]["features"]["value"])
        roles, basis = parameter_role(operation, event["parameter"], source_type, bound)
        if roles is None:
            new = {"value": None, "quality": "unknown"}
        elif basis == "feature_binding_heuristic":
            new = {"value": roles, "quality": "candidate"}
        else:
            new = {"value": roles, "quality": "system"}
        if new != current:
            changes.append({
                "event_id": event["id"],
                "operation": operation,
                "parameter": event["parameter"],
                "from": current,
                "to": new,
                "basis": basis,
            })
            event["labels"]["roles"] = new
    return result, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path,
                        help="Dataset directory with manifest.json and cases/.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes. Without it, only report them.")
    parser.add_argument("--report", type=Path,
                        help="Optional CSV path for the per-event changes.")
    args = parser.parse_args()

    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    all_changes: list[dict] = []
    touched = 0
    for entry in manifest["cases"]:
        case_dir = args.dataset / Path(entry["design"]).parent
        target_path = case_dir / "target.json"
        design = json.loads((case_dir / "design.json").read_text(encoding="utf-8"))
        target = json.loads(target_path.read_text(encoding="utf-8"))
        retagged, changes = retag_target(target)
        if not changes:
            continue
        touched += 1
        for change in changes:
            change["case_id"] = entry["case_id"]
            all_changes.append(change)
        if args.apply:
            validate_target(design, retagged)
            target_path.write_text(
                json.dumps(retagged, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.report:
        import csv
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "case_id", "event_id", "operation", "parameter",
                "from_value", "from_quality", "to_value", "to_quality", "basis"])
            writer.writeheader()
            for change in all_changes:
                writer.writerow({
                    "case_id": change["case_id"],
                    "event_id": change["event_id"],
                    "operation": change["operation"],
                    "parameter": change["parameter"],
                    "from_value": json.dumps(change["from"]["value"], ensure_ascii=False),
                    "from_quality": change["from"]["quality"],
                    "to_value": json.dumps(change["to"]["value"], ensure_ascii=False),
                    "to_quality": change["to"]["quality"],
                    "basis": change["basis"],
                })

    summary: dict[str, int] = {}
    for change in all_changes:
        key = f"{change['to']['quality']}:{change['basis']}"
        summary[key] = summary.get(key, 0) + 1
    print(json.dumps({
        "dataset": str(args.dataset.resolve()),
        "applied": args.apply,
        "cases_touched": touched,
        "labels_changed": len(all_changes),
        "by_outcome": summary,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
