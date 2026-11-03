"""Run the whole test suite many times, each in a fresh interpreter, in a shuffled order.

    python3 scripts/test_many.py              # 10 runs
    python3 scripts/test_many.py --runs 25

Every run uses Python's development mode with warnings turned into errors, so leaked
sockets, unclosed files and deprecated calls fail loudly. Each run gets its own seed; the
seed sets the test order and the random meshes of the property and soak tests, and a
failing run prints it so the exact run can be replayed with --seed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "UPLINK-RESULT "


def child(seed: int) -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    tests = list(_flatten(suite))
    random.Random(seed).shuffle(tests)
    result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(unittest.TestSuite(tests))
    print(
        MARKER
        + json.dumps(
            {
                "tests": result.testsRun,
                "failures": len(result.failures) + len(result.errors),
                "skipped": len(result.skipped),
            }
        )
    )
    return 0 if result.wasSuccessful() else 1


def parent(runs: int, seed: int | None) -> int:
    seeds = [seed] if seed is not None else [random.SystemRandom().randrange(1 << 30) for _ in range(runs)]
    failed: list[tuple[int, str]] = []
    total = time.monotonic()
    print(
        f"Running the Uplink test suite {len(seeds)} times (fresh interpreter, shuffled order, warnings are errors)\n"
    )
    for number, run_seed in enumerate(seeds, 1):
        started = time.monotonic()
        process = subprocess.run(
            [sys.executable, "-X", "dev", "-W", "error", __file__, "--child", "--seed", str(run_seed)],
            cwd=ROOT,
            env={**os.environ, "UPLINK_TEST_SEED": str(run_seed), "PYTHONHASHSEED": str(run_seed)},
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        summary = next(
            (json.loads(line[len(MARKER) :]) for line in process.stdout.splitlines() if line.startswith(MARKER)), None
        )
        ok = process.returncode == 0 and summary is not None
        counts = f"{summary['tests']} tests, {summary['skipped']} skipped" if summary else "no result"
        print(
            f"  run {number:>2}/{len(seeds)}  seed {run_seed:<10}  {counts:<24} {elapsed:5.1f} s  {'OK' if ok else 'FAILED'}"
        )
        if not ok:
            failed.append((run_seed, process.stderr[-6000:]))
    print(f"\n{len(seeds) - len(failed)} of {len(seeds)} runs passed in {time.monotonic() - total:.1f} s.")
    for run_seed, output in failed:
        print(f"\n--- failing run, replay with: python3 scripts/test_many.py --seed {run_seed}\n{output}")
    return 1 if failed else 0


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--seed", type=int, help="replay one run")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.exit(child(args.seed) if args.child else parent(args.runs, args.seed))
