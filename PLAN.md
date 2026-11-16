# rdna4-tuner — plan

Work top to bottom. Tick a step only after it is verified. Rules are in `AGENTS.md`.

## 1. Goal

Find a safe undervolt and VRAM clock for a Radeon card and keep it applied, where "safe" means it survives real gaming, not a three-minute benchmark.

Done means: on a real RX 9070 XT and a real RX 6700 XT the tool produces a profile; the profile passes a
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

In: Windows 11 first (AMD ADLX), Linux second (amdgpu sysfs). Core voltage, VRAM maximum
frequency and memory timing. Power limit stays at stock. CLI first, GUI later.

Card families, in priority order:

| Family | Cards | Status |
|---|---|---|
| RDNA 4 | RX 9070 XT, 9070, 9060 XT | first; the owner has a 9070 XT |
| RDNA 2 | RX 6600 XT, 6700 XT, rest of RX 6000 | first; the owner has a 6700 XT |
| RDNA 3 | RX 7000 | must work by design; untested until a card is available |

The families expose tuning differently, and the design must not assume one of them. As
far as we know before checking real cards at M2: RDNA 4 exposes a **voltage offset**
(negative millivolts from stock); RDNA 2 and RDNA 3 expose an **absolute voltage** in
millivolts together with minimum and maximum clock frequency; VRAM tuning is a maximum
frequency plus a timing level on all of them, with different ranges. Treat every item in
this paragraph as unverified until M2 reads it from the driver.

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
    def read_capabilities(self) -> Capabilities: ...  # see below
    def read_limits(self) -> Limits: ...  # ranges and steps for what is supported
    def read_telemetry(self) -> Telemetry: ...  # clock, power, temps, current settings
    def set_voltage(self, reduction_mv: int) -> None: ...  # mV below stock; raises BackendError
    def set_vram(self, mhz: int, timing: Timing) -> None: ...
    def reset(self) -> None: ...  # back to driver defaults
    def crash_events_since(self, t: datetime) -> list[CrashEvent]: ...


class Workload(Protocol):
    name: str

    def run(self, seconds: float) -> WorkloadResult: ...  # score, noise, ok, detail
