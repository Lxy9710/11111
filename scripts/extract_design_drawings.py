"""Extract the design drawings that have no JSON yet.

`设计图/geo/设计图/` holds 19 design drawings as DWG. Six of them already
have a matching FAXIN folder; the other 13 have no extracted JSON. Those 13
give us the *nominal* state of each part, which pairs with the FAXIN
process-step drawings to form "design drawing + cascaded modification".

The extraction reuses the upstream AutoCAD COM extractor, which is what
produced the FAXIN files, so the output schema is identical:

    NOVbcj/smartCAD/CAD_Process_Hub/src/TQ/dag_process/extract_from_dwg.py
        extract_single_dwg(acad, dwg_path, output_dir)

Usage:
    python scripts/extract_design_drawings.py --list
    python scripts/extract_design_drawings.py            # extract all missing
    python scripts/extract_design_drawings.py --only 2720A-02-04
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DESIGN_DIR = BASE / "设计图" / "geo" / "设计图"
FAXIN_DIR = BASE / "设计图" / "geo" / "FAXIN"
UPSTREAM = BASE / "NOVbcj" / "smartCAD" / "CAD_Process_Hub"


def add_upstream_to_path() -> None:
    """Make `src.TQ...` importable from the upstream checkout."""

    for candidate in (UPSTREAM, UPSTREAM.parent):
        if (candidate / "src").is_dir():
            sys.path.insert(0, str(candidate))
            return
    raise RuntimeError(f"找不到上游 CAD_Process_Hub: {UPSTREAM}")


def design_drawings() -> list[Path]:
    return sorted(DESIGN_DIR.glob("*.dwg"))


def faxin_parts() -> set[str]:
    if not FAXIN_DIR.exists():
        return set()
    return {p.name for p in FAXIN_DIR.iterdir() if p.is_dir()}


def plan() -> dict:
    """What would be extracted, and what is already covered."""

    parts = faxin_parts()
    items = []
    for dwg in design_drawings():
        items.append({
            "dwg": dwg.name,
            "part": dwg.stem,
            "has_faxin": dwg.stem in parts,
            "has_json": (DESIGN_DIR / f"{dwg.stem}.json").exists(),
        })
    return {
        "items": items,
        "total": len(items),
        "with_faxin": sum(1 for i in items if i["has_faxin"]),
        "missing_json": [i for i in items if not i["has_json"]],
    }


def extract(targets: list[Path], output_dir: Path) -> dict:
    """Run the upstream extractor over the given drawings."""

    # This is an offline batch script, not a business thread, so the CAD
    # scheduler gate is bypassed explicitly.
    os.environ.setdefault("CAD_SCHEDULER_ALLOW_DIRECT", "1")
    add_upstream_to_path()
    from src.TQ.dag_process.extract_from_dwg import (  # noqa: PLC0415
        connect_autocad,
        extract_single_dwg,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"连接 AutoCAD ...")
    acad = connect_autocad()
    print(f"AutoCAD {acad.Version} 已连接")

    results = {"success": [], "failed": [], "skipped": []}
    for index, dwg in enumerate(targets, 1):
        print(f"[{index}/{len(targets)}] {dwg.name}")
        start = time.time()
        try:
            status = extract_single_dwg(
                acad, str(dwg), str(output_dir), raise_on_error=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    失败: {type(exc).__name__}: {exc}")
            results["failed"].append({"dwg": dwg.name, "error": str(exc)})
            continue
        elapsed = round(time.time() - start, 1)
        print(f"    {status} ({elapsed}s)")
        results.setdefault(status, []).append(dwg.name)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="只列出计划, 不抽取")
    parser.add_argument("--only", action="append", default=None,
                        help="只抽取指定图号(可多次指定)")
    parser.add_argument("--include-faxin", action="store_true",
                        help="连已有 FAXIN 的 6 个零件也一起抽取")
    parser.add_argument("--output", type=Path, default=None,
                        help="输出目录, 默认写回 设计图/geo/设计图/")
    args = parser.parse_args()

    report = plan()
    if args.list:
        print(f"设计图 {report['total']} 个, 其中 {report['with_faxin']} 个已有 FAXIN 工序序列")
        print()
        print(f"{'图号':<20s} {'已有FAXIN':<10s} {'已有JSON':<9s}")
        for item in report["items"]:
            print(f"{item['part']:<20s} {'是' if item['has_faxin'] else '-':<10s} "
                  f"{'是' if item['has_json'] else '-':<9s}")
        return 0

    if args.only:
        wanted = set(args.only)
        targets = [p for p in design_drawings() if p.stem in wanted]
    elif args.include_faxin:
        targets = design_drawings()
    else:
        targets = [DESIGN_DIR / f"{item['dwg']}" for item in report["missing_json"]]

    if not targets:
        print("没有待抽取的图纸")
        return 0

    output = args.output or DESIGN_DIR
    print(f"待抽取 {len(targets)} 个 -> {output}")
    results = extract(targets, output)
    print()
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in results.items()}, ensure_ascii=False))
    return 0 if not results.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
