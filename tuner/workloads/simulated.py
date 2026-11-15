from dataclasses import dataclass

from tuner.backends.simulated import SimulatedBackend


@dataclass
class SimulatedWorkload:
    backend: SimulatedBackend
    name: str

    def run(self, seconds: float):
        if seconds <= 0:
            raise ValueError("Workload duration must be positive")
        return self.backend.exercise(self.name, seconds)


def make_workloads(backend):
    return {
        name: SimulatedWorkload(backend, name)
        for name in ("core", "burst", "vram_bandwidth", "vram_verify", "combined")
    }
