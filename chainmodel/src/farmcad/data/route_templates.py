"""按零件类别 + 图纸特征签名生成默认工艺路线。

路线不依赖按图号查 Excel。上游 6 张工序选择表共 17417 行 / 645 个图号的
实测结论是：同一类别零件的路线由「每个加工特征的阶段阶梯」加「脊柱工序」
决定，工序词表是封闭的（外圈 58 / 内圈 56 个规范 token）。

已验证的量化结论（见 docs/route-template-analysis.md）：
- 槽位集合可预测：给定真实槽位集合，规范空间的 Jaccard = 1.000（词表封闭）。
- 顺序不可由单条固定规则完全确定：oracle 精确匹配仅 1%。
- 阶段主序 + 特征门控 + 脊柱按实测相对位插入，Jaccard 0.78(外圈) / 0.73(内圈)，
  高于"随机取同类别另一件"基线 0.657 / 0.621。

本模块只做路线结构推理，不产生任何参数值，也不声称路线已被人工确认。
生成结果的质量等级是 `system`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "data" / "route_templates" / "route_templates_v1.json"

CATEGORY_OUTER = "outer_ring"
CATEGORY_INNER = "inner_ring"
CATEGORY_CAGE = "cage"
CATEGORY_ROLLING = "rolling_element"

FEATURE_KEYS = ("GROOVE", "OD", "ID", "PLANE")

# 脊柱工序：与特征阶梯无关、按实测相对位置插入的工序
SPINE_OPS = (
    "磨工总图", "退磁", "退磁、清洗", "清洗", "中温补充回火", "低温补充回火",
    "打倒角", "去锐角", "去各部锐角", "粗光饰", "精光饰", "光饰", "酸洗", "除氢",
    "荧光磁粉探伤", "激光打字", "装前检查", "提交",
)


class RouteTemplateError(ValueError):
    """路线模板缺失，或特征签名无法映射到该类别。"""


@dataclass(frozen=True)
class FeatureSignature:
    """从设计图解析出的加工特征与工艺条件。

    布尔字段只表示「图纸上存在该特征/该条件」，不表示是否加工到终态。
    """

    category: str
    groove: bool = False
    od: bool = False
    id: bool = False
    plane: bool = False
    lapping: bool = False
    regrind: bool = False
    chamfer: bool = False
    pickling: bool = True
    dehydrogenation: bool = True
    magnetic_particle_inspection: bool = True
    laser_marking: bool = True
    preassembly_check: bool = False
    submit: bool = True
    temper: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def feature_set(self) -> set:
        return {key for key in FEATURE_KEYS if getattr(self, key.lower() if key == "ID" else key)}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "features": {
                "GROOVE": self.groove,
                "OD": self.od,
                "ID": self.id,
                "PLANE": self.plane,
            },
            "conditions": {
                "lapping": self.lapping,
                "regrind": self.regrind,
                "chamfer": self.chamfer,
                "pickling": self.pickling,
                "dehydrogenation": self.dehydrogenation,
                "magnetic_particle_inspection": self.magnetic_particle_inspection,
                "laser_marking": self.laser_marking,
                "preassembly_check": self.preassembly_check,
                "submit": self.submit,
                "temper": self.temper,
            },
            "evidence": dict(self.evidence),
        }


@dataclass
class GeneratedRoute:
    """生成结果：工序行 + 生成依据 + 质量声明。"""

    category: str
    rows: List[Dict[str, Any]]
    provenance: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"category": self.category, "rows": self.rows, "provenance": self.provenance}


def load_templates(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取路线模板资产；缺失时显式报错，不静默回退到按图号查表。"""

    target = Path(path or TEMPLATE_PATH)
    if not target.exists():
        raise RouteTemplateError(f"路线模板资产不存在：{target}")
    return json.loads(target.read_text(encoding="utf-8"))


def _row(
    process_no: str,
    process_type: str,
    process_name: str,
    part_region: str,
    origin: str,
) -> Dict[str, Any]:
    return {
        "process_no": process_no,
        "process_type": process_type,
        "process_name_only": process_name,
        "process_name": f"{part_region}.{process_no} {process_name}",
        "display_name": f"{part_region}.{process_no} {process_name}",
        "part_region": part_region,
        "route_origin": origin,
        "source_status": "system_derived_route_template",
    }


