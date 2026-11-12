from __future__ import annotations

import unittest

from farmcad.data.route_source import (
    SOURCE_EXCEL_EXACT,
    SOURCE_TEMPLATE_CATEGORY,
    RouteResolver,
)
from farmcad.data.route_templates import (
    CATEGORY_CAGE,
    CATEGORY_INNER,
    CATEGORY_OUTER,
    FeatureSignature,
    RouteTemplateError,
    generate_route,
    load_templates,
    signature_from_design,
)


def full_signature(category: str) -> FeatureSignature:
    return FeatureSignature(
        category=category,
        groove=True,
        od=True,
        id=True,
        plane=True,
        lapping=True,
        regrind=True,
        chamfer=True,
        temper="中温补充回火",
        preassembly_check=True,
    )


class RouteTemplateTests(unittest.TestCase):
    def setUp(self):
        self.templates = load_templates()

    def test_asset_shape(self):
        self.assertEqual(self.templates["schema_version"], "farmcad-route-templates-v1")
        self.assertEqual(self.templates["stage_order"], ["M", "R", "F", "S", "R2", "P", "X"])
        for key in ("outer_ring", "inner_ring", "cage", "rolling_element"):
            self.assertIn(key, self.templates["category_profiles"])

    def test_generated_route_covers_present_features(self):
        for category in (CATEGORY_OUTER, CATEGORY_INNER):
            route = generate_route(full_signature(category), self.templates)
            names = [row["process_name_only"] for row in route.rows]
            self.assertTrue(names)
            self.assertTrue(any("沟" in name for name in names))
            self.assertTrue(any("平面" in name for name in names))
            self.assertTrue(any("退磁" in name for name in names))

    def test_process_numbers_are_unique_and_ordered(self):
        route = generate_route(full_signature(CATEGORY_OUTER), self.templates)
        numbers = [row["process_no"] for row in route.rows]
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual(numbers, sorted(numbers, key=int))

    def test_absent_feature_is_not_generated(self):
        signature = FeatureSignature(category=CATEGORY_OUTER, groove=True, od=True)
        route = generate_route(signature, self.templates)
        names = [row["process_name_only"] for row in route.rows]
        self.assertFalse(any("平面" in name for name in names))
        self.assertFalse(any("圈内径" in name for name in names))

    def test_no_feature_raises_instead_of_empty_route(self):
        signature = FeatureSignature(category=CATEGORY_OUTER)
        with self.assertRaises(RouteTemplateError):
            generate_route(signature, self.templates)

    def test_generated_route_is_not_human_verified(self):
        route = generate_route(full_signature(CATEGORY_OUTER), self.templates)
        self.assertEqual(
            route.provenance["quality_status"], "system_derived_not_human_verified"
        )

    def test_fixed_route_categories(self):
        for category in (CATEGORY_CAGE,):
            route = generate_route(FeatureSignature(category=category), self.templates)
            self.assertTrue(route.rows)
            self.assertEqual(route.provenance["method"], "fixed_category_route")

    def test_signature_from_design_reads_parameter_keys(self):
        signature = signature_from_design(
            CATEGORY_OUTER,
            {"D": {"nominal": 90.0}, "De": {"nominal": 80.0}, "C": {"nominal": 3.0}},
            {"产品名称": "外圈"},
        )
        self.assertTrue(signature.od)
        self.assertTrue(signature.groove)
        self.assertTrue(signature.plane)
        self.assertFalse(signature.id)


class RouteResolverTests(unittest.TestCase):
    def setUp(self):
        self.templates = load_templates()

    def test_exact_lookup_wins(self):
        resolver = RouteResolver(
            templates=self.templates,
            excel_loader=lambda part: [
                {"process_no": "300", "process_name_only": "粗磨外径"}
            ],
        )
        resolved = resolver.resolve("D208N4Q.01", CATEGORY_OUTER, full_signature(CATEGORY_OUTER))
        self.assertEqual(resolved.source, SOURCE_EXCEL_EXACT)
        self.assertFalse(resolved.is_human_verified)

    def test_fallback_to_category_template(self):
        resolver = RouteResolver(
            templates=self.templates,
            excel_loader=lambda part: [],
        )
        resolved = resolver.resolve("C110K1QT2U(H).01", CATEGORY_OUTER, full_signature(CATEGORY_OUTER))
        self.assertEqual(resolved.source, SOURCE_TEMPLATE_CATEGORY)
        self.assertTrue(resolved.rows)
        self.assertTrue(any("回退" in w for w in resolved.warnings))
        self.assertFalse(resolved.is_human_verified)

    def test_no_route_and_no_signature_raises(self):
        resolver = RouteResolver(templates=self.templates, excel_loader=lambda part: [])
        with self.assertRaises(RouteTemplateError):
            resolver.resolve("D209NW1U1(H).01", CATEGORY_OUTER, None)


if __name__ == "__main__":
    unittest.main()
