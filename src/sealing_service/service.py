"""年度核算审计封账的领域服务。

状态机（与 domain/contract.json 对齐）：
    草稿 → 待核算 → 已确认 → 执行中 → 已封存

关键约束：
- 封账输入快照：确认时冻结候选内容，封存时把申报输入、核算规则、人工调整、
  签署人一并固化为不可变快照。
- 法定签署人数：达到 quorum 个指定签署人的不同签名才允许封存。
- 分块摘要校验：封存时对规范字节流分块计算 SHA-256，并生成 Merkle 根。
- 受控重开更正：封存后迟到材料只能进入下一版或更正单；重开必须由
  独立于申请人的监管审计员批准；旧快照永久可导出、可校验。
"""
from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from . import canonical
from .errors import Forbidden, InvalidState, NotFound, Validation
from .store import Store

CENT = Decimal("0.01")
AUDITOR_ROLE = "监管审计员"
ADJUSTABLE_FIELDS = ("amount", "tax", "total")

# 契约状态
STATE_DRAFT = "草稿"
STATE_PENDING = "待核算"
STATE_CONFIRMED = "已确认"
STATE_SIGNING = "执行中"
STATE_SEALED = "已封存"


def _money(value: Any) -> str:
    """金额规范化为两位小数字符串（HALF_UP）。"""
    return str(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def _number_text(value: Any) -> str:
    """数量规范化为无多余零的十进制字符串。"""
    return format(Decimal(str(value)).normalize(), "f")


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_id_factory() -> Callable[[str], str]:
    return lambda prefix: f"{prefix}-{uuid.uuid4().hex[:12]}"


class SealingService:
    """封账领域用例集合；所有写操作在 Store 事务内完成。"""

    def __init__(
        self,
        store: Store,
        *,
        chunk_size: int = 4096,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正整数")
        self._store = store
        self._chunk_size = chunk_size
        self._clock = clock or _default_clock
        self._ids = id_factory or _default_id_factory()

    # ------------------------------------------------------------------
    # 期间生命周期
    # ------------------------------------------------------------------
    def create_period(
        self,
        *,
        period_id: str,
        year: int,
        quorum: int,
        signers: list[dict],
        ruleset: dict,
        actor: str,
    ) -> dict:
        """创建核算期间（草稿），登记法定签署人数与指定签署人。"""
        if not period_id:
            raise Validation("period_id 不能为空")
        if not isinstance(year, int) or year < 2000:
            raise Validation("year 必须是有效年份")
        if not isinstance(quorum, int) or quorum < 1:
            raise Validation("quorum 必须是不小于 1 的整数")
        if not isinstance(signers, list) or not signers:
            raise Validation("signers 必须是非空列表")
        if quorum > len(signers):
            raise Validation("quorum 不能超过指定签署人数量")
        normalized_signers = []
        seen = set()
        for item in signers:
            signer_id = item.get("signer_id")
            if not signer_id or signer_id in seen:
                raise Validation("签署人 signer_id 不能为空且不能重复")
            seen.add(signer_id)
            normalized_signers.append(
                {
                    "signer_id": signer_id,
                    "name": str(item.get("name") or signer_id),
                    "role": str(item.get("role") or ""),
                }
            )
        ruleset_normalized = self._normalize_ruleset(ruleset)
        with self._store.transact() as data:
            if period_id in data["periods"]:
                raise Validation(f"期间已存在：{period_id}", code="DUPLICATE_PERIOD", status=409)
            period = {
                "period_id": period_id,
                "year": year,
                "state": STATE_DRAFT,
                "version": 1,
                "quorum": quorum,
                "signers": normalized_signers,
                "ruleset": ruleset_normalized,
                "inputs": [],
                "adjustments": [],
                "pending_materials": [],
                "signatures": [],
                "candidate": None,
                "candidate_digest": None,
                "snapshot_ids": [],
                "active_snapshot_id": None,
                "adj_seq": 0,
                "created_at": self._clock(),
            }
            data["periods"][period_id] = period
            self._audit(data, period_id, actor, "PERIOD_CREATED", {"year": year, "quorum": quorum})
            return self._period_view(period)

    def add_inputs(self, period_id: str, *, lines: list[dict], actor: str) -> dict:
        """在草稿状态下追加申报输入（车型明细行）。"""
        if not isinstance(lines, list) or not lines:
            raise Validation("lines 必须是非空列表")
        with self._store.transact() as data:
            period = self._period(data, period_id)
            self._require_state(period, STATE_DRAFT, "仅草稿状态可录入申报输入")
            existing = {line["line_id"] for line in period["inputs"]}
            added = []
            for raw in lines:
                line = self._normalize_line(raw, period)
                if line["line_id"] in existing:
                    raise Validation(f"申报行重复：{line['line_id']}", code="DUPLICATE_LINE")
                existing.add(line["line_id"])
                period["inputs"].append(line)
                added.append(line)
            self._audit(data, period_id, actor, "INPUTS_ADDED", {"count": len(added)})
            return self._period_view(period)

    def submit(self, period_id: str, *, actor: str) -> dict:
        """草稿 → 待核算。"""
        with self._store.transact() as data:
            period = self._period(data, period_id)
            self._require_state(period, STATE_DRAFT, "仅草稿状态可提交核算")
            if not period["inputs"]:
                raise Validation("申报输入为空，不能提交核算")
            period["state"] = STATE_PENDING
            self._audit(data, period_id, actor, "SUBMITTED", {"line_count": len(period["inputs"])})
            return self._period_view(period)

    def add_adjustment(self, period_id: str, *, adjustment: dict, actor: str) -> dict:
        """在待核算状态下登记人工调整。"""
        with self._store.transact() as data:
            period = self._period(data, period_id)
            self._require_state(period, STATE_PENDING, "仅待核算状态可登记人工调整")
            record = self._normalize_adjustment(adjustment, period, actor)
            period["adjustments"].append(record)
            self._audit(data, period_id, actor, "ADJUSTMENT_ADDED", {"adjustment_id": record["adjustment_id"]})
            return self._period_view(period)

    def confirm(self, period_id: str, *, actor: str) -> dict:
        """待核算 → 已确认：冻结候选内容并生成待签署摘要。"""
        with self._store.transact() as data:
            period = self._period(data, period_id)
            self._require_state(period, STATE_PENDING, "仅待核算状态可确认")
            candidate = self._build_candidate(period)
            period["candidate"] = candidate
            period["candidate_digest"] = canonical.sha256_hex(canonical.canonical_bytes(candidate))
            period["signatures"] = []
            period["state"] = STATE_CONFIRMED
            self._audit(data, period_id, actor, "CONFIRMED", {"candidate_digest": period["candidate_digest"]})
            return self._period_view(period)

    def sign(self, period_id: str, *, signer_id: str, signature: str) -> dict:
        """指定签署人签署候选内容；达到法定人数即封存并生成分块摘要。"""
        if not signature or not str(signature).strip():
            raise Validation("signature 不能为空")
        with self._store.transact() as data:
            period = self._period(data, period_id)
            if period["state"] not in (STATE_CONFIRMED, STATE_SIGNING):
                raise InvalidState(f"当前状态 {period['state']} 不允许签署")
            designated = {item["signer_id"]: item for item in period["signers"]}
            if signer_id not in designated:
                raise Forbidden("签署人不在指定名单中", code="UNKNOWN_SIGNER")
            if any(item["signer_id"] == signer_id for item in period["signatures"]):
                raise InvalidState("该签署人已签署，不能重复签署", code="DUPLICATE_SIGNATURE")
            period["signatures"].append(
                {
                    "signer_id": signer_id,
                    "name": designated[signer_id]["name"],
                    "role": designated[signer_id]["role"],
                    "signed_at": self._clock(),
                    "signature": str(signature),
                    "statement_digest": period["candidate_digest"],
                }
            )
            signed = len(period["signatures"])
            snapshot = None
            if signed >= period["quorum"]:
                snapshot = self._seal(data, period)
            else:
                period["state"] = STATE_SIGNING
                self._audit(data, period_id, signer_id, "SIGNED", {"signed": signed, "required": period["quorum"]})
            view = self._period_view(period)
            view["snapshot"] = self._manifest(snapshot) if snapshot else None
            return view

    # ------------------------------------------------------------------
    # 封存产物：清单、导出、分块
    # ------------------------------------------------------------------
    def get_snapshot(self, snapshot_id: str) -> dict:
        with self._store.read() as data:
            return self._manifest(self._snapshot(data, snapshot_id), include_chunks=True)

    def list_snapshots(self, period_id: str) -> list[dict]:
        with self._store.read() as data:
            period = self._period(data, period_id)
            return [self._manifest(data["snapshots"][sid]) for sid in period["snapshot_ids"]]

    def export_snapshot(self, snapshot_id: str, *, actor: str = "系统") -> tuple[bytes, str]:
        """导出封存字节流；重复导出返回完全一致的 bytes 与 ETag。"""
        with self._store.transact() as data:
            snapshot = self._snapshot(data, snapshot_id)
            payload = snapshot["payload_json"].encode("utf-8")
            self._audit(
                data,
                snapshot["period_id"],
                actor,
                "EXPORTED",
                {"snapshot_id": snapshot_id, "bytes": len(payload), "content_digest": snapshot["content_digest"]},
            )
            return payload, snapshot["content_digest"]

    def get_chunk(self, snapshot_id: str, index: int) -> tuple[bytes, str, int]:
        """按块下载（中断续传的最小单元），返回块内容、块摘要、总块数。"""
        with self._store.read() as data:
            snapshot = self._snapshot(data, snapshot_id)
            chunks = self._chunks(snapshot)
            if not 0 <= index < len(chunks):
                raise NotFound(f"分块不存在：{index}", code="CHUNK_NOT_FOUND")
            return chunks[index], snapshot["chunk_digests"][index], len(chunks)

    def chunk_proof(self, snapshot_id: str, index: int) -> dict:
        with self._store.read() as data:
            snapshot = self._snapshot(data, snapshot_id)
            if not 0 <= index < len(snapshot["chunk_digests"]):
                raise NotFound(f"分块不存在：{index}", code="CHUNK_NOT_FOUND")
            proof = canonical.merkle_proof(snapshot["chunk_digests"], index)
            leaf = snapshot["chunk_digests"][index]
            return {
                "snapshot_id": snapshot_id,
                "index": index,
                "leaf": leaf,
                "proof": proof,
                "root": snapshot["content_digest"],
                "valid": canonical.merkle_verify(leaf, proof, snapshot["content_digest"]),
            }

    def verify_chunk(self, snapshot_id: str, *, index: int, data: bytes) -> dict:
        """校验任一分块：比对清单摘要并验证其 Merkle 路径是否归于封存根。"""
        with self._store.read() as store_data:
            snapshot = self._snapshot(store_data, snapshot_id)
            if not 0 <= index < len(snapshot["chunk_digests"]):
                raise NotFound(f"分块不存在：{index}", code="CHUNK_NOT_FOUND")
            digest = canonical.sha256_hex(data)
            expected = snapshot["chunk_digests"][index]
            proof = canonical.merkle_proof(snapshot["chunk_digests"], index)
            proof_valid = canonical.merkle_verify(digest, proof, snapshot["content_digest"])
            return {
                "snapshot_id": snapshot_id,
                "index": index,
                "digest": digest,
                "expected": expected,
                "matches_manifest": digest == expected,
                "proof": proof,
                "proof_valid": proof_valid,
                "root": snapshot["content_digest"],
                "verified": digest == expected and proof_valid,
            }

    # ------------------------------------------------------------------
    # 迟到材料、更正单、受控重开
    # ------------------------------------------------------------------
    def submit_late_material(
        self,
        period_id: str,
        *,
        strategy: str,
        materials: dict,
        reason: str,
        author: str,
    ) -> dict:
        """封存后迟到材料的唯一入口：进入下一版（排队）或生成更正单。"""
        if strategy not in ("next_version", "correction"):
            raise Validation("strategy 必须是 next_version 或 correction")
        if not reason or not str(reason).strip():
            raise Validation("迟到材料必须说明原因")
        with self._store.transact() as data:
            period = self._period(data, period_id)
            self._require_state(period, STATE_SEALED, "迟到材料通道仅面向已封存期间")
            normalized = self._normalize_materials(materials, period, author)
            if strategy == "next_version":
                record = {
                    "material_id": self._ids("mat"),
                    "strategy": strategy,
                    "reason": str(reason),
                    "author": author,
                    "created_at": self._clock(),
                    **normalized,
                }
                period["pending_materials"].append(record)
                self._audit(data, period_id, author, "LATE_MATERIAL_QUEUED", {"material_id": record["material_id"]})
                return {"status": "已进入下一版", "material": record}
            correction = {
                "correction_id": self._ids("cor"),
                "period_id": period_id,
                "snapshot_id": period["active_snapshot_id"],
                "base_version": period["version"],
                "materials": normalized,
                "reason": str(reason),
                "author": author,
                "status": "待批准",
                "created_at": self._clock(),
                "decided_by": None,
                "decided_at": None,
            }
            data["corrections"][correction["correction_id"]] = correction
            self._audit(data, period_id, author, "CORRECTION_CREATED", {"correction_id": correction["correction_id"]})
            return {"status": "待批准", "correction": deepcopy(correction)}

    def approve_correction(self, correction_id: str, *, approver: str, role: str) -> dict:
        """监管审计员批准更正单：基于封存内容开启下一版草稿。"""
        with self._store.transact() as data:
            correction = data["corrections"].get(correction_id)
            if not correction:
                raise NotFound(f"更正单不存在：{correction_id}", code="CORRECTION_NOT_FOUND")
            if correction["status"] != "待批准":
                raise InvalidState(f"更正单当前状态为 {correction['status']}，不能重复处理")
            self._require_independent_auditor(correction["author"], approver, role)
            period = self._period(data, correction["period_id"])
            snapshot = self._snapshot(data, correction["snapshot_id"])
            correction["status"] = "已批准"
            correction["decided_by"] = approver
            correction["decided_at"] = self._clock()
            self._audit(data, period["period_id"], approver, "CORRECTION_APPROVED", {"correction_id": correction_id})
            self._rollover(
                data,
                period,
                snapshot,
                extra_materials=[correction["materials"]],
                reason=f"更正单 {correction_id} 批准",
                actor=approver,
                snapshot_status="superseded",
            )
            return {"correction": deepcopy(correction), "period": self._period_view(period)}

    def request_reopen(self, snapshot_id: str, *, requester: str, reason: str) -> dict:
        """申请重开已封存快照；必须经独立批准才生效。"""
        if not reason or not str(reason).strip():
            raise Validation("重开必须说明原因")
        with self._store.transact() as data:
            snapshot = self._snapshot(data, snapshot_id)
            if snapshot["status"] != "active":
                raise InvalidState("仅当前生效的封存快照可申请重开")
            request = {
                "request_id": self._ids("reopen"),
                "snapshot_id": snapshot_id,
                "period_id": snapshot["period_id"],
                "requester": requester,
                "reason": str(reason),
                "status": "待批准",
                "created_at": self._clock(),
                "decided_by": None,
                "decided_at": None,
            }
            data["reopen_requests"][request["request_id"]] = request
            self._audit(data, snapshot["period_id"], requester, "REOPEN_REQUESTED", {"request_id": request["request_id"]})
            return deepcopy(request)

    def approve_reopen(self, request_id: str, *, approver: str, role: str) -> dict:
        """独立批准重开：旧快照保持不可变，基于其内容开启下一版草稿。"""
        with self._store.transact() as data:
            request = data["reopen_requests"].get(request_id)
            if not request:
                raise NotFound(f"重开申请不存在：{request_id}", code="REOPEN_NOT_FOUND")
            if request["status"] != "待批准":
                raise InvalidState(f"重开申请当前状态为 {request['status']}，不能重复处理")
            self._require_independent_auditor(request["requester"], approver, role)
            period = self._period(data, request["period_id"])
            snapshot = self._snapshot(data, request["snapshot_id"])
            request["status"] = "已批准"
            request["decided_by"] = approver
            request["decided_at"] = self._clock()
            self._audit(data, period["period_id"], approver, "REOPEN_APPROVED", {"request_id": request_id})
            self._rollover(
                data,
                period,
                snapshot,
                extra_materials=[],
                reason=f"重开申请 {request_id} 批准",
                actor=approver,
                snapshot_status="reopened",
            )
            return {"request": deepcopy(request), "period": self._period_view(period)}

    # ------------------------------------------------------------------
    # 差异对比与审计
    # ------------------------------------------------------------------
    def diff_snapshots(self, from_snapshot_id: str, to_snapshot_id: str) -> dict:
        """对比两个封存快照（封账前后版本差异）。"""
        with self._store.read() as data:
            source = self._snapshot(data, from_snapshot_id)
            target = self._snapshot(data, to_snapshot_id)
            return self._diff_views(
                self._snapshot_view(source),
                self._snapshot_view(target),
                from_label=f"snapshot:{from_snapshot_id}",
                to_label=f"snapshot:{to_snapshot_id}",
            )

    def diff_working(self, period_id: str, *, snapshot_id: str | None = None) -> dict:
        """对比封存快照与当前工作稿（封账前后的实时差异）。"""
        with self._store.read() as data:
            period = self._period(data, period_id)
            if snapshot_id is None:
                if not period["snapshot_ids"]:
                    raise InvalidState("该期间尚无封存快照可对比")
                snapshot_id = period["snapshot_ids"][-1]
            snapshot = self._snapshot(data, snapshot_id)
            working = {
                "version": period["version"],
                "ruleset": period["ruleset"],
                "inputs": period["inputs"],
                "adjustments": period["adjustments"],
                "computed": self._compute_view(period["ruleset"], period["inputs"], period["adjustments"]),
            }
            return self._diff_views(
                self._snapshot_view(snapshot),
                working,
                from_label=f"snapshot:{snapshot_id}",
                to_label=f"working:{period_id}@v{period['version']}",
            )

    def audit_trail(self, period_id: str) -> list[dict]:
        with self._store.read() as data:
            self._period(data, period_id)
            return [event for event in data["audit"] if event["period_id"] == period_id]

    def get_period(self, period_id: str) -> dict:
        with self._store.read() as data:
            return self._period_view(self._period(data, period_id))

    # ------------------------------------------------------------------
    # 内部：封存与版本滚动
    # ------------------------------------------------------------------
    def _seal(self, data: dict, period: dict) -> dict:
        """达到法定签署数后固化快照：分块摘要 + Merkle 根，内容自此不可变。"""
        for sid in period["snapshot_ids"]:
            other = data["snapshots"][sid]
            if other["status"] == "active":
                other["status"] = "superseded"
        snapshot_id = self._ids("snap")
        payload = {
            **deepcopy(period["candidate"]),
            "snapshot_id": snapshot_id,
            "sealed_at": self._clock(),
            "quorum": period["quorum"],
            "signatures": deepcopy(period["signatures"]),
        }
        payload_bytes = canonical.canonical_bytes(payload)
        chunks = canonical.split_chunks(payload_bytes, self._chunk_size)
        digests = canonical.chunk_digests(chunks)
        snapshot = {
            "snapshot_id": snapshot_id,
            "period_id": period["period_id"],
            "version": period["version"],
            "state": STATE_SEALED,
            "status": "active",
            "sealed_at": payload["sealed_at"],
            "quorum": period["quorum"],
            "payload": payload,
            "payload_json": payload_bytes.decode("utf-8"),
            "payload_sha256": canonical.sha256_hex(payload_bytes),
            "chunk_size": self._chunk_size,
            "chunk_digests": digests,
            "content_digest": canonical.merkle_root(digests),
        }
        data["snapshots"][snapshot_id] = snapshot
        period["state"] = STATE_SEALED
        period["snapshot_ids"].append(snapshot_id)
        period["active_snapshot_id"] = snapshot_id
        self._audit(
            data,
            period["period_id"],
            "系统",
            "SEALED",
            {"snapshot_id": snapshot_id, "content_digest": snapshot["content_digest"], "chunks": len(digests)},
        )
        return snapshot

    def _rollover(
        self,
        data: dict,
        period: dict,
        source_snapshot: dict,
        *,
        extra_materials: list[dict],
        reason: str,
        actor: str,
        snapshot_status: str,
    ) -> None:
        """以封存内容为基线开启下一版草稿，并折叠排队材料与更正材料。"""
        source_snapshot["status"] = snapshot_status
        inputs = deepcopy(source_snapshot["payload"]["inputs"])
        adjustments = deepcopy(source_snapshot["payload"]["adjustments"])
        by_line_id = {line["line_id"]: idx for idx, line in enumerate(inputs)}
        for material in [*period["pending_materials"], *extra_materials]:
            for line in material.get("lines", []):
                line = deepcopy(line)
                if line["line_id"] in by_line_id:
                    inputs[by_line_id[line["line_id"]]] = line
                else:
                    by_line_id[line["line_id"]] = len(inputs)
                    inputs.append(line)
            for adjustment in material.get("adjustments", []):
                record = deepcopy(adjustment)
                period["adj_seq"] += 1
                record["adjustment_id"] = f"A{period['adj_seq']}"
                adjustments.append(record)
        from_version = period["version"]
        period["version"] = from_version + 1
        period["state"] = STATE_DRAFT
        period["inputs"] = inputs
        period["adjustments"] = adjustments
        period["pending_materials"] = []
        period["signatures"] = []
        period["candidate"] = None
        period["candidate_digest"] = None
        period["active_snapshot_id"] = None
        self._audit(
            data,
            period["period_id"],
            actor,
            "VERSION_ROLLED_OVER",
            {"from_version": from_version, "to_version": period["version"], "reason": reason},
        )

    # ------------------------------------------------------------------
    # 内部：校验与规范化
    # ------------------------------------------------------------------
    def _period(self, data: dict, period_id: str) -> dict:
        period = data["periods"].get(period_id)
        if not period:
            raise NotFound(f"期间不存在：{period_id}", code="PERIOD_NOT_FOUND")
        return period

    def _snapshot(self, data: dict, snapshot_id: str) -> dict:
        snapshot = data["snapshots"].get(snapshot_id)
        if not snapshot:
            raise NotFound(f"快照不存在：{snapshot_id}", code="SNAPSHOT_NOT_FOUND")
        return snapshot

    @staticmethod
    def _require_state(period: dict, expected: str, message: str) -> None:
        if period["state"] != expected:
            raise InvalidState(f"{message}（当前状态：{period['state']}）")

    @staticmethod
    def _require_independent_auditor(applicant: str, approver: str, role: str) -> None:
        if approver == applicant:
            raise Forbidden("批准人必须独立于申请人", code="NOT_INDEPENDENT")
        if role != AUDITOR_ROLE:
            raise Forbidden("批准人必须是监管审计员", code="NOT_AUDITOR")

    @staticmethod
    def _normalize_ruleset(ruleset: dict) -> dict:
        if not isinstance(ruleset, dict):
            raise Validation("ruleset 必须是对象")
        version = ruleset.get("version")
        tax_rate = ruleset.get("tax_rate")
        currency = ruleset.get("currency", "CNY")
        if not version:
            raise Validation("ruleset.version 不能为空")
        try:
            rate = Decimal(str(tax_rate))
        except Exception as exc:
            raise Validation("ruleset.tax_rate 必须是数值") from exc
        if not (Decimal("0") <= rate <= Decimal("1")):
            raise Validation("ruleset.tax_rate 必须在 [0, 1] 区间")
        return {
            "version": str(version),
            "tax_rate": _number_text(rate),
            "currency": str(currency),
            "rounding": "HALF_UP",
        }

    @staticmethod
    def _next_line_id(period: dict, reserved: set[str]) -> str:
        """分配下一个可用行号：扫描现有输入与排队材料，避免与显式 line_id 冲突。"""
        used = {line["line_id"] for line in period["inputs"]}
        for material in period["pending_materials"]:
            used.update(line["line_id"] for line in material.get("lines", []))
        used |= reserved
        next_number = 0
        for line_id in used:
            match = re.fullmatch(r"L(\d+)", line_id)
            if match:
                next_number = max(next_number, int(match.group(1)))
        candidate = f"L{next_number + 1}"
        while candidate in used:
            next_number += 1
            candidate = f"L{next_number + 1}"
        return candidate

    @staticmethod
    def _normalize_line(raw: dict, period: dict, reserved: set[str] | None = None) -> dict:
        if not isinstance(raw, dict):
            raise Validation("申报行必须是对象")
        vehicle_model = str(raw.get("vehicle_model") or "").strip()
        if not vehicle_model:
            raise Validation("申报行缺少 vehicle_model")
        try:
            quantity = Decimal(str(raw.get("quantity")))
            unit_price = Decimal(str(raw.get("unit_price")))
        except Exception as exc:
            raise Validation("quantity 与 unit_price 必须是数值") from exc
        if quantity <= 0:
            raise Validation("quantity 必须大于 0")
        if unit_price < 0:
            raise Validation("unit_price 不能为负")
        line_id = raw.get("line_id")
        if not line_id:
            line_id = SealingService._next_line_id(period, reserved or set())
        return {
            "line_id": str(line_id),
            "vehicle_model": vehicle_model,
            "quantity": _number_text(quantity),
            "unit_price": _money(unit_price),
            "note": str(raw.get("note") or ""),
        }

    def _normalize_adjustment(self, raw: dict, period: dict, author: str) -> dict:
        if not isinstance(raw, dict):
            raise Validation("人工调整必须是对象")
        target = raw.get("target")
        field = raw.get("field")
        if field not in ADJUSTABLE_FIELDS:
            raise Validation(f"field 必须是 {ADJUSTABLE_FIELDS} 之一")
        if target != "total" and target not in {line["line_id"] for line in period["inputs"]}:
            raise Validation("target 必须是 total 或已存在的 line_id")
        try:
            delta = Decimal(str(raw.get("delta")))
        except Exception as exc:
            raise Validation("delta 必须是数值") from exc
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise Validation("人工调整必须说明原因")
        period["adj_seq"] += 1
        return {
            "adjustment_id": f"A{period['adj_seq']}",
            "target": str(target),
            "field": field,
            "delta": _money(delta),
            "reason": reason,
            "author": author,
            "created_at": self._clock(),
        }

    def _normalize_materials(self, materials: dict, period: dict, author: str) -> dict:
        if not isinstance(materials, dict):
            raise Validation("materials 必须是对象")
        reserved: set[str] = set()
        lines = []
        for item in materials.get("lines", []):
            line = self._normalize_line(item, period, reserved)
            reserved.add(line["line_id"])
            lines.append(line)
        adjustments = []
        for item in materials.get("adjustments", []):
            record = dict(item)
            record.setdefault("author", author)
            record.setdefault("created_at", self._clock())
            if "delta" in record:
                try:
                    record["delta"] = _money(Decimal(str(record["delta"])))
                except Exception as exc:
                    raise Validation("调整 delta 必须是数值") from exc
            adjustments.append(record)
        if not lines and not adjustments:
            raise Validation("迟到材料不能为空")
        return {"lines": lines, "adjustments": adjustments}

    # ------------------------------------------------------------------
    # 内部：视图、核算与差异
    # ------------------------------------------------------------------
    def _build_candidate(self, period: dict) -> dict:
        return {
            "period_id": period["period_id"],
            "version": period["version"],
            "year": period["year"],
            "ruleset": deepcopy(period["ruleset"]),
            "inputs": deepcopy(period["inputs"]),
            "adjustments": deepcopy(period["adjustments"]),
            "computed": self._compute_view(period["ruleset"], period["inputs"], period["adjustments"]),
        }

    @staticmethod
    def _compute_view(ruleset: dict, inputs: list[dict], adjustments: list[dict]) -> dict:
        """确定性核算：行金额 → 行税额 → 行调整 → 汇总 → 总额调整。"""
        rate = Decimal(ruleset["tax_rate"])
        lines = []
        for line in inputs:
            amount = (Decimal(line["quantity"]) * Decimal(line["unit_price"])).quantize(CENT, rounding=ROUND_HALF_UP)
            tax = (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
            lines.append(
                {
                    **deepcopy(line),
                    "amount": _money(amount),
                    "tax": _money(tax),
                    "total": _money(amount + tax),
                }
            )
        by_line_id = {line["line_id"]: line for line in lines}
        for adjustment in adjustments:
            target = adjustment.get("target")
            field = adjustment.get("field")
            if field in ADJUSTABLE_FIELDS and target in by_line_id:
                line = by_line_id[target]
                line[field] = _money(Decimal(line[field]) + Decimal(adjustment["delta"]))
        totals = {
            "amount": _money(sum(Decimal(line["amount"]) for line in lines)),
            "tax": _money(sum(Decimal(line["tax"]) for line in lines)),
            "total": _money(sum(Decimal(line["total"]) for line in lines)),
        }
        for adjustment in adjustments:
            if adjustment.get("target") == "total" and adjustment.get("field") in ADJUSTABLE_FIELDS:
                field = adjustment["field"]
                totals[field] = _money(Decimal(totals[field]) + Decimal(adjustment["delta"]))
        totals["line_count"] = len(lines)
        return {"lines": lines, "totals": totals}

    def _period_view(self, period: dict) -> dict:
        return {
            "period_id": period["period_id"],
            "year": period["year"],
            "state": period["state"],
            "version": period["version"],
            "quorum": period["quorum"],
            "signers": deepcopy(period["signers"]),
            "ruleset": deepcopy(period["ruleset"]),
            "line_count": len(period["inputs"]),
            "adjustment_count": len(period["adjustments"]),
            "pending_material_count": len(period["pending_materials"]),
            "signatures": [item["signer_id"] for item in period["signatures"]],
            "candidate_digest": period["candidate_digest"],
            "active_snapshot_id": period["active_snapshot_id"],
            "snapshot_ids": list(period["snapshot_ids"]),
            "computed": self._compute_view(period["ruleset"], period["inputs"], period["adjustments"]),
        }

    def _manifest(self, snapshot: dict, *, include_chunks: bool = False) -> dict:
        manifest = {
            "snapshot_id": snapshot["snapshot_id"],
            "period_id": snapshot["period_id"],
            "version": snapshot["version"],
            "state": snapshot["state"],
            "status": snapshot["status"],
            "sealed_at": snapshot["sealed_at"],
            "quorum": snapshot["quorum"],
            "chunk_size": snapshot["chunk_size"],
            "chunk_count": len(snapshot["chunk_digests"]),
            "payload_bytes": len(snapshot["payload_json"].encode("utf-8")),
            "payload_sha256": snapshot["payload_sha256"],
            "content_digest": snapshot["content_digest"],
            "signers": [item["signer_id"] for item in snapshot["payload"]["signatures"]],
            "totals": deepcopy(snapshot["payload"]["computed"]["totals"]),
        }
        if include_chunks:
            manifest["chunk_digests"] = list(snapshot["chunk_digests"])
        return manifest

    @staticmethod
    def _snapshot_view(snapshot: dict) -> dict:
        payload = snapshot["payload"]
        return {
            "version": payload["version"],
            "ruleset": payload["ruleset"],
            "inputs": payload["inputs"],
            "adjustments": payload["adjustments"],
            "computed": payload["computed"],
        }

    @staticmethod
    def _diff_views(source: dict, target: dict, *, from_label: str, to_label: str) -> dict:
        lines_from = {line["line_id"]: line for line in source["computed"]["lines"]}
        lines_to = {line["line_id"]: line for line in target["computed"]["lines"]}
        added = [deepcopy(lines_to[lid]) for lid in lines_to if lid not in lines_from]
        removed = [deepcopy(lines_from[lid]) for lid in lines_from if lid not in lines_to]
        changed = []
        for lid in lines_from.keys() & lines_to.keys():
            fields = {}
            for key in sorted(set(lines_from[lid]) | set(lines_to[lid])):
                before, after = lines_from[lid].get(key), lines_to[lid].get(key)
                if before != after:
                    fields[key] = {"from": before, "to": after}
            if fields:
                changed.append({"line_id": lid, "fields": fields})
        totals = {}
        for key in sorted(set(source["computed"]["totals"]) | set(target["computed"]["totals"])):
            before = source["computed"]["totals"].get(key)
            after = target["computed"]["totals"].get(key)
            if before != after:
                entry = {"from": before, "to": after}
                try:
                    delta = Decimal(str(after)) - Decimal(str(before))
                    entry["delta"] = str(int(delta)) if key == "line_count" else _money(delta)
                except Exception:
                    pass
                totals[key] = entry
        adj_from = {item["adjustment_id"] for item in source["adjustments"]}
        adj_to = {item["adjustment_id"] for item in target["adjustments"]}
        return {
            "from": from_label,
            "to": to_label,
            "meta": {
                "from_version": source["version"],
                "to_version": target["version"],
                "from_ruleset": source["ruleset"]["version"],
                "to_ruleset": target["ruleset"]["version"],
            },
            "lines": {"added": added, "removed": removed, "changed": changed},
            "totals": totals,
            "adjustments": {"added": sorted(adj_to - adj_from), "removed": sorted(adj_from - adj_to)},
        }

    def _chunks(self, snapshot: dict) -> list[bytes]:
        return canonical.split_chunks(snapshot["payload_json"].encode("utf-8"), snapshot["chunk_size"])

    def _audit(self, data: dict, period_id: str, actor: str, action: str, details: dict) -> None:
        data["audit"].append(
            {
                "seq": len(data["audit"]) + 1,
                "at": self._clock(),
                "period_id": period_id,
                "actor": actor,
                "action": action,
                "details": details,
            }
        )
