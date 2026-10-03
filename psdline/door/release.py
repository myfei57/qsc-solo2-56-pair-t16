"""分区放行与背压。

批量开门不再一次按里程排完就下发：调度结果先按 ``wave_size`` 切成波（默认一个
门区一波），每一波开门后必须等站台侧确认到位，前一波确认不了就停下，并把这次
放行已经打开的门区收回来。进度只有一份——``release/<station_id>`` 文档，门控
报告、站台视图与审计都读它；进程崩溃重启后由恢复流程按这份文档把未完成的放行
收回关闭位。

背压两种触发方式都实现，配置 ``door.backpressure`` 选择：

- ``count``：按已开门数触发。下一波开门前先看站台当前开门数，加上这一波会
  超过上限就等它回落，等不到就停。安全优先：站台拥挤时直接限制暴露面，吞吐
  让位。
- ``latency``：按确认耗时触发。上一波确认耗时超过预算就把下一波切小（减半、
  最小一波一个门区），准时的波再逐步放回配置粒度。吞吐优先：站台空闲时几乎
  不降速，但反应总比现场慢一波。

回收方式配置 ``door.recall_mode`` 选择：``targeted`` 原地关门（只关本次放行
打开的门，不碰其他流程开着的门），``sweep`` 走一遍整站清扫（连不在清单里的
半开门一起收）。崩溃恢复一律按进度文档里记录的门清单原地收回，保证可重入。
"""

from __future__ import annotations

from ..audit import AuditLog
from ..clock import Clock
from ..errors import RailError
from ..psd import sweep_station
from ..station import DoorZone, open_order
from .door import DoorController

# 放行生命周期：releasing → done；异常时 releasing → recalling → recalled /
# recall_failed；崩溃后由 recover_release 把非终态的放行收成 recovered。
TERMINAL_STATES = frozenset({"done", "recalled", "recall_failed", "recovered"})
# 已经（可能部分）把门打开、回收时需要关门的波次状态。
WAVE_OPEN_STATES = frozenset({"opening", "open", "confirmed"})


def release_doc_id(station_id: str) -> str:
    """返回站台放行进度文档的标识。"""

    return f"release/{station_id}"


def load_release(controller: DoorController, station_id: str) -> dict | None:
    """读取站台放行进度文档，不存在时返回 ``None``。"""

    raw = controller.store.read(release_doc_id(station_id))
    return raw if isinstance(raw, dict) else None


def release_progress(controller: DoorController, station_id: str) -> dict:
    """读取站台当前的放行进度；没有放行记录时返回空闲骨架。"""

    doc = load_release(controller, station_id)
    if doc is None:
        return {
            "station_id": station_id,
            "state": "idle",
            "release_id": "",
            "opened": [],
            "recalled": [],
            "skipped": {},
            "deferred": [],
            "waves": [],
            "violations": [],
            "updated_at": 0.0,
        }
    return doc


def run_release(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    clock: Clock,
    audit: AuditLog | None = None,
) -> dict:
    """按里程顺序分区放行；前一波确认到位才放下一波，确认超时收回已开门区。"""

    if not controller.acquire_release(station_id):
        return {
            "ok": False,
            "state": "busy",
            "station_id": station_id,
            "release_id": "",
            "order": [],
            "opened": [],
            "recalled": [],
            "deferred": [],
            "skipped": {},
            "limit": controller.batch_limit(),
            "waves": [],
            "violations": ["站台已有放行进行中"],
        }
    try:
        return _run(controller, station_id, zones, clock, audit)
    finally:
        controller.release_release(station_id)


def recover_release(
    controller: DoorController,
    station_id: str,
    clock: Clock,
    audit: AuditLog | None = None,
) -> dict | None:
    """把未完成的放行收回关闭位；没有未完成放行时返回 ``None``。"""

    doc = load_release(controller, station_id)
    if doc is None or doc.get("state", "idle") in TERMINAL_STATES:
        return None
    failures = _recall_waves(controller, station_id, doc, controller.release_policy())
    recalled = [group for wave in doc["waves"] if wave["state"] == "recalled" for group in wave["zones"]]
    doc["recalled"] = sorted(set(doc.get("recalled", [])) | set(recalled))
    doc["opened"] = []
    if failures:
        doc["recall_failures"] = failures
    doc["state"] = "recall_failed" if failures else "recovered"
    doc["finished_at"] = clock.now()
    _save(controller, doc, clock)
    _audit(
        audit,
        "release_recovered",
        station_id,
        release_id=str(doc.get("release_id", "")),
        recalled=len(doc["recalled"]),
        failures=len(failures),
    )
    return doc


def recover_all_releases(
    controller: DoorController,
    clock: Clock,
    audit: AuditLog | None = None,
) -> list[dict]:
    """把存储里所有未完成的放行逐个收回；返回被恢复的放行进度。"""

    recovered: list[dict] = []
    for doc_id in controller.store.list_ids():
        if not doc_id.startswith("release/") or doc_id.count("/") != 1:
            continue
        doc = recover_release(controller, doc_id.split("/", 1)[1], clock, audit=audit)
        if doc is not None:
            recovered.append(doc)
    return recovered


