from dataclasses import asdict
from pathlib import Path

from .core.config import PRESETS
from .core.journal import read_records


def report(path: Path):
    records = read_records(path)
    start = max((i for i, r in enumerate(records) if r["kind"] == "run_start"), default=0)
    current = records[start:]
    last = next((r for r in reversed(current) if r["kind"] == "run_end"), None)
    trials = [r for r in current if r["kind"] == "test_result"]
    caps = current[0].get("identity", {}).get("capabilities", {}) if current else {}
    skipped = []
    if caps.get("voltage_kind") == "none":
        skipped.append("voltage")
    if caps and not caps.get("vram_frequency"):
        skipped.append("VRAM frequency")
    if caps and not caps.get("vram_timing"):
        skipped.append("memory timing")
    return {
        "hardware_tested": False,
        "time_basis": "synthetic workload seconds; no real GPU load or wall-clock burn-in",
        "outcome": last.get("outcome") if last else "interrupted or not started",
        "accepted": next(
            (
                r["settings"]
                for r in reversed(current)
                if r["kind"] == "accepted" and r.get("verified")
            ),
            None,
        ),
        "end_settings": last.get("settings") if last else None,
        "preset": current[0].get("preset") if current else None,
        "identity": current[0].get("identity") if current else None,
        "workloads": [
            {
                **{k: r[k] for k in ("workload", "seconds", "settings", "ok", "reasons")},
                "requested_seconds": r.get("requested_seconds", r["seconds"]),
                "slices": r.get("slices", 1),
            }
            for r in trials
        ],
        "failures": [
            r
            for r in records
            if r["kind"] == "rejected" or (r["kind"] == "test_result" and not r["ok"])
        ],
        "margins": asdict(PRESETS[current[0]["preset"]]) if current else None,
        "skipped": skipped,
        "timing": "fast timing trialled only when the backend reports support",
        "synthetic_seconds_tested": sum(r["seconds"] for r in trials),
        "verification": [
            r for r in current if r["kind"] in ("verification", "verification_result")
        ],
    }
