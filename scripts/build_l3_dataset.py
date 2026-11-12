"""Build the L3 geometric state dataset from the FAXIN corpus.

The parameter chain says what number each operation sets. It does not say
what the part looks like after that operation, so a cascade cannot be
checked. This module turns a folder of process drawings into an explicit
sequence of geometric states:

    state 0  ->  operation 1  ->  state 1  ->  operation 2  ->  state 2 ...

Each state records the entities that exist, the radial profile of the part
in millimetres, the dimensions that measure it with their real values, and
the correspondence to the previous state: what was added, what was removed,
and what changed value.

The transitions are what a cascaded modification has to explain. A transition
is legal when the part did not lose material it should have kept, when a
dimension did not move in a direction the operation cannot cause, and when
the entities the operation claims to touch are actually present.

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
    cascade_trace,
    collect_entities,
    dimension_truth,
    has_part_contour,
    merge_process_only_steps,
    pair_across_steps,
    pair_geometry_across_steps,
    plot_scale,
    profile_delta,
    radial_profile,
)

GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"

STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")

#: Operations that remove material and therefore shrink the part.
REMOVING_OPERATIONS = ("车", "铣", "磨", "刨", "钻", "镗", "珩", "研", "切")

#: Operations that add material back.
ADDING_OPERATIONS = ("滚", "压", "镶", "焊", "涂", "镀")

L3_SCHEMA_VERSION = "farmcad-l3-v1"


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


def _state_of(data: dict, label: str, step_index: int, route: list[str],
              machining: bool) -> dict:
    """Build the geometric description of one state."""

    scale = plot_scale(data)
    truth = dimension_truth(data)
    profile = radial_profile(data, scale)
    return {
        "operation": label,
        "route": route,
        "step_index": step_index,
        "operation_kind": operation_kind(label),
        "machining": machining,
        "entities": len(collect_entities(data)),
        "scale": scale,
        "has_part_contour": has_part_contour(data),
        "radial_profile": profile,
        "dimensions": truth,
    }


def build_states(folder: Path, design_path: Path | None = None) -> dict:
    """Build the geometric state sequence of one part."""

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

    states = []
    previous_data = None
    previous_state = None
    for index, step in enumerate(merged):
        data = step["data"]
        entry = _state_of(data, step["label"], index, step["route"],
                          step["machining"])
        entry["state_id"] = f"{part}#S{index:02d}"
        if previous_data is not None:
            pairing = pair_across_steps(previous_data, data)
            shapes = pair_geometry_across_steps(previous_data, data)
            delta = profile_delta(previous_state["radial_profile"],
                                  entry["radial_profile"])
            entry["transition"] = {
                "paired_dimensions": len(pairing["pairs"]),
                "changed_dimensions": len(pairing["changed"]),
                "added_dimensions": len(pairing["added"]),
                "removed_dimensions": len(pairing["removed"]),
                "value_changes": pairing["changed"],
                "geometry_common": shapes["common"],
                "geometry_added": shapes["added"],
                "geometry_removed": shapes["removed"],
                "geometry_jaccard": round(shapes["jaccard"], 4),
                "profile": delta,
            }
        states.append(entry)
        previous_data = data
        previous_state = entry

    traces = cascade_trace([{"label": s["label"], "data": s["data"]}
                            for s in merged])

    design = None
    if design_path is not None and design_path.exists():
        design_data = load(design_path)
        design_state = _state_of(design_data, "设计图", -1, ["设计图"], True)
        design_state["path"] = str(design_path.relative_to(BASE))
        design = design_state

    return {
        "schema_version": L3_SCHEMA_VERSION,
        "case_id": part,
        "design": design,
        "states": states,
        "cascade_traces": traces,
        "summary": {
            "steps": len(raw_steps),
            "geometric_states": len(states),
            "machining_steps": sum(1 for s in states if s["machining"]),
            "states_with_part_contour": sum(1 for s in states if s["has_part_contour"]),
            "cross_step_dimensions": sum(
                s.get("transition", {}).get("paired_dimensions", 0) for s in states),
            "changed_dimensions": sum(
                s.get("transition", {}).get("changed_dimensions", 0) for s in states),
            "decreasing_traces": sum(1 for t in traces if t["decreasing"]),
        },
    }


def check_consistency(case: dict) -> list[dict]:
    """Check the geometric states for contradictions.

    These are the failure modes the paper has to rule out: a feature that
    the operation touches is not present, the part gained material where an
    operation only removes it, a dimension moved in a direction the operation
    cannot cause, or a dimension points at geometry that is gone.

    Only checks that the evidence actually supports are performed. Entity
    counts are not compared across steps, because each process drawing is
    laid out independently and adds its own section views, tool paths and
    annotation, so the entity count carries no part-level meaning.
    """

    issues = []
    states = case["states"]
    for state in states:
        transition = state.get("transition")
        if transition is None:
            continue
        kind = state["operation_kind"]
        name = state["operation"]
        profile = transition["profile"]

        # An operation that only removes material must not add a radius to
        # the part's radial profile.
        if kind == "removing" and profile["added_radii"]:
            issues.append({
                "state": state["state_id"],
                "operation": name,
                "kind": "material_added",
                "detail": f"{name} 去料工序却新增了半径 "
                          f"{sorted(profile['added_radii'])}",
            })

        # A dimension that moved in the direction the operation cannot cause.
        if kind == "removing":
            for change in transition["value_changes"]:
                before = change["before_value"]
                after = change["after_value"]
                if before is None or after is None:
                    continue
                if after > before:
                    issues.append({
                        "state": state["state_id"],
                        "operation": name,
                        "kind": "dimension_grew",
                        "detail": f"跨度 {change['span']} 的尺寸由 {before} 增至 {after}",
                    })

        # A dimension that disappeared while the geometry stayed is a
        # dangling reference: something was un-annotated, not removed.
        if transition["removed_dimensions"] and not profile["removed_radii"]:
            issues.append({
                "state": state["state_id"],
                "operation": name,
                "kind": "dangling_dimension",
                "detail": f"移除了 {transition['removed_dimensions']} 个尺寸标注, "
                          f"但径向轮廓没有减少",
            })

    # A part drawn at a later state must not be larger than at an earlier
    # state, when the operations between them only remove material.
    contours = [s for s in states if s["has_part_contour"]]
    for i in range(len(contours)):
        for j in range(i + 1, len(contours)):
            between = states[contours[i]["step_index"]:contours[j]["step_index"]]
            if all(s["operation_kind"] != "adding" for s in between):
                before = contours[i]["radial_profile"]["max"]
                after = contours[j]["radial_profile"]["max"]
                if after > before:
                    issues.append({
                        "state": contours[j]["state_id"],
                        "operation": contours[j]["operation"],
                        "kind": "contour_grew",
                        "detail": f"最大半径由 {before} 增至 {after}, 中间工序只去料",
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
        case = build_states(folder, design_path)
        issues = check_consistency(case)
        case["consistency_issues"] = issues
        cases.append(case)
        summary = case["summary"]
        print(f"{folder.name:<16s} 状态 {summary['geometric_states']:>2d} "
              f"(工序 {summary['steps']:>2d})  "
              f"轮廓步 {summary['states_with_part_contour']:>2d}  "
              f"跨步尺寸 {summary['cross_step_dimensions']:>3d}  "
              f"值变化 {summary['changed_dimensions']:>2d}  "
              f"递减轨迹 {summary['decreasing_traces']:>2d}  "
              f"一致性问题 {len(issues):>2d}")
        for issue in issues[:5]:
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
