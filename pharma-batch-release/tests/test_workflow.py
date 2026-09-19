"""批次状态机、职责分离与正常/异常放行流程测试。"""

from __future__ import annotations

from tests.base import EBRTestCase
from ebr import service
from ebr.errors import (
    ConflictError,
    PermissionDeniedError,
    ReleaseBlockedError,
    ValidationError,
)


class StateMachineTest(EBRTestCase):
    def test_legal_and_illegal_transitions(self):
        self.make_batch("S1")
        # DRAFT 不能直接报交/放行
        with self.assertRaises(ConflictError):
            service.complete_production(actor=self.user("lead"),
                                        batch_no="S1", actual_size=1000)
        with self.assertRaises(ConflictError):
            service.release_batch(actor=self.user("qp"), batch_no="S1", reason="x")
        service.start_production(actor=self.user("op"), batch_no="S1")
        # 化验员无权开工（另一批次上尝试）
        self.make_batch("S2")
        with self.assertRaises(PermissionDeniedError):
            service.start_production(actor=self.user("analyst"), batch_no="S2")
        # RELEASED 是终态（S1 补全合规记录后放行）
        self.run_full_good_batch("S1")
        service.release_batch(actor=self.user("qp"), batch_no="S1",
                              reason="合格放行")
        with self.assertRaises(ConflictError):
            service.reject_batch(actor=self.user("qa"), batch_no="S1", reason="x")
        with self.assertRaises(ConflictError):
            service.dispense_material(actor=self.user("op"), batch_no="S1",
                                      material_code="M1", qty_actual=10)

    def test_only_qp_can_release(self):
        self.run_full_good_batch("S2")
        for role in ("op", "lead", "analyst", "qa"):
            with self.assertRaises(PermissionDeniedError):
                service.release_batch(actor=self.user(role), batch_no="S2",
                                      reason="尝试")
        out = service.release_batch(actor=self.user("qp"), batch_no="S2",
                                    reason="同意放行")
        self.assertEqual(out["status"], "RELEASED")
        self.assertEqual(out["decision"]["decision"], "RELEASE")
        self.assertIn("同等法律效力", out["decision"]["meaning"])


class HappyPathTest(EBRTestCase):
    def test_good_batch_releases_with_all_gates_passed(self):
        self.run_full_good_batch("H1")
        ev = service.evaluate_batch("H1")
        self.assertTrue(ev["releasable"])
        self.assertEqual(ev["open_blocking_gates"], [])
        out = service.release_batch(actor=self.user("qp"), batch_no="H1",
                                    reason="电子签名放行")
        self.assertEqual(out["status"], "RELEASED")
        # 签名链自洽
        self.assertTrue(service.verify_signature_chain()["ok"])

    def test_release_requires_reason(self):
        self.run_full_good_batch("H2")
        with self.assertRaises(ValidationError):
            service.release_batch(actor=self.user("qp"), batch_no="H2", reason="  ")


