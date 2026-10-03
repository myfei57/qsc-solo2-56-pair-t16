"""门控控制器装配。"""

from __future__ import annotations

import threading
from typing import Callable

from ..ids import IdFactory
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
        # 分区放行策略：波粒度、确认超时、背压与回收方式，由平台按配置装配。
        self._wave_size = 1
        self._confirm_timeout_s = 5.0
        self._confirm_poll_s = 0.2
        self._max_open_gates = 24
        self._confirm_budget_s = 2.0
        self._backpressure = "count"
        self._recall_mode = "targeted"
        self._confirmer: Callable[[DoorController, list[str]], bool] | None = None
        # 同一站台的放行互斥：并发下发时后到者直接拒绝，不交叉开门。
        self._release_guard = threading.Lock()
        self._release_locks: dict[str, threading.Lock] = {}
        self._release_ids = IdFactory("rel")

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

    # ---- 分区放行 -----------------------------------------------------

    def set_release_policy(
        self,
        *,
        wave_size: int,
        confirm_timeout_s: float,
        confirm_poll_s: float,
        max_open_gates: int,
        confirm_budget_s: float,
        backpressure: str,
        recall_mode: str,
    ) -> None:
        """调整分区放行策略（波粒度、确认超时、背压与回收方式）。"""

        if wave_size <= 0:
            raise ValueError("放行波粒度必须为正")
        if confirm_timeout_s <= 0:
            raise ValueError("确认超时必须为正")
        if confirm_poll_s <= 0 or confirm_poll_s > confirm_timeout_s:
            raise ValueError("确认轮询间隔必须为正且不超过确认超时")
        if max_open_gates <= 0:
            raise ValueError("站台开门数上限必须为正")
        if confirm_budget_s <= 0:
            raise ValueError("确认耗时预算必须为正")
        if backpressure not in ("count", "latency"):
            raise ValueError("背压方式必须是 count 或 latency")
        if recall_mode not in ("targeted", "sweep"):
            raise ValueError("回收方式必须是 targeted 或 sweep")
        self._wave_size = wave_size
        self._confirm_timeout_s = confirm_timeout_s
        self._confirm_poll_s = confirm_poll_s
        self._max_open_gates = max_open_gates
        self._confirm_budget_s = confirm_budget_s
        self._backpressure = backpressure
        self._recall_mode = recall_mode

    def release_policy(self) -> dict:
        """返回当前分区放行策略的快照。"""

        return {
            "wave_size": self._wave_size,
            "confirm_timeout_s": self._confirm_timeout_s,
            "confirm_poll_s": self._confirm_poll_s,
            "max_open_gates": self._max_open_gates,
            "confirm_budget_s": self._confirm_budget_s,
            "backpressure": self._backpressure,
            "recall_mode": self._recall_mode,
        }

    def set_release_confirmer(
        self, confirmer: Callable[["DoorController", list[str]], bool] | None
    ) -> None:
        """接管波次确认；``None`` 恢复为回读落盘状态。"""

        self._confirmer = confirmer

    def confirm_gates(self, gate_ids: list[str]) -> bool:
        """确认一组门已开到位；默认回读落盘状态（开且未锁）。"""

        if self._confirmer is not None:
            return bool(self._confirmer(self, list(gate_ids)))
        for gate_id in gate_ids:
            gate = self._gates.get(gate_id)
            if gate is None or not gate.open or gate.locked:
                return False
        return True

    def acquire_release(self, station_id: str) -> bool:
        """尝试占有站台的放行权；已有放行进行中时返回 ``False``。"""

        with self._release_guard:
            lock = self._release_locks.setdefault(station_id, threading.Lock())
        return lock.acquire(blocking=False)

    def release_release(self, station_id: str) -> None:
        """释放站台的放行权。"""

        with self._release_guard:
            lock = self._release_locks.get(station_id)
        if lock is not None and lock.locked():
            lock.release()

    def next_release_id(self, station_id: str) -> str:
        """生成一次放行的标识。"""

        return self._release_ids.labeled(station_id)
