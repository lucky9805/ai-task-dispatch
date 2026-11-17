"""类型化错误体系：业务错误与 HTTP 呈现解耦。

服务层抛这些异常；控制器层统一映射成规范化的错误响应，不暴露堆栈。
"""
from __future__ import annotations


class DispatchError(Exception):
    """所有业务错误的基类。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None,
                 http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status

    def as_dict(self) -> dict:
        return {"error": self.message, "code": self.code}


class ValidationError(DispatchError):
    code = "invalid_argument"
    http_status = 400


class AuthError(DispatchError):
    code = "unauthorized"
    http_status = 401


class PermissionDeniedError(DispatchError):
    code = "forbidden"
    http_status = 403


class NotFoundError(DispatchError):
    code = "not_found"
    http_status = 404


class ConflictError(DispatchError):
    code = "conflict"
    http_status = 409


def to_http(exc: Exception) -> tuple[int, dict]:
    """把任意异常映射成 (HTTP 状态, 错误体)。未知异常一律 500 且不泄露细节。"""
    if isinstance(exc, DispatchError):
        return exc.http_status, exc.as_dict()
    return 500, {"error": "服务端异常", "code": "internal_error"}
