"""Build the route asset registry: per part number, the best available route.

A part's route can come from four independent sources. Their evidence strength
differs and is never merged silently:

1. `excel_exact`      - an exact drawing-number match in an active route table.
2. `card_exact`      - a route recovered from historical Process Hub working
                       cards, corroborated across deployments when possible.
3. `template_category`- the category default route template (system level).
4. `none`            - no route could be established.

For each part the registry records every source that was found, its SHA256,
and picks the strongest one. The selection is recorded, not hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recover_route_from_cards import (  # noqa: E402
    collapse,
    collect_task_cards,
    route_signature,
)

SOURCE_EXCEL = "excel_exact"
SOURCE_CARD = "card_exact"
SOURCE_TEMPLATE = "template_category"
SOURCE_NONE = "none"

SOURCE_RANK = {SOURCE_EXCEL: 0, SOURCE_CARD: 1, SOURCE_TEMPLATE: 2, SOURCE_NONE: 3}

CAGE_OPERATIONS = (
    "总图", "粗车第一平面及内径、倒角", "粗车第二平面及外径、倒角",
    "热处理", "调质", "粗磨外径", "切断", "均车两平面及倒角",
    "细车内径及倒角", "钻铆钉孔", "倒铆钉孔角", "插销子", "打标记",
    "钻铣孔", "终磨外径", "终车内径及倒角", "去毛刺", "拆销子",
    "光饰", "清洗", "荧光磁粉探伤", "终检", "镀银", "包装",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_part_key(part_number: str) -> str:
    """Canonical lookup key for a drawing number.

    The same part appears with `/` in the title block (`6006FBC1/P4YA6.66`) and
    with `-` in the file name (`6006FBC1-P4YA6.66`). Route tables, working cards
    and the drawing title block do not agree on which separator is used, so the
    key normalizes both away.
    """

    return part_number.strip().upper().replace("/", "-")


def excel_candidates(tables: list[dict], part_number: str) -> list[dict]:
    number = normalize_part_key(part_number)
    found = []
    for table in tables:
        count = table["part_counts"].get(number)
        if not count:
            continue
        found.append({
            "source": SOURCE_EXCEL,
            "table": table["path"],
            "deployment": table["deployment"],
            "sha256": table["sha256"],
            "rows": count,
            "active": table.get("active", False),
        })
    return found


def read_excel_rows(table_path: str, part_number: str) -> list[dict]:
    frame = pd.read_excel(table_path)
    frame.columns = [str(c).strip() for c in frame.columns]
    number = normalize_part_key(part_number)
    key = frame["部件型号（图号）"].astype(str).str.strip().str.upper().str.replace("/", "-", regex=False)
    subset = frame[key == number].copy()
    subset = subset.copy()
    subset["_order"] = range(len(subset))
    subset["_sort"] = pd.to_numeric(subset["工序号"], errors="coerce")
    subset = subset.sort_values(["_sort", "工序号", "_order"], na_position="last")

    rows = []
    for record in subset.to_dict(orient="records"):
        name = str(record.get("工序名称") or "").strip()
        if not name:
            continue
        no = str(record.get("工序号") or "").strip()
        if no.endswith(".0"):
            no = no[:-2]
        region = str(record.get("部位名称") or "").strip()
        rows.append({
            "process_no": no,
            "process_type": str(record.get("工序类型") or "").strip(),
            "process_name_only": name,
            "process_name": f"{part_number}_{no} {name}",
            "display_name": f"{part_number}_{no} {name}",
            "part_region": region or "保持架",
            "route_origin": "excel_exact_lookup",
            "source_status": "exact_route_from_active_table",
        })
    return rows


def card_candidates(card_roots: list[Path], part_number: str) -> tuple[list[dict], list[dict]]:
    """Return (best candidate rows, all deployment summaries)."""

    candidates = _scan_card_root(card_roots).get(normalize_part_key(part_number), {})
    if not candidates:
        return [], []

    def rank(item):
        key, operations = item
        patched = sum(1 for op in operations if op["mogong_patch"])
        return (-len(operations), -patched, key)

    chosen_key, chosen = sorted(candidates.items(), key=rank)[0]
    signature = route_signature(chosen)
    summaries = []
    for key, operations in sorted(candidates.items()):
        other = route_signature(operations)
        summaries.append({
            "deployment": key,
            "operations": len(operations),
            "patched_operations": sum(1 for op in operations if op["mogong_patch"]),
            "identical_to_chosen": other == signature,
        })
    for row in chosen:
        row["route_origin"] = "working_card_recovery"
        row["source_status"] = "recovered_from_working_card"
    return chosen, summaries


_CARD_SCAN_CACHE: dict[tuple, dict] = {}


def _scan_card_root(card_roots: list[Path]) -> dict[str, dict[str, list[dict]]]:
    """Scan every card once and group by part number.

    Reading the cards is the expensive part of route recovery, so the scan is
    done a single time and reused for every part number.
    """

    key = tuple(str(p) for p in card_roots)
    if key in _CARD_SCAN_CACHE:
        return _CARD_SCAN_CACHE[key]
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for root in card_roots:
        if not root.is_dir():
            continue
        for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            cards = collect_task_cards(task_dir)
            if not cards:
                continue
            by_part: dict[str, list[dict]] = defaultdict(list)
            for card in cards:
                by_part[normalize_part_key(card["part_number"])].append(card)
            for part_number, subset in by_part.items():
                operations = collapse(subset, part_number)
                if operations:
                    grouped[part_number][f"{root.name}/{task_dir.name}"] = operations
    _CARD_SCAN_CACHE[key] = grouped
    return grouped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", required=True, type=Path,
                        help="JSON file: list of part numbers.")
    parser.add_argument("--tables", required=True, type=Path,
                        help="JSON file: list of route table paths.")
    parser.add_argument("--card-root", action="append", default=[], type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--categories", type=Path,
                        help="JSON file: map of part number -> category "
                             "(cage / outer_ring / inner_ring / rolling_element).")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    parts = json.loads(args.parts.read_text(encoding="utf-8"))
    table_paths = json.loads(args.tables.read_text(encoding="utf-8"))

    tables = []
    for entry in table_paths:
        path = Path(entry["path"])
        frame = pd.read_excel(path)
        frame.columns = [str(c).strip() for c in frame.columns]
        counts = defaultdict(int)
        for value in frame["部件型号（图号）"]:
            number = normalize_part_key(str(value))
            if number and number != "0":
                counts[number] += 1
        tables.append({
            "path": str(path.resolve()),
            "deployment": entry.get("deployment", path.parent.parent.name),
            "sha256": digest(path),
            "part_counts": dict(counts),
            "active": bool(entry.get("active", False)),
        })

    templates = json.loads(args.template.read_text(encoding="utf-8")) if args.template else None
    declared_categories = (
        json.loads(args.categories.read_text(encoding="utf-8")) if args.categories else {}
    )

    registry = {}
    for part in parts:
        entry: dict = {"part_number": part, "sources": {}}
        excel_found = excel_candidates(tables, part)
        if excel_found:
            # Prefer the active table, then the table with the most rows for
            # this part: a larger row count means a more detailed route.
            best = sorted(excel_found, key=lambda t: (not t["active"], -t["rows"], t["table"]))[0]
            entry["sources"][SOURCE_EXCEL] = {
                "candidates": excel_found,
                "selected": best,
                "rows": read_excel_rows(best["table"], part),
            }

        card_rows, deployments = card_candidates(args.card_root, part)
        if card_rows:
            entry["sources"][SOURCE_CARD] = {
                "deployments": deployments,
                "corroborated": any(d["identical_to_chosen"] for d in deployments[1:]),
                "rows": card_rows,
            }

        # Category inference for the template fallback. The operation vocabulary
        # is disjoint between cage and ring routes, so it identifies the
        # category without needing the drawing to be parsed first.
        observed_ops = {
            row["process_name_only"]
            for source in entry["sources"].values()
            for row in source.get("rows", [])
        }
        category = None
        if observed_ops:
            if observed_ops & set(CAGE_OPERATIONS):
                category = "cage"
            elif observed_ops:
                category = "outer_ring"
        # A declared category (from the drawing title block) is authoritative.
        # The vocabulary heuristic only fires when nothing was declared.
        declared = declared_categories.get(part) or declared_categories.get(normalize_part_key(part))
        if declared:
            category = declared
        entry["inferred_category"] = category
        entry["declared_category"] = declared

        if templates is not None and category:
            profile = templates["category_profiles"].get(category)
            if profile and profile.get("fixed_route"):
                entry["sources"][SOURCE_TEMPLATE] = {
                    "category": category,
                    "rows": [
                        {
                            "process_no": no,
                            "process_type": typ,
                            "process_name_only": name,
                            "process_name": f"{part}_{no} {name}",
                            "display_name": f"{part}_{no} {name}",
                            "part_region": profile.get("label", ""),
                            "route_origin": "fixed_category_route",
                            "source_status": "system_derived_route_template",
                        }
                        for no, typ, name in profile["fixed_route"]
                    ],
                }

        chosen_source = sorted(
            entry["sources"],
            key=lambda name: SOURCE_RANK.get(name, 9),
        )[0] if entry["sources"] else SOURCE_NONE
        entry["chosen_source"] = chosen_source
        entry["route_rows"] = entry["sources"].get(chosen_source, {}).get("rows", [])
        entry["operation_count"] = len(entry["route_rows"])
        entry["human_verified"] = False
        entry["quality_status"] = (
            "exact_route_from_active_table" if chosen_source == SOURCE_EXCEL else
            "recovered_from_working_card" if chosen_source == SOURCE_CARD else
            "system_derived_route_template" if chosen_source == SOURCE_TEMPLATE else
            "no_route_available"
        )
        registry[part] = entry

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = defaultdict(int)
    for entry in registry.values():
        counts[entry["chosen_source"]] += 1
    print(json.dumps({"parts": len(registry), "by_source": dict(counts)}, ensure_ascii=False))
    for part, entry in sorted(registry.items()):
        print(f'{part:28s} {entry["chosen_source"]:18s} ops={entry["operation_count"]:3d} '
              f'({entry["quality_status"]})')


if __name__ == "__main__":
    main()
