"""Assemble the combined ring + cage dataset into the research data tree.

Merges the existing outer-ring pilot case with the newly built cage cases into
one dataset directory with the standard layout:

    <output>/
        manifest.json          training manifest (train / validation / test)
        cases/<case>/design.json, target.json, provenance.json
        design_pool/           inputs without a route-backed target
        reserved_splits/       split reservation tables
        review/                review tables for the engineer
        dataset_summary.json

Splits are assigned by family. A family never appears in two splits, so the
same physical part cannot leak across train and validation.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cage-batch", required=True, type=Path)
    parser.add_argument("--ring-case", type=Path,
                        help="Existing ring case directory (design.json + target.json).")
    parser.add_argument("--ring-case-id", default="")
    parser.add_argument("--validation-cases", type=Path, action="append", default=[],
                        help="Case directories to place in the validation split.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite dataset: {args.output}")
    cases_dir = args.output / "cases"
    cases_dir.mkdir(parents=True)

    rows = []
    manifest_cases = []

    def adopt(name: str, case_id: str, family: str, split: str, case_dir: Path,
              source: dict) -> None:
        target_dir = cases_dir / name
        target_dir.mkdir()
        for filename in ("design.json", "target.json"):
            (target_dir / filename).write_text(
                (case_dir / filename).read_text(encoding="utf-8"), encoding="utf-8")
        provenance = {
            "case_id": case_id,
            "family_id": family,
            "split": split,
            "source": source,
            "design_sha256": digest(target_dir / "design.json"),
            "target_sha256": digest(target_dir / "target.json"),
            "human_verified": False,
        }
        if (case_dir / "provenance.json").exists():
            provenance["upstream"] = json.loads(
                (case_dir / "provenance.json").read_text(encoding="utf-8"))
        (target_dir / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append({
            "case_id": case_id, "family_id": family, "split": split,
            "case_dir": f"cases/{name}",
            "route_source": source.get("route_source", ""),
            "design_sha256": provenance["design_sha256"],
            "target_sha256": provenance["target_sha256"],
        })
        manifest_cases.append({
            "case_id": case_id, "family_id": family, "split": split,
            "design": f"cases/{name}/design.json",
            "target": f"cases/{name}/target.json",
        })

    if args.ring_case and args.ring_case.exists():
        adopt(
            "ring-" + (args.ring_case_id or args.ring_case.name),
            args.ring_case_id or args.ring_case.name,
            args.ring_case_id or args.ring_case.name,
            "train",
            args.ring_case,
            {"route_source": "excel_exact", "origin": "outer_ring_pilot"},
        )

    cage_summary = json.loads((args.cage_batch / "batch_summary.json").read_text(encoding="utf-8"))
    cage_rows = list(csv.DictReader(
        (args.cage_batch / "review" / "cases.csv").open(encoding="utf-8-sig")))

    validation_ids = set()
    for index, case_dir in enumerate(args.validation_cases):
        validation_ids.add(Path(case_dir).name)

    for index, cage_row in enumerate(cage_rows):
        source_dir = args.cage_batch / cage_row["case_dir"]
        split = "validation" if Path(cage_row["case_dir"]).name in validation_ids else "train"
        adopt(
            f"cage-{index:02d}",
            cage_row["case_id"],
            cage_row["case_id"],
            split,
            source_dir,
            {
                "route_source": cage_row["route_source"],
                "route_quality_status": cage_row["route_quality_status"],
                "origin": "cage_test_data_drawing",
                "source_json": cage_row["source_json"],
                "source_json_sha256": cage_row["source_json_sha256"],
            },
        )

    manifest = {"schema_version": SCHEMA_VERSION, "cases": manifest_cases}
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    review_dir = args.output / "review"
    review_dir.mkdir()
    with (review_dir / "cases.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    splits = defaultdict(list)
    for row in rows:
        splits[row["split"]].append(row["case_id"])
    reserved = args.output / "reserved_splits"
    reserved.mkdir()
    for split, case_ids in splits.items():
        (reserved / f"{split}.json").write_text(
            json.dumps({"split": split, "cases": case_ids}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(rows),
        "splits": {name: len(ids) for name, ids in splits.items()},
        "route_sources": dict(Counter(row["route_source"] for row in rows)),
        "human_verified_cases": 0,
        "scope": (
            "cage system weak labels from the upstream platform plus the outer-ring "
            "pilot. All supervision is system level; no engineer review yet."
        ),
        "cage_batch": {k: v for k, v in cage_summary.items() if k != "failures_detail"},
    }
    (args.output / "dataset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
