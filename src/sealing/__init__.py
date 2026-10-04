"""年度核算审计封账服务。

将申报输入、核算规则、人工调整和签署人汇总为封账快照；
达到法定签署数后锁定内容并生成分块摘要；迟到材料进入下一版或
更正单；重开需独立批准；重复导出与中断续传不改变签发内容。
"""
from __future__ import annotations

from .errors import DomainError
from .service import LedgerSealingService

__all__ = ["DomainError", "LedgerSealingService"]
