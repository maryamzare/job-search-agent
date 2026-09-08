"""Deprecated. Resume generation no longer has a bypass entry point.

This script used to run scoring for one job and then tailor its resume with no
authoritative-profile check and no validation. Resume generation now goes
through a single validated path only.
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("run_job.py has been retired.")
print()
print("To generate a resume for one job (validated, single .docx):")
print("  python3 main.py resume-list                 # eligible job IDs (read-only)")
print("  python3 main.py resume-one <job-id>         # generate ONE resume")
print()
print("To score newly discovered jobs:  python3 main.py score")
sys.exit(2)
