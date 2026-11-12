"""The L3 experiment: can a synthesiser reproduce a process step's drawing?

This is the falsifiable target. The chain of a part, plus its nominal
design drawing, has to predict what each operation's drawing contains.
The prediction is scored against the real drawing, so the claim can fail.

Two things are measured, and the difference between them is the point.

The oracle
    The chain is replaced by the real operation drawing. Whatever this
    scores is the ceiling: it says how much of the drawing is explained by
    the operation's own stated dimensions. It is not a model, it is the
    target itself.

The rule baseline
    The chain is replaced by a rule: an operation states the nominal
    dimensions it touches, unchanged. This is what a parameter-only system
    achieves, because it has no way to know that an operation changes a
    value.

The gap between them is what the geometric part of the chain has to
supply: the ability to say that an operation changes a value, and to say
which value.

The split is by part, never by step, because the steps of one part share
its geometry and would otherwise leak across the split.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "chainmodel" / "src"))

from farmcad.data.geometry import merge_process_only_steps  # noqa: E402
from farmcad.data.localization import (  # noqa: E402
    dimension_features,
    localise_onto_design,
)
from farmcad.data.synthesis import (  # noqa: E402
    SynthesisedDimension,
    score_synthesis,
)

GEO = BASE / "设计图" / "geo"
FAXIN = GEO / "FAXIN"
DESIGN = GEO / "设计图"
STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def step_of(path: Path, part: str) -> tuple[int, str]:
    match = STEP.match(path.stem)
    if match:
        return int(match.group("number")), path.stem[len(part):].lstrip("-")
    return 9999, path.stem


def oracle_prediction(actual_features: list[dict]) -> list[SynthesisedDimension]:
    """The ceiling: state exactly what the drawing states."""

    return [SynthesisedDimension(value_mm=f["value_mm"],
                                 origin="nominal",
                                 source_feature=f.get("handle"))
            for f in actual_features]


def rule_prediction(localisation: dict,
                    nominal: list[dict]) -> list[SynthesisedDimension]:
    """The baseline: restate the nominal values the operation touches.

    This is what a parameter-only chain can do. It knows which nominal
    feature an operation works on, but it has no way to say that the
    operation changes the value, so it restates the nominal one.
    """

    by_handle = {f["handle"]: f for f in nominal}
    out = []
    for match in localisation["matches"]:
        feature = by_handle.get(match["target_handle"])
        if feature is None:
            continue
        out.append(SynthesisedDimension(
            value_mm=feature["value_mm"],
            origin="nominal",
            source_feature=feature["handle"],
        ))
    return out


def run(part: str) -> dict:
    folder = FAXIN / part
    design_path = DESIGN / f"{part}.json"
    design = load(design_path)
    nominal = dimension_features(design)

    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])
    raw = [{"label": step_of(p, part)[1], "data": load(p)} for p in files]
    merged = merge_process_only_steps(raw)

    rows = []
    for step in merged:
        actual = dimension_features(step["data"])
        if not actual:
            continue
        localisation = localise_onto_design(design, step["data"])
        oracle = score_synthesis(oracle_prediction(actual), actual)
        rule = score_synthesis(rule_prediction(localisation, nominal), actual)
        rows.append({
            "operation": step["label"],
            "actual": len(actual),
            "oracle": oracle,
            "rule": rule,
        })

    def aggregate(key):
        predicted = sum(r[key]["predicted"] for r in rows)
        matched = sum(r[key]["matched"] for r in rows)
        actual = sum(r[key]["actual"] for r in rows)
        return {
            "predicted": predicted,
            "matched": matched,
            "actual": actual,
            "precision": matched / max(1, predicted),
            "recall": matched / max(1, actual),
            "f1": 2 * matched / max(1, predicted + actual),
        }

    return {
        "case_id": part,
        "steps": rows,
        "oracle": aggregate("oracle"),
        "rule": aggregate("rule"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--train-ratio", type=float, default=0.5)
    args = parser.parse_args()

    cases = []
    for folder in sorted(p for p in FAXIN.iterdir() if p.is_dir()):
        part = folder.name
        if not (DESIGN / f"{part}.json").exists():
            print(f"  {part}: 设计图缺失, 跳过")
            continue
        case = run(part)
        cases.append(case)
        print(f"\n  {part}")
        for row in case["steps"]:
            print(f"    {row['operation'][:14]:<15s} 实际尺寸 {row['actual']:>3d}  "
                  f"oracle P{row['oracle']['precision'] * 100:5.1f}% R{row['oracle']['recall'] * 100:5.1f}%  "
                  f"rule P{row['rule']['precision'] * 100:5.1f}% R{row['rule']['recall'] * 100:5.1f}%")

    names = sorted(c["case_id"] for c in cases)
    cut = max(1, int(round(len(names) * args.train_ratio)))
    split = {"train": names[:cut], "validation": names[cut:]}

    def aggregate(key, subset):
        rows = [c for c in cases if c["case_id"] in subset]
        predicted = sum(c[key]["predicted"] for c in rows)
        matched = sum(c[key]["matched"] for c in rows)
        actual = sum(c[key]["actual"] for c in rows)
        return {
            "predicted": predicted, "matched": matched, "actual": actual,
            "precision": matched / max(1, predicted),
            "recall": matched / max(1, actual),
            "f1": 2 * matched / max(1, predicted + actual),
        }

    print()
    print("=" * 88)
    print("L3 可证伪目标: 合成器复现每道工序图纸的尺寸集合")
    print("=" * 88)
    for name, subset in (("训练集", split["train"]), ("验证集", split["validation"]), ("全部", names)):
        if not subset:
            continue
        oracle = aggregate("oracle", subset)
        rule = aggregate("rule", subset)
        print(f"  {name} ({len(subset)} 个零件)")
        print(f"      oracle  P {oracle['precision'] * 100:5.1f}%  "
              f"R {oracle['recall'] * 100:5.1f}%  F1 {oracle['f1'] * 100:5.1f}%  "
              f"({oracle['matched']}/{oracle['actual']})")
        print(f"      rule    P {rule['precision'] * 100:5.1f}%  "
              f"R {rule['recall'] * 100:5.1f}%  F1 {rule['f1'] * 100:5.1f}%  "
              f"({rule['matched']}/{rule['actual']})")
        print(f"      几何链需要补足的召回: "
              f"{(oracle['recall'] - rule['recall']) * 100:5.1f} 个百分点")

    output = args.output or (BASE / "chainmodel" / "data" / "l3_experiment.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "cases": cases,
        "split": split,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
