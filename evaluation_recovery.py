"""
CLI for the evaluation recovery workflow. See modules/eval_recovery.py for
the implementation and ARCHITECTURE.md's "Evaluation Recovery Workflow"
section for the design rationale and scheduling instructions.

Usage:
  python3 evaluation_recovery.py check-and-run
      Check whether the account's quota has reset and, if so, re-run the
      pending live evaluation and write a before/after report. Safe to
      call repeatedly (e.g. every hour via cron) - it's a no-op until the
      reset time, and a no-op again after it succeeds once.

  python3 evaluation_recovery.py status
      Print the current recovery state without attempting anything.
"""
import asyncio
import json
import sys

from dotenv import load_dotenv
load_dotenv()

from config import JOB_QUEUE_PATH
import modules.module3b_resume_board as resume_board
from modules.eval_recovery import load_recovery_state, run_recovery_check, RECOVERY_STATE_PATH


def _live_rerun(target: dict) -> dict:
    """The real live re-run: re-run the resume board against the same
    job/resume recorded in the recovery state, and extract the same
    fields the original "before" snapshot recorded so they're directly
    comparable."""
    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)
    job = next(
        j for j in queue["jobs"]
        if j.get("company") == target["company"] and j.get("title") == target["title"]
    )
    with open(target["resume_path"]) as f:
        draft_resume = f.read()

    result = asyncio.run(resume_board.review_resume(draft_resume, job))
    scorecard = result["scorecard"]
    red_flag = result["raw_reviews"].get("red_flag_reviewer", {})
    return {
        "red_flag_verdict": red_flag.get("verdict"),
        "red_flag_score": red_flag.get("score"),
        "composite_score": scorecard.get("composite_score"),
        "ready_to_submit": scorecard.get("ready_to_submit"),
        "blocker": scorecard.get("blocker"),
    }


def cmd_status():
    state = load_recovery_state()
    if state is None:
        print("No recovery state found.")
        return
    print(json.dumps(state, indent=2))


def cmd_check_and_run():
    exit_code = run_recovery_check(RECOVERY_STATE_PATH, live_rerun_fn=_live_rerun)
    sys.exit(exit_code)


COMMANDS = {
    "status": cmd_status,
    "check-and-run": cmd_check_and_run,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
