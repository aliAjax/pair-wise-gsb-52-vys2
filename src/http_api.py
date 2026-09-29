"""HTTP 路由与统一错误输出。

计划接口与规则接口分开挂载：
- /api/records/...：支持计划建档、流转、复查；
- /api/rules/...：区定规则草稿、发布、回滚；
- /api/timeline：规则+计划的合并时间线。
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
RECORD_TIMELINE_RE = re.compile(r"^/api/records/(\d+)/timeline$")
RULE_VERSION_RE = re.compile(r"^/api/rules/(\d+)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "special-education/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        @staticmethod
        def _expected_version(body: dict) -> int:
            version = body.get("expected_version")
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValidationError("expected_version必须是整数")
            return version

        @staticmethod
        def _expected_revision(body: dict) -> int:
            revision = body.get("expected_revision")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise ValidationError("expected_revision必须是整数")
            return revision

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        # ---- GET ----
        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/health":
                    self._send(200, {"status": "ok", "service": "special-education", "database": service.repository.health()})
                    return
                if path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = AUDIT_RE.match(path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                match = RECORD_TIMELINE_RE.match(path)
                if match:
                    self._send(200, {"items": service.combined_timeline(self._actor(), int(match.group(1)))})
                    return
                match = RECORD_RE.match(path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                if path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if path == "/api/rules":
                    self._send(200, {"items": service.list_rules(self._actor())})
                    return
                if path == "/api/rules/current":
                    self._send(200, service.current_rule(self._actor()))
                    return
                if path == "/api/rules/timeline":
                    self._send(200, {"items": service.rule_timeline(self._actor())})
                    return
                match = RULE_VERSION_RE.match(path)
                if match:
                    self._send(200, service.get_rule(self._actor(), int(match.group(1))))
                    return
                if path == "/api/timeline":
                    self._send(200, {"items": service.combined_timeline(self._actor())})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        # ---- POST ----
        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._body()
                if path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(path)
                if match:
                    version = self._expected_version(body)
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if path == "/api/rules/drafts":
                    self._send(201, service.create_rule_draft(self._actor(), body.get("data", {})))
                    return
                if path == "/api/rules/drafts/revise":
                    version = self._expected_version(body)
                    revision = self._expected_revision(body)
                    self._send(200, service.revise_rule_draft(self._actor(), version, revision, body.get("data", {})))
                    return
                if path == "/api/rules/drafts/publish":
                    version = self._expected_version(body)
                    revision = self._expected_revision(body)
                    self._send(200, service.publish_rule_draft(self._actor(), version, revision, body.get("data", {})))
                    return
                if path == "/api/rules/drafts/discard":
                    version = self._expected_version(body)
                    revision = self._expected_revision(body)
                    self._send(200, service.discard_rule_draft(self._actor(), version, revision))
                    return
                if path == "/api/rules/rollback":
                    target = body.get("target_version")
                    if isinstance(target, bool) or not isinstance(target, int):
                        raise ValidationError("target_version必须是整数")
                    self._send(200, service.rollback_rule(self._actor(), target))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
