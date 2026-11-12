"""Tests for the operation semantics that decide a parameter's role.

A parameter's role cannot be read off the parameter alone. The same physical
quantity is a machining target in the operation that produces it, a reference
copy in the operation that merely records it, and an inspection target in the
operation that measures it. These tests pin the classification, because a
wrong role is not a weak label but a wrong lesson.
"""

from __future__ import annotations

import unittest

from farmcad.data.prepare import CUTTING_OPERATIONS, operation_role, parameter_role


class OperationSemanticsTest(unittest.TestCase):

    def test_inspection_operations_measure_rather_than_cut(self) -> None:
        for operation in ("终检", "荧光磁粉探伤", "原材料检验", "锻造检验"):
            self.assertEqual(operation_role(operation, "Dc"), "inspection_target", operation)

    def test_auxiliary_operations_only_record_state(self) -> None:
        for operation in ("包装", "镀银", "光饰", "粗光饰", "精光饰", "去锐角",
                          "退磁(粗磨后)", "打标记"):
            self.assertEqual(operation_role(operation, "Dc"), "reference_copy", operation)

    def test_cutting_operations_produce_their_target(self) -> None:
        for operation in ("粗磨外径", "终磨外沟", "细车内径及倒角", "切断",
                          "均车两平面及倒角", "粗车第一平面及内径、倒角"):
            self.assertEqual(operation_role(operation, "Dc"), "machining_target", operation)

    def test_datum_operations_produce_their_hole(self) -> None:
        self.assertEqual(operation_role("钻铣孔", "兜孔尺寸"), "machining_target")
        self.assertEqual(operation_role("钻铆钉孔", "n"), "machining_target")

    def test_unknown_operation_yields_no_role(self) -> None:
        self.assertIsNone(operation_role("某道没见过的工序", "Dc"))
        self.assertIsNone(operation_role("", "Dc"))

    def test_every_cut_operation_resolves(self) -> None:
        for operation in CUTTING_OPERATIONS:
            self.assertIsNotNone(operation_role(operation, "Dc"), operation)


class ParameterRoleTest(unittest.TestCase):

    def test_copied_parameter_consumed_by_a_cutting_operation_is_a_datum(self) -> None:
        roles, basis = parameter_role("终磨外径", "C", "copy_from_other_process", True)
        self.assertEqual(roles, ["datum_use"])
        self.assertEqual(basis, "consumed_by_cutting_operation")

    def test_copied_parameter_consumed_by_a_non_cutting_operation_is_a_reference(self) -> None:
        roles, basis = parameter_role("终检", "D", "copy_from_other_process", True)
        self.assertEqual(roles, ["reference_copy"])
        self.assertEqual(basis, "consumed_by_non_cutting_operation")

    def test_external_source_consumed_by_a_cutting_operation_is_a_datum(self) -> None:
        roles, basis = parameter_role("均粗磨平面", "D", "external_source", True)
        self.assertEqual(roles, ["datum_use"])
        self.assertEqual(basis, "consumed_by_cutting_operation")

    def test_operation_semantics_outrank_the_feature_binding(self) -> None:
        roles, basis = parameter_role("终检", "Dc", "", True)
        self.assertEqual(roles, ["inspection_target"])
        self.assertEqual(basis, "operation_semantics")

    def test_feature_binding_is_only_a_fallback(self) -> None:
        roles, basis = parameter_role("某道没见过的工序", "Dc", "", True)
        self.assertEqual(roles, ["machining_target"])
        self.assertEqual(basis, "feature_binding_heuristic")

    def test_unbound_parameter_without_semantics_has_no_role(self) -> None:
        roles, basis = parameter_role("某道没见过的工序", "Dc", "", False)
        self.assertIsNone(roles)
        self.assertEqual(basis, "")


if __name__ == "__main__":
    unittest.main()
