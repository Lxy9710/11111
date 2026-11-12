"""Scan historical modifier working-card directories for recoverable routes.

Reads only the small header fields of each card JSON (filename-derived part number,
process number, process name, logical order) so the scan stays cheap on the
multi-hundred-megabyte card payloads. Output is a registry of candidate route
sources keyed by part number.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOTS = [
    Path(r"E:\AAhz\20260908\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\20260909\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\20260917\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\20260918\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\Amix\mix717\CAD_Process_Hublh\temp\modifier"),
    Path(r"E:\AAhz\Amix\mix717\CAD_Process_Hubwxf\temp\modifier"),
    Path(r"E:\AAhz\LXY\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\smartCAD-LIHAO-HAZHOU\smartCAD-LIHAO-HAZHOU\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\smartCAD-wxf\smartCAD-wxf\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\NOVbcj\smartCAD\CAD_Process_Hub\temp\modifier"),
]

# A card file name looks like <part>_<process_no>_<process name>[_第N页].json
NAME_RE = re.compile(r"^(?P<part>.+?)_(?P<no>\d+)_(?P<name>.+)$")


def header(path: Path) -> dict:
    """Read only the top-level scalar metadata of a card without parsing entities."""

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "part_number": str(data.get("part_number") or ""),
        "process_no": str(data.get("process_no") or ""),
        "process_name": str(data.get("process_name") or ""),
        "process_type": str(data.get("process_type") or ""),
        "logical_order": str(data.get("logical_process_order") or ""),
        "page_index": str(data.get("process_page_index") or ""),
        "page_count": str(data.get("process_page_count") or ""),
        "has_mogong_patch": bool(data.get("mogong_patch")),
        "total_card_pages": str(data.get("total_card_pages") or ""),
    }


def main() -> None:
    registry = defaultdict(lambda: defaultdict(list))
    for root in ROOTS:
        if not root.is_dir():
            continue
        for task_dir in sorted(root.iterdir()):
            if not task_dir.is_dir():
                continue
            for card in sorted(task_dir.glob("*.json")):
                if card.name.endswith(".extents.json"):
                    continue
                meta = header(card)
                match = NAME_RE.match(card.stem)
                part = meta.get("part_number") or (match.group("part") if match else "")
                if not part:
                    continue
                registry[part][str(root)].append({
                    "task": task_dir.name,
                    "file": card.name,
                    "no": meta.get("process_no") or (match.group("no") if match else ""),
                    "name": meta.get("process_name") or (match.group("name") if match else ""),
                    "type": meta.get("process_type"),
                    "order": meta.get("logical_order"),
                    "pages": meta.get("page_count"),
                    "patch": meta.get("has_mogong_patch"),
                })

    out = {}
    for part, per_root in sorted(registry.items()):
        out[part] = {
            "roots": {
                str(root): {
                    "cards": len(cards),
                    "distinct_ops": len({c["name"] for c in cards}),
                    "with_patch": len([c for c in cards if c["patch"]]),
                    "tasks": sorted({c["task"] for c in cards}),
                    "cards_detail": cards,
                }
                for root, cards in per_root.items()
            }
        }
    Path(sys.argv[1]).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for part, entry in out.items():
        print(f"### {part}")
        for root, info in entry["roots"].items():
            print(f"  {root}  cards={info['cards']} ops={info['distinct_ops']} patch={info['with_patch']} tasks={info['tasks']}")


if __name__ == "__main__":
    main()
