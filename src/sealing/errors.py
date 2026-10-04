"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则冲突。

    code 取值与 HTTP 映射：
    - validation  -> 400 请求内容不合法
    - forbidden   -> 403 角色或职责分离冲突
    - not_found   -> 404 对象不存在
    - conflict    -> 409 状态机或唯一性冲突
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def validation(message: str) -> DomainError:
    return DomainError("validation", message)


def forbidden(message: str) -> DomainError:
    return DomainError("forbidden", message)


def not_found(message: str) -> DomainError:
    return DomainError("not_found", message)


def conflict(message: str) -> DomainError:
    return DomainError("conflict", message)