```

`Capabilities` says what this card and driver allow: voltage control kind (`offset`,
`absolute` or `none`), stock voltage where it is absolute, whether clock limits exist,
whether VRAM frequency and timing can be set, and the card family and name for the
report. The search engine works in one unit, **millivolts below stock**, and the backend
translates that to an offset or an absolute value. A card that cannot tune something
skips that stage and the report says so. `tuner/core/` never branches on a card name.

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

Model cards built from profiles, at least: `rdna4_offset` (offset voltage, RX 9070 XT-like
ranges), `rdna2_absolute` (absolute voltage and clock limits, RX 6700 XT-like ranges),
`rdna3_absolute`, and `no_vram_tuning` (VRAM stage unsupported). Each has: a hidden voltage limit that depends on load type (bursts fail earlier
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
- [x] Package layout, `pyproject.toml`, ruff and pytest configured, `THIRD_PARTY.md`.
- [x] GitHub Actions: lint, format check and tests on Windows and Linux.
- [x] `types.py`, `config.py`, `Backend` and `Workload` protocols, typed errors.
- [x] Journal with crash-recovery reader. Tests: truncated last line, unfinished intent,
      unfinished test, concurrent open refused.
- [x] Simulated backend and workloads as in 4.5, with tests for each behaviour.

Done when CI is green on both systems and the journal tests include a real process kill
(a child process killed mid-write, then recovered by the parent).

### M1 — Search and safety against the simulator (no hardware)
- [x] Baseline measurement with noise estimate.
- [x] Coarse, bisect, margin and verification stages; presets.
- [x] Supervisor: limits, crash events, watchdog child that resets the backend.
- [x] Crash-resume: a simulated hard crash at any step resumes correctly.
- [x] `cli.py tune --backend simulated`, `status`, `report`, `--dry-run`.
- [x] Report: values, margins, what was tested and for how long, every failure seen.

Acceptance, as tests over 1,000 seeded simulated cards per preset **and per simulated
profile** (offset, absolute, no VRAM tuning):
- the accepted voltage is never beyond the card's true stable limit for the verification
  duration, in at least 99.5 % of cards;
- the accepted value is within `margin + 2 × fine_step` of the true limit in at least
  90 % of cards (it is not uselessly conservative);
- no setter is ever called without a preceding journal intent (assert in the backend);
- after any injected crash, the next run never re-tests the crashed value or anything
  beyond it;
- every run ends with the backend at the accepted profile or at stock, never in between.

**STOP. Gate M1: Claude reviews. Hardware work needs the owner's go-ahead.**

### M1.1 — Gate M1 review fixes (no hardware)

Gate M1 is **not passed yet**. Reviewed at `42423f0`: the structure is right, 62 tests pass
and CI is green. The items below were found by running the code; fix them in order.

- [x] **R1 Crash during verification can lock the card at stock.** `crash_bounds()` turns a
      `profile` crash into a VRAM bound even when VRAM was at stock, so the next run says
      "previous stock test crashed" and returns stock for ever. Reproduce: `no_vram_tuning`,
      seed 42, hard crash on workload run 33. Rule: a crash never bounds a setting that was
      at its stock value. Test: after that crash the next run accepts a non-zero reduction
      below the crashed one.
- [x] **R2 Crash at true stock.** Keep refusing to tune, but say so: outcome
      `crash at stock settings`, a CLI message that explains it, and a documented way to
      start a new journal. Not a silent stock result.
- [x] **R3 Fast timing crash is forgotten.** After a crash with fast timing the next run
      tries fast timing again at a slightly lower frequency. Record timing in the crash
      bound and do not try fast timing again in that journal.
- [x] **R4 Timing trial spends the VRAM margin.** Fast timing is tried after the margin is
      subtracted. With `balanced`, 779 of 1,000 cards end past the bandwidth knee (the
      error-correction zone). The margin must apply to the final frequency and timing pair.
- [x] **R5 Long runs are unsupervised.** `Supervisor.test` checks temperature, power and
      crash events only after `run(seconds)` returns, so a 10-minute run has no check for
      10 minutes. Run in slices of `check_interval_seconds` (config), check between slices,
      stop at once on a breach. Test: a thermal fault injected mid-verification stops the
      run within one slice.
- [x] **R6 `accepted` means two things.** Short-test passes and the final verified profile
      both write `accepted`. Write `passed` for trial passes and keep `accepted` for the
      final profile only, so a later `apply` can never pick up a short-test pass.
- [x] **R7 The simulator is too gentle to test verification.** Failure chance is one draw
      per run, almost independent of duration (at the burst edge: 0.51 for 8 s, 0.98 for
      600 s and for 1,800 s alike), and the edge moves by at most 2 mV. In all 12,000 sweep
      cards verification passed first time, so back-off was never exercised. Replace with a
      failure **rate per second**: `rate(v) = ln2/1800 * exp((v - edge) / w)`, with `w`
      drawn per card from 2 to 5 mV and the burst edge 6 mV lower than the core edge. Keep
      the same idea for VRAM data errors. Beyond `edge + 3w` a failure is a hard crash, not
      a wrong result: in the sweep raise a `SimulatedHardCrash` that the harness treats as
      process death (reopen the journal, recover, run again), so crash-resume is part of
      the statistics.
- [ ] **R8 Acceptance on the new model**, 1,000 cards per preset and profile:
      - report, per preset, the share of accepted profiles whose modelled chance of failing
        in one hour of burst load is under 5 %. Targets: `safe` at least 95 %, `balanced`
        at least 90 %, `edge` reported only;
      - accepted VRAM is never past the knee (timing included) in at least 99.5 % of cards
        and within `margin + 2 × fine_step` of it in at least 90 %;
      - verification fails at least once in more than 5 % of cards (proves the path runs);
      - the earlier rules on journal intent, crashed values and end state still hold.
      You may change the search to meet this. Two changes I expect to be needed: apply the
      margin **below the value that passed verification**, and back off by at least the
      margin, not one fine step, after a failed verification. **Do not retune the simulator
      to make the targets pass.** If a target is missed, put the measured numbers under
      "Open" and stop; the owner and Claude decide.
- [x] **R9 Small items.** Report verification time per workload (30 minutes is currently
      split three ways). Truncate a torn journal tail in place instead of rewriting the
      file. `stop_watchdog` in `finally` must not hide the original error. Add a simulated
      profile with `voltage_kind = none` and one with a 5 mV voltage step to the sweep.

R8 remains open: balanced hourly-risk results are 89.5–89.9% on voltage-tunable profiles
(target 90% each); no-voltage verification failure coverage is 0–0.2% (target >5% per
profile). See CHANGELOG.md for the complete per-profile risk shares. No simulator or
search retuning followed these misses. Functional CI success does not pass this gate.

**STOP. Gate M1.1: Claude reviews.**

### M1.2 — Fewer crashes, shorter tunes (no hardware)

Reviewed at `1f44918`. R1–R7 and R9 are fixed and confirmed by re-running the probes.
Reporting the R8 misses instead of retuning was the right call. Gate M1.1 is still **not
passed**, for two things the new model exposed. Measured on 300 cards per preset:

| | hard crashes per tune | synthetic hours per tune |
|---|---|---|
| `rdna4_offset`, safe / balanced | mean 9.6, 90th percentile 18, max 40 | median 5.7 / 6.3, 90th percentile 18 / 20, max 52 |
| `no_vram_tuning`, safe / balanced | mean 7.3, 90th percentile 14, max 20 | median 3.6 / 3.8 |

On a real card every hard crash is a frozen or rebooted PC. Fewer than 4 % of cards tune
without one.

- [ ] **S1 A crash must move the search down by more than 1 mV.** After a crash at `v` the
      cap is `v - 1`, the coarse step jumps straight back to it, and it crashes again:
      seed 42 on `no_vram_tuning` crashes at 120, 119, 118, 117, 116, 115 in a row. Treat a
      crash as a failed test in the bisection: resume between the last passed value and the
      crashed one, never within one preset margin of a crashed value. Same for VRAM.
- [ ] **S2 Approach the edge so that a failure is usually a failed test, not a crash.** For
      example a smaller coarse step, or longer tests as the steps get smaller. Your choice;
      short tests are cheap and crashes are not.
- [ ] **S3 Do not lower voltage for a memory failure.** In the final phase every failed
      round takes a margin off voltage and VRAM together. Lower only what failed; when that
      is unclear, lower VRAM first, because voltage already passed its own verification.
- [ ] **S4 Cut wasted verification time** without shortening a passing verification. The
      first verification starts at the 8-second edge, where a 30-minute run is almost sure
      to fail. Step the test length up (for example 8 s, 60 s, 300 s) before the full run.
- [ ] **S5 Acceptance additions**, same sweep, firm assertions:
      - hard crashes per tune: mean at most 2.5 and 90th percentile at most 4;
      - synthetic hours per tune for `safe` and `balanced`: median at most 4 and 90th
        percentile at most 7 on VRAM-capable profiles;
      - the R8 hourly-risk and VRAM targets stay as they are and are measured again;
      - the verification-failure coverage target is dropped for `no_voltage_tuning` only;
      - print crashes and hours per profile and preset next to the other numbers.
      Replace `pytest.xfail` with real assertions for every target that is met. As before:
      no simulator retuning; if a target is missed, report the numbers under "Open" and stop.
- [ ] **S6 Report** the number of hard crashes recovered from and the total test time.

**STOP. Gate M1.2: Claude reviews.**

### M2 — Windows backend, read-only (needs the owner)
- [ ] C++ ADLX helper: `--limits`, `--status` only. Built by CI. No setter code path.
- [ ] `adlx_windows.py` implementing the read half of `Backend`.
- [ ] `cli.py status --backend adlx`, printing capabilities as well as limits.

Done when the owner runs `status` on a real RX 9070 XT and a real RX 6700 XT and the values
match Adrenalin on both. Record what each family really exposes in `docs/families.md` and
correct section 3 where it was wrong.

**STOP. Gate M2.**

### M3 — Workloads at stock (needs the owner)
- [ ] Compute-shader workloads: core, burst, VRAM bandwidth, VRAM verify, combined.
- [ ] Baseline run at stock settings only.

Done when repeated baseline runs agree within 1 % on both real cards.

**STOP. Gate M3.**

### M4 — First real tune, nothing persisted (needs the owner)
- [ ] Setters in the helper, with range checks against driver limits and readback.
- [ ] `tune --backend adlx --no-persist`, `edge` preset verification time only.
- [ ] Forced-crash recovery checked on the real card.
- [ ] Repeat on the second family once the first passes.

**STOP. Gate M4.**

### M5 — Verification, apply and undo (needs the owner)
- [ ] Full verification times, final report.
- [ ] `apply`, `apply --at-logon` from a protected folder, crash check at logon, `undo`.

**STOP. Gate M5.**

### Later (do not start without the owner)
Linux backend; RDNA 3 confirmation on a real card; small GUI; soak mode that backs off automatically after a driver crash
during normal use; packaged release built by CI with a checksum.

## 6. Risks

- **Driver API changes or blocks a setting.** The backend is thin; fall back to printing
  the values to enter in Adrenalin by hand.
- **A hard crash loses unsaved work.** The tool warns before starting and never runs at
  boot by default.
- **Bandwidth is a noisy signal for VRAM errors.** Pattern verification is used as well,
  and thresholds scale with measured noise.
- **Short tests mislead.** Addressed by verification and, later, soak mode.
