# Third-party dependencies

This implementation is clean-room code. No source from another tuner is included.

| Dependency | Use | Licence | Source |
|---|---|---|---|
| filelock | Kernel-backed, cross-platform exclusive journal ownership; releases on process death. A homemade PID lock cannot safely provide this on Windows and Linux. | Unlicense | https://github.com/tox-dev/filelock |
| pytest (development) | Tests, including subprocess recovery and seeded acceptance | MIT | https://github.com/pytest-dev/pytest |
| ruff (development) | Linting and formatting | MIT | https://github.com/astral-sh/ruff |
| setuptools (build) | Python package build | MIT | https://github.com/pypa/setuptools |

Runtime uses Python's standard library otherwise. No AMD SDK, GPU library or bundled
executable is present at M0–M1. Dependency installations may bring their own dependencies;
their upstream distributions retain the corresponding licence notices.
