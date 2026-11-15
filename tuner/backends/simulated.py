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


PROFILES = {
    "rdna4_offset": Profile(VoltageKind.OFFSET, None, True, False, 2500),
    "rdna2_absolute": Profile(VoltageKind.ABSOLUTE, 1200, True, True, 2000),
    "rdna3_absolute": Profile(VoltageKind.ABSOLUTE, 1150, True, True, 2400),
    "no_vram_tuning": Profile(VoltageKind.OFFSET, None, False, False, 2100),
}


class SimulatedBackend:
    """A deterministic synthetic card; shared settings allow a real child watchdog test."""

    def __init__(
        self, journal_path: Path, profile="rdna4_offset", seed=0, fault=None, fault_at=1
    ):
        self.journal_path = Path(journal_path)
        self.profile_name, self.profile = profile, PROFILES[profile]
        self.seed, self.rng = seed, random.Random(seed)
        self.fault, self.fault_at = fault, fault_at
        self.runs = 0
        self.events = []
        self.temperature = 45.0
        self.power_fault = False
        self.voltage_edge = self.rng.randrange(85, 151)
        self.memory_edge = self.profile.stock_memory + self.rng.randrange(90, 181)
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
            Range(0, 200, 1),
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
        # Bursts reveal a lower edge; sustained heating lowers it by up to two more mV.
        burst = 6 if load in ("burst", "combined") else 0
        heat = min(2, seconds / 300)
        return self.voltage_edge - burst - heat

    def exercise(self, name, seconds):
        self.runs += 1
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
            self.temperature = 55 + min(25, seconds / 60) + self.rng.uniform(-0.5, 0.5)
        edge = self.true_voltage_limit(seconds, name)
        # A smooth probability curve near the edge, deterministic for a given seed.
        chance = 1 / (1 + math.exp(max(-50, min(50, -(settings.reduction_mv - edge) * 2))))
        voltage_ok = self.rng.random() >= chance
        excess = (
            settings.vram_mhz - self.memory_edge + (15 if settings.timing == Timing.FAST else 0)
        )
        memory_ok = excess <= 15
        score = 1000.0
        noise = 2.0
        if name in ("vram_bandwidth", "vram_verify", "combined"):
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
