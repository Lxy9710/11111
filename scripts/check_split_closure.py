"""Verify that a proposed split is closed with respect to the training vocabulary.

The trainer builds the operation and parameter vocabulary from the training
split only. If a held-out family introduces an operation or parameter that no
training family contains, the model cannot emit it, and the held-out loss also
becomes a meaningless model-selection signal.

This check is therefore a precondition for publishing a split, not an
after-the-fact observation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(batch: Path) -> list[dict]:
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    cases = []
    for case in manifest["cases"]:
        target = json.loads((batch / case["target"]).read_text(encoding="utf-8"))
        cases.append({
            "case_id": case["case_id"], "family_id": case["family_id"], "split": case["split"],
            "operations": {e["operation"] for e in target["events"] if e["action"] == "OP"},
            "parameters": {e["parameter"] for e in target["events"] if e["action"] == "PARAM"},
            "param_events": sum(1 for e in target["events"] if e["action"] == "PARAM"),
        })
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, type=Path)
    args = parser.parse_args()

    cases = load(args.batch)
    train = [c for c in cases if c["split"] == "train"]
    train_ops = set().union(*[c["operations"] for c in train])
    train_params = set().union(*[c["parameters"] for c in train])

    ok = True
    print(f"train: {len(train)} cases, {len(train_ops)} operations, {len(train_params)} parameters")
    for split in ("validation", "test"):
        held = [c for c in cases if c["split"] == split]
        lost_ops = set().union(*[c["operations"] for c in held]) - train_ops
        lost_params = set().union(*[c["parameters"] for c in held]) - train_params
        print(f"\n{split}: {len(held)} cases")
        for case in held:
            print(f"  {case['case_id']:24s} {case['family_id']:26s} events={case['param_events']}")
        if lost_ops or lost_params:
            ok = False
            if lost_ops:
                print(f"  NOT CLOSED, unseen operations   : {sorted(lost_ops)}")
            if lost_params:
                print(f"  NOT CLOSED, unseen parameters  : {sorted(lost_params)}")
        else:
            print("  closed: every held-out operation and parameter occurs in train")

    print("\nRESULT:", "closed" if ok else "NOT CLOSED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
