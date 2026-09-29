"""支持计划用例：权限、建档规则绑定、乐观并发。

计划侧只通过规则服务读取当前生效版本；规则草稿的编辑、发布、
回滚属于规则模块，不在这里处理。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class PlanService:
    def __init__(self, repository: Repository, rules: DomainRules, rule_service: Any, audit: Optional[AuditRecorder] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.rule_service = rule_service
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        # 建档时锁定当时生效的规则版本，之后调规不影响在用计划。
        current_rule = self.rule_service.current_rule(actor)
        prepared = self.rules.prepare_create(payload or {}, current_rule)
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        # 仅在复查修订“改用新规则”时才读取当前生效版本。
        data = data or {}
        current_rule = None
        if action == "amend" and str(data.get("rule_change_reason", "")).strip():
            current_rule = self.rule_service.current_rule(actor)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data, current_rule)
        rule_change = new_payload.pop("_rule_change", None)
        details = {"summary": summary, "input": data, "from": record["state"], "to": new_state}
        if rule_change:
            details["rule_change"] = rule_change
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)
