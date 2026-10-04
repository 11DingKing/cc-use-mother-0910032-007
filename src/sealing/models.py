"""领域模型：状态、角色与数据结构。

状态机与 domain/contract.json 保持一致：
草稿 -> 待核算 -> 已确认 -> 执行中 -> 已封存
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 状态（与领域契约一致）
DRAFT = "草稿"
PENDING_ACCOUNTING = "待核算"
CONFIRMED = "已确认"
IN_PROGRESS = "执行中"
SEALED = "已封存"

STATE_ORDER = [DRAFT, PENDING_ACCOUNTING, CONFIRMED, IN_PROGRESS, SEALED]

# 角色（与领域契约一致）
ROLE_DECLARANT = "企业申报员"
ROLE_ACCOUNTANT = "核算专员"
ROLE_OPERATOR = "交易运营员"
ROLE_AUDITOR = "监管审计员"

ALL_ROLES = {ROLE_DECLARANT, ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_AUDITOR}
# 法定签署人角色范围：申报员不参与签署
SIGNER_ROLES = {ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_AUDITOR}

# 更正单 / 重开申请状态
PENDING = "待批准"
APPROVED = "已批准"
REJECTED = "已驳回"

# 迟到材料路由
ROUTE_NEXT_VERSION = "next_version"
ROUTE_CORRECTION = "correction"

DEFAULT_STATUTORY_SIGNATURES = 3


@dataclass
class Actor:
    actor_id: str
    role: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeclarationInput:
    """申报输入（车型明细行）。金额一律用分，避免浮点误差。"""

    input_id: str
    model_code: str
    model_name: str
    category: str
    quantity: int
    unit_price_cents: int
    declared_by: str
    declared_at: str

    @property
    def gross_cents(self) -> int:
        return self.quantity * self.unit_price_cents

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ManualAdjustment:
    adjustment_id: str
    target_model_code: str | None  # None 表示针对整份报告
    delta_cents: int
    reason: str
    operator: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountingRuleSet:
    rule_set_id: str
    version: str
    rules: list[dict]  # 每项含 rule_id/type/params，按 rule_id 排序后确定性地应用

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signature:
    signer_id: str
    signer_name: str
    role: str
    signed_at: str
    payload_digest: str  # 签署时锁定的内容摘要

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SealedInfo:
    """封存产物：冻结载荷 + 分块摘要。"""

    sealed_at: str
    pre_seal_digest: str  # 封存前内容摘要（签署人签署的对象）
    payload_sha256: str  # 冻结载荷整体哈希
    payload: str  # 冻结的规范 JSON 文本（导出唯一数据源）
    chunk_size: int
    chunk_hashes: list[str]
    merkle_root: str
    levels: list[list[str]]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Snapshot:
    snapshot_id: str
    period: str
    version: int
    state: str
    statutory_signatures: int
    created_by: str
    created_at: str
    inputs: list[DeclarationInput] = field(default_factory=list)
    adjustments: list[ManualAdjustment] = field(default_factory=list)
    rule_set: AccountingRuleSet | None = None
    totals: dict | None = None
    signable_digest: str | None = None  # 已确认时锁定的待签署摘要
    signatures: list[Signature] = field(default_factory=list)
    sealed: SealedInfo | None = None
    reopened_from: str | None = None
    reopen_approval: dict | None = None

    def to_dict(self, include_payload: bool = False) -> dict:
        data = asdict(self)
        if not include_payload and data.get("sealed"):
            data["sealed"] = {k: v for k, v in data["sealed"].items() if k not in ("payload", "levels")}
        return data


@dataclass
class CorrectionOrder:
    """更正单：针对已封存快照的受控修正，需独立批准。"""

    correction_id: str
    snapshot_id: str
    reason: str
    adjustments: list[ManualAdjustment]
    requested_by: str
    requested_at: str
    state: str = PENDING
    decided_by: str | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReopenRequest:
    """重开申请：批准后派生下一版草稿，原封存版本保持不动。"""

    request_id: str
    snapshot_id: str
    reason: str
    requested_by: str
    requested_at: str
    state: str = PENDING
    decided_by: str | None = None
    decided_at: str | None = None
    new_snapshot_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExportSession:
    """导出会话：分块拉取、可中断续传；内容永远来自冻结载荷。"""

    export_id: str
    snapshot_id: str
    created_by: str
    created_at: str
    chunk_count: int
    chunk_size: int
    merkle_root: str
    payload_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


def snapshot_from_dict(data: dict) -> Snapshot:
    """从存储字典还原快照对象。"""
    payload = dict(data)
    payload["inputs"] = [DeclarationInput(**item) for item in payload.get("inputs", [])]
    payload["adjustments"] = [ManualAdjustment(**item) for item in payload.get("adjustments", [])]
    payload["signatures"] = [Signature(**item) for item in payload.get("signatures", [])]
    if payload.get("rule_set"):
        payload["rule_set"] = AccountingRuleSet(**payload["rule_set"])
    if payload.get("sealed"):
        payload["sealed"] = SealedInfo(**payload["sealed"])
    return Snapshot(**payload)


def correction_from_dict(data: dict) -> CorrectionOrder:
    payload = dict(data)
    payload["adjustments"] = [ManualAdjustment(**item) for item in payload.get("adjustments", [])]
    return CorrectionOrder(**payload)


def reopen_from_dict(data: dict) -> ReopenRequest:
    return ReopenRequest(**dict(data))


def export_from_dict(data: dict) -> ExportSession:
    return ExportSession(**dict(data))


def as_storable(value: Any) -> Any:
    return value.to_dict() if hasattr(value, "to_dict") else value
