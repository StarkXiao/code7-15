"""HTTP 服务：仅依赖标准库的 REST API + 静态前端托管。

路由风格 /api/<资源>，JSON 收发，Authorization: Bearer <token>。
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import auth, services
from .database import append_audit, connect, init_db, transaction
from .errors import AppError, Unauthorized

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# 无需登录的接口前缀白名单
PUBLIC_PATHS = {"/api/auth/login", "/api/health"}


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatchRelease/1.0"

        def log_message(self, fmt, *args):  # 精简日志
            import sys
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

        # ---------- 基础工具 ----------
        def _send_json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                raise AppError("请求体不是合法 JSON", 400, "bad_json")
            if not isinstance(data, dict):
                raise AppError("请求体必须是 JSON 对象", 400, "bad_json")
            return data

        def _user(self) -> dict:
            if urlparse(self.path).path in PUBLIC_PATHS:
                return None
            token = self.headers.get("Authorization", "")
            token = token[7:] if token.startswith("Bearer ") else None
            return auth.current_user(token)

        # ---------- 入口 ----------
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path.startswith("/api/"):
                    user = self._user()
                    self._route_api(method, path, user)
                else:
                    self._serve_static(path)
            except AppError as e:
                self._send_json({"error": e.code, "message": e.message}, e.status)
            except Exception as e:  # noqa: BLE001 - 兜底，避免 500 裸栈
                import traceback, sys
                traceback.print_exc(file=sys.stderr)
                self._send_json({"error": "internal", "message": str(e)}, 500)

        # ---------- 静态文件 ----------
        def _serve_static(self, path: str):
            rel = "index.html" if path == "/" else path.lstrip("/")
            target = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
                target = os.path.join(STATIC_DIR, "index.html")  # SPA 回退
            ctype = {".html": "text/html; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}.get(
                os.path.splitext(target)[1], "application/octet-stream")
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- API 路由 ----------
        def _route_api(self, method, path, user):
            D = db_path
            body = self._read_json() if method == "POST" else {}

            # --- 认证 ---
            if path == "/api/auth/login" and method == "POST":
                u, token = auth.login(D, str(body.get("username", "")),
                                      str(body.get("password", "")))
                conn = connect(D)
                try:
                    with transaction(conn):
                        append_audit(conn, actor_id=u["id"], actor_name=u["display_name"],
                                     action="login_ok", entity_type="auth",
                                     entity_id=u["username"])
                finally:
                    conn.close()
                return self._send_json({"user": u, "token": token})

            if path == "/api/auth/logout" and method == "POST":
                token = self.headers.get("Authorization", "")
                auth.logout(token[7:] if token.startswith("Bearer ") else "")
                return self._send_json({"ok": True})

            if path == "/api/health":
                return self._send_json({"ok": True, "service": "batch-release"})

            if path == "/api/me" and method == "GET":
                return self._send_json(user)

            if path == "/api/catalog" and method == "GET":
                return self._send_json(services.get_catalog(D))

            # --- 批次 ---
            if path == "/api/batches" and method == "GET":
                return self._send_json(services.list_batches(D))
            if path == "/api/batches" and method == "POST":
                return self._send_json(
                    services.create_batch(D, user, body.get("batch_no"),
                                          body.get("batch_size")), 201)

            m = re.fullmatch(r"/api/batches/(\d+)", path)
            if m and method == "GET":
                return self._send_json(services.get_ebr(D, int(m.group(1))))

            bid_match = lambda sub: re.fullmatch(rf"/api/batches/(\d+)/{sub}", path)

            if (mm := bid_match(r"start-weighing")) and method == "POST":
                return self._send_json(services.start_weighing(D, int(mm.group(1)), user))
            if (mm := bid_match(r"weighing")) and method == "POST":
                return self._send_json(services.record_weighing(
                    D, int(mm.group(1)), str(body["material_code"]),
                    body.get("actual_qty"), user), 201)
            if (mm := re.fullmatch(r"/api/batches/(\d+)/weighing/(\d+)/check", path)) \
                    and method == "POST":
                return self._send_json(
                    services.check_weighing(D, int(mm.group(1)), int(mm.group(2)), user))
            if (mm := bid_match(r"steps/start")) and method == "POST":
                return self._send_json(services.start_step(
                    D, int(mm.group(1)), int(body["step_no"]), user), 201)
            if (mm := bid_match(r"steps/finish")) and method == "POST":
                return self._send_json(services.finish_step(
                    D, int(mm.group(1)), int(body["step_no"]), user))
            if (mm := bid_match(r"params")) and method == "POST":
                return self._send_json(services.record_param(
                    D, int(mm.group(1)), int(body["step_no"]),
                    str(body["param_name"]), body.get("value"), user), 201)
            if (mm := bid_match(r"finish-production")) and method == "POST":
                return self._send_json(services.finish_production(
                    D, int(mm.group(1)), body.get("actual_yield_pct"), user))
            if (mm := bid_match(r"qc/start")) and method == "POST":
                return self._send_json(services.start_qc(D, int(mm.group(1)), user))
            if (mm := bid_match(r"qc")) and method == "POST":
                return self._send_json(services.record_qc(
                    D, int(mm.group(1)), str(body["test_name"]),
                    body.get("value"), user), 201)
            if (mm := re.fullmatch(r"/api/batches/(\d+)/qc/(\d+)/check", path)) \
                    and method == "POST":
                return self._send_json(
                    services.check_qc(D, int(mm.group(1)), int(mm.group(2)), user))
            if (mm := bid_match(r"submit")) and method == "POST":
                return self._send_json(services.submit_for_review(D, int(mm.group(1)), user))
            if (mm := bid_match(r"review")) and method == "GET":
                return self._send_json(services.review_status(D, int(mm.group(1))))
            if (mm := bid_match(r"release")) and method == "POST":
                return self._send_json(services.release_batch(
                    D, int(mm.group(1)), str(body.get("comment", "")),
                    str(body.get("password", "")), user))
            if (mm := bid_match(r"reject")) and method == "POST":
                return self._send_json(services.reject_batch(
                    D, int(mm.group(1)), str(body.get("reason", "")), user))
            if (mm := bid_match(r"return-retest")) and method == "POST":
                return self._send_json(services.return_for_retest(
                    D, int(mm.group(1)), str(body.get("reason", "")), user))
            if (mm := bid_match(r"deviations")) and method == "GET":
                return self._send_json(services.list_deviations(D, int(mm.group(1))))
            if (mm := bid_match(r"deviations")) and method == "POST":
                return self._send_json(services.raise_manual_deviation(
                    D, int(mm.group(1)), str(body.get("severity", "")),
                    str(body.get("description", "")), user), 201)
            if (mm := bid_match(r"integrity")) and method == "GET":
                return self._send_json(services.full_integrity_report(D, int(mm.group(1))))

            # --- 偏差（全局）---
            if path == "/api/deviations" and method == "GET":
                return self._send_json(services.list_deviations(D))
            m = re.fullmatch(r"/api/deviations/(\d+)/close", path)
            if m and method == "POST":
                return self._send_json(services.close_deviation(
                    D, int(m.group(1)), str(body.get("disposition", "")),
                    str(body.get("capa_summary", "")), user))

            # --- 审计 ---
            if path == "/api/audit" and method == "GET":
                return self._send_json(services.list_audit(D))
            if path == "/api/integrity" and method == "GET":
                return self._send_json(services.full_integrity_report(D))

            raise AppError("接口不存在", 404, "not_found")

    return Handler


def serve(db_path: str, host: str = "0.0.0.0", port: int = 8000) -> None:
    init_db(db_path)
    services.seed(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"药品批放行管理系统已启动: http://{host}:{port}  (db={db_path})")
    httpd.serve_forever()
