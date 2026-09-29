"""区级支持规则的版本化领域逻辑（纯函数，不依赖持久化）。

规则内容目前包含：
- service_cap：单个支持计划的服务上限（分钟）；
- review_cycle_days：复查周期（天）。

生命周期：draft（草稿） -> scheduled（到点生效） -> effective（生效），
被更新版本取代后变为 superseded。回滚不删除旧版本，而是复制旧值生成一个
新的生效版本。计划建档时固化规则快照，此后按快照核对，与规则是否再调整无关。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .domain import ValidationError, integer, optional_text


DRAFT = "draft"
SCHEDULED = "scheduled"
EFFECTIVE = "effective"
SUPERSEDED = "superseded"

# 旧库迁移时锚定的初始基线版本
BASELINE_VERSION_NO = 1
DEFAULT_SERVICE_CAP = 600
DEFAULT_REVIEW_CYCLE_DAYS = 30

RULE_VALUE_KEYS = ("service_cap", "review_cycle_days")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class RuleSnapshot:
    """计划建档（或复查改用新版本）时固化的规则快照。"""

    version_no: int
    service_cap: int
    review_cycle_days: int
    captured_at: str
    backfilled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_no": self.version_no,
            "service_cap": self.service_cap,
            "review_cycle_days": self.review_cycle_days,
            "captured_at": self.captured_at,
            "backfilled": self.backfilled,
        }


class RuleVersioning:
    """规则版本的状态判定、快照、差异与核对规则，全部无状态。"""

    DRAFT = DRAFT
    SCHEDULED = SCHEDULED
    EFFECTIVE = EFFECTIVE
    SUPERSEDED = SUPERSEDED

    def validate_values(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        values = {
            "service_cap": integer(data, "service_cap", 1, 1000000),
            "review_cycle_days": integer(data, "review_cycle_days", 1, 3650),
            "reason": optional_text(data, "reason"),
        }
        return values

    def parse_effective_at(self, raw: Any, now: datetime) -> str:
        """发布时间：缺省表示立即生效；接受带或不带时区的ISO时间。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return to_iso(now)
        if not isinstance(raw, str):
            raise ValidationError("effective_at必须是ISO时间字符串")
        try:
            moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("effective_at必须是ISO时间字符串") from exc
        return to_iso(moment)

    def is_due(self, effective_at: str, now: datetime) -> bool:
        return datetime.fromisoformat(effective_at) <= now

    def snapshot(self, rule: Dict[str, Any], captured_at: str, backfilled: bool = False) -> Dict[str, Any]:
        return RuleSnapshot(
            version_no=int(rule["version_no"]),
            service_cap=int(rule["service_cap"]),
            review_cycle_days=int(rule["review_cycle_days"]),
            captured_at=captured_at,
            backfilled=backfilled,
        ).to_dict()

    def diff_values(self, old_values: Optional[Dict[str, Any]], new_values: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
        """逐字段给出规则差异，例如 {"service_cap": {"from": 600, "to": 400}}。"""
        diff: Dict[str, Dict[str, int]] = {}
        for key in RULE_VALUE_KEYS:
            if not old_values or key not in old_values or key not in new_values:
                continue
            before = int(old_values[key])
            after = int(new_values[key])
            if before != after:
                diff[key] = {"from": before, "to": after}
        return diff

    def check_plan_within_cap(self, prepared: Dict[str, Any], rule: Dict[str, Any]) -> None:
        cap = int(rule["service_cap"])
        if int(prepared["service_minutes"]) > cap:
            raise ValidationError("计划服务分钟超过现行服务上限%s分钟" % cap)

    def adopt(
        self,
        current_snapshot: Dict[str, Any],
        target: Dict[str, Any],
        reason: str,
        captured_at: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]]]:
        """计划复查时改用更新的生效规则版本，必须带原因，返回新快照与差异。"""
        if str(target.get("status")) != EFFECTIVE:
            raise ValidationError("只能改用已生效的规则版本")
        current_no = int(current_snapshot["version_no"])
        target_no = int(target["version_no"])
        if target_no <= current_no:
            raise ValidationError("改用的规则版本必须比当前版本新")
        if not reason or not reason.strip():
            raise ValidationError("改用新版本必须填写原因")
        diff = self.diff_values(current_snapshot, target)
        return self.snapshot(target, captured_at), diff

    def evaluate_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """始终按计划自己的规则快照核对，不看区里后来的调整。"""
        snap = payload.get("rule_snapshot")
        service_minutes = int(payload.get("service_minutes", 0))
        delivered = int(payload.get("delivered_minutes", 0))
        if snap:
            version_no = int(snap["version_no"])
            cap = int(snap["service_cap"])
            cycle = int(snap["review_cycle_days"])
        else:
            # 没有快照（理论上迁移已补齐）时，退化为按计划自身建档值核对
            version_no = None
            cap = service_minutes
            cycle = int(payload.get("review_cycle_days", DEFAULT_REVIEW_CYCLE_DAYS))
        return {
            "rule_version_no": version_no,
            "service_cap": cap,
            "review_cycle_days": cycle,
            "service_minutes": service_minutes,
            "delivered_minutes": delivered,
            "over_cap": service_minutes > cap,
            "within_cap": service_minutes <= cap and delivered <= cap,
            "review_overdue": bool(payload.get("review_overdue")),
        }
