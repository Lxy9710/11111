"""Audit the FAXIN corpus as a source of geometric process states.

The corpus under 设计图/geo/FAXIN holds one drawing per process step of a
part, which is the structure a geometric state machine needs: a sequence of
drawings that differ because the part was machined.

This script measures, for every part:

1. step count and how many steps are machining steps versus process-only
   steps (heat treatment, cleaning, surface treatment);
2. whether the part's own contour is drawn, and how its radius changes
   across the machining steps;
3. whether dimensions carry true geometry: `measurement` versus the geometric
   span between the extension points, and the ratio that recovers the real
   value from the displayed text;
4. whether a dimension's value cascades along the route, keyed by what it
   measures (span and orientation), so relayout between drawings does not
   break the tracking.

The output is a report, not a converted dataset.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
GEO = BASE / "设计图" / "geo" / "FAXIN"

STEP = re.compile(r"^(?P<part>.+?)(?P<op>[^0-9]+?)(?P<number>\d+)$")
NUMBER = re.compile(r"-?\d+\.?\d*")
NON_MACHINING = ("下料", "热处理", "清洗", "表面处理", "表处理", "磁粉", "荧光",
                 "退磁", "酸洗", "喷丸", "时效", "检测")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def all_entities(data: dict) -> list[dict]:
    """Top-level entities plus the entities inside every block."""

    out = list(data.get("entities") or [])
    for block in (data.get("blocks") or {}).values():
        out.extend(block.get("entities") or [])
    return out


def plain_numbers(text) -> list[float]:
    """The plain numbers inside an AutoCAD dimension text, without tolerances."""

    if not text:
        return []
    cleaned = re.sub(r"\\[A-Za-z][^;{}]*;", " ", str(text))
    cleaned = re.sub(r"[{}<>]", " ", cleaned)
    return [float(m) for m in NUMBER.findall(cleaned)]


def point(entity: dict, key: str):
    value = entity.get(key)
    if isinstance(value, list) and len(value) >= 2 and isinstance(value[0], (int, float)):
        return (float(value[0]), float(value[1]))
    return None


def step_of(path: Path, part: str) -> tuple[int, str]:
    match = STEP.match(path.stem)
    if match:
        return int(match.group("number")), path.stem[len(part):].lstrip("-")
    return 9999, path.stem


def contour_radius(entities: list[dict]) -> float:
    """The largest curve radius, which is the part contour when it is drawn."""

    radii = [float(e["radius"]) for e in entities
             if e.get("type") in ("CIRCLE", "ARC") and e.get("radius")]
    return max(radii) if radii else 0.0


def analyse(folder: Path) -> dict:
    part = folder.name
    files = sorted(folder.glob("*.json"), key=lambda p: step_of(p, part)[0])
    steps, track = [], defaultdict(dict)
    span_agree = span_total = 0
    ratios = []

    for path in files:
        number, label = step_of(path, part)
        data = load(path)
        entities = all_entities(data)
        machining = not any(k in label for k in NON_MACHINING)
        radius = contour_radius(entities)

        dimensions = []
        for dim in entities:
            if dim.get("type") != "DIMENSION":
                continue
            measurement = dim.get("measurement")
            first = point(dim, "ext_line1_point")
            second = point(dim, "ext_line2_point")
            values = plain_numbers(dim.get("text"))
            if measurement is None or not first or not second or not values:
                continue
            span = math.dist(first, second)
            angle = round(math.atan2(second[1] - first[1], second[0] - first[0]) % math.pi, 2)
            dimensions.append((measurement, span, values[0], angle))
            span_total += 1
            if abs(measurement - span) <= max(0.5, 0.01 * span):
                span_agree += 1
            if abs(values[0]) > 1.0 and 0.5 < measurement / values[0] < 10:
                ratios.append(round(measurement / values[0], 3))
            track[(round(span, 1), angle)][number] = (label, values[0])

        steps.append({
            "number": number, "label": label, "machining": machining,
            "entities": len(entities), "contour_radius": round(radius, 3),
            "dimensions": len(dimensions),
        })

    multi = {k: v for k, v in track.items() if len(v) > 1}
    decreasing = 0
    trajectories = []
    for key, entries in multi.items():
        ordered = sorted(entries.items())
        values = [v[1] for _, v in ordered]
        if len(set(values)) > 1 and all(values[i] >= values[i + 1]
                                       for i in range(len(values) - 1)):
            decreasing += 1
        trajectories.append({
            "span": key[0], "angle": key[1],
            "trajectory": [[label, value] for _, (label, value) in ordered],
        })

    scale = Counter(ratios).most_common(1)[0][0] if ratios else None
    return {
        "part": part,
        "steps": steps,
        "machining_steps": sum(1 for s in steps if s["machining"]),
        "steps_with_contour": sum(1 for s in steps if s["contour_radius"] >= 8),
        "contour_sequence": {s["label"]: s["contour_radius"] for s in steps
                             if s["contour_radius"] >= 8},
        "measurement_matches_span": span_agree,
        "dimension_total": span_total,
        "display_scale": scale,
        "cross_step_dimensions": len(multi),
        "decreasing_dimensions": decreasing,
        "trajectories": trajectories,
    }


def main() -> None:
    if not GEO.exists():
        raise SystemExit(f"找不到 FAXIN 目录: {GEO}")
    folders = sorted(p for p in GEO.iterdir() if p.is_dir())
    print(f"FAXIN 语料: {GEO}")
    print(f"零件数: {len(folders)}")
    print()
    print(f"{'零件':<16s} {'步':>3s} {'加工步':>5s} {'有轮廓':>5s} "
          f"{'尺寸':>5s} {'跨度一致':>8s} {'比例':>6s} {'跨步标注':>7s} {'其中递减':>7s}")
    print("-" * 92)

    reports = []
    for folder in folders:
        report = analyse(folder)
        reports.append(report)
        agreement = (f"{report['measurement_matches_span']}/{report['dimension_total']}"
                     if report["dimension_total"] else "-")
        print(f"{report['part'][:15]:<16s} {len(report['steps']):>3d} "
              f"{report['machining_steps']:>5d} {report['steps_with_contour']:>5d} "
              f"{report['dimension_total']:>5d} {agreement:>8s} "
              f"{str(report['display_scale']):>6s} {report['cross_step_dimensions']:>7d} "
              f"{report['decreasing_dimensions']:>7d}")

    print()
    print("=== 轮廓半径序列(加工步, 只列带零件轮廓的步)")
    for report in reports:
        if not report["contour_sequence"]:
            continue
        sequence = "  ".join(f"{label}:{radius}" for label, radius
                             in report["contour_sequence"].items())
        print(f"  {report['part']:<16s} {sequence}")

    print()
    print("=== 尺寸值的级联轨迹(按被测几何配对, 跨步)")
    for report in reports:
        if not report["trajectories"]:
            continue
        print(f"  {report['part']}")
        for trajectory in report["trajectories"]:
            path = " -> ".join(f"{label}:{value:g}" for label, value in trajectory["trajectory"])
            print(f"      跨距{trajectory['span']:<9} {path}")

    total_dims = sum(r["dimension_total"] for r in reports)
    total_agree = sum(r["measurement_matches_span"] for r in reports)
    total_cross = sum(r["cross_step_dimensions"] for r in reports)
    total_decreasing = sum(r["decreasing_dimensions"] for r in reports)
    print()
    print("=== 汇总")
    print(f"  零件 {len(reports)} 个, 工序步 {sum(len(r['steps']) for r in reports)} 道")
    print(f"  尺寸标注 {total_dims} 个, 其中 measurement 与几何跨度一致 {total_agree} "
          f"({total_agree / max(1, total_dims) * 100:.1f}%)")
    print(f"  跨步可配对的尺寸 {total_cross} 个, 其中值沿路线单调递减 {total_decreasing} 个")

    output = BASE / "chainmodel" / "runs" / "geometry-audit"
    output.mkdir(parents=True, exist_ok=True)
    (output / "faxin_corpus.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告: {output / 'faxin_corpus.json'}")


if __name__ == "__main__":
    main()
