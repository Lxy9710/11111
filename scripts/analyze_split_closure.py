"""Choose a family-level split whose held-out part is closed w.r.t. the train vocabulary.

The trainer builds the operation and parameter vocabulary from the training
split only. A held-out family that introduces an unseen operation or parameter
cannot be predicted at all, and its loss also pollutes the checkpoint-selection
signal. This script reports, for every family, the vocabulary it uniquely
contributes, and then proposes a split in which validation and test are closed
with respect to train.

It also reports the operations and parameters that occur in exactly one family.
Those can never be held out without breaking closure, so they belong in train by
construction rather than by accident.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load(batch: Path) -> list[dict]:
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    cases = []
    for case in manifest["cases"]:
        target = json.loads((batch / case["target"]).read_text(encoding="utf-8"))
        operations = {e["operation"] for e in target["events"] if e["action"] == "OP"}
        parameters = {e["parameter"] for e in target["events"] if e["action"] == "PARAM"}
        cases.append({
            "case_id": case["case_id"], "family_id": case["family_id"],
            "route_source": case.get("route_source", ""),
            "operations": operations, "parameters": parameters,
            "param_events": sum(1 for e in target["events"] if e["action"] == "PARAM"),
        })
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, type=Path)
    args = parser.parse_args()

    cases = load(args.batch)
    by_family: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_family[case["family_id"]].append(case)

    family_ops = {f: set().union(*[c["operations"] for c in group]) for f, group in by_family.items()}
    family_params = {f: set().union(*[c["parameters"] for c in group]) for f, group in by_family.items()}

    # Vocabulary tokens that only one family can supply.
    op_families = defaultdict(set)
    param_families = defaultdict(set)
    for family in by_family:
        for op in family_ops[family]:
            op_families[op].add(family)
        for param in family_params[family]:
            param_families[param].add(family)

    unique_ops = {op: sorted(fs) for op, fs in op_families.items() if len(fs) == 1}
    unique_params = {param: sorted(fs) for param, fs in param_families.items() if len(fs) == 1}

    print("== families ==")
    for family in sorted(by_family):
        group = by_family[family]
        sources = sorted({c["route_source"] for c in group})
        print(f"  {family:26s} cases={len(group)} events={sum(c['param_events'] for c in group)} sources={sources}")

    print("\n== operations only one family can supply ==")
    for op, fs in sorted(unique_ops.items()):
        print(f"  {op:12s} -> {fs}")
    print("\n== parameters only one family can supply ==")
    for param, fs in sorted(unique_params.items()):
        print(f"  {param:12s} -> {fs}")

    # A family may be held out only if the remaining train covers its vocabulary.
    print("\n== hold-out feasibility (family removed from train) ==")
    for family in sorted(by_family):
        rest_ops = set().union(*[family_ops[f] for f in by_family if f != family])
        rest_params = set().union(*[family_params[f] for f in by_family if f != family])
        lost_ops = sorted(family_ops[family] - rest_ops)
        lost_params = sorted(family_params[family] - rest_params)
        closed = not lost_ops and not lost_params
        print(f"  {family:26s} closed={closed}"
              + (f" lost_ops={lost_ops}" if lost_ops else "")
              + (f" lost_params={lost_params}" if lost_params else ""))

    # Families that cannot be held out at all must stay in train.
    mandatory = {fs[0] for fs in unique_ops.values()} | {fs[0] for fs in unique_params.values()}
    print(f"\n== families that must stay in train (unique vocabulary) ==\n  {sorted(mandatory)}")

    holdable = sorted(set(by_family) - mandatory)
    print(f"\n== holdable families ==\n  {holdable}")


if __name__ == "__main__":
    main()
