"""Build the L3 geometric state dataset from the FAXIN corpus.

The parameter chain says what number each operation sets. It does not say
which feature of the part that number belongs to, or whether the operation
changed that feature. This module turns a folder of process drawings into
the geometric counterpart of the chain: for every operation, the features
it touches, the values it states, and whether it kept or changed each one.

What the corpus turned out to be
--------------------------------
The process drawings are per-operation views, not cumulative states. Each
one is laid out on its own, annotates what matters for its operation, and
does not accumulate the geometry of the steps before it: measured on the
corpus, the radial profiles of two consecutive steps share no radius at
all and are never nested.

So the state of the part cannot be read off a single drawing, and the
correspondence between an operation and the nominal part has to be
established through the geometry each dimension measures. That is what
:mod:`farmcad.data.localization` does.

The chain this module produces
------------------------------
For every operation:

    touched   the nominal features the operation states
    kept      those it states unchanged
    changed   those it states with a different value
    introduced  the dimensions it states that no nominal feature matches

That last group matters. An operation that states a dimension with no
nominal counterpart is introducing a feature the nominal part does not
have, and the chain has to say so rather than silently dropping it.

Usage:
    python scripts/build_l3_dataset.py --part C3359-08-01
    python scripts/build_l3_dataset.py --all --output chainmodel/data/l3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.geometry import (  # noqa: E402
    collect_entities,
    dimension_truth,
    merge_process_only_steps,
    plot_scale,
    radial_profile,
)
from farmcad.data.localization import (  # noqa: E402
    dimension_features,
    localise_onto_design,
)

GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"

STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")

#: Operations that remove material and therefore shrink the part.
REMOVING_OPERATIONS = ("车", "铣", "磨", "刨", "钻", "镗", "珩", "研", "切")

#: Operations that add material back.
ADDING_OPERATIONS = ("滚", "压", "镶", "焊", "涂", "镀")

L3_SCHEMA_VERSION = "farmcad-l3-v2"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def step_of(path: Path, part: str) -> tuple[int, str]:
    match = STEP.match(path.stem)
    if match:
        return int(match.group("number")), path.stem[len(part):].lstrip("-")
    return 9999, path.stem


def operation_kind(label: str) -> str:
    """Classify an operation by what it does to the material."""

    if any(marker in label for marker in ADDING_OPERATIONS):
        return "adding"
    if any(marker in label for marker in REMOVING_OPERATIONS):
        return "removing"
    return "neutral"


def build_case(folder: Path, design_path: Path | None = None) -> dict:
    """Build the geometric counterpart of one part's process chain."""

    part = folder.name
    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])

    raw_steps = []
    for path in files:
        number, label = step_of(path, part)
        raw_steps.append({"number": number, "label": label, "data": load(path)})

    # The state sequence only advances at steps that remove material. Steps
    # that merely mark the process are folded into the step they follow, so
    # the sequence contains genuine geometric states.
    merged = merge_process_only_steps(raw_steps)

    design = None
    nominal = []
    if design_path is not None and design_path.exists():
        design_data = load(design_path)
        nominal = dimension_features(design_data)
        scale = plot_scale(design_data)
        design = {
            "path": str(design_path.relative_to(BASE)),
            "entities": len(collect_entities(design_data)),
            "scale": scale,
            "radial_profile": radial_profile(design_data, scale),
            "dimensions": nominal,
            "dimension_truth": dimension_truth(design_data),
        }

    operations = []
    for index, step in enumerate(merged):
        data = step["data"]
        actual = dimension_features(data)
        scale = plot_scale(data)

        if design is not None:
            localisation = localise_onto_design(design_data, data)
        else:
            localisation = {
                "matches": [], "changed": [], "kept": [],
                "unmatched_operation": actual, "unmatched_design": [],
                "matched": 0, "operation_features": len(actual),
                "design_features": 0,
            }

        operations.append({
            "state_id": f"{part}#S{index:02d}",
            "step_index": index,
            "operation": step["label"],
            "route": step["route"],
            "operation_kind": operation_kind(step["label"]),
            "machining": step["machining"],
            "entities": len(collect_entities(data)),
            "scale": scale,
            "radial_profile": radial_profile(data, scale),
            "dimensions": actual,
            "touched": [
                {
                    "nominal_handle": m["target_handle"],
                    "nominal_value_mm": m["target_value_mm"],
                    "stated_value_mm": m["source_value_mm"],
                    "delta_mm": m["delta_mm"],
                }
                for m in localisation["matches"]
            ],
            "kept": [m["target_handle"] for m in localisation["kept"]],
            "changed": localisation["changed"],
            "introduced": [
                {"value_mm": f["value_mm"], "handle": f["handle"]}
                for f in localisation["unmatched_operation"]
            ],
            "untouched_nominal": [
                {"nominal_handle": f["handle"], "value_mm": f["value_mm"]}
                for f in localisation["unmatched_design"]
            ],
        })

    return {
        "schema_version": L3_SCHEMA_VERSION,
        "case_id": part,
        "design": design,
        "operations": operations,
        "summary": {
            "steps": len(raw_steps),
            "operations": len(operations),
            "dimensions_stated": sum(len(o["dimensions"]) for o in operations),
            "localised": sum(len(o["touched"]) for o in operations),
            "kept": sum(len(o["kept"]) for o in operations),
            "changed": sum(len(o["changed"]) for o in operations),
            "introduced": sum(len(o["introduced"]) for o in operations),
            "untouched_nominal": sum(len(o["untouched_nominal"]) for o in operations),
        },
    }


