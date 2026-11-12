"""统一路线来源：按图号查 Excel，或按类别+特征签名生成默认路线。

新增的类别默认路线让「图纸里没有该图号」不再等于「没有路线」。
但两条来源的证据强度不同，必须在产物里显式区分，不能混为一谈。

来源等级（由强到弱）：
- `excel_exact`      活动/指定工序表中存在该图号的精确行
- `template_category` 由类别默认路线模板生成（system 级，未人工确认）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .route_templates import (
    CATEGORY_CAGE,
    CATEGORY_INNER,
    CATEGORY_OUTER,
    CATEGORY_ROLLING,
    FeatureSignature,
    RouteTemplateError,
    generate_route,
    load_templates,
)

SOURCE_EXCEL_EXACT = "excel_exact"
SOURCE_TEMPLATE_CATEGORY = "template_category"


@dataclass
class ResolvedRoute:
    """路线及其来源与证据强度。"""

    rows: List[Dict[str, Any]]
    source: str
    provenance: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_human_verified(self) -> bool:
        """模板路线永远不是人工确认的。"""

        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "rows": self.rows,
            "provenance": self.provenance,
            "warnings": self.warnings,
            "human_verified": self.is_human_verified,
        }


class RouteResolver:
    """按图号优先查表，查不到则回退到类别默认路线。"""

    def __init__(
        self,
        templates: Optional[Mapping[str, Any]] = None,
        excel_loader: Optional[Any] = None,
    ) -> None:
        self._templates = templates
        self._excel_loader = excel_loader

    def _load_templates(self) -> Mapping[str, Any]:
        if self._templates is None:
            self._templates = load_templates()
        return self._templates

    def resolve(
        self,
        part_number: str,
        category: str,
        signature: Optional[FeatureSignature] = None,
        prefer_template: bool = False,
    ) -> ResolvedRoute:
        """解析路线。

        `prefer_template=True` 时跳过 Excel 查表，直接用类别模板。
        查表命中时返回 `excel_exact`；两者都没有时抛出 `RouteTemplateError`。
        """

        warnings: List[str] = []

        if not prefer_template and self._excel_loader is not None:
            rows = self._excel_loader(part_number)
            if rows:
                return ResolvedRoute(
                    rows=list(rows),
                    source=SOURCE_EXCEL_EXACT,
                    provenance={
                        "method": "excel_exact_lookup",
                        "part_number": part_number,
                    },
                    warnings=warnings,
                )
            warnings.append(
                f"工序表中没有图号 {part_number} 的精确路线，已回退到类别默认路线模板"
            )

        if signature is None:
            raise RouteTemplateError(
                f"图号 {part_number} 无精确路线，且未提供特征签名，无法生成类别默认路线"
            )

        templates = self._load_templates()
        generated = generate_route(signature, templates)
        provenance = dict(generated.provenance)
        provenance["method"] = "category_template_fallback"
        provenance["part_number"] = part_number
        provenance["excel_exact_available"] = False
        return ResolvedRoute(
            rows=generated.rows,
            source=SOURCE_TEMPLATE_CATEGORY,
            provenance=provenance,
            warnings=warnings,
        )


__all__ = [
    "CATEGORY_CAGE",
    "CATEGORY_INNER",
    "CATEGORY_OUTER",
    "CATEGORY_ROLLING",
    "ResolvedRoute",
    "RouteResolver",
    "SOURCE_EXCEL_EXACT",
    "SOURCE_TEMPLATE_CATEGORY",
]
