import json
import math
import statistics
from dataclasses import asdict

from .config import DEFAULT_CONFIG, PRESETS
from .supervisor import Supervisor
from .types import Settings, Timing, VoltageKind


def grid_floor(value, bounds):
    return max(
        bounds.minimum,
        bounds.minimum + (int(value) - bounds.minimum) // bounds.step * bounds.step,
    )


class Search:
    def __init__(self, backend, workloads, journal, preset="balanced", config=DEFAULT_CONFIG):
        self.backend, self.workloads, self.journal = backend, workloads, journal
        self.preset_name, self.preset, self.config = preset, PRESETS[preset], config
        self.supervisor = Supervisor(backend, journal, config)
        self.limits, self.caps = self.supervisor.limits, self.supervisor.capabilities
        self.stock = Settings(0, self.limits.stock_vram_mhz)
        self.baselines = {}
        self._validate()

    def _validate(self):
        if not all(
            math.isfinite(v) and v > 0
            for v in [self.limits.max_temperature_c, self.limits.max_power_w]
        ):
            raise ValueError("Invalid backend safety limits")
        for bounds in [self.limits.voltage_reduction, self.limits.vram]:
            if bounds.step <= 0 or bounds.minimum > bounds.maximum:
                raise ValueError("Invalid backend range")
        if not self.limits.voltage_reduction.contains(0):
            raise ValueError("Voltage reduction range must include stock")
        if not self.limits.vram.contains(self.stock.vram_mhz):
            raise ValueError("Memory range must include stock")
        if self.config.baseline_runs < 2 or self.config.short_seconds <= 0:
            raise ValueError("Baseline needs repeated positive-duration measurements")
        if (
            min(
                self.config.coarse_step_mv,
                self.config.fine_step_mv,
                self.config.coarse_step_mhz,
                self.config.fine_step_mhz,
            )
            <= 0
        ):
            raise ValueError("Search steps must be positive")
        if self.config.max_backoffs < 0:
            raise ValueError("Backoff count cannot be negative")

    def baseline(self):
        for name, workload in self.workloads.items():
            if name.startswith("vram") and not (
                self.caps.vram_frequency or self.caps.vram_timing
            ):
                continue
            scores, noise = [], []
            for _ in range(self.config.baseline_runs):
                ok, result = self.supervisor.test(workload, self.config.short_seconds)
                if not ok:
                    return False
                scores.append(result.score)
                noise.append(result.noise)
            mean = statistics.mean(scores)
            self.baselines[name] = {
                "score": mean,
                "noise": max(
                    statistics.stdev(scores),
                    statistics.mean(noise),
                    mean * self.config.relative_noise_floor,
                ),
            }
        self.journal.append("baseline", workloads=self.baselines)
        return True

    def trial(self, settings, names, seconds, risk="profile"):
        self.supervisor.apply(settings)
        for name in names:
            ok, _ = self.supervisor.test(
                self.workloads[name], seconds, self.baselines[name], risk
            )
            if not ok:
                self.journal.append("rejected", reason="test", settings=asdict(settings))
                return False
        self.journal.accepted(settings)
        return True

    def bracket(self, start, maximum, coarse, fine, bounds, evaluate):
        good, bad = start, None
        coarse = max(bounds.step, math.ceil(coarse / bounds.step) * bounds.step)
        fine = max(bounds.step, math.ceil(fine / bounds.step) * bounds.step)
        while good < maximum:
            candidate = min(maximum, good + coarse)
            if evaluate(candidate):
                good = candidate
            else:
                bad = candidate
                break
        while bad is not None and bad - good > fine:
            middle = grid_floor((good + bad) // 2, bounds)
            if middle <= good:
                break
            if evaluate(middle):
                good = middle
            else:
                bad = middle
        return good

    def _search(self):
        voltage_crash, memory_crash = self.journal.crash_bounds()
        if voltage_crash == 0 or (
            memory_crash is not None and memory_crash <= self.stock.vram_mhz
        ):
            return self.stock, "stock: previous stock test crashed"
        if not self.baseline():
            return self.stock, "stock: baseline failed"
        recovered = (
            self.journal.last_accepted()
            if (voltage_crash is not None or memory_crash is not None)
            else None
        )
        voltage = 0
        if self.caps.voltage_kind != VoltageKind.NONE:
            maximum = self.limits.voltage_reduction.maximum
            if voltage_crash is not None:
                maximum = min(maximum, voltage_crash - 1)
            maximum = grid_floor(maximum, self.limits.voltage_reduction)
            start = 0
            if recovered:
                last = grid_floor(
                    min(maximum, recovered.reduction_mv), self.limits.voltage_reduction
                )
                if self.trial(
                    Settings(last, self.stock.vram_mhz),
                    ["core", "burst"],
                    self.config.short_seconds,
                    "voltage",
                ):
                    start = last
            voltage = self.bracket(
                start,
                maximum,
                self.config.coarse_step_mv,
                self.config.fine_step_mv,
                self.limits.voltage_reduction,
                lambda v: self.trial(
                    Settings(v, self.stock.vram_mhz),
                    ["core", "burst"],
                    self.config.short_seconds,
                    "voltage",
                ),
            )
            voltage = grid_floor(
                max(0, voltage - self.preset.voltage_margin_mv), self.limits.voltage_reduction
            )
        memory = self.stock.vram_mhz
        self.supervisor.apply(Settings(voltage, memory))
        if self.caps.vram_frequency:
            maximum = self.limits.vram.maximum
            if memory_crash is not None:
                maximum = min(maximum, memory_crash - 1)
            maximum = grid_floor(maximum, self.limits.vram)
            start = memory
            if recovered:
                last = grid_floor(min(maximum, recovered.vram_mhz), self.limits.vram)
                if self.trial(
                    Settings(voltage, last),
                    ["vram_bandwidth", "vram_verify"],
                    self.config.short_seconds,
                    "vram",
                ):
                    start = last
            memory = self.bracket(
                start,
                maximum,
                self.config.coarse_step_mhz,
                self.config.fine_step_mhz,
                self.limits.vram,
                lambda m: self.trial(
                    Settings(voltage, m),
                    ["vram_bandwidth", "vram_verify", "burst"],
                    self.config.short_seconds,
                    "vram",
                ),
            )
            memory = grid_floor(
                max(self.stock.vram_mhz, memory - self.preset.vram_margin_mhz), self.limits.vram
            )
        candidate = Settings(voltage, memory)
        if self.caps.vram_timing and Timing.FAST in self.limits.timings:
            fast = Settings(voltage, memory, Timing.FAST)
            if self.trial(
                fast, ["vram_bandwidth", "vram_verify"], self.config.short_seconds, "vram"
            ):
                candidate = fast
        names = ["combined", "burst"]
        if self.caps.vram_frequency or self.caps.vram_timing:
            names.append("vram_verify")
        for attempt in range(self.config.max_backoffs + 1):
            self.journal.append(
                "verification",
                attempt=attempt,
                settings=asdict(candidate),
                seconds=self.preset.verification_seconds,
            )
            if self.trial(candidate, names, self.preset.verification_seconds / len(names)):
                return candidate, "verified in simulation"
            candidate = Settings(
                grid_floor(
                    max(0, candidate.reduction_mv - self.config.fine_step_mv),
                    self.limits.voltage_reduction,
                ),
                grid_floor(
                    max(self.stock.vram_mhz, candidate.vram_mhz - self.config.fine_step_mhz),
                    self.limits.vram,
                ),
                candidate.timing,
            )
        return self.stock, "stock: verification backoffs exhausted"

    def tune(self, watchdog=True):
        identity = json.loads(
            json.dumps({"capabilities": asdict(self.caps), "limits": asdict(self.limits)})
        )
        previous = next((r for r in self.journal.records if r["kind"] == "run_start"), None)
        if previous and previous["identity"] != identity:
            raise ValueError(
                "Journal belongs to a different device/profile; choose another path"
            )
        self.journal.append(
            "run_start", identity=identity, preset=self.preset_name, config=asdict(self.config)
        )
        if watchdog:
            self.supervisor.start_watchdog()
        try:
            self.supervisor.reset()
            candidate, outcome = self._search()
            if candidate == self.stock:
                self.supervisor.reset()
            else:
                self.supervisor.apply(candidate)
            self.journal.accepted(candidate)
            self.journal.append("run_end", outcome=outcome, settings=asdict(candidate))
            return candidate
        except BaseException as exc:
            self.journal.append(
                "rejected", reason="operation_error", error=type(exc).__name__, detail=str(exc)
            )
            self.supervisor.reset()
            self.journal.append("run_end", outcome="error: restored stock")
            raise
        finally:
            if watchdog:
                self.supervisor.stop_watchdog()
