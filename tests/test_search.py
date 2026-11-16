import ast
import json
import multiprocessing as mp
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tuner.backends.simulated import PROFILES, SimulatedBackend, SimulatedHardCrash
from tuner.cli import main
from tuner.core.config import PRESETS, Config
from tuner.core.journal import Journal, read_records
from tuner.core.search import Search
from tuner.core.supervisor import Supervisor
from tuner.core.types import (
    BackendError,
    ReadbackMismatch,
    SafetyLimitExceeded,
    Settings,
    VoltageKind,
)
from tuner.report import report
from tuner.workloads.simulated import make_workloads


def recover_tune(backend, journal, preset="balanced", config=None, workloads=None):
    config = config or Config(check_interval_seconds=30)
    for _ in range(510):
        try:
            return Search(
                backend, workloads or make_workloads(backend), journal, preset, config
            ).tune(watchdog=False)
        except SimulatedHardCrash:
            journal.recover()
    pytest.fail("Model did not reach a terminal state")


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("preset", PRESETS)
def test_tune_finishes_at_verified_profile(tmp_path, profile, preset):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, profile, seed=42)
    with Journal(path) as journal:
        accepted = recover_tune(backend, journal, preset)
        assert backend.read_telemetry().settings == accepted
        assert journal.records[-1]["kind"] == "run_end"
    result = report(path)
    assert result["hardware_tested"] is False
    assert result["margins"]["voltage_margin_mv"] == PRESETS[preset].voltage_margin_mv
    if profile == "no_vram_tuning":
        assert result["skipped"]


@pytest.mark.parametrize("fault", ["rejected", "unavailable", "readback"])
def test_typed_setter_errors_abort_and_restore_stock(tmp_path, fault):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, fault=fault)
    with Journal(path) as journal:
        with pytest.raises(ReadbackMismatch if fault == "readback" else BackendError):
            Search(backend, make_workloads(backend), journal).tune(watchdog=False)
        assert backend.read_telemetry().settings == Settings(0, backend.profile.stock_memory)
        assert journal.records[-1]["outcome"] == "error: restored stock"


@pytest.mark.parametrize(
    "fault,reason", [("thermal", "temperature"), ("power", "power"), ("driver_reset", "crash")]
)
def test_supervisor_rejects_limits_and_driver_events(tmp_path, fault, reason):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, fault=fault)
    with Journal(path) as journal:
        engine = Search(backend, make_workloads(backend), journal)
        if fault in ("thermal", "power"):
            with pytest.raises(SafetyLimitExceeded):
                engine.tune(watchdog=False)
        else:
            assert engine.tune(watchdog=False) == Settings(0, backend.profile.stock_memory)
        assert backend.read_telemetry().settings == Settings(0, backend.profile.stock_memory)
        assert any(reason in str(r.get("reasons", [])) for r in journal.records)


def test_journal_device_mismatch_refused_before_setter(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        backend = SimulatedBackend(path, seed=1)
        recover_tune(backend, journal)
    with Journal(path) as journal:
        before = len(journal.records)
        backend = SimulatedBackend(path, seed=2)
        with pytest.raises(ValueError, match="different device"):
            Search(backend, make_workloads(backend), journal).tune(watchdog=False)
        assert len(journal.records) == before


def test_dry_run_creates_no_journal_and_no_setter(tmp_path, capsys, monkeypatch):
    path = tmp_path / "absent.jsonl"

    def forbidden(*args):
        pytest.fail("Dry run called a setter")

    monkeypatch.setattr(SimulatedBackend, "set_voltage", forbidden)
    monkeypatch.setattr(SimulatedBackend, "set_vram", forbidden)
    monkeypatch.setattr(SimulatedBackend, "reset", forbidden)
    assert main(["tune", "--dry-run", "--journal", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert not path.exists()


def test_no_hardware_backend_cli_path():
    with pytest.raises(SystemExit) as exc:
        main(["tune", "--backend", "adlx"])
    assert exc.value.code == 2


def test_core_has_no_backend_or_os_specific_imports():
    core = Path(__file__).parents[1] / "tuner" / "core"
    forbidden = ("tuner.backends", "tuner.workloads", "ctypes", "winreg", "msvcrt", "fcntl")
    for path in core.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not any(x.name.startswith(forbidden) for x in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden)


def _die_during_tune(backend):
    backend.hard_crash_mode = "process"
    with Journal(backend.journal_path) as journal:
        Search(backend, make_workloads(backend), journal).tune()


@pytest.mark.parametrize(
    "fault_at,profile",
    [(n, "rdna4_offset") for n in (1, 25, 26, 30, 40, 50, 60)] + [(33, "no_vram_tuning")],
)
def test_real_hard_crash_watchdog_reset_and_resume(tmp_path, fault_at, profile):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, profile, seed=42, fault="hard_crash", fault_at=fault_at)
    child = mp.get_context("spawn").Process(target=_die_during_tune, args=(backend,))
    child.start()
    child.join(20)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("Injected crash process hung")
    assert child.exitcode == 86
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        records = read_records(path)
        if any(r.get("source") == "watchdog" and r["kind"] == "applied" for r in records):
            break
        time.sleep(0.02)
    else:
        pytest.fail("Watchdog did not journal and reset after parent death")
    assert backend.read_telemetry().settings == Settings(0, backend.profile.stock_memory)
    with Journal(path) as journal:
        voltage, memory = journal.crash_bounds()
        assert (
            journal.crash_policy(Settings(0, backend.profile.stock_memory)).stock_crashed
            or voltage is not None
            or memory is not None
        )
        start = len(journal.records)
        resumed = SimulatedBackend(path, profile, seed=42)
        accepted = recover_tune(resumed, journal)
        if fault_at == 33:
            # Smaller M1.2 probes put this injected crash at 5 mV. A full
            # crash-clearance margin then permits stock only, not a false stock-crash lock.
            assert accepted.reduction_mv == 0
            assert journal.records[-1]["outcome"] == "verified in simulation"
        assert resumed.read_telemetry().settings == accepted
        for r in journal.records[start:]:
            if r["kind"] == "test_start":
                assert voltage is None or r["settings"]["reduction_mv"] < voltage
                assert memory is None or r["settings"]["vram_mhz"] < memory


def test_normal_cli_watchdog_disarms_and_report_is_read_only(tmp_path):
    path = tmp_path / "journal.jsonl"
    command = [sys.executable, "-m", "tuner", "tune", "--journal", str(path)]
    for _ in range(50):
        previous_seq = len(read_records(path))
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode != 86:
            break
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(
                r["seq"] > previous_seq
                and r.get("source") == "watchdog"
                and r["kind"] == "applied"
                for r in read_records(path)
            ):
                break
            time.sleep(0.02)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["outcome"] == "verified in simulation"
    before = path.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "tuner", "report", "--journal", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert path.read_bytes() == before


def test_unsupported_voltage_and_coarse_driver_steps(tmp_path):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, "no_vram_tuning")
    original = backend.read_capabilities
    backend.read_capabilities = lambda: replace(original(), voltage_kind=VoltageKind.NONE)
    with Journal(path) as journal:
        accepted = Search(backend, make_workloads(backend), journal).tune(watchdog=False)
        assert accepted.reduction_mv == 0
        assert not any(
            r["kind"] == "intent" and r["setting"] == "voltage" for r in journal.records
        )


def test_verification_failure_backs_off_and_can_return_stock(tmp_path):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path)
    workloads = make_workloads(backend)
    original = workloads["combined"].run

    def fail_long(seconds):
        result = original(seconds)
        return replace(result, ok=False) if seconds > 8 else result

    workloads["combined"].run = fail_long
    with Journal(path) as journal:
        accepted = recover_tune(
            backend,
            journal,
            workloads=workloads,
            config=Config(max_backoffs=2, check_interval_seconds=30),
        )
        assert accepted == Settings(0, backend.profile.stock_memory)
        assert {r["attempt"] for r in journal.records if r["kind"] == "verification"} == {
            0,
            1,
            2,
        }


