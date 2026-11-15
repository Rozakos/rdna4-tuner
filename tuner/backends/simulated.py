import json
import math
import multiprocessing as mp
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tuner.core.types import (
    Capabilities,
    CrashEvent,
    Limits,
    OutOfRange,
    Range,
    Rejected,
    Settings,
    Telemetry,
    Timing,
    Unavailable,
    VoltageKind,
    WorkloadResult,
)


@dataclass(frozen=True)
class Profile:
    kind: VoltageKind
    voltage_mv: int | None
    vram: bool
    clocks: bool
    stock_memory: int
    voltage_step: int = 1


PROFILES = {
    "rdna4_offset": Profile(VoltageKind.OFFSET, None, True, False, 2500),
    "rdna2_absolute": Profile(VoltageKind.ABSOLUTE, 1200, True, True, 2000),
    "rdna3_absolute": Profile(VoltageKind.ABSOLUTE, 1150, True, True, 2400),
    "no_vram_tuning": Profile(VoltageKind.OFFSET, None, False, False, 2100),
    "no_voltage_tuning": Profile(VoltageKind.NONE, None, True, False, 2100),
    "voltage_step_5mv": Profile(VoltageKind.OFFSET, None, True, False, 2500, 5),
}


class SimulatedHardCrash(BaseException):
    """The statistical harness must reopen/recover the journal, not treat this as a result."""