#: A value change smaller than this is rounding, not a geometric change.
#: A drawing is quoted to a fixed number of decimals, so the same feature
#: is annotated slightly differently in two layouts.
ROUNDING_TOLERANCE = 0.05


def _significant(before: float, after: float) -> bool:
    """Whether two values differ by more than the rounding tolerance."""

    return abs(after - before) > max(ROUNDING_TOLERANCE,
                                     0.001 * abs(before))


def check_consistency(case: dict) -> list[dict]:
    """Check the geometric chain for contradictions.

    These are the failure modes the paper has to rule out, and each check
    is one that the evidence can actually support.

    An interference is two operations that state the same feature with
    values that cannot both hold: the feature goes down and then back up,
    which no single pass of a route does.

    An illegal constraint is a material removing operation that states a
    value larger than the nominal one for a feature, when nothing in
    between added material back.

    A dangling reference is a nominal feature that every operation states
    differently, so no operation ever pins it down.

    Changes smaller than the rounding tolerance are not reported. A
    feature annotated 65.90 in one drawing and 65.95 in another is the
    same feature with a rounded value, not a feature that grew.
    """

    issues = []
    operations = case["operations"]

    # Interference: the same feature, stated inconsistently by two steps.
    stated_by = {}
    for operation in operations:
        for touch in operation["touched"]:
            handle = touch["nominal_handle"]
            stated_by.setdefault(handle, []).append((operation, touch))

    for handle, entries in stated_by.items():
        if len(entries) < 2:
            continue
        # The values a single feature takes must not oscillate: a feature
        # that goes down and then back up was changed twice, which no
        # single pass of a route does. Rounding differences do not count
        # as a direction change.
        values = [touch["stated_value_mm"] for _, touch in entries]
        directions = []
        for i in range(len(values) - 1):
            if not _significant(values[i], values[i + 1]):
                continue
            directions.append(1 if values[i + 1] > values[i] else -1)
        turns = sum(1 for i in range(len(directions) - 1)
                    if directions[i] * directions[i + 1] < 0)
        if turns:
            issues.append({
                "kind": "interference",
                "feature": handle,
                "detail": f"同一特征在工序间取值反复: {values}",
                "operations": [o["operation"] for o, _ in entries],
            })

    # An illegal constraint: a removing operation states a value larger
    # than the value the previous operation left on the same feature.
    #
    # The comparison is against the previous operation, not against the
    # nominal value. A removing operation works from the stock towards the
    # finished size, so its value is normally larger than the nominal one
    # and shrinks as the route proceeds. A value that grows from one
    # removing operation to the next means material was added back
    # without anything adding it.
    previous_value: dict[str, tuple] = {}
    for index, operation in enumerate(operations):
        for touch in operation["touched"]:
            handle = touch["nominal_handle"]
            stated = touch["stated_value_mm"]
            earlier = previous_value.get(handle)
            if earlier is not None and operation["operation_kind"] == "removing":
                before_operation, before_value = earlier
                if _significant(before_value, stated) and stated > before_value:
                    between = operations[before_operation["step_index"]:index]
                    if not any(o["operation_kind"] == "adding" for o in between):
                        issues.append({
                            "kind": "illegal_growth",
                            "feature": handle,
                            "state": operation["state_id"],
                            "detail": f"{before_operation['operation']} 之后特征 "
                                      f"{handle} 为 {before_value}, "
                                      f"{operation['operation']} 却增至 {stated}, "
                                      f"中间没有加料工序",
                        })
            previous_value[handle] = (operation, stated)

    # A dangling reference: a feature every operation states, but none
    # pins down.
    for handle, entries in stated_by.items():
        if len(entries) < 2:
            continue
        if len({touch["stated_value_mm"] for _, touch in entries}) == 1:
            continue
        if all(_significant(touch["nominal_value_mm"], touch["stated_value_mm"])
               for _, touch in entries):
            issues.append({
                "kind": "dangling_feature",
                "feature": handle,
                "detail": f"特征 {handle} 被每道工序都声明, 但每次都与名义值不同",
            })

    return issues


