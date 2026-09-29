"""时间来源：业务规则只认UTC ISO文本，测试可替换时钟模拟到点生效。"""
from datetime import datetime, timezone
from typing import Callable, Optional


class Clock:
    def __init__(self, fixed: Optional[str] = None) -> None:
        self._fixed = fixed
        self._override: Optional[Callable[[], datetime]] = None

    def now(self) -> datetime:
        if self._override is not None:
            return self._override()
        if self._fixed is not None:
            return datetime.fromisoformat(self._fixed)
        return datetime.now(timezone.utc)

    def iso(self) -> str:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def set_fixed(self, value: Optional[str]) -> None:
        self._fixed = value