def test_all_setters_follow_fsync(tmp_path, monkeypatch):
    import tuner.core.journal as journal_module

    path = tmp_path / "journal.jsonl"
    calls = []
    original_sync = journal_module.os.fsync

    def sync(fd):
        original_sync(fd)
        calls.append("fsync")

    monkeypatch.setattr(journal_module.os, "fsync", sync)
    backend = SimulatedBackend(path)
    original = backend.set_voltage

    def setter(value):
        assert calls[-1] == "fsync"
        calls.append("setter")
        original(value)

    backend.set_voltage = setter
    with Journal(path) as journal:
        Supervisor(backend, journal).apply(Settings(10, backend.profile.stock_memory))


def test_resume_at_every_recorded_trial_boundary(tmp_path):
    path = tmp_path / "original.jsonl"
    backend = SimulatedBackend(path, seed=42)
    with Journal(path) as journal:
        recover_tune(backend, journal)
        records = list(journal.records)
    for index, record in enumerate(records):
        if record["kind"] not in ("intent", "test_start") or record.get("setting") == "reset":
            continue
        target = tmp_path / f"recovery-{index}.jsonl"
        target.write_text("".join(json.dumps(r) + "\n" for r in records[: index + 1]))
        resumed = SimulatedBackend(target, seed=42)
        with Journal(target) as journal:
            voltage, memory = journal.crash_bounds()
            # An interrupted setter moving a knob back to stock need not add a
            # numeric bound, but the interruption itself must still be recovered.
            assert any(
                r["kind"] == "rejected" and r.get("recovered_seq") == record["seq"]
                for r in journal.records
            )
            start = len(journal.records)
            accepted = recover_tune(resumed, journal)
            assert resumed.read_telemetry().settings == accepted
            for trial in journal.records[start:]:
                if trial["kind"] == "test_start":
                    assert voltage is None or trial["settings"]["reduction_mv"] < voltage
                    assert memory is None or trial["settings"]["vram_mhz"] < memory


def test_nonfinite_measurement_aborts_to_stock(tmp_path):
    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path)
    workloads = make_workloads(backend)
    original = workloads["core"].run
    workloads["core"].run = lambda seconds: replace(original(seconds), score=float("nan"))
    with Journal(path) as journal:
        with pytest.raises(SafetyLimitExceeded, match="non-finite"):
            Search(backend, workloads, journal).tune(watchdog=False)
        assert backend.read_telemetry().settings == Settings(0, backend.profile.stock_memory)


def test_timing_only_memory_capability_and_driver_grids(tmp_path):
    from tuner.core.types import Range

    path = tmp_path / "journal.jsonl"
    backend = SimulatedBackend(path, seed=42)
    capabilities, limits = backend.read_capabilities(), backend.read_limits()
    backend.read_capabilities = lambda: replace(capabilities, vram_frequency=False)
    backend.read_limits = lambda: replace(
        limits, voltage_reduction=Range(0, 200, 5), vram=Range(2500, 2800, 10)
    )
    with Journal(path) as journal:
        accepted = recover_tune(backend, journal)
        assert accepted.reduction_mv % 5 == 0
        assert accepted.vram_mhz == 2500
        assert accepted.timing.value in ("default", "fast")
    assert report(path)["skipped"] == ["VRAM frequency"]
