"""多轮交互会话状态机（内存）。"""

from __future__ import annotations

import datetime as dt
import time
from typing import Dict, List, Optional, Tuple

from .models import Train

STEP_DATE = "date"
STEP_DEPART = "depart"
STEP_ARRIVE = "arrive"
STEP_RETRY = "retry"
STEP_DONE = "done"


class QuerySession:
    """一次交互式查询的进行中状态。"""

    def __init__(
        self,
        user_id: str,
        unified_msg_origin: str,
        train_types: Optional[List[str]],
    ):
        self.user_id = user_id
        self.unified_msg_origin = unified_msg_origin
        self.train_types = train_types  # None 表示查询全部车次类型
        self.step = STEP_DATE
        self.date: Optional[dt.date] = None
        self.depart_station: Optional[str] = None
        self.arrive_station: Optional[str] = None
        self.last_query: List[Train] = []
        self.updated_at = time.time()

    def touch(self) -> None:
        self.updated_at = time.time()


class SessionManager:
    """内存会话表：按（发送者, 会话）维护进行中的查询，超时自动过期。"""

    def __init__(self, timeout_seconds: int = 600):
        self.timeout_seconds = timeout_seconds
        self._sessions: Dict[Tuple[str, str], QuerySession] = {}

    @staticmethod
    def _key(user_id: str, origin: str) -> Tuple[str, str]:
        return (user_id, origin)

    def start(
        self,
        user_id: str,
        origin: str,
        train_types: Optional[List[str]],
    ) -> QuerySession:
        session = QuerySession(user_id, origin, train_types)
        self._sessions[self._key(user_id, origin)] = session
        return session

    def get(self, user_id: str, origin: str) -> Optional[QuerySession]:
        key = self._key(user_id, origin)
        session = self._sessions.get(key)
        if session is None:
            return None
        if time.time() - session.updated_at > self.timeout_seconds:
            del self._sessions[key]
            return None
        session.touch()
        return session

    def clear(self, user_id: str, origin: str) -> None:
        self._sessions.pop(self._key(user_id, origin), None)
