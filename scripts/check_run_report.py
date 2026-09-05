"""
Run-report gate script for cron/CI consumption.

Reads exports/run_report.json and exits non-zero when the run is not healthy:
- report missing / unreadable / status not in {completed}
- Phase I verticals below the configured fraction of target (default 95%)
- sheets upload requested but failed

Usage:
    python scripts/check_run_report.py [--report exports/run_report.json]
                                       [--min-fraction 0.95]
Exit codes: 0 healthy, 1 unhealthy, 2 usage/IO error.
"""

import argparse
import json
import sys
from pathlib import Path

TARGET_VERTICALS = ("startups", "products", "papers")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate on exports/run_report.json health")
    parser.add_argument("--report", type=Path, default=Path("exports/run_report.json"))
    parser.add_argument("--min-fraction", type=float, default=0.95,
                        help="Minimum collected/target fraction per Phase I vertical (default 0.95)")
    args = parser.parse_args()

    if not args.report.exists():
        print(f"FAIL: run report not found at {args.report}")
        return 2
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"FAIL: run report unreadable: {exc}")
        return 2

    status = report.get("status")
    if status == "interrupted":
        print("FAIL: run was interrupted before completion")
        return 1
    if status == "shortfall":
        print("FAIL: Phase I target shortfall reported by the pipeline")
        return 1
    if status != "completed":
        print(f"FAIL: unexpected run status '{status}'")
        return 1

    failures: list = []
    if report.get("phase1"):
        target = report.get("target_count", 0)
        collected = report.get("collected", {})
        for vertical in TARGET_VERTICALS:
            got = collected.get(vertical, 0)
            if target <= 0:
                failures.append(f"{vertical}: target_count must be positive, got {target}")
            elif got < args.min_fraction * target:
                failures.append(f"{vertical}: collected {got}/{target} < {args.min_fraction:.0%} of target")

    if report.get("sheets_upload") == "failed":
        failures.append("sheets_upload: upload was requested but failed after retries")

    stale = report.get("stale_sources") or []
    for entry in stale:
        print(f"WARN: stale source {entry.get('source')} ({entry.get('crawler')}) — "
              f"{entry.get('consecutive_zero_runs')} consecutive zero-fresh runs")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(f"OK: run {report.get('run_id')} status=completed, "
          f"collected={report.get('collected')}, sheets_upload={report.get('sheets_upload')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
