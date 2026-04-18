"""Phase 12B Week 3 evaluation harness.

These are *benchmarks*, not unit tests. They share pytest collection
(so CI catches regressions), but each module also exposes a
`python -m tests.eval.<name>` entrypoint that prints a canonical JSON
report suitable for `docs/benchmarks.md` and the nightly workflow.

All benchmarks are:

- **Synthetic.** No third-party datasets, no network. Seed-reproducible.
- **LLM-free by default.** We measure memory-layer semantics directly —
  a good LLM can't rescue a broken bitemporal query, and a bad LLM
  can't invalidate one.
- **Flag-gated.** They enable `MEMORY_ENABLED=True` for their own run
  and restore it in teardown so the MEMORY_ENABLED=False regression
  tripwire (tests/test_memory_regression.py) stays green.

Exit-gate thresholds from MEMORY_FLAGSHIP_PLAN.md §4 are enforced
inline via `assert` — the pytest run IS the exit gate.
"""

from __future__ import annotations

import logging
import sys


def reroute_logging_to_stderr() -> None:
    """Send root-logger output to stderr for CLI benchmark runs.

    The app's `app.logging_config.configure_logging()` routes logs to
    stdout because production Nexus runs as a server where stdout is
    the canonical log stream (JSON-to-stdout ingestion). For a CLI
    benchmark the stdout contract is inverted: stdout MUST be a
    parseable JSON document, so any log line on stdout corrupts the
    `--json` output that `docs/benchmarks.md` and the
    `nightly_benchmark.yml` workflow consume.

    This helper is a no-op under pytest (pytest owns logging capture
    and our test path never touches stdout anyway). Call it at the
    top of each benchmark's `_main()` BEFORE any `app.*` imports
    that might trigger `configure_logging()` — otherwise a stdout
    handler can be installed before we get to swap it.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
