"""区级支持规则的版本策略。

规则只包含两类区定参数：
- service_cap：单个计划周期内的服务分钟数上限；
- review_cycle_days：计划复查周期（天）。

本模块只负责纯领域判断：草稿校验、版本差异、计划建档快照、
复查时改用新版本，不接触数据库和HTTP，便于与计划逻辑分开演进。
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .domain import Conflict, ValidationError, integer, text


RULE_STATUS_DRAFT = "draft"
RULE_STATUS_SCHEDULED = "scheduled"
RULE_STATUS_EFFECTIVE = "effective"
RULE_STATUS_SUPERSEDED = "superseded"
RULE_STATUS_DISCARDED = "discarded"

DEFAULT_SERVICE_CAP = 1000
DEFAULT_REVIEW_CYCLE_DAYS = 30

CONFIG_FIELDS = ("service_cap", "review_cycle_days")


class RulePolicy:
    """规则版本的纯领域规则，无外部依赖。"""

    def baseline(self) -> Dict[str, Any]:
        """系统首版规则，同时用于全新库初始化和旧库回填。"""
        return {
            "version": 1,
            "service_cap": DEFAULT_SERVICE_CAP,
            "review_cycle_days": DEFAULT_REVIEW_CYCLE_DAYS,
            "note": "系统基线规则",
        }

    def validate_payload(self, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        data = data or {}
        config = {
            "service_cap": integer(data, "service_cap", 1),
            "review_cycle_days": integer(data, "review_cycle_days", 1),
        }
        note = data.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValidationError("note必须是文本")
        config["note"] = note.strip()
        return config

    def snapshot_of(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        """从规则版本抽取要固化进计划的建档快照。"""
        return {
            "rule_version": int(rule["version"]),
            "service_cap": int(rule["service_cap"]),
            "review_cycle_days": int(rule["review_cycle_days"]),
        }

    @staticmethod
    def diff(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """只比较规则参数本身，版本号差异由调用方单列。"""
        old = old or {}
        new = new or {}
        changes: Dict[str, Dict[str, Any]] = {}
        for key in CONFIG_FIELDS:
            old_value = old.get(key)
            new_value = new.get(key)
            if old_value != new_value:
                changes[key] = {"from": old_value, "to": new_value}
        if old.get("note") != new.get("note"):
            changes["note"] = {"from": old.get("note", ""), "to": new.get("note", "")}
        return changes

    def check_plan_against_rule(self, prepared: Dict[str, Any], rule: Dict[str, Any]) -> None:
        """建档核对：计划服务分钟数和复查期限必须符合所绑定的规则。"""
        cap = int(rule["service_cap"])
        cycle = int(rule["review_cycle_days"])
        if int(prepared["service_minutes"]) > cap:
            raise ValidationError("计划服务分钟数不能超过区定服务上限（%s分钟）" % cap)
        if int(prepared["review_due_days"]) > cycle:
            raise ValidationError("复查间隔不能超过区定复查周期（%s天）" % cycle)

    def parse_effective_at(self, value: Any, now_dt: datetime) -> Optional[datetime]:
        """草稿发布的到点时间：空值表示立即生效，否则解析为UTC时间。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValidationError("effective_at必须是ISO时间文本")
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError("effective_at时间格式无效") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def adopt(
        self, plan_payload: Dict[str, Any], current_rule: Optional[Dict[str, Any]], reason: str
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """复查修订时把计划改用新生效规则，返回新payload和变更说明。"""
        snapshot = plan_payload.get("rule_snapshot")
        if not snapshot:
            raise Conflict("计划缺少建档规则快照，请先补建后再复查")
        if current_rule is None:
            raise ValidationError("当前没有生效的规则版本")
        old_version = int(snapshot["rule_version"])
        new_version = int(current_rule["version"])
        if new_version == old_version:
            raise ValidationError("计划已采用当前生效规则，无需改用")
        reason = text({"rule_change_reason": reason}, "rule_change_reason")

        new_payload = dict(plan_payload)
        new_snapshot = self.snapshot_of(current_rule)
        change = {
            "from_version": old_version,
            "to_version": new_version,
            "reason": reason,
            "diff": self.diff(snapshot, new_snapshot),
        }
        # 改用新版本后，复查期限按新周期重新起算。
        new_payload["review_due_days"] = new_snapshot["review_cycle_days"]
        new_payload["review_overdue"] = False
        new_payload["rule_version"] = new_version
        new_payload["rule_snapshot"] = new_snapshot
        return new_payload, change
