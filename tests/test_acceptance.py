from dataclasses import asdict

import pytest

from tuner.backends.simulated import PROFILES, SimulatedBackend
from tuner.core.config import PRESETS, Config
from tuner.core.search import Search
from tuner.workloads.simulated import make_workloads


class SearchJournal:
    """In-memory journal test double for the statistical search sweep only.

    Real fsync ordering, exclusivity, torn writes and killed processes are exercised
    separately. Repeating millions of physical fsyncs would measure CI storage latency.
    """

    def __init__(self, path):
        self.path, self.records = path, []

    def append(self, kind, **data):
        record = {"kind": kind, "seq": len(self.records) + 1, **data}
        self.records.append(record)
        return record

    def accepted(self, settings):
        self.append("accepted", settings=asdict(settings))

    def crash_bounds(self):
        return None, None


class SweepBackend(SimulatedBackend):
    def _guard(self, setting, value):
        record = self.test_journal.records[-1]
        assert record["kind"] == "intent" and record["setting"] == setting
        assert record["value"] == value and record["seq"] > self.state[3]
        self.state[3] = record["seq"]


@pytest.mark.acceptance
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("preset", PRESETS)
def test_1000_seeded_cards_per_profile_and_preset(tmp_path, profile, preset):
    safe = close = 0
    config = Config()
    margin = PRESETS[preset].voltage_margin_mv
    for seed in range(1000):
        path = tmp_path / f"{seed}.jsonl"
        backend = SweepBackend(path, profile, seed)
        journal = SearchJournal(path)
        backend.test_journal = journal
        accepted = Search(backend, make_workloads(backend), journal, preset).tune(
            watchdog=False
        )
        assert backend.read_telemetry().settings == accepted
        limit = backend.true_voltage_limit(PRESETS[preset].verification_seconds)
        safe += accepted.reduction_mv <= limit
        close += 0 <= limit - accepted.reduction_mv <= margin + 2 * config.fine_step_mv
    assert safe >= 995, (profile, preset, "safe", safe)
    assert close >= 900, (profile, preset, "within margin", close)
