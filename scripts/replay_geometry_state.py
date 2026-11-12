"""Reconstruct the geometry state per operation from the parameter chain.

The question this answers: if the design drawing gains a groove, what would
have to be true for every operation's drawing to be legal, and can the
parameter chain we already have tell us?

The chain carries `derive`/`state_copy`/`design_copy` links between features,
so the working value of every feature at every operation is recoverable from
the target alone. This replays that and checks the geometric relations that
the feature vocabulary makes checkable.

Deliberately conservative: a check is only reported when the feature set
actually contains the quantities the check needs. A missing quantity is
reported as a gap, not silently assumed satisfied.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "chainmodel" / "data" / "processed" / "combined-batch-v1" / "cases"


def replay(case: Path) -> dict:
    design = json.loads((case / "design.json").read_text(encoding="utf-8"))
    target = json.loads((case / "target.json").read_text(encoding="utf-8"))

    current = {f["id"]: f["value"]["nominal"] for f in design["features"]}
    state_value: dict[str, float | None] = {}
    snapshots: list[dict] = []
    operation = None

    for event in target["events"]:
        if event["action"] == "OP":
            operation = event["operation"]
            snapshots.append({"operation": operation, "values": dict(current)})
            continue
        if event["action"] != "PARAM":
            continue
        labels = event["labels"]
        features = labels["features"]["value"] or []
        value = labels["value"]["value"]
        nominal = (value or {}).get("nominal")
        update = labels["update"]["value"]
        if not features or nominal is None:
            continue
        feature = features[0]
        if update == "state_copy":
            sources = labels["sources"]["value"] or []
            if sources and sources[0] in state_value:
                current[feature] = state_value[sources[0]]
            else:
                current[feature] = nominal
        elif update in ("derive", "design_copy", "external"):
            current[feature] = nominal
        state_value[event["id"]] = current.get(feature)

    return {
        "case_id": design["case_id"],
        "features": {f["id"]: f["value"]["nominal"] for f in design["features"]},
        "snapshots": snapshots,
        "final": current,
    }


def check_subordination(snapshot: dict) -> list[dict]:
    """Checks that need only quantities present in the feature vocabulary.

    The outer groove sits inside the outer diameter. For the groove to exist
    at all, its bottom diameter must be smaller than the outer diameter, and
    the material left between them (the wall) must not vanish.
    """

    results = []
    values = snapshot["values"]
    outer = values.get("outer_diameter")
    groove = values.get("groove_diameter")
    if outer is None or groove is None:
        return results
    wall = (outer - groove) / 2
    results.append({
        "operation": snapshot["operation"],
        "outer_diameter": outer,
        "groove_diameter": groove,
        "wall_thickness": wall,
        "violations": [] if wall > 0 else ["groove_breaks_through_outer_diameter"],
    })
    return results


def main() -> None:
    print("=== 特征词汇表检查:哪些几何量缺失")
    ring = json.loads((CASES / "ring-D207NQ.01" / "design.json").read_text(encoding="utf-8"))
    names = {f["id"] for f in ring["features"]}
    required_for_groove = {
        "groove_diameter": "沟底直径 De",
        "groove_position": "沟位置 ae",
        "groove_radius": "沟曲率 R",
        "groove_width": "沟宽 (判定 R <= width/2 需要)",
        "outer_diameter": "外径 D",
    }
    for key, meaning in required_for_groove.items():
        mark = "有" if key in names else "缺"
        print(f"  [{mark}] {key:<18s} {meaning}")

    print()
    print("=== 外圈案例(D207NQ.01)的几何状态重放")
    result = replay(CASES / "ring-D207NQ.01")
    print(f"{'工序':<16s} {'D外径':>9s} {'De沟底径':>10s} {'R曲率':>8s} {'壁厚':>8s}  判定")
    print("-" * 78)
    for snapshot in result["snapshots"]:
        checks = check_subordination(snapshot)
        values = snapshot["values"]
        outer = values.get("outer_diameter")
        groove = values.get("groove_diameter")
        radius = values.get("groove_radius")
        if not checks:
            continue
        check = checks[0]
        verdict = "; ".join(check["violations"]) if check["violations"] else "OK"
        print(f"{snapshot['operation'][:15]:<16s} {outer:>9.3f} {groove:>10.3f} "
              f"{radius if radius is not None else float('nan'):>8.2f} "
              f"{check['wall_thickness']:>8.3f}  {verdict}")

    print()
    print("=== 各工序后被写入的特征值(级联轨迹)")
    design_value = result["features"]
    previous = dict(design_value)
    for snapshot in result["snapshots"]:
        changed = {k: (previous[k], v) for k, v in snapshot["values"].items()
                   if k in previous and previous[k] != v}
        for feature, (before, after) in changed.items():
            print(f"  {snapshot['operation'][:16]:<17s} {feature:<18s} "
                  f"{before if before is not None else '-':>9} -> {after}")
        previous = snapshot["values"]


if __name__ == "__main__":
    main()