def split_parts(cases: list[dict], train_ratio: float = 0.5) -> dict:
    """Split the parts into a training and a validation set.

    The split is by part, never by step, because the steps of one part share
    its geometry and would otherwise leak across the split.
    """

    names = sorted(case["case_id"] for case in cases)
    cut = max(1, int(round(len(names) * train_ratio)))
    return {"train": names[:cut], "validation": names[cut:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", action="append", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--train-ratio", type=float, default=0.5)
    args = parser.parse_args()

    folders = sorted(p for p in FAXIN.iterdir() if p.is_dir())
    if args.part:
        folders = [p for p in folders if p.name in set(args.part)]
    if not folders:
        print("没有可处理的零件")
        return 1

    cases = []
    for folder in folders:
        design_path = DESIGN / f"{folder.name}.json"
        case = build_case(folder, design_path)
        issues = check_consistency(case)
        case["consistency_issues"] = issues
        cases.append(case)
        summary = case["summary"]
        print(f"{folder.name:<16s} 工序 {summary['operations']:>2d}  "
              f"声明尺寸 {summary['dimensions_stated']:>3d}  "
              f"定位 {summary['localised']:>3d}  "
              f"保持 {summary['kept']:>3d}  "
              f"改动 {summary['changed']:>3d}  "
              f"新增 {summary['introduced']:>3d}  "
              f"未提及 {summary['untouched_nominal']:>3d}  "
              f"一致性问题 {len(issues):>2d}")
        for issue in issues[:4]:
            print(f"      [{issue['kind']}] {issue['detail']}")

    split = split_parts(cases, args.train_ratio)
    print()
    print(f"训练集: {split['train']}")
    print(f"验证集: {split['validation']}")

    output = args.output or (BASE / "chainmodel" / "data" / "l3")
    output.mkdir(parents=True, exist_ok=True)
    for case in cases:
        target = output / f"{case['case_id'].replace('/', '__')}.json"
        target.write_text(json.dumps(case, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    (output / "split.json").write_text(
        json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
