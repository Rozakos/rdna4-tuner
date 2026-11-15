# rdna4-tuner — plan

Work top to bottom. Tick a step only after it is verified. Rules are in `AGENTS.md`.

## 1. Goal

Find a safe undervolt and VRAM clock for an RX 9070 XT / 9070 / 9060 XT and keep it
applied, where "safe" means it survives real gaming, not a three-minute benchmark.

Done means: on a real RX 9070 XT the tool produces a profile; the profile passes a
30-minute mixed verification and a week of normal gaming with no driver crash; and one
command puts the card back to stock and removes everything the tool installed.

## 2. Principles

1. **Never leave the card in a state the tool cannot recover from.** Every change is
   journalled before it is applied. After a crash the tool reads the journal, marks that
   value as failed and continues from the last good one.
2. **Short tests find the edge; long tests decide.**
3. **Nothing is applied at boot unless the user asks**, and anything that runs elevated
   lives in a folder only administrators can write to.
4. **Everything that can be tested without a GPU is tested without a GPU.**
5. **No numbers calibrated from one card.**
6. **Honest output.** The report states what was tested, for how long, and the margin.

## 3. Scope

In: Windows 11 first (AMD ADLX), Linux second (amdgpu sysfs). Core voltage offset, VRAM
maximum frequency and memory timing. Power limit stays at stock. CLI first, GUI later.

Out: BIOS flashing, power-table edits, fan curves, other GPU families, anything that
raises voltage or power above stock.

## 4. Layout

```
tuner/
  core/        search.py  journal.py  supervisor.py  config.py  types.py
  backends/    base.py  simulated.py  adlx_windows.py  sysfs_linux.py
  workloads/   base.py  simulated.py  core_load.py  burst_load.py
               vram_bandwidth.py  vram_verify.py  combined.py
  persist/     install.py  apply.py  undo.py
  report.py
  cli.py       tune | verify | apply | status | undo | report
bridge/        C++ ADLX helper, built by CI
tests/
```

### 4.1 Interfaces

```python
class Backend(Protocol):
    def read_limits(self) -> Limits: ...            # voltage offset range, VRAM range, steps
    def read_telemetry(self) -> Telemetry: ...      # clock, power, temps, current settings
    def set_voltage_offset(self, mv: int) -> None: ...   # raises BackendError subclasses
    def set_vram(self, mhz: int, timing: Timing) -> None: ...
    def reset(self) -> None: ...                    # back to driver defaults
    def crash_events_since(self, t: datetime) -> list[CrashEvent]: ...

class Workload(Protocol):
    name: str
    def run(self, seconds: float) -> WorkloadResult: ...   # score, noise, ok, detail
```

Typed errors: `OutOfRange`, `Rejected`, `ReadbackMismatch`, `Unavailable`,
`PermissionDenied`. "Any error" is never treated as "limit reached".

### 4.2 Journal

Append-only JSON lines, flushed and fsynced. Records: `run_start`, `baseline`,
`intent {setting, value}`, `applied`, `test_start`, `test_result`, `accepted`, `rejected`,
`run_end`. On start, an `intent` or `test_start` with no following result means the
machine died there: that value is recorded as failed with reason `crash`.

### 4.3 Search

1. **Baseline:** N repeated runs of each workload at stock give score and noise.
2. **Coarse voltage search:** step down by `coarse_step_mv` with short tests until the
   first failure, then bisect to `fine_step_mv`.
3. **VRAM search:** the same, upward.
4. **Margin:** back off by the preset margin.
5. **Verification:** long combined and burst load at the candidate. Any failure backs off
   one step and repeats the whole verification. Give up and return stock after
   `max_backoffs`.

A test fails when: a workload reports wrong results; a crash event appears; score drops
by more than `k_sigma` times baseline noise; temperature or power exceeds the backend's
limit; or the journal shows a crash at that value.

Presets (margin / verification time): `safe` 15 mV, 25 MHz, 30 min; `balanced` 10 mV,
15 MHz, 30 min; `edge` 5 mV, 10 MHz, 10 min. Defaults live in `config.py`.

### 4.4 Supervisor

Owns the journal, the temperature and power checks, crash-event checks after every test,
and a watchdog child process that resets the backend if the main process dies.

### 4.5 Simulated backend and workloads

