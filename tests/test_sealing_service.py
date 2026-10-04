"""封账领域服务的回归测试：生命周期、封存不可变、分块校验、迟到材料、受控重开。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_service import canonical
from sealing_service.errors import DomainError, Forbidden, InvalidState, NotFound, Validation
from sealing_service.service import SealingService
from sealing_service.store import Store

SIGNERS = [
    {"signer_id": "s-fin", "name": "财务负责人", "role": "核算专员"},
    {"signer_id": "s-ops", "name": "运营负责人", "role": "交易运营员"},
    {"signer_id": "s-audit", "name": "审计负责人", "role": "监管审计员"},
]
RULESET = {"version": "v2026.1", "tax_rate": "0.13", "currency": "CNY"}
LINES = [
    {"line_id": "L1", "vehicle_model": "车型甲", "quantity": 2, "unit_price": "100.00"},
    {"line_id": "L2", "vehicle_model": "车型乙", "quantity": 1, "unit_price": "50.50"},
]


def make_service(chunk_size: int = 64) -> SealingService:
    counter = {"n": 0}

    def ids(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:04d}"

    clock = lambda: "2026-10-04T09:00:00+00:00"  # noqa: E731
    return SealingService(Store(), chunk_size=chunk_size, clock=clock, id_factory=ids)


def make_period(service: SealingService, period_id: str = "P-2026", quorum: int = 3) -> dict:
    return service.create_period(
        period_id=period_id,
        year=2026,
        quorum=quorum,
        signers=SIGNERS,
        ruleset=RULESET,
        actor="企业申报员",
    )


def seal_period(service: SealingService, period_id: str = "P-2026") -> dict:
    """走完整流程直到封存，返回快照清单。"""
    make_period(service, period_id)
    service.add_inputs(period_id, lines=LINES, actor="企业申报员")
    service.submit(period_id, actor="企业申报员")
    service.confirm(period_id, actor="核算专员")
    service.sign(period_id, signer_id="s-fin", signature="sig-fin")
    service.sign(period_id, signer_id="s-ops", signature="sig-ops")
    result = service.sign(period_id, signer_id="s-audit", signature="sig-audit")
    return result["snapshot"]


class LifecycleTest(unittest.TestCase):
    def test_full_lifecycle_seals_with_quorum(self) -> None:
        service = make_service()
        make_period(service)
        service.add_inputs("P-2026", lines=LINES, actor="企业申报员")
        view = service.submit("P-2026", actor="企业申报员")
        self.assertEqual(view["state"], "待核算")
        view = service.confirm("P-2026", actor="核算专员")
        self.assertEqual(view["state"], "已确认")
        self.assertTrue(view["candidate_digest"])

        view = service.sign("P-2026", signer_id="s-fin", signature="x")
        self.assertEqual(view["state"], "执行中")
        self.assertIsNone(view["snapshot"])
        view = service.sign("P-2026", signer_id="s-ops", signature="x")
        self.assertEqual(view["state"], "执行中")
        view = service.sign("P-2026", signer_id="s-audit", signature="x")
        self.assertEqual(view["state"], "已封存")

        snapshot = view["snapshot"]
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["quorum"], 3)
        self.assertEqual(snapshot["signers"], ["s-fin", "s-ops", "s-audit"])
        self.assertGreater(snapshot["chunk_count"], 1)
        self.assertEqual(snapshot["totals"]["amount"], "250.50")
        self.assertEqual(snapshot["totals"]["tax"], "32.57")
        self.assertEqual(snapshot["totals"]["total"], "283.07")

    def test_computation_is_deterministic(self) -> None:
        service = make_service()
        make_period(service)
        view = service.add_inputs("P-2026", lines=LINES, actor="企业申报员")
        lines = {line["line_id"]: line for line in view["computed"]["lines"]}
        self.assertEqual(lines["L1"]["amount"], "200.00")
        self.assertEqual(lines["L1"]["tax"], "26.00")
        self.assertEqual(lines["L1"]["total"], "226.00")
        self.assertEqual(lines["L2"]["tax"], "6.57")  # 50.50 * 0.13 = 6.565 → 6.57 (HALF_UP)

    def test_adjustments_apply_to_lines_and_totals(self) -> None:
        service = make_service()
        make_period(service)
        service.add_inputs("P-2026", lines=LINES, actor="企业申报员")
        service.submit("P-2026", actor="企业申报员")
        view = service.add_adjustment(
            "P-2026",
            adjustment={"target": "L1", "field": "tax", "delta": "1.00", "reason": "税率口径调整"},
            actor="核算专员",
        )
        lines = {line["line_id"]: line for line in view["computed"]["lines"]}
        self.assertEqual(lines["L1"]["tax"], "27.00")
        self.assertEqual(view["computed"]["totals"]["tax"], "33.57")
        view = service.add_adjustment(
            "P-2026",
            adjustment={"target": "total", "field": "total", "delta": "-0.07", "reason": "尾差处理"},
            actor="核算专员",
        )
        self.assertEqual(view["computed"]["totals"]["total"], "283.00")

    def test_cannot_modify_after_seal(self) -> None:
        service = make_service()
        seal_period(service)
        with self.assertRaises(InvalidState):
            service.add_inputs("P-2026", lines=[{"vehicle_model": "车型丙", "quantity": 1, "unit_price": 1}], actor="企业申报员")
        with self.assertRaises(InvalidState):
            service.add_adjustment(
                "P-2026",
                adjustment={"target": "total", "field": "total", "delta": "1", "reason": "x"},
                actor="核算专员",
            )
        with self.assertRaises(InvalidState):
            service.submit("P-2026", actor="企业申报员")
        with self.assertRaises(InvalidState):
            service.sign("P-2026", signer_id="s-fin", signature="again")

    def test_signer_validation(self) -> None:
        service = make_service()
        make_period(service)
        service.add_inputs("P-2026", lines=LINES, actor="企业申报员")
        service.submit("P-2026", actor="企业申报员")
        with self.assertRaises(InvalidState):
            service.sign("P-2026", signer_id="s-fin", signature="x")  # 未确认不能签
        service.confirm("P-2026", actor="核算专员")
        with self.assertRaises(Forbidden):
            service.sign("P-2026", signer_id="stranger", signature="x")
        service.sign("P-2026", signer_id="s-fin", signature="x")
        with self.assertRaises(InvalidState):
            service.sign("P-2026", signer_id="s-fin", signature="x")  # 重复签署

    def test_period_validation(self) -> None:
        service = make_service()
        with self.assertRaises(Validation):
            make_period(service, quorum=4)  # 超过签署人数量
        with self.assertRaises(Validation):
            service.create_period(
                period_id="P-bad", year=2026, quorum=1,
                signers=[{"signer_id": "a"}, {"signer_id": "a"}],
                ruleset=RULESET, actor="x",
            )
        with self.assertRaises(Validation):
            service.create_period(
                period_id="P-bad2", year=2026, quorum=1, signers=[{"signer_id": "a"}],
                ruleset={"version": "v", "tax_rate": "1.5"}, actor="x",
            )
        with self.assertRaises(NotFound):
            service.get_period("P-none")


class ExportAndChunkTest(unittest.TestCase):
    def test_export_is_deterministic_and_resumable(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        sid = snapshot["snapshot_id"]
        first, etag1 = service.export_snapshot(sid)
        second, etag2 = service.export_snapshot(sid)
        self.assertEqual(first, second)  # 重复导出内容恒定
        self.assertEqual(etag1, etag2)
        self.assertEqual(canonical.sha256_hex(first), snapshot["payload_sha256"])

        # 中断续传：逐块下载后拼接必须等于完整导出
        chunks = [service.get_chunk(sid, i)[0] for i in range(snapshot["chunk_count"])]
        self.assertEqual(b"".join(chunks), first)

    def test_every_chunk_verifies_against_manifest_and_root(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        sid = snapshot["snapshot_id"]
        full = service.get_snapshot(sid)
        for index, expected in enumerate(full["chunk_digests"]):
            chunk, digest, _ = service.get_chunk(sid, index)
            self.assertEqual(digest, expected)
            result = service.verify_chunk(sid, index=index, data=chunk)
            self.assertTrue(result["verified"])
            self.assertTrue(result["matches_manifest"])
            self.assertTrue(result["proof_valid"])
            proof = service.chunk_proof(sid, index)
            self.assertTrue(proof["valid"])

    def test_tampered_chunk_fails_verification(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        sid = snapshot["snapshot_id"]
        chunk, _, _ = service.get_chunk(sid, 0)
        tampered = chunk[:-1] + (b"0" if chunk[-1:] != b"0" else b"1")
        result = service.verify_chunk(sid, index=0, data=tampered)
        self.assertFalse(result["verified"])
        self.assertFalse(result["matches_manifest"])
        with self.assertRaises(NotFound):
            service.get_chunk(sid, 10_000)


class LateMaterialAndReopenTest(unittest.TestCase):
    def test_late_material_requires_sealed_state(self) -> None:
        service = make_service()
        make_period(service)
        with self.assertRaises(InvalidState):
            service.submit_late_material(
                "P-2026", strategy="next_version",
                materials={"lines": [{"vehicle_model": "车型丙", "quantity": 1, "unit_price": 10}]},
                reason="迟到", author="企业申报员",
            )

    def test_correction_approval_rolls_into_next_version(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        sid = snapshot["snapshot_id"]
        before, _ = service.export_snapshot(sid)

        queued = service.submit_late_material(
            "P-2026", strategy="next_version",
            materials={"lines": [{"vehicle_model": "车型丙", "quantity": 3, "unit_price": "20.00"}]},
            reason="月末补单", author="企业申报员",
        )
        self.assertEqual(queued["status"], "已进入下一版")

        created = service.submit_late_material(
            "P-2026", strategy="correction",
            materials={"lines": [{"line_id": "L2", "vehicle_model": "车型乙", "quantity": 2, "unit_price": "50.50"}]},
            reason="车型乙数量更正", author="核算专员",
        )
        correction_id = created["correction"]["correction_id"]
        self.assertEqual(created["correction"]["status"], "待批准")

        # 非监管审计员 / 申请人本人 都不能批准
        with self.assertRaises(Forbidden):
            service.approve_correction(correction_id, approver="别人", role="核算专员")
        with self.assertRaises(Forbidden):
            service.approve_correction(correction_id, approver="核算专员", role="监管审计员")

        result = service.approve_correction(correction_id, approver="审计负责人", role="监管审计员")
        period = result["period"]
        self.assertEqual(period["state"], "草稿")
        self.assertEqual(period["version"], 2)
        self.assertEqual(period["pending_material_count"], 0)  # 排队材料已折叠
        self.assertEqual(period["line_count"], 3)

        # 旧快照内容保持不变，仍可导出、可校验
        after, _ = service.export_snapshot(sid)
        self.assertEqual(before, after)
        self.assertEqual(service.get_snapshot(sid)["status"], "superseded")

        # 封账前后差异：车型乙数量变化 + 新增车型丙
        diff = service.diff_working("P-2026", snapshot_id=sid)
        self.assertEqual([line["vehicle_model"] for line in diff["lines"]["added"]], ["车型丙"])
        changed = {item["line_id"]: item for item in diff["lines"]["changed"]}
        self.assertEqual(changed["L2"]["fields"]["quantity"], {"from": "1", "to": "2"})
        self.assertEqual(diff["totals"]["amount"]["from"], "250.50")
        self.assertEqual(diff["totals"]["amount"]["to"], "361.00")

        # 新版本重新走流程并封存为 v2，两个快照可对比
        service.submit("P-2026", actor="企业申报员")
        service.confirm("P-2026", actor="核算专员")
        service.sign("P-2026", signer_id="s-fin", signature="x")
        service.sign("P-2026", signer_id="s-ops", signature="x")
        sealed = service.sign("P-2026", signer_id="s-audit", signature="x")["snapshot"]
        self.assertEqual(sealed["version"], 2)
        diff2 = service.diff_snapshots(sid, sealed["snapshot_id"])
        self.assertEqual(diff2["meta"]["from_version"], 1)
        self.assertEqual(diff2["meta"]["to_version"], 2)
        self.assertEqual(diff2["totals"]["total"]["to"], sealed["totals"]["total"])

    def test_reopen_requires_independent_approval(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        sid = snapshot["snapshot_id"]
        before, _ = service.export_snapshot(sid)

        request = service.request_reopen(sid, requester="核算专员", reason="发现申报口径错误")
        rid = request["request_id"]
        with self.assertRaises(Forbidden):
            service.approve_reopen(rid, approver="核算专员", role="监管审计员")  # 同一人
        with self.assertRaises(Forbidden):
            service.approve_reopen(rid, approver="运营负责人", role="交易运营员")  # 非审计

        result = service.approve_reopen(rid, approver="审计负责人", role="监管审计员")
        self.assertEqual(result["period"]["state"], "草稿")
        self.assertEqual(result["period"]["version"], 2)
        self.assertEqual(service.get_snapshot(sid)["status"], "reopened")
        with self.assertRaises(InvalidState):
            service.approve_reopen(rid, approver="审计负责人", role="监管审计员")  # 不能重复处理
        with self.assertRaises(InvalidState):
            service.request_reopen(sid, requester="核算专员", reason="再次申请")  # 已非生效快照

        # 重开后旧快照导出内容不变
        after, _ = service.export_snapshot(sid)
        self.assertEqual(before, after)

    def test_audit_trail_records_key_events(self) -> None:
        service = make_service()
        snapshot = seal_period(service)
        service.export_snapshot(snapshot["snapshot_id"])
        events = [event["action"] for event in service.audit_trail("P-2026")]
        for expected in ("PERIOD_CREATED", "INPUTS_ADDED", "SUBMITTED", "CONFIRMED", "SEALED", "EXPORTED"):
            self.assertIn(expected, events)


if __name__ == "__main__":
    unittest.main()
