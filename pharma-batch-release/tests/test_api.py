"""HTTP API 冒烟测试：登录鉴权 + 放行阻断通过真实 HTTP 栈验证。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tests.base import EBRTestCase
from ebr.api.server import Handler
from ebr import service


def _request(base, method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class ApiTest(EBRTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        super().setUp()
        self.base = f"http://127.0.0.1:{self.port}"

    def _login(self, username):
        status, body = _request(self.base, "POST", "/api/auth/login",
                                body={"username": username, "password": "pw123456"})
        self.assertEqual(status, 200, body)
        return body["token"]

    def test_health_and_auth(self):
        status, body = _request(self.base, "GET", "/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))
        # 错误口令
        status, body = _request(self.base, "POST", "/api/auth/login",
                                body={"username": "qp", "password": "wrong"})
        self.assertEqual(status, 401)
        # 无 token 访问受保护接口
        status, _ = _request(self.base, "GET", "/api/products")
        self.assertEqual(status, 401)

    def test_full_cycle_via_http(self):
        tok_op, tok_lead = self._login("op"), self._login("lead")
        tok_qa, tok_qp = self._login("qa"), self._login("qp")
        tok_a1, tok_a2 = self._login("analyst"), self._login("analyst2")

        status, batch = _request(self.base, "POST", "/api/batches",
                                 token=tok_op,
                                 body={"product_code": "T-100", "planned_size": 1000,
                                       "batch_no": "API1"})
        self.assertEqual(status, 201, batch)

        _request(self.base, "POST", "/api/batches/API1/start", token=tok_op)
        for code, q in [("M1", 10.02), ("M2", 2.0)]:
            _request(self.base, "POST", "/api/batches/API1/dispensing", token=tok_op,
                     body={"material_code": code, "qty_actual": q})
            _request(self.base, "POST", "/api/batches/API1/dispensing/verify",
                     token=tok_lead, body={"material_code": code})
        _request(self.base, "POST", "/api/batches/API1/process-records",
                 token=tok_op, body={"step_no": 1, "param_name": "主压力",
                                     "actual_value": 20.0})
        for test, num, txt in [("含量", 99.5, None), ("性状", None, "白色片")]:
            _request(self.base, "POST", "/api/batches/API1/qc-results", token=tok_a1,
                     body={"test_name": test, "numeric_value": num, "text_value": txt})
            _request(self.base, "POST", "/api/batches/API1/qc-results/review",
                     token=tok_a2, body={"test_name": test})
        _request(self.base, "POST", "/api/batches/API1/complete", token=tok_lead,
                 body={"actual_size": 1000})

        # RBAC：操作员不能放行
        status, body = _request(self.base, "POST", "/api/batches/API1/release",
                                token=tok_op, body={"reason": "x"})
        self.assertEqual(status, 403)

        status, out = _request(self.base, "POST", "/api/batches/API1/release",
                               token=tok_qp, body={"reason": "API 端电子签名放行"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["status"], "RELEASED")

    def test_blocked_release_via_http(self):
        # 空批次直接流转到 PENDING_QA（缺所有记录），API 层面同样被 409 阻断
        tok_op, tok_lead, tok_qp = (self._login(x) for x in ("op", "lead", "qp"))
        _request(self.base, "POST", "/api/batches", token=tok_op,
                 body={"product_code": "T-100", "planned_size": 1000, "batch_no": "API2"})
        _request(self.base, "POST", "/api/batches/API2/start", token=tok_op)
        _request(self.base, "POST", "/api/batches/API2/complete", token=tok_lead,
                 body={"actual_size": 1000})
        status, body = _request(self.base, "POST", "/api/batches/API2/release",
                                token=tok_qp, body={"reason": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "RELEASE_BLOCKED")
        gate_ids = {g["id"] for g in body["details"]["gates"] if not g["passed"]}
        self.assertIn("BOM_COMPLETENESS", gate_ids)

    def test_audit_verify_endpoint(self):
        tok_qa = self._login("qa")
        status, body = _request(self.base, "POST", "/api/audit/verify", token=tok_qa)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
