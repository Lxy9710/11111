"""Split the combined dataset into train / validation by drawing series.

The data contract (`chainmodel/docs/data-contract.md`) requires that every
member of a family sits in the same split. A family here is the numeric series
of a drawing number, e.g. `D209NW1U1(H).01` and `D209NW1U1(H).02` are one
series. The series is a conservative reservation rather than a certified
physical family: it can only reduce apparent generalization, never inflate it.

The split is stratified by category, so both train and validation contain cage
and ring cases. Every assignment is verified: no series and no design
fingerprint may appear in two splits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA_VERSION = "farmcad-t2-v1"


def design_fingerprint(design: dict) -> str:
    payload = {k: v for k, v in design.items() if k != "case_id"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def series_of(case_id: str) -> str:
    match = re.search(r"[A-Za-z](\d+)", case_id)
    return match.group(1) if match else ""


def category_of(case_id: str, entry: dict) -> str:
    category = entry.get("declared_category") or ""
    if category:
        return category
    upper = case_id.strip().upper()
    if upper.endswith(".56") or upper.endswith(".66"):
        return "cage"
    if upper.endswith(".01"):
        return "outer_ring"
    if upper.endswith(".02"):
        return "inner_ring"
    return "undetermined"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validation-ratio", type=float, default=0.2,
                        help="Target fraction of cases in validation.")
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()

    dataset = args.dataset.resolve()
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    registry = json.loads((dataset / "route_registry.json").read_text(encoding="utf-8"))

    # Group cases into series families.
    series_groups: dict[str, list[dict]] = defaultdict(list)
    category_by_case: dict[str, str] = {}
    for row in manifest["cases"]:
        case_id = row["case_id"]
        entry = registry.get(case_id) or registry.get(case_id.replace("/", "-")) or {}
        category = category_of(case_id, entry)
        category_by_case[case_id] = category
        series_groups[f"{category}-series-{series_of(case_id)}"].append(row)

    # Assign whole series to validation, largest-series-last so the split is
    # deterministic and the training set keeps the most data.
    ordered = sorted(series_groups, key=lambda s: (len(series_groups[s]), s))
    total = len(manifest["cases"])
    target = max(1, int(round(total * args.validation_ratio)))
    assignment: dict[str, str] = {}
    validation_series = []
    for series in reversed(ordered):
        size = len(series_groups[series])
        if sum(len(series_groups[s]) for s in validation_series) + size > target and validation_series:
            continue
        validation_series.append(series)
        if sum(len(series_groups[s]) for s in validation_series) >= target:
            break
    for series in validation_series:
        for row in series_groups[series]:
            assignment[row["case_id"]] = "validation"
    for series, rows in series_groups.items():
        for row in rows:
            assignment.setdefault(row["case_id"], "train")

    cases = []
    for row in manifest["cases"]:
        case_id = row["case_id"]
        series = f"{category_by_case[case_id]}-series-{series_of(case_id)}"
        cases.append({
            "case_id": case_id,
            "family_id": series,
            "split": assignment[case_id],
            "design": row["design"],
            "target": row["target"],
        })
    cases.sort(key=lambda c: (c["split"], c["case_id"]))

    # Verification: no series and no design fingerprint may span two splits.
    family_splits = defaultdict(set)
    fingerprints = defaultdict(set)
    for row in cases:
        family_splits[row["family_id"]].add(row["split"])
        design = json.loads((dataset / row["design"]).read_text(encoding="utf-8"))
        fingerprints[design_fingerprint(design)].add(row["split"])
    assert all(len(v) == 1 for v in family_splits.values()), "Series spans multiple splits"
    assert all(len(v) == 1 for v in fingerprints.values()), "Design fingerprint shared across splits"

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "cases": cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    splits = defaultdict(list)
    for row in cases:
        splits[row["split"]].append(row["case_id"])
    reserved = args.output / "reserved_splits"
    reserved.mkdir(parents=True, exist_ok=True)
    for split, case_ids in splits.items():
        (reserved / f"{split}.json").write_text(
            json.dumps({"split": split, "cases": case_ids}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "cases": len(cases),
        "families": len(series_groups),
        "splits": {name: len(ids) for name, ids in splits.items()},
        "categories": dict(Counter(category_by_case[c["case_id"]] for c in cases)),
        "validation_series": sorted(validation_series),
        "series_case_counts": {s: len(rows) for s, rows in sorted(series_groups.items())},
    }
    (args.output / "dataset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "series_case_counts"},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
