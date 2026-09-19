"""系统测试：服务层规则/审计 + 真实 HTTP 端到端（标准库 unittest）。

运行: python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services
from app.database import connect, init_db
from app.errors import Conflict, Forbidden, ValidationError
from app.server import make_handler

OP1 = {"id": 1, "username": "op01", "display_name": "王操作", "role": "operator"}
OP2 = {"id": 2, "username": "op02", "display_name": "李操作", "role": "operator"}
PL = {"id": 3, "username": "pl01", "display_name": "赵班长", "role": "production_lead"}
QC1 = {"id": 4, "username": "qc01", "display_name": "孙检验", "role": "qc"}
QC2 = {"id": 5, "username": "qc02", "display_name": "周复核", "role": "qc"}
QA = {"id": 6, "username": "qa01", "display_name": "钱质保", "role": "qa"}

WEIGH = [("M-API-001", 10.50), ("M-EXC-101", 5.20),
         ("M-EXC-102", 0.26), ("M-PKG-201", 12.00)]
PARAMS = [
    (20, "混合时间", 25.0), (20, "混合转速", 15.0),
    (30, "平均片重", 0.186), (30, "片重差异", 1.8), (30, "硬度", 75.0),
    (40, "密封性", True),
]
QC = [("性状", True), ("维生素C含量", 99.2), ("溶出度", 92.0),
      ("片重差异", 2.1), ("水分", 3.2), ("微生物限度", True)]


def drive_good_batch(db, batch_no="VC-T-01", qc_overrides=None,
                     weigh_overrides=None, yield_pct=99.2, submit=True,
                     do_checks=True):
    """构造一个走到待审核的批次，返回 batch_id。do_checks=False 时跳过双人复核。"""
    bid = services.create_batch(db, PL, batch_no=batch_no)["id"]
    services.start_weighing(db, bid, OP1)
    wl = weigh_overrides or WEIGH
    for code, qty in wl:
        r = services.record_weighing(db, bid, code, qty, OP1)
        if do_checks:
            services.check_weighing(db, bid, r["record"]["id"], OP2)
    for step_no in (10, 20, 30, 40):
        services.start_step(db, bid, step_no, OP1)
        for sn, name, val in PARAMS:
            if sn == step_no:
                services.record_param(db, bid, sn, name, val, OP1)
        services.finish_step(db, bid, step_no, PL)
    services.finish_production(db, bid, yield_pct, PL)
    services.start_qc(db, bid, QC1)
    ql = qc_overrides or QC
    for name, val in ql:
        r = services.record_qc(db, bid, name, val, QC1)
        if do_checks:
            services.check_qc(db, bid, r["record"]["id"], QC2)
    if submit:
        services.submit_for_review(db, bid, QC2)
    return bid


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = self.tmp.name
        for ext in ("", "-wal", "-shm"):
            p = self.db + ext
            if os.path.exists(p):
                os.remove(p)
        init_db(self.db)
        services.seed(self.db)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            p = self.db + ext
            if os.path.exists(p):
                os.remove(p)

    # ---- 正常放行 ----
    def test_01_happy_path_release(self):
        bid = drive_good_batch(self.db)
        review = services.review_status(self.db, bid)
        self.assertEqual(review["blockers"], [])
        self.assertTrue(review["can_release"])
        b = services.release_batch(self.db, bid, "记录完整，同意放行", "Qa@12345", QA)
        self.assertEqual(b["status"], "released")
        self.assertIn("qa01#", b["e_signature"])

    # ---- 双人复核约束 ----
    def test_02_weighing_requires_distinct_checker(self):
        bid = services.create_batch(self.db, PL, batch_no="VC-T-02")["id"]
        services.start_weighing(self.db, bid, OP1)
        r = services.record_weighing(self.db, bid, "M-API-001", 10.5, OP1)
        with self.assertRaises(ValidationError):
            services.check_weighing(self.db, bid, r["record"]["id"], OP1)  # 同一人
        services.check_weighing(self.db, bid, r["record"]["id"], OP2)      # 第二人 ✓

    # ---- 未复核不能提交 QA ----
    def test_03_incomplete_ebr_blocks_submission(self):
        bid = drive_good_batch(self.db, "VC-T-03", submit=False, do_checks=False)
        with self.assertRaises(Conflict) as ctx:
            services.submit_for_review(self.db, bid, QC1)
        self.assertIn("双人复核", str(ctx.exception))

    # ---- OOS 自动偏差 + 引擎阻断 ----
    def test_04_oos_blocks_release(self):
        bad = [("性状", True), ("维生素C含量", 90.0), ("溶出度", 92.0),
               ("片重差异", 2.1), ("水分", 3.2), ("微生物限度", True)]
        bid = drive_good_batch(self.db, "VC-T-04", qc_overrides=bad)
        review = services.review_status(self.db, bid)
        codes = {b["code"] for b in review["blockers"]}
        self.assertIn("QC_OOS", codes)
        self.assertFalse(review["can_release"])
        with self.assertRaises(Conflict):
            services.release_batch(self.db, bid, "强行放行", "Qa@12345", QA)

    # ---- 偏差 accepted 后解除阻断；rejected 永久阻断 ----
    def test_05_deviation_accepted_then_release(self):
        bad = QC.copy()
        bad[2] = ("溶出度", 74.0)   # <80 critical
        bid = drive_good_batch(self.db, "VC-T-05", qc_overrides=bad)
        dev = services.list_deviations(self.db, bid)[0]
        services.close_deviation(self.db, dev["id"], "accepted",
                                 "复测合格，调查接受，已执行 CAPA 培训", QA)
        self.assertEqual(services.review_status(self.db, bid)["blockers"], [])
        services.release_batch(self.db, bid, "偏差关闭，放行", "Qa@12345", QA)
        self.assertEqual(services.review_status(self.db, bid)["batch"]["status"], "released")

    def test_06_deviation_rejected_blocks_forever(self):
        bad = QC.copy()
        bad[2] = ("溶出度", 74.0)
        bid = drive_good_batch(self.db, "VC-T-06", qc_overrides=bad)
        dev = services.list_deviations(self.db, bid)[0]
        services.close_deviation(self.db, dev["id"], "rejected",
                                 "溶出严重不达标，判该批报废", QA)
        review = services.review_status(self.db, bid)
        self.assertTrue(any(b["code"] == "DEVIATION_REJECTED" for b in review["blockers"]))
        with self.assertRaises(Conflict):
            services.release_batch(self.db, bid, "尝试放行", "Qa@12345", QA)

    # ---- 角色控制 ----
    def test_07_rbac_operator_cannot_release(self):
        bid = drive_good_batch(self.db, "VC-T-07")
        with self.assertRaises(Forbidden):
            services.release_batch(self.db, bid, "操作工放行", "Op@12345", OP1)

    # ---- 电子签名口令校验 ----
    def test_08_wrong_esignature_password(self):
        bid = drive_good_batch(self.db, "VC-T-08")
        with self.assertRaises(ValidationError):
            services.release_batch(self.db, bid, "同意放行", "wrong-pass", QA)

    # ---- 已放行终态不可改 ----
    def test_09_terminal_state_transitions_empty(self):
        bid = drive_good_batch(self.db, "VC-T-09")
        services.release_batch(self.db, bid, "同意放行该批次", "Qa@12345", QA)
        with self.assertRaises(Conflict):
            services.record_param(self.db, bid, 20, "混合转速", 16.0, OP1)

    # ---- 审计链完整 + 防篡改 ----
    def test_10_audit_chain_tamper_detection(self):
        drive_good_batch(self.db, "VC-T-10")
        self.assertTrue(services.verify_chain(self.db)["ok"])

        # 直接改审计日志：触发器拒绝
        raw = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE audit_log SET reason='x' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM audit_log WHERE id=1")
        raw.close()

        # 模拟绕开触发器改业务记录值：record_hash 复算失败
        raw = sqlite3.connect(self.db)
        raw.execute("DROP TRIGGER trg_freeze_qc_upd")
        raw.execute("UPDATE qc_results SET numeric_value=120 WHERE test_name='维生素C含量'")
        raw.commit()
        raw.close()
        rep = services.full_integrity_report(self.db, 1)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(m["entity"] == "qc_result"
                            for m in rep["records"]["mismatches"]))

    # ---- 阻断放行也必须留痕 ----
    def test_11_blocked_release_is_audited(self):
        bad = QC.copy()
        bad[1] = ("维生素C含量", 88.0)
        bid = drive_good_batch(self.db, "VC-T-11", qc_overrides=bad)
        with self.assertRaises(Conflict):
            services.release_batch(self.db, bid, "强行放行试试", "Qa@12345", QA)
        logs = services.list_audit(self.db, bid)
        self.assertTrue(any(a["action"] == "release_blocked" for a in logs))

    # ---- 状态机：未结束生产不能进 QC ----
    def test_12_state_machine_illegal_transition(self):
        bid = services.create_batch(self.db, PL, batch_no="VC-T-12")["id"]
        with self.assertRaises(Conflict):
            services.start_qc(self.db, bid, QC1)

    # ---- 冻结期禁止补插记录（触发器级）----
    def test_12b_frozen_batch_rejects_insert(self):
        bid = drive_good_batch(self.db, "VC-T-12B")  # 已 pending_review
        with self.assertRaises(sqlite3.IntegrityError):
            raw = sqlite3.connect(self.db)
            try:
                raw.execute(
                    "INSERT INTO qc_results (batch_id,test_name,test_type,numeric_value,"
                    "pass_fail,result_conforms,tested_by,tested_at,record_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (bid, "伪造项目", "numeric", 100.0, None, 1,
                     QC1["id"], "2026-01-01T00:00:00Z", "x"))
                raw.commit()
            finally:
                raw.close()
        # 服务层同样拒绝
        with self.assertRaises(Conflict):
            services.record_qc(self.db, bid, "维生素C含量", 100.0, QC1)

    # ---- QA 退回补检：解冻 → 偏差处置 → 重新提交 → 放行，全程留痕 ----
    def test_12c_qa_return_for_retest_flow(self):
        bad = QC.copy()
        bad[2] = ("溶出度", 74.0)
        bid = drive_good_batch(self.db, "VC-T-12C", qc_overrides=bad)
        self.assertFalse(services.review_status(self.db, bid)["can_release"])
        services.return_for_retest(self.db, bid, "溶出度数据异常，退回复测", QA)
        self.assertEqual(services.review_status(self.db, bid)["batch"]["status"], "qc_sampling")
        # 非 QA 不能退回
        with self.assertRaises(Forbidden):
            services.return_for_retest(self.db, bid, "操作工退回", OP1)
        dev = services.list_deviations(self.db, bid)[0]
        services.close_deviation(self.db, dev["id"], "accepted",
                                 "复测三批均合格，调查确认为取样误差，CAPA：修订取样SOP", QA)
        services.submit_for_review(self.db, bid, QC2)
        self.assertEqual(services.review_status(self.db, bid)["blockers"], [])
        services.release_batch(self.db, bid, "复测合格，偏差闭环，同意放行", "Qa@12345", QA)
        logs = services.list_audit(self.db, bid)
        self.assertTrue(any(a["action"] == "batch_returned_retest" for a in logs))
        # 终态不可退回
        with self.assertRaises(Conflict):
            services.return_for_retest(self.db, bid, "已放行还想退回", QA)

    # ---- 收率超限自动偏差 ----
    def test_13_yield_out_of_window(self):
        bid = drive_good_batch(self.db, "VC-T-13", yield_pct=95.0)
        review = services.review_status(self.db, bid)
        self.assertTrue(any(b["code"] == "YIELD_OOS" for b in review["blockers"]))

    # ---- 定性参数 false 触发 critical ----
    def test_14_seal_fail_critical(self):
        params = [(s, n, False if n == "密封性" else v) for s, n, v in PARAMS]
        # 需要自定义 drive：PARAMS 是常量，这里直接手工构造
        bid = services.create_batch(self.db, PL, batch_no="VC-T-14")["id"]
        services.start_weighing(self.db, bid, OP1)
        for code, qty in WEIGH:
            r = services.record_weighing(self.db, bid, code, qty, OP1)
            services.check_weighing(self.db, bid, r["record"]["id"], OP2)
        for step_no in (10, 20, 30, 40):
            services.start_step(self.db, bid, step_no, OP1)
            for sn, name, val in params:
                if sn == step_no:
                    services.record_param(self.db, bid, sn, name, val, OP1)
            services.finish_step(self.db, bid, step_no, PL)
        services.finish_production(self.db, bid, 99.0, PL)
        services.start_qc(self.db, bid, QC1)
        for name, val in QC:
            r = services.record_qc(self.db, bid, name, val, QC1)
            services.check_qc(self.db, bid, r["record"]["id"], QC2)
        services.submit_for_review(self.db, bid, QC2)
        review = services.review_status(self.db, bid)
        self.assertTrue(any(b["code"] == "PARAM_OOS" and b["severity"] == "critical"
                            for b in review["blockers"]))


class HttpEndToEndTests(unittest.TestCase):
    """真实起 HTTP 服务跑端到端（含未认证 401、登录、完整放行闭环）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        for ext in ("", "-wal", "-shm"):
            p = cls.tmp.name + ext
            if os.path.exists(p):
                os.remove(p)
        cls.db = cls.tmp.name
        init_db(cls.db)
        services.seed(cls.db)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for ext in ("", "-wal", "-shm"):
            p = cls.tmp.name + ext
            if os.path.exists(p):
                os.remove(p)

    def call(self, method, path, token=None, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def login(self, username, password):
        st, data = self.call("POST", "/api/auth/login",
                             body={"username": username, "password": password})
        self.assertEqual(st, 200)
        return data["token"]

    def test_e2e_full_flow(self):
        # 未认证 → 401
        st, _ = self.call("GET", "/api/batches")
        self.assertEqual(st, 401)
        # 错误密码 → 401
        st, _ = self.call("POST", "/api/auth/login",
                          body={"username": "qa01", "password": "bad"})
        self.assertEqual(st, 401)

        tpl, top2 = self.login("pl01", "Pl@12345"), self.login("op02", "Op@12345")
        tq1, tq2 = self.login("qc01", "Qc@12345"), self.login("qc02", "Qc@12345")
        tqa = self.login("qa01", "Qa@12345")

        st, b = self.call("POST", "/api/batches", tpl, {"batch_no": "VC-E2E-01"})
        self.assertEqual(st, 201)
        bid = b["id"]

        self.call("POST", f"/api/batches/{bid}/start-weighing", tpl)
        for code, qty in WEIGH:
            st, r = self.call("POST", f"/api/batches/{bid}/weighing", tpl,
                              {"material_code": code, "actual_qty": qty})
            self.assertEqual(st, 201)
            st, _ = self.call("POST",
                              f"/api/batches/{bid}/weighing/{r['record']['id']}/check", top2)
            self.assertEqual(st, 200)
        for step_no in (10, 20, 30, 40):
            self.call("POST", f"/api/batches/{bid}/steps/start", tpl, {"step_no": step_no})
            for sn, name, val in PARAMS:
                if sn == step_no:
                    st, r = self.call("POST", f"/api/batches/{bid}/params", tpl,
                                      {"step_no": sn, "param_name": name, "value": val})
                    self.assertEqual(st, 201, r)
            self.call("POST", f"/api/batches/{bid}/steps/finish", tpl, {"step_no": step_no})
        st, _ = self.call("POST", f"/api/batches/{bid}/finish-production", tpl,
                          {"actual_yield_pct": 99.3})
        self.assertEqual(st, 200)
        self.call("POST", f"/api/batches/{bid}/qc/start", tq1)
        for name, val in QC:
            st, r = self.call("POST", f"/api/batches/{bid}/qc", tq1,
                              {"test_name": name, "value": val})
            self.call("POST", f"/api/batches/{bid}/qc/{r['record']['id']}/check", tq2)
        st, _ = self.call("POST", f"/api/batches/{bid}/submit", tq2)
        self.assertEqual(st, 200)

        st, review = self.call("GET", f"/api/batches/{bid}/review", tqa)
        self.assertTrue(review["can_release"])
        st, b = self.call("POST", f"/api/batches/{bid}/release", tqa,
                          {"comment": "端到端测试放行", "password": "Qa@12345"})
        self.assertEqual(st, 200)
        self.assertEqual(b["status"], "released")

        # 审计链验证
        st, rep = self.call("GET", f"/api/batches/{bid}/integrity", tqa)
        self.assertTrue(rep["chain"]["ok"] and rep["records"]["ok"])

        # EBR 聚合读取
        st, ebr = self.call("GET", f"/api/batches/{bid}", tqa)
        self.assertEqual(len(ebr["weighing"]), 4)
        self.assertEqual(len(ebr["qc_results"]), 6)

        # RBAC：操作工不能关偏差以外——这里验证操作工无法调放行（用异常批）
        st, b2 = self.call("POST", "/api/batches", tpl, {"batch_no": "VC-E2E-02"})
        st, err = self.call("POST", f"/api/batches/{b2['id']}/release", tpl,
                            {"comment": "越权", "password": "Pl@12345"})
        self.assertEqual(st, 403)

    def test_e2e_blocked_batch(self):
        tpl = self.login("pl01", "Pl@12345")
        top2 = self.login("op02", "Op@12345")
        tq1, tq2 = self.login("qc01", "Qc@12345"), self.login("qc02", "Qc@12345")
        tqa = self.login("qa01", "Qa@12345")
        st, b = self.call("POST", "/api/batches", tpl, {"batch_no": "VC-E2E-03"})
        bid = b["id"]
        bad_weigh = [("M-API-001", 10.98)] + WEIGH[1:]
        self.call("POST", f"/api/batches/{bid}/start-weighing", tpl)
        for code, qty in bad_weigh:
            _, r = self.call("POST", f"/api/batches/{bid}/weighing", tpl,
                             {"material_code": code, "actual_qty": qty})
            self.call("POST", f"/api/batches/{bid}/weighing/{r['record']['id']}/check", top2)
        for step_no in (10, 20, 30, 40):
            self.call("POST", f"/api/batches/{bid}/steps/start", tpl, {"step_no": step_no})
            for sn, name, val in PARAMS:
                if sn == step_no:
                    self.call("POST", f"/api/batches/{bid}/params", tpl,
                              {"step_no": sn, "param_name": name, "value": val})
            self.call("POST", f"/api/batches/{bid}/steps/finish", tpl, {"step_no": step_no})
        self.call("POST", f"/api/batches/{bid}/finish-production", tpl,
                  {"actual_yield_pct": 98.8})
        self.call("POST", f"/api/batches/{bid}/qc/start", tq1)
        bad_qc = QC.copy()
        bad_qc[1] = ("维生素C含量", 91.0)
        for name, val in bad_qc:
            _, r = self.call("POST", f"/api/batches/{bid}/qc", tq1,
                             {"test_name": name, "value": val})
            self.call("POST", f"/api/batches/{bid}/qc/{r['record']['id']}/check", tq2)
        self.call("POST", f"/api/batches/{bid}/submit", tq2)

        # 放行被硬阻断 409
        st, err = self.call("POST", f"/api/batches/{bid}/release", tqa,
                            {"comment": "QA意见：申请放行", "password": "Qa@12345"})
        self.assertEqual(st, 409)
        self.assertIn("阻断", err["message"])

        # 拒绝放行成功
        st, b = self.call("POST", f"/api/batches/{bid}/reject", tqa,
                          {"reason": "OOS 未关闭，拒绝该批放行"})
        self.assertEqual(st, 200)
        self.assertEqual(b["status"], "rejected")

        # 阻断动作留痕
        st, logs = self.call("GET", "/api/audit", tqa)
        actions = {a["action"] for a in logs if a["batch_id"] == bid}
        self.assertIn("release_blocked", actions)
        self.assertIn("batch_rejected", actions)


if __name__ == "__main__":
    unittest.main()
