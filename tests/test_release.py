"""分区放行与背压：逐区放行、确认超时回收、并发与崩溃恢复。"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.door import (
    ReleasePolicy,
    begin_release,
    confirm_release,
    door_report,
    open_group,
    release_next,
    release_progress,
)
from psdline.errors import ConfigError, InterlockError, StorageError


def _zones(platform, station_id="ST1"):
    return list(platform.layouts.get(station_id).zones)


def test_release_opens_zones_in_order_with_confirmation(platform):
    outcome = platform.batch_open("ST1")
    assert outcome["ok"] is True
    assert outcome["opened"] == ["G01", "G02", "G03", "G04"]
    progress = outcome["release"]
    assert progress["status"] == "done"
    assert [entry["state"] for entry in progress["zones"]] == ["confirmed"] * 4
    for entry in progress["zones"]:
        assert entry["released_at"] is not None
        assert entry["confirmed_at"] is not None
        assert entry["confirm_s"] >= 0
    # 窗口为 1 时严格串行：每一区都是先放行、确认到位，才放下一区。
    zone_events = [
        (entry.detail.get("zone"), entry.detail.get("state"))
        for entry in platform.audit.list()
        if entry.kind == "batch_release_zone"
    ]
    assert zone_events == [
        ("G01", "released"), ("G01", "confirmed"),
        ("G02", "released"), ("G02", "confirmed"),
        ("G03", "released"), ("G03", "confirmed"),
        ("G04", "released"), ("G04", "confirmed"),
    ]


def test_release_defers_zones_beyond_batch_limit(platform):
    platform.doors.set_batch_limit(2)
    outcome = platform.batch_open("ST1")
    assert outcome["opened"] == ["G01", "G02"]
    assert outcome["deferred"] == ["G03", "G04"]
    assert outcome["release"]["status"] == "done"


def test_release_skips_locked_zone(platform):
    gate = platform.gates.by_station("ST1")[0]
    platform.set_fault(gate.id)
    outcome = platform.batch_open("ST1")
    assert outcome["skipped"] == {"G01": "locked"}
    assert outcome["opened"] == ["G02", "G03", "G04"]
    assert outcome["ok"] is False


def test_count_backpressure_blocks_until_confirm(platform, clock):
    policy = ReleasePolicy(mode="count", window=1)
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    blocked = release_next(platform.doors, "ST1", clock.now())
    assert blocked["cursor"] == 1
    confirm_release(platform.doors, "ST1", clock.now())
    forward = release_next(platform.doors, "ST1", clock.now())
    assert forward["cursor"] == 2


def test_count_window_allows_limited_concurrency(platform, clock):
    policy = ReleasePolicy(mode="count", window=2)
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    assert release_next(platform.doors, "ST1", clock.now())["cursor"] == 1
    assert release_next(platform.doors, "ST1", clock.now())["cursor"] == 2
    assert release_next(platform.doors, "ST1", clock.now())["cursor"] == 2


def test_confirm_timeout_stops_and_recalls_opened(platform, clock):
    policy = ReleasePolicy(confirm_timeout_s=5.0)
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    gate = platform.gates.by_station("ST1")[0]
    platform.set_fault(gate.id)
    clock.advance(10.0)
    progress = confirm_release(platform.doors, "ST1", clock.now())
    assert progress["status"] == "aborted"
    assert progress["reason"] == "confirm_timeout"
    assert progress["recall"]["mode"] == "close"
    assert set(progress["recall"]["closed"]) == set(progress["zones"][0]["gates"])
    assert progress["cursor"] == 1
    assert all(not item.open for item in platform.gates.by_station("ST1"))
    assert progress["zones"][0]["state"] == "failed"


def test_recall_close_keeps_foreign_open_gates(platform, clock):
    open_group(platform.doors, platform.mappings, "ST1", "G03")
    policy = ReleasePolicy(confirm_timeout_s=5.0, recall_mode="close")
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    platform.set_fault("ST1-G01-01")
    clock.advance(10.0)
    confirm_release(platform.doors, "ST1", clock.now())
    foreign = [gate for gate in platform.gates.by_station("ST1") if gate.group == "G03"]
    assert all(gate.open for gate in foreign)
    session_zones = [gate for gate in platform.gates.by_station("ST1") if gate.group == "G01"]
    assert all(not gate.open for gate in session_zones)


def test_recall_sweep_closes_whole_station(platform, clock):
    open_group(platform.doors, platform.mappings, "ST1", "G03")
    policy = ReleasePolicy(confirm_timeout_s=5.0, recall_mode="sweep")
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    platform.set_fault("ST1-G01-01")
    clock.advance(10.0)
    progress = confirm_release(platform.doors, "ST1", clock.now())
    assert progress["recall"]["mode"] == "sweep"
    assert all(not gate.open for gate in platform.gates.by_station("ST1"))


def test_time_mode_holds_on_slow_confirm(platform, clock):
    policy = ReleasePolicy(mode="time", confirm_budget_s=2.0, confirm_timeout_s=30.0)
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    clock.advance(5.0)
    progress = confirm_release(platform.doors, "ST1", clock.now())
    assert progress["status"] == "held"
    assert progress["reason"] == "confirm_budget_exceeded"
    assert progress["deferred"] == ["G02", "G03", "G04"]
    # 已确认的门区保持开着，只是不再放新批。
    opened = [gate for gate in platform.gates.by_station("ST1") if gate.group == "G01"]
    assert all(gate.open for gate in opened)


def test_count_mode_ignores_slow_confirm(platform, clock):
    policy = ReleasePolicy(mode="count", confirm_budget_s=2.0, confirm_timeout_s=30.0)
    begin_release(platform.doors, "ST1", _zones(platform), policy, "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    clock.advance(5.0)
    progress = confirm_release(platform.doors, "ST1", clock.now())
    # count 模式只看窗口不看耗时：确认慢不会拦下后续放行。
    assert progress["status"] == "running"
    for _ in range(8):
        if progress["status"] != "running":
            break
        progress = release_next(platform.doors, "ST1", clock.now())
        if progress["status"] != "running":
            break
        progress = confirm_release(platform.doors, "ST1", clock.now())
    assert progress["status"] == "done"


def test_second_begin_is_rejected(platform, clock):
    begin_release(platform.doors, "ST1", _zones(platform), ReleasePolicy(), "s1", clock.now())
    with pytest.raises(InterlockError):
        begin_release(platform.doors, "ST1", _zones(platform), ReleasePolicy(), "s2", clock.now())


def test_batch_open_reports_live_session(platform, clock):
    begin_release(platform.doors, "ST1", _zones(platform), ReleasePolicy(), "s1", clock.now())
    outcome = platform.batch_open("ST1")
    assert outcome["ok"] is False
    assert outcome["violations"]
    assert platform.release_progress("ST1")["session_id"] == "s1"


def test_concurrent_begin_has_single_winner(platform, clock):
    zones = _zones(platform)
    barrier = threading.Barrier(6)
    won: list[int] = []
    lost: list[int] = []

    def worker(index: int) -> None:
        barrier.wait()
        try:
            begin_release(platform.doors, "ST1", zones, ReleasePolicy(), f"s{index}", clock.now())
            won.append(index)
        except InterlockError:
            lost.append(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 1
    assert len(lost) == 5


def test_concurrent_batch_open_keeps_single_consistent_session(platform):
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(platform.batch_open("ST1"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 2
    assert platform.release_progress("ST1")["status"] == "done"
    assert platform.gates.count_open() == 24


def test_different_stations_release_in_parallel(platform):
    barrier = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def worker(station_id: str) -> None:
        barrier.wait()
        outcomes[station_id] = platform.batch_open(station_id)

    threads = [threading.Thread(target=worker, args=(station,)) for station in ("ST1", "ST2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes["ST1"]["release"]["status"] == "done"
    assert outcomes["ST2"]["release"]["status"] == "done"


def test_partial_failure_rolls_back_and_raises(platform, clock):
    begin_release(platform.doors, "ST1", _zones(platform), ReleasePolicy(), "s1", clock.now())
    release_next(platform.doors, "ST1", clock.now())
    confirm_release(platform.doors, "ST1", clock.now())
    original = platform.gates.set_open

    def broken(psd_id, open_state):
        if open_state and psd_id == "ST1-G02-03":
            raise StorageError("写入失败", component="store")
        return original(psd_id, open_state)

    platform.gates.set_open = broken
    try:
        with pytest.raises(StorageError):
            release_next(platform.doors, "ST1", clock.now())
    finally:
        platform.gates.set_open = original
    progress = release_progress(platform.doors, "ST1")
    assert progress["status"] == "aborted"
    assert progress["reason"].startswith("release_failed:")
    states = {entry["zone"]: entry["state"] for entry in progress["zones"]}
    assert states["G01"] == "recalled"
    assert states["G02"] == "failed"
    assert all(not gate.open for gate in platform.gates.by_station("ST1"))


def test_crash_recovery_recalls_open_gates(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    begin_release(first.doors, "ST1", _zones(first), ReleasePolicy(), "s1", clock.now())
    release_next(first.doors, "ST1", clock.now())
    assert any(gate.open for gate in first.gates.by_station("ST1"))
    # 模拟崩溃重启：同一存储目录上重新装配，进行中的会话必须被收回。
    second = build_platform(default_config(), clock=clock, storage_root=storage)
    progress = release_progress(second.doors, "ST1")
    assert progress["status"] == "aborted"
    assert progress["reason"] == "crash_recovery"
    assert all(not gate.open for gate in second.gates.by_station("ST1"))
    assert any(entry.kind == "batch_release_recovered" for entry in second.audit.list())


def test_progress_is_shared_by_door_station_and_audit(platform):
    outcome = platform.batch_open("ST1")
    progress = platform.release_progress("ST1")
    assert progress["status"] == "done"
    view = platform.station_view("ST1")
    assert view["release"]["status"] == progress["status"]
    assert view["release"]["confirmed"] == len(outcome["opened"])
    report = door_report(platform.doors, ["ST1"])
    assert report["stations"][0]["release"]["status"] == progress["status"]
    done = [entry for entry in platform.audit.list() if entry.kind == "batch_release_done"]
    assert done
    assert done[-1].detail["session_id"] == progress["session_id"]


def test_batch_open_scans_gate_store_once(platform):
    calls: list[int] = []
    original = platform.gates.list

    def counting():
        calls.append(1)
        return original()

    platform.gates.list = counting
    try:
        platform.batch_open("ST1")
    finally:
        platform.gates.list = original
    assert len(calls) == 1


def test_console_exposes_release_progress(platform):
    from psdline.console.api import Api

    api = Api(platform)
    platform.batch_open("ST1")
    status, payload = api.handle("GET", "/api/release/ST1", {}, {})
    assert status == 200
    assert payload["status"] == "done"
    assert payload["session_id"]
    status, idle = api.handle("GET", "/api/release/ST3", {}, {})
    assert status == 200
    assert idle["status"] == "idle"


def test_config_rejects_bad_release_values():
    config = default_config()
    with pytest.raises(ConfigError):
        replace(config, door=replace(config.door, release_mode="nope")).validate()
    with pytest.raises(ConfigError):
        replace(config, door=replace(config.door, release_window=0)).validate()
    with pytest.raises(ConfigError):
        replace(config, door=replace(config.door, release_confirm_s=0)).validate()
    with pytest.raises(ConfigError):
        replace(
            config, door=replace(config.door, release_budget_s=10.0, release_confirm_s=5.0)
        ).validate()
    with pytest.raises(ConfigError):
        replace(config, door=replace(config.door, recall_mode="nope")).validate()
    valid = replace(
        config, door=replace(config.door, release_mode="time", recall_mode="sweep")
    ).validate()
    assert valid.door.release_mode == "time"


def test_run_release_with_time_mode_platform(tmp_path):
    clock = ManualClock()
    config = replace(
        default_config(),
        door=replace(
            default_config().door,
            release_mode="time",
            release_budget_s=2.0,
            release_confirm_s=30.0,
        ),
    ).validate()
    platform = build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))
    outcome = platform.batch_open("ST1")
    # 确认即时到位（耗时 0，预算内）：time 模式也能放完全部门区。
    assert outcome["release"]["status"] == "done"
    assert outcome["opened"] == ["G01", "G02", "G03", "G04"]