A model card with: a hidden voltage limit that depends on load type (bursts fail earlier
than full load) and on temperature; a hidden VRAM limit above which bandwidth flattens,
noise rises and, further up, data errors appear; run-to-run noise; thermal drift during
long runs; a probability of failure that rises smoothly near the limit instead of a hard
edge; injected faults (hard crash that kills the process, driver reset, rejected setter,
readback mismatch). Seeded and deterministic.

### 4.6 Persistence (later milestones)

`apply --at-logon` copies the helper and a config into a protected folder and registers
the task or service from there. At each logon it checks for a crash or unexpected
shutdown since the last apply and does not apply if there was one. `undo` removes all of
it and resets the card.

## 5. Milestones

### M0 — Skeleton (no hardware)
- [ ] Package layout, `pyproject.toml`, ruff and pytest configured, `THIRD_PARTY.md`.
- [ ] GitHub Actions: lint, format check and tests on Windows and Linux.
- [ ] `types.py`, `config.py`, `Backend` and `Workload` protocols, typed errors.
- [ ] Journal with crash-recovery reader. Tests: truncated last line, unfinished intent,
      unfinished test, concurrent open refused.
- [ ] Simulated backend and workloads as in 4.5, with tests for each behaviour.

Done when CI is green on both systems and the journal tests include a real process kill
(a child process killed mid-write, then recovered by the parent).

### M1 — Search and safety against the simulator (no hardware)
- [ ] Baseline measurement with noise estimate.
- [ ] Coarse, bisect, margin and verification stages; presets.
- [ ] Supervisor: limits, crash events, watchdog child that resets the backend.
- [ ] Crash-resume: a simulated hard crash at any step resumes correctly.
- [ ] `cli.py tune --backend simulated`, `status`, `report`, `--dry-run`.
- [ ] Report: values, margins, what was tested and for how long, every failure seen.

Acceptance, as tests over 1,000 seeded simulated cards per preset:
- the accepted voltage is never beyond the card's true stable limit for the verification
  duration, in at least 99.5 % of cards;
- the accepted value is within `margin + 2 × fine_step` of the true limit in at least
  90 % of cards (it is not uselessly conservative);
- no setter is ever called without a preceding journal intent (assert in the backend);
- after any injected crash, the next run never re-tests the crashed value or anything
  beyond it;
- every run ends with the backend at the accepted profile or at stock, never in between.

**STOP. Gate M1: Claude reviews. Hardware work needs the owner's go-ahead.**

### M2 — Windows backend, read-only (needs the owner)
- [ ] C++ ADLX helper: `--limits`, `--status` only. Built by CI. No setter code path.
- [ ] `adlx_windows.py` implementing the read half of `Backend`.
- [ ] `cli.py status --backend adlx`.

Done when the owner runs `status` on a real card and the values match Adrenalin.

**STOP. Gate M2.**

### M3 — Workloads at stock (needs the owner)
- [ ] Compute-shader workloads: core, burst, VRAM bandwidth, VRAM verify, combined.
- [ ] Baseline run at stock settings only.

Done when repeated baseline runs on the real card agree within 1 %.

**STOP. Gate M3.**

### M4 — First real tune, nothing persisted (needs the owner)
- [ ] Setters in the helper, with range checks against driver limits and readback.
- [ ] `tune --backend adlx --no-persist`, `edge` preset verification time only.
- [ ] Forced-crash recovery checked on the real card.

**STOP. Gate M4.**

### M5 — Verification, apply and undo (needs the owner)
- [ ] Full verification times, final report.
- [ ] `apply`, `apply --at-logon` from a protected folder, crash check at logon, `undo`.

**STOP. Gate M5.**

### Later (do not start without the owner)
Linux backend; small GUI; soak mode that backs off automatically after a driver crash
during normal use; packaged release built by CI with a checksum.

## 6. Risks

- **Driver API changes or blocks a setting.** The backend is thin; fall back to printing
  the values to enter in Adrenalin by hand.
- **A hard crash loses unsaved work.** The tool warns before starting and never runs at
  boot by default.
- **Bandwidth is a noisy signal for VRAM errors.** Pattern verification is used as well,
  and thresholds scale with measured noise.
- **Short tests mislead.** Addressed by verification and, later, soak mode.
