"""门控运行报告。"""

from __future__ import annotations

from .door import DoorController
from .limit import open_pressure
from .queue import queued_total
from .release import release_progress, release_summary


def door_report(controller: DoorController, station_ids: list[str]) -> dict:
    """汇总门控吞吐、队列与各站台开门压力；放行进度与门控读同一份文档。"""

    return {
        "opens": controller.opens,
        "closes": controller.closes,
        "batch_limit": controller.batch_limit(),
        "queued": queued_total(controller),
        "locked": controller.lockouts.locked_count(),
        "stations": [
            {
                **open_pressure(controller, station_id),
                "release": release_summary(release_progress(controller, station_id)),
            }
            for station_id in station_ids
        ],
    }
