"""规则版本化、计划快照固化与复查改用新版本的完整流程。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class RuleLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.service = build_service(str(Path(self.temp.name) / "test.db"), clock=self.clock)
        self.admin = Actor("admin-1", "administrator")

    def tearDown(self):
        self.temp.cleanup()

    def _plan_under_rule_v1(self):
        record = self.service.create(Actor("cm", "case_manager"), "IEP-30001", CREATE_DATA)
        self.assertEqual(record["payload"]["rule_snapshot"]["version_no"], 1)
        self.assertEqual(record["payload"]["rule_snapshot"]["service_cap"], 600)
        self.assertEqual(record["payload"]["rule_snapshot"]["review_cycle_days"], 30)
        return record

    def _publish_v2(self, cap=400, cycle=20, immediate=True):
        draft = self.service.create_rule_draft(self.admin, {"service_cap": cap, "review_cycle_days": cycle, "reason": "秋季调整"})
        if not immediate:
            self.clock.now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
            return self.service.publish_rule_draft(
                self.admin, draft["id"], draft["revision"],
                {"reason": "10月15日生效", "effective_at": "2026-10-15T00:00:00+00:00"},
            )
        return self.service.publish_rule_draft(self.admin, draft["id"], draft["revision"], {"reason": "秋季调整"})

    def test_draft_scheduled_activation(self):
        v2 = self._publish_v2(immediate=False)
        self.assertEqual(v2["status"], "scheduled")
        # 到点前当前生效仍是v1
        self.assertEqual(self.service.current_rule()["version_no"], 1)
        self.clock.now = datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc)
        current = self.service.current_rule()
        self.assertEqual(current["version_no"], 2)
        self.assertEqual(current["status"], "effective")
        rule_rows = [r for r in self.service.list_rules(self.admin)]
        self.assertEqual(next(r for r in rule_rows if r["version_no"] == 1)["status"], "superseded")

    def test_existing_plan_keeps_frozen_snapshot_after_rule_change(self):
        old_plan = self._plan_under_rule_v1()
        self._publish_v2(cap=400, cycle=20)
        fetched = self.service.get_record(Actor("cm", "case_manager"), old_plan["id"])
        snapshot = fetched["payload"]["rule_snapshot"]
        # 区里改了服务上限和复查周期，旧计划仍按建档时规则核对
        self.assertEqual(snapshot["version_no"], 1)
        self.assertEqual(fetched["evaluation"]["service_cap"], 600)
        self.assertEqual(fetched["evaluation"]["review_cycle_days"], 30)
        self.assertTrue(fetched["evaluation"]["within_cap"])
        # 新计划按新规则建档，且超上限被拒
        too_big = dict(CREATE_DATA, service_minutes=500)
        with self.assertRaises(ValidationError) as ctx:
            self.service.create(Actor("cm", "case_manager"), "IEP-30002", too_big)
        self.assertIn("上限", str(ctx.exception))
        new_plan = self.service.create(Actor("cm", "case_manager"), "IEP-30003", dict(CREATE_DATA, service_minutes=400))
        self.assertEqual(new_plan["payload"]["rule_snapshot"]["version_no"], 2)
        self.assertEqual(new_plan["payload"]["rule_snapshot"]["service_cap"], 400)

    def test_review_can_adopt_new_version_with_reason(self):
        plan = self._plan_under_rule_v1()
        self._publish_v2(cap=400, cycle=20)
        record = self.service.act(Actor("p", "parent_rep"), plan["id"], plan["version"], "consent",
                                  {"guardian_confirmed": True, "consent_scope": "个别化服务"})
        record = self.service.act(Actor("cm", "case_manager"), record["id"], record["version"], "activate", {})
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service",
                                  {"session_minutes": 60, "provider": "SP-3"})
        # 没带原因不能改用新版本
        with self.assertRaises(ValidationError):
            self.service.act(self.admin, record["id"], record["version"], "review",
                             {"progress_note": "阶段复盘", "use_latest_rule": True})
        record = self.service.act(self.admin, record["id"], record["version"], "review",
                                  {"progress_note": "阶段复盘", "use_latest_rule": True,
                                   "rule_change_reason": "家长同意按新周期复查"})
        self.assertEqual(record["payload"]["rule_snapshot"]["version_no"], 2)
        timeline = self.service.timeline(Actor("cm", "case_manager"), record["id"])
        review_event = [e for e in timeline if e["action"] == "review"][0]
        self.assertEqual(review_event["details"]["plan_version"], record["version"])
        change = review_event["details"]["rule_change"]
        self.assertEqual(change["from_version"], 1)
        self.assertEqual(change["to_version"], 2)
        self.assertEqual(change["diff"]["service_cap"], {"from": 600, "to": 400})
        self.assertEqual(change["diff"]["review_cycle_days"], {"from": 30, "to": 20})

    def test_adopt_older_version_rejected(self):
        plan = self._plan_under_rule_v1()
        self._publish_v2()
        record = plan
        record = self.service.act(Actor("p", "parent_rep"), record["id"], record["version"], "consent",
                                  {"guardian_confirmed": True, "consent_scope": "个别化服务"})
        record = self.service.act(Actor("cm", "case_manager"), record["id"], record["version"], "activate", {})
        with self.assertRaises(ValidationError):
            self.service.act(self.admin, record["id"], record["version"], "review",
                             {"progress_note": "复盘", "use_rule_version": 1, "rule_change_reason": "想降级"})

    def test_concurrent_draft_revision_late_submit_fails(self):
        draft = self.service.create_rule_draft(self.admin, {"service_cap": 450, "review_cycle_days": 25, "reason": "初版"})
        self.assertEqual(draft["revision"], 1)
        # 管理员A先提交修订成功
        saved = self.service.revise_rule_draft(self.admin, draft["id"], 1,
                                               {"service_cap": 420, "review_cycle_days": 25, "reason": "A修订"})
        self.assertEqual(saved["revision"], 2)
        # 管理员B基于旧修订号晚到提交，失败并提示先刷新
        with self.assertRaises(Conflict) as ctx:
            self.service.revise_rule_draft(Actor("admin-2", "administrator"), draft["id"], 1,
                                           {"service_cap": 380, "review_cycle_days": 25, "reason": "B修订"})
        self.assertIn("刷新", str(ctx.exception))
        # 发布时若草稿刚被修订，晚到的发布同样失败
        with self.assertRaises(Conflict) as ctx:
            self.service.publish_rule_draft(self.admin, draft["id"], 1, {"reason": "拿着旧号发布"})
        self.assertIn("刷新", str(ctx.exception))

    def test_only_one_open_draft(self):
        self.service.create_rule_draft(self.admin, {"service_cap": 450, "review_cycle_days": 25})
        with self.assertRaises(Conflict):
            self.service.create_rule_draft(self.admin, {"service_cap": 350, "review_cycle_days": 15})

    def test_rollback_creates_new_effective_version_and_timeline(self):
        self._plan_under_rule_v1()
        self._publish_v2(cap=400, cycle=20)
        rolled = self.service.rollback_rule(self.admin, {"version_no": 1, "reason": "新上限执行困难，恢复600"})
        self.assertEqual(rolled["status"], "effective")
        self.assertEqual(rolled["version_no"], 3)
        self.assertEqual(rolled["service_cap"], 600)
        self.assertEqual(rolled["review_cycle_days"], 30)
        self.assertEqual(self.service.current_rule()["version_no"], 3)
        # 旧版本原样保留
        v1 = self.service.get_rule(self.admin, 1)
        self.assertEqual(v1["status"], "superseded")
        actions = [e["action"] for e in self.service.rule_timeline(self.admin)]
        self.assertIn("rollback", actions)
        combined = self.service.combined_timeline(self.admin)
        sources = {event["source"] for event in combined}
        self.assertEqual(sources, {"rule", "plan"})
        rollback_event = [e for e in combined if e["action"] == "rollback"][0]
        self.assertEqual(rollback_event["details"]["rollback_from"], 1)
        self.assertEqual(rollback_event["details"]["diff"]["service_cap"], {"from": 400, "to": 600})

    def test_rule_management_requires_administrator(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.create_rule_draft(Actor("cm", "case_manager"), {"service_cap": 400, "review_cycle_days": 20})

    def test_late_scheduled_version_does_not_overtake_newer(self):
        # v2 定时在未来生效
        self._publish_v2(cap=400, cycle=20, immediate=False)
        # 之后直接回滚基线，生成更新的生效 v3（此时无草稿，scheduled 存在 -> 应阻止回滚）
        with self.assertRaises(Conflict):
            self.service.rollback_rule(self.admin, {"version_no": 1, "reason": "尝试"})
        # 立刻发布一版立即生效的 v3：先把定时草稿处理掉（模拟其被新版本追上）：
        # 直接发布一个新草稿 v3 立即生效
        draft3 = self.service.create_rule_draft(self.admin, {"service_cap": 500, "review_cycle_days": 22, "reason": "加急"})
        # 因 v2 处于 scheduled，当前没有 open draft，可以创建；立即发布
        v3 = self.service.publish_rule_draft(self.admin, draft3["id"], draft3["revision"], {"reason": "加急立即生效"})
        self.assertEqual(v3["version_no"], 3)
        # v2 到点：不应覆盖更高的 v3，应被归档
        self.clock.now = datetime(2026, 10, 15, 0, 1, tzinfo=timezone.utc)
        self.service.repository.activate_due_rules()
        current = self.service.current_rule()
        self.assertEqual(current["version_no"], 3)
        v2 = self.service.get_rule(self.admin, 2)
        self.assertEqual(v2["status"], "superseded")
        events = [e for e in self.service.rule_timeline(self.admin) if e["version_no"] == 2]
        self.assertEqual(events[-1]["action"], "overtaken")