def _run(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    clock: Clock,
    audit: AuditLog | None,
) -> dict:
    """放行主流程：调度 → 逐波开门与确认 → 异常时回收。"""

    # 上一次放行如果半途崩溃，先按进度文档收回，再开始新的放行。
    recover_release(controller, station_id, clock, audit=audit)

    policy = controller.release_policy()
    order = open_order(zones)
    limit = controller.batch_limit()
    scheduled = order[:limit]
    deferred = order[limit:]

    # 全站门只取一次，按门区分组；开门基于这份快照，避免按区反复扫库。
    by_group: dict[str, list[str]] = {}
    for gate in controller.gates.by_station(station_id):
        by_group.setdefault(gate.group, []).append(gate.id)

    skipped: dict[str, str] = {}
    runnable: list[tuple[str, list[str]]] = []
    for group in scheduled:
        gate_ids = by_group.get(group, [])
        if not gate_ids:
            skipped[group] = "gate_missing"
            continue
        runnable.append((group, gate_ids))

    release_id = controller.next_release_id(station_id)
    doc = {
        "release_id": release_id,
        "station_id": station_id,
        "state": "releasing",
        "backpressure": policy["backpressure"],
        "recall_mode": policy["recall_mode"],
        "order": order,
        "deferred": deferred,
        "skipped": dict(skipped),
        "limit": limit,
        "waves": [],
        "current_wave": -1,
        "opened": [],
        "recalled": [],
        "started_at": clock.now(),
        "updated_at": clock.now(),
        "finished_at": None,
        "violations": [],
    }
    # 先落进度再动手：崩溃后恢复流程能拿到完整的调度意图。
    _save(controller, doc, clock)
    _audit(
        audit,
        "release_start",
        station_id,
        release_id=release_id,
        zones=len(runnable),
        deferred=len(deferred),
        skipped=len(skipped),
    )

    opened: list[str] = []
    violations: list[str] = []
    wave_size = policy["wave_size"]
    cursor = 0
    state = "done"
    while cursor < len(runnable):
        pairs = runnable[cursor : cursor + wave_size]
        wave_groups = [group for group, _ in pairs]
        wave_gates = [gate_id for _, gate_ids in pairs for gate_id in gate_ids]

        # 联锁状态每波开考时现查：开考前一刻被锁的门区整波跳过，不静默漏开。
        if any(controller.lockouts.is_locked(gate_id) for gate_id in wave_gates):
            for group in wave_groups:
                skipped[group] = "locked"
            doc["skipped"] = dict(skipped)
            _save(controller, doc, clock)
            cursor += len(pairs)
            continue

        if policy["backpressure"] == "count":
            allowed, open_count, waited = _wait_headroom(
                controller, station_id, len(wave_gates), policy, clock
            )
            if waited > 0 or not allowed:
                _audit(
                    audit,
                    "release_backpressure",
                    station_id,
                    release_id=release_id,
                    mode="count",
                    open_count=open_count,
                    ceiling=policy["max_open_gates"],
                    waited_s=round(waited, 3),
                )
            if not allowed:
                violations.append(
                    f"站台开门数 {open_count} 加上本波 {len(wave_gates)} 樘超过上限 "
                    f"{policy['max_open_gates']}，等待回落超时"
                )
                state = "recalling"
                break

        wave = {
            "index": len(doc["waves"]),
            "zones": wave_groups,
            "gates": wave_gates,
            "state": "opening",
            "opened_at": None,
            "confirmed_at": None,
            "latency_s": None,
        }
        doc["waves"].append(wave)
        doc["current_wave"] = wave["index"]
        _save(controller, doc, clock)

        try:
            for gate_id in wave_gates:
                controller.gates.set_open(gate_id, True)
        except RailError as error:
            violations.append(f"开门失败: {error.message}")
            state = "recalling"
            break
        wave["state"] = "open"
        wave["opened_at"] = clock.now()
        _save(controller, doc, clock)
        _audit(
            audit,
            "release_wave_open",
            station_id,
            release_id=release_id,
            wave=wave["index"],
            zones=len(wave_groups),
            gates=len(wave_gates),
        )

        confirmed, latency = _wait_confirm(controller, wave_gates, policy, clock)
        if not confirmed:
            violations.append(
                f"第 {wave['index'] + 1} 波 {','.join(wave_groups)} 确认超时"
                f"（{policy['confirm_timeout_s']}s），停止放行"
            )
            state = "recalling"
            break
        wave["state"] = "confirmed"
        wave["confirmed_at"] = clock.now()
        wave["latency_s"] = round(latency, 3)
        opened.extend(wave_groups)
        doc["opened"] = list(opened)
        _save(controller, doc, clock)
        _audit(
            audit,
            "release_wave_confirmed",
            station_id,
            release_id=release_id,
            wave=wave["index"],
            latency_s=round(latency, 3),
        )

        if policy["backpressure"] == "latency":
            if latency > policy["confirm_budget_s"]:
                shrunk = max(1, wave_size // 2)
                if shrunk != wave_size:
                    _audit(
                        audit,
                        "release_backpressure",
                        station_id,
                        release_id=release_id,
                        mode="latency",
                        latency_s=round(latency, 3),
                        budget_s=policy["confirm_budget_s"],
                        wave_size=shrunk,
                    )
                wave_size = shrunk
            elif wave_size < policy["wave_size"]:
                wave_size = min(policy["wave_size"], wave_size + 1)

        cursor += len(pairs)

    if state == "recalling":
        doc["state"] = "recalling"
        _save(controller, doc, clock)
        failures = _recall_waves(controller, station_id, doc, policy)
        recalled = [
            group for wave in doc["waves"] if wave["state"] == "recalled" for group in wave["zones"]
        ]
        doc["recalled"] = recalled
        doc["opened"] = []
        opened = []
        if failures:
            doc["recall_failures"] = failures
            violations.append(f"回收失败: {','.join(sorted(failures))}")
            state = "recall_failed"
        else:
            state = "recalled"
        _audit(
            audit,
            "release_recall",
            station_id,
            release_id=release_id,
            zones=len(recalled),
            failures=len(failures),
            mode=policy["recall_mode"],
        )

    doc["state"] = state
    doc["violations"] = violations
    doc["finished_at"] = clock.now()
    _save(controller, doc, clock)
    _audit(
        audit,
        f"release_{state}",
        station_id,
        release_id=release_id,
        opened=len(doc["opened"]),
        recalled=len(doc["recalled"]),
    )

    return {
        "ok": state == "done",
        "state": state,
        "station_id": station_id,
        "release_id": release_id,
        "order": order,
        "opened": doc["opened"],
        "recalled": doc["recalled"],
        "deferred": deferred,
        "skipped": skipped,
        "limit": limit,
        "waves": [_wave_summary(wave) for wave in doc["waves"]],
        "violations": violations,
    }


def _wait_headroom(
    controller: DoorController,
    station_id: str,
    incoming: int,
    policy: dict,
    clock: Clock,
) -> tuple[bool, int, float]:
    """按已开门数等站台腾出余量，返回（是否放行、当前开门数、等待秒数）。"""

    open_count = len([gate for gate in controller.gates.by_station(station_id) if gate.open])
    if open_count + incoming <= policy["max_open_gates"]:
        return True, open_count, 0.0
    start = clock.monotonic()
    deadline = start + policy["confirm_timeout_s"]
    while True:
        clock.sleep(policy["confirm_poll_s"])
        open_count = len([gate for gate in controller.gates.by_station(station_id) if gate.open])
        if open_count + incoming <= policy["max_open_gates"]:
            return True, open_count, clock.monotonic() - start
        if clock.monotonic() >= deadline:
            return False, open_count, clock.monotonic() - start


def _wait_confirm(
    controller: DoorController,
    gate_ids: list[str],
    policy: dict,
    clock: Clock,
) -> tuple[bool, float]:
    """等一波门全部确认到位，返回（是否确认、耗时秒数）。"""

    start = clock.monotonic()
    deadline = start + policy["confirm_timeout_s"]
    while True:
        if controller.confirm_gates(gate_ids):
            return True, clock.monotonic() - start
        if clock.monotonic() >= deadline:
            return False, clock.monotonic() - start
        clock.sleep(policy["confirm_poll_s"])


def _recall_waves(
    controller: DoorController,
    station_id: str,
    doc: dict,
    policy: dict,
) -> dict[str, str]:
    """收回本次放行打开的全部门，返回回收失败的门与原因。"""

    targets = [wave for wave in doc["waves"] if wave["state"] in WAVE_OPEN_STATES]
    gate_ids = [gate_id for wave in targets for gate_id in wave["gates"]]
    failures: dict[str, str] = {}
    closed: set[str] = set()
    if policy["recall_mode"] == "sweep":
        try:
            sweep_station(controller.gates, station_id)
            closed = set(gate_ids)
        except RailError as error:
            failures["sweep"] = error.message
    else:
        for gate_id in gate_ids:
            try:
                controller.gates.set_open(gate_id, False)
                closed.add(gate_id)
            except RailError as error:
                failures[gate_id] = error.message
    for wave in targets:
        if all(gate_id in closed for gate_id in wave["gates"]):
            wave["state"] = "recalled"
    return failures


def _wave_summary(wave: dict) -> dict:
    """把一波的执行结果压缩成报告里的一行。"""

    return {
        "index": wave["index"],
        "zones": list(wave["zones"]),
        "gates": len(wave["gates"]),
        "state": wave["state"],
        "latency_s": wave["latency_s"],
    }


def _save(controller: DoorController, doc: dict, clock: Clock) -> None:
    """落盘放行进度，并刷新更新时间。"""

    doc["updated_at"] = clock.now()
    controller.store.write(release_doc_id(doc["station_id"]), doc)


def _audit(audit: AuditLog | None, kind: str, station_id: str, **detail: object) -> None:
    """有审计流水时记录一条放行事件。"""

    if audit is not None:
        audit.record(kind, "", station_id, **detail)
