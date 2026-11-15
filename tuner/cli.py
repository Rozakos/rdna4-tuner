import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .backends.simulated import PROFILES, SimulatedBackend
from .core.config import PRESETS
from .core.journal import Journal
from .core.search import Search
from .report import report
from .workloads.simulated import make_workloads


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simulated tuning only; no GPU driver access")
    parser.add_argument("command", choices=["tune", "status", "report"])
    parser.add_argument("--backend", choices=["simulated"], default="simulated")
    parser.add_argument("--profile", choices=PROFILES, default="rdna4_offset")
    parser.add_argument("--preset", choices=PRESETS, default="balanced")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--journal", type=Path, default=Path("journal/simulated.jsonl"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--fault",
        choices=[
            "hard_crash",
            "driver_reset",
            "rejected",
            "readback",
            "unavailable",
            "thermal",
            "power",
        ],
    )
    parser.add_argument("--fault-at", type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == "report":
        print(json.dumps(report(args.journal), indent=2))
        return 0
    backend = SimulatedBackend(
        args.journal,
        args.profile,
        args.seed,
        args.fault,
        args.fault_at,
        hard_crash_mode="process",
    )
    if args.command == "status" or args.dry_run:
        print(
            json.dumps(
                {
                    "simulated": True,
                    "dry_run": args.dry_run,
                    "capabilities": asdict(backend.read_capabilities()),
                    "limits": asdict(backend.read_limits()),
                    "telemetry": asdict(backend.read_telemetry()),
                    "preset": asdict(PRESETS[args.preset]),
                    "state_scope": "fresh simulated card; saved run is separate",
                    "saved_run": report(args.journal) if args.journal.exists() else None,
                },
                indent=2,
            )
        )
        return 0
    try:
        with Journal(args.journal) as journal:
            Search(backend, make_workloads(backend), journal, args.preset).tune()
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
    result = report(args.journal)
    print(json.dumps(result, indent=2))
    if result["outcome"] == "crash at stock settings":
        parser.exit(
            2,
            "A crash was recorded at true stock settings. Tuning is refused. "
            "Investigate stock stability; to explicitly start over, choose a NEW "
            "--journal path. Keep the old journal for diagnosis.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
