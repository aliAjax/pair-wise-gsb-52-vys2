"""计划建档快照、调规不影响在用计划、复查改用新版本、旧库回填。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, ValidationError
from src.rule_policy import RulePolicy


ADMIN = Actor("admin-1", "administrator")
MANAGER = Actor("manager-1", "case_manager")
SPECIALIST = Actor("sp-1", "specialist")
PARENT = Actor("parent-1", "parent_rep")

CREATE_DATA = {
    "student_id": "S-300",
    "disability": "autism",
    "service_minutes": 600,
    "delivered_minutes": 0,
    "review_due_days": 15,
    "goals_count": 3,
    "consent": False,
}


def create_consent_activate(service, reference="IEP-40001", data=None, actor=MANAGER):
    data = data or CREATE_DATA
    record = service.create(actor, reference, data)
    record = service.act(PARENT, record["id"], record["version"], "consent",
                         {"guardian_confirmed": True, "consent_scope": "个别化服务"})
    record = service.act(MANAGER, record["id"], record["version"], "activate", {})
    return record


class PlanSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_is_bound_to_rule_snapshot_at_creation(self):
        record = self.service.create(MANAGER, "IEP-40001", CREATE_DATA)
        snapshot = record["payload"]["rule_snapshot"]
        self.assertEqual(snapshot["rule_version"], 1)
        self.assertEqual(snapshot["service_cap"], RulePolicy().baseline()["service_cap"])
        self.assertEqual(record["payload"]["rule_version"], 1)

    def test_creation_must_respect_current_rule_cap(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 500, "review_cycle_days": 20})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        with self.assertRaises(ValidationError) as caught:
            self.service.create(MANAGER, "IEP-40001", CREATE_DATA)
        self.assertIn("服务上限", str(caught.exception))

    def test_creation_must_respect_current_review_cycle(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 2000, "review_cycle_days": 10})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        with self.assertRaises(ValidationError) as caught:
            self.service.create(MANAGER, "IEP-40001", CREATE_DATA)
        self.assertIn("复查周期", str(caught.exception))

    def test_rule_change_does_not_affect_existing_plan(self):
        record = create_consent_activate(self.service)
        self.assertEqual(record["payload"]["rule_version"], 1)
        # 区里把服务上限调低到500，旧计划仍按建档时的1000核对。
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 500, "review_cycle_days": 10})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        record = self.service.act(SPECIALIST, record["id"], record["version"], "log_service",
                                  {"session_minutes": 400, "provider": "SP-9"})
        self.assertEqual(record["payload"]["delivered_minutes"], 400)
        # 快照仍然是建档时的v1。
        self.assertEqual(record["payload"]["rule_snapshot"]["rule_version"], 1)
        self.assertEqual(record["payload"]["rule_snapshot"]["service_cap"], 1000)

    def test_existing_plan_cap_still_checked_under_snapshot(self):
        record = create_consent_activate(self.service)
        # 一次性登记超过建档快照上限（1000分钟）应被拒。
        big = dict(CREATE_DATA)
        with self.assertRaises(ValidationError):
            self.service.act(SPECIALIST, record["id"], record["version"], "log_service",
                             {"session_minutes": 1001, "provider": "SP-9"})

    def test_review_adopt_new_rule_requires_reason_and_creates_new_plan_version(self):
        record = create_consent_activate(self.service)
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": "复查"})
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 45})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        before_version = record["version"]
        record = self.service.act(MANAGER, record["id"], record["version"], "amend", {
            "amendment_reason": "阶段调整",
            "updated_goals": ["目标X"],
            "rule_change_reason": "家长与学校同意按新周期复查",
        })
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["version"], before_version + 1)
        self.assertEqual(record["payload"]["rule_version"], 2)
        self.assertEqual(record["payload"]["rule_snapshot"]["service_cap"], 900)
        self.assertEqual(record["payload"]["review_due_days"], 45)
        timeline = self.service.timeline(MANAGER, record["id"])
        adopt_event = timeline[-1]
        self.assertEqual(adopt_event["action"], "amend")
        change = adopt_event["details"]["rule_change"]
        self.assertEqual(change["from_version"], 1)
        self.assertEqual(change["to_version"], 2)
        self.assertIn("service_cap", change["diff"])
        self.assertIn("review_cycle_days", change["diff"])

    def test_amend_without_reason_keeps_old_rule(self):
        record = create_consent_activate(self.service)
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": "复查"})
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 45})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        record = self.service.act(MANAGER, record["id"], record["version"], "amend",
                                  {"amendment_reason": "阶段调整", "updated_goals": ["目标X"]})
        self.assertEqual(record["payload"]["rule_version"], 1)

    def test_adopt_when_already_current_is_rejected(self):
        record = create_consent_activate(self.service)
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": "复查"})
        with self.assertRaises(ValidationError) as caught:
            self.service.act(MANAGER, record["id"], record["version"], "amend", {
                "amendment_reason": "调整",
                "updated_goals": ["目标X"],
                "rule_change_reason": "想切一下",
            })
        self.assertIn("无需改用", str(caught.exception))


class LegacyBackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")

    def tearDown(self):
        self.temp.cleanup()

    def _insert_legacy_record(self):
        now = "2026-01-01T00:00:00+00:00"
        legacy_payload = {
            "student_id": "S-LEGACY",
            "disability": "hearing",
            "service_minutes": 600,
            "delivered_minutes": 120,
            "review_due_days": 15,
            "goals_count": 4,
            "consent": True,
            "missing_minutes": 480,
            "compliance_rate": 20.0,
            "review_overdue": False,
            "plan_status": "active",
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                ("LEGACY-1", "active", 3, json.dumps(legacy_payload, ensure_ascii=False), "mgr", "mgr", now, now),
            )

    def test_legacy_plans_get_snapshot_without_version_change(self):
        self._insert_legacy_record()
        service = build_service(self.db_path)
        record = service.get_record(ADMIN, 1)
        snapshot = record["payload"]["rule_snapshot"]
        self.assertEqual(snapshot["rule_version"], 1)
        self.assertEqual(snapshot["service_cap"], RulePolicy().baseline()["service_cap"])
        # 业务版本号不被回填改动。
        self.assertEqual(record["version"], 3)
        timeline = service.timeline(ADMIN, 1)
        actions = [event["action"] for event in timeline]
        self.assertIn("rule_snapshot_backfilled", actions)
        # 回填幂等：重新构造服务不会重复补。
        service2 = build_service(self.db_path)
        timeline2 = service2.timeline(ADMIN, 1)
        self.assertEqual(
            [e["action"] for e in timeline2].count("rule_snapshot_backfilled"),
            1,
        )
