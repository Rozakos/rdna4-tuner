# Changelog

One entry per milestone, at most 15 lines: what changed, what was verified and how, what
could not be verified.

## 2026-11-15 — M1 search and simulator safety
- Added baseline/noise measurement, coarse/bisect voltage and VRAM search, margins and timing trials.
- Added mixed verification with bounded backoff, stock fallback and typed fatal safety errors.
- Added journal-aware readback, subprocess watchdog reset and failed-value crash-resume bounds.
- Added simulated-only tune/status/report CLI and a setter-free, journal-free dry run.
- Reports distinguish synthetic seconds from hardware burn-in and list all failures/skips.
- Verified 12,000 seeded cards (1,000 per preset/profile), meeting 99.5% safety / 90% accuracy gates.
- Statistical tests use a journal double; actual fsync, killed processes and resets tested separately.
- Verified interrupted trials at every recorded setter/test boundary; no crashed value is retried.
- Local suite: 62 tests passed; Windows/Linux Python 3.11/3.13 CI runs the same full suite.
- STOP at gate M1 for Claude review. No real hardware, bridge, workloads or persistence implemented.

## 2026-11-15 — M0 skeleton and simulator
- Added package layout, typed interfaces/errors, configuration and synthetic card profiles.
- Added fsynced JSONL journal, exclusive ownership and interrupted-write recovery.
- Added seeded noise, burst/thermal limits, memory errors and explicit injected faults.
- Added filelock (MIT): portable kernel locks release when a writer is killed.
- Verified locally: 14 tests, including a child process killed during a partial journal write.
- Windows/Linux Python 3.11/3.13 CI passed.
- No hardware backend, driver calls, workloads or persistence were run or implemented.

## 2026-11-15 — Project start
- Repository, licence, plan and agent rules created. No code yet.
- Scope widened from RDNA 4 only to RDNA 4 and RDNA 2 first, RDNA 3 by design.
