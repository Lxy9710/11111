"""Import human edits made in the upstream ProcessCAD frontend as gold labels.

The engineer runs an experiment in the ProcessCAD web frontend, corrects the
generated parameters there, and the corrected values are persisted by the
platform. This script reads those persisted edits and converts them into the
annotation worksheet format that `farmcad.data.annotation` imports.

Where the platform stores the edits
-----------------------------------

The platform keeps the human corrections in two places, and this script reads
both because they record different things:

- `output/_task_runtime/<task>/task_snapshot.json`
  `result.manual_table_edits.applied_edits` - table cells the engineer typed
  into directly. Each entry carries the old text, the new text, the target
  cell or entity handle, and the process index.
- `output/_task_runtime/<task>/stages/rule_llm/attempts/<n>/output.json`
  `plan_details[].changes[]` - the engine's proposal for every field, with
  `current_value`, `rule_value`, `rule_evidence` and `confidence`.

The working cards under `temp/modifier/<task>/*.json` carry the resolved
parameter block per process page, which is what makes the join to our events
possible.

Matching upstream artefacts to our events
-----------------------------------------

The upstream platform and this dataset both identify a process by the pair
(part number, process number), and a parameter by its symbol. Upstream card
file names carry exactly that pair, `C180506K1NW1R3U(H).56_115_粗磨外径.json`,
and our events are keyed `C180506K1NW1R3U(H).56:state:115:Dc`. The join is
therefore exact and needs no fuzzy matching: `(part_number, process_no,
parameter_symbol)`.

What this script does not do
----------------------------

It does not decide that a value is correct. A value the engineer confirmed by
leaving it alone is still a system proposal. Only the rows the engineer
actually touched are marked for promotion; everything else keeps its level.

The output is a worksheet-shaped CSV with the same columns as the worksheet
`params.csv`, so it can be merged into an exported worksheet or inspected on
its own. It never edits `design.json` or `target.json`.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chainmodel" / "src"))

from farmcad.data.annotation import PARAM_SHEET_COLUMNS  # noqa: E402

CARD_NAME = re.compile(r"^(?P<part>.+?)_(?P<process_no>\d+)_(?P<process>.+)\.json$")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def card_files(modifier_dir: Path) -> list[tuple[Path, dict]]:
    """Parse `part_processNo_process.json` card file names."""

    found = []
    for path in sorted(modifier_dir.glob("*.json")):
        if path.name.endswith(".backup"):
            continue
        match = CARD_NAME.match(path.name)
        if not match:
            continue
        found.append((path, match.groupdict()))
    return found


def collect_manual_edits(task_dir: Path) -> list[dict]:
    """Human table edits from the persisted task snapshot."""

    snapshot = task_dir / "task_snapshot.json"
    if not snapshot.exists():
        return []
    payload = read_json(snapshot)
    payload = payload.get("payload") or payload
    result = payload.get("result") or {}
    edits = ((result.get("manual_table_edits") or {}).get("applied_edits") or [])
    rows = []
    for edit in edits:
        rows.append({
            "field_name": str(edit.get("field_name") or ""),
            "process_name": str(edit.get("process_name") or ""),
            "process_index": edit.get("process_index"),
            "old_text": str(edit.get("old_text") or ""),
            "new_text": str(edit.get("new_text") or ""),
            "table_id": str(edit.get("table_id") or ""),
            "target_cell": str(edit.get("target_cell") or ""),
            "handle": str(edit.get("handle") or ""),
        })
    return rows


def collect_proposals(task_dir: Path) -> list[dict]:
    """Engine proposals from the rule_llm stage output."""

    rows = []
    stage_dir = task_dir / "stages" / "rule_llm" / "attempts"
    if not stage_dir.exists():
        return rows
    for output in sorted(stage_dir.glob("*/output.json")):
        data = read_json(output)
        for plan in data.get("plan_details") or []:
            for change in plan.get("changes") or []:
                rows.append({
                    "process_name": str(plan.get("process_name") or ""),
                    "param_name": str(change.get("param_name") or ""),
                    "param_id": str(change.get("param_id") or ""),
                    "current_value": str(change.get("current_value") or ""),
                    "rule_value": str(change.get("rule_value") or ""),
                    "change_type": str(change.get("change_type") or ""),
                    "rule_evidence": str(change.get("rule_evidence") or ""),
                    "confidence": change.get("confidence"),
                })
    return rows


def collect_cards(modifier_dir: Path) -> dict[tuple[str, str], dict]:
    """Resolved parameter blocks per `(part, process_no)` from the working cards."""

    cards: dict[tuple[str, str], dict] = {}
    for path, parts in card_files(modifier_dir):
        data = read_json(path)
        block = data.get("cage_patch") or data.get("mogong_patch") or {}
        parameters: dict[str, dict] = {}
        for process, info in (block or {}).items():
            if not isinstance(info, dict):
                continue
            for symbol, value in (info.get("parameters") or {}).items():
                if isinstance(value, dict):
                    parameters[symbol] = value
        key = (parts["part"], parts["process_no"])
        cards[key] = {
            "process": parts["process"],
            "parameters": parameters,
            "path": str(path),
        }
    return cards


def _to_number(text: str):
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace("−", "-"))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _deviations(text: str):
    """Parse a tolerance pair such as `16.5 +0.15 0` into (lower, upper)."""

    numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", text.replace("−", "-"))
    if len(numbers) >= 3:
        return float(numbers[-2]), float(numbers[-1])
    if len(numbers) == 2:
        return float(numbers[0]), float(numbers[1])
    return None, None


def build_rows(case_dir: Path, modifier_dir: Path, task_dirs: list[Path]) -> list[dict]:
    """Rows for the events the upstream artefacts can be matched to."""

    target = read_json(case_dir / "target.json")

    # Our event ids carry `part:state:process_no:symbol`.
    symbol_of = {}
    for event in target["events"]:
        if event["action"] != "PARAM":
            continue
        parts = event["id"].split(":")
        if len(parts) >= 4 and parts[1] == "state":
            symbol_of[(parts[0], parts[2], event["parameter"])] = event

    cards = collect_cards(modifier_dir)
    edits: list[dict] = []
    proposals: list[dict] = []
    for task_dir in task_dirs:
        edits.extend(collect_manual_edits(task_dir))
        proposals.extend(collect_proposals(task_dir))

    card_by_process: dict[str, dict[str, tuple[str, dict]]] = {}
    for (part, process_no), card in cards.items():
        card_by_process.setdefault(part, {})[card["process"]] = (process_no, card)

    rows: list[dict] = []
    handled: set[str] = set()

    # A human table edit wins over the engine proposal: it is the explicit
    # correction. The edit names the field and the process, so it is matched
    # through the working-card parameter table of that process.
    for edit in edits:
        field = edit["field_name"]
        if not field:
            continue
        for part, processes in card_by_process.items():
            for process, (process_no, card) in processes.items():
                if field not in card["parameters"]:
                    continue
                event = symbol_of.get((part, process_no, field))
                if event is None:
                    continue
                lower, upper = _deviations(edit["new_text"])
                rows.append({
                    "event_id": event["id"],
                    "operation": card["process"],
                    "parameter": field,
                    "action": "value",
                    "value_nominal": _to_number(edit["new_text"]),
                    "value_lower_dev": lower,
                    "value_upper_dev": upper,
                    "value_unit": "mm",
                    "note": f"engineer edit: {edit['old_text']} -> {edit['new_text']}",
                })
                handled.add(event["id"])

    # Working-card values that the engineer did not touch are system proposals.
    # They are reported for review but are not marked for promotion.
    for (part, process_no, symbol), event in symbol_of.items():
        if event["id"] in handled:
            continue
        card = cards.get((part, process_no))
        if not card:
            continue
        value = card["parameters"].get(symbol)
        if not isinstance(value, dict):
            continue
        nominal = value.get("nominal")
        if nominal is None:
            continue
        rows.append({
            "event_id": event["id"],
            "operation": card["process"],
            "parameter": symbol,
            "action": "skip",
            "value_nominal": nominal,
            "value_lower_dev": value.get("lower"),
            "value_upper_dev": value.get("upper"),
            "value_unit": "mm",
            "note": "upstream working card; not engineer-confirmed",
        })

    return rows


def merge_into_worksheet(worksheet_csv: Path, rows: list[dict]) -> dict:
    """Merge bridged rows into an exported worksheet, keyed by event id.

    Only the fields the engineer actually changed are written. A worksheet row
    the engineer left at `skip` stays at `skip`, and a bridged row that is only
    a system proposal never upgrades a skipped row.
    """

    if not worksheet_csv.exists():
        raise SystemExit(f"Worksheet not found: {worksheet_csv}")
    with worksheet_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        sheet = list(reader)
    index = {row.get("event_id"): row for row in sheet}

    merged = 0
    for row in rows:
        target = index.get(row["event_id"])
        if target is None:
            continue
        if row.get("action") != "value":
            continue
        if (target.get("action") or "").strip().lower() not in ("", "skip"):
            # The engineer already made a decision on this row in the worksheet.
            # Their decision wins over the bridged proposal.
            continue
        target["action"] = "value"
        for key in ("value_nominal", "value_lower_dev", "value_upper_dev", "value_unit"):
            if row.get(key) not in (None, ""):
                target[key] = row[key]
        target["note"] = row.get("note", "")
        merged += 1

    with worksheet_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(sheet)
    return {"worksheet": str(worksheet_csv.resolve()), "merged_rows": merged}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True, type=Path,
                        help="Dataset case directory holding design.json and target.json.")
    parser.add_argument("--modifier-dir", required=True, type=Path,
                        help="Upstream `temp/modifier/<task>` working card directory.")
    parser.add_argument("--task-dir", action="append", default=[], type=Path,
                        help="Upstream `output/_task_runtime/<task>` directory; repeatable.")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output CSV path for the bridged rows.")
    parser.add_argument("--merge-into", type=Path,
                        help="Existing worksheet `params.csv` to merge the engineer edits into.")
    args = parser.parse_args()

    rows = build_rows(args.case_dir, args.modifier_dir, args.task_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    columns = [c for c, _ in PARAM_SHEET_COLUMNS]
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    result = {
        "case_dir": str(args.case_dir.resolve()),
        "rows": len(rows),
        "engineer_edited": sum(1 for r in rows if r["action"] == "value"),
        "output": str(args.output.resolve()),
    }
    if args.merge_into:
        result["merge"] = merge_into_worksheet(args.merge_into, rows)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
