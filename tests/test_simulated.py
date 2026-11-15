import pytest

from tuner.backends.simulated import PROFILES, SimulatedBackend, SimulatedHardCrash
from tuner.core.journal import Journal
from tuner.core.types import OutOfRange, Rejected, Timing, VoltageKind
from tuner.workloads.simulated import make_workloads


@pytest.mark.parametrize("profile", PROFILES)
def test_capabilities_readback_limits_and_seeded_workload(tmp_path, profile):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        backend = SimulatedBackend(path, profile, seed=4)
        clone = SimulatedBackend(path, profile, seed=4)
        assert backend.exercise("core", 8) == clone.exercise("core", 8)
        journal.append("intent", setting="voltage", value=50)
        caps = backend.read_capabilities()
        if caps.voltage_kind == VoltageKind.NONE:
            with pytest.raises(Rejected):
                backend.set_voltage(50)
        else:
            backend.set_voltage(50)
            assert backend.read_telemetry().settings.reduction_mv == 50
        if caps.voltage_kind == VoltageKind.ABSOLUTE:
            assert backend.read_telemetry().voltage_mv == caps.stock_voltage_mv - 50
        journal.append("intent", setting="vram", value={"mhz": 2200, "timing": "default"})
        if not caps.vram_frequency:
            with pytest.raises(Rejected):
                backend.set_vram(2200, Timing.DEFAULT)
        journal.append("intent", setting="voltage", value=999)
        with pytest.raises(OutOfRange):
            backend.set_voltage(999)


def test_setter_requires_a_fresh_durable_intent(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        backend = SimulatedBackend(path)
        journal.append("run_start")
        with pytest.raises(AssertionError):
            backend.set_voltage(30)
        journal.append("intent", setting="voltage", value=30)
        backend.set_voltage(30)
        with pytest.raises(AssertionError):
            backend.set_voltage(30)


def test_bursts_heat_and_memory_errors_are_modelled(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        backend = SimulatedBackend(path, seed=19)
        assert backend.true_voltage_limit(1800, "burst") < backend.true_voltage_limit(8, "core")
        memory = backend.memory_edge + 40
        journal.append("intent", setting="vram", value={"mhz": memory, "timing": "default"})
        backend.set_vram(memory, Timing.DEFAULT)
        with pytest.raises(SimulatedHardCrash):
            make_workloads(backend)["vram_verify"].run(30)


@pytest.mark.parametrize("fault", ["rejected", "unavailable", "readback", "driver_reset"])
def test_injected_faults_are_observable(tmp_path, fault):
    from tuner.core.types import BackendError

    path = tmp_path / "journal.jsonl"
    with Journal(path) as journal:
        backend = SimulatedBackend(path, fault=fault)
        journal.append("intent", setting="voltage", value=25)
        if fault in ("rejected", "unavailable"):
            with pytest.raises(BackendError):
                backend.set_voltage(25)
        else:
            backend.set_voltage(25)
            if fault == "readback":
                assert backend.read_telemetry().settings.reduction_mv != 25
            else:
                backend.exercise("core", 8)
                assert len(backend.events) == 1
