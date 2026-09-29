"""业务用例编排、权限检查、乐观并发与审计。

计划建档时固化当时的区级规则快照，之后一律按快照核对；区里调整服务上限、
复查周期只影响之后新建的计划。计划复查时可带原因改用更新的生效规则版本。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rule_versioning import RuleVersioning, to_iso, utc_now
from .rules import DomainRules


RULE_WRITE_ROLES = {"administrator"}


class Service:
    def __init__(
        self,
        repository: Repository,
        rules: DomainRules,
        audit: AuditRecorder = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.rule_logic = RuleVersioning()
        self.clock = clock or getattr(repository, "clock", None) or utc_now

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_rule_writer(self, actor: Actor) -> None:
        if actor.role != "admin" and actor.role not in RULE_WRITE_ROLES:
            raise PermissionDenied("角色无权管理区级规则")

    # ----------------------------- 支持计划 -----------------------------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        rule = self.repository.current_rule()
        self.rule_logic.check_plan_within_cap(prepared, rule)
        # 建档时固化规则：旧计划此后仍按这份快照核对，不受区里后续调整影响
        prepared["rule_snapshot"] = self.rule_logic.snapshot(rule, to_iso(self.clock()))
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["evaluation"] = self.rule_logic.evaluate_plan(record["payload"])
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details: Dict[str, Any] = {
            "summary": summary,
            "input": data or {},
            "from": record["state"],
            "to": new_state,
            "plan_version": int(expected_version) + 1,
        }
        if action == "review":
            new_payload, rule_details = self._maybe_adopt_rule(new_payload, data or {})
            if rule_details:
                details["rule_change"] = rule_details
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def _maybe_adopt_rule(self, payload: Dict[str, Any], data: Dict[str, Any]) -> tuple:
        """复查时可选改用新规则版本，必须带原因；不改则计划仍按原快照核对。"""
        want_latest = bool(data.get("use_latest_rule"))
        target_no = data.get("use_rule_version")
        if not want_latest and target_no is None:
            return payload, None
        if target_no is not None and not isinstance(target_no, int):
            raise ValidationError("use_rule_version必须是整数")
        current_snapshot = payload.get("rule_snapshot")
        target = self.repository.current_rule() if want_latest else self.repository.get_rule(int(target_no))
        reason = data.get("rule_change_reason", "")
        if not isinstance(reason, str):
            raise ValidationError("rule_change_reason必须是文本")
        new_snapshot, diff = self.rule_logic.adopt(current_snapshot, target, reason.strip(), to_iso(self.clock()))
        payload["rule_snapshot"] = new_snapshot
        return payload, {
            "from_version": int(current_snapshot["version_no"]),
            "to_version": new_snapshot["version_no"],
            "reason": reason.strip(),
            "diff": diff,
        }

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ----------------------------- 区级规则版本 -----------------------------

    def list_rules(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_rules()

    def current_rule(self, actor: Actor = None) -> Dict[str, Any]:
        if actor is not None:
            actor = self._actor(actor)
            self._ensure_known_role(actor)
        return self.repository.current_rule()

    def get_rule(self, actor: Actor, version_no: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_rule(version_no)

    def create_rule_draft(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_rule_writer(actor)
        values = self.rule_logic.validate_values(payload or {})
        return self.repository.create_rule_draft(values, actor.user_id)

    def revise_rule_draft(self, actor: Actor, rule_id: int, expected_revision: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_rule_writer(actor)
        if not isinstance(expected_revision, int):
            raise ValidationError("expected_revision必须是整数")
        values = self.rule_logic.validate_values(payload or {})
        return self.repository.revise_rule_draft(rule_id, expected_revision, values, actor.user_id)

    def publish_rule_draft(self, actor: Actor, rule_id: int, expected_revision: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_rule_writer(actor)
        if not isinstance(expected_revision, int):
            raise ValidationError("expected_revision必须是整数")
        data = payload or {}
        effective_at = self.rule_logic.parse_effective_at(data.get("effective_at"), self.clock())
        reason = data.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("发布原因不能为空")
        return self.repository.publish_rule_draft(rule_id, expected_revision, effective_at, reason.strip(), actor.user_id)

    def rollback_rule(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_rule_writer(actor)
        data = payload or {}
        version_no = data.get("version_no")
        if not isinstance(version_no, int):
            raise ValidationError("version_no必须是整数")
        reason = data.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("回滚原因不能为空")
        return self.repository.rollback_rule(int(version_no), reason.strip(), actor.user_id)

    def rule_timeline(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.rule_timeline()

    def combined_timeline(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.combined_timeline()
