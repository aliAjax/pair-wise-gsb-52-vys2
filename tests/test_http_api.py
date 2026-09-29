"""HTTP接口冒烟测试：规则、计划路由与409刷新提示。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from app import build_service
from src.http_api import create_server


BASE_DIR = Path(__file__).resolve().parent.parent


class HttpClient:
    def __init__(self, base_url: str, role: str = "administrator") -> None:
        self.base_url = base_url
        self.role = role

    def request(self, method: str, path: str, payload=None):
        data = None
        headers = {"X-User-Id": "tester", "X-Role": self.role, "X-Org": "test"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.service = build_service(str(Path(cls.temp.name) / "http.db"))
        cls.server = create_server("127.0.0.1", 0, cls.service, BASE_DIR / "static")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        self.admin = HttpClient(self.base, "administrator")
        self.manager = HttpClient(self.base, "case_manager")

    def _publish_rule(self, cap: int, cycle: int):
        status, draft = self.admin.request(
            "POST", "/api/rules/drafts", {"data": {"service_cap": cap, "review_cycle_days": cycle}}
        )
        self.assertEqual(status, 201)
        status, published = self.admin.request(
            "POST", "/api/rules/drafts/publish",
            {"expected_version": draft["version"], "expected_revision": draft["revision"]},
        )
        self.assertEqual(status, 200)
        return published

    def test_rule_flow_over_http_and_conflict(self):
        status, rules = self.admin.request("GET", "/api/rules")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["status"] == "effective" for item in rules["items"]))

        status, current = self.admin.request("GET", "/api/rules/current")
        self.assertEqual(status, 200)
        self.assertEqual(current["status"], "effective")

        status, draft = self.admin.request(
            "POST", "/api/rules/drafts", {"data": {"service_cap": 900, "review_cycle_days": 25}}
        )
        self.assertEqual(status, 201)
        self.assertEqual(draft["status"], "draft")

        status, revised = self.admin.request(
            "POST", "/api/rules/drafts/revise",
            {"expected_version": draft["version"], "expected_revision": draft["revision"],
             "data": {"service_cap": 880, "review_cycle_days": 25}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(revised["revision"], 2)

        # 晚到的发布使用旧修订号，必须409并提示刷新。
        status, error = self.admin.request(
            "POST", "/api/rules/drafts/publish",
            {"expected_version": draft["version"], "expected_revision": draft["revision"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "conflict")
        self.assertIn("刷新", error["message"])

        status, published = self.admin.request(
            "POST", "/api/rules/drafts/publish",
            {"expected_version": revised["version"], "expected_revision": revised["revision"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "effective")

    def test_plan_creation_bound_to_current_rule(self):
        self._publish_rule(880, 25)
        status, record = self.manager.request(
            "POST", "/api/records",
            {"reference": "HTTP-1", "data": {
                "student_id": "S-HTTP", "disability": "hearing",
                "service_minutes": 2000, "delivered_minutes": 0,
                "review_due_days": 10, "goals_count": 2, "consent": False}},
        )
        # 当前生效规则服务上限880，建档应被拒。
        self.assertEqual(status, 422)
        self.assertIn("服务上限", record["message"])

        status, record = self.manager.request(
            "POST", "/api/records",
            {"reference": "HTTP-1", "data": {
                "student_id": "S-HTTP", "disability": "hearing",
                "service_minutes": 600, "delivered_minutes": 0,
                "review_due_days": 10, "goals_count": 2, "consent": False}},
        )
        self.assertEqual(status, 201)
        self.assertEqual(record["payload"]["rule_snapshot"]["service_cap"], 880)

    def test_non_admin_forbidden_on_rule_drafts(self):
        status, error = self.manager.request(
            "POST", "/api/rules/drafts", {"data": {"service_cap": 1, "review_cycle_days": 1}}
        )
        self.assertEqual(status, 403)

    def test_combined_timeline_endpoint(self):
        status, payload = self.admin.request("GET", "/api/timeline")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["items"], list)
        self.assertTrue(any(event["scope"] == "rule" for event in payload["items"]))

    def test_rollback_endpoint(self):
        # 先发一版与v1不同的规则，使其被取代，再回滚v1生成新生效版本。
        status, draft = self.admin.request(
            "POST", "/api/rules/drafts", {"data": {"service_cap": 700, "review_cycle_days": 12}}
        )
        self.admin.request(
            "POST", "/api/rules/drafts/publish",
            {"expected_version": draft["version"], "expected_revision": draft["revision"]},
        )
        status, rolled = self.admin.request("POST", "/api/rules/rollback", {"target_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(rolled["status"], "effective")
        self.assertEqual(rolled["service_cap"], 1000)
        self.assertEqual(rolled["review_cycle_days"], 30)
