"""Assign train / validation / test splits to a published dataset batch.

Splitting is by family, never by case: the same drawing number may not appear
in two splits, and the design fingerprint is checked so that two physically
identical drawings cannot straddle a split either.

The choice of which family goes where is explicit and recorded, because a
system-generated dataset with a single split cannot be evaluated. The rule used
here is deliberately conservative: the outer-ring pilot is held out as a
cross-domain validation family, and a small number of cage families are held
out from the cage population.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from farmcad.utils import object_hash


def supervision_fingerprint(target: dict) -> str:
    """指纹：工序序列 + 参数 + 特征/要求绑定 + 更新算子，**忽略数值**。

    这衡量的是"监督了什么"，而不是"数值是多少"。
    类别级路线模板会让同一类别的所有案例在此指纹下相同，
    因此该指纹跨 split 即意味着留出案例并未带来新的监督内容。
    """
    events = []
    for event in target.get("events", []):
        if event.get("action") == "OP":
            events.append(["OP", event.get("operation")])
        elif event.get("action") == "PARAM":
            labels = event.get("labels", {})
            events.append([
                "PARAM",
                event.get("parameter"),
                sorted(labels.get("features", {}).get("value") or []),
                sorted(labels.get("requirements", {}).get("value") or []),
                labels.get("update", {}).get("value"),
            ])
    return object_hash(events)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--validation-family", action="append", default=[],
                        help="Family IDs to place in the validation split.")
    parser.add_argument("--test-family", action="append", default=[],
                        help="Family IDs to place in the test split.")
    args = parser.parse_args()

    manifest_path = args.batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = set(args.validation_family)
    test = set(args.test_family)

    splits = {}
    for case in manifest["cases"]:
        family = case["family_id"]
        if family in test:
            splits[case["case_id"]] = "test"
        elif family in validation:
            splits[case["case_id"]] = "validation"
        else:
            splits[case["case_id"]] = "train"

    # A family must not appear in two splits.
    family_splits: dict[str, set[str]] = {}
    for case in manifest["cases"]:
        family_splits.setdefault(case["family_id"], set()).add(splits[case["case_id"]])
    for family, names in family_splits.items():
        if len(names) > 1:
            raise ValueError(f"Family {family} spans multiple splits: {sorted(names)}")

    # Identical designs must not straddle a split.
    fingerprints: dict[str, str] = {}
    for case in manifest["cases"]:
        design = json.loads((args.batch / case["design"]).read_text(encoding="utf-8"))
        fingerprint = object_hash({k: v for k, v in design.items() if k != "case_id"})
        if fingerprint in fingerprints and fingerprints[fingerprint] != splits[case["case_id"]]:
            raise ValueError(f"Identical design straddles a split: {case['case_id']}")
        fingerprints[fingerprint] = splits[case["case_id"]]

    # Identical *supervision* must not straddle a split either.
    #
    # Two cases can have different drawings and still carry the same supervision:
    # a category-level route template gives every cage case the same operation
    # sequence, the same parameter set and the same feature/requirement bindings,
    # differing only in the numeric values. Such a case teaches the model a fixed
    # lookup table rather than any geometric reasoning, so holding one out does
    # not test generalization.
    #
    # The fingerprint below deliberately ignores numeric values and event IDs:
    # it captures *what* is supervised (operation, parameter, bindings, update),
    # not *how much*.
    supervision: dict[str, str] = {}
    for case in manifest["cases"]:
        target = json.loads((args.batch / case["target"]).read_text(encoding="utf-8"))
        fingerprint = supervision_fingerprint(target)
        if fingerprint in supervision and supervision[fingerprint] != splits[case["case_id"]]:
            raise ValueError(
                f"Identical supervision straddles a split: {case['case_id']} and the case "
                f"in split '{supervision[fingerprint]}' share the same operation/parameter/"
                f"binding pattern. Held-out cases must differ in what is supervised, "
                f"not only in numeric values.")
        supervision[fingerprint] = splits[case["case_id"]]

    for case in manifest["cases"]:
        case["split"] = splits[case["case_id"]]

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    review_path = args.batch / "review" / "cases.csv"
    review_updated = False
    try:
        with review_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            row["split"] = splits[row["case_id"]]
        with review_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        review_updated = True
    except PermissionError:
        # The review sheet is often open in a spreadsheet while the dataset is
        # being split. The manifest is the authoritative training input, so a
        # locked sheet is reported rather than blocking the split.
        print(f"WARNING: review sheet is locked and was not updated: {review_path}")

    reserved = args.batch / "reserved_splits"
    reserved.mkdir(exist_ok=True)
    grouped: dict[str, list[str]] = {}
    for case in manifest["cases"]:
        grouped.setdefault(splits[case["case_id"]], []).append(case["case_id"])
    for name, case_ids in grouped.items():
        (reserved / f"{name}.json").write_text(
            json.dumps({"split": name, "cases": case_ids}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    summary_path = args.batch / "dataset_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["splits"] = {name: len(ids) for name, ids in sorted(grouped.items())}
    summary["split_policy"] = (
        "Split by family. The outer-ring pilot family and the listed cage families "
        "are held out; no drawing number and no design fingerprint may straddle "
        "a split."
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "splits": summary["splits"],
        "validation_families": sorted(validation),
        "test_families": sorted(test),
        "review_sheet_updated": review_updated,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
