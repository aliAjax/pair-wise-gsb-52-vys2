"""特殊教育支持计划的领域规则与状态转换。

计划在“建档”那一刻绑定当时生效的区定规则快照（服务上限、复查周期）。
之后区里调整规则不会改动在用计划：服务履约、复查核对始终使用计划
自身的快照。复查修订（amend）时可携带原因改用当前生效的新版本。
"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Conflict, ValidationError, boolean, integer, text, text_list
from .rule_policy import RulePolicy


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def __init__(self, rule_policy: Optional[RulePolicy] = None) -> None:
        self.rule_policy = rule_policy or RulePolicy()

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any], rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        rule = rule or self.rule_policy.baseline()
        # 复查期限未显式给出时，按建档时规则的复查周期起算。
        if p.get("review_due_days") is None:
            p["review_due_days"] = int(rule["review_cycle_days"])
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any], rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rule = rule or self.rule_policy.baseline()
        p = self.validate_create(payload, rule)
        # 建档核对：服务上限、复查周期按建档时的规则版本。
        self.rule_policy.check_plan_against_rule(p, rule)
        snapshot = self.rule_policy.snapshot_of(rule)
        p["rule_version"] = snapshot["rule_version"]
        p["rule_snapshot"] = snapshot
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(
        self,
        record: Dict[str, Any],
        action: str,
        data: Dict[str, Any],
        current_rule: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            session = integer(data, "session_minutes", 1)
            if session + int(p["delivered_minutes"]) > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            # 核对依据始终是计划自身的建档规则快照，不受区里后续调规影响。
            cap = self._bound_cap(p)
            if session + int(p["delivered_minutes"]) > cap:
                raise ValidationError("记录服务超过建档规则服务上限（%s分钟）" % cap)
            changes["delivered_minutes"] = int(p["delivered_minutes"]) + session
            changes["last_provider"] = text(data, "provider")
            changes["missing_minutes"] = int(p["service_minutes"]) - changes["delivered_minutes"]
            changes["compliance_rate"] = round(changes["delivered_minutes"] / int(p["service_minutes"]) * 100, 2)
            summary = "服务记录已登记"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
            # 复查时可带原因改用当前生效的规则新版本。
            reason = data.get("rule_change_reason")
            if reason is not None and str(reason).strip():
                p, rule_change = self.rule_policy.adopt(p, current_rule, reason)
                changes["_rule_change"] = rule_change
                summary = "计划已修订并改用规则v%s" % p["rule_version"]
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def _bound_cap(self, payload: Dict[str, Any]) -> int:
        snapshot = payload.get("rule_snapshot")
        if snapshot and isinstance(snapshot, dict) and snapshot.get("service_cap") is not None:
            return int(snapshot["service_cap"])
        # 旧库回填前的兜底：按基线规则核对。
        return int(self.rule_policy.baseline()["service_cap"])
