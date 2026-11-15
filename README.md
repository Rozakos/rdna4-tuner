# rdna4-tuner

A crash-safe automatic undervolt and VRAM tuner for AMD Radeon graphics cards.
Windows first, Linux later.

| Family | Cards | Priority |
|---|---|---|
| RDNA 4 | RX 9070 XT, 9070, 9060 XT | first |
| RDNA 2 | RX 6600 XT, 6700 XT and the rest of the RX 6000 series | first |
| RDNA 3 | RX 7000 series | supported by design, untested until someone has a card |

The name is historical: the project started with RDNA 4 only.

**Status: M0–M1 implemented; review gate M1. Simulated hardware only.**

## Try the simulator

```powershell
python -m pip install -e ".[dev]"
python -m tuner tune --backend simulated --profile rdna4_offset --preset balanced --seed 42
python -m tuner report --journal journal/simulated.jsonl
python -m tuner status --backend simulated
python -m tuner tune --backend simulated --dry-run
```

Profiles: `rdna4_offset`, `rdna2_absolute`, `rdna3_absolute`, `no_vram_tuning`.
Use a separate `--journal` path for each card/profile/seed. Repeating a tune with the same
journal recovers interrupted trials and excludes crashed values and more aggressive ones.
`--dry-run` reads synthetic capabilities without setters or journal writes. `status`
describes a fresh simulator instance and, when present, the saved result separately.

The report contains margins, tested settings and synthetic durations, skipped features and
failures. A simulated 30-minute verification advances a model; it does **not** spend 30
minutes stressing or establish stability of a physical GPU. No real backend, stress load,
ADLX bridge, boot task, apply/undo command or persistent setting is available at this gate.

## Development checks

```text
ruff check .
ruff format --check .
pytest -q
```

The statistical sweep tests 1,000 seeds for each of three presets and each of four profiles
(12,000 cards), requiring at least 99.5% below the model's long-duration voltage limit and
90% within the specified margin plus two fine steps. It uses an in-memory journal double
and asserts intent-before-setter in the simulated backend. Filesystem durability, fsync
ordering, exclusive ownership, interrupted writes, process death and watchdog resets are
tested separately with actual files and child processes. No simulator metric proves real
GPU stability. M2 requires a separate owner-authorized milestone after review.

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
