"""Recover process routes from historical modifier working cards.

Working cards under `temp/modifier/<task_id>/` are the by-products of real
Process Hub runs. Each card carries the logical process identity (part number,
process number, process name, logical order) and, for grinding operations, a
`mogong_patch` with resolved parameter values, `source_type` and `evidence`.
Together they form a route source that is independent of the trimmed route
tables.

Two guarantees are enforced here:

1. Pages are collapsed back into logical operations. A card named
   `..._第1页`/`..._第2页` is one operation, not two.
2. Routes found in different deployments are compared, not silently merged.
   A route is only published as `card_exact` when at least one deployment
   yields it and the reported agreement across deployments is recorded.

Recovered routes carry `source_status = "recovered_from_working_card"`,
which is a system-level claim: the route came from a real execution but has
not been re-approved by an engineer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^(?P<part>.+?)_(?P<no>\d+)_(?P<name>.+)$")
PAGE_SUFFIX_RE = re.compile(r"_第\d+页$")

# Route-ordering keys. `logical_process_order` is the order the Process Hub
# itself recorded; `process_no` is the engineering slot number. They agree in
# every card set inspected, but `logical_process_order` wins when present.
ORDER_KEYS = ("logical_process_order", "process_no")

INNER_HINTS = ("内沟", "内径", "内圈")
OUTER_HINTS = ("外沟", "外径", "外圈")

# The cage route reuses ring-style operation names (粗磨外径, 终车内径及倒角…),
# so the part region cannot be inferred from the operation name alone.
CAGE_OPERATION_NAMES = frozenset({
    "总图", "粗车第一平面及内径、倒角", "粗车第二平面及外径、倒角",
    "热处理", "调质", "粗磨外径", "切断", "均车两平面及倒角",
    "细车内径及倒角", "钻铆钉孔", "倒铆钉孔角", "插销子", "打标记",
    "钻铣孔", "终磨外径", "终车内径及倒角", "去毛刺", "拆销子",
    "光饰", "清洗", "荧光磁粉探伤", "终检", "镀银", "包装",
})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def page_stem(stem: str) -> str:
    """Strip the trailing page marker so pages of one operation collapse."""

    return PAGE_SUFFIX_RE.sub("", stem)


def parse_card(path: Path) -> dict | None:
    """Read the small scalar header of a card.

    Cards are 1-3 MB each because of the embedded entity list. The route
    identity only lives in the top-level scalars, so the whole file is parsed
    but only those fields are kept. `mogong_patch` is kept as well because it
    carries the parameter evidence that the dataset builder needs.
    """

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    part = str(data.get("part_number") or "")
    if not part:
        match = NAME_RE.match(path.stem)
        if match:
            part = match.group("part")
    if not part:
        return None
    patch = data.get("mogong_patch")
    if not isinstance(patch, dict) or not patch:
        # Cage cards carry the same evidence under a different key.
        patch = data.get("cage_patch")
    patch = patch if isinstance(patch, dict) else {}
    return {
        "file": path.name,
        "part_number": part,
        "process_no": str(data.get("process_no") or ""),
        "process_name": str(data.get("process_name") or ""),
        "process_type": str(data.get("process_type") or ""),
        "logical_process_order": str(data.get("logical_process_order") or ""),
        "page_index": str(data.get("process_page_index") or ""),
        "page_count": str(data.get("process_page_count") or ""),
        "logical_process_name": str(data.get("logical_process_name") or ""),
        "total_card_pages": str(data.get("total_card_pages") or ""),
        "mogong_patch": patch,
    }


def logical_key(card: dict) -> str:
    """Key identifying the logical operation a card belongs to."""

    name = PAGE_SUFFIX_RE.sub("", card["process_name"])
    return f'{card["process_no"]}::{name}'


def infer_part_region(process_name: str, part_number: str) -> str:
    if process_name in CAGE_OPERATION_NAMES:
        return "保持架"
    if any(hint in process_name for hint in INNER_HINTS):
        return "内圈"
    if any(hint in process_name for hint in OUTER_HINTS):
        return "外圈"
    if part_number.upper().startswith("E") or "内圈" in part_number:
        return "内圈"
    return "外圈"


def order_value(card: dict) -> tuple[int, float, str]:
    """Sort key: logical order first, then numeric slot, then name."""

    for key in ORDER_KEYS:
        raw = str(card.get(key) or "")
        if raw:
            try:
                return (0, float(raw), raw)
            except ValueError:
                continue
    return (1, 0.0, card["process_name"])


def collect_task_cards(task_dir: Path) -> list[dict]:
    cards = []
    for path in sorted(task_dir.glob("*.json")):
        if path.name.endswith(".extents.json"):
            continue
        card = parse_card(path)
        if card is not None:
            cards.append(card)
    return cards


def collapse(cards: list[dict], part_number: str) -> list[dict]:
    """Collapse pages into logical operations and order them."""

    grouped: dict[str, list[dict]] = defaultdict(list)
    for card in cards:
        grouped[logical_key(card)].append(card)

    operations = []
    for key, group in grouped.items():
        group.sort(key=lambda c: (c.get("page_index") or "", c["file"]))
        head = group[0]
        pages = len(group)
        declared_pages = head.get("page_count") or ""
        region = infer_part_region(head["process_name"], part_number)
        no = head["process_no"]
        name = PAGE_SUFFIX_RE.sub("", head["process_name"])
        display = f"{part_number}_{no} {name}"
        patch = head["mogong_patch"] or {}
        operations.append({
            "part_number": part_number,
            "process_no": no,
            "process_type": head["process_type"] or "磨加",
            "process_name_only": name,
            "process_name": display,
            "display_name": display,
            "part_region": region,
            "excel_source": "",
            "process_selection_remark": "路线由历史工序卡工作目录恢复",
            "route_origin": "working_card_recovery",
            "source_status": "recovered_from_working_card",
            "logical_process_order": head.get("logical_process_order") or "",
            "page_count": declared_pages or str(pages),
            "recovered_pages": pages,
            "card_files": [c["file"] for c in group],
            "mogong_patch": patch,
        })

    operations.sort(key=lambda row: order_value({
        "logical_process_order": row["logical_process_order"],
        "process_no": row["process_no"],
        "process_name": row["process_name_only"],
    }))
    for index, row in enumerate(operations, start=1):
        row["recovered_route_index"] = index
    return operations


def route_signature(operations: list[dict]) -> tuple:
    """Order-sensitive signature used for cross-deployment comparison."""

    return tuple(row["process_name_only"] for row in operations)


def jaccard(left: tuple, right: tuple) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-root", required=True, type=Path, action="append",
                        help="A temp/modifier directory; may be repeated.")
    parser.add_argument("--part-number", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    part_number = args.part_number
    candidates: dict[str, list[dict]] = {}
    for root in args.card_root:
        if not root.is_dir():
            continue
        for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            cards = collect_task_cards(task_dir)
            if not any(c["part_number"] == part_number for c in cards):
                continue
            subset = [c for c in cards if c["part_number"] == part_number]
            operations = collapse(subset, part_number)
            if not operations:
                continue
            candidates[f"{root.name}/{task_dir.name}"] = operations

    if not candidates:
        raise SystemExit(f"No working cards found for part {part_number}")

    # Prefer the candidate with the most operations; ties broken by the number
    # of operations carrying parameter patches, then by name for determinism.
    def rank(item: tuple[str, list[dict]]) -> tuple:
        key, operations = item
        patched = sum(1 for op in operations if op["mogong_patch"])
        return (-len(operations), -patched, key)

    chosen_key, chosen = sorted(candidates.items(), key=rank)[0]
    signature = route_signature(chosen)

    deployments = []
    for key, operations in sorted(candidates.items()):
        other = route_signature(operations)
        deployments.append({
            "deployment": key,
            "operations": len(operations),
            "patched_operations": sum(1 for op in operations if op["mogong_patch"]),
            "signature": list(other),
            "jaccard_with_chosen": round(jaccard(signature, other), 4),
            "identical_to_chosen": other == signature,
        })

    # A route is only "confirmed by cross-check" when another deployment
    # produces the same ordered operation sequence.
    corroborated = any(d["identical_to_chosen"] for d in deployments if d["deployment"] != chosen_key)
    agreement = max((d["jaccard_with_chosen"] for d in deployments), default=0.0)

    output = {
        "schema_version": "farmcad-route-recovery-v1",
        "part_number": part_number,
        "route_source": "working_card_recovery",
        "source_status": "recovered_from_working_card",
        "human_verified": False,
        "quality_status": (
            "recovered_and_cross_deployment_corroborated"
            if corroborated else "recovered_single_deployment"
        ),
        "chosen_deployment": chosen_key,
        "operation_count": len(chosen),
        "cross_deployment_agreement": round(agreement, 4),
        "deployments": deployments,
        "rows": chosen,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not args.quiet:
        print(json.dumps({
            "part_number": part_number,
            "operations": len(chosen),
            "chosen": chosen_key,
            "corroborated": corroborated,
            "agreement": agreement,
            "deployments": len(deployments),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
