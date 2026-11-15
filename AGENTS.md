# rdna4-tuner — agent rules

A crash-safe automatic undervolt and VRAM tuner for AMD RDNA 4 cards. Read this file, then
`PLAN.md` (what to build, in order) and `CHANGELOG.md` (where things stand).

Roles: **Codex builds.** Claude reviews at each gate. The owner decides and is the only
person who runs anything against a real graphics card.

## Hard safety rules

1. **Never change settings on a real GPU.** Until the owner says otherwise in `PLAN.md`,
   the only backend you run is the simulated one. Do not call a voltage, clock, timing or
   reset function of a real driver, and do not run a stress workload on a real card.
2. **Stop at every gate marked STOP in `PLAN.md`.** Push, write the changelog entry and
   end the run. Do not start the next milestone.
3. **Clean room.** Do not copy code from `SpawnItems/rx9070xt-sweetspot-tuner` or any
   other project without a compatible licence. Ideas are fine; code is not. Record every
   third-party dependency and its licence in `THIRD_PARTY.md`.
4. **Never commit binaries.** Executables are built by CI and attached to releases.
5. **Nothing the tool installs may run elevated from a user-writable folder.**

## Stack

- Python 3.11+, standard library first. `pytest` for tests, `ruff` for lint and format.
  Add a dependency only when the plan names it or the alternative is clearly worse; say
  why in the commit.
- Windows driver access: a small C++ helper using AMD's ADLX SDK, speaking JSON on stdout.
  Linux: amdgpu sysfs. Both sit behind one `Backend` interface.
- Layout is fixed in `PLAN.md` section 4. Ask (in the changelog "Open" list) before adding
  a new top-level package or abstraction layer.

## Design rules

1. **The search engine and safety supervisor are pure logic.** `tuner/core/` imports no
   backend, no GPU library and no OS-specific module. It talks to a `Backend` and a
   `Workload` interface only.
2. **Journal before apply.** Every setter call is preceded by a flushed journal record of
   the intended value. No exceptions, including in tests.
3. **Read back after apply.** A setter is only successful when the value read back from
   the backend matches. A mismatch is a typed error, never a silent pass.
4. **Limits come from the backend**, never from constants in the code.
5. **Thresholds are relative to measured baseline and noise**, never absolute numbers
   taken from one card.
6. **Every tunable is data** in one config module with a comment giving its reason.
7. **Errors are typed and specific.** Never treat "any error" as "limit reached".
8. Keep it small: plain classes and functions, no plugin framework, no async unless a
   milestone needs it.

## Verifying

- `ruff check . && ruff format --check . && pytest -q` must pass before every push.
- CI runs the same on Windows and Linux.
- Report what you verified and how. Say plainly what you could not verify. Never claim
  anything about real hardware from simulator results.

## Workflow

- Small commits on `main`, imperative subject, push after each green step.
- One changelog entry per milestone, at most 15 lines, one bullet per line.
- No review-only commits, receipts, manifests or evidence inventories in git.
- If a step fails twice, write it under "Open" in the changelog entry and move on. Do not
  loop on polish and do not invent work beyond the plan.
- Do not put the owner's name, machine names, addresses or other private details in this
  public repository.