class BlockingTest(EBRTestCase):
    def _pending_batch(self, no: str):
        self.make_batch(no)
        self.run_full_good_batch(no)

    def test_missing_dispensing_blocks(self):
        # 只投 M1，缺 M2
        self.make_batch("K1")
        op, lead = self.user("op"), self.user("lead")
        service.start_production(actor=op, batch_no="K1")
        service.dispense_material(actor=op, batch_no="K1",
                                  material_code="M1", qty_actual=10.0)
        service.verify_dispensing(actor=lead, batch_no="K1", material_code="M1")
        service.record_process_value(actor=op, batch_no="K1", step_no=1,
                                     param_name="主压力", actual_value=20.0)
        a1, a2 = self.user("analyst"), self.user("analyst2")
        service.record_qc_result(actor=a1, batch_no="K1", test_name="含量",
                                 numeric_value=100.0)
        service.review_qc_result(actor=a2, batch_no="K1", test_name="含量")
        service.record_qc_result(actor=a1, batch_no="K1", test_name="性状",
                                 text_value="白色片")
        service.review_qc_result(actor=a2, batch_no="K1", test_name="性状")
        service.complete_production(actor=lead, batch_no="K1", actual_size=1000)

        ev = service.evaluate_batch("K1")
        self.assertIn("BOM_COMPLETENESS", ev["open_blocking_gates"])
        with self.assertRaises(ReleaseBlockedError) as ctx:
            service.release_batch(actor=self.user("qp"), batch_no="K1",
                                  reason="试")
        self.assertIn("BOM_COMPLETENESS", ctx.exception.gates and
                      [g["id"] for g in ctx.exception.gates if not g["passed"]])

    def test_over_tolerance_dispensing_blocks(self):
        # M1 标准 10kg±2%，投 10.5kg 超差
        self.make_batch("K2")
        self.run_full_good_batch("K2", m1=10.5)
        self.assertIn("DISPENSING_TOLERANCE",
                      service.evaluate_batch("K2")["open_blocking_gates"])

    def test_unverified_dispensing_blocks(self):
        self.make_batch("K3")
        op, lead = self.user("op"), self.user("lead")
        service.start_production(actor=op, batch_no="K3")
        service.dispense_material(actor=op, batch_no="K3",
                                  material_code="M1", qty_actual=10.0)
        # 故意不复核 M1
        service.dispense_material(actor=op, batch_no="K3",
                                  material_code="M2", qty_actual=2.0)
        service.verify_dispensing(actor=lead, batch_no="K3", material_code="M2")
        service.record_process_value(actor=op, batch_no="K3", step_no=1,
                                     param_name="主压力", actual_value=20.0)
        a1, a2 = self.user("analyst"), self.user("analyst2")
        for t, n, t2 in [("含量", 100.0, None), ("性状", None, "白色片")]:
            service.record_qc_result(actor=a1, batch_no="K3", test_name=t,
                                     numeric_value=n, text_value=t2)
            service.review_qc_result(actor=a2, batch_no="K3", test_name=t)
        service.complete_production(actor=lead, batch_no="K3", actual_size=1000)
        gates = service.evaluate_batch("K3")["open_blocking_gates"]
        self.assertIn("DISPENSING_VERIFICATION", gates)

    def test_self_verification_rejected(self):
        self.make_batch("K4")
        service.start_production(actor=self.user("op"), batch_no="K4")
        service.dispense_material(actor=self.user("op"), batch_no="K4",
                                  material_code="M1", qty_actual=10.0)
        with self.assertRaises(PermissionDeniedError):
            service.verify_dispensing(actor=self.user("op"), batch_no="K4",
                                      material_code="M1")

        a1 = self.user("analyst")
        service.record_qc_result(actor=a1, batch_no="K4", test_name="含量",
                                 numeric_value=100.0)
        with self.assertRaises(PermissionDeniedError):
            service.review_qc_result(actor=a1, batch_no="K4", test_name="含量")

    def test_critical_process_oot_blocks_without_deviation(self):
        # 主压力 25 > 上限 22
        self.make_batch("K5")
        self.run_full_good_batch("K5", pressure=25.0)
        gates = service.evaluate_batch("K5")["open_blocking_gates"]
        self.assertIn("PROCESS_PARAMETERS", gates)

    def test_yield_out_of_range_blocks(self):
        # 收率 90% < 下限 95%
        self.make_batch("K6")
        self.run_full_good_batch("K6", actual=900)
        self.assertIn("YIELD", service.evaluate_batch("K6")["open_blocking_gates"])

    def test_oos_blocks_and_reject_works(self):
        self.make_batch("K7")
        self.run_full_good_batch("K7", assay=90.0)  # 含量 90 < 95 OOS
        ev = service.evaluate_batch("K7")
        self.assertIn("QC_RESULTS_CONFORM", ev["open_blocking_gates"])
        with self.assertRaises(ReleaseBlockedError):
            service.release_batch(actor=self.user("qp"), batch_no="K7", reason="x")
        out = service.reject_batch(actor=self.user("qa"), batch_no="K7",
                                   reason="含量 OOS，拒放")
        self.assertEqual(out["status"], "REJECTED")
        self.assertTrue(service.verify_signature_chain()["ok"])

    def test_unreviewed_qc_blocks(self):
        self.make_batch("K8")
        op, lead, a1 = self.user("op"), self.user("lead"), self.user("analyst")
        service.start_production(actor=op, batch_no="K8")
        for code, q in [("M1", 10.0), ("M2", 2.0)]:
            service.dispense_material(actor=op, batch_no="K8",
                                      material_code=code, qty_actual=q)
            service.verify_dispensing(actor=lead, batch_no="K8", material_code=code)
        service.record_process_value(actor=op, batch_no="K8", step_no=1,
                                     param_name="主压力", actual_value=20.0)
        service.record_qc_result(actor=a1, batch_no="K8", test_name="含量",
                                 numeric_value=100.0)
        # 不复核，也不做性状
        service.complete_production(actor=lead, batch_no="K8", actual_size=1000)
        gates = set(service.evaluate_batch("K8")["open_blocking_gates"])
        self.assertIn("QC_TESTS_COMPLETE", gates)
