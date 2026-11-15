# Changelog

One entry per milestone, at most 15 lines: what changed, what was verified and how, what
could not be verified.

## 2026-11-15 — M1.1 review fixes; gate NOT passed
- R1–R3: stock knobs no longer bound tuning; true-stock crashes refuse explicitly; crashed fast timing stays blocked.
- R4: search each memory timing separately and margin the chosen pair; voltage verification precedes its margin.
- R5–R6: supervise five-second slices; separate trial `passed` from final verified `accepted` (legacy records ineligible).
- R7: frozen per-second exponential failure law, stochastic hard deaths and actual journal recovery in the sweep.
- R9: per-workload verification durations, in-place torn-tail truncation, preserved original cleanup errors; six profiles.
- Verified: lint/format, 71 functional tests; 18,000 cards measured: 10 numerical groups pass, 8 explicitly XFAIL.
- Safe hourly-risk shares by profile (RDNA4/RDNA2/RDNA3/no-VRAM/no-voltage/5mV): 98.8/98.8/98.8/98.1/100/99.1%.
- Balanced shares in the same order: 89.9/89.9/89.9/89.8/100/89.5%; edge (report only): 17.7/17.7/17.7/15.3/100/15.7%.
- Aggregate hourly-risk shares safe/balanced/edge: 98.933/91.500/30.683%; aggregation does not remove per-profile misses.
- All memory-capable groups: 100% at/below the timing-adjusted knee; margin accuracy 96.9–100% (target 90%).
- Open R8: balanced misses the 90% hourly-risk gate on every voltage-tunable profile; do not round or retune to pass.
- Open R8: no-voltage verification-failure coverage is 0/0/0.2% for safe/balanced/edge, below >5% per-profile coverage.
- STOP at M1.1 for owner/Claude decision; no GPU touched. Full measurements emit in CI and ignored reports/acceptance.json.

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
