import json
import math
import statistics
from dataclasses import asdict, replace

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
        self.last_failure = None
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
        if (
            not math.isfinite(self.config.check_interval_seconds)
            or self.config.check_interval_seconds <= 0
        ):
            raise ValueError("Supervision interval must be positive and finite")
        if (
            not math.isfinite(self.config.refine_seconds)
            or self.config.refine_seconds <= 0
            or any(
                not math.isfinite(s) or s <= 0 for s in self.config.verification_ramp_seconds
            )
        ):
            raise ValueError("Refinement and ramp durations must be positive and finite")

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
        self.last_failure = None
        self.supervisor.apply(settings)
        for name in names:
            ok, _ = self.supervisor.test(
                self.workloads[name], seconds, self.baselines[name], risk
            )
            if not ok:
                self.last_failure = name
                self.journal.append(
                    "rejected", reason="test", workload=name, settings=asdict(settings)
                )
                return False
        self.journal.append("passed", settings=asdict(settings), risk=risk, seconds=seconds)
        return True

    def bracket(self, start, maximum, coarse, fine, bounds, evaluate, refine=None, failed=None):
        good, bad = start, failed
        coarse = max(bounds.step, math.ceil(coarse / bounds.step) * bounds.step)
        fine = max(bounds.step, math.ceil(fine / bounds.step) * bounds.step)
        while bad is None and good < maximum:
            candidate = min(maximum, good + coarse)
            if evaluate(candidate):
                good = candidate
            else:
                bad = candidate
                break
        while bad is not None and bad - good > fine and good < maximum:
            middle = min(maximum, grid_floor((good + bad) // 2, bounds))
            if middle <= good:
                break
            if (refine or evaluate)(middle):
                good = middle
            else:
                bad = middle
        return good

    def _search(self):
        bounds = self.journal.crash_policy(self.stock)
        voltage_crash = bounds.voltage
        if bounds.stock_crashed:
            return self.stock, "crash at stock settings"
        if not self.baseline():
            return self.stock, "stock: baseline failed"
        voltage = 0
        if self.caps.voltage_kind != VoltageKind.NONE:
            maximum = self.limits.voltage_reduction.maximum
            if voltage_crash is not None:
                maximum = min(maximum, voltage_crash - self.preset.voltage_margin_mv)
            maximum = grid_floor(maximum, self.limits.voltage_reduction)
            start = 0
            if voltage_crash is not None:
                start = self.journal.passed_below("voltage", maximum) or 0
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
                refine=lambda v: self.trial(
                    Settings(v, self.stock.vram_mhz),
                    ["core", "burst"],
                    self.config.refine_seconds,
                    "voltage",
                ),
                failed=voltage_crash,
            )
        # Verify voltage independently: a core failure must not spend the memory margin.
        candidate = Settings(voltage, self.stock.vram_mhz)
        if self.caps.voltage_kind != VoltageKind.NONE:
            for attempt in range(self.config.max_backoffs + 1):
                if self.verify(candidate, ["core", "burst", "combined"], attempt, "voltage"):
                    candidate = self.backoff(candidate, "voltage")
                    break
                candidate = self.backoff(candidate, "voltage")
            else:
                return self.stock, "stock: voltage verification backoffs exhausted"
        candidate = self.memory_pair(candidate.reduction_mv, bounds)
        names = ["burst"]
        if self.caps.vram_frequency or self.caps.vram_timing:
            names.append("vram_verify")
        names.append("combined")
        for attempt in range(self.config.max_backoffs + 1):
            if self.verify(candidate, names, attempt, "final"):
                return candidate, "verified in simulation"
            # Bursty/core failures identify voltage. Memory-verifier failures identify
            # memory; combined failures are ambiguous, so reduce memory first.
            knob = "voltage" if self.last_failure in ("core", "burst") else "vram"
            next_candidate = self.backoff(candidate, knob)
            if next_candidate == candidate:
                next_candidate = self.backoff(candidate, "voltage")
            candidate = next_candidate
        return self.stock, "stock: verification backoffs exhausted"

    def backoff(self, settings, setting):
        if setting == "voltage":
            return replace(
                settings,
                reduction_mv=grid_floor(
                    max(
                        0,
                        settings.reduction_mv
                        - max(
                            self.preset.voltage_margin_mv, self.limits.voltage_reduction.step
                        ),
                    ),
                    self.limits.voltage_reduction,
                ),
            )
        if setting != "vram":
            raise ValueError("Unknown backoff setting")
        frequency = (
            grid_floor(
                max(
                    self.stock.vram_mhz,
                    settings.vram_mhz - max(self.preset.vram_margin_mhz, self.limits.vram.step),
                ),
                self.limits.vram,
            )
            if self.caps.vram_frequency
            else settings.vram_mhz
        )
        timing = Timing.DEFAULT if frequency == settings.vram_mhz else settings.timing
        return replace(settings, vram_mhz=frequency, timing=timing)

    def verify(self, settings, names, attempt, phase):
        # Staging rejects an eight-second edge before spending a full burn-in on it.
        # The successful full run still exercises EVERY workload for its full duration.
        probe = "burst" if phase == "voltage" else "combined"
        for seconds in self.config.verification_ramp_seconds:
            record = self.journal.append(
                "verification",
                attempt=attempt,
                phase=phase,
                stage="ramp",
                settings=asdict(settings),
                workloads=[probe],
                seconds_per_workload=seconds,
            )
            ok = self.trial(settings, [probe], min(seconds, self.preset.verification_seconds))
            self.journal.append("verification_result", verification_seq=record["seq"], ok=ok)
            if not ok:
                return False
        record = self.journal.append(
            "verification",
            attempt=attempt,
            phase=phase,
            stage="full",
            settings=asdict(settings),
            workloads=names,
            seconds_per_workload=self.preset.verification_seconds,
        )
        ok = self.trial(settings, names, self.preset.verification_seconds)
        self.journal.append("verification_result", verification_seq=record["seq"], ok=ok)
        if ok:
            self.verification_seq = record["seq"]
        return ok

    def memory_pair(self, voltage, bounds):
        if not (self.caps.vram_frequency or self.caps.vram_timing):
            return Settings(voltage, self.stock.vram_mhz)
        timings = self.limits.timings if self.caps.vram_timing else (Timing.DEFAULT,)
        best, best_score = Settings(voltage, self.stock.vram_mhz), -math.inf
        for timing in timings:
            if timing in bounds.timings:
                continue
            maximum = (
                self.limits.vram.maximum if self.caps.vram_frequency else self.stock.vram_mhz
            )
            if bounds.memory is not None:
                maximum = min(maximum, bounds.memory - self.preset.vram_margin_mhz)
            maximum = grid_floor(maximum, self.limits.vram)

            def evaluate(mhz, timing=timing, seconds=self.config.short_seconds):
                return self.trial(
                    Settings(voltage, mhz, timing),
                    ["vram_bandwidth", "vram_verify", "burst"],
                    seconds,
                    "vram",
                )

            if not evaluate(self.stock.vram_mhz):
                continue
            start = self.stock.vram_mhz
            if bounds.memory is not None:
                start = self.journal.passed_below("vram", maximum, timing) or start
            edge = self.bracket(
                start,
                maximum,
                self.config.coarse_step_mhz,
                self.config.fine_step_mhz,
                self.limits.vram,
                evaluate,
                refine=lambda m, evaluate=evaluate: evaluate(
                    m, seconds=self.config.refine_seconds
                ),
                failed=bounds.memory,
            )
            # Search each timing separately, THEN margin its frequency/timing pair.
            memory = grid_floor(
                max(self.stock.vram_mhz, edge - self.preset.vram_margin_mhz), self.limits.vram
            )
            if evaluate(memory):
                score = next(
                    r["score"]
                    for r in reversed(self.journal.records)
                    if r["kind"] == "test_result" and r["workload"] == "vram_bandwidth"
                )
                if score > best_score:
                    best, best_score = Settings(voltage, memory, timing), score
        return best

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
            "run_start",
            schema=2,
            identity=identity,
            preset=self.preset_name,
            config=asdict(self.config),
        )
        if watchdog:
            self.supervisor.start_watchdog()
        original_error = None
        try:
            self.supervisor.reset()
            candidate, outcome = self._search()
            if candidate == self.stock:
                self.supervisor.reset()
            else:
                self.supervisor.apply(candidate)
            if outcome == "verified in simulation":
                self.journal.accepted(candidate, self.verification_seq)
            self.journal.append("run_end", outcome=outcome, settings=asdict(candidate))
            return candidate
        except (Exception, KeyboardInterrupt) as exc:
            original_error = exc
            self.journal.append(
                "rejected", reason="operation_error", error=type(exc).__name__, detail=str(exc)
            )
            try:
                self.supervisor.reset()
                self.journal.append("run_end", outcome="error: restored stock")
            except Exception as cleanup:
                exc.add_note(f"Stock-reset cleanup also failed: {cleanup}")
            raise
        finally:
            if watchdog:
                try:
                    self.supervisor.stop_watchdog()
                except Exception as cleanup:
                    if original_error is None:
                        raise
                    original_error.add_note(f"Watchdog cleanup also failed: {cleanup}")
