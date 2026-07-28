"""
CLI for the performance baseline: reads data/llm_usage_log.jsonl and
data/pipeline_stage_log.jsonl and prints per-module API usage and stage
timing summaries. See modules/performance_baseline.py for the aggregation
logic and docs/PERFORMANCE_BASELINE.md for the written baseline analysis
this data feeds into.

Usage:
  python3 performance_baseline_report.py
"""
import json

from config import LLM_USAGE_LOG_PATH, PIPELINE_STAGE_LOG_PATH
from modules.performance_baseline import (
    compute_api_usage_by_module,
    compute_stage_timing_by_module,
    slowest_modules,
)


def _load_jsonl(path: str) -> list:
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        pass
    return entries


def print_report() -> None:
    usage_entries = _load_jsonl(LLM_USAGE_LOG_PATH)
    stage_entries = _load_jsonl(PIPELINE_STAGE_LOG_PATH)

    print("\n=== Performance Baseline ===\n")

    print("--- API usage by module ---")
    usage_by_module = compute_api_usage_by_module(usage_entries)
    if not usage_by_module:
        print("No LLM calls logged yet. Run the pipeline, then re-run this report.")
    else:
        print(f"{'module':<28} {'calls':>6} {'fail':>5} {'in_tok':>8} {'out_tok':>8} {'total_tok':>10} "
              f"{'cache_wr':>9} {'cache_rd':>9} {'cost_usd':>10}")
        for module, row in sorted(usage_by_module.items()):
            print(f"{module:<28} {row['calls']:>6} {row['failures']:>5} {row['input_tokens']:>8} "
                  f"{row['output_tokens']:>8} {row['total_tokens']:>10} {row['cache_creation_tokens']:>9} "
                  f"{row['cache_read_tokens']:>9} ${row['cost_usd']:>9.4f}")

    print("\n--- Stage timing by module (slowest first) ---")
    stage_timing = compute_stage_timing_by_module(stage_entries)
    if not stage_timing:
        print("No stage timing logged yet. Run the pipeline, then re-run this report.")
    else:
        print(f"{'stage':<28} {'runs':>6} {'fail':>5} {'avg_s':>8} {'median_s':>9} {'total_s':>9}")
        for stage in slowest_modules(stage_timing):
            row = stage_timing[stage]
            print(f"{stage:<28} {row['runs']:>6} {row['failures']:>5} {row['avg_duration_s']:>8} "
                  f"{row['median_duration_s']:>9} {row['total_duration_s']:>9}")

    print()


if __name__ == "__main__":
    print_report()
