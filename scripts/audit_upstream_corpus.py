"""Survey the upstream corpus: how many drawings, operations, and entity variants?

The upstream `temp/modifier` trees hold per-operation working JSON for many
more drawings than the four in the workspace copy. This counts them and
measures the entity variety, which decides whether a drawing-level (L3)
representation is learnable or whether we should stay at the part level (L1).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

CARD = re.compile(r"^(?P<drawing>.+?)_(?P<number>\d+)_(?P<operation>.+)\.json$")

ROOTS = [
    Path(r"E:\AAhz\20260909\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\20260917\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\20260918\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\NOVbcj\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\LXY\smartCAD\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\smartCAD715\CAD_Processwxf\CAD_Process_Hub\temp\modifier"),
    Path(r"E:\AAhz\Amix\mix717\CAD_Process_Hublh\temp\modifier"),
]

drawings = defaultdict(set)
operations = Counter()
entity_types = Counter()
with_mogong = 0
with_conditional = 0
total = 0
errors = 0
entity_counts = []

for root in ROOTS:
    if not root.exists():
        continue
    for path in root.rglob("*.json"):
        if path.name.endswith(".backup"):
            continue
        match = CARD.match(path.name)
        if not match:
            continue
        total += 1
        drawings[match.group("drawing")].add(match.group("number"))
        operations[match.group("operation")] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            errors += 1
            continue
        entities = data.get("entities") or []
        entity_counts.append(len(entities))
        for entity in entities:
            entity_types[entity.get("type", "?")] += 1
        if data.get("mogong_patch"):
            with_mogong += 1
            for body in (data["mogong_patch"] or {}).values():
                if isinstance(body, dict) and body.get("conditional_sdc"):
                    with_conditional += 1
                    break

print(f"工序卡文件: {total}  (解析失败 {errors})")
print(f"不同图纸: {len(drawings)}")
print(f"不同工序名: {len(operations)}")
print(f"带 mogong_patch 的: {with_mogong}")
print(f"带 conditional_sdc 的: {with_conditional}")
print()
print("=== 实体类型总分布(全部工序卡合并)")
for name, count in entity_types.most_common(25):
    print(f"  {name:<16s} {count:>8d}")
print()
if entity_counts:
    entity_counts.sort()
    print(f"每卡实体数: 中位 {entity_counts[len(entity_counts)//2]}, "
          f"最小 {entity_counts[0]}, 最大 {entity_counts[-1]}")
print()
print("=== 工序名频次(前 30)")
for name, count in operations.most_common(30):
    print(f"  {count:>5d}  {name}")
print()
print("=== 图纸清单(前 25)")
for drawing in sorted(drawings)[:25]:
    print(f"  {drawing:<34s} {len(drawings[drawing])} 道工序")
print(f"  ... 共 {len(drawings)} 张")
