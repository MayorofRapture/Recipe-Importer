"""Detached process entrypoint for durable UI extraction jobs."""
from __future__ import annotations

import argparse
from pathlib import Path

from ui_job_service import JobService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--range-job", action="store_true",
                        help="Run a durable cookbook page-range job.")
    args = parser.parse_args()
    service = JobService(args.output_dir)
    if args.range_job:
        service.run_range_pending(args.job_id)
    else:
        service.run_pending(args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
