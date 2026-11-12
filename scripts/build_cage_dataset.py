"""Build the whole cage dataset batch from the test-data drawings.

Runs `build_cage_case` for every cage drawing, then assembles a training
manifest plus a review table. Failures are recorded per case instead of
aborting the batch, so a single unreadable drawing cannot hide the rest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "farmcad-t2-v1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--route", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--builder", type=Path,
                        default=Path(__file__).resolve().parent / "build_cage_case.py")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    cases_dir = args.output / "cases"
    cases_dir.mkdir(exist_ok=True)

    rows = []
    failures = []
    for index, json_path in enumerate(sorted(args.source_root.rglob("*.json"))):
        upper = json_path.stem.strip().upper()
        if not (upper.endswith(".56") or upper.endswith(".66")):
            continue
        case_dir = cases_dir / f"case-{index:02d}"
        command = [
            sys.executable, str(args.builder),
            "--project-root", str(args.project_root.resolve()),
            "--json", str(json_path.resolve()),
            "--route", str(args.route.resolve()),
            "--output", str(case_dir),
        ]
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            failures.append({
                "source_json": str(json_path),
                "error": (result.stderr or "")[-800:],
            })
            print(f"FAIL {json_path.stem}")
            continue
        try:
            summary = json.loads(result.stdout.strip().splitlines()[-1])
        except Exception:
            summary = {}
        case_id = summary.get("case_id", json_path.stem)
        provenance_path = case_dir / "provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        rows.append({
            "case_id": case_id,
            "case_dir": f"cases/case-{index:02d}",
            "source_json": str(json_path.resolve()),
            "source_json_sha256": digest(json_path),
            "route_source": provenance["route_source"],
            "route_quality_status": provenance["route_quality_status"],
            "entities": summary.get("entities", 0),
            "features": summary.get("features", 0),
            "requirements": summary.get("requirements", 0),
            "operations": summary.get("operations", 0),
            "operations_with_parameters": summary.get("operations_with_parameters", 0),
            "events": summary.get("events", 0),
            "design_sha256": digest(case_dir / "design.json"),
            "target_sha256": digest(case_dir / "target.json"),
        })
        print(f'OK   {case_id:26s} ops={summary.get("operations", 0):3d} '
              f'with_params={summary.get("operations_with_parameters", 0):3d} '
              f'events={summary.get("events", 0):4d}')

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "cases": [
            {
                "case_id": row["case_id"],
                "family_id": row["case_id"],
                "split": "train",
                "design": f'{row["case_dir"]}/design.json',
                "target": f'{row["case_dir"]}/target.json',
            }
            for row in rows
        ],
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    columns = list(rows[0]) if rows else []
    review_dir = args.output / "review"
    review_dir.mkdir(exist_ok=True)
    with (review_dir / "cases.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "cases": len(rows),
        "failures": len(failures),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "cage_system_weak_labels_requires_engineer_review",
        "total_entities": sum(r["entities"] for r in rows),
        "total_operations": sum(r["operations"] for r in rows),
        "total_operations_with_parameters": sum(r["operations_with_parameters"] for r in rows),
        "total_events": sum(r["events"] for r in rows),
        "route_sources": {},
        "failures_detail": failures,
    }
    for row in rows:
        key = row["route_source"]
        summary["route_sources"][key] = summary["route_sources"].get(key, 0) + 1
    (args.output / "batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "failures_detail"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
