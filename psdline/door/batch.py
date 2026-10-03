"""批量开门。

批量开门按门区里程由近及远调度，并受批量上限约束：超限时只调度最近的那一批，
剩下的留给下一个周期。调度结果不一次下发，而是交给分区放行（``release``）按波
执行：前一波确认到位才放下一波，确认超时就把已经开的收回来。被联锁锁定的门区
直接跳过并记进 ``skipped``，不会静默漏开。
"""

from __future__ import annotations

from ..audit import AuditLog
from ..clock import Clock, SystemClock
from ..station import DoorZone
from .door import DoorController
from .release import run_release


def batch_open(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    clock: Clock | None = None,
    audit: AuditLog | None = None,
) -> dict:
    """按里程顺序分区放行；``clock`` 缺省时使用系统时钟。"""

    return run_release(controller, station_id, zones, clock or SystemClock(), audit=audit)
