# Changelog

One entry per milestone, at most 15 lines: what changed, what was verified and how, what
could not be verified.

## 2026-11-15 — M0 skeleton and simulator
- Added package layout, typed interfaces/errors, configuration and synthetic card profiles.
- Added fsynced JSONL journal, exclusive ownership and interrupted-write recovery.
- Added seeded noise, burst/thermal limits, memory errors and explicit injected faults.
- Added filelock (Unlicense): portable kernel locks release when a writer is killed.
- Verified locally: 14 tests, including a child process killed during a partial journal write.
- Added Windows/Linux Python 3.11/3.13 CI; remote results pending this push.
- No hardware backend, driver calls, workloads or persistence were run or implemented.

## 2026-11-15 — Project start
- Repository, licence, plan and agent rules created. No code yet.
- Scope widened from RDNA 4 only to RDNA 4 and RDNA 2 first, RDNA 3 by design.
