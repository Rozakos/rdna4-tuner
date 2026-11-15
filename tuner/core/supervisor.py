import math
import multiprocessing as mp
from dataclasses import asdict, replace
from datetime import UTC, datetime

from .config import DEFAULT_CONFIG
from .journal import Journal
from .types import (
    ReadbackMismatch,
    Rejected,
    SafetyLimitExceeded,
    Settings,
    Timing,
    VoltageKind,
    WorkloadResult,
)


def _watch_parent(connection, backend, path, ready, lock_timeout):
    ready.set()
    try:
        message = connection.recv()
        if message == "disarm":
            return
    except EOFError:
        pass
    finally:
        connection.close()
    # Parent's kernel lock is released on death. Opening also records its interrupted trial.
    # EOF can precede the OS releasing the dead parent's journal handle. Wait only
    # here, after EOF; ordinary concurrent journal opens still fail immediately.
    with Journal(path, lock_timeout=lock_timeout) as journal:
        journal.append("intent", setting="reset", value="stock", source="watchdog")
        backend.reset()
        stock = Settings(0, backend.read_limits().stock_vram_mhz)
        if backend.read_telemetry().settings != stock:
            raise ReadbackMismatch("Watchdog stock reset did not read back")
        journal.append("applied", setting="reset", value="stock", source="watchdog")


class Supervisor:
    def __init__(self, backend, journal, config=DEFAULT_CONFIG):
        self.backend, self.journal, self.config = backend, journal, config
        self.capabilities, self.limits = backend.read_capabilities(), backend.read_limits()
        self.watchdog = self.connection = None

    def start_watchdog(self):
        ctx = mp.get_context("spawn")
        receiver, self.connection = ctx.Pipe(duplex=False)
        ready = ctx.Event()
        self.watchdog = ctx.Process(
            target=_watch_parent,
            args=(
                receiver,
                self.backend,
                self.journal.path,
                ready,
                self.config.watchdog_join_seconds,
            ),
            name="simulator-safety-watchdog",
        )
        self.watchdog.start()
        receiver.close()
        if not ready.wait(self.config.watchdog_join_seconds):
            raise RuntimeError("Watchdog failed to start")

    def stop_watchdog(self):
        if self.connection:
            self.connection.send("disarm")
            self.connection.close()
            self.watchdog.join(self.config.watchdog_join_seconds)
            if self.watchdog.is_alive() or self.watchdog.exitcode != 0:
                raise RuntimeError("Watchdog failed to disarm cleanly")
            self.watchdog = self.connection = None

    def _change(self, setting, value, setter, check):
        current = self.backend.read_telemetry().settings
        desired = (
            Settings(0, self.limits.stock_vram_mhz)
            if setting == "reset"
            else replace(current, reduction_mv=value)
            if setting == "voltage"
            else replace(current, vram_mhz=value["mhz"], timing=Timing(value["timing"]))
        )
        self.journal.append("intent", setting=setting, value=value, settings=asdict(desired))
        setter()
        if not check(self.backend.read_telemetry().settings):
            raise ReadbackMismatch(f"{setting} did not read back as requested")
        self.journal.append("applied", setting=setting, value=value)

    def reset(self):
        stock = Settings(0, self.limits.stock_vram_mhz)
        self._change("reset", "stock", self.backend.reset, lambda actual: actual == stock)

    def apply(self, settings):
        caps, limits = self.capabilities, self.limits
        if settings.reduction_mv and caps.voltage_kind == VoltageKind.NONE:
            raise Rejected("Voltage control unsupported")
        if not limits.voltage_reduction.contains(settings.reduction_mv):
            raise Rejected("Voltage not on the reported driver grid")
        if not limits.vram.contains(settings.vram_mhz):
            raise Rejected("VRAM not on the reported driver grid")
        if settings.timing not in limits.timings:
            raise Rejected("Unsupported memory timing")
        if not caps.vram_frequency and settings.vram_mhz != limits.stock_vram_mhz:
            raise Rejected("VRAM frequency tuning unsupported")
        if not caps.vram_timing and settings.timing != Timing.DEFAULT:
            raise Rejected("Memory timing tuning unsupported")
        current = self.backend.read_telemetry().settings
        if current.reduction_mv != settings.reduction_mv:
            self._change(
                "voltage",
                settings.reduction_mv,
                lambda: self.backend.set_voltage(settings.reduction_mv),
                lambda s: s.reduction_mv == settings.reduction_mv,
            )
        current = self.backend.read_telemetry().settings
        if (current.vram_mhz, current.timing) != (settings.vram_mhz, settings.timing):
            self._change(
                "vram",
                {"mhz": settings.vram_mhz, "timing": settings.timing},
                lambda: self.backend.set_vram(settings.vram_mhz, settings.timing),
                lambda s: (s.vram_mhz, s.timing) == (settings.vram_mhz, settings.timing),
            )

    def _reasons(self, result, settings, start, baseline):
        telemetry = self.backend.read_telemetry()
        reasons = []
        if not result.ok:
            reasons.append(result.detail or "incorrect workload result")
        if not all(
            math.isfinite(x)
            for x in (result.score, result.noise, telemetry.temperature_c, telemetry.power_w)
        ):
            reasons.append("non-finite measurement")
        if result.noise < 0 or result.score <= 0:
            reasons.append("invalid score/noise")
        if telemetry.settings != settings:
            reasons.append("settings changed during workload")
        if telemetry.temperature_c > self.limits.max_temperature_c:
            reasons.append("temperature limit")
        if telemetry.power_w > self.limits.max_power_w:
            reasons.append("power limit")
        if self.backend.crash_events_since(start):
            reasons.append("driver crash event")
        if baseline:
            if result.score < baseline["score"] - self.config.k_sigma * baseline["noise"]:
                reasons.append("score below baseline noise allowance")
            if result.noise > baseline["noise"] * self.config.noise_multiplier:
                reasons.append("noise above baseline allowance")
        return reasons

    def test(self, workload, seconds, baseline=None, risk="profile"):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Workload duration must be positive and finite")
        settings = self.backend.read_telemetry().settings
        start = datetime.now(UTC)
        value = (
            settings.reduction_mv
            if risk == "voltage"
            else {"mhz": settings.vram_mhz, "timing": settings.timing}
            if risk == "vram"
            else asdict(settings)
        )
        self.journal.append(
            "test_start",
            setting=risk,
            value=value,
            settings=asdict(settings),
            workload=workload.name,
            seconds=seconds,
        )
        elapsed, weighted_score, noise, slices = 0.0, 0.0, 0.0, 0
        reasons = []
        while elapsed < seconds:
            duration = min(self.config.check_interval_seconds, seconds - elapsed)
            result = workload.run(duration)
            elapsed += duration
            slices += 1
            weighted_score += result.score * duration
            noise = max(noise, result.noise)
            reasons = self._reasons(result, settings, start, baseline)
            if reasons:
                break
        score = weighted_score / elapsed
        self.journal.append(
            "test_result",
            workload=workload.name,
            seconds=elapsed,
            requested_seconds=seconds,
            slices=slices,
            settings=asdict(settings),
            score=score if math.isfinite(score) else None,
            noise=noise if math.isfinite(noise) else None,
            ok=not reasons,
            reasons=reasons,
        )
        unsafe = {
            "non-finite measurement",
            "temperature limit",
            "power limit",
            "settings changed during workload",
        }
        if unsafe.intersection(reasons):
            raise SafetyLimitExceeded("; ".join(reasons))
        return not reasons, WorkloadResult(score, noise, not reasons, "; ".join(reasons))
