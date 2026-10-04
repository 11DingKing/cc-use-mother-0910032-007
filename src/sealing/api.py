"""封账服务的 HTTP API（纯标准库实现）。

调用方通过请求头声明身份（角色为中文，需百分号编码）：
  X-Actor-Id:   操作人编号
  X-Actor-Role: 企业申报员 / 核算专员 / 交易运营员 / 监管审计员（URL 编码）
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from .errors import DomainError
from .models import Actor, ALL_ROLES
from .service import LedgerSealingService

STATUS_BY_CODE = {
    "validation": 400,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}

RouteHandler = Callable[["Request"], Any]


class Request:
    def __init__(self, body: dict, params: dict, query: dict, actor: Actor) -> None:
        self.body = body
        self.params = params
        self.query = query
        self.actor = actor


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, RouteHandler]] = []

    def add(self, method: str, pattern: str, handler: RouteHandler) -> None:
        self._routes.append((method, re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str) -> tuple[RouteHandler, dict] | None:
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            matched = regex.match(path)
            if matched:
                return handler, matched.groupdict()
        return None


def build_router(service: LedgerSealingService) -> Router:
    router = Router()

    router.add("GET", r"/health", lambda req: {"ok": True, "service": "annual-ledger-sealing"})
    router.add(
        "GET", r"/snapshots",
        lambda req: {"snapshots": service.list_snapshots(req.query.get("period"))},
    )
    router.add(
        "POST", r"/periods/(?P<period>[^/]+)/snapshots",
        lambda req: service.create_snapshot(
            req.params["period"],
            req.actor,
            statutory_signatures=int(req.body.get("statutory_signatures", 3)),
        ),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)",
        lambda req: service.get_snapshot(req.params["sid"]),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/inputs",
        lambda req: service.add_input(req.params["sid"], req.actor, **req.body),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/rule-set",
        lambda req: service.attach_rule_set(
            req.params["sid"],
            req.actor,
            rule_set_id=req.body["rule_set_id"],
            version=req.body["version"],
            rules=req.body.get("rules", []),
        ),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/adjustments",
        lambda req: service.add_adjustment(req.params["sid"], req.actor, **req.body),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/submit",
        lambda req: service.submit(req.params["sid"], req.actor),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/compute",
        lambda req: service.compute_totals(req.params["sid"], req.actor),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/signatures",
        lambda req: service.sign(
            req.params["sid"], req.actor, signer_name=req.body.get("signer_name")
        ),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/late-materials",
        lambda req: service.register_late_material(
            req.params["sid"],
            req.actor,
            kind=req.body["kind"],
            route=req.body["route"],
            fields=req.body.get("fields", {}),
        ),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/corrections",
        lambda req: service.create_correction(
            req.params["sid"],
            req.actor,
            reason=req.body["reason"],
            adjustments=req.body.get("adjustments", []),
        ),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)/corrections",
        lambda req: {"corrections": service.list_corrections(req.params["sid"])},
    )
    router.add(
        "POST", r"/corrections/(?P<cid>[^/]+)/decision",
        lambda req: service.decide_correction(
            req.params["cid"], req.actor, approve=bool(req.body.get("approve"))
        ),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/reopen-requests",
        lambda req: service.request_reopen(
            req.params["sid"], req.actor, reason=req.body["reason"]
        ),
    )
    router.add(
        "POST", r"/reopen-requests/(?P<rid>[^/]+)/decision",
        lambda req: service.decide_reopen(
            req.params["rid"], req.actor, approve=bool(req.body.get("approve"))
        ),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/exports",
        lambda req: service.create_export(req.params["sid"], req.actor),
    )
    router.add(
        "GET", r"/exports/(?P<eid>[^/]+)",
        lambda req: service.export_manifest(req.params["eid"]),
    )
    router.add(
        "GET", r"/exports/(?P<eid>[^/]+)/chunks/(?P<index>\d+)",
        lambda req: service.get_export_chunk(req.params["eid"], int(req.params["index"])),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)/chunks/(?P<index>\d+)/proof",
        lambda req: service.chunk_proof(req.params["sid"], int(req.params["index"])),
    )
    router.add(
        "POST", r"/snapshots/(?P<sid>[^/]+)/chunks/(?P<index>\d+)/verify",
        lambda req: service.verify_chunk(
            req.params["sid"], int(req.params["index"]), req.body["chunk_hex"]
        ),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)/seal-integrity",
        lambda req: service.seal_integrity(req.params["sid"]),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)/diff/(?P<other>[^/]+)",
        lambda req: service.diff_snapshots(req.params["sid"], req.params["other"]),
    )
    router.add(
        "GET", r"/snapshots/(?P<sid>[^/]+)/effective",
        lambda req: service.effective_view(req.params["sid"]),
    )
    return router


def make_handler(service: LedgerSealingService) -> type[BaseHTTPRequestHandler]:
    router = build_router(service)

    class SealingHandler(BaseHTTPRequestHandler):
        server_version = "LedgerSealing/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
            return

        def _handle(self, method: str) -> None:
            path, _, raw_query = self.path.partition("?")
            matched = router.match(method, path)
            if matched is None:
                self._respond(404, {"error": {"code": "not_found", "message": "路由不存在"}})
                return
            handler, params = matched
            query = {
                key: values[0]
                for key, values in parse_qs(raw_query).items()
            }
            try:
                body = self._read_body()
                actor = self._read_actor()
                result = handler(Request(body, params, query, actor))
                self._respond(200, result if result is not None else {"ok": True})
            except DomainError as exc:
                self._respond(STATUS_BY_CODE.get(exc.code, 400), exc.to_dict())
            except (KeyError, ValueError, TypeError) as exc:
                self._respond(
                    400,
                    {"error": {"code": "validation", "message": f"请求参数不合法：{exc}"}},
                )

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _read_actor(self) -> Actor:
            actor_id = self.headers.get("X-Actor-Id", "anonymous")
            # 角色为中文，按百分号编码传输（HTTP 头仅支持 latin-1）
            role = unquote(self.headers.get("X-Actor-Role", ""))
            if role and role not in ALL_ROLES:
                raise DomainError("validation", f"未知角色：{role}")
            return Actor(actor_id=actor_id, role=role)

        def _respond(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = lambda self: self._handle("GET")  # noqa: E731
        do_POST = lambda self: self._handle("POST")  # noqa: E731

    return SealingHandler


def run_server(
    service: LedgerSealingService, host: str = "127.0.0.1", port: int = 8091
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"封账服务已启动：http://{host}:{port}")
    server.serve_forever()
    return server
