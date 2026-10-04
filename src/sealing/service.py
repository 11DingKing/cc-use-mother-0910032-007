"""封账领域服务：状态机、签署 quorum、分块摘要、受控重开与导出。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import errors
from .canonical import canonical_json, content_digest, sha256_hex
from .merkle import CHUNK_SIZE, build_tree, leaf_hash, proof_for, split_chunks, verify_proof
from .models import (
    APPROVED,
    CONFIRMED,
    DRAFT,
    IN_PROGRESS,
    PENDING,
    PENDING_ACCOUNTING,
    REJECTED,
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_DECLARANT,
    ROUTE_CORRECTION,
    ROUTE_NEXT_VERSION,
    SEALED,
    SIGNER_ROLES,
    AccountingRuleSet,
    Actor,
    CorrectionOrder,
    DeclarationInput,
    ExportSession,
    ManualAdjustment,
    ReopenRequest,
    SealedInfo,
    Signature,
    Snapshot,
    correction_from_dict,
    export_from_dict,
    reopen_from_dict,
    snapshot_from_dict,
)
from .store import LedgerStore

OPEN_STATES = (DRAFT, PENDING_ACCOUNTING, CONFIRMED, IN_PROGRESS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerSealingService:
    """年度核算审计封账的应用服务。"""

    def __init__(
        self,
        store: LedgerStore | None = None,
        clock: Callable[[], str] | None = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self.store = store or LedgerStore()
        self.clock = clock or _utc_now
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock()

    def _save(self) -> None:
        self.store.flush()

    def _load_snapshot(self, snapshot_id: str) -> Snapshot:
        raw = self.store.get("snapshots", snapshot_id)
        if raw is None:
            raise errors.not_found(f"快照不存在：{snapshot_id}")
        return snapshot_from_dict(raw)

    def _store_snapshot(self, snapshot: Snapshot) -> None:
        self.store.put("snapshots", snapshot.snapshot_id, snapshot.to_dict(include_payload=True))
        self._save()

    @staticmethod
    def _require_role(actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise errors.forbidden(
                f"角色 {actor.role} 无权执行该操作，需要：{'、'.join(roles)}"
            )

    @staticmethod
    def _require_state(snapshot: Snapshot, *states: str) -> None:
        if snapshot.state not in states:
            raise errors.conflict(
                f"快照 {snapshot.snapshot_id} 当前为「{snapshot.state}」，"
                f"该操作要求状态：{'、'.join(states)}"
            )

    def _open_snapshot_of_period(self, period: str) -> Snapshot | None:
        for raw in self.store.find("snapshots", period=period):
            candidate = snapshot_from_dict(raw)
            if candidate.state in OPEN_STATES:
                return candidate
        return None

    def _next_version(self, period: str) -> int:
        versions = [raw["version"] for raw in self.store.find("snapshots", period=period)]
        return max(versions, default=0) + 1

    # ------------------------------------------------------------------
    # 快照生命周期
    # ------------------------------------------------------------------
    def create_snapshot(
        self, period: str, actor: Actor, statutory_signatures: int = 3
    ) -> dict:
        """创建某核算期间的新版草稿。同一期间只允许一个未封存版本。"""
        self._require_role(actor, ROLE_DECLARANT, ROLE_ACCOUNTANT)
        if not period or not period.strip():
            raise errors.validation("核算期间不能为空")
        if statutory_signatures < 1:
            raise errors.validation("法定签署人数至少为 1")
        existing = self._open_snapshot_of_period(period)
        if existing is not None:
            raise errors.conflict(
                f"期间 {period} 已存在未封存版本 {existing.snapshot_id}（{existing.state}）"
            )
        snapshot = Snapshot(
            snapshot_id=self.store.next_id("SNP"),
            period=period,
            version=self._next_version(period),
            state=DRAFT,
            statutory_signatures=statutory_signatures,
            created_by=actor.actor_id,
            created_at=self._now(),
        )
        self._store_snapshot(snapshot)
        return snapshot.to_dict()

    def get_snapshot(self, snapshot_id: str) -> dict:
        snapshot = self._load_snapshot(snapshot_id)
        view = snapshot.to_dict()
        view["signature_count"] = len(snapshot.signatures)
        view["exportable"] = snapshot.state == SEALED
        return view

    def list_snapshots(self, period: str | None = None) -> list[dict]:
        raws = (
            self.store.find("snapshots", period=period)
            if period
            else self.store.all("snapshots")
        )
        result = []
        for raw in sorted(raws, key=lambda item: (item["period"], item["version"])):
            snapshot = snapshot_from_dict(raw)
            result.append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "period": snapshot.period,
                    "version": snapshot.version,
                    "state": snapshot.state,
                    "signature_count": len(snapshot.signatures),
                    "statutory_signatures": snapshot.statutory_signatures,
                    "merkle_root": snapshot.sealed.merkle_root if snapshot.sealed else None,
                    "reopened_from": snapshot.reopened_from,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 申报输入与人工调整（仅草稿/待核算阶段可改）
    # ------------------------------------------------------------------
    def add_input(self, snapshot_id: str, actor: Actor, **fields: Any) -> dict:
        self._require_role(actor, ROLE_DECLARANT)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, DRAFT)
        model_code = str(fields.get("model_code") or "").strip()
        if not model_code:
            raise errors.validation("车型代码不能为空")
        if any(item.model_code == model_code for item in snapshot.inputs):
            raise errors.conflict(f"车型 {model_code} 已申报，不能重复录入")
        quantity = int(fields.get("quantity", 0))
        unit_price_cents = int(fields.get("unit_price_cents", 0))
        if quantity <= 0 or unit_price_cents < 0:
            raise errors.validation("数量必须为正，单价不能为负")
        item = DeclarationInput(
            input_id=self.store.next_id("INP"),
            model_code=model_code,
            model_name=str(fields.get("model_name") or model_code),
            category=str(fields.get("category") or "未分类"),
            quantity=quantity,
            unit_price_cents=unit_price_cents,
            declared_by=actor.actor_id,
            declared_at=self._now(),
        )
        snapshot.inputs.append(item)
        self._store_snapshot(snapshot)
        return item.to_dict()

    def attach_rule_set(
        self, snapshot_id: str, actor: Actor, rule_set_id: str, version: str, rules: list[dict]
    ) -> dict:
        self._require_role(actor, ROLE_ACCOUNTANT)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, DRAFT, PENDING_ACCOUNTING)
        for rule in rules:
            if "rule_id" not in rule or "type" not in rule:
                raise errors.validation("每条核算规则必须包含 rule_id 与 type")
        snapshot.rule_set = AccountingRuleSet(
            rule_set_id=rule_set_id, version=version, rules=list(rules)
        )
        self._store_snapshot(snapshot)
        return snapshot.rule_set.to_dict()

    def add_adjustment(self, snapshot_id: str, actor: Actor, **fields: Any) -> dict:
        """人工调整只在确认前允许直接入账；确认后一律走迟到材料通道。"""
        self._require_role(actor, ROLE_ACCOUNTANT)
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot.state not in (DRAFT, PENDING_ACCOUNTING):
            raise errors.conflict(
                f"快照已推进到「{snapshot.state}」，人工调整须登记为迟到材料"
                "（进入下一版或更正单）"
            )
        adjustment = self._new_adjustment(actor, fields)
        snapshot.adjustments.append(adjustment)
        self._store_snapshot(snapshot)
        return adjustment.to_dict()

    def _new_adjustment(self, actor: Actor, fields: dict) -> ManualAdjustment:
        reason = str(fields.get("reason") or "").strip()
        if not reason:
            raise errors.validation("人工调整必须填写原因")
        return ManualAdjustment(
            adjustment_id=self.store.next_id("ADJ"),
            target_model_code=fields.get("target_model_code"),
            delta_cents=int(fields.get("delta_cents", 0)),
            reason=reason,
            operator=actor.actor_id,
            created_at=self._now(),
        )

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------
    def submit(self, snapshot_id: str, actor: Actor) -> dict:
        self._require_role(actor, ROLE_DECLARANT)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, DRAFT)
        if not snapshot.inputs:
            raise errors.conflict("没有申报输入，不能提交核算")
        snapshot.state = PENDING_ACCOUNTING
        self._store_snapshot(snapshot)
        return self.get_snapshot(snapshot_id)

    def compute_totals(self, snapshot_id: str, actor: Actor) -> dict:
        """核算专员执行核算：应用规则与调整，锁定待签署摘要。"""
        self._require_role(actor, ROLE_ACCOUNTANT)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, PENDING_ACCOUNTING)
        if snapshot.rule_set is None:
            raise errors.conflict("尚未绑定核算规则集")
        snapshot.totals = self._apply_rules(snapshot)
        snapshot.signable_digest = content_digest(self._signable_content(snapshot))
        snapshot.state = CONFIRMED
        self._store_snapshot(snapshot)
        return self.get_snapshot(snapshot_id)

    @staticmethod
    def _apply_rules(snapshot: Snapshot) -> dict:
        """确定性地应用核算规则：规则按 rule_id 排序，全程整数运算。"""
        gross_by_category: dict[str, int] = {}
        for item in snapshot.inputs:
            gross_by_category[item.category] = (
                gross_by_category.get(item.category, 0) + item.gross_cents
            )
        gross_total = sum(gross_by_category.values())
        effects = []
        running = gross_total
        assert snapshot.rule_set is not None
        for rule in sorted(snapshot.rule_set.rules, key=lambda item: item["rule_id"]):
            params = rule.get("params", {})
            rule_type = rule["type"]
            if rule_type == "tax_rate":
                base = gross_by_category.get(params.get("category", ""), 0)
                effect = base * int(params.get("rate_bp", 0)) // 10000
            elif rule_type == "surcharge_bp":
                effect = running * int(params.get("rate_bp", 0)) // 10000
            elif rule_type == "deduction":
                effect = -int(params.get("amount_cents", 0))
            else:
                raise errors.validation(f"未知核算规则类型：{rule_type}")
            running += effect
            effects.append(
                {"rule_id": rule["rule_id"], "type": rule_type, "effect_cents": effect}
            )
        adjustment_total = sum(item.delta_cents for item in snapshot.adjustments)
        net = running + adjustment_total
        return {
            "gross_cents": gross_total,
            "gross_by_category": gross_by_category,
            "rule_effects": effects,
            "rules_total_cents": sum(item["effect_cents"] for item in effects),
            "adjustment_total_cents": adjustment_total,
            "net_cents": net,
            "input_count": len(snapshot.inputs),
            "rule_set_id": snapshot.rule_set.rule_set_id,
            "rule_set_version": snapshot.rule_set.version,
        }

    @staticmethod
    def _signable_content(snapshot: Snapshot) -> dict:
        """待签署/待封存的内容：确认后任何字段都不可再变。"""
        return {
            "snapshot_id": snapshot.snapshot_id,
            "period": snapshot.period,
            "version": snapshot.version,
            "statutory_signatures": snapshot.statutory_signatures,
            "inputs": sorted(
                (item.to_dict() for item in snapshot.inputs),
                key=lambda item: item["input_id"],
            ),
            "adjustments": sorted(
                (item.to_dict() for item in snapshot.adjustments),
                key=lambda item: item["adjustment_id"],
            ),
            "rule_set": snapshot.rule_set.to_dict() if snapshot.rule_set else None,
            "totals": snapshot.totals,
        }

    # ------------------------------------------------------------------
    # 签署与封存
    # ------------------------------------------------------------------
    def sign(self, snapshot_id: str, actor: Actor, signer_name: str | None = None) -> dict:
        """签署。达到法定人数即自动封存并生成分块摘要。"""
        self._require_role(actor, *sorted(SIGNER_ROLES))
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, CONFIRMED, IN_PROGRESS)
        if any(item.signer_id == actor.actor_id for item in snapshot.signatures):
            raise errors.conflict(f"签署人 {actor.actor_id} 已签署，不能重复签署")
        assert snapshot.signable_digest is not None
        snapshot.signatures.append(
            Signature(
                signer_id=actor.actor_id,
                signer_name=signer_name or actor.actor_id,
                role=actor.role,
                signed_at=self._now(),
                payload_digest=snapshot.signable_digest,
            )
        )
        if len(snapshot.signatures) >= snapshot.statutory_signatures:
            self._seal(snapshot)
        else:
            snapshot.state = IN_PROGRESS
            self._store_snapshot(snapshot)
        return self.get_snapshot(snapshot_id)

    def _seal(self, snapshot: Snapshot) -> None:
        """锁定内容：冻结载荷、分块、生成 Merkle 摘要。"""
        sealed_at = self._now()
        pre_seal_digest = snapshot.signable_digest
        assert pre_seal_digest is not None
        sealed_doc = {
            **self._signable_content(snapshot),
            "signatures": sorted(
                (item.to_dict() for item in snapshot.signatures),
                key=lambda item: item["signer_id"],
            ),
            "pre_seal_digest": pre_seal_digest,
            "sealed_at": sealed_at,
        }
        payload = canonical_json(sealed_doc)
        chunks = split_chunks(payload, self.chunk_size)
        tree = build_tree(chunks)
        snapshot.sealed = SealedInfo(
            sealed_at=sealed_at,
            pre_seal_digest=pre_seal_digest,
            payload_sha256=sha256_hex(payload),
            payload=payload.decode("utf-8"),
            chunk_size=self.chunk_size,
            chunk_hashes=tree["chunk_hashes"],
            merkle_root=tree["root"],
            levels=tree["levels"],
        )
        snapshot.state = SEALED
        self._store_snapshot(snapshot)

    # ------------------------------------------------------------------
    # 迟到材料：进入下一版或更正单
    # ------------------------------------------------------------------
    def register_late_material(
        self, snapshot_id: str, actor: Actor, kind: str, route: str, fields: dict
    ) -> dict:
        """登记迟到材料。

        kind:  input（车型明细）/ adjustment（人工调整）
        route: next_version（进入下一版草稿）/ correction（更正单，仅已封存）
        """
        self._require_role(actor, ROLE_DECLARANT, ROLE_ACCOUNTANT)
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot.state in (DRAFT, PENDING_ACCOUNTING):
            raise errors.conflict("当前版本仍在开放录入，材料不算迟到，请直接录入")
        if route == ROUTE_NEXT_VERSION:
            target = self._open_snapshot_of_period(snapshot.period)
            if target is None:
                target = self._fork_empty_next_version(snapshot)
            if kind == "input":
                result = self.add_input(target.snapshot_id, Actor(actor.actor_id, ROLE_DECLARANT), **fields)
            elif kind == "adjustment":
                result = self.add_adjustment(
                    target.snapshot_id, Actor(actor.actor_id, ROLE_ACCOUNTANT), **fields
                )
            else:
                raise errors.validation(f"未知材料类型：{kind}")
            record = {
                "material_id": self.store.next_id("LMT"),
                "source_snapshot_id": snapshot_id,
                "route": route,
                "kind": kind,
                "target_snapshot_id": target.snapshot_id,
                "registered_by": actor.actor_id,
                "registered_at": self._now(),
            }
            self.store.put("late_materials", record["material_id"], record)
            self._save()
            return {"material": record, "accepted": result}
        if route == ROUTE_CORRECTION:
            if snapshot.state != SEALED:
                raise errors.conflict("更正单只能针对已封存快照；未封存版本请走下一版")
            if kind != "adjustment":
                raise errors.validation("更正单只承载人工调整类材料；车型明细请进入下一版")
            correction = self.create_correction(
                snapshot_id, actor, reason=f"迟到材料：{fields.get('reason', '')}", adjustments=[fields]
            )
            return {"material_route": route, "correction": correction}
        raise errors.validation(f"未知路由：{route}")

    def _fork_empty_next_version(self, source: Snapshot) -> Snapshot:
        """为迟到材料自动开立下一版草稿（继承规则集，不带明细）。"""
        snapshot = Snapshot(
            snapshot_id=self.store.next_id("SNP"),
            period=source.period,
            version=self._next_version(source.period),
            state=DRAFT,
            statutory_signatures=source.statutory_signatures,
            created_by="system:late-material",
            created_at=self._now(),
            rule_set=source.rule_set,
        )
        self._store_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 更正单：独立批准后生效
    # ------------------------------------------------------------------
    def create_correction(
        self, snapshot_id: str, actor: Actor, reason: str, adjustments: list[dict]
    ) -> dict:
        self._require_role(actor, ROLE_ACCOUNTANT, ROLE_AUDITOR)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        if not adjustments:
            raise errors.validation("更正单至少包含一条调整")
        items = [self._new_adjustment(actor, fields) for fields in adjustments]
        correction = CorrectionOrder(
            correction_id=self.store.next_id("COR"),
            snapshot_id=snapshot_id,
            reason=reason,
            adjustments=items,
            requested_by=actor.actor_id,
            requested_at=self._now(),
        )
        self.store.put("corrections", correction.correction_id, correction.to_dict())
        self._save()
        return correction.to_dict()

    def decide_correction(self, correction_id: str, actor: Actor, approve: bool) -> dict:
        """批准/驳回更正单。批准人必须是独立于申请人的监管审计员。"""
        self._require_role(actor, ROLE_AUDITOR)
        raw = self.store.get("corrections", correction_id)
        if raw is None:
            raise errors.not_found(f"更正单不存在：{correction_id}")
        correction = correction_from_dict(raw)
        if correction.state != PENDING:
            raise errors.conflict(f"更正单已处理（{correction.state}）")
        if correction.requested_by == actor.actor_id:
            raise errors.forbidden("批准人必须独立于申请人（职责分离）")
        correction.state = APPROVED if approve else REJECTED
        correction.decided_by = actor.actor_id
        correction.decided_at = self._now()
        self.store.put("corrections", correction.correction_id, correction.to_dict())
        self._save()
        return correction.to_dict()

    def list_corrections(self, snapshot_id: str) -> list[dict]:
        return self.store.find("corrections", snapshot_id=snapshot_id)

    # ------------------------------------------------------------------
    # 重开：独立批准后派生下一版，原封存版本不动
    # ------------------------------------------------------------------
    def request_reopen(self, snapshot_id: str, actor: Actor, reason: str) -> dict:
        self._require_role(actor, ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_DECLARANT)
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        if not reason or not reason.strip():
            raise errors.validation("重开必须说明原因")
        pending = [
            item
            for item in self.store.find("reopen_requests", snapshot_id=snapshot_id)
            if item["state"] == PENDING
        ]
        if pending:
            raise errors.conflict(f"已存在待批准的重开申请：{pending[0]['request_id']}")
        request = ReopenRequest(
            request_id=self.store.next_id("ROP"),
            snapshot_id=snapshot_id,
            reason=reason,
            requested_by=actor.actor_id,
            requested_at=self._now(),
        )
        self.store.put("reopen_requests", request.request_id, request.to_dict())
        self._save()
        return request.to_dict()

    def decide_reopen(self, request_id: str, actor: Actor, approve: bool) -> dict:
        """批准重开：派生下一版草稿（继承封存内容），原版本保持已封存。"""
        self._require_role(actor, ROLE_AUDITOR)
        raw = self.store.get("reopen_requests", request_id)
        if raw is None:
            raise errors.not_found(f"重开申请不存在：{request_id}")
        request = reopen_from_dict(raw)
        if request.state != PENDING:
            raise errors.conflict(f"重开申请已处理（{request.state}）")
        if request.requested_by == actor.actor_id:
            raise errors.forbidden("批准人必须独立于申请人（职责分离）")
        request.decided_by = actor.actor_id
        request.decided_at = self._now()
        if not approve:
            request.state = REJECTED
            self.store.put("reopen_requests", request.request_id, request.to_dict())
            self._save()
            return request.to_dict()
        source = self._load_snapshot(request.snapshot_id)
        if self._open_snapshot_of_period(source.period) is not None:
            raise errors.conflict("该期间已存在未封存版本，请先处理后再批准重开")
        request.state = APPROVED
        forked = Snapshot(
            snapshot_id=self.store.next_id("SNP"),
            period=source.period,
            version=self._next_version(source.period),
            state=DRAFT,
            statutory_signatures=source.statutory_signatures,
            created_by=actor.actor_id,
            created_at=self._now(),
            inputs=list(source.inputs),
            adjustments=list(source.adjustments),
            rule_set=source.rule_set,
            reopened_from=source.snapshot_id,
            reopen_approval={
                "request_id": request.request_id,
                "approved_by": actor.actor_id,
                "approved_at": request.decided_at,
            },
        )
        request.new_snapshot_id = forked.snapshot_id
        self.store.put("reopen_requests", request.request_id, request.to_dict())
        self._store_snapshot(forked)
        return {"request": request.to_dict(), "new_snapshot": forked.to_dict()}

    # ------------------------------------------------------------------
    # 导出：重复导出与中断续传不改变签发内容
    # ------------------------------------------------------------------
    def create_export(self, snapshot_id: str, actor: Actor) -> dict:
        self._require_role(actor, *sorted(SIGNER_ROLES | {ROLE_DECLARANT}))
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        assert snapshot.sealed is not None
        session = ExportSession(
            export_id=self.store.next_id("EXP"),
            snapshot_id=snapshot_id,
            created_by=actor.actor_id,
            created_at=self._now(),
            chunk_count=len(snapshot.sealed.chunk_hashes),
            chunk_size=snapshot.sealed.chunk_size,
            merkle_root=snapshot.sealed.merkle_root,
            payload_sha256=snapshot.sealed.payload_sha256,
        )
        self.store.put("export_sessions", session.export_id, session.to_dict())
        self._save()
        return session.to_dict()

    def _load_export(self, export_id: str) -> ExportSession:
        raw = self.store.get("export_sessions", export_id)
        if raw is None:
            raise errors.not_found(f"导出会话不存在：{export_id}")
        return export_from_dict(raw)

    def export_manifest(self, export_id: str) -> dict:
        """导出清单：客户端据此校验续传重组结果。"""
        session = self._load_export(export_id)
        snapshot = self._load_snapshot(session.snapshot_id)
        assert snapshot.sealed is not None
        return {
            **session.to_dict(),
            "chunk_hashes": snapshot.sealed.chunk_hashes,
        }

    def get_export_chunk(self, export_id: str, index: int) -> dict:
        """按块拉取导出内容。内容只来自封存时冻结的载荷，幂等。"""
        session = self._load_export(export_id)
        snapshot = self._load_snapshot(session.snapshot_id)
        assert snapshot.sealed is not None
        payload = snapshot.sealed.payload.encode("utf-8")
        chunks = split_chunks(payload, snapshot.sealed.chunk_size)
        if not 0 <= index < len(chunks):
            raise errors.not_found(f"分块序号越界：{index}（共 {len(chunks)} 块）")
        chunk = chunks[index]
        digest = leaf_hash(chunk)
        if digest != snapshot.sealed.chunk_hashes[index]:
            raise errors.conflict("冻结载荷与封存摘要不一致，存储已损坏")
        return {
            "export_id": export_id,
            "snapshot_id": snapshot.snapshot_id,
            "index": index,
            "chunk_count": len(chunks),
            "chunk_hex": chunk.hex(),
            "chunk_hash": digest,
            "merkle_root": snapshot.sealed.merkle_root,
        }

    # ------------------------------------------------------------------
    # 分块验证与封账前后对比
    # ------------------------------------------------------------------
    def chunk_proof(self, snapshot_id: str, index: int) -> dict:
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        assert snapshot.sealed is not None
        try:
            proof = proof_for(snapshot.sealed.levels, index)
        except IndexError:
            raise errors.not_found(f"分块序号越界：{index}") from None
        return {
            "snapshot_id": snapshot_id,
            "index": index,
            "chunk_hash": snapshot.sealed.chunk_hashes[index],
            "proof": proof,
            "merkle_root": snapshot.sealed.merkle_root,
        }

    def verify_chunk(self, snapshot_id: str, index: int, chunk_hex: str) -> dict:
        """验证任一分块：叶子哈希 + Merkle 路径双重校验。"""
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        assert snapshot.sealed is not None
        try:
            chunk = bytes.fromhex(chunk_hex)
        except ValueError:
            raise errors.validation("chunk_hex 不是合法的十六进制") from None
        if not 0 <= index < len(snapshot.sealed.chunk_hashes):
            raise errors.not_found(f"分块序号越界：{index}")
        leaf_ok = leaf_hash(chunk) == snapshot.sealed.chunk_hashes[index]
        proof = proof_for(snapshot.sealed.levels, index)
        proof_ok = verify_proof(chunk, proof, snapshot.sealed.merkle_root)
        return {
            "snapshot_id": snapshot_id,
            "index": index,
            "leaf_ok": leaf_ok,
            "proof_ok": proof_ok,
            "valid": leaf_ok and proof_ok,
            "merkle_root": snapshot.sealed.merkle_root,
        }

    def seal_integrity(self, snapshot_id: str) -> dict:
        """封存完整性自检：签发内容未被任何后续操作改变。"""
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        assert snapshot.sealed is not None
        sealed = snapshot.sealed
        doc = json.loads(sealed.payload)
        content = {
            key: value
            for key, value in doc.items()
            if key not in ("signatures", "sealed_at", "pre_seal_digest")
        }
        checks = {
            "pre_seal_digest_matches": content_digest(content) == sealed.pre_seal_digest,
            "payload_hash_matches": sha256_hex(sealed.payload.encode("utf-8"))
            == sealed.payload_sha256,
            "quorum_reached": len(doc["signatures"]) >= snapshot.statutory_signatures,
            "signatures_bind_content": all(
                item["payload_digest"] == sealed.pre_seal_digest
                for item in doc["signatures"]
            ),
        }
        chunks = split_chunks(sealed.payload.encode("utf-8"), sealed.chunk_size)
        tree = build_tree(chunks)
        checks["chunks_match"] = tree["chunk_hashes"] == sealed.chunk_hashes
        checks["root_matches"] = tree["root"] == sealed.merkle_root
        return {
            "snapshot_id": snapshot_id,
            "checks": checks,
            "ok": all(checks.values()),
            "merkle_root": sealed.merkle_root,
            "pre_seal_digest": sealed.pre_seal_digest,
        }

    def diff_snapshots(self, base_id: str, other_id: str) -> dict:
        """对比两个版本（封账前后）的结构性差异。"""
        base = self._load_snapshot(base_id)
        other = self._load_snapshot(other_id)

        def index_by(items: list[dict], key: str) -> dict:
            return {item[key]: item for item in items}

        base_inputs = index_by([item.to_dict() for item in base.inputs], "input_id")
        other_inputs = index_by([item.to_dict() for item in other.inputs], "input_id")
        base_adjs = index_by([item.to_dict() for item in base.adjustments], "adjustment_id")
        other_adjs = index_by([item.to_dict() for item in other.adjustments], "adjustment_id")

        def collection_diff(before: dict, after: dict) -> dict:
            added = [after[key] for key in sorted(after.keys() - before.keys())]
            removed = [before[key] for key in sorted(before.keys() - after.keys())]
            changed = [
                {"id": key, "before": before[key], "after": after[key]}
                for key in sorted(before.keys() & after.keys())
                if before[key] != after[key]
            ]
            return {"added": added, "removed": removed, "changed": changed}

        base_net = base.totals["net_cents"] if base.totals else None
        other_net = other.totals["net_cents"] if other.totals else None
        return {
            "base": {"snapshot_id": base_id, "state": base.state, "version": base.version},
            "other": {"snapshot_id": other_id, "state": other.state, "version": other.version},
            "inputs": collection_diff(base_inputs, other_inputs),
            "adjustments": collection_diff(base_adjs, other_adjs),
            "rule_set_changed": (base.rule_set.to_dict() if base.rule_set else None)
            != (other.rule_set.to_dict() if other.rule_set else None),
            "net_cents": {
                "before": base_net,
                "after": other_net,
                "delta": (other_net - base_net)
                if base_net is not None and other_net is not None
                else None,
            },
        }

    def effective_view(self, snapshot_id: str) -> dict:
        """封账后视图：封存净额 + 已批准更正单 = 当前有效净额。"""
        snapshot = self._load_snapshot(snapshot_id)
        self._require_state(snapshot, SEALED)
        assert snapshot.totals is not None
        corrections = self.list_corrections(snapshot_id)
        approved = [item for item in corrections if item["state"] == APPROVED]
        delta = sum(
            adj["delta_cents"]
            for item in approved
            for adj in item["adjustments"]
        )
        return {
            "snapshot_id": snapshot_id,
            "sealed_net_cents": snapshot.totals["net_cents"],
            "approved_corrections": approved,
            "pending_corrections": [item for item in corrections if item["state"] == PENDING],
            "correction_delta_cents": delta,
            "effective_net_cents": snapshot.totals["net_cents"] + delta,
        }
