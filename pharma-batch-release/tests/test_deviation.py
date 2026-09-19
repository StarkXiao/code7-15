"""偏差与 CAPA 门禁闭环测试（含化验室差错 OOS 作废重测路径）。"""

from __future__ import annotations

from tests.base import EBRTestCase
from ebr import service
from ebr.errors import ConflictError, ReleaseBlockedError


class ProcessOOTDeviationTest(EBRTestCase):
    def _batch_with_oot(self, no: str, category: str = "MAJOR"):
        self.make_batch(no)
        self.run_full_good_batch(no, pressure=24.0)  # CPP 超标
        ebr = service.get_ebr(no)
        rec = next(r for r in ebr["process_records"] if r["param_name"] == "主压力")
        qa = self.user("qa")
        service.reopen_for_correction(
            actor=qa, batch_no=no, reason="CPP 超标，立案偏差")
        dev = service.raise_deviation(
            actor=self.user("op"), batch_no=no, category=category,
            title="主压力超标", description="24kN 超出 18~22kN",
            source_type="PROCESS_OOT", source_ref=f"process_record:{rec['id']}")
        service.complete_production(actor=self.user("lead"), batch_no=no,
                                    actual_size=1000)
        return dev

    def test_open_and_rejected_deviation_blocks(self):
        dev = self._batch_with_oot("D1")
        gates = service.evaluate_batch("D1")["open_blocking_gates"]
        self.assertIn("DEVIATIONS", gates)
        self.assertIn("PROCESS_PARAMETERS", gates)  # 未有效关闭，OOT 未覆盖

        service.close_deviation(actor=self.user("qa"), dev_no=dev["dev_no"],
                                root_cause="调查不充分，退回重新调查",
                                root_cause_confirmed=False,
                                outcome="REJECTED")
        gates = service.evaluate_batch("D1")["open_blocking_gates"]
        self.assertIn("DEVIATIONS", gates)

    def test_minor_deviation_closed_effective_releases_without_capa(self):
        dev = self._batch_with_oot("D2", category="MINOR")
        service.close_deviation(actor=self.user("qa"), dev_no=dev["dev_no"],
                                root_cause="物料细度波动，评估无质量影响",
                                root_cause_confirmed=True)
        ev = service.evaluate_batch("D2")
        self.assertTrue(ev["releasable"], ev["open_blocking_gates"])
        out = service.release_batch(actor=self.user("qp"), batch_no="D2",
                                    reason="次要偏差已闭环，放行")
        self.assertEqual(out["status"], "RELEASED")

    def test_major_deviation_requires_effective_capa(self):
        dev = self._batch_with_oot("D3", category="MAJOR")
        service.close_deviation(actor=self.user("qa"), dev_no=dev["dev_no"],
                                root_cause="压片机加料器故障",
                                root_cause_confirmed=True)
        # 无 CAPA → 阻断
        self.assertIn("CAPA", service.evaluate_batch("D3")["open_blocking_gates"])

        capa = service.create_capa(
            actor=self.user("qa"), dev_no=dev["dev_no"],
            action="检修加料器并增加过程巡检", owner_username="lead",
            due_date="2026-10-01")
        service.implement_capa(actor=self.user("lead"), capa_id=capa["id"])
        # 验证无效 → 仍阻断
        service.verify_capa(actor=self.user("qa"), capa_id=capa["id"],
                            effective=False, note="巡检未落实")
        self.assertIn("CAPA", service.evaluate_batch("D3")["open_blocking_gates"])

        # CAPA 责任人不能验证自己的措施（职责分离）
        from ebr.errors import PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            service.verify_capa(actor=self.user("lead"), capa_id=capa["id"],
                                effective=True, note="自己验证自己")

        # 重做并验证有效 → 放行
        from ebr.db import connect
        conn = connect(self.db_file)
        conn.execute("UPDATE capas SET status='OPEN', closed_at=NULL WHERE id=?",
                     (capa["id"],))
        conn.commit()
        conn.close()
        service.implement_capa(actor=self.user("lead"), capa_id=capa["id"])
        service.verify_capa(actor=self.user("qa"), capa_id=capa["id"],
                            effective=True, note="巡检记录齐全，确认有效")
        ev = service.evaluate_batch("D3")
        self.assertTrue(ev["releasable"], ev["open_blocking_gates"])


class LabErrorRetestTest(EBRTestCase):
    def test_lab_error_oos_invalidated_then_retest_pass_releases(self):
        no = "D4"
        self.make_batch(no)
        self.run_full_good_batch(no, assay=90.0)  # 含量 OOS
        qa, a1, a2 = self.user("qa"), self.user("analyst"), self.user("analyst2")
        ebr = service.get_ebr(no)
        oos = next(r for r in ebr["qc_records"]
                   if r["test_name"] == "含量" and r["result"] == "OOS")
        self.assertIn("QC_RESULTS_CONFORM",
                      service.evaluate_batch(no)["open_blocking_gates"])

        service.reopen_for_correction(actor=qa, batch_no=no, reason="OOS 调查")
        dev = service.raise_deviation(
            actor=a1, batch_no=no, category="MINOR",
            title="含量测定 OOS（疑化验室差错）",
            description="HPLC 进样针漏液导致响应偏低",
            source_type="QC_OOS", source_ref=f"qc_record:{oos['id']}")
        closed = service.close_deviation(
            actor=qa, dev_no=dev["dev_no"],
            root_cause="进样针漏液，原始样品本身合格（复测储备液正常）",
            root_cause_confirmed=True, is_lab_error=True)
        self.assertEqual(closed["invalidated_qc_records"], [oos["id"]])

        # 作废后缺生效记录 → 完整性门禁提示重测
        self.assertIn("QC_TESTS_COMPLETE",
                      service.evaluate_batch(no)["open_blocking_gates"])

        # 重新取样检验，结果合格并复核
        service.record_qc_result(actor=a1, batch_no=no, test_name="含量",
                                 numeric_value=99.8)
        service.review_qc_result(actor=a2, batch_no=no, test_name="含量")
        service.complete_production(actor=self.user("lead"), batch_no=no,
                                    actual_size=1000)
        ev = service.evaluate_batch(no)
        self.assertTrue(ev["releasable"], ev["open_blocking_gates"])
        out = service.release_batch(actor=self.user("qp"), batch_no=no,
                                    reason="化验室差错已查明，重测合格，放行")
        self.assertEqual(out["status"], "RELEASED")

    def test_real_oos_can_only_be_rejected(self):
        no = "D5"
        self.make_batch(no)
        self.run_full_good_batch(no, assay=88.0)
        with self.assertRaises(ReleaseBlockedError):
            service.release_batch(actor=self.user("qp"), batch_no=no, reason="x")
        out = service.reject_batch(actor=self.user("qa"), batch_no=no,
                                   reason="真实 OOS，拒放销毁")
        self.assertEqual(out["status"], "REJECTED")
        # 终态不可再改
        with self.assertRaises(ConflictError):
            service.release_batch(actor=self.user("qp"), batch_no=no, reason="x")