def _spine_selection(signature: FeatureSignature, profile: Mapping[str, Any]) -> List[str]:
    """按特征签名决定哪些脊柱工序出现。

    脊柱工序是否出现由实测频次门控：只在该类别中普遍出现的工序才默认入选，
    稀有工序（如磨工总图仅 13/207 件出现）需由图纸证据显式触发。
    """

    freq = profile.get("spine_frequency") or {}
    total = max(1, int(profile.get("sample_parts") or 1))

    def common(name: str, threshold: float = 0.30) -> bool:
        return freq.get(name, 0) / total >= threshold

    selected: List[str] = []
    if common("磨工总图"):
        selected.append("磨工总图")
    if signature.lapping:
        for name in ("粗光饰", "精光饰"):
            if name in freq:
                selected.append(name)
    if signature.chamfer and "打倒角" in freq:
        selected.append("打倒角")
    if signature.regrind:
        for name in ("去锐角", "去各部锐角"):
            if name in freq:
                selected.append(name)
    if signature.temper:
        selected.append(signature.temper)
    if signature.pickling and "酸洗" in freq:
        selected.append("酸洗")
    if signature.dehydrogenation and "除氢" in freq:
        selected.append("除氢")
    if signature.magnetic_particle_inspection and "荧光磁粉探伤" in freq:
        selected.append("荧光磁粉探伤")
    if signature.laser_marking and "激光打字" in freq:
        selected.append("激光打字")
    if signature.preassembly_check and "装前检查" in freq:
        selected.append("装前检查")
    if signature.submit and "提交" in freq:
        selected.append("提交")
    for name in ("退磁", "清洗"):
        if name in freq:
            selected.append(name)
    return selected


def generate_route(
    signature: FeatureSignature,
    templates: Optional[Mapping[str, Any]] = None,
) -> GeneratedRoute:
    """按特征签名生成默认路线。

    生成策略：
    1. 展开每个存在特征的阶段阶梯（阶段主序 M→R→F→S→R2→P→X）。
    2. 按实测相对位置把脊柱工序插入到对应阶段之间。
    3. 分配工序号槽位（磨加段 300-450，步长 5）。
    """

    templates = templates or load_templates()
    category = signature.category
    if category not in templates["category_profiles"]:
        raise RouteTemplateError(f"未知零件类别：{category}")
    profile = templates["category_profiles"][category]

    # 保持架与滚动体在现有资产中是完整固定路线，不做阶梯展开。
    if profile.get("fixed_route"):
        rows = [
            _row(no, typ, name, profile["label"], "fixed_category_route")
            for no, typ, name in profile["fixed_route"]
        ]
        return GeneratedRoute(
            category=category,
            rows=rows,
            provenance={
                "source": profile.get("source", "category_profile"),
                "method": "fixed_category_route",
                "feature_signature": signature.to_dict(),
                "quality_status": "system_derived_not_human_verified",
            },
        )

    stage_order: Sequence[str] = templates["stage_order"]
    ladders = profile["grind_feature_ladder"]
    spine_pos = profile["spine_relative_position"]

    present = [
        key for key in FEATURE_KEYS
        if getattr(signature, {"GROOVE": "groove", "OD": "od", "ID": "id", "PLANE": "plane"}[key])
    ]
    if not present:
        # 该件没有可识别的加工特征（如仅有备料段），模板无法生成路线。
        # 这是数据缺口，不是模板故障：调用方应回退到按图号查表或人工补录。
        raise RouteTemplateError(
            "特征签名未包含任何可加工特征，无法生成路线；"
            "该件应回退到按图号查表或人工补录，不可用空模板冒充"
        )

    # 1. 特征阶梯展开
    items: List[Tuple[float, str, str]] = []  # (排序锚, 工序名, 来源)
    used: Dict[str, List[str]] = {}
    for stage in stage_order:
        anchor = stage_order.index(stage) / max(1, len(stage_order) - 1)
        for key in present:
            ladder = ladders.get(key)
            if not ladder:
                continue
            stage_names = _names_for_stage(ladder, stage)
            for name in stage_names:
                items.append((anchor, name, f"feature_ladder:{key}:{stage}"))
            if stage_names:
                used.setdefault(key, []).extend(stage_names)

    # 2. 脊柱工序按实测相对位置插入
    spine_selected = _spine_selection(signature, profile)
    for name in spine_selected:
        anchor = spine_pos.get(name)
        if anchor is None:
            continue
        items.append((anchor, name, "conditional_spine"))

    items.sort(key=lambda item: (item[0], item[1]))

    part_region = profile["label"]
    rows = [
        _row("", "磨加", name, part_region, origin)
        for _, name, origin in items
    ]

    # 3. 工序号槽位分配
    numbers = _assign_slots(len(rows))
    for row, number in zip(rows, numbers):
        row["process_no"] = number

    return GeneratedRoute(
        category=category,
        rows=rows,
        provenance={
            "source": "route_templates_v1.json",
            "method": "stage_major_feature_gated_with_spine_anchors",
            "feature_signature": signature.to_dict(),
            "feature_ladders": used,
            "spine_ops": spine_selected,
            "quality_status": "system_derived_not_human_verified",
            "caveat": (
                "路线由类别默认模板生成，未经工程师确认。"
                "同阶段内不同特征的先后在实测中接近 50/50 随机，"
                "因此本模板不能保证工序的精确先后，只保证工序集合与阶段归属。"
            ),
        },
    )


