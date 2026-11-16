import math
from dataclasses import asdict
from pathlib import Path

import pytest

from tuner.backends.simulated import SimulatedBackend, SimulatedHardCrash
from tuner.cli import main
from tuner.core.config import PRESETS, Config
from tuner.core.journal import Journal
from tuner.core.search import Search
from tuner.core.types import ReadbackMismatch, SafetyLimitExceeded, Settings, Timing
from tuner.workloads.simulated import make_workloads


def pending(path, settings, risk="profile"):
    with Journal(path) as journal:
        value = (
            asdict(settings)
            if risk == "profile"
            else {"mhz": settings.vram_mhz, "timing": settings.timing}
        )
        journal.append("test_start", setting=risk, value=value, settings=asdict(settings))


def test_verification_crash_with_stock_vram_can_tune_again(tmp_path):
    path = tmp_path / "resume.jsonl"
    backend = SimulatedBackend(path, "no_vram_tuning", seed=42)
    pending(path, Settings(70, backend.profile.stock_memory))
    for _ in range(3):
        with Journal(path) as journal:
            bounds = journal.crash_policy(Settings(0, backend.profile.stock_memory))
            assert bounds.memory is None and not bounds.stock_crashed
            final = Search(
                backend,
                make_workloads(backend),
                journal,
                config=Config(check_interval_seconds=30),
            ).tune(watchdog=False)
            assert 0 < final.reduction_mv < 70


def test_crash_at_true_stock_refuses_and_explains_restart(tmp_path, capsys):
    path = tmp_path / "stock.jsonl"
    pending(path, Settings(0, 2100))
    for _ in range(3):
        with pytest.raises(SystemExit) as exc:
            main(
                ["tune", "--profile", "no_vram_tuning", "--seed", "42", "--journal", str(path)]
            )
        assert exc.value.code == 2
        output = capsys.readouterr()
        assert "crash at stock settings" in output.out
        assert "NEW --journal" in output.err


@pytest.mark.parametrize("risk", ["profile", "vram"])
def test_fast_timing_crash_blocks_fast_for_the_whole_journal(tmp_path, risk):
    path = tmp_path / "fast.jsonl"
    backend = SimulatedBackend(path, seed=42)
    pending(path, Settings(30, 2530, Timing.FAST), risk)
    with Journal(path) as journal:
        bounds = journal.crash_policy(Settings(0, 2500))
        assert Timing.FAST in bounds.timings
        start = len(journal.records)
        for _ in range(2):
            for _ in range(510):
                try:
                    final = Search(
                        backend,
                        make_workloads(backend),
                        journal,
                        config=Config(check_interval_seconds=30),
                    ).tune(watchdog=False)
                    break
                except SimulatedHardCrash:
                    journal.recover()
            assert final.timing == Timing.DEFAULT
        assert all(
            r["settings"]["timing"] != "fast"
            for r in journal.records[start:]
            if r["kind"] in ("intent", "test_start")
        )


@pytest.mark.parametrize("preset", PRESETS)
def test_margin_is_applied_to_each_timing_frequency_pair(tmp_path, preset):
    path = tmp_path / "pair.jsonl"
    backend = SimulatedBackend(path, seed=42)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal, preset)
        search.supervisor.reset()
        assert search.baseline()
        for _ in range(510):
            try:
                pair = search.memory_pair(0, journal.crash_policy(search.stock))
                break
            except SimulatedHardCrash:
                journal.recover()
                search.supervisor.reset()
        assert pair.vram_mhz <= backend.memory_knee(pair.timing)
        assert (
            backend.memory_knee(pair.timing) - pair.vram_mhz
            >= PRESETS[preset].vram_margin_mhz - 5
        )


def test_mid_verification_thermal_fault_stops_within_one_slice(tmp_path):
    path = tmp_path / "thermal.jsonl"
    backend = SimulatedBackend(path)
    workloads = make_workloads(backend)
    original = workloads["combined"].run

    def thermal(seconds):
        result = original(seconds)
        if backend.elapsed_seconds >= 13:
            backend.temperature = 100
        return result

    workloads["combined"].run = thermal
    with Journal(path) as journal:
        search = Search(
            backend,
            workloads,
            journal,
            config=Config(check_interval_seconds=5, verification_ramp_seconds=()),
        )
        search.baselines = {"combined": {"score": 1000, "noise": 2}}
        with pytest.raises(SafetyLimitExceeded, match="temperature"):
            search.verify(search.stock, ["combined"], 0, "final")
        assert 13 <= backend.elapsed_seconds <= 18
        result = journal.records[-1]
        assert result["requested_seconds"] == 1800 and result["seconds"] == 15
        search.supervisor.reset()


def test_trials_and_legacy_records_cannot_be_used_as_final_profiles(tmp_path):
    path = tmp_path / "passed.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal)
        search.baselines = {"core": {"score": 1000, "noise": 2}}
        assert search.trial(search.stock, ["core"], 8)
        assert journal.records[-1]["kind"] == "passed"
        assert journal.last_accepted() is None
        journal.append("accepted", settings=asdict(Settings(80, 2500)))
        assert journal.last_accepted() is None


def test_torn_tail_truncation_never_rewrites_valid_prefix(tmp_path, monkeypatch):
    path = tmp_path / "tail.jsonl"
    prefix = b'{ "kind": "run_start", "seq": 1, "note": "original spacing" }\n'
    path.write_bytes(prefix + b'{"kind":')
    original = Path.open
    modes = []

    def opening(self, mode="r", *args, **kwargs):
        if self == path:
            modes.append(mode)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opening)
    with Journal(path):
        pass
    assert path.read_bytes() == prefix
    assert "r+b" in modes and "wb" not in modes


def test_watchdog_cleanup_never_masks_original_setter_error(tmp_path, monkeypatch):
    path = tmp_path / "cleanup.jsonl"
    backend = SimulatedBackend(path, fault="readback")
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal)
        monkeypatch.setattr(search.supervisor, "start_watchdog", lambda: None)

        def broken():
            raise RuntimeError("injected cleanup error")

        monkeypatch.setattr(search.supervisor, "stop_watchdog", broken)
        with pytest.raises(ReadbackMismatch) as exc:
            search.tune()
        assert any("injected cleanup error" in note for note in exc.value.__notes__)


def test_frozen_duration_model_and_new_profiles(tmp_path):
    backend = SimulatedBackend(tmp_path / "model.jsonl", seed=42)
    assert 2 <= backend.width_mv <= 5 and 2 <= backend.memory_width_mhz <= 5
    edge = backend.load_edge("burst")
    assert backend.load_edge("core") - edge == 6
    assert backend.failure_rate(edge, edge, backend.width_mv) == pytest.approx(
        math.log(2) / 1800
    )
    settings = Settings(edge, 2500)
    p8, p600, p1800 = [
        backend.voltage_failure_probability(t, settings=settings) for t in (8, 600, 1800)
    ]
    assert p8 < p600 < p1800
    assert p1800 == pytest.approx(0.5)
    # Statistical grouping of constant-state exposure must not change failure odds.
    p300 = backend.voltage_failure_probability(300, settings=settings)
    assert 1 - (1 - p300) ** 6 == pytest.approx(p1800)
    assert (
        SimulatedBackend(tmp_path / "none", "no_voltage_tuning")
        .read_capabilities()
        .voltage_kind
        == "none"
    )
    assert (
        SimulatedBackend(tmp_path / "step", "voltage_step_5mv")
        .read_limits()
        .voltage_reduction.step
        == 5
    )
