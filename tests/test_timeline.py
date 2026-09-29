"""规则+计划合并时间线测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


ADMIN = Actor("admin-1", "administrator")
MANAGER = Actor("manager-1", "case_manager")
PARENT = Actor("parent-1", "parent_rep")

CREATE_DATA = {
    "student_id": "S-500",
    "disability": "hearing",
    "service_minutes": 600,
    "delivered_minutes": 0,
    "review_due_days": 15,
    "goals_count": 3,
    "consent": False,
}


class CombinedTimelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_combined_timeline_shows_rules_and_plans_with_diff(self):
        record = self.service.create(MANAGER, "IEP-50001", CREATE_DATA)
        record = self.service.act(PARENT, record["id"], record["version"], "consent",
                                  {"guardian_confirmed": True, "consent_scope": "全部"})
        record = self.service.act(MANAGER, record["id"], record["version"], "activate", {})
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": "复查"})

        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 800, "review_cycle_days": 40})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        record = self.service.act(MANAGER, record["id"], record["version"], "amend", {
            "amendment_reason": "年度调整",
            "updated_goals": ["新目标"],
            "rule_change_reason": "新周期更合理",
        })

        events = self.service.combined_timeline(ADMIN)
        scopes = [(event["scope"], event["action"]) for event in events]
        self.assertIn(("rule", "rule_draft_created"), scopes)
        self.assertIn(("rule", "rule_effective"), scopes)
        self.assertIn(("plan", "created"), scopes)
        self.assertIn(("plan", "amend"), scopes)
        # 时间排序：整体按时间升序。
        stamps = [event["created_at"] for event in events]
        self.assertEqual(stamps, sorted(stamps))

        plan_adopt = next(
            event for event in events
            if event["scope"] == "plan" and event["action"] == "amend"
        )
        change = plan_adopt["details"]["rule_change"]
        self.assertEqual(change["from_version"], 1)
        self.assertEqual(change["to_version"], 2)
        self.assertEqual(change["diff"]["service_cap"], {"from": 1000, "to": 800})
        self.assertEqual(change["diff"]["review_cycle_days"], {"from": 30, "to": 40})

    def test_record_scoped_combined_timeline(self):
        record = self.service.create(MANAGER, "IEP-50002", CREATE_DATA)
        events = self.service.combined_timeline(ADMIN, record_id=record["id"])
        self.assertTrue(all(event["scope"] == "plan" for event in events))
        self.assertEqual(events[0]["action"], "created")
