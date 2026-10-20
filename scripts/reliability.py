"""Run the seeded reliability scenarios at full size and print what was measured.

Prints one JSON report per scenario and exits 1 if any guarantee was violated.
The unit tests run the same scenarios at a smaller size.
"""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from crashes import crash_recovery, kill_during_delivery  # noqa: E402
from scenarios import alert_delivery, probe_faults  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the monitor against injected faults.")
    parser.add_argument("--seed", type=int, default=1, help="Seed for every scenario (default: 1)")
    parser.add_argument("--rounds", type=int, default=200, help="Rounds for the alert scenario (default: 200)")
    args = parser.parse_args(argv)
    if not 10 <= args.rounds <= 10000:
        parser.error("--rounds must be between 10 and 10000")
    scenarios = [
        lambda: probe_faults(seed=args.seed, rounds=max(8, args.rounds // 10)),
        lambda: alert_delivery(seed=args.seed, rounds=args.rounds),
        lambda: crash_recovery(seed=args.seed, rounds=max(10, args.rounds // 5)),
        kill_during_delivery,
    ]
    failed = False
    for scenario in scenarios:
        started = time.perf_counter()
        report = scenario()
        report["seconds"] = round(time.perf_counter() - started, 1)
        print(json.dumps(report), flush=True)
        failed = failed or not report["ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
