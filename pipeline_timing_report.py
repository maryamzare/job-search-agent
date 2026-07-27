"""
CLI for the pipeline timing report. See modules/lifecycle_metrics.py for
the metric definitions and ARCHITECTURE.md's "Pipeline Timing
Instrumentation" section for why this exists.

Usage:
  python3 pipeline_timing_report.py
      Compute metrics from data/job_queue.json, print a summary, and
      write the full report to outputs/reports/.
"""
import os
from datetime import datetime, timezone

from config import JOB_QUEUE_PATH
from modules.util import load_queue
from modules.lifecycle_metrics import compute_metrics, generate_report

REPORTS_DIR = "outputs/reports"


def main():
    queue = load_queue(JOB_QUEUE_PATH)
    metrics = compute_metrics(queue["jobs"])
    report = generate_report(metrics)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(
        REPORTS_DIR, f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_pipeline_timing.md"
    )
    with open(report_path, "w") as f:
        f.write(report)

    print(report)
    print(f"[pipeline-timing] Report written to {report_path}")


if __name__ == "__main__":
    main()
