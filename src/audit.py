"""审计事件封装：计划时间线、规则时间线与统一时间线，查询保持只读。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, record_id: int) -> List[Dict[str, Any]]:
        return self.repository.audit_timeline(record_id)

    def note(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        self.repository.add_audit(record_id, actor_id, action, details)

    def rule_timeline(self) -> List[Dict[str, Any]]:
        return self.repository.rule_timeline()

    def combined_timeline(self) -> List[Dict[str, Any]]:
        return self.repository.combined_timeline()
