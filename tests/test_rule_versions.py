"""规则版本、计划快照与复查改用新版本的端到端测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.rule_policy import RulePolicy


ADMIN = Actor("admin-1", "administrator")
MANAGER = Actor("manager-1", "case_manager")
SPECIALIST = Actor("sp-1", "specialist")
PARENT = Actor("parent-1", "parent_rep")

CREATE_DATA = {
    "student_id": "S-200",
    "disability": "hearing",
    "service_minutes": 600,
    "delivered_minutes": 0,
    "review_due_days": 15,
    "goals_count": 3,
    "consent": False,
}


def consent_to_active(service, reference="IEP-30001", data=None, actor=MANAGER):
    data = data or CREATE_DATA
    record = service.create(actor, reference, data)
    record = service.act(PARENT, record["id"], record["version"], "consent",
                         {"guardian_confirmed": True, "consent_scope": "个别化服务"})
    record = service.act(MANAGER, record["id"], record["version"], "activate", {})
    return record


def to_under_review(service, record):
    return service.act(ADMIN, record["id"], record["version"], "review", {"progress_note": "阶段复查"})


def advance_to_review(service, reference="IEP-30001", data=None):
    return to_under_review(service, consent_to_active(service, reference, data))


class RuleVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_baseline_effective_on_fresh_db(self):
        rules = self.service.list_rules(ADMIN)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["version"], 1)
        self.assertEqual(rules[0]["status"], "effective")

    def test_draft_revise_publish_immediately(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 800, "review_cycle_days": 20})
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["version"], 2)
        revised = self.service.revise_rule_draft(
            ADMIN, draft["version"], draft["revision"], {"service_cap": 750, "review_cycle_days": 20}
        )
        self.assertEqual(revised["service_cap"], 750)
        published = self.service.publish_rule_draft(ADMIN, revised["version"], revised["revision"], {})
        self.assertEqual(published["status"], "effective")
        rules = self.service.list_rules(ADMIN)
        self.assertEqual({r["status"] for r in rules}, {"effective", "superseded"})
        current = self.service.current_rule(ADMIN)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["service_cap"], 750)

    def test_scheduled_draft_becomes_effective_in_order(self):
        clock = self.service.clock
        clock.set_fixed("2026-09-01T00:00:00+00:00")
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 25})
        scheduled = self.service.publish_rule_draft(
            ADMIN, draft["version"], draft["revision"], {"effective_at": "2026-10-01T00:00:00+00:00"}
        )
        self.assertEqual(scheduled["status"], "scheduled")
        # 未到点，仍是v1生效。
        self.assertEqual(self.service.current_rule(ADMIN)["version"], 1)
        clock.set_fixed("2026-10-02T00:00:00+00:00")
        self.assertEqual(self.service.current_rule(ADMIN)["version"], 2)

    def test_non_admin_cannot_manage_rules(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.create_rule_draft(MANAGER, {"service_cap": 1, "review_cycle_days": 1})

    def test_only_one_pending_draft(self):
        self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 25})
        with self.assertRaises(Conflict):
            self.service.create_rule_draft(ADMIN, {"service_cap": 700, "review_cycle_days": 10})

    def test_stale_revision_fails_with_refresh_hint(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 25})
        self.service.revise_rule_draft(ADMIN, draft["version"], draft["revision"],
                                       {"service_cap": 880, "review_cycle_days": 25})
        with self.assertRaises(Conflict) as caught:
            self.service.revise_rule_draft(ADMIN, draft["version"], draft["revision"],
                                           {"service_cap": 700, "review_cycle_days": 25})
        self.assertIn("刷新", str(caught.exception))

    def test_late_publish_after_another_commit_fails(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 900, "review_cycle_days": 25})
        revised = self.service.revise_rule_draft(ADMIN, draft["version"], draft["revision"],
                                                  {"service_cap": 880, "review_cycle_days": 24})
        # 拿着旧修订号来发布，必须失败并提示先刷新。
        with self.assertRaises(Conflict) as caught:
            self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        self.assertIn("刷新", str(caught.exception))
        # 新修订仍可正常发布。
        published = self.service.publish_rule_draft(ADMIN, revised["version"], revised["revision"], {})
        self.assertEqual(published["status"], "effective")

    def test_rollback_creates_new_effective_version(self):
        draft = self.service.create_rule_draft(ADMIN, {"service_cap": 500, "review_cycle_days": 10})
        self.service.publish_rule_draft(ADMIN, draft["version"], draft["revision"], {})
        self.assertEqual(self.service.current_rule(ADMIN)["version"], 2)
        rolled = self.service.rollback_rule(ADMIN, 1)
        self.assertEqual(rolled["version"], 3)
        self.assertEqual(rolled["status"], "effective")
        self.assertEqual(rolled["service_cap"], RulePolicy().baseline()["service_cap"])
        # v1没有被复活，而是保持被取代状态。
        v1 = self.service.get_rule(ADMIN, 1)
        self.assertEqual(v1["status"], "superseded")
        v2 = self.service.get_rule(ADMIN, 2)
        self.assertEqual(v2["status"], "superseded")

    def test_rollback_only_for_superseded(self):
        with self.assertRaises(Conflict):
            self.service.rollback_rule(ADMIN, 1)
