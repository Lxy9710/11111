"""Classify drawings in the test-data tree and locate their routes.

Classification follows the suffix rule used by the team:
- filename ends with `.56` or `.66`  -> cage (保持架)
- filename ends with `.01`            -> outer ring (套圈外圈)
- filename ends with `.02`            -> inner ring (套圈内圈)
- otherwise                          -> undetermined, reported separately

The script then searches every route table found across the historical
deployments for an exact drawing-number match, and reports which tables
contain the part, with row counts and SHA256 of the table.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

DEPLOYMENT_ROOTS = [
    Path(r"E:\AAhz\NOVbcj\smartCAD\CAD_Process_Hub"),
    Path(r"E:\AAhz\20260917\smartCAD\CAD_Process_Hub"),
    Path(r"E:\AAhz\20260918\smartCAD\CAD_Process_Hub"),
    Path(r"E:\AAhz\20260909\smartCAD\CAD_Process_Hub"),
    Path(r"E:\AAhz\Amix\mix717\CAD_Process_Hublh"),
    Path(r"E:\AAhz\smartCAD715\smartCADwxf"),
    Path(r"E:\AAhz\LXY\smartCAD\CAD_Process_Hub"),
]

TEST_DATA_ROOT = Path(r"E:\AAhz\测试数据")

DRAWING_NUMBER_RE = re.compile(r"[A-Za-z0-9()\-.]+")

CATEGORY_CAGE = "cage"
CATEGORY_OUTER = "outer_ring"
CATEGORY_INNER = "inner_ring"
CATEGORY_UNKNOWN = "undetermined"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(filename_stem: str) -> tuple[str, str]:
    """Return (category, reason)."""

    upper = filename_stem.strip().upper()
    if upper.endswith(".56") or upper.endswith(".66"):
        return CATEGORY_CAGE, "suffix_56_or_66_is_cage"
    if upper.endswith(".01"):
        return CATEGORY_OUTER, "suffix_01_is_outer_ring"
    if upper.endswith(".02"):
        return CATEGORY_INNER, "suffix_02_is_inner_ring"
    return CATEGORY_UNKNOWN, "suffix_not_in_known_rule"


def collect_drawings() -> list[dict]:
    rows = []
    for path in sorted(TEST_DATA_ROOT.rglob("*.dwg")):
        relative = path.relative_to(TEST_DATA_ROOT)
        group = relative.parts[0] if len(relative.parts) > 1 else "root"
        category, reason = classify(path.stem)
        rows.append({
            "group": group,
            "filename": path.name,
            "stem": path.stem,
            "path": str(path.resolve()),
            "sha256": digest(path),
            "category": category,
            "category_reason": reason,
            "json_exists": path.with_suffix(".json").exists(),
        })
    return rows


def load_tables() -> list[dict]:
    tables = []
    seen = set()
    for root in DEPLOYMENT_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("工序选择*.xlsx")):
            resolved = path.resolve()
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            try:
                frame = pd.read_excel(resolved)
            except Exception:
                continue
            columns = [str(c).strip() for c in frame.columns]
            if "部件型号（图号）" not in columns:
                continue
            frame.columns = columns
            counts = Counter(str(v).strip().upper() for v in frame["部件型号（图号）"])
            counts.pop("", None)
            counts.pop("0", None)
            tables.append({
                "path": key,
                "deployment": root.name,
                "relative": str(resolved.relative_to(root)),
                "sha256": digest(resolved),
                "rows": len(frame),
                "part_counts": {k: int(v) for k, v in counts.items()},
            })
    return tables


def main() -> None:
    drawings = collect_drawings()
    tables = load_tables()

    for row in drawings:
        number = row["stem"].strip().upper()
        matches = []
        for table in tables:
            count = table["part_counts"].get(number)
            if count:
                matches.append({
                    "table": table["relative"],
                    "deployment": table["deployment"],
                    "rows": count,
                    "sha256": table["sha256"],
                })
        row["route_matches"] = matches
        row["route_found"] = bool(matches)

    summary = {
        "drawings": len(drawings),
        "by_category": dict(Counter(r["category"] for r in drawings)),
        "by_group": dict(Counter(r["group"] for r in drawings)),
        "with_route": sum(r["route_found"] for r in drawings),
        "without_route": sum(not r["route_found"] for r in drawings),
        "route_found_by_category": dict(Counter(
            r["category"] for r in drawings if r["route_found"]
        )),
        "tables": [
            dict({k: v for k, v in t.items() if k != "part_counts"}, parts=len(t["part_counts"]))
            for t in tables
        ],
    }

    output = Path(sys.argv[1])
    output.write_text(
        json.dumps({"summary": summary, "drawings": drawings}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("\n--- drawings")
    for row in drawings:
        mark = "OK " if row["route_found"] else "-- "
        sources = ",".join(sorted({m["table"] for m in row["route_matches"]}))
        print(f'{mark}{row["category"]:12s} {row["group"]:8s} {row["stem"]:28s} {sources}')


if __name__ == "__main__":
    main()
