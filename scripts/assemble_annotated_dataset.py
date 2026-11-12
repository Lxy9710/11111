"""Assemble a trainable dataset from base cases plus engineer-annotated cases.

The base dataset carries system-level supervision only. Once an engineer has
annotated a case, the annotated version replaces the base version of that case
and the gold fields enter training.

The split assignment is by family and is stated explicitly. A family never
appears in two splits, so the same physical part cannot leak from training into
validation or test. The script refuses to write a dataset whose splits are not
family-isolated, and it refuses to write a validation split that is empty,
because both mistakes make an evaluation number meaningless rather than wrong.

Every output records the review status of each case, so the training report can
state how much of the supervision was human confirmed and how much was not.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "farmcad-t2-v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count_gold(target: dict) -> tuple[int, int]:
    """(gold parameter events, total parameter events) for one target."""

    total = 0
    gold = 0
    for event in target["events"]:
        if event["action"] != "PARAM":
            continue
        total += 1
        if event["labels"]["value"]["quality"] == "gold":
            gold += 1
    return gold, total


def load_split_map(path: Path | None) -> dict[str, str]:
    """`family_id -> split` from a two-column CSV."""

    if path is None:
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            family = (row.get("family_id") or "").strip()
            split = (row.get("split") or "").strip()
            if not family or not split:
                continue
            if split not in ("train", "validation", "test"):
                raise ValueError(f"Unknown split {split!r} for family {family!r}")
            mapping[family] = split
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dataset", required=True, type=Path,
                        help="Existing dataset directory with manifest.json and cases/.")
    parser.add_argument("--annotated-case", action="append", default=[], type=Path,
                        help="Annotated case directory (design.json, target.json, "
                             "review_log.json); repeatable.")
    parser.add_argument("--splits", type=Path,
                        help="CSV with family_id,split columns. Overrides the base splits.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite dataset: {args.output}")
    manifest = json.loads((args.base_dataset / "manifest.json").read_text(encoding="utf-8"))
    split_map = load_split_map(args.splits)

    annotated = {}
    for case_dir in args.annotated_case:
        log_path = case_dir / "review_log.json"
        if not log_path.exists():
            raise ValueError(f"Annotated case lacks review_log.json: {case_dir}")
        log = json.loads(log_path.read_text(encoding="utf-8"))
        annotated[log["case_id"]] = (case_dir, log)

    cases_dir = args.output / "cases"
    cases_dir.mkdir(parents=True)

    rows = []
    manifest_cases = []
    family_splits: dict[str, set[str]] = defaultdict(set)

    for index, entry in enumerate(manifest["cases"]):
        case_id = entry["case_id"]
        family = entry["family_id"]
        split = split_map.get(family, entry["split"])
        family_splits[family].add(split)

        source_dir = args.base_dataset / Path(entry["design"]).parent
        if case_id in annotated:
            source_dir, log = annotated[case_id]
            review = {
                "status": "engineer_reviewed",
                "reviewer": log.get("reviewer", ""),
                "reviewed_at": log.get("reviewed_at", ""),
                "promoted_fields": log.get("promoted_count", 0),
            }
        else:
            review = {"status": "system_only", "reviewer": "", "reviewed_at": "",
                      "promoted_fields": 0}

        name = Path(entry["design"]).parent.name
        target_dir = cases_dir / name
        target_dir.mkdir()
        for filename in ("design.json", "target.json"):
            (target_dir / filename).write_text(
                (source_dir / filename).read_text(encoding="utf-8"), encoding="utf-8")
        target = json.loads((target_dir / "target.json").read_text(encoding="utf-8"))
        gold, total = count_gold(target)
        provenance = {
            "case_id": case_id,
            "family_id": family,
            "split": split,
            "design_sha256": digest(target_dir / "design.json"),
            "target_sha256": digest(target_dir / "target.json"),
            "review": review,
            "gold_parameter_events": gold,
            "parameter_events": total,
            "human_verified": gold > 0,
        }
        (target_dir / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append({
            "case_id": case_id, "family_id": family, "split": split,
            "case_dir": f"cases/{name}",
            "review_status": review["status"],
            "reviewer": review["reviewer"],
            "gold_parameter_events": gold,
            "parameter_events": total,
            "design_sha256": provenance["design_sha256"],
            "target_sha256": provenance["target_sha256"],
        })
        manifest_cases.append({
            "case_id": case_id, "family_id": family, "split": split,
            "design": f"cases/{name}/design.json",
            "target": f"cases/{name}/target.json",
        })

    leaked = {f: s for f, s in family_splits.items() if len(s) > 1}
    if leaked:
        raise ValueError(f"A family appears in more than one split: {leaked}")
    splits = {row["split"] for row in rows}
    if "validation" not in splits:
        raise ValueError(
            "The dataset has no validation split. A validation family is required "
            "before an evaluation number means anything.")

    (args.output / "manifest.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "cases": manifest_cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    review_dir = args.output / "review"
    review_dir.mkdir()
    with (review_dir / "cases.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    split_counts = Counter(row["split"] for row in rows)
    gold_total = sum(row["gold_parameter_events"] for row in rows)
    param_total = sum(row["parameter_events"] for row in rows)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(rows),
        "splits": dict(split_counts),
        "review_status": dict(Counter(row["review_status"] for row in rows)),
        "gold_parameter_events": gold_total,
        "parameter_events": param_total,
        "gold_fraction": (gold_total / param_total) if param_total else 0.0,
        "engineer_reviewed_cases": sum(
            1 for row in rows if row["review_status"] == "engineer_reviewed"),
        "scope": (
            "Base supervision is system level. Gold fields come only from the "
            "engineer-annotated cases listed in review/cases.csv."
        ),
    }
    (args.output / "dataset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
