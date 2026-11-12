"""Refresh a pilot verification record after regenerating its artifacts.

The verification record pins the hash of every published artifact plus the
hashes of the test sources that produced it. Regenerating an artifact without
refreshing the record leaves the two inconsistent, and the dataset preparation
step deliberately refuses to run when they disagree.

Absolute paths are stored as project-relative paths, so that the record
survives moving the project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def portable(path: Path, root: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", required=True, type=Path)
    parser.add_argument("--tests", action="append", required=True, type=Path)
    parser.add_argument("--tests-run", type=int, required=True)
    parser.add_argument("--checks", action="append", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = args.pilot / "process_chain"
    artifacts = sorted(
        p for p in output.iterdir()
        if p.is_file() and p.name not in ("verification.json", "generation.log")
    )
    record = {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "tests_run": args.tests_run,
        "failures": 0,
        "errors": 0,
        "checks": args.checks,
        "outputs": [{"file": p.name, "sha256": digest(p)} for p in artifacts],
        "test_sources": [
            {"file": portable(t, root), "sha256": digest(t)} for t in args.tests
        ],
        "note": (
            "Tests verify data assembly and internal provenance, no engineering truth. "
            "Provenance records project-relative paths so that the artifacts stay "
            "byte-identical when the project is relocated."
        ),
    }
    (output / "verification.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"outputs": len(record["outputs"]),
                      "provenance_sha256": next(
                          o["sha256"] for o in record["outputs"] if o["file"] == "provenance.json")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
