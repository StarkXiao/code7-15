"""零依赖 HTTP API（标准库 http.server）。

启动：``python -m ebr.api.server``（默认 :8080）。
鉴权：``Authorization: Bearer <token>``，token 由 /api/auth/login 获取。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import masterdata, service
from ..audit import AuditTrail
from ..auth import SessionStore, authenticate
from ..errors import AppError
from ..models import user_badge


class ApiContext:
    def __init__(self):
        self.sessions = SessionStore()
        self.audit = AuditTrail()


CTX = ApiContext()


class Handler(BaseHTTPRequestHandler):
    server_version = "EBR/1.0"
    ctx = CTX

    # ---------------------------------------------------------- 基础收发

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise AppError("请求体必须是 JSON 对象")
        return body

    def _send(self, status: int, payload: dict | list):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _current_user(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self.ctx.sessions.resolve(auth[7:])

    def log_message(self, fmt, *args):  # 静默默认访问日志，审计链才是正式记录
        return

    # ---------------------------------------------------------- 路由

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                return self._send(200, {"status": "ok"})

            if method == "POST" and path == "/api/auth/login":
                body = self._read_json()
                token, user = authenticate(
                    body.get("username", ""), body.get("password", ""),
                    self.ctx.sessions, self.ctx.audit)
                return self._send(200, {"token": token,
                                        "user": {"username": user["username"],
                                                 "display_name": user["display_name"],
                                                 "role": user["role"]}})

            user = self._current_user()
            if user is None:
                from ..errors import AuthError
                raise AuthError("缺少或无效的 Authorization token")
            self._route(method, path, user)
        except AppError as e:
            self._send(e.status, e.to_dict())
        except Exception as exc:  # noqa: BLE001 — API 边界统一兜底
            self._send(500, {"error": "INTERNAL", "message": str(exc)})

    def _route(self, method: str, path: str, user: dict):
        r = self.router.get((method, path))
        if r:
            return r(self, self._read_json() if method == "POST" else {}, user)

        for pattern, (verbs, fn) in self.dynamic:
            if method in verbs:
                m = pattern.fullmatch(path)
                if m:
                    return fn(self, self._read_json() if method == "POST" else {},
                              user, **m.groupdict())
        self._send(404, {"error": "NOT_FOUND", "message": f"无此路由: {method} {path}"})

    # ---------------------------------------------------------- 端点实现

    def _ep_master_materials(self, body, user):
        item = masterdata.create_material(
            actor=user, code=body["code"], name=body["name"], spec=body["spec"],
            category=body["category"], unit=body["unit"])
        self._send(201, item)

    def _ep_master_products(self, body, user):
        item = masterdata.create_product(
            actor=user, code=body["code"], name=body["name"],
            dosage_form=body["dosage_form"], spec=body["spec"],
            batch_size=body["batch_size"], batch_size_unit=body["batch_size_unit"],
            yield_lower_pct=body["yield_lower_pct"],
            yield_upper_pct=body["yield_upper_pct"])
        self._send(201, item)

    def _ep_master_bom(self, body, user):
        item = masterdata.add_bom_item(
            actor=user, product_code=body["product_code"],
            material_code=body["material_code"], qty_required=body["qty_required"],
            tolerance_pct=body.get("tolerance_pct", 5),
            sequence_no=body["sequence_no"])
        self._send(201, item)

    def _ep_master_params(self, body, user):
        item = masterdata.add_process_parameter(
            actor=user, product_code=body["product_code"], step_no=body["step_no"],
            step_name=body["step_name"], param_name=body["param_name"],
            target=body.get("target"), lower_limit=body.get("lower_limit"),
            upper_limit=body.get("upper_limit"), unit=body.get("unit", ""),
            is_critical=body.get("is_critical", False))
        self._send(201, item)

    def _ep_master_specs(self, body, user):
        item = masterdata.add_qc_spec(
            actor=user, product_code=body["product_code"], test_name=body["test_name"],
            test_type=body["test_type"], lower_limit=body.get("lower_limit"),
            upper_limit=body.get("upper_limit"), expected_text=body.get("expected_text"),
            unit=body.get("unit", ""), is_critical=body.get("is_critical", False))
        self._send(201, item)

    def _ep_batches(self, body, user):
        batch = service.create_batch(
            actor=user, product_code=body["product_code"],
            planned_size=body["planned_size"], batch_no=body.get("batch_no"))
        self._send(201, batch)

    def _ep_batch_ebr(self, body, user, batch_no):
        self._send(200, service.get_ebr(batch_no))

    def _ep_batch_evaluate(self, body, user, batch_no):
        self._send(200, service.evaluate_batch(batch_no))

    def _ep_batch_start(self, body, user, batch_no):
        self._send(200, service.start_production(actor=user, batch_no=batch_no))

    def _ep_batch_complete(self, body, user, batch_no):
        self._send(200, service.complete_production(
            actor=user, batch_no=batch_no, actual_size=body["actual_size"]))

    def _ep_batch_reopen(self, body, user, batch_no):
        self._send(200, service.reopen_for_correction(
            actor=user, batch_no=batch_no, reason=body["reason"]))

    def _ep_dispense(self, body, user, batch_no):
        self._send(201, service.dispense_material(
            actor=user, batch_no=batch_no, material_code=body["material_code"],
            qty_actual=body["qty_actual"]))

    def _ep_dispense_verify(self, body, user, batch_no):
        self._send(200, service.verify_dispensing(
            actor=user, batch_no=batch_no, material_code=body["material_code"]))

    def _ep_process(self, body, user, batch_no):
        self._send(201, service.record_process_value(
            actor=user, batch_no=batch_no, step_no=body["step_no"],
            param_name=body["param_name"], actual_value=body["actual_value"]))

    def _ep_qc(self, body, user, batch_no):
        self._send(201, service.record_qc_result(
            actor=user, batch_no=batch_no, test_name=body["test_name"],
            numeric_value=body.get("numeric_value"), text_value=body.get("text_value")))

    def _ep_qc_review(self, body, user, batch_no):
        self._send(200, service.review_qc_result(
            actor=user, batch_no=batch_no, test_name=body["test_name"]))

    def _ep_deviations(self, body, user, batch_no):
        self._send(201, service.raise_deviation(
            actor=user, batch_no=batch_no, category=body["category"],
            title=body["title"], description=body["description"],
            source_type=body.get("source_type", "OTHER"),
            source_ref=body.get("source_ref", "")))

    def _ep_deviation_close(self, body, user, dev_no):
        self._send(200, service.close_deviation(
            actor=user, dev_no=dev_no, root_cause=body["root_cause"],
            root_cause_confirmed=body.get("root_cause_confirmed", False),
            is_lab_error=body.get("is_lab_error", False),
            outcome=body.get("outcome", "EFFECTIVE")))

    def _ep_capas(self, body, user):
        self._send(201, service.create_capa(
            actor=user, dev_no=body["dev_no"], action=body["action"],
            owner_username=body["owner_username"], due_date=body["due_date"]))

    def _ep_capa_implement(self, body, user, capa_id):
        self._send(200, service.implement_capa(actor=user, capa_id=int(capa_id)))

    def _ep_capa_verify(self, body, user, capa_id):
        self._send(200, service.verify_capa(
            actor=user, capa_id=int(capa_id), effective=body["effective"],
            note=body["note"]))

    def _ep_release(self, body, user, batch_no):
        self._send(200, service.release_batch(
            actor=user, batch_no=batch_no, reason=body["reason"]))

    def _ep_reject(self, body, user, batch_no):
        self._send(200, service.reject_batch(
            actor=user, batch_no=batch_no, reason=body["reason"]))

    def _ep_audit(self, body, user):
        self._send(200, self.ctx.audit.list(
            entity_type=body.get("entity_type"),
            entity_ref=body.get("entity_ref"),
            limit=body.get("limit", 200)))

    def _ep_audit_verify(self, body, user):
        self._send(200, self.ctx.audit.verify_chain())


# 静态路由表（在类定义完成后装配）
Handler.router = {
    ("GET", "/api/materials"): lambda h, b, u: h._send(200, masterdata.list_materials()),
    ("GET", "/api/products"): lambda h, b, u: h._send(200, masterdata.list_products()),
    ("POST", "/api/master/materials"): Handler._ep_master_materials,
    ("POST", "/api/master/products"): Handler._ep_master_products,
    ("POST", "/api/master/bom-items"): Handler._ep_master_bom,
    ("POST", "/api/master/process-parameters"): Handler._ep_master_params,
    ("POST", "/api/master/qc-specs"): Handler._ep_master_specs,
    ("POST", "/api/batches"): Handler._ep_batches,
    ("POST", "/api/capas"): Handler._ep_capas,
    ("POST", "/api/audit"): Handler._ep_audit,
    ("POST", "/api/audit/verify"): Handler._ep_audit_verify,
}

Handler.dynamic = [
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/ebr$"),
     ({"GET"}, Handler._ep_batch_ebr)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/evaluate$"),
     ({"POST"}, Handler._ep_batch_evaluate)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/start$"),
     ({"POST"}, Handler._ep_batch_start)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/complete$"),
     ({"POST"}, Handler._ep_batch_complete)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/reopen$"),
     ({"POST"}, Handler._ep_batch_reopen)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/dispensing$"),
     ({"POST"}, Handler._ep_dispense)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/dispensing/verify$"),
     ({"POST"}, Handler._ep_dispense_verify)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/process-records$"),
     ({"POST"}, Handler._ep_process)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/qc-results$"),
     ({"POST"}, Handler._ep_qc)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/qc-results/review$"),
     ({"POST"}, Handler._ep_qc_review)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/deviations$"),
     ({"POST"}, Handler._ep_deviations)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/release$"),
     ({"POST"}, Handler._ep_release)),
    (re.compile(r"/api/batches/(?P<batch_no>[^/]+)/reject$"),
     ({"POST"}, Handler._ep_reject)),
    (re.compile(r"/api/deviations/(?P<dev_no>[^/]+)/close$"),
     ({"POST"}, Handler._ep_deviation_close)),
    (re.compile(r"/api/capas/(?P<capa_id>\d+)/implement$"),
     ({"POST"}, Handler._ep_capa_implement)),
    (re.compile(r"/api/capas/(?P<capa_id>\d+)/verify$"),
     ({"POST"}, Handler._ep_capa_verify)),
]


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EBR 批放行管理系统 API 已启动: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import os
    serve(port=int(os.environ.get("EBR_PORT", "8080")))
