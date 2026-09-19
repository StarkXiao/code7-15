"""业务异常：携带 HTTP 状态码，由统一错误处理器转成 JSON。"""


class AppError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFound(AppError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, 404, "not_found")


class Conflict(AppError):
    def __init__(self, message: str):
        super().__init__(message, 409, "conflict")


class Forbidden(AppError):
    def __init__(self, message: str = "无权执行该操作"):
        super().__init__(message, 403, "forbidden")


class Unauthorized(AppError):
    def __init__(self, message: str = "未认证或登录已失效"):
        super().__init__(message, 401, "unauthorized")


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(message, 422, "validation_error")
