"""用例门面：计划、规则、统一时间线。

规则、计划、审计在各自模块中独立实现；本门面只做转发，
保持对既有调用方（HTTP层、测试）的稳定接口。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .clock import Clock
from .domain import Actor
from .plan_service import PlanService
from .repository import Repository
from .rule_policy import RulePolicy
from .rule_service import RuleService
from .rule_store import RuleStore
from .rules import DomainRules
from .timeline_service import TimelineService


class Service:
    def __init__(
        self,
        repository: Repository,
        rules: DomainRules,
        audit: Optional[AuditRecorder] = None,
        policy: Optional[RulePolicy] = None,
        rule_store: Optional[RuleStore] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.clock = clock or Clock()
        self.policy = policy or RulePolicy()
        self.rule_store = rule_store or RuleStore(repository.db_path, self.policy)
        self.rule_service = RuleService(self.rule_store, self.policy, self.clock)
        self.plan_service = PlanService(repository, rules, self.rule_service, self.audit)
        self.timeline_service = TimelineService(repository, self.rule_service, self.plan_service)
        self._backfill_legacy_plans()

    def _backfill_legacy_plans(self) -> None:
        """旧库计划补建建档时规则快照（回填事件由仓储写入各计划时间线）。"""
        self.repository.backfill_rule_snapshots(self.policy.baseline())

    # ---- 计划用例 ----
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.plan_service.create(actor, reference, payload)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self.plan_service.list_records(actor, state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        return self.plan_service.get_record(actor, record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.plan_service.act(actor, record_id, expected_version, action, data)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        return self.plan_service.timeline(actor, record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        return self.repository.stats()

    # ---- 规则用例 ----
    def list_rules(self, actor: Actor) -> List[Dict[str, Any]]:
        return self.rule_service.list_rules(actor)

    def get_rule(self, actor: Actor, version: int) -> Dict[str, Any]:
        return self.rule_service.get_rule(actor, version)

    def current_rule(self, actor: Actor) -> Dict[str, Any]:
        return self.rule_service.current_rule(actor)

    def create_rule_draft(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.rule_service.create_draft(actor, data)

    def revise_rule_draft(self, actor: Actor, expected_version: int, expected_revision: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.rule_service.revise_draft(actor, expected_version, expected_revision, data)

    def publish_rule_draft(self, actor: Actor, expected_version: int, expected_revision: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.rule_service.publish_draft(actor, expected_version, expected_revision, data)

    def discard_rule_draft(self, actor: Actor, expected_version: int, expected_revision: int) -> Dict[str, Any]:
        return self.rule_service.discard_draft(actor, expected_version, expected_revision)

    def rollback_rule(self, actor: Actor, target_version: int) -> Dict[str, Any]:
        return self.rule_service.rollback(actor, target_version)

    def rule_timeline(self, actor: Actor) -> List[Dict[str, Any]]:
        return self.rule_service.timeline(actor)

    def combined_timeline(self, actor: Actor, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.timeline_service.combined(actor, record_id=record_id)
