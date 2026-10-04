"""HTTP API 端到端测试：真实起服务，走完整封账与审计流程。"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing.api import make_handler
from sealing.service import LedgerSealingService


def actor(actor_id: str, role: str) -> dict:
    # 中文角色按百分号编码放入请求头
    return {"X-Actor-Id": actor_id, "X-Actor-Role": urllib.parse.quote(role)}


DECLARANT = actor("declarant-01", "企业申报员")
ACCOUNTANT = actor("accountant-01", "核算专员")
ACCOUNTANT_2 = actor("accountant-02", "核算专员")
OPERATOR = actor("operator-01", "交易运营员")
AUDITOR = actor("auditor-01", "监管审计员")


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        service = LedgerSealingService(
            clock=lambda: "2026-10-04T10:00:00+00:00", chunk_size=128
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def seal_period(self, period: str) -> str:
        _, created = self.call("POST", f"/periods/{period}/snapshots",
                               {"statutory_signatures": 3}, DECLARANT)
        sid = created["snapshot_id"]
        self.call("POST", f"/snapshots/{sid}/inputs",
                  {"model_code": "EV-A", "model_name": "电动轿车A", "category": "乘用车",
                   "quantity": 10, "unit_price_cents": 2_000_000}, DECLARANT)
        self.call("POST", f"/snapshots/{sid}/inputs",
                  {"model_code": "EV-B", "model_name": "电动SUV B", "category": "乘用车",
                   "quantity": 5, "unit_price_cents": 3_000_000}, DECLARANT)
        self.call("POST", f"/snapshots/{sid}/rule-set",
                  {"rule_set_id": "RS-1", "version": "v1",
                   "rules": [{"rule_id": "R01", "type": "tax_rate",
                              "params": {"category": "乘用车", "rate_bp": 1000}}]},
                  ACCOUNTANT)
        self.call("POST", f"/snapshots/{sid}/adjustments",
                  {"delta_cents": -15000, "reason": "返利调整"}, ACCOUNTANT)
        self.call("POST", f"/snapshots/{sid}/submit", {}, DECLARANT)
        self.call("POST", f"/snapshots/{sid}/compute", {}, ACCOUNTANT)
        for headers in (ACCOUNTANT_2, OPERATOR, AUDITOR):
            status, _ = self.call("POST", f"/snapshots/{sid}/signatures", {}, headers)
            self.assertEqual(status, 200)
        return sid

    def download(self, export_id: str) -> bytes:
        status, manifest = self.call("GET", f"/exports/{export_id}", None, AUDITOR)
        self.assertEqual(status, 200)
        payload = b""
        for index in range(manifest["chunk_count"]):
            status, chunk = self.call(
                "GET", f"/exports/{export_id}/chunks/{index}", None, AUDITOR)
            self.assertEqual(status, 200)
            self.assertEqual(chunk["chunk_hash"], manifest["chunk_hashes"][index])
            payload += bytes.fromhex(chunk["chunk_hex"])
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), manifest["payload_sha256"])
        return payload

    def test_health(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_audit_scenario_end_to_end(self) -> None:
        sid = self.seal_period("2025")

        # 封存后：状态为已封存，完整性自检通过
        status, view = self.call("GET", f"/snapshots/{sid}", None, AUDITOR)
        self.assertEqual(view["state"], "已封存")
        self.assertTrue(view["exportable"])
        status, integrity = self.call("GET", f"/snapshots/{sid}/seal-integrity", None, AUDITOR)
        self.assertTrue(integrity["ok"])

        # 审计发现的核心问题已消除：签发后不能再改车型明细
        status, body = self.call("POST", f"/snapshots/{sid}/inputs",
                                 {"model_code": "EV-X", "quantity": 1,
                                  "unit_price_cents": 1}, DECLARANT)
        self.assertEqual(status, 409)
        status, body = self.call("POST", f"/snapshots/{sid}/adjustments",
                                 {"delta_cents": 1, "reason": "篡改"}, ACCOUNTANT)
        self.assertEqual(status, 409)

        # 迟到材料 → 下一版
        status, late = self.call(
            "POST", f"/snapshots/{sid}/late-materials",
            {"kind": "input", "route": "next_version",
             "fields": {"model_code": "EV-C", "quantity": 2,
                        "unit_price_cents": 900_000, "category": "乘用车"}},
            DECLARANT)
        self.assertEqual(status, 200)
        next_id = late["material"]["target_snapshot_id"]
        self.assertNotEqual(next_id, sid)

        # 迟到调整 → 更正单，需独立审计批准
        status, late_adj = self.call(
            "POST", f"/snapshots/{sid}/late-materials",
            {"kind": "adjustment", "route": "correction",
             "fields": {"delta_cents": 30000, "reason": "补报返利"}}, ACCOUNTANT)
        self.assertEqual(status, 200)
        correction_id = late_adj["correction"]["correction_id"]
        status, _ = self.call("POST", f"/corrections/{correction_id}/decision",
                              {"approve": True}, ACCOUNTANT_2)
        self.assertEqual(status, 403)  # 非审计角色不能批
        status, decided = self.call("POST", f"/corrections/{correction_id}/decision",
                                    {"approve": True}, AUDITOR)
        self.assertEqual(decided["state"], "已批准")
        status, effective = self.call("GET", f"/snapshots/{sid}/effective", None, AUDITOR)
        self.assertEqual(effective["correction_delta_cents"], 30000)

        # 重开需独立批准；期间已有未封存版本时会被暂缓
        status, reopen = self.call("POST", f"/snapshots/{sid}/reopen-requests",
                                   {"reason": "监管要求"}, ACCOUNTANT)
        rid = reopen["request_id"]
        status, body = self.call("POST", f"/reopen-requests/{rid}/decision",
                                 {"approve": True}, ACCOUNTANT)
        self.assertEqual(status, 403)
        status, body = self.call("POST", f"/reopen-requests/{rid}/decision",
                                 {"approve": True}, AUDITOR)
        self.assertEqual(status, 409)  # 迟到材料已生成下一版，先处理它

        # 重复导出字节一致；中断续传重组一致
        status, export_a = self.call("POST", f"/snapshots/{sid}/exports", {}, AUDITOR)
        status, export_b = self.call("POST", f"/snapshots/{sid}/exports", {}, ACCOUNTANT)
        payload_a = self.download(export_a["export_id"])
        payload_b = self.download(export_b["export_id"])
        self.assertEqual(payload_a, payload_b)
        manifest = self.call("GET", f"/exports/{export_a['export_id']}", None, AUDITOR)[1]
        cut = manifest["chunk_count"] // 2
        head = b"".join(
            bytes.fromhex(self.call(
                "GET", f"/exports/{export_a['export_id']}/chunks/{i}", None, AUDITOR
            )[1]["chunk_hex"])
            for i in range(cut)
        )
        tail = b"".join(
            bytes.fromhex(self.call(
                "GET", f"/exports/{export_b['export_id']}/chunks/{i}", None, AUDITOR
            )[1]["chunk_hex"])
            for i in range(cut, manifest["chunk_count"])
        )
        self.assertEqual(head + tail, payload_a)

        # 任一分块可验证；篡改分块被拒绝
        status, chunk0 = self.call(
            "GET", f"/exports/{export_a['export_id']}/chunks/0", None, AUDITOR)
        status, verified = self.call(
            "POST", f"/snapshots/{sid}/chunks/0/verify",
            {"chunk_hex": chunk0["chunk_hex"]}, AUDITOR)
        self.assertTrue(verified["valid"])
        tampered = bytearray.fromhex(chunk0["chunk_hex"])
        tampered[-1] ^= 0x01
        status, verified = self.call(
            "POST", f"/snapshots/{sid}/chunks/0/verify",
            {"chunk_hex": tampered.hex()}, AUDITOR)
        self.assertFalse(verified["valid"])
        status, proof = self.call("GET", f"/snapshots/{sid}/chunks/0/proof", None, AUDITOR)
        self.assertEqual(proof["merkle_root"], manifest["merkle_root"])

        # 封账前后差异对比：封存版 vs 迟到材料生成的下一版
        status, diff = self.call("GET", f"/snapshots/{sid}/diff/{next_id}", None, AUDITOR)
        self.assertEqual(status, 200)
        self.assertEqual(diff["base"]["state"], "已封存")
        self.assertEqual(len(diff["inputs"]["added"]), 1)
        self.assertEqual(diff["inputs"]["added"][0]["model_code"], "EV-C")

    def test_role_and_state_errors_map_to_http(self) -> None:
        _, created = self.call("POST", "/periods/2026/snapshots", {}, DECLARANT)
        sid = created["snapshot_id"]
        # 申报员不能执行核算
        status, body = self.call("POST", f"/snapshots/{sid}/compute", {}, DECLARANT)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        # 不存在的快照
        status, body = self.call("GET", "/snapshots/SNP-9999", None, AUDITOR)
        self.assertEqual(status, 404)
        # 未知路由
        status, _ = self.call("GET", "/no-such-route", None, AUDITOR)
        self.assertEqual(status, 404)
        # 空草稿不能提交
        status, body = self.call("POST", f"/snapshots/{sid}/submit", {}, DECLARANT)
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
