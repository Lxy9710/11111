"""Extract private copies of previously unextracted drawings for a reviewed batch."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import psutil

from build_pilot_pair import digest, save_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drawings", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.resume:
        raise FileExistsError(args.output)
    drawings = sorted(args.drawings.glob("*.dwg"))
    if not drawings:
        raise ValueError("No root-level drawings found")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    records_path = args.output / "extraction_results.json"
    records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
    indexed = sorted(enumerate(drawings), key=lambda item: (not item[1].stem.endswith(".01"), item[0]))
    for index, drawing in indexed:
        if drawing.stem == "D207NQ.01":
            continue
        identity = f"drawing-{index:02d}"
        case = args.output / identity
        case.mkdir(exist_ok=args.resume)
        source_hash = digest(drawing)
        manifest_path = case / "source" / "extraction_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["status"] != "completed":
                raise ValueError(f"Failed extraction requires manual review before retry: {manifest_path}")
            if manifest["source_sha256"] != source_hash or digest(Path(manifest["json_path"])) != manifest["json_sha256"]:
                raise ValueError(f"Resume source mismatch: {drawing}")
            continue
        attempt = len(list(case.glob("extract-command*.log")))
        with (case / f"extract-command-{attempt}.log").open("x", encoding="utf-8") as stream:
            result = subprocess.run([
                sys.executable, str(Path(__file__).with_name("extract_pilot_dwg.py")),
                "--project-root", str(args.project_root), "--drawing", str(drawing),
                "--output", str(case / "source"),
            ], stdout=stream, stderr=subprocess.STDOUT)
        record = {
            "batch_id": identity, "source_dwg": str(drawing.resolve()),
            "source_sha256": source_hash, "exit_code": result.returncode,
            "manifest": str((case / "source" / "extraction_manifest.json").resolve()),
        }
        if digest(drawing) != source_hash:
            raise RuntimeError(f"Source drawing changed: {drawing}")
        records.append(record)
        save_json(records_path, records)
        print(json.dumps(record, ensure_ascii=True), flush=True)
        if result.returncode:
            logging.error("Extraction failed for %s; see %s", drawing.name, case)
            # Do not start another CAD instance after a failed ownership/cleanup path.
            raise RuntimeError("Extraction batch stopped; review the failed extraction before continuing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        owned_pid = manifest["autocad_instance"]["pid"]
        try:
            process = psutil.Process(owned_pid)
        except psutil.NoSuchProcess:
            continue
        logging.info("Waiting for owned AutoCAD PID %s to exit after Quit", owned_pid)
        process.wait(timeout=120)


if __name__ == "__main__":
    main()
