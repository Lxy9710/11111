"""Check portable dataset contracts, optionally checking original Windows CAD sources."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

from farmcad.data.dataset import load_cases
from farmcad.data.schema import validate_design
from farmcad.utils import file_hash, object_hash, read_json, write_json


def verify(dataset: Path, check_sources: bool, checkpoint: Path = None, device: str = "cpu") -> dict:
    cases = load_cases(dataset / "manifest.ready.json", smoke=True)
    catalog = read_json(dataset / "design_pool_manifest.json")["cases"]
    inventory = read_json(dataset / "audit" / "inventory.json")
    summary = read_json(dataset / "dataset_summary.json")
    families, fingerprints, source_hashes = {}, {}, {}
    counts = Counter()
    gold_fields = 0
    encoded = []
    model = None
    if checkpoint is not None:
        import torch
        from farmcad.models.local import LocalPredictor
        from farmcad.training.trainer import load_checkpoint
        torch.set_num_threads(1)
        model, _, _ = load_checkpoint(checkpoint, device)
        if not isinstance(model, LocalPredictor):
            raise ValueError("Expected a local-model checkpoint")
    for row in catalog:
        design = read_json(dataset / row["design"])
        validate_design(design)
        if design["case_id"] != row["case_id"]:
            raise ValueError("Design-pool identity mismatch")
        if object_hash({k: v for k, v in design.items() if k != "case_id"}) != row["design_fingerprint"]:
            raise ValueError("Design-pool fingerprint changed")
        if row["target"] is not None:
            provenance = read_json((dataset / row["target"]).parent / "provenance.json")
            if file_hash(dataset / row["target"]) != provenance["target_sha256"]:
                raise ValueError("Paired target changed since generation")
        for lookup, key in ((families, "family_id"), (fingerprints, "design_fingerprint"),
                            (source_hashes, "drawing_sha256")):
            if lookup.setdefault(row[key], row["split"]) != row["split"]:
                raise ValueError(f"Cross-split leakage by {key}")
        counts["outer_designs"] += 1
        counts["outer_entities"] += len(design["entities"])
        counts["feature_candidates"] += len(design["features"])
        counts["requirement_candidates"] += len(design["requirements"])
        if row["target"] is None and row["readiness"] == "system_weak_pair_ready_smoke_only":
            raise ValueError("Unlabelled sample incorrectly claims readiness")
        if model is not None:
            prediction = model.predict(design, model.vocabulary.operations[2],
                                       model.vocabulary.parameters[2], [], 0)
            encoded.append({"case_id": row["case_id"], "prediction_fields": sorted(prediction),
                            "purpose": "input_compatibility_only_not_accuracy_evaluation"})
    for case in cases:
        for event in case.target["events"]:
            counts["ready_operations"] += event["action"] == "OP"
            counts["ready_parameters"] += event["action"] == "PARAM"
            gold_fields += event["quality"] == "gold"
            gold_fields += sum(a["quality"] == "gold" for a in event.get("labels", {}).values())
    for key, count in counts.items():
        if summary[key] != count:
            raise ValueError(f"Summary count mismatch for {key}: {summary[key]} != {count}")
    if gold_fields != summary["human_gold_fields"]:
        raise ValueError("Incorrect gold count")
    ready_counts = {split: sum(c.split == split for c in cases) for split in ("train", "validation", "test")}
    if ready_counts != summary["ready_pairs_by_split"]:
        raise ValueError("Ready-split count mismatch")
    for split in ready_counts:
        reserved = read_json(dataset / "reserved_splits" / f"{split}.json")
        if reserved["cases"] != [r for r in catalog if r["split"] == split]:
            raise ValueError("Reservation files differ from design pool")
    verified_extractions = 0
    if check_sources:
        for row in inventory:
            for path_key, hash_key in (("source_dwg", "source_dwg_sha256"), ("raw_json", "raw_json_sha256")):
                if file_hash(Path(row[path_key])) != row[hash_key]:
                    raise ValueError(f"Original source changed: {row[path_key]}")
            if row["extraction_manifest"]:
                manifest = read_json(Path(row["extraction_manifest"]))
                if not all((manifest["status"] == "completed", manifest["source_unchanged"], manifest["autocad_released"])):
                    raise ValueError("Incomplete or unsafe CAD extraction")
                if any(manifest["diagnostics"].values()):
                    raise ValueError("CAD extraction has unresolved diagnostics")
                if manifest["source_sha256"] != row["source_dwg_sha256"] or manifest["json_sha256"] != row["raw_json_sha256"]:
                    raise ValueError("Extraction manifest differs from audited source")
                verified_extractions += 1
    return {
        "status": "verified_partial_dataset", "counts": dict(counts),
        "inventory_rows": len(inventory), "ready_pairs": ready_counts, "human_gold_fields": gold_fields,
        "original_sources_checked": check_sources, "verified_extraction_manifests": verified_extractions,
        "model_input_checks": encoded, "model_device": device if model is not None else None,
        "heldout_accuracy_claimed": False,
        "outputs": [{"file": str(p.relative_to(dataset)).replace("\\", "/"), "sha256": file_hash(p)}
                    for p in sorted(dataset.rglob("*")) if p.is_file()],
        "scripts": [{"file": p.name, "sha256": file_hash(p)}
                    for p in (Path(__file__), Path(__file__).with_name("build_outer_dataset.py"),
                              Path(__file__).with_name("assemble_outer_dataset.py"),
                              Path(__file__).with_name("prepare_batch_sources.py"))],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-sources", action="store_true")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = verify(args.dataset, args.check_sources, args.checkpoint, args.device)
    write_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("outputs", "scripts", "model_input_checks")},
                     ensure_ascii=False))
