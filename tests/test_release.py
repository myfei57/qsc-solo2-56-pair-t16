"""分区放行：按波放行、背压、确认超时回收、并发下发与崩溃恢复。"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from psdline.app import Platform, build_platform
from psdline.clock import ManualClock
from psdline.config import default_config
from psdline.console.api import Api
from psdline.door import batch_open, recover_release, release_progress


def _platform(tmp_path, clock: ManualClock, **door_overrides) -> Platform:
    """按可选的门控策略覆盖构造一个平台。"""

    config = default_config()
    if door_overrides:
        config = replace(config, door=replace(config.door, **door_overrides))
    return build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))


def _audit_kinds(platform: Platform) -> list[str]:
    return [entry.kind for entry in platform.audit.list()]


def test_release_opens_wave_by_wave_in_mileage_order(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)
    confirmed: list[list[str]] = []

    def spy(controller, gate_ids):
        confirmed.append(list(gate_ids))
        return True

    platform.doors.set_release_confirmer(spy)
    report = platform.batch_open("ST1")

    assert report["ok"] is True
    assert report["state"] == "done"
    assert report["opened"] == ["G01", "G02", "G03", "G04"]
    assert report["recalled"] == []
    # 默认一波一个门区，按里程由近及远逐波确认。
    assert [wave["zones"] for wave in report["waves"]] == [["G01"], ["G02"], ["G03"], ["G04"]]
    assert all(wave["state"] == "confirmed" for wave in report["waves"])
    assert [gates[0] for gates in confirmed] == [
        "ST1-G01-01",
        "ST1-G02-01",
        "ST1-G03-01",
        "ST1-G04-01",
    ]
    assert platform.gates.count_open() == 24
    # 进度文档与审计读同一份：状态、放行标识一致。
    progress = platform.release_progress("ST1")
    assert progress["state"] == "done"
    assert progress["release_id"] == report["release_id"]
    kinds = _audit_kinds(platform)
    assert kinds.count("release_wave_confirmed") == 4
    assert "release_start" in kinds and "release_done" in kinds


def test_confirm_timeout_stops_and_recalls_opened_zones(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock, confirm_timeout_s=2.0, confirm_poll_s=0.5)

    def flaky(controller, gate_ids):
        # 第二波（G02）永远确认不了。
        return not gate_ids[0].startswith("ST1-G02")

    platform.doors.set_release_confirmer(flaky)
    report = platform.batch_open("ST1")

    assert report["ok"] is False
    assert report["state"] == "recalled"
    assert report["opened"] == []
    assert report["recalled"] == ["G01", "G02"]
    assert "确认超时" in report["violations"][0]
    # 已经开的全部被收回，站台不留半开门。
    assert platform.gates.count_open() == 0
    progress = platform.release_progress("ST1")
    assert progress["state"] == "recalled"
    assert progress["recalled"] == ["G01", "G02"]
    kinds = _audit_kinds(platform)
    assert "release_recall" in kinds and "release_recalled" in kinds


def test_count_backpressure_waits_then_recalls(tmp_path):
    clock = ManualClock()
    platform = _platform(
        tmp_path, clock, max_open_gates=6, confirm_timeout_s=1.0, confirm_poll_s=0.25
    )

    report = platform.batch_open("ST1")

    # 第一波 6 樘顶着上限放行；第二波会超限，等不到回落就停下并收回第一波。
    assert report["state"] == "recalled"
    assert report["recalled"] == ["G01"]
    assert "超过上限" in report["violations"][0]
    assert platform.gates.count_open() == 0
    backpressure = [
        entry
        for entry in platform.audit.list()
        if entry.kind == "release_backpressure" and entry.detail.get("mode") == "count"
    ]
    assert backpressure and backpressure[0].detail["ceiling"] == 6


def test_count_backpressure_drains_and_continues(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock, max_open_gates=6)

    def draining(controller, gate_ids):
        # 确认一波后，另一路流程随即将这波门关上（站台侧腾出余量）。
        confirmed = all(
            (gate := controller.gates.get(gate_id)) is not None and gate.open
            for gate_id in gate_ids
        )
        if confirmed:
            for gate_id in gate_ids:
                controller.gates.set_open(gate_id, False)
        return confirmed

    platform.doors.set_release_confirmer(draining)
    report = platform.batch_open("ST1")

    assert report["state"] == "done"
    assert report["opened"] == ["G01", "G02", "G03", "G04"]
    # 放行逻辑上完成；物理门位被另一路流程陆续收回。
    assert platform.gates.count_open() == 0


def test_latency_backpressure_shrinks_waves(tmp_path):
    clock = ManualClock()
    platform = _platform(
        tmp_path,
        clock,
        backpressure="latency",
        wave_size=2,
        confirm_budget_s=0.3,
        confirm_poll_s=0.1,
        confirm_timeout_s=5.0,
    )
    polls = {"count": 0}

    def slow_first_wave(controller, gate_ids):
        if gate_ids[0].startswith("ST1-G01"):
            polls["count"] += 1
            return polls["count"] > 4  # 0.4s，超过 0.3s 预算
        return True

    platform.doors.set_release_confirmer(slow_first_wave)
    report = platform.batch_open("ST1")

    assert report["state"] == "done"
    # 第一波 2 个门区确认超预算，后续波被切小成一个门区。
    assert [len(wave["zones"]) for wave in report["waves"]] == [2, 1, 1]
    latency_events = [
        entry
        for entry in platform.audit.list()
        if entry.kind == "release_backpressure" and entry.detail.get("mode") == "latency"
    ]
    assert latency_events and latency_events[0].detail["wave_size"] == 1


def test_concurrent_release_is_rejected(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)
    entered = threading.Event()
    allow = threading.Event()

    def blocking(controller, gate_ids):
        entered.set()
        assert allow.wait(timeout=5)
        return True

    platform.doors.set_release_confirmer(blocking)
    outcome: dict[str, dict] = {}

    def run() -> None:
        outcome["report"] = platform.batch_open("ST1")

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        # 放行进行中：进度对站台可见，并发下发被拒绝。
        assert platform.release_progress("ST1")["state"] == "releasing"
        second = platform.batch_open("ST1")
        assert second["ok"] is False
        assert second["state"] == "busy"
        assert second["violations"]
    finally:
        allow.set()
        thread.join(timeout=5)
    assert outcome["report"]["state"] == "done"
    assert platform.release_progress("ST1")["state"] == "done"


def test_crash_recovery_recalls_open_gates(tmp_path):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=ManualClock(), storage_root=storage)

    class Crash(Exception):
        pass

    def crashing(controller, gate_ids):
        if gate_ids[0].startswith("ST1-G02"):
            raise Crash()
        return True

    first.doors.set_release_confirmer(crashing)
    with pytest.raises(Crash):
        first.batch_open("ST1")
    # 崩溃现场：第一波已确认、第二波门已开未确认，进度停在 releasing。
    assert first.release_progress("ST1")["state"] == "releasing"
    assert len([gate for gate in first.gates.by_station("ST1") if gate.open]) == 12

    second = build_platform(default_config(), clock=ManualClock(), storage_root=storage)
    progress = second.release_progress("ST1")
    assert progress["state"] == "recovered"
    assert progress["recalled"] == ["G01", "G02"]
    assert [gate for gate in second.gates.by_station("ST1") if gate.open] == []
    assert "release_recovered" in _audit_kinds(second)
    # 恢复可重入：再次恢复不再产生动作。
    assert recover_release(second.doors, "ST1", ManualClock()) is None


def test_partial_lock_mid_release_skips_and_continues(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)

    def saboteur(controller, gate_ids):
        # 第一波确认时，第二波的门区被联锁锁定。
        if gate_ids[0].startswith("ST1-G01"):
            controller.lockouts.set("ST1-G02-01", True)
        return True

    platform.doors.set_release_confirmer(saboteur)
    report = platform.batch_open("ST1")

    assert report["state"] == "done"
    assert report["skipped"] == {"G02": "locked"}
    assert report["opened"] == ["G01", "G03", "G04"]
    # 有跳过则 ok 为 False，与既有批量开门语义一致。
    assert report["ok"] is False
    assert platform.gates.get("ST1-G02-02").open is False


def test_recall_failure_is_reported(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock, confirm_timeout_s=1.0, confirm_poll_s=0.5)

    def sabotaging(controller, gate_ids):
        if gate_ids[0].startswith("ST1-G01"):
            # 把第一波的一樘门从登记表里抹掉，让回收失败。
            controller.store.remove("psd/ST1-G01-03")
            return True
        return False  # 第二波永不确认，触发回收

    platform.doors.set_release_confirmer(sabotaging)
    report = platform.batch_open("ST1")

    assert report["ok"] is False
    assert report["state"] == "recall_failed"
    assert any("ST1-G01-03" in violation for violation in report["violations"])
    progress = platform.release_progress("ST1")
    assert progress["state"] == "recall_failed"
    assert "ST1-G01-03" in progress["recall_failures"]
    # 第二波全部收回；第一波的缺口被如实报出而不是静默放过。
    assert progress["recalled"] == ["G02"]
    assert platform.gates.get("ST1-G02-01").open is False


def test_targeted_recall_leaves_unrelated_doors_open(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock, confirm_timeout_s=1.0, confirm_poll_s=0.5)
    # 另一路流程开着的门，不在本次放行的门清单里。
    platform.gates.set_open("ST1-G04-01", True)

    def fail_second(controller, gate_ids):
        return gate_ids[0].startswith("ST1-G01")

    platform.doors.set_release_confirmer(fail_second)
    report = platform.batch_open("ST1")

    assert report["state"] == "recalled"
    # 原地关门只关本次放行打开的门，不碰其他流程的门。
    assert platform.gates.get("ST1-G04-01").open is True
    assert platform.gates.get("ST1-G01-01").open is False


def test_sweep_recall_closes_whole_station(tmp_path):
    clock = ManualClock()
    platform = _platform(
        tmp_path, clock, recall_mode="sweep", confirm_timeout_s=1.0, confirm_poll_s=0.5
    )
    platform.gates.set_open("ST1-G04-01", True)

    def fail_second(controller, gate_ids):
        return gate_ids[0].startswith("ST1-G01")

    platform.doors.set_release_confirmer(fail_second)
    report = platform.batch_open("ST1")

    assert report["state"] == "recalled"
    # 清扫模式连不在放行清单里的门一起收。
    assert platform.gates.get("ST1-G04-01").open is False
    assert platform.gates.count_open() == 0


def test_progress_shared_between_door_platform_audit_and_console(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)
    report = platform.batch_open("ST1")
    release_id = report["release_id"]

    assert platform.release_progress("ST1")["release_id"] == release_id
    assert platform.station_view("ST1")["release"]["release_id"] == release_id
    door_report = platform.status()["door_report"]
    assert door_report["releases"]["ST1"]["release_id"] == release_id
    assert door_report["releases"]["ST1"]["state"] == "done"
    entries = [
        entry for entry in platform.audit.list() if entry.detail.get("release_id") == release_id
    ]
    assert entries and any(entry.kind == "release_done" for entry in entries)

    api = Api(platform)
    status, payload = api.handle("GET", "/api/release", {"station_id": "ST1"}, {})
    assert status == 200
    assert payload["release_id"] == release_id
    assert payload["state"] == "done"
    status, payload = api.handle("GET", "/api/release", {}, {})
    assert status == 400


def test_open_path_lists_station_gates_once(tmp_path):
    clock = ManualClock()
    # latency 模式不做开门数检查，开门路径只应取一次全站门清单。
    platform = _platform(tmp_path, clock, backpressure="latency")
    calls = {"count": 0}
    original = platform.gates.by_station

    def counting(station_id):
        calls["count"] += 1
        return original(station_id)

    platform.gates.by_station = counting
    report = platform.batch_open("ST1")

    assert report["state"] == "done"
    assert calls["count"] == 1


def test_release_scales_to_more_zones(tmp_path):
    clock = ManualClock()
    config = default_config()
    config = replace(
        config, station=replace(config.station, groups_per_station=8, doors_per_group=4)
    )
    # 站台变大后开门数上限同步放宽，否则背压会按设计拦下后半程。
    config = replace(config, door=replace(config.door, max_open_gates=32))
    platform = build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))

    report = platform.batch_open("ST1")

    assert report["state"] == "done"
    assert len(report["opened"]) == 8
    assert len(report["waves"]) == 8
    assert platform.gates.count_open() == 32


def test_idle_progress_and_recover_noop(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)

    progress = release_progress(platform.doors, "ST1")
    assert progress["state"] == "idle"
    assert progress["opened"] == []
    assert recover_release(platform.doors, "ST1", clock) is None


def test_function_level_batch_open_keeps_legacy_keys(tmp_path):
    clock = ManualClock()
    platform = _platform(tmp_path, clock)
    platform.doors.set_batch_limit(2)
    layout = platform.layouts.get("ST1")

    outcome = batch_open(platform.doors, "ST1", list(layout.zones), clock=clock)

    assert outcome["limit"] == 2
    assert outcome["opened"] == ["G01", "G02"]
    assert outcome["deferred"] == ["G03", "G04"]
    assert outcome["order"][0] == "G01"
    assert outcome["state"] == "done"
