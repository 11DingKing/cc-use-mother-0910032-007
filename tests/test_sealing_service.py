"""封账领域服务的回归测试：覆盖契约中的四项关键约束。"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing.errors import DomainError
from sealing.models import (
    APPROVED,
    CONFIRMED,
    DRAFT,
    IN_PROGRESS,
    PENDING_ACCOUNTING,
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_DECLARANT,
    ROLE_OPERATOR,
    SEALED,
    Actor,
)
from sealing.service import LedgerSealingService
from sealing.store import LedgerStore

DECLARANT = Actor("declarant-01", ROLE_DECLARANT)
ACCOUNTANT = Actor("accountant-01", ROLE_ACCOUNTANT)
ACCOUNTANT_2 = Actor("accountant-02", ROLE_ACCOUNTANT)
OPERATOR = Actor("operator-01", ROLE_OPERATOR)
AUDITOR = Actor("auditor-01", ROLE_AUDITOR)
AUDITOR_2 = Actor("auditor-02", ROLE_AUDITOR)

RULES = [
    {"rule_id": "R01", "type": "tax_rate", "params": {"category": "乘用车", "rate_bp": 1000}},
    {"rule_id": "R02", "type": "deduction", "params": {"amount_cents": 5000}},
]


def make_service(chunk_size: int = 256) -> LedgerSealingService:
    ticks = iter(f"2026-10-04T09:{minute:02d}:00+00:00" for minute in range(600))
    return LedgerSealingService(clock=lambda: next(ticks), chunk_size=chunk_size)


def build_sealed(service: LedgerSealingService, period: str = "2025") -> str:
    """走完 草稿→待核算→已确认→执行中→已封存 全流程，返回快照号。"""
    snapshot_id = service.create_snapshot(period, DECLARANT)["snapshot_id"]
    service.add_input(
        snapshot_id, DECLARANT,
        model_code="EV-A", model_name="电动轿车A", category="乘用车",
        quantity=10, unit_price_cents=2_000_000,
    )
    service.add_input(
        snapshot_id, DECLARANT,
        model_code="EV-B", model_name="电动SUV B", category="乘用车",
        quantity=5, unit_price_cents=3_000_000,
    )
    service.attach_rule_set(snapshot_id, ACCOUNTANT, "RS-2025", "v1", RULES)
    service.add_adjustment(
        snapshot_id, ACCOUNTANT,
        target_model_code="EV-A", delta_cents=-15000, reason="返利调整",
    )
    service.submit(snapshot_id, DECLARANT)
    service.compute_totals(snapshot_id, ACCOUNTANT)
    service.sign(snapshot_id, ACCOUNTANT_2, "核算复核")
    service.sign(snapshot_id, OPERATOR, "运营确认")
    service.sign(snapshot_id, AUDITOR, "审计签发")
    return snapshot_id


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_full_lifecycle_reaches_sealed_with_chunk_digests(self) -> None:
        snapshot_id = build_sealed(self.service)
        view = self.service.get_snapshot(snapshot_id)
        self.assertEqual(view["state"], SEALED)
        self.assertEqual(view["signature_count"], 3)
        self.assertEqual(view["statutory_signatures"], 3)
        sealed = view["sealed"]
        self.assertTrue(sealed["merkle_root"])
        self.assertGreater(len(sealed["chunk_hashes"]), 1)  # 小块大小强制多块
        self.assertEqual(sealed["chunk_size"], 256)
        # 快照汇总了申报输入、核算规则、人工调整与签署人
        self.assertEqual(len(view["inputs"]), 2)
        self.assertEqual(view["rule_set"]["rule_set_id"], "RS-2025")
        self.assertEqual(len(view["adjustments"]), 1)
        self.assertEqual({s["role"] for s in view["signatures"]},
                         {ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_AUDITOR})

    def test_state_machine_order(self) -> None:
        service = make_service()
        snapshot_id = service.create_snapshot("2025", DECLARANT)["snapshot_id"]
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], DRAFT)
        service.add_input(snapshot_id, DECLARANT, model_code="EV-A", quantity=1,
                          unit_price_cents=100)
        # 未提交不能核算
        with self.assertRaises(DomainError):
            service.compute_totals(snapshot_id, ACCOUNTANT)
        service.attach_rule_set(snapshot_id, ACCOUNTANT, "RS", "v1", RULES)
        service.submit(snapshot_id, DECLARANT)
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], PENDING_ACCOUNTING)
        service.compute_totals(snapshot_id, ACCOUNTANT)
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], CONFIRMED)
        service.sign(snapshot_id, ACCOUNTANT_2)
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], IN_PROGRESS)
        service.sign(snapshot_id, OPERATOR)
        service.sign(snapshot_id, AUDITOR)
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], SEALED)

    def test_totals_are_deterministic(self) -> None:
        snapshot_id = build_sealed(self.service)
        totals = self.service.get_snapshot(snapshot_id)["totals"]
        # 毛额：10*2,000,000 + 5*3,000,000 = 35,000,000
        self.assertEqual(totals["gross_cents"], 35_000_000)
        # 税 10% = 3,500,000；扣减 5,000；人工调整 -15,000
        self.assertEqual(totals["rules_total_cents"], 3_500_000 - 5_000)
        self.assertEqual(totals["net_cents"], 35_000_000 + 3_495_000 - 15_000)

    def test_cannot_mutate_after_confirmation(self) -> None:
        service = make_service()
        snapshot_id = service.create_snapshot("2025", DECLARANT)["snapshot_id"]
        service.add_input(snapshot_id, DECLARANT, model_code="EV-A", quantity=1,
                          unit_price_cents=100)
        service.attach_rule_set(snapshot_id, ACCOUNTANT, "RS", "v1", RULES)
        service.submit(snapshot_id, DECLARANT)
        service.compute_totals(snapshot_id, ACCOUNTANT)
        # 确认后：申报输入、人工调整、规则集全部冻结
        with self.assertRaises(DomainError):
            service.add_input(snapshot_id, DECLARANT, model_code="EV-B", quantity=1,
                              unit_price_cents=1)
        with self.assertRaises(DomainError):
            service.add_adjustment(snapshot_id, ACCOUNTANT, delta_cents=1, reason="x")
        with self.assertRaises(DomainError):
            service.attach_rule_set(snapshot_id, ACCOUNTANT, "RS", "v2", RULES)

    def test_signature_quorum_rules(self) -> None:
        service = make_service()
        snapshot_id = service.create_snapshot("2025", DECLARANT, statutory_signatures=2)[
            "snapshot_id"
        ]
        service.add_input(snapshot_id, DECLARANT, model_code="EV-A", quantity=1,
                          unit_price_cents=100)
        service.attach_rule_set(snapshot_id, ACCOUNTANT, "RS", "v1", RULES)
        service.submit(snapshot_id, DECLARANT)
        service.compute_totals(snapshot_id, ACCOUNTANT)
        # 申报员不能签署；同一人不能重复签署
        with self.assertRaises(DomainError):
            service.sign(snapshot_id, DECLARANT)
        service.sign(snapshot_id, ACCOUNTANT_2)
        with self.assertRaises(DomainError):
            service.sign(snapshot_id, ACCOUNTANT_2)
        service.sign(snapshot_id, AUDITOR)
        self.assertEqual(service.get_snapshot(snapshot_id)["state"], SEALED)
        # 封存后不能再签署
        with self.assertRaises(DomainError):
            service.sign(snapshot_id, OPERATOR)

    def test_one_open_version_per_period(self) -> None:
        service = make_service()
        service.create_snapshot("2025", DECLARANT)
        with self.assertRaises(DomainError):
            service.create_snapshot("2025", DECLARANT)


class LateMaterialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.snapshot_id = build_sealed(self.service)

    def test_late_input_goes_to_next_version(self) -> None:
        result = self.service.register_late_material(
            self.snapshot_id, DECLARANT, kind="input", route="next_version",
            fields={"model_code": "EV-C", "quantity": 2, "unit_price_cents": 900_000,
                    "category": "乘用车"},
        )
        target_id = result["material"]["target_snapshot_id"]
        self.assertNotEqual(target_id, self.snapshot_id)
        target = self.service.get_snapshot(target_id)
        self.assertEqual(target["state"], DRAFT)
        self.assertEqual(target["version"], 2)
        self.assertEqual(target["inputs"][0]["model_code"], "EV-C")
        # 原封存版本内容不变
        sealed_view = self.service.get_snapshot(self.snapshot_id)
        self.assertEqual(len(sealed_view["inputs"]), 2)
        self.assertEqual(sealed_view["state"], SEALED)

    def test_late_adjustment_becomes_correction_order(self) -> None:
        result = self.service.register_late_material(
            self.snapshot_id, ACCOUNTANT, kind="adjustment", route="correction",
            fields={"delta_cents": 30000, "reason": "补报返利"},
        )
        correction = result["correction"]
        self.assertEqual(correction["state"], "待批准")
        self.assertEqual(correction["snapshot_id"], self.snapshot_id)

    def test_correction_requires_independent_approval(self) -> None:
        correction = self.service.create_correction(
            self.snapshot_id, ACCOUNTANT, reason="补报",
            adjustments=[{"delta_cents": 30000, "reason": "补报返利"}],
        )
        cid = correction["correction_id"]
        # 申请人不能自批；非审计角色不能批
        with self.assertRaises(DomainError):
            self.service.decide_correction(cid, ACCOUNTANT, approve=True)
        with self.assertRaises(DomainError):
            self.service.decide_correction(cid, OPERATOR, approve=True)
        decided = self.service.decide_correction(cid, AUDITOR, approve=True)
        self.assertEqual(decided["state"], APPROVED)
        # 有效视图反映已批准更正，封存净额不变
        effective = self.service.effective_view(self.snapshot_id)
        self.assertEqual(effective["correction_delta_cents"], 30000)
        self.assertEqual(
            effective["effective_net_cents"],
            effective["sealed_net_cents"] + 30000,
        )
        self.assertEqual(self.service.get_snapshot(self.snapshot_id)["state"], SEALED)


class ReopenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.snapshot_id = build_sealed(self.service)

    def test_reopen_requires_independent_auditor_approval(self) -> None:
        request = self.service.request_reopen(
            self.snapshot_id, ACCOUNTANT, reason="监管要求补充披露"
        )
        rid = request["request_id"]
        # 申请人自批被拒；非审计角色被拒
        with self.assertRaises(DomainError):
            self.service.decide_reopen(rid, ACCOUNTANT, approve=True)
        with self.assertRaises(DomainError):
            self.service.decide_reopen(rid, OPERATOR, approve=True)
        outcome = self.service.decide_reopen(rid, AUDITOR, approve=True)
        new_id = outcome["request"]["new_snapshot_id"]
        forked = self.service.get_snapshot(new_id)
        self.assertEqual(forked["state"], DRAFT)
        self.assertEqual(forked["version"], 2)
        self.assertEqual(forked["reopened_from"], self.snapshot_id)
        self.assertEqual(forked["reopen_approval"]["approved_by"], AUDITOR.actor_id)
        self.assertEqual(len(forked["inputs"]), 2)  # 继承封存内容
        # 原版本依旧封存，摘要不变
        original = self.service.get_snapshot(self.snapshot_id)
        self.assertEqual(original["state"], SEALED)

    def test_reopen_rejection_keeps_single_sealed_version(self) -> None:
        request = self.service.request_reopen(self.snapshot_id, AUDITOR, reason="x")
        decided = self.service.decide_reopen(request["request_id"], AUDITOR_2, approve=False)
        self.assertEqual(decided["state"], "已驳回")
        snapshots = self.service.list_snapshots("2025")
        self.assertEqual(len(snapshots), 1)

    def test_duplicate_pending_reopen_rejected(self) -> None:
        self.service.request_reopen(self.snapshot_id, ACCOUNTANT, reason="a")
        with self.assertRaises(DomainError):
            self.service.request_reopen(self.snapshot_id, AUDITOR, reason="b")


class ExportDeterminismTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.snapshot_id = build_sealed(self.service)

    def _fetch_all(self, export_id: str) -> bytes:
        manifest = self.service.export_manifest(export_id)
        payload = b""
        for index in range(manifest["chunk_count"]):
            chunk = self.service.get_export_chunk(export_id, index)
            self.assertEqual(chunk["chunk_hash"], manifest["chunk_hashes"][index])
            payload += bytes.fromhex(chunk["chunk_hex"])
        return payload

    def test_repeated_exports_are_identical(self) -> None:
        first = self.service.create_export(self.snapshot_id, AUDITOR)
        second = self.service.create_export(self.snapshot_id, ACCOUNTANT)
        self.assertEqual(self._fetch_all(first["export_id"]), self._fetch_all(second["export_id"]))
        self.assertEqual(first["merkle_root"], second["merkle_root"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])

    def test_interrupted_resume_yields_same_content(self) -> None:
        # 第一次导出拉到一半中断；另开会话续传剩余分块，重组结果一致
        first = self.service.create_export(self.snapshot_id, AUDITOR)
        manifest = self.service.export_manifest(first["export_id"])
        total = manifest["chunk_count"]
        cut = total // 2
        head = b"".join(
            bytes.fromhex(self.service.get_export_chunk(first["export_id"], i)["chunk_hex"])
            for i in range(cut)
        )
        resumed = self.service.create_export(self.snapshot_id, AUDITOR)
        tail = b"".join(
            bytes.fromhex(self.service.get_export_chunk(resumed["export_id"], i)["chunk_hex"])
            for i in range(cut, total)
        )
        self.assertEqual(head + tail, self._fetch_all(first["export_id"]))

    def test_export_requires_sealed_state(self) -> None:
        service = make_service()
        draft_id = service.create_snapshot("2026", DECLARANT)["snapshot_id"]
        with self.assertRaises(DomainError):
            service.create_export(draft_id, AUDITOR)

    def test_chunk_out_of_range(self) -> None:
        export = self.service.create_export(self.snapshot_id, AUDITOR)
        with self.assertRaises(DomainError):
            self.service.get_export_chunk(export["export_id"], 10_000)


class ChunkVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.snapshot_id = build_sealed(self.service)
        self.export = self.service.create_export(self.snapshot_id, AUDITOR)

    def test_every_chunk_verifies(self) -> None:
        manifest = self.service.export_manifest(self.export["export_id"])
        for index in range(manifest["chunk_count"]):
            chunk = self.service.get_export_chunk(self.export["export_id"], index)
            result = self.service.verify_chunk(
                self.snapshot_id, index, chunk["chunk_hex"]
            )
            self.assertTrue(result["valid"], f"分块 {index} 验证失败")

    def test_tampered_chunk_fails(self) -> None:
        chunk = self.service.get_export_chunk(self.export["export_id"], 0)
        raw = bytearray.fromhex(chunk["chunk_hex"])
        raw[0] ^= 0xFF
        result = self.service.verify_chunk(self.snapshot_id, 0, raw.hex())
        self.assertFalse(result["valid"])

    def test_proof_endpoint_matches_root(self) -> None:
        proof = self.service.chunk_proof(self.snapshot_id, 0)
        self.assertEqual(proof["merkle_root"], self.export["merkle_root"])
        self.assertTrue(proof["proof"])  # 多块时必有兄弟路径

    def test_seal_integrity_ok(self) -> None:
        integrity = self.service.seal_integrity(self.snapshot_id)
        self.assertTrue(integrity["ok"], integrity["checks"])


class DiffTest(unittest.TestCase):
    def test_diff_between_sealed_and_reopened_version(self) -> None:
        service = make_service()
        sealed_id = build_sealed(service)
        request = service.request_reopen(sealed_id, ACCOUNTANT, reason="补充车型")
        new_id = service.decide_reopen(request["request_id"], AUDITOR, approve=True)[
            "request"
        ]["new_snapshot_id"]
        service.add_input(new_id, DECLARANT, model_code="EV-C", quantity=3,
                          unit_price_cents=800_000, category="乘用车")
        diff = service.diff_snapshots(sealed_id, new_id)
        self.assertEqual(len(diff["inputs"]["added"]), 1)
        self.assertEqual(diff["inputs"]["added"][0]["model_code"], "EV-C")
        self.assertEqual(diff["inputs"]["removed"], [])
        self.assertEqual(diff["base"]["state"], SEALED)
        self.assertEqual(diff["other"]["state"], DRAFT)


class PersistenceTest(unittest.TestCase):
    def test_sealed_payload_survives_restart(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            ticks = iter(f"2026-10-04T09:{m:02d}:00+00:00" for m in range(600))
            service = LedgerSealingService(
                store=LedgerStore(path), clock=lambda: next(ticks), chunk_size=256
            )
            sealed_id = build_sealed(service)
            before = service.create_export(sealed_id, AUDITOR)

            # 模拟重启：从同一文件重建服务
            revived = LedgerSealingService(store=LedgerStore(path), chunk_size=256)
            after = revived.create_export(sealed_id, AUDITOR)
            self.assertEqual(before["merkle_root"], after["merkle_root"])
            self.assertEqual(before["payload_sha256"], after["payload_sha256"])
            manifest = revived.export_manifest(after["export_id"])
            payload = b"".join(
                bytes.fromhex(
                    revived.get_export_chunk(after["export_id"], i)["chunk_hex"]
                )
                for i in range(manifest["chunk_count"])
            )
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(), manifest["payload_sha256"]
            )
            self.assertTrue(revived.seal_integrity(sealed_id)["ok"])


if __name__ == "__main__":
    unittest.main()
