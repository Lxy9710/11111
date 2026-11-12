"""Prepare an annotation batch: export worksheets and add model suggestions.

The engineer should receive one directory per case, each holding a worksheet
that already contains the local model's suggestions. Running this script for
every case in a dataset is the mechanical part of the annotation workflow; the
judgement is the engineer's part.

Cases that already carry gold labels are skipped by default. Re-suggesting for
an already reviewed case is harmless, but the engineer would have to redo the
review, so it is opt-in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

from farmcad.data.annotation import AnnotationError  # noqa: E402
from farmcad.data.assist import annotate_assist  # noqa: E402


def safe_name(case_id: str) -> str:
    """A directory name that cannot escape the output directory.

    A case id may contain a path separator: `6006FBC1/P4YA6.66` is a real
    drawing number. Using it verbatim would silently create a nested
    directory, and the sheet would land in the wrong place.
    """

    name = case_id.replace("/", "__").replace("\\", "__")
    if name in (".", "..") or not name:
        raise ValueError(f"case id cannot be used as a directory name: {case_id!r}")
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path,
                        help="Dataset directory with manifest.json.")
    parser.add_argument("--checkpoint", required=True, type=Path,
                        help="Local checkpoint used to produce the suggestions.")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output directory; one subdirectory per case.")
    parser.add_argument("--case", action="append", default=[], type=str,
                        help="Only these case ids; repeatable. Default is all.")
    parser.add_argument("--include-reviewed", action="store_true",
                        help="Also prepare cases that already carry gold labels.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after this many cases. 0 means no limit.")
    args = parser.parse_args()

    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    wanted = set(args.case)
    results = []
    for entry in manifest["cases"]:
        case_id = entry["case_id"]
        if wanted and case_id not in wanted:
            continue
        case_dir = args.dataset / Path(entry["design"]).parent
        target = json.loads((case_dir / "target.json").read_text(encoding="utf-8"))
        reviewed = any(
            event["action"] == "PARAM"
            and any(label["quality"] == "gold" for label in event["labels"].values())
            for event in target["events"]
        )
        if reviewed and not args.include_reviewed:
            results.append({"case_id": case_id, "status": "skipped_already_reviewed"})
            continue
        try:
            result = annotate_assist(case_dir, args.checkpoint, args.output / safe_name(case_id))
        except AnnotationError as error:
            results.append({"case_id": case_id, "status": "failed", "error": str(error)})
            continue
        results.append({"case_id": case_id, "status": "prepared",
                        "directory": safe_name(case_id),
                        "rows": result["rows"],
                        "gap_rows": result["gap_rows"],
                        "high_priority_rows": result["high_priority_rows"]})
        if args.limit and len(results) >= args.limit:
            break

    summary = {
        "output": str(args.output.resolve()),
        "checkpoint": str(args.checkpoint.resolve()),
        "cases": len(results),
        "prepared": sum(1 for r in results if r["status"] == "prepared"),
        "skipped": sum(1 for r in results if r["status"].startswith("skipped")),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "total_rows": sum(r.get("rows", 0) for r in results),
        "total_gap_rows": sum(r.get("gap_rows", 0) for r in results),
        "total_high_priority": sum(r.get("high_priority_rows", 0) for r in results),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
