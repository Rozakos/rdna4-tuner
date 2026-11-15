from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    voltage_margin_mv: int
    vram_margin_mhz: int
    verification_seconds: float


# Preset values are the owner's specified margins and mixed verification durations.
PRESETS = {
    "safe": Preset(15, 25, 30 * 60),
    "balanced": Preset(10, 15, 30 * 60),
    "edge": Preset(5, 10, 10 * 60),
}


@dataclass(frozen=True)
class Config:
    baseline_runs: int = 5  # Repeated measurements estimate baseline variation.
    short_seconds: float = 8  # Edge discovery only; never a stability claim.
    coarse_step_mv: int = 20  # Bracket the voltage edge without excessive trials.
    fine_step_mv: int = 2  # Requested search resolution, rounded to driver steps.
    coarse_step_mhz: int = 40  # Bracket the memory edge.
    fine_step_mhz: int = 5  # Memory search resolution, rounded to driver steps.
    k_sigma: float = 4  # Reject score loss relative to baseline noise.
    noise_multiplier: float = 4  # Catch rising memory noise even while bandwidth rises.
    relative_noise_floor: float = 0.002  # Avoid zero-noise baselines implying exact scores.
    max_backoffs: int = 8  # Bound failed full-verification attempts.
    watchdog_join_seconds: float = 10  # Bound normal shutdown of the watchdog.
