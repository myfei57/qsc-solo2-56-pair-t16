"""分区放行与背压。

批量开门按里程排好序后不再一次推完：门区逐个放行，前一批确认到位才放下一批；
确认超时立刻停下，并把已经开的门区收回来。背压可以按已开门数（``count``）或
确认耗时（``time``）触发：``count`` 用窗口限制同时等待确认的门区数，窗口越
大吞吐越高、站台挤的时候同时敞开的门也越多；``time`` 看上一批确认花了多久，
超过预算就不再放新批，更保守。回收默认只关本会话打开的门（``close``），也可
以整站清扫（``sweep``）回到已知干净现场。

进度只落一份文档 ``door/<station>/release``：门控写、站台视图与门控报告读、
审计按它落流水，崩溃后重启也按它把还开着的门收回关闭位。同一站台的读写用站
台锁串行，不同站台互不阻塞。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..audit import AuditLog
from ..clock import Clock
from ..errors import InterlockError, RailError
from ..psd import sweep_station
from ..station import DoorZone, open_order
from .door import DoorController

MODE_COUNT = "count"
MODE_TIME = "time"
RECALL_CLOSE = "close"
RECALL_SWEEP = "sweep"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_RECALLING = "recalling"
STATUS_DONE = "done"
STATUS_HELD = "held"
STATUS_ABORTED = "aborted"

ZONE_PENDING = "pending"
ZONE_RELEASED = "released"
ZONE_CONFIRMED = "confirmed"
ZONE_FAILED = "failed"
ZONE_RECALLED = "recalled"

LIVE_STATES = (STATUS_RUNNING, STATUS_RECALLING)


def release_doc_id(station_id: str) -> str:
    """返回某站台批量放行进度文档的标识。"""

    return f"door/{station_id}/release"


@dataclass(frozen=True)
class ReleasePolicy:
    """一次分区放行的背压与回收策略。"""

    mode: str = MODE_COUNT
    window: int = 1
    confirm_timeout_s: float = 6.0
    confirm_budget_s: float = 2.0
    recall_mode: str = RECALL_CLOSE

    def validate(self) -> "ReleasePolicy":
        """校验策略取值，返回自身以便链式调用。"""

        if self.mode not in (MODE_COUNT, MODE_TIME):
            raise ValueError(f"背压模式必须是 {MODE_COUNT} 或 {MODE_TIME}")
        if self.window <= 0:
            raise ValueError("放行窗口必须为正")
        if self.confirm_timeout_s <= 0:
            raise ValueError("确认超时必须为正")
        if self.confirm_budget_s <= 0:
            raise ValueError("确认耗时预算必须为正")
        if self.recall_mode not in (RECALL_CLOSE, RECALL_SWEEP):
            raise ValueError(f"回收模式必须是 {RECALL_CLOSE} 或 {RECALL_SWEEP}")
        return self


def begin_release(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    policy: ReleasePolicy,
    session_id: str,
    at: float,
    audit: AuditLog | None = None,
) -> dict:
    """开始一次分区放行：排序、截断、跳过锁定门区，并把计划落盘。"""

    policy.validate()
    with controller.release_lock(station_id):
        existing = _load(controller, station_id)
        if existing is not None and existing.get("status") in LIVE_STATES:
            raise InterlockError(
                f"站台 {station_id} 已有进行中的批量放行: {existing.get('session_id')}",
                component="door",
            )
        order = open_order(zones)
        limit = controller.batch_limit()
        planned = order[:limit]
        groups = _gate_groups(controller, station_id)
        entries: list[dict] = []
        skipped: dict[str, str] = {}
        for zone_id in planned:
            gate_ids = groups.get(zone_id, [])
            if not gate_ids:
                skipped[zone_id] = "gate_missing"
                continue
            if any(controller.lockouts.is_locked(gate_id) for gate_id in gate_ids):
                skipped[zone_id] = "locked"
                continue
            entries.append(
                {
                    "zone": zone_id,
                    "state": ZONE_PENDING,
                    "gates": gate_ids,
                    "released_at": None,
                    "confirmed_at": None,
                    "confirm_s": None,
                    "reason": "",
                }
            )
        session = {
            "station_id": station_id,
            "session_id": session_id,
            "status": STATUS_RUNNING,
            "mode": policy.mode,
            "window": policy.window,
            "confirm_timeout_s": policy.confirm_timeout_s,
            "confirm_budget_s": policy.confirm_budget_s,
            "recall_mode": policy.recall_mode,
            "order": order,
            "planned": planned,
            "deferred": order[limit:],
            "zones": entries,
            "skipped": skipped,
            "cursor": 0,
            "opened_gate_ids": [],
            "recall": None,
            "reason": "",
            "started_at": at,
            "updated_at": at,
            "finished_at": None,
        }
        _persist(controller, session)
        _audit(
            audit,
            "batch_release_begin",
            session,
            planned=len(planned),
            deferred=len(session["deferred"]),
            skipped=len(skipped),
        )
        return session


def release_next(
    controller: DoorController,
    station_id: str,
    at: float,
    audit: AuditLog | None = None,
) -> dict:
    """放行下一个门区；背压不允许或没有待放行门区时原样返回进度。"""

    with controller.release_lock(station_id):
        session = _require_live(controller, station_id)
        if session["status"] != STATUS_RUNNING:
            return session
        if session["cursor"] >= len(session["zones"]):
            return session
        if not _backpressure_allows(session):
            return session
        entry = session["zones"][session["cursor"]]
        opened_here: list[str] = []
        try:
            for gate_id in entry["gates"]:
                if controller.lockouts.is_locked(gate_id):
                    raise InterlockError(f"门 {gate_id} 在放行前被联锁锁定", component="door")
                controller.gates.set_open(gate_id, True)
                opened_here.append(gate_id)
        except RailError as error:
            # 本区开了一半：把已开的门都记进回收清单，连同前面已放行的区一起收回。
            session["opened_gate_ids"].extend(opened_here)
            session["cursor"] += 1
            entry["state"] = ZONE_FAILED
            entry["reason"] = error.message
            _recall_locked(controller, session, at, audit, reason=f"release_failed:{error.message}")
            raise
        entry["state"] = ZONE_RELEASED
        entry["released_at"] = at
        session["cursor"] += 1
        session["opened_gate_ids"].extend(opened_here)
        session["updated_at"] = at
        _persist(controller, session)
        _audit(
            audit,
            "batch_release_zone",
            session,
            zone=entry["zone"],
            state=ZONE_RELEASED,
            cursor=session["cursor"],
        )
        return session


def confirm_release(
    controller: DoorController,
    station_id: str,
    at: float,
    audit: AuditLog | None = None,
) -> dict:
    """确认已放行的门区：读回开闭状态，到位记确认，超时停下并回收。"""

    with controller.release_lock(station_id):
        session = _require_live(controller, station_id)
        if session["status"] != STATUS_RUNNING:
            return session
        for entry in session["zones"]:
            if entry["state"] != ZONE_RELEASED:
                continue
            if _zone_confirmed(controller, entry):
                entry["state"] = ZONE_CONFIRMED
                entry["confirmed_at"] = at
                entry["confirm_s"] = round(at - entry["released_at"], 6)
                _audit(
                    audit,
                    "batch_release_zone",
                    session,
                    zone=entry["zone"],
                    state=ZONE_CONFIRMED,
                    confirm_s=entry["confirm_s"],
                )
                continue
            if at - entry["released_at"] > session["confirm_timeout_s"]:
                entry["state"] = ZONE_FAILED
                entry["reason"] = "confirm_timeout"
                return _recall_locked(controller, session, at, audit, reason="confirm_timeout")
            # 还没到位也没超时：保持等待，下一拍再来确认。
        session["updated_at"] = at
        if _awaiting(session):
            _persist(controller, session)
            return session
        if session["cursor"] < len(session["zones"]):
            if session["mode"] == MODE_TIME:
                confirmed = [item for item in session["zones"] if item["state"] == ZONE_CONFIRMED]
                if confirmed and confirmed[-1]["confirm_s"] > session["confirm_budget_s"]:
                    # 确认耗时超预算：站台确认太慢，剩下的门区不再放行。
                    remaining = [item["zone"] for item in session["zones"][session["cursor"] :]]
                    session["deferred"] = session["deferred"] + remaining
                    session["cursor"] = len(session["zones"])
                    session["status"] = STATUS_HELD
                    session["reason"] = "confirm_budget_exceeded"
                    session["finished_at"] = at
                    _persist(controller, session)
                    _audit(
                        audit,
                        "batch_release_held",
                        session,
                        confirm_s=confirmed[-1]["confirm_s"],
                        deferred=len(remaining),
                    )
                    return session
            _persist(controller, session)
            return session
        session["status"] = STATUS_DONE
        session["finished_at"] = at
        _persist(controller, session)
        _audit(audit, "batch_release_done", session, opened=len(session["opened_gate_ids"]))
        return session


def run_release(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    policy: ReleasePolicy,
    session_id: str,
    clock: Clock,
    audit: AuditLog | None = None,
) -> dict:
    """把一次分区放行驱动到底：能放就放、该等就等、确认到位再放下一批。"""

    session = begin_release(controller, station_id, zones, policy, session_id, clock.now(), audit=audit)
    while True:
        if session["status"] != STATUS_RUNNING:
            return session
        if _awaiting(session) and not _backpressure_allows(session):
            session = confirm_release(controller, station_id, clock.now(), audit=audit)
            if session["status"] != STATUS_RUNNING or _awaiting(session):
                return session
            continue
        if session["cursor"] >= len(session["zones"]):
            return confirm_release(controller, station_id, clock.now(), audit=audit)
        if not _backpressure_allows(session):
            # time 模式预算耗尽：确认已到位只是太慢，交给 confirm 落定 held。
            return confirm_release(controller, station_id, clock.now(), audit=audit)
        session = release_next(controller, station_id, clock.now(), audit=audit)


def release_progress(controller: DoorController, station_id: str) -> dict:
    """读取站台批量放行进度；没有放行过时返回 ``idle``。"""

    session = _load(controller, station_id)
    if session is None:
        return {
            "station_id": station_id,
            "status": STATUS_IDLE,
            "zones": [],
            "skipped": {},
            "deferred": [],
            "opened_gate_ids": [],
        }
    return session


def release_summary(progress: dict) -> dict:
    """从进度文档里抽出一行摘要，供状态快照与控制台内嵌。"""

    zones = progress.get("zones", [])
    return {
        "station_id": progress.get("station_id", ""),
        "status": progress.get("status", STATUS_IDLE),
        "session_id": progress.get("session_id", ""),
        "cursor": progress.get("cursor", 0),
        "total": len(zones),
        "confirmed": len([item for item in zones if item.get("state") == ZONE_CONFIRMED]),
        "recalled": len([item for item in zones if item.get("state") == ZONE_RECALLED]),
        "open_gates": len(progress.get("opened_gate_ids", [])),
        "reason": progress.get("reason", ""),
    }


def recover_releases(
    controller: DoorController,
    at: float,
    audit: AuditLog | None = None,
) -> list[dict]:
    """崩溃恢复：把还处在进行中状态的放行会话收回关闭位。"""

    recovered: list[dict] = []
    for doc_id in controller.store.list_ids():
        if not doc_id.startswith("door/") or not doc_id.endswith("/release"):
            continue
        station_id = doc_id[len("door/") : -len("/release")]
        with controller.release_lock(station_id):
            session = _load(controller, station_id)
            if session is None or session.get("status") not in LIVE_STATES:
                continue
            # 崩溃恢复一律定点关本会话的门：不碰站台上其他来源打开的门。
            closed = _close_tracked(controller, session, list(session.get("opened_gate_ids", [])))
            for entry in session.get("zones", []):
                if entry.get("state") in (ZONE_RELEASED, ZONE_CONFIRMED):
                    entry["state"] = ZONE_RECALLED
            session["recall"] = {
                "mode": RECALL_CLOSE,
                "closed": closed,
                "at": at,
                "reason": "crash_recovery",
            }
            session["status"] = STATUS_ABORTED
            session["reason"] = "crash_recovery"
            session["finished_at"] = at
            session["updated_at"] = at
            _persist(controller, session)
            _audit(audit, "batch_release_recovered", session, closed=len(closed))
            recovered.append(session)
    return recovered


# ---- 内部 -------------------------------------------------------------


def _load(controller: DoorController, station_id: str) -> dict | None:
    raw = controller.store.read(release_doc_id(station_id))
    return raw if isinstance(raw, dict) else None


def _require_live(controller: DoorController, station_id: str) -> dict:
    session = _load(controller, station_id)
    if session is None or session.get("status") not in LIVE_STATES:
        raise InterlockError(f"站台 {station_id} 没有进行中的批量放行", component="door")
    return session


def _persist(controller: DoorController, session: dict) -> None:
    controller.store.write(release_doc_id(session["station_id"]), session)


def _audit(audit: AuditLog | None, kind: str, session: dict, **detail: Any) -> None:
    if audit is None:
        return
    audit.record(kind, "", session["station_id"], session_id=session["session_id"], **detail)


def _gate_groups(controller: DoorController, station_id: str) -> dict[str, list[str]]:
    """一次取回站台全部门并按门区分组，避免逐区重扫存储。"""

    groups: dict[str, list[str]] = {}
    for gate in controller.gates.by_station(station_id):
        groups.setdefault(gate.group, []).append(gate.id)
    return {group: sorted(ids) for group, ids in groups.items()}


def _awaiting(session: dict) -> bool:
    """是否还有已放行但未确认的门区。"""

    return any(entry["state"] == ZONE_RELEASED for entry in session["zones"])


def _backpressure_allows(session: dict) -> bool:
    """判断背压是否允许放行下一批。"""

    released = len([entry for entry in session["zones"] if entry["state"] == ZONE_RELEASED])
    if released == 0:
        if session["mode"] == MODE_TIME:
            confirmed = [entry for entry in session["zones"] if entry["state"] == ZONE_CONFIRMED]
            last = confirmed[-1]["confirm_s"] if confirmed else None
            if last is not None and last > session["confirm_budget_s"]:
                return False
        return True
    if session["mode"] == MODE_TIME:
        return False
    return released < session["window"]


def _zone_confirmed(controller: DoorController, entry: dict) -> bool:
    """读回一个门区的全部站台门，全部开到位且未被锁定才算确认。"""

    for gate_id in entry["gates"]:
        gate = controller.gates.get(gate_id)
        if gate is None or not gate.open or gate.locked:
            return False
    return True


def _close_tracked(controller: DoorController, session: dict, gate_ids: list[str]) -> list[str]:
    """逐个关门并同步推进度文档；中途掉电时剩余门仍在 ``opened_gate_ids`` 里。"""

    closed: list[str] = []
    for gate_id in gate_ids:
        controller.gates.set_open(gate_id, False)
        closed.append(gate_id)
        if gate_id in session["opened_gate_ids"]:
            session["opened_gate_ids"].remove(gate_id)
        _persist(controller, session)
    return closed


def _recall_locked(
    controller: DoorController,
    session: dict,
    at: float,
    audit: AuditLog | None,
    reason: str,
) -> dict:
    """回收本会话已经打开的门（调用方必须已持有站台锁）。"""

    session["status"] = STATUS_RECALLING
    session["reason"] = reason
    session["updated_at"] = at
    # 先落盘回收意图：此刻崩溃，重启按 opened_gate_ids 继续收。
    _persist(controller, session)
    if session["recall_mode"] == RECALL_SWEEP:
        report = sweep_station(controller.gates, session["station_id"])
        closed = list(report["gates"])
        # 清扫覆盖全站开着的门，本会话的门要么被收掉、要么此前已关，清单直接清空。
        session["opened_gate_ids"] = []
        _persist(controller, session)
    else:
        closed = _close_tracked(controller, session, list(session["opened_gate_ids"]))
    closed_set = set(closed)
    for entry in session["zones"]:
        if entry["state"] in (ZONE_RELEASED, ZONE_CONFIRMED) and any(
            gate_id in closed_set for gate_id in entry["gates"]
        ):
            entry["state"] = ZONE_RECALLED
    session["recall"] = {"mode": session["recall_mode"], "closed": closed, "at": at, "reason": reason}
    session["status"] = STATUS_ABORTED
    session["finished_at"] = at
    session["updated_at"] = at
    _persist(controller, session)
    _audit(audit, "batch_release_aborted", session, closed=len(closed), reason=reason)
    return session
