"""
Evaluation recovery workflow.

When a live evaluation re-run (e.g. verifying a prompt fix actually changed
a reviewer's verdict) fails because the Anthropic account is over its usage
quota, this module persists everything needed to retry automatically once
the quota resets: what job/resume to re-check, what the "before" result
was, and when the account regains access (parsed straight from Anthropic's
own error message - that's the only place it's reported for this specific
error, unlike a 429 rate limit, which carries a structured retry-after
header).

Flow:
  1. A live verification attempt raises a quota-exceeded error.
  2. save_failed_eval_state(...) records the target, the "before" snapshot,
     and the parsed reset time to data/eval_recovery_state.json.
  3. Something calls run_recovery_check(...) periodically (see
     evaluation_recovery.py for the CLI, and ARCHITECTURE.md for scheduling
     options - cron or Claude Code's own scheduler).
  4. Before the reset time, it's a no-op. After it, it re-runs the same
     evaluation live, captures the "after" result, and writes a
     before/after comparison report to outputs/eval_reports/.

State machine: pending -> completed | failed. "failed" means the retry
attempt hit something other than a quota error (auth failure, code bug,
etc.) and needs a human, not another scheduled retry.
"""
import os
import re
from datetime import datetime, timezone

from modules.util import load_json, save_json

RECOVERY_STATE_PATH = "data/eval_recovery_state.json"
EVAL_REPORTS_DIR = "outputs/eval_reports"

_QUOTA_RESET_PATTERN = re.compile(
    r"regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC"
)


def parse_quota_reset_time(error_message: str):
    """Extract the quota-reset timestamp from an Anthropic error message.

    Anthropic reports this as free text inside error.message (e.g. "You
    have reached your specified API usage limits. You will regain access
    on 2026-08-01 at 00:00 UTC."), not as a structured field. Returns None
    (never a guessed fallback) if the message doesn't match this format.
    """
    match = _QUOTA_RESET_PATTERN.search(error_message)
    if not match:
        return None
    date_str, time_str = match.groups()
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def save_failed_eval_state(target: dict, before: dict, error: Exception, path: str = RECOVERY_STATE_PATH) -> dict:
    """Persist a failed live-evaluation attempt so it can be retried later.

    target: identifies what to re-run, e.g.
        {"company": "...", "title": "...", "resume_path": "..."}
    before: the pre-fix snapshot to compare the eventual re-run against.
    """
    now = datetime.now(timezone.utc)
    reset_at = parse_quota_reset_time(str(error))
    state = {
        "status": "pending",
        "created_at": now.isoformat(),
        "last_attempt_at": now.isoformat(),
        "quota_reset_at": reset_at.isoformat() if reset_at else None,
        "target": target,
        "before": before,
        "after": None,
        "report_path": None,
        "last_error": str(error),
        "attempts": 1,
    }
    save_json(state, path)
    return state


def load_recovery_state(path: str = RECOVERY_STATE_PATH):
    try:
        return load_json(path)
    except FileNotFoundError:
        return None


def _time_remaining(reset_at_iso: str) -> str:
    reset_at = datetime.fromisoformat(reset_at_iso)
    remaining = reset_at - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "0s (should be ready)"
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def generate_comparison_report(target: dict, before: dict, after: dict) -> str:
    """Build a markdown before/after comparison report."""
    lines = [
        "# Evaluation Recovery Report",
        "",
        f"**Target:** {target.get('company')} — {target.get('title')}",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "| Metric | Before | After |",
        "|---|---|---|",
    ]
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        b = before.get(key, "—")
        a = after.get(key, "—")
        changed = " **← changed**" if b != a else ""
        lines.append(f"| {key} | {b} | {a}{changed} |")
    return "\n".join(lines) + "\n"


def run_recovery_check(state_path: str = RECOVERY_STATE_PATH, live_rerun_fn=None,
                        reports_dir: str = EVAL_REPORTS_DIR) -> int:
    """Check recovery state and, if the quota has reset, re-run the
    evaluation. Designed to be called repeatedly (e.g. hourly via cron) -
    it's a no-op until the reset time, then runs exactly once.

    live_rerun_fn: callable(target: dict) -> dict, returning the "after"
    snapshot. Injected so this is unit-testable without a real API call;
    evaluation_recovery.py's CLI passes the real live-call implementation.

    Returns a process exit code: 0 for "nothing to do" or "succeeded",
    1 for "needs human attention" (an unexpected, non-quota failure).
    """
    state = load_recovery_state(state_path)
    if state is None:
        print("[recovery] No recovery state found - nothing to check.")
        return 0

    if state["status"] == "completed":
        print(f"[recovery] Already completed. Report: {state.get('report_path')}")
        return 0

    if state["status"] == "failed":
        print(f"[recovery] Previously marked failed (needs manual attention): {state.get('last_error')}")
        return 1

    reset_at_iso = state.get("quota_reset_at")
    if reset_at_iso:
        reset_at = datetime.fromisoformat(reset_at_iso)
        if datetime.now(timezone.utc) < reset_at:
            print(f"[recovery] Not ready yet - quota resets at {reset_at_iso} "
                  f"({_time_remaining(reset_at_iso)} remaining).")
            return 0

    print("[recovery] Quota should have reset - attempting live re-run...")
    state["attempts"] = state.get("attempts", 0) + 1
    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()

    try:
        after = live_rerun_fn(state["target"])
    except Exception as e:
        new_reset_at = parse_quota_reset_time(str(e))
        if new_reset_at:
            state["quota_reset_at"] = new_reset_at.isoformat()
            state["last_error"] = str(e)
            save_json(state, state_path)
            print(f"[recovery] Still over quota - new reset time {new_reset_at.isoformat()}. Will retry later.")
            return 0
        state["status"] = "failed"
        state["last_error"] = str(e)
        save_json(state, state_path)
        print(f"[recovery] Unexpected error, marking failed for manual review: {e}")
        return 1

    report = generate_comparison_report(state["target"], state["before"], after)
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(
        reports_dir, f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_recovery_report.md"
    )
    with open(report_path, "w") as f:
        f.write(report)

    state["status"] = "completed"
    state["after"] = after
    state["report_path"] = report_path
    save_json(state, state_path)
    print(f"[recovery] Success - report written to {report_path}")
    return 0
