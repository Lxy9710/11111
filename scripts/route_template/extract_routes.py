"""抽取全部工序选择表的路线数据，作为路线模板学习与回测的唯一输入。

产出 `routes_raw.json`：图号 -> [(工序号, 工序名称, 部位名称, 工序类型), ...]。
这是 `build_route_templates.py` 与 `backtest_route_templates.py` 的共同上游，
保证模板的证据来源可追溯到具体的 Excel 文件与 SHA256。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "NOVbcj" / "smartCAD" / "CAD_Process_Hub"

# 路线表来源：(标签, 相对路径)
ROUTE_TABLES = (
    ("TQ1", "dataset/TQ/excel/工序选择1.xlsx"),
    ("TQ_TEST", "dataset/TQ/excel/工序选择（测试流程完整版）.xlsx"),
    ("TQ_MAIN", "dataset/TQ/excel/工序选择.xlsx"),
    ("BJ", "dataset/BJ/excel/工序选择.xlsx"),
    ("GZ", "dataset/excel/工序选择GZ.xlsx"),
)

# 项目外的历史部署表（不在工作区内，按需启用）
EXTRA_TABLES = (
    ("MIX717", Path(r"E:/AAhz/Amix/mix717/CAD_Process_Hublh/dataset/excel/工序选择.xlsx")),
)

PART_COLUMN = "部件型号（图号）"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def categorize(rows: List[Dict[str, Any]]) -> str:
    """按部位名称与工序名称判定零件类别。"""

    for row in rows:
        text = str(row.get("part_region")) + str(row.get("process_name")) + str(row.get("process_type"))
        if "保持架" in text or "保持器" in text:
            return "cage"
        if "滚动体" in text or "钢球" in text:
            return "rolling"
        if "内圈" in text:
            return "inner"
        if "外圈" in text:
            return "outer"
    return "unknown"


def extract(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    frame = pd.read_excel(path)
    frame.columns = [str(c).strip() for c in frame.columns]
    if PART_COLUMN not in frame.columns:
        raise ValueError(f"路线表缺少图号列：{path}")
    result: Dict[str, List[Dict[str, Any]]] = {}
    for part, group in frame.groupby(frame[PART_COLUMN].astype(str).str.strip()):
        if part in ("nan", ""):
            continue
        rows: List[Dict[str, Any]] = []
        for _, record in group.iterrows():
            try:
                process_no = int(float(record.get("工序号")))
            except (TypeError, ValueError):
                continue
            rows.append({
                "process_no": process_no,
                "process_name": str(record.get("工序名称")).strip(),
                "part_region": str(record.get("部位名称")).strip(),
                "process_type": str(record.get("工序类型")).strip(),
            })
        if rows:
            result[part] = rows
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-external", action="store_true",
                        help="包含 E:/AAhz 下的历史部署路线表")
    args = parser.parse_args()

    tables: List[tuple] = [(tag, PROJECT / rel) for tag, rel in ROUTE_TABLES]
    if args.include_external:
        tables.extend(EXTRA_TABLES)

    merged: Dict[str, List[Dict[str, Any]]] = {}
    sources = []
    for tag, path in tables:
        if not path.exists():
            sources.append({"tag": tag, "path": str(path), "status": "missing"})
            continue
        part_rows = extract(path)
        sources.append({
            "tag": tag,
            "path": str(path),
            "sha256": digest(path),
            "parts": len(part_rows),
            "rows": sum(len(v) for v in part_rows.values()),
            "status": "loaded",
        })
        for part, rows in part_rows.items():
            # 同一图号在多张表出现时，保留工序号更多的一份（通常是更完整的版本）
            existing = merged.get(part)
            if existing is None or len(rows) > len(existing):
                merged[part] = rows

    payload = {
        "schema_version": "farmcad-routes-raw-v1",
        "sources": sources,
        "distinct_parts": len(merged),
        "total_rows": sum(len(v) for v in merged.values()),
        "category_counts": dict(Counter(categorize(v) for v in merged.values())),
        "routes": merged,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {output}")
    print(f"图号 {payload['distinct_parts']} / 路线行 {payload['total_rows']}")
    print(f"类别分布 {payload['category_counts']}")


if __name__ == "__main__":
    main()
