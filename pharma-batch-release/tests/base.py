"""测试公共夹具：每个用例使用独立临时库 + 已播种的主数据与账号。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from ebr import masterdata, service  # noqa: E402
from ebr.audit import AuditTrail  # noqa: E402
from ebr.auth import hash_password  # noqa: E402
from ebr.db import connect, init_db  # noqa: E402
from ebr.models import utcnow_iso  # noqa: E402

PRODUCT = "T-100"

USER_DEFS = {
    "admin": ("管理员", "ADMIN"),
    "op": ("操作员", "OPERATOR"),
    "lead": ("生产负责人", "PRODUCTION_LEAD"),
    "analyst": ("化验员A", "ANALYST"),
    "analyst2": ("化验员B", "ANALYST"),
    "qa": ("QA", "QA"),
    "qp": ("受权放行人", "QP"),
}


class EBRTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_file = str(Path(self.tmpdir.name) / "test.db")
        os.environ["EBR_DB_PATH"] = self.db_file
        # ebr.config 在导入时读环境变量做默认值，但 connect 每次都重新读，故无需 patch
        init_db(self.db_file)
        self._seed()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    # ------------------------------------------------------------------

    def _seed(self) -> None:
        conn = connect(self.db_file)
        for uname, (display, role) in USER_DEFS.items():
            salt, digest = hash_password("pw123456")
            conn.execute(
                """INSERT INTO users (username, display_name, role, pwd_salt, pwd_hash,
                                      is_active, created_at)
                   VALUES (?,?,?,?,?,1,?)""",
                (uname, display, role, salt, digest, utcnow_iso()))
        for code, name, cat, unit in [
                ("M1", "原料药", "API", "kg"),
                ("M2", "辅料", "EXCIPIENT", "kg")]:
            conn.execute(
                """INSERT INTO materials (code, name, spec, category, unit, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (code, name, "std", cat, unit, utcnow_iso()))
        conn.execute(
            """INSERT INTO products
               (code, name, dosage_form, spec, batch_size, batch_size_unit,
                yield_lower_pct, yield_upper_pct, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (PRODUCT, "测试片", "片剂", "10mg", 1000, "片", 95.0, 105.0, utcnow_iso()))
        conn.commit()
        conn.close()

        actor = self.user("admin")
        masterdata.add_bom_item(actor=actor, product_code=PRODUCT, material_code="M1",
                                qty_required=10.0, tolerance_pct=2, sequence_no=1)
        masterdata.add_bom_item(actor=actor, product_code=PRODUCT, material_code="M2",
                                qty_required=2.0, tolerance_pct=5, sequence_no=2)
        masterdata.add_process_parameter(
            actor=actor, product_code=PRODUCT, step_no=1, step_name="压片",
            param_name="主压力", target=20, lower_limit=18, upper_limit=22,
            unit="kN", is_critical=True)
        masterdata.add_qc_spec(
            actor=actor, product_code=PRODUCT, test_name="含量", test_type="NUMERIC",
            lower_limit=95, upper_limit=105, unit="%", is_critical=True)
        masterdata.add_qc_spec(
            actor=actor, product_code=PRODUCT, test_name="性状", test_type="TEXT",
            expected_text="白色片", is_critical=False)

    def user(self, username: str) -> dict:
        conn = connect(self.db_file)
        try:
            return dict(conn.execute("SELECT * FROM users WHERE username = ?",
                                     (username,)).fetchone())
        finally:
            conn.close()

    # ---- 业务流水线快捷方法 ----

    def make_batch(self, batch_no: str = "B001", planned: float = 1000) -> dict:
        return service.create_batch(actor=self.user("op"), product_code=PRODUCT,
                                    planned_size=planned, batch_no=batch_no)

    def run_full_good_batch(self, batch_no: str = "B001", actual: float = 1000,
                            m1: float = 10.05, m2: float = 2.01,
                            pressure: float = 20.5, assay: float = 100.0):
        """走完整条合规流水线并报交，批次进入 PENDING_QA（不存在则先创建）。"""
        from ebr.errors import NotFoundError
        try:
            service.evaluate_batch(batch_no)
        except NotFoundError:
            self.make_batch(batch_no)
        op, lead, qa, a1, a2 = (self.user(x) for x in
                                ("op", "lead", "qa", "analyst", "analyst2"))
        status = service.evaluate_batch(batch_no)["status"]
        if status == "DRAFT":
            service.start_production(actor=op, batch_no=batch_no)
        service.dispense_material(actor=op, batch_no=batch_no,
                                  material_code="M1", qty_actual=m1)
        service.verify_dispensing(actor=lead, batch_no=batch_no, material_code="M1")
        service.dispense_material(actor=op, batch_no=batch_no,
                                  material_code="M2", qty_actual=m2)
        service.verify_dispensing(actor=qa, batch_no=batch_no, material_code="M2")
        service.record_process_value(actor=op, batch_no=batch_no, step_no=1,
                                     param_name="主压力", actual_value=pressure)
        service.record_qc_result(actor=a1, batch_no=batch_no, test_name="含量",
                                 numeric_value=assay)
        service.review_qc_result(actor=a2, batch_no=batch_no, test_name="含量")
        service.record_qc_result(actor=a1, batch_no=batch_no, test_name="性状",
                                 text_value="白色片")
        service.review_qc_result(actor=a2, batch_no=batch_no, test_name="性状")
        return service.complete_production(actor=lead, batch_no=batch_no,
                                           actual_size=actual)
