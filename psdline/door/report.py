"""门控运行报告。"""

from __future__ import annotations

from .door import DoorController
from .limit import open_pressure
from .queue import queued_total
from .release import release_progress


def door_report(controller: DoorController, station_ids: list[str]) -> dict:
    """汇总门控吞吐、队列、各站台开门压力与分区放行进度。"""

    return {
        "opens": controller.opens,
        "closes": controller.closes,
        "batch_limit": controller.batch_limit(),
        "queued": queued_total(controller),
        "locked": controller.lockouts.locked_count(),
        "stations": [open_pressure(controller, station_id) for station_id in station_ids],
        "releases": {
            station_id: release_progress(controller, station_id) for station_id in station_ids
        },
    }
