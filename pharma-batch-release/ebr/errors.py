"""业务异常定义。

所有异常携带 ``code``（机器可读）与 HTTP 风格 ``status``，
API 层据此生成统一错误响应。
"""

from __future__ import annotations


class AppError(Exception):
    code = "APP_ERROR"
    status = 400

    def __init__(self, message: str, *, details=None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status = 404


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status = 422


class AuthError(AppError):
    code = "AUTH_ERROR"
    status = 401


class PermissionDeniedError(AppError):
    code = "PERMISSION_DENIED"
    status = 403


class ConflictError(AppError):
    """状态机非法迁移、重复提交等冲突。"""

    code = "CONFLICT"
    status = 409


class ReleaseBlockedError(AppError):
    """放行门禁未通过 —— 系统中最关键的异常，绝不允许被参数绕过。"""

    code = "RELEASE_BLOCKED"
    status = 409

    def __init__(self, message: str, *, gates: list | None = None):
        super().__init__(message, details={"gates": gates or []})
        self.gates = gates or []


class AuditChainBrokenError(AppError):
    code = "AUDIT_CHAIN_BROKEN"
    status = 500
