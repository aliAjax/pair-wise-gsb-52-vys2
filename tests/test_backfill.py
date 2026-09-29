"""旧库迁移：缺少规则快照的在库计划补上原规则快照，且迁移幂等。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service


LEGACY_SCHEMA = """
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

LEGACY_PAYLOAD = {
    "student_id": "S-900", "disability": "vision", "service_minutes": 480,
    "delivered_minutes": 100, "review_due_days": 9, "goals_count": 3, "consent": True,
    "missing_minutes": 380, "compliance_rate": 20.83, "review_overdue": False, "plan_status": "active",
}


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) "
            "VALUES('IEP-00001','active',1,?,'m','m','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (json.dumps(LEGACY_PAYLOAD, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(1,'created','m',1,'{}','2026-01-01T00:00:00+00:00')"
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_plans_get_original_rule_snapshot(self):
        from src.domain import Actor
        service = build_service(self.db_path)
        record = service.get_record(Actor("m", "case_manager"), 1)
        snapshot = record["payload"]["rule_snapshot"]
        self.assertTrue(snapshot["backfilled"])
        self.assertEqual(snapshot["version_no"], 1)
        self.assertEqual(snapshot["service_cap"], 600)
        self.assertEqual(snapshot["review_cycle_days"], 30)
        # 旧计划仍按补入的原规则核对
        self.assertEqual(record["evaluation"]["service_cap"], 600)
        self.assertEqual(record["evaluation"]["review_cycle_days"], 30)
        self.assertTrue(record["evaluation"]["within_cap"])
        # 计划自己的审计时间线不被污染
        timeline = service.timeline(Actor("m", "case_manager"), 1)
        self.assertEqual([e["action"] for e in timeline], ["created"])
        # 规则时间线能看到一次补快照记录
        rule_actions = [e["action"] for e in service.rule_timeline(Actor("m", "case_manager"))]
        self.assertEqual(rule_actions.count("snapshot_backfill"), 1)

    def test_backfill_is_idempotent(self):
        from src.domain import Actor
        build_service(self.db_path)
        service = build_service(self.db_path)
        record = service.get_record(Actor("m", "case_manager"), 1)
        self.assertTrue(record["payload"]["rule_snapshot"]["backfilled"])
        self.assertEqual(
            [e["action"] for e in service.rule_timeline(Actor("m", "case_manager"))].count("snapshot_backfill"),
            1,
        )
