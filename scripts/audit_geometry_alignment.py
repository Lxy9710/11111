"""S0 audit: how far do our process chains line up with the upstream geometry?

The upstream `mogong` subsystem writes a working JSON per operation, and each
one carries the full entity set of that operation's drawing. If our target
chains can be matched to those working files, then the geometry states exist
upstream and a replay kernel has something to reproduce.

This script answers three questions:

1. How many of our cases have an upstream working JSON to compare against?
2. Do the operation sequences line up, and how far do they diverge?
3. Does the upstream geometry carry the information a legality check needs
   (layer, group, handle, explicit geometry)?

It is an audit, not a conversion. The output is a report.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chainmodel" / "src"))

DATASET = ROOT / "chainmodel" / "data" / "processed" / "combined-batch-v1"
UPSTREAM = ROOT / "NOVbcj" / "smartCAD" / "CAD_Process_Hub" / "temp" / "modifier"

# The upstream file name is `<drawing number>_<process no>_<process name>.json`.
CARD_PATTERN = re.compile(r"^(?P<drawing>.+?)_(?P<number>\d+)_(?P<operation>.+)\.json$")


def load_cases():
    manifest = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["cases"]:
        case_dir = DATASET / Path(entry["design"]).parent
        target = json.loads((case_dir / "target.json").read_text(encoding="utf-8"))
        yield entry, case_dir, target


def operations_of(target: dict) -> list[tuple[str, str]]:
    """The (process number, operation name) pairs of a target, in order.

    Operation events are identified as `<case id>:operation:<process number>`,
    so the number is the last colon-separated field. The upstream file names
    use the same process numbers, which is what makes the two comparable.
    """

    result = []
    for event in target["events"]:
        if event["action"] != "OP":
            continue
        parts = event["id"].rsplit(":", 1)
        number = parts[1] if len(parts) == 2 else ""
        result.append((number, event["operation"]))
    return result


def index_upstream() -> dict[str, list[dict]]:
    """Group the upstream working JSON by drawing number."""

    index: dict[str, list[dict]] = defaultdict(list)
    if not UPSTREAM.exists():
        return index
    for path in UPSTREAM.rglob("*.json"):
        if path.name.endswith(".backup"):
            continue
        match = CARD_PATTERN.match(path.name)
        if not match:
            continue
        index[match.group("drawing")].append({
            "path": path,
            "number": match.group("number"),
            "operation": match.group("operation"),
        })
    return index


def entity_capability(card: dict) -> dict:
    """What the upstream geometry can support.

    A legality check needs to know which entity is which, where it is, and
    which group it belongs to. This reports whether that is present.
    """

    entities = card.get("entities") or []
    capabilities = Counter()
    for entity in entities:
        if entity.get("handle"):
            capabilities["has_handle"] += 1
        if entity.get("layer"):
            capabilities["has_layer"] += 1
        if entity.get("group"):
            capabilities["has_group"] += 1
        if entity.get("attributes"):
            capabilities["has_attributes"] += 1
        if entity.get("insertion_point") is not None or entity.get("start") is not None:
            capabilities["has_point_geometry"] += 1
        if entity.get("radius") is not None:
            capabilities["has_radius"] += 1
    types = Counter(e.get("type", "?") for e in entities)
    return {
        "entity_count": len(entities),
        "capabilities": dict(capabilities),
        "top_types": types.most_common(8),
    }


def main() -> None:
    upstream = index_upstream()
    print(f"上游 working JSON: {sum(len(v) for v in upstream.values())} 个, "
          f"覆盖 {len(upstream)} 张图纸")
    print()

    report = []
    for entry, case_dir, target in load_cases():
        drawing = entry["case_id"]
        ours = operations_of(target)
        theirs = sorted(upstream.get(drawing, []), key=lambda c: int(c["number"]))
        record = {
            "case_id": drawing,
            "our_operations": len(ours),
            "upstream_operations": len(theirs),
            "matched": 0,
            "upstream_available": bool(theirs),
        }
        if theirs:
            ours_by_number = {number: name for number, name in ours}
            for card in theirs:
                if ours_by_number.get(card["number"]) == card["operation"]:
                    record["matched"] += 1
        report.append(record)

    available = [r for r in report if r["upstream_available"]]
    print(f"我们的案例: {len(report)}")
    print(f"有上游几何可比: {len(available)}")
    if available:
        total_ours = sum(r["our_operations"] for r in available)
        total_matched = sum(r["matched"] for r in available)
        print(f"这些案例的工序总数: {total_ours}, 工序号+名称都对上: {total_matched}")
        print()
        print(f"{'case_id':26s} {'我们':>5s} {'上游':>5s} {'对上':>5s}")
        for r in sorted(available, key=lambda x: -x["our_operations"])[:15]:
            print(f"{r['case_id'][:26]:26s} {r['our_operations']:5d} {r['upstream_operations']:5d} {r['matched']:5d}")

    print()
    print("=== 上游几何携带了什么")
    sample = None
    for cards in upstream.values():
        for card in cards:
            try:
                data = json.loads(card["path"].read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if data.get("entities"):
                sample = (card, data)
                break
        if sample:
            break
    if sample:
        card, data = sample
        print(f"样本: {card['path'].name}")
        capability = entity_capability(data)
        print(json.dumps(capability, ensure_ascii=False, indent=2))
        print()
        print("每个工序的 blocks:")
        for name, block in (data.get("blocks") or {}).items():
            print(f"  {name}: {len(block.get('entities') or [])} 实体")
    else:
        print("(没找到可解析的样本)")

    output = ROOT / "chainmodel" / "runs" / "geometry-audit"
    output.mkdir(parents=True, exist_ok=True)
    (output / "s0_alignment.json").write_text(
        json.dumps({"upstream_drawings": len(upstream), "cases": report},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {output / 's0_alignment.json'}")


if __name__ == "__main__":
    main()
