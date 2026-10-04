"""封账服务的领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类，携带 HTTP 状态码与稳定错误码。"""

    status = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.message = message


class NotFound(DomainError):
    status = 404
    code = "NOT_FOUND"


class InvalidState(DomainError):
    status = 409
    code = "INVALID_STATE"


class Validation(DomainError):
    status = 422
    code = "VALIDATION"


class Forbidden(DomainError):
    status = 403
    code = "FORBIDDEN"
