"""
Performance baseline metrics.

Turns the two raw logs the pipeline already writes into per-module
summaries suitable for a cost/latency baseline:

  - data/llm_usage_log.jsonl     (written by util.tracked_create/
    tracked_create_async): one entry per Claude API call - latency,
    tokens, estimated cost.
  - data/pipeline_stage_log.jsonl (written by util.track_stage): one
    entry per pipeline-stage unit of work (e.g. scoring one job,
    tailoring one resume) - wall-clock duration, independent of how many
    LLM calls that stage made internally.

Both are append-only JSONL logs read directly off disk - there is no
database, no server, nothing to stand up. This module only aggregates;
it never calls the API and never writes to either log itself (see
modules/util.py for the writers).
"""
import statistics

# Maps a tracked_create/tracked_create_async `label` to the module that
# issued the call, so API usage can be rolled up "by module" the same way
# stage timing already is. Kept here (not in util.py) since it's
# reporting-only knowledge, not something the instrumentation itself needs.
_LABEL_TO_MODULE = {
    "score_job": "module2_scoring",
    "tailor_resume": "module3_resume",
    "generate_cover_letter": "module4_coverletter",
}


def module_for_label(label: str) -> str:
    """Map a tracked_create/tracked_create_async label to its module."""
    if label in _LABEL_TO_MODULE:
        return _LABEL_TO_MODULE[label]
    if label.startswith("board_review"):
        return "module2b_board_review"
    if label.startswith("resume_board"):
        return "module3b_resume_board"
    return "unknown"


def compute_api_usage_by_module(usage_log_entries: list) -> dict:
    """Aggregate util.tracked_create/tracked_create_async log entries by
    module: number of calls, input/output/total tokens, and estimated
    cost. Entries with no usage recorded (a failed call) are counted
    toward `calls` and `failures` but contribute zero tokens/cost, since
    a failed call has no response.usage to read.
    """
    by_module = {}
    for entry in usage_log_entries:
        module = module_for_label(entry.get("label", ""))
        row = by_module.setdefault(module, {
            "calls": 0,
            "failures": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        })
        row["calls"] += 1
        if not entry.get("success", True):
            row["failures"] += 1
            continue
        input_tokens = entry.get("input_tokens") or 0
        output_tokens = entry.get("output_tokens") or 0
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens
        row["total_tokens"] += input_tokens + output_tokens
        row["cost_usd"] += entry.get("cost_usd") or 0.0

    for row in by_module.values():
        row["cost_usd"] = round(row["cost_usd"], 6)

    return by_module


def compute_stage_timing_by_module(stage_log_entries: list) -> dict:
    """Aggregate util.track_stage log entries by stage (module): number
    of runs, average/median/total duration, and how many failed.
    """
    by_stage = {}
    for entry in stage_log_entries:
        stage = entry.get("stage", "unknown")
        by_stage.setdefault(stage, {"durations": [], "failures": 0})
        by_stage[stage]["durations"].append(entry.get("duration_s", 0.0))
        if not entry.get("success", True):
            by_stage[stage]["failures"] += 1

    summary = {}
    for stage, data in by_stage.items():
        durations = data["durations"]
        summary[stage] = {
            "runs": len(durations),
            "failures": data["failures"],
            "avg_duration_s": round(statistics.mean(durations), 3) if durations else None,
            "median_duration_s": round(statistics.median(durations), 3) if durations else None,
            "total_duration_s": round(sum(durations), 3),
        }
    return summary


def slowest_modules(stage_timing_summary: dict, by: str = "avg_duration_s") -> list:
    """Stage names sorted by `by` descending (slowest first). Stages with
    no runs (avg is None) sort last rather than crashing the comparison.
    """
    def sort_key(item):
        _, data = item
        value = data.get(by)
        return (value is None, -(value or 0))

    return [stage for stage, _ in sorted(stage_timing_summary.items(), key=sort_key)]


def rough_token_estimate(text: str) -> int:
    """A rough, non-authoritative token-count approximation (chars / 4),
    for structural cost-planning discussion only - NOT a substitute for
    a real count (client.messages.count_tokens()) or for the actual
    measured usage in data/llm_usage_log.jsonl. Anthropic's real tokenizer
    is not a fixed chars-per-token ratio; treat this as order-of-magnitude
    only.
    """
    return max(0, len(text) // 4)
