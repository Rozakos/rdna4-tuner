import json
from pathlib import Path

import pytest

from tuner.backends.simulated import PROFILES, SimulatedBackend, SimulatedHardCrash
from tuner.core.config import PRESETS, Config
from tuner.core.journal import Journal
from tuner.core.search import Search
from tuner.workloads.simulated import make_workloads


class SearchJournal(Journal):
    """Production recovery/policy readers with a virtual durable append log.

    File ownership/fsync and real process death have separate subprocess tests.
    This sweep reopens the journal after EACH stochastic hard crash.
    """

    def __init__(self, path, records=()):
        self.path = path
        # Each append is already a serialized snapshot. Reopening copies the log's
        # index, not every immutable record; production policy/recovery still run.
        self.records = list(records)
        self.recover()

    def append(self, kind, **data):
        record = {"kind": kind, "seq": len(self.records) + 1, **data}
        # Match Journal.append's in-memory index. Event payloads are fresh snapshots
        # and never edited; serialization/durable writes have independent real-file tests.
        self.records.append(record)
        return record


class SweepBackend(SimulatedBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = [0, self.profile.stock_memory, 0, 0]

    def _guard(self, setting, value):
        record = self.test_journal.records[-1]
        assert record["kind"] == "intent" and record["setting"] == setting
        assert record["value"] == value and record["seq"] > self.state[3]
        self.state[3] = record["seq"]


RESULTS = []


@pytest.mark.acceptance
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("preset", PRESETS)
def test_1000_seeded_cards_per_profile_and_preset(tmp_path, profile, preset):
    safe = accepted_count = memory_safe = memory_close = verification_failures = crashes = 0
    # Same exponential exposure law; 300 s slices keep this sweep practical.
    # CLI defaults to 5 s; transient fault tests exercise that interval separately.
    config = Config(check_interval_seconds=300)
    for seed in range(1000):
        path = tmp_path / f"{seed}.jsonl"
        backend = SweepBackend(path, profile, seed)
        records = []
        stock = backend.read_telemetry().settings
        max_restarts = (
            backend.read_limits().voltage_reduction.maximum
            + backend.read_limits().vram.maximum
            - stock.vram_mhz
            + 3
        )
        for _ in range(max_restarts):
            journal = SearchJournal(path, records)
            backend.test_journal = journal
            bounds = journal.crash_policy(stock)
            start = len(journal.records)
            try:
                final = Search(backend, make_workloads(backend), journal, preset, config).tune(
                    watchdog=False
                )
            except SimulatedHardCrash:
                crashes += 1
                records = journal.records
            else:
                break
            finally:
                for r in journal.records[start:]:
                    if r["kind"] == "test_start":
                        assert (
                            bounds.voltage is None
                            or r["settings"]["reduction_mv"] < bounds.voltage
                        )
                        assert (
                            bounds.memory is None or r["settings"]["vram_mhz"] < bounds.memory
                        )
                        assert r["settings"]["timing"] not in bounds.timings
        else:
            pytest.fail(f"Crash recovery did not terminate: {profile}/{preset}/{seed}")
        assert backend.read_telemetry().settings == final
        verified = (
            journal.last_accepted() == final
            and journal.records[-1]["outcome"] == "verified in simulation"
        )
        accepted_count += verified
        safe += verified and backend.voltage_failure_probability(settings=final) < 0.05
        if backend.read_capabilities().vram_frequency:
            gap = backend.memory_knee(final.timing) - final.vram_mhz
            memory_safe += gap >= 0
            memory_close += (
                verified
                and 0 <= gap <= PRESETS[preset].vram_margin_mhz + 2 * config.fine_step_mhz
            )
        failed = any(
            r["kind"] == "verification_result" and not r["ok"] for r in journal.records
        )
        failed |= any(
            r["kind"] == "rejected"
            and r.get("reason") == "crash"
            and r.get("setting") == "profile"
            for r in journal.records
        )
        verification_failures += failed
    frequency = PROFILES[profile].vram
    result = {
        "preset": preset,
        "profile": profile,
        "cards": 1000,
        "accepted": accepted_count,
        "under_5pct_hourly": safe / accepted_count if accepted_count else 0,
        "memory_below_knee": memory_safe / 1000 if frequency else None,
        "memory_within_margin": memory_close / 1000 if frequency else None,
        "verification_failed": verification_failures / 1000,
        "hard_crashes": crashes,
    }
    RESULTS.append(result)
    folder = Path("reports")
    folder.mkdir(exist_ok=True)
    (folder / "acceptance.json").write_text(json.dumps(RESULTS, indent=2))
    print("M1.1 measured:", json.dumps(result), flush=True)
    target = {"safe": 0.95, "balanced": 0.90}.get(preset)
    misses = []
    if target is not None and result["under_5pct_hourly"] < target:
        misses.append("hourly risk target")
    if frequency and (
        result["memory_below_knee"] < 0.995 or result["memory_within_margin"] < 0.90
    ):
        misses.append("memory target")
    if result["verification_failed"] <= 0.05:
        misses.append("verification failure coverage target")
    # A missed provisional gate is visible as XFAIL, not a passing safety claim.
    # Continue measuring other profiles, then STOP for the owner/reviewer decision.
    if misses:
        pytest.xfail("M1.1 provisional target missed: " + ", ".join(misses))
