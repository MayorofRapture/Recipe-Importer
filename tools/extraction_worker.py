"""Detached process entrypoint for durable UI extraction jobs."""
from __future__ import annotations

import argparse
from pathlib import Path

from ui_job_service import JobService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    JobService(args.output_dir).run_pending(args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
