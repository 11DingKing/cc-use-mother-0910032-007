"""封账服务的 HTTP API（仅依赖标准库）。

导出与续传约定：
- GET /snapshots/{id}/export 返回规范字节流，ETag 为内容 Merkle 根；
  If-None-Match 命中返回 304，重复导出内容恒定。
- 支持 Range: bytes=start-end / bytes=start- / bytes=-suffix 断点续传，
  响应 206 与 Content-Range；越界返回 416。
- GET /snapshots/{id}/chunks/{i} 逐块下载，POST /snapshots/{id}/verify-chunk
  校验任一分块是否归于封存根。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, Validation
from .service import SealingService


class Response(NamedTuple):
    status: int
    body: bytes
    headers: dict[str, str]


def _json_response(status: int, value: Any, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return Response(status, body, {"Content-Type": "application/json; charset=utf-8", **(headers or {})})


def _error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    return _json_response(status, {"error": {"code": code, "message": message}}, headers)


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """解析单段字节区间，返回闭区间 (start, end)；不合法抛 416。"""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise DomainError("Range 头格式不支持", code="RANGE_NOT_SATISFIABLE", status=416)
    start_text, end_text = match.group(1), match.group(2)
    if not start_text:  # suffix: 最后 N 字节
        length = int(end_text)
        if length <= 0:
            raise DomainError("Range 后辍长度必须为正", code="RANGE_NOT_SATISFIABLE", status=416)
        start, end = max(size - length, 0), size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size or start > end:
        raise DomainError("Range 超出内容范围", code="RANGE_NOT_SATISFIABLE", status=416)
    return start, min(end, size - 1)


class SealingAPI:
    """把 HTTP 请求映射到 SealingService 用例，统一错误响应。"""

    def __init__(self, service: SealingService) -> None:
        self._service = service
        self._routes: list[tuple[str, re.Pattern, Callable]] = [
            ("GET", re.compile(r"/health"), self._health),
            ("POST", re.compile(r"/periods"), self._create_period),
            ("GET", re.compile(r"/periods/(?P<pid>[^/]+)"), self._get_period),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/inputs"), self._add_inputs),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/submit"), self._submit),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/adjustments"), self._add_adjustment),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/confirm"), self._confirm),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/sign"), self._sign),
            ("POST", re.compile(r"/periods/(?P<pid>[^/]+)/late-material"), self._late_material),
            ("GET", re.compile(r"/periods/(?P<pid>[^/]+)/audit"), self._audit),
            ("GET", re.compile(r"/periods/(?P<pid>[^/]+)/snapshots"), self._list_snapshots),
            ("GET", re.compile(r"/periods/(?P<pid>[^/]+)/diff-working"), self._diff_working),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)"), self._get_snapshot),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)/manifest"), self._get_manifest),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)/export"), self._export),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)/chunks/(?P<index>\d+)"), self._get_chunk),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)/chunks/(?P<index>\d+)/proof"), self._chunk_proof),
            ("POST", re.compile(r"/snapshots/(?P<sid>[^/]+)/verify-chunk"), self._verify_chunk),
            ("POST", re.compile(r"/snapshots/(?P<sid>[^/]+)/reopen-requests"), self._request_reopen),
            ("GET", re.compile(r"/snapshots/(?P<sid>[^/]+)/diff/(?P<other>[^/]+)"), self._diff_snapshots),
            ("POST", re.compile(r"/corrections/(?P<cid>[^/]+)/approve"), self._approve_correction),
            ("POST", re.compile(r"/reopen-requests/(?P<rid>[^/]+)/approve"), self._approve_reopen),
        ]

    # 路由匹配顺序注意：/snapshots/{sid} 精确段在前，带子路径的会被先匹配到对应模式
    def handle(self, method: str, raw_path: str, body: bytes, headers: dict[str, str]) -> Response:
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            payload: dict = {}
            if body:
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise Validation("请求体必须是 JSON 对象")
            for route_method, pattern, handler in self._routes:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if match:
                    return handler(match, query, payload, headers)
            return _error_response(404, "ROUTE_NOT_FOUND", f"路由不存在：{method} {path}")
        except DomainError as exc:
            extra = {}
            return _error_response(exc.status, exc.code, exc.message, extra)
        except json.JSONDecodeError:
            return _error_response(400, "BAD_JSON", "请求体不是合法 JSON")
        except Exception:  # pragma: no cover - 兜底
            traceback.print_exc()
            return _error_response(500, "INTERNAL", "服务内部错误")

    # ------------------------------------------------------------------
    # 期间与签署
    # ------------------------------------------------------------------
    def _health(self, match, query, body, headers) -> Response:
        return _json_response(200, {"status": "ok"})

    def _create_period(self, match, query, body, headers) -> Response:
        view = self._service.create_period(
            period_id=str(body.get("period_id") or ""),
            year=body.get("year"),
            quorum=body.get("quorum"),
            signers=body.get("signers") or [],
            ruleset=body.get("ruleset") or {},
            actor=str(body.get("actor") or "系统"),
        )
        return _json_response(201, view)

    def _get_period(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.get_period(match.group("pid")))

    def _add_inputs(self, match, query, body, headers) -> Response:
        view = self._service.add_inputs(
            match.group("pid"),
            lines=body.get("lines") or [],
            actor=str(body.get("actor") or "系统"),
        )
        return _json_response(200, view)

    def _submit(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.submit(match.group("pid"), actor=str(body.get("actor") or "系统")))

    def _add_adjustment(self, match, query, body, headers) -> Response:
        view = self._service.add_adjustment(
            match.group("pid"),
            adjustment=body.get("adjustment") or {},
            actor=str(body.get("actor") or "系统"),
        )
        return _json_response(200, view)

    def _confirm(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.confirm(match.group("pid"), actor=str(body.get("actor") or "系统")))

    def _sign(self, match, query, body, headers) -> Response:
        view = self._service.sign(
            match.group("pid"),
            signer_id=str(body.get("signer_id") or ""),
            signature=str(body.get("signature") or ""),
        )
        return _json_response(200, view)

    # ------------------------------------------------------------------
    # 快照、导出与分块
    # ------------------------------------------------------------------
    def _get_snapshot(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.get_snapshot(match.group("sid")))

    def _get_manifest(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.get_snapshot(match.group("sid")))

    def _list_snapshots(self, match, query, body, headers) -> Response:
        return _json_response(200, {"snapshots": self._service.list_snapshots(match.group("pid"))})

    def _export(self, match, query, body, headers) -> Response:
        payload, etag = self._service.export_snapshot(match.group("sid"), actor=str(body.get("actor") or "系统"))
        etag_header = f'"{etag}"'
        base_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "ETag": etag_header,
            "Accept-Ranges": "bytes",
            "X-Content-Digest": etag,
            "X-Payload-SHA256": self._service.get_snapshot(match.group("sid"))["payload_sha256"],
        }
        if headers.get("if-none-match") and etag_header in headers["if-none-match"]:
            return Response(304, b"", base_headers)
        range_header = headers.get("range")
        if range_header:
            try:
                start, end = _parse_range(range_header, len(payload))
            except DomainError as exc:
                return _error_response(
                    exc.status,
                    exc.code,
                    exc.message,
                    {"Content-Range": f"bytes */{len(payload)}", **base_headers},
                )
            return Response(
                206,
                payload[start : end + 1],
                {**base_headers, "Content-Range": f"bytes {start}-{end}/{len(payload)}"},
            )
        return Response(200, payload, base_headers)

    def _get_chunk(self, match, query, body, headers) -> Response:
        chunk, digest, count = self._service.get_chunk(match.group("sid"), int(match.group("index")))
        return Response(
            200,
            chunk,
            {
                "Content-Type": "application/octet-stream",
                "X-Chunk-Index": match.group("index"),
                "X-Chunk-SHA256": digest,
                "X-Chunk-Count": str(count),
            },
        )

    def _chunk_proof(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.chunk_proof(match.group("sid"), int(match.group("index"))))

    def _verify_chunk(self, match, query, body, headers) -> Response:
        try:
            raw = base64.b64decode(str(body.get("data_base64") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Validation("data_base64 不是合法的 Base64") from exc
        result = self._service.verify_chunk(match.group("sid"), index=int(body.get("index", -1)), data=raw)
        return _json_response(200, result)

    # ------------------------------------------------------------------
    # 迟到材料、更正与重开
    # ------------------------------------------------------------------
    def _late_material(self, match, query, body, headers) -> Response:
        result = self._service.submit_late_material(
            match.group("pid"),
            strategy=str(body.get("strategy") or ""),
            materials=body.get("materials") or {},
            reason=str(body.get("reason") or ""),
            author=str(body.get("author") or "系统"),
        )
        return _json_response(201, result)

    def _approve_correction(self, match, query, body, headers) -> Response:
        result = self._service.approve_correction(
            match.group("cid"),
            approver=str(body.get("approver") or ""),
            role=str(body.get("role") or ""),
        )
        return _json_response(200, result)

    def _request_reopen(self, match, query, body, headers) -> Response:
        result = self._service.request_reopen(
            match.group("sid"),
            requester=str(body.get("requester") or ""),
            reason=str(body.get("reason") or ""),
        )
        return _json_response(201, result)

    def _approve_reopen(self, match, query, body, headers) -> Response:
        result = self._service.approve_reopen(
            match.group("rid"),
            approver=str(body.get("approver") or ""),
            role=str(body.get("role") or ""),
        )
        return _json_response(200, result)

    # ------------------------------------------------------------------
    # 差异与审计
    # ------------------------------------------------------------------
    def _diff_snapshots(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.diff_snapshots(match.group("sid"), match.group("other")))

    def _diff_working(self, match, query, body, headers) -> Response:
        return _json_response(200, self._service.diff_working(match.group("pid"), snapshot_id=query.get("snapshot_id")))

    def _audit(self, match, query, body, headers) -> Response:
        return _json_response(200, {"events": self._service.audit_trail(match.group("pid"))})


def create_server(service: SealingService, host: str, port: int) -> ThreadingHTTPServer:
    """创建 HTTP 服务实例。"""
    api = SealingAPI(service)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = api.handle(method, self.path, body, headers)
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
            return

    return ThreadingHTTPServer((host, port), Handler)