class SimulatedBackend:
    """A deterministic synthetic card; shared settings allow a real child watchdog test."""

    def __init__(
        self,
        journal_path: Path,
        profile="rdna4_offset",
        seed=0,
        fault=None,
        fault_at=1,
        hard_crash_mode="raise",
    ):
        self.journal_path = Path(journal_path)
        self.profile_name, self.profile = profile, PROFILES[profile]
        self.seed, self.rng = seed, random.Random(seed)
        self.fault, self.fault_at = fault, fault_at
        self.hard_crash_mode = hard_crash_mode
        self.runs = 0
        self.events = []
        self.temperature = 45.0
        self.power_fault = False
        self.voltage_edge = self.rng.randrange(85, 151)
        self.memory_edge = self.profile.stock_memory + self.rng.randrange(90, 181)
        # Frozen M1.1 model: independent per-card transition widths, 2–5 in each unit.
        self.width_mv = self.rng.uniform(2, 5)
        self.memory_width_mhz = self.rng.uniform(2, 5)
        self.elapsed_seconds = 0.0
        ctx = mp.get_context("spawn")
        self.state = ctx.Array("i", [0, self.profile.stock_memory, 0, 0], lock=True)

    def read_capabilities(self):
        p = self.profile
        return Capabilities(
            p.kind,
            p.voltage_mv,
            p.clocks,
            p.vram,
            p.vram,
            self.profile_name,
            f"Simulated {self.profile_name} seed {self.seed}",
        )

    def read_limits(self):
        return Limits(
            Range(0, 200, self.profile.voltage_step),
            Range(self.profile.stock_memory, self.profile.stock_memory + 300, 1),
            self.profile.stock_memory,
            (Timing.DEFAULT, Timing.FAST),
            95.0,
            350.0,
        )

    def read_telemetry(self):
        reduction, memory, timing, _ = self.state[:]
        settings = Settings(reduction, memory, Timing.FAST if timing else Timing.DEFAULT)
        voltage = (
            None if self.profile.voltage_mv is None else self.profile.voltage_mv - reduction
        )
        power = (
            self.read_limits().max_power_w + 1 if self.power_fault else 220 - reduction * 0.2
        )
        return Telemetry(settings, voltage, 2400, power, self.temperature)

    def _guard(self, setting, value):
        # Read only the durable final record, rather than repeatedly parsing the whole log.
        with self.journal_path.open("rb") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            stream.seek(max(0, end - 8192))
            last = json.loads(stream.read().splitlines()[-1])
        assert last["kind"] == "intent" and last["setting"] == setting
        assert last["value"] == value and last["seq"] > self.state[3]
        self.state[3] = last["seq"]

    def set_voltage(self, reduction_mv):
        self._guard("voltage", reduction_mv)
        if not self.read_limits().voltage_reduction.contains(reduction_mv):
            raise OutOfRange("Voltage reduction outside reported range")
        if self.profile.kind == VoltageKind.NONE and reduction_mv:
            raise Rejected("Voltage tuning unsupported")
        if self.fault == "rejected" and reduction_mv:
            raise Rejected("Injected setter rejection")
        if self.fault == "unavailable" and reduction_mv:
            raise Unavailable("Injected driver loss")
        self.state[0] = reduction_mv + (1 if self.fault == "readback" and reduction_mv else 0)

    def set_vram(self, mhz, timing):
        self._guard("vram", {"mhz": mhz, "timing": timing})
        limits = self.read_limits()
        if not self.profile.vram:
            raise Rejected("VRAM tuning unsupported")
        if not limits.vram.contains(mhz) or timing not in limits.timings:
            raise OutOfRange("Memory setting outside reported capabilities")
        self.state[1], self.state[2] = mhz, int(timing == Timing.FAST)

    def reset(self):
        self._guard("reset", "stock")
        self.state[0], self.state[1], self.state[2] = 0, self.profile.stock_memory, 0

    def crash_events_since(self, t):
        return [event for event in self.events if event.timestamp >= t]

    def true_voltage_limit(self, seconds, load="burst"):
        """Median failure point, not a safe limit; retained for older diagnostic callers."""
        return self.load_edge(load) + self.width_mv * math.log(1800 / seconds)

    def load_edge(self, load):
        return self.voltage_edge - (6 if load in ("burst", "combined") else 0)

    @staticmethod
    def failure_rate(value, edge, width):
        return math.log(2) / 1800 * math.exp((value - edge) / width)

    def voltage_failure_probability(self, seconds=3600, load="burst", settings=None):
        if self.profile.kind == VoltageKind.NONE:
            return 0.0
        settings = settings or self.read_telemetry().settings
        rate = self.failure_rate(settings.reduction_mv, self.load_edge(load), self.width_mv)
        return -math.expm1(-rate * seconds)

    def memory_knee(self, timing):
        return self.memory_edge - (15 if timing == Timing.FAST else 0)

    def _hard_crash(self):
        if self.hard_crash_mode == "process":
            os._exit(86)
        raise SimulatedHardCrash("Synthetic process death beyond edge + 3 widths")

    def exercise(self, name, seconds):
        self.runs += 1
        self.elapsed_seconds += seconds
        if self.runs == self.fault_at:
            if self.fault == "hard_crash":
                os._exit(86)
            if self.fault == "driver_reset":
                self.state[0], self.state[1], self.state[2] = 0, self.profile.stock_memory, 0
                self.events.append(CrashEvent(datetime.now(UTC), "Injected driver reset"))
            if self.fault == "thermal":
                self.temperature = self.read_limits().max_temperature_c + 1
            if self.fault == "power":
                self.power_fault = True
        settings = self.read_telemetry().settings
        if self.fault != "thermal":
            self.temperature = (
                55 + min(25, self.elapsed_seconds / 60) + self.rng.uniform(-0.5, 0.5)
            )
        edge = self.load_edge(name)
        chance = self.voltage_failure_probability(seconds, name, settings)
        voltage_ok = self.rng.random() >= chance
        excess = (
            settings.vram_mhz - self.memory_edge + (15 if settings.timing == Timing.FAST else 0)
        )
        touches_memory = name in ("vram_bandwidth", "vram_verify", "combined")
        # Preserve the original 15 MHz gap between the ECC knee and the data-error edge.
        memory_rate = self.failure_rate(excess, 15, self.memory_width_mhz)
        memory_ok = not touches_memory or self.rng.random() >= -math.expm1(
            -memory_rate * seconds
        )
        if (not voltage_ok and settings.reduction_mv > edge + 3 * self.width_mv) or (
            not memory_ok and excess > 15 + 3 * self.memory_width_mhz
        ):
            self._hard_crash()
        score = 1000.0
        noise = 2.0
        if touches_memory:
            gain = min(
                settings.vram_mhz - self.profile.stock_memory,
                self.memory_edge - self.profile.stock_memory,
            )
            score += gain * 0.4
            noise += max(0, excess) * 1.5
            score -= max(0, excess) * 1.0
        score += self.rng.uniform(-noise, noise)
        return WorkloadResult(
            score,
            noise,
            voltage_ok and memory_ok,
            "" if voltage_ok and memory_ok else "Synthetic instability/data error",
        )
