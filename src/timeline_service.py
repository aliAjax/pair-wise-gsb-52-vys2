"""审计时间线：合并规则版本事件与计划版本事件。

规则审计和计划审计分别落在不同表里，本模块只做只读合并：
按时间排序输出，并标注每一条属于规则还是计划，使时间线能同时
看出规则版本、计划版本以及两者之间的差异。
"""
from typing import Any, Dict, List, Optional

from .domain import Actor, PermissionDenied


class TimelineService:
    def __init__(self, repository: Any, rule_service: Any, plan_service: Any = None) -> None:
        self.repository = repository
        self.rule_service = rule_service
        self.plan_service = plan_service

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def combined(self, actor: Actor, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        events: List[Dict[str, Any]] = []
        if record_id is None:
            for event in self.rule_service.timeline(actor):
                events.append(
                    {
                        "scope": "rule",
                        "created_at": event["created_at"],
                        "version": event["rule_version"],
                        "action": event["action"],
                        "actor_id": event["actor_id"],
                        "details": event["details"],
                    }
                )
        if record_id is not None:
            for event in self.repository.audit_timeline(int(record_id)):
                events.append(
                    {
                        "scope": "plan",
                        "record_id": int(record_id),
                        "created_at": event["created_at"],
                        "version": event["version"],
                        "action": event["action"],
                        "actor_id": event["actor_id"],
                        "details": event["details"],
                    }
                )
        else:
            for record in self.repository.list_records(limit=500):
                for event in self.repository.audit_timeline(int(record["id"])):
                    events.append(
                        {
                            "scope": "plan",
                            "record_id": int(record["id"]),
                            "reference": record["reference"],
                            "created_at": event["created_at"],
                            "version": event["version"],
                            "action": event["action"],
                            "actor_id": event["actor_id"],
                            "details": event["details"],
                        }
                    )
        events.sort(key=lambda item: (item["created_at"], item["scope"], item["action"]))
        return events
