"""规则用例：草稿、到点生效、发布并发、回滚。

规则的管理动作只允许administrator/admin执行；读取对已知角色开放。
所有写操作基于乐观版本号：别人先提交了修订或发布时，
晚到的请求直接409并提示先刷新。
"""
from typing import Any, Dict, List, Optional

from .clock import Clock
from .domain import Actor, PermissionDenied, integer
from .rule_policy import RulePolicy
from .rule_store import RuleStore


MANAGE_ROLES = {"administrator", "admin"}


class RuleService:
    def __init__(self, store: RuleStore, policy: RulePolicy, clock: Optional[Clock] = None) -> None:
        self.store = store
        self.policy = policy
        self.clock = clock or Clock()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def known_role(self, role: str) -> bool:
        # 规则页面所有登录的已知业务角色可读，只有管理员可写。
        return role in {"admin", "administrator", "case_manager", "specialist", "parent_rep"}

    def _ensure_readable(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        if not self.known_role(actor.role):
            raise PermissionDenied("角色无权访问规则服务")
        return actor

    def _ensure_manager(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        if actor.role not in MANAGE_ROLES:
            raise PermissionDenied("只有管理员可以管理规则版本")
        return actor

    def list_rules(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._ensure_readable(actor)
        self.store.activate_due(self.clock.iso())
        return self.store.list_versions()

    def get_rule(self, actor: Actor, version: int) -> Dict[str, Any]:
        actor = self._ensure_readable(actor)
        return self.store.get_version(int(version))

    def current_rule(self, actor: Actor) -> Dict[str, Any]:
        # 计划建档/复查时的内部读取，调用方已完成身份校验。
        actor = self._ensure_readable(actor)
        return self.store.current(self.clock.iso())

    def create_draft(self, actor: Actor, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._ensure_manager(actor)
        config = self.policy.validate_payload(data)
        return self.store.create_draft(config, actor.user_id, self.clock.iso())

    def revise_draft(self, actor: Actor, expected_version: int, expected_revision: int, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._ensure_manager(actor)
        config = self.policy.validate_payload(data)
        return self.store.revise_draft(
            int(expected_version), int(expected_revision), config, actor.user_id, self.clock.iso()
        )

    def publish_draft(self, actor: Actor, expected_version: int, expected_revision: int, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._ensure_manager(actor)
        data = data or {}
        effective_dt = self.policy.parse_effective_at(data.get("effective_at"), self.clock.now())
        effective_at = effective_dt.isoformat() if effective_dt is not None else None
        return self.store.publish_draft(
            int(expected_version), int(expected_revision), effective_at, actor.user_id, self.clock.iso()
        )

    def discard_draft(self, actor: Actor, expected_version: int, expected_revision: int) -> Dict[str, Any]:
        actor = self._ensure_manager(actor)
        return self.store.discard_draft(int(expected_version), int(expected_revision), actor.user_id, self.clock.iso())

    def rollback(self, actor: Actor, target_version: int) -> Dict[str, Any]:
        actor = self._ensure_manager(actor)
        integer({"target_version": target_version}, "target_version", 1)
        return self.store.rollback(int(target_version), actor.user_id, self.clock.iso())

    def timeline(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._ensure_readable(actor)
        self.store.activate_due(self.clock.iso())
        return self.store.audit_timeline()
