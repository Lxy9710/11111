"""Search the large route tables for related part families.

A drawing without an exact route row can still have a family. The route tables
contain hundreds of part numbers; several share the numeric series that the
missing drawings belong to. This script reports, for each part without an
exact route, the table rows whose part number shares its series.

The output is a candidate list for review. A related part's route is NOT a
correct route for another part; it is evidence that the family exists and a
starting point for the engineer who approves the route.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

# A series is the leading numeric block of a drawing number, e.g.
# `D209NW1U1(H).01` -> `209`, `C110K1QT2U(H).01` -> `110`.
SERIES_RE = re.compile(r"(\d{3,4})")


def series(part: str) -> str:
    match = SERIES_RE.search(part.upper())
    return match.group(1) if match else ""


def family_prefix(part: str) -> str:
    """Leading letter block, e.g. `D2` for `D209NW1U1(H).01`."""

    match = re.match(r"([A-Z]+\d)", part.upper())
    return match.group(1) if match else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", required=True, type=Path)
    parser.add_argument("--parts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    parts = json.loads(args.parts.read_text(encoding="utf-8"))
    tables = json.loads(args.tables.read_text(encoding="utf-8"))

    by_series: dict[str, list[dict]] = defaultdict(list)
    by_prefix: dict[str, list[dict]] = defaultdict(list)
    for entry in tables:
        path = Path(entry["path"])
        try:
            frame = pd.read_excel(path)
        except Exception:
            continue
        frame.columns = [str(c).strip() for c in frame.columns]
        if "部件型号（图号）" not in frame.columns:
            continue
        grouped = frame.groupby(frame["部件型号（图号）"].astype(str).str.strip())
        for part_number, group in grouped:
            number = str(part_number).strip().upper()
            if not number or number == "0":
                continue
            record = {
                "part_number": number,
                "rows": len(group),
                "table": str(path.resolve()),
                "operations": sorted({
                    str(v).strip() for v in group["工序名称"] if str(v).strip()
                }) if "工序名称" in group.columns else [],
            }
            by_series[series(number)].append(record)
            by_prefix[family_prefix(number)].append(record)

    report = {}
    for part in parts:
        key = series(part)
        prefix = family_prefix(part)
        series_matches = sorted(by_series.get(key, []), key=lambda r: -r["rows"])
        prefix_matches = sorted(by_prefix.get(prefix, []), key=lambda r: -r["rows"])
        report[part] = {
            "series": key,
            "family_prefix": prefix,
            "series_matches": series_matches,
            "family_prefix_matches": prefix_matches,
            "series_match_count": len(series_matches),
        }
        print(f"{part:30s} series={key:5s} prefix={prefix:4s} "
              f"series_matches={len(series_matches)} prefix_matches={len(prefix_matches)}")
        for record in series_matches[:5]:
            print(f"    {record['part_number']:26s} rows={record['rows']:3d} "
                  f"ops={len(record['operations'])}")

    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
