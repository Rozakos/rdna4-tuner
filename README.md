# rdna4-tuner

A crash-safe automatic undervolt and VRAM tuner for AMD Radeon graphics cards.
Windows first, Linux later.

| Family | Cards | Priority |
|---|---|---|
| RDNA 4 | RX 9070 XT, 9070, 9060 XT | first |
| RDNA 2 | RX 6600 XT, 6700 XT and the rest of the RX 6000 series | first |
| RDNA 3 | RX 7000 series | supported by design, untested until someone has a card |

The name is historical: the project started with RDNA 4 only.

**Status: planning. Nothing here changes a GPU yet.**

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
