"""
Usage report — summarizes data/llm_usage_log.jsonl (written by every Claude
API call via modules.util.tracked_create/tracked_create_async) into
per-label latency/success/cost stats.

This is the comparison tool for "current vs optimized": run the pipeline,
note the current time, make a change (e.g. add prompt caching), run the
pipeline again, then diff the before/after reports with --since.

Usage:
  python3 usage_report.py                    # summarize the whole log
  python3 usage_report.py --since <unix_ts>  # only entries at/after this time
"""
import json
import sys
from collections import defaultdict

from config import LLM_USAGE_LOG_PATH


def load_entries(path: str, since: float = None) -> list:
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if since is not None and entry.get("timestamp", 0) < since:
                    continue
                entries.append(entry)
    except FileNotFoundError:
        pass
    return entries


def summarize(entries: list) -> list:
    by_label = defaultdict(list)
    for e in entries:
        by_label[e["label"]].append(e)

    rows = []
    for label, es in sorted(by_label.items()):
        count = len(es)
        successes = [e for e in es if e.get("success")]
        latencies = [e["latency_s"] for e in es if "latency_s" in e]
        costs = [e["cost_usd"] for e in es if e.get("cost_usd") is not None]
        rows.append({
            "label": label,
            "calls": count,
            "success_rate": len(successes) / count if count else 0,
            "failures": count - len(successes),
            "avg_latency_s": sum(latencies) / len(latencies) if latencies else None,
            "total_cost_usd": sum(costs) if costs else None,
        })
    return rows


def print_report(entries: list) -> None:
    rows = summarize(entries)
    if not rows:
        print("No usage log entries found. Run the pipeline at least once, then re-run this report.")
        return

    total_calls = sum(r["calls"] for r in rows)
    total_cost = sum(r["total_cost_usd"] or 0 for r in rows)
    total_failures = sum(r["failures"] for r in rows)

    print(f"\n=== LLM Usage Report ({total_calls} calls) ===\n")
    print(f"{'label':<28} {'calls':>6} {'success%':>9} {'avg_latency_s':>14} {'total_cost_usd':>15}")
    for r in rows:
        success_pct = f"{r['success_rate'] * 100:.0f}%"
        avg_lat = f"{r['avg_latency_s']:.2f}" if r["avg_latency_s"] is not None else "?"
        cost = f"${r['total_cost_usd']:.4f}" if r["total_cost_usd"] is not None else "?"
        print(f"{r['label']:<28} {r['calls']:>6} {success_pct:>9} {avg_lat:>14} {cost:>15}")

    print(f"\n  Total calls: {total_calls}  |  Total failures: {total_failures}  |  Total cost: ${total_cost:.4f}\n")


if __name__ == "__main__":
    since_arg = None
    if len(sys.argv) > 2 and sys.argv[1] == "--since":
        since_arg = float(sys.argv[2])
    print_report(load_entries(LLM_USAGE_LOG_PATH, since=since_arg))
