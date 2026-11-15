from typing import Protocol

from tuner.core.types import WorkloadResult


class Workload(Protocol):
    name: str

    def run(self, seconds: float) -> WorkloadResult: ...
