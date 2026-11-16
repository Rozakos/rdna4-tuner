# rdna4-tuner

A crash-safe automatic undervolt and VRAM tuner for AMD Radeon graphics cards.
Windows first, Linux later.

| Family | Cards | Priority |
|---|---|---|
| RDNA 4 | RX 9070 XT, 9070, 9060 XT | first |
| RDNA 2 | RX 6600 XT, 6700 XT and the rest of the RX 6000 series | first |
| RDNA 3 | RX 7000 series | supported by design, untested until someone has a card |

The name is historical: the project started with RDNA 4 only.

**Status: M1.2 implemented; simulator only. Numerical gate NOT passed.**

## Try the simulator

```powershell
python -m pip install -e ".[dev]"
python -m tuner tune --backend simulated --profile rdna4_offset --preset balanced --seed 42
python -m tuner report --journal journal/simulated.jsonl
python -m tuner status --backend simulated
python -m tuner tune --backend simulated --dry-run
```

Profiles: `rdna4_offset`, `rdna2_absolute`, `rdna3_absolute`, `no_vram_tuning`,
`no_voltage_tuning`, `voltage_step_5mv`.
Use a separate `--journal` path for each card/profile/seed. Repeating a tune with the same
journal recovers interrupted trials and excludes crashed values and more aggressive ones.
Stock-valued knobs never create a tuning bound. A crash with fast memory timing blocks
fast timing for that entire journal. Trial passes use `passed`; only a fully verified
final profile uses `accepted`. Legacy M1 `accepted` records lack that proof and are not
eligible final profiles.
Recovery caps each tuned knob at least one preset margin below its crashed value and
bisects from a recorded passed value. Coarse probes use smaller steps; refinement uses
60-second tests. Final memory failures reduce memory only; ambiguous combined failures
reduce memory first, while core/burst failures reduce voltage.
`--dry-run` reads synthetic capabilities without setters or journal writes. `status`
describes a fresh simulator instance and, when present, the saved result separately.

The supervisor checks every five seconds by default, stopping at the first unsafe slice.
Verification durations are **per workload**, not divided between workloads: safe/balanced
request 30 minutes each, edge requests 10 minutes each. Reports include each workload's
requested and actual seconds, slice count, margins, settings, skips and failures.
Verification first runs 60- and 300-second probes; passing candidates still complete
every full workload duration. Reports count recovered hard deaths and sum completed
test seconds across all runs in the journal. Interrupted tests contribute an exposure
range, from zero to their requested duration, because exact time at process death is unknown.
A simulated 30-minute verification advances a model; it does **not** spend 30
minutes stressing or establish stability of a physical GPU. No real backend, stress load,
ADLX bridge, boot task, apply/undo command or persistent setting is available at this gate.

## Development checks

```text
ruff check .
ruff format --check .
pytest -q
```

The statistical and durability tests are separate. The M1.2 sweep and provisional targets
are described below. It uses an in-memory journal double and asserts intent-before-setter
in the simulated backend. Filesystem durability, fsync
ordering, exclusive ownership, interrupted writes, process death and watchdog resets are
tested separately with actual files and child processes. No simulator metric proves real
GPU stability. M2 requires a separate owner-authorized milestone after review.

## Frozen M1.1 model and review gate

The original M1 statistics were insufficient: short and long tests were almost equally
demanding. M1.1 uses the review's fixed voltage failure rate:

```text
rate(v) = ln(2) / 1800 * exp((v - edge) / w)
P(failure over t seconds) = 1 - exp(-rate(v) * t)
```

Each seeded card draws `w` uniformly from 2–5 mV. Burst/combined edge is 6 mV below core.
Memory uses the same law with an independent 2–5 MHz width; the old model's 15 MHz gap
from the ECC knee to its data-error edge is retained. Fast timing lowers both by 15 MHz.
A failure beyond its error edge plus three widths is process death. The CLI exits and
its watchdog resets; the statistical harness raises a sentinel, reopens/replays the
journal and resumes. Coefficients are a fixed **unmeasured review fixture**, not Radeon
calibration. They must not be adjusted to improve acceptance results.

The sweep now covers 18,000 cards (1,000 per preset and each of six profiles), including
crash-resume. It reports hourly burst risk, timing-adjusted knee/margin accuracy,
verification failures, hard deaths (mean/p90/max) and total synthetic hours
(median/p90/max) per group. It uses 300-second statistical slices
under the same exponential exposure law; the CLI's five-second transient monitoring is
tested separately. The log double retains production crash-policy/recovery code.
Metrics are emitted in CI and written to ignored `reports/acceptance.json`.

Provisional targets are defined in `PLAN.md` R8 and S5. Every target uses a firm assertion;
numerical misses fail pytest and CI, are recorded under Open in the changelog, and leave
the gate closed. The no-voltage profile is exempt only from verification-failure coverage.
The M1.2 18,000-card sweep meets crash-count targets in every group, but safe/balanced
VRAM tune medians reach 4.082/4.452 hours (limit 4), and balanced voltage-tunable
hourly-risk shares are 88.8–89.9% (target 90%). No model retuning followed these misses.
Do not start M2 until the owner and reviewer approve the gate.

## A crash at true stock

The CLI refuses tuning with outcome `crash at stock settings` and exit code 2; repeating
the same journal does not clear the refusal. Investigate the crash first. To explicitly
start over, choose a **new** journal path; retain the old one for diagnosis:

```text
python -m tuner tune --backend simulated --profile no_vram_tuning --seed 42 --journal journal/new-attempt-42.jsonl
```

The aim is a profile that survives real gaming, not a three-minute benchmark:

- every change is journalled before it is applied, so the tool recovers from a crash and
  knows which value caused it;
- short tests find the limit, a long mixed verification decides;
- nothing runs at boot unless you ask, and one `undo` command removes everything and
  resets the card;
- the search logic is tested against a simulated card, without a GPU.

See [PLAN.md](PLAN.md) for the design and milestones, [AGENTS.md](AGENTS.md) for the rules
contributors and coding agents follow, and [CHANGELOG.md](CHANGELOG.md) for progress.

Undervolting and memory overclocking can crash your system and lose unsaved work. Use at
your own risk.

MIT licence.
