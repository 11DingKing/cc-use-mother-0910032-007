"""封账 HTTP API 的端到端测试：生命周期、幂等导出、断点续传、分块校验、差异对比。"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_service.api import create_server
from sealing_service.service import SealingService
from sealing_service.store import Store

SIGNERS = [
    {"signer_id": "s-fin", "name": "财务负责人", "role": "核算专员"},
    {"signer_id": "s-audit", "name": "审计负责人", "role": "监管审计员"},
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        service = SealingService(Store(), chunk_size=96)
        cls.server = create_server(service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        status, resp_headers, raw = self.request(method, path, body, headers)
        return status, resp_headers, json.loads(raw) if raw else {}

    def seal_via_api(self, period_id: str) -> dict:
        status, _, _ = self.json("POST", "/periods", {
            "period_id": period_id, "year": 2026, "quorum": 2,
            "signers": SIGNERS, "ruleset": {"version": "v2026.1", "tax_rate": "0.13"},
            "actor": "企业申报员",
        })
        self.assertEqual(status, 201)
        status, _, _ = self.json("POST", f"/periods/{period_id}/inputs", {
            "lines": [
                {"line_id": "L1", "vehicle_model": "车型甲", "quantity": 2, "unit_price": "100.00"},
                {"line_id": "L2", "vehicle_model": "车型乙", "quantity": 1, "unit_price": "50.50"},
            ],
            "actor": "企业申报员",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.json("POST", f"/periods/{period_id}/submit", {"actor": "企业申报员"})[0], 200)
        self.assertEqual(self.json("POST", f"/periods/{period_id}/confirm", {"actor": "核算专员"})[0], 200)
        status, _, view = self.json("POST", f"/periods/{period_id}/sign", {"signer_id": "s-fin", "signature": "a"})
        self.assertEqual(status, 200)
        self.assertEqual(view["state"], "执行中")
        status, _, view = self.json("POST", f"/periods/{period_id}/sign", {"signer_id": "s-audit", "signature": "b"})
        self.assertEqual(status, 200)
        self.assertEqual(view["state"], "已封存")
        return view["snapshot"]

    def test_lifecycle_export_etag_and_resume(self) -> None:
        snapshot = self.seal_via_api("P-HTTP-1")
        sid = snapshot["snapshot_id"]

        status, headers, full = self.request("GET", f"/snapshots/{sid}/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("ETag"), f'"{snapshot["content_digest"]}"')
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(hashlib.sha256(full).hexdigest(), snapshot["payload_sha256"])

        # 重复导出：字节一致；If-None-Match 命中返回 304
        _, _, again = self.request("GET", f"/snapshots/{sid}/export")
        self.assertEqual(full, again)
        status, _, body = self.request("GET", f"/snapshots/{sid}/export", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

        # 中断续传：三段 Range 拼接等于完整导出
        size = len(full)
        p1 = size // 3
        p2 = 2 * size // 3
        status, h1, b1 = self.request("GET", f"/snapshots/{sid}/export", headers={"Range": f"bytes=0-{p1 - 1}"})
        self.assertEqual(status, 206)
        self.assertEqual(h1.get("Content-Range"), f"bytes 0-{p1 - 1}/{size}")
        _, _, b2 = self.request("GET", f"/snapshots/{sid}/export", headers={"Range": f"bytes={p1}-{p2 - 1}"})
        _, _, b3 = self.request("GET", f"/snapshots/{sid}/export", headers={"Range": f"bytes={p2}-"})
        self.assertEqual(b1 + b2 + b3, full)

        # 越界区间返回 416
        status, _, err = self.json("GET", f"/snapshots/{sid}/export", headers={"Range": f"bytes={size + 10}-"})
        self.assertEqual(status, 416)
        self.assertEqual(err["error"]["code"], "RANGE_NOT_SATISFIABLE")

    def test_chunk_download_and_verify(self) -> None:
        snapshot = self.seal_via_api("P-HTTP-2")
        sid = snapshot["snapshot_id"]
        status, _, manifest = self.json("GET", f"/snapshots/{sid}/manifest")
        self.assertEqual(status, 200)
        self.assertGreater(manifest["chunk_count"], 1)

        assembled = b""
        for index in range(manifest["chunk_count"]):
            status, headers, chunk = self.request("GET", f"/snapshots/{sid}/chunks/{index}")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Chunk-SHA256"), manifest["chunk_digests"][index])
            self.assertEqual(hashlib.sha256(chunk).hexdigest(), manifest["chunk_digests"][index])
            assembled += chunk
            status, _, result = self.json("POST", f"/snapshots/{sid}/verify-chunk", {
                "index": index,
                "data_base64": base64.b64encode(chunk).decode("ascii"),
            })
            self.assertEqual(status, 200)
            self.assertTrue(result["verified"])
            self.assertEqual(result["root"], manifest["content_digest"])

        status, _, full = self.request("GET", f"/snapshots/{sid}/export")
        self.assertEqual(assembled, full)

        # 篡改的分块不能通过校验
        status, _, result = self.json("POST", f"/snapshots/{sid}/verify-chunk", {
            "index": 0,
            "data_base64": base64.b64encode(b"tampered").decode("ascii"),
        })
        self.assertEqual(status, 200)
        self.assertFalse(result["verified"])

        # Merkle 证明端点
        status, _, proof = self.json("GET", f"/snapshots/{sid}/chunks/0/proof")
        self.assertEqual(status, 200)
        self.assertTrue(proof["valid"])
        self.assertEqual(proof["root"], manifest["content_digest"])

    def test_error_responses(self) -> None:
        status, _, err = self.json("GET", "/periods/P-none")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "PERIOD_NOT_FOUND")

        status, _, err = self.json("POST", "/periods", {"period_id": "P-bad"})
        self.assertEqual(status, 422)

        self.seal_via_api("P-HTTP-3")
        status, _, err = self.json("POST", "/periods/P-HTTP-3/inputs", {
            "lines": [{"vehicle_model": "车型丙", "quantity": 1, "unit_price": 1}],
            "actor": "企业申报员",
        })
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "INVALID_STATE")

        status, _, err = self.json("POST", "/periods/P-HTTP-3/sign", {"signer_id": "s-fin", "signature": "x"})
        self.assertEqual(status, 409)

        req = urllib.request.Request(self.base + "/periods", data=b"{not json", method="POST")
        try:
            with urllib.request.urlopen(req):
                self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_late_material_reopen_and_diff_over_http(self) -> None:
        snapshot = self.seal_via_api("P-HTTP-4")
        sid = snapshot["snapshot_id"]

        # 迟到材料 → 下一版
        status, _, queued = self.json("POST", "/periods/P-HTTP-4/late-material", {
            "strategy": "next_version",
            "materials": {"lines": [{"vehicle_model": "车型丙", "quantity": 1, "unit_price": "10.00"}]},
            "reason": "签发后补单", "author": "企业申报员",
        })
        self.assertEqual(status, 201)
        self.assertEqual(queued["status"], "已进入下一版")

        # 重开：同一人批准被禁止，独立监管审计员批准生效
        status, _, request = self.json("POST", f"/snapshots/{sid}/reopen-requests", {
            "requester": "核算专员", "reason": "口径错误",
        })
        self.assertEqual(status, 201)
        rid = request["request_id"]
        status, _, err = self.json("POST", f"/reopen-requests/{rid}/approve", {
            "approver": "核算专员", "role": "监管审计员",
        })
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "NOT_INDEPENDENT")
        status, _, approved = self.json("POST", f"/reopen-requests/{rid}/approve", {
            "approver": "审计负责人", "role": "监管审计员",
        })
        self.assertEqual(status, 200)
        self.assertEqual(approved["period"]["state"], "草稿")
        self.assertEqual(approved["period"]["version"], 2)

        # 封账前后差异：排队材料已进入工作稿
        status, _, diff = self.json("GET", f"/periods/P-HTTP-4/diff-working?snapshot_id={sid}")
        self.assertEqual(status, 200)
        self.assertEqual([line["vehicle_model"] for line in diff["lines"]["added"]], ["车型丙"])
        self.assertEqual(diff["totals"]["amount"]["delta"], "10.00")

        # 审计留档可追溯
        status, _, audit = self.json("GET", "/periods/P-HTTP-4/audit")
        self.assertEqual(status, 200)
        actions = [event["action"] for event in audit["events"]]
        self.assertIn("SEALED", actions)
        self.assertIn("REOPEN_APPROVED", actions)
        self.assertIn("VERSION_ROLLED_OVER", actions)


if __name__ == "__main__":
    unittest.main()
