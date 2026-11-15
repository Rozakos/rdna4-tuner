from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BackendError(RuntimeError):
    """A driver or simulator operation failed; not evidence of a tuning limit."""


class OutOfRange(BackendError):
    pass


class Rejected(BackendError):
    pass


class ReadbackMismatch(BackendError):
    pass


class Unavailable(BackendError):
    pass


class PermissionDenied(BackendError):
    pass


class SafetyLimitExceeded(BackendError):
    pass


class JournalBusy(RuntimeError):
    pass


class JournalCorrupt(RuntimeError):
    pass


class Timing(StrEnum):
    DEFAULT = "default"
    FAST = "fast"


class VoltageKind(StrEnum):
    OFFSET = "offset"
    ABSOLUTE = "absolute"
    NONE = "none"


@dataclass(frozen=True)
class Range:
    minimum: int
    maximum: int
    step: int

    def contains(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum and (value - self.minimum) % self.step == 0


@dataclass(frozen=True)
class Capabilities:
    voltage_kind: VoltageKind
    stock_voltage_mv: int | None
    clock_limits: bool
    vram_frequency: bool
    vram_timing: bool
    family: str
    name: str


@dataclass(frozen=True)
class Limits:
    voltage_reduction: Range
    vram: Range
    stock_vram_mhz: int
    timings: tuple[Timing, ...]
    max_temperature_c: float
    max_power_w: float


@dataclass(frozen=True)
class Settings:
    reduction_mv: int
    vram_mhz: int
    timing: Timing = Timing.DEFAULT

    @classmethod
    def from_dict(cls, value: dict) -> "Settings":
        return cls(value["reduction_mv"], value["vram_mhz"], Timing(value["timing"]))


@dataclass(frozen=True)
class Telemetry:
    settings: Settings
    voltage_mv: int | None
    clock_mhz: int
    power_w: float
    temperature_c: float


@dataclass(frozen=True)
class CrashEvent:
    timestamp: datetime
    detail: str


@dataclass(frozen=True)
class WorkloadResult:
    score: float
    noise: float
    ok: bool
    detail: str = ""
