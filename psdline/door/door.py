"""门控控制器装配。"""

from __future__ import annotations

import threading

from ..psd import LockoutRegistry, PSDRegistry
from ..store import CommandLog, JsonStore
from ..train import AlignRegistry


class DoorController:
    """门控动作的装配入口。"""

    def __init__(
        self,
        store: JsonStore,
        align: AlignRegistry,
        commands: CommandLog,
        gates: PSDRegistry,
        lockouts: LockoutRegistry,
    ) -> None:
        self._store = store
        self._align = align
        self._commands = commands
        self._gates = gates
        self._lockouts = lockouts
        self._batch_limit = 64
        self._opens = 0
        self._closes = 0
        self._release_locks: dict[str, threading.Lock] = {}
        self._release_guard = threading.Lock()

    @property
    def store(self) -> JsonStore:
        return self._store

    @property
    def align(self) -> AlignRegistry:
        return self._align

    @property
    def commands(self) -> CommandLog:
        return self._commands

    @property
    def gates(self) -> PSDRegistry:
        return self._gates

    @property
    def lockouts(self) -> LockoutRegistry:
        return self._lockouts

    @property
    def opens(self) -> int:
        """返回累计开门次数。"""

        return self._opens

    @property
    def closes(self) -> int:
        """返回累计关门次数。"""

        return self._closes

    def set_batch_limit(self, limit: int) -> None:
        """调整一次批量开门的门区上限。"""

        if limit <= 0:
            raise ValueError("批量上限必须为正")
        self._batch_limit = limit

    def note_open(self) -> int:
        """在一次成功开门后累加计数。"""

        self._opens += 1
        return self._opens

    def note_close(self) -> int:
        """在一次成功关门后累加计数。"""

        self._closes += 1
        return self._closes

    def batch_limit(self) -> int:
        """返回当前批量上限。"""

        return self._batch_limit

    def release_lock(self, station_id: str) -> threading.Lock:
        """返回某站台批量放行的互斥锁：同一站台串行，不同站台并行。"""

        with self._release_guard:
            lock = self._release_locks.get(station_id)
            if lock is None:
                lock = threading.Lock()
                self._release_locks[station_id] = lock
            return lock
