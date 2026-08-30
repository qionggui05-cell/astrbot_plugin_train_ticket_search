"""锁定数据的 JSON 持久化。"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import List, Optional, Set

from .models import Lock


class LockStorage:
    """将锁定车次数据持久化到 JSON 文件，原子写入，损坏时备份重建。"""

    def __init__(self, path: str):
        self.path = path
        self._rlock = threading.RLock()
        self._locks: List[Lock] = []
        self.load()

    def _ensure_dir(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)

    def load(self) -> None:
        with self._rlock:
            self._locks = self._read_locks()

    def _read_locks(self) -> List[Lock]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("locks", []) if isinstance(data, dict) else data
            return [Lock.from_dict(x) for x in raw if isinstance(x, dict)]
        except Exception:
            try:
                backup = f"{self.path}.corrupt-{int(time.time())}"
                os.replace(self.path, backup)
            except Exception:
                pass
            return []

    def save(self) -> None:
        with self._rlock:
            self._ensure_dir()
            tmp = self.path + ".tmp"
            payload = {"version": 1, "locks": [l.to_dict() for l in self._locks]}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def all_locks(self) -> List[Lock]:
        with self._rlock:
            return list(self._locks)

    def locks_by_user(self, user_id: str) -> List[Lock]:
        with self._rlock:
            return [l for l in self._locks if l.user_id == user_id]

    def lock_exists(self, lock: Lock) -> bool:
        with self._rlock:
            return any(l.key() == lock.key() for l in self._locks)

    def add_lock(self, lock: Lock) -> bool:
        with self._rlock:
            if self.lock_exists(lock):
                return False
            self._locks.append(lock)
            self.save()
            return True

    def remove_locks(
        self,
        user_id: str,
        train_nos: Set[str],
        date_filter: Optional[str] = None,
    ) -> int:
        with self._rlock:
            before = len(self._locks)
            self._locks = [
                l
                for l in self._locks
                if not (
                    l.user_id == user_id
                    and l.train_no in train_nos
                    and (date_filter is None or l.depart_date == date_filter)
                )
            ]
            removed = before - len(self._locks)
            if removed:
                self.save()
            return removed

    def remove_departed(self, user_id: str, now=None) -> List[Lock]:
        """解除该用户所有已发车（出发时刻已过）的锁定车次，返回被移除的列表。"""
        with self._rlock:
            removed = [
                l for l in self._locks if l.user_id == user_id and l.is_departed(now)
            ]
            if removed:
                keys = {l.key() for l in removed}
                self._locks = [l for l in self._locks if l.key() not in keys]
                self.save()
            return removed

    def set_ticket_alert(
        self,
        user_id: str,
        enabled: bool,
        train_nos: Optional[Set[str]],
        date_filter: Optional[str] = None,
    ) -> int:
        with self._rlock:
            n = 0
            for l in self._locks:
                if (
                    l.user_id == user_id
                    and (train_nos is None or l.train_no in train_nos)
                    and (date_filter is None or l.depart_date == date_filter)
                ):
                    l.ticket_alert_enabled = bool(enabled)
                    l.alert_armed = True
                    n += 1
            if n:
                self.save()
            return n

    def replace_all(self, locks: List[Lock]) -> None:
        with self._rlock:
            self._locks = list(locks)
            self.save()
