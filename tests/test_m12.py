from dataclasses import asdict

import pytest

from tuner.backends.simulated import SimulatedBackend
from tuner.core.config import Config
from tuner.core.journal import Journal
from tuner.core.search import Search
from tuner.core.types import Range, Settings, Timing
from tuner.report import report
from tuner.workloads.simulated import make_workloads


def test_crashed_upper_bracket_resumes_by_bisection_with_margin_clearance(tmp_path):
    path = tmp_path / "bisect.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal, "safe")
        attempted = []

        def evaluate(value):
            attempted.append(value)
            return value <= 100

        result = search.bracket(
            95, 120 - 15, 5, 2, Range(0, 200, 1), evaluate, refine=evaluate, failed=120
        )
        assert result == 100
        assert attempted == [105, 100, 102]
        assert all(v <= 120 - 15 for v in attempted)


@pytest.mark.parametrize("failure", ["vram_verify", "combined", "burst"])
def test_final_failure_changes_only_the_attributed_knob(tmp_path, monkeypatch, failure):
    path = tmp_path / "attribution.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal)
        monkeypatch.setattr(search, "baseline", lambda: True)
        monkeypatch.setattr(search, "bracket", lambda *a, **kw: 80)
        monkeypatch.setattr(
            search, "memory_pair", lambda voltage, bounds: Settings(voltage, 2600, Timing.FAST)
        )
        finals = []

        def verification(settings, names, attempt, phase):
            if phase == "voltage":
                return True
            finals.append(settings)
            search.last_failure = failure
            return len(finals) > 1

        monkeypatch.setattr(search, "verify", verification)
        accepted, outcome = search._search()
        assert outcome == "verified in simulation"
        assert finals[0] == Settings(70, 2600, Timing.FAST)
        if failure == "burst":
            assert accepted == Settings(60, 2600, Timing.FAST)
        else:
            assert accepted == Settings(70, 2585, Timing.FAST)


def test_ramps_preserve_full_passing_duration_for_every_workload(tmp_path, monkeypatch):
    path = tmp_path / "ramp.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal)
        calls = []

        def trial(settings, names, seconds):
            calls.append((list(names), seconds))
            return True

        monkeypatch.setattr(search, "trial", trial)
        assert search.verify(Settings(50, 2500), ["core", "burst", "combined"], 0, "voltage")
        assert calls == [
            (["burst"], 60),
            (["burst"], 300),
            (["core", "burst", "combined"], 1800),
        ]


def test_failed_ramp_never_starts_full_verification(tmp_path, monkeypatch):
    path = tmp_path / "failed-ramp.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        search = Search(backend, make_workloads(backend), journal)
        monkeypatch.setattr(search, "trial", lambda *args: False)
        assert not search.verify(Settings(50, 2500), ["combined", "burst"], 0, "final")
        assert [r["stage"] for r in journal.records if r["kind"] == "verification"] == ["ramp"]


def test_report_counts_recovered_crashes_and_test_time_across_runs(tmp_path):
    path = tmp_path / "totals.jsonl"
    settings = asdict(Settings(30, 2500))
    start = {"preset": "balanced", "identity": {"capabilities": {"voltage_kind": "offset"}}}
    with Journal(path) as journal:
        journal.append("run_start", **start)
        journal.append(
            "test_result", seconds=30, workload="burst", settings=settings, ok=True, reasons=[]
        )
        journal.append(
            "test_start", setting="profile", value=settings, settings=settings, seconds=60
        )
    with Journal(path) as journal:
        journal.append("run_start", **start)
        journal.append(
            "test_result", seconds=15, workload="burst", settings=settings, ok=True, reasons=[]
        )
    result = report(path)
    assert result["hard_crashes_recovered"] == 1
    assert result["synthetic_seconds_tested"] == 45
    assert result["test_time"]["total_seconds_lower_bound"] == 45
    assert result["test_time"]["total_seconds_upper_bound"] == 105


def test_invalid_probe_durations_are_refused(tmp_path):
    path = tmp_path / "invalid.jsonl"
    backend = SimulatedBackend(path)
    with Journal(path) as journal:
        with pytest.raises(ValueError, match="durations"):
            Search(backend, make_workloads(backend), journal, config=Config(refine_seconds=0))