def _names_for_stage(ladder: Mapping[str, Any], stage: str) -> List[str]:
    """取该特征在指定阶段的规范工序名。

    规范名取自该类别真实路线中该 token 的最高频写法，避免生成同义异名
    （如 `均粗磨平面` / `均磨平面`）而拉低与真实路线的匹配度。
    """

    stage_names = (ladder.get("stage_names") or {})
    name = stage_names.get(stage)
    if name:
        return [name]
    stages = ladder.get("stages") or []
    canonical = ladder.get("canonical") or []
    if stage in stages:
        index = stages.index(stage)
        if index < len(canonical):
            return [canonical[index]]
    return []


def _assign_slots(count: int) -> List[str]:
    """磨加段槽位：300-450，步长 5。"""

    return [str(300 + 5 * i) for i in range(count)]


def signature_from_design(
    category: str,
    design_parameters: Mapping[str, Any],
    field_map: Optional[Mapping[str, Any]] = None,
) -> FeatureSignature:
    """从上游抽取的设计参数推断特征签名。

    上游 `build_design_part_params` 返回的键是语义名（`groove_diameter`、
    `outer_diameter`、`inner_diameter`、`planar_dimension`…），不是符号名，
    所以这里按语义键判定；符号名（`De`/`D`/`D2`/`C`）作为兼容分支。

    判据是可否证的：参数键里出现了某特征的相关项，就认为图纸上存在该特征。
    判据来源与命中项都写进 evidence，便于复核。
    """

    field_map = field_map or {}
    keys = set(design_parameters.keys())
    texts = [str(v) for v in field_map.values() if isinstance(v, str)]

    def has(*needles: str) -> bool:
        return any(n in t for t in texts for n in needles)

    groove = bool(
        {"groove_diameter", "groove_position", "groove_radius", "De", "ae"} & keys
    ) or has("沟")
    od = bool({"outer_diameter", "D"} & keys) or has("外径")
    id_ = bool({"inner_diameter", "D2", "di", "d2"} & keys) or has("内径")
    plane = bool({"planar_dimension", "C", "B"} & keys) or has("平面")

    signature = FeatureSignature(
        category=category,
        groove=groove,
        od=od,
        id=id_,
        plane=plane,
        lapping=has("研"),
        regrind=has("修磨"),
        chamfer=bool({"outer_chamfer", "inner_chamfer"} & keys) or has("倒角"),
        pickling=True,
        dehydrogenation=True,
        magnetic_particle_inspection=True,
        laser_marking=True,
        preassembly_check=has("装前"),
        submit=True,
        temper=next((t for t in texts if "回火" in t), None),
        evidence={
            "groove": sorted(k for k in keys if "groove" in k or k in ("De", "ae")),
            "od": sorted(k for k in keys if "outer_diameter" in k or k == "D"),
            "id": sorted(k for k in keys if "inner_diameter" in k or k in ("D2", "di", "d2")),
            "plane": sorted(k for k in keys if "planar" in k or k in ("C", "B")),
            "chamfer": sorted(k for k in keys if "chamfer" in k),
            "source": "design_parameters semantic keys + field_map text",
        },
    )
    return signature


__all__ = [
    "CATEGORY_CAGE",
    "CATEGORY_INNER",
    "CATEGORY_OUTER",
    "CATEGORY_ROLLING",
    "FEATURE_KEYS",
    "FeatureSignature",
    "GeneratedRoute",
    "RouteTemplateError",
    "SPINE_OPS",
    "generate_route",
    "load_templates",
    "signature_from_design",
]
