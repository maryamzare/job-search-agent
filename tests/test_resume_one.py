"""
Safety + behavior tests for the single-job resume workflow.

No live API calls: the three model steps in modules.module3_resume
(tailor_resume, authenticity_and_format_pass, validate_semantic) are patched.
Failure-path tests assert those patches were never invoked.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util
import modules.module3_resume as m3
import main as cli

_HAS_DOCX = m3.resume_docx is not None
_needs_docx = unittest.skipUnless(_HAS_DOCX, "python-docx not installed (.venv only)")

PROFILE = """# Career Profile — Source of Truth

## 2. Education
| B.S. Computer Science & Software Engineering | University of Washington | 2017 | CONFIRMED |

## 3. Certifications

| Certification | Year | Source | Status |
|---|---|---|---|
| PMP | — | config | **CONFIRMED held** |
| Certified Scrum Master — Scrum Alliance | 2020 | `[O1][O2]` | **CONFIRMED current** |
| AI Foundations — OpenAI Academy | — | config | **CONFIRMED held** |
| Applied AI Foundations — OpenAI Academy | — | config | **CONFIRMED held** |
| Google Project Manager Certification | 2021 | `[O1][O2]` | **CONFIRMED held** |
| Google UX Design Certificate | — | `[O1][O2]` | **CONFIRMED NOT held** — never completed |
| SAFe / SAFe Program Consultant (SPC) | — | config | **CONFIRMED NOT held** |

## 4. Employment history
### 4.1 INRIX
Senior Technical Program Manager, Jun 2022 - Nov 2024.
Source quote (not a candidate claim): "Spearheaded solution design for the
GenAI Signals Data Lake."
### 4.2 IHME
Technical Program Manager, 2019 - 2022.

## 5. Metrics ledger
| M1 | 99% on-time delivery | INRIX |
| M2 | 40% cost reduction | INRIX |
| M3 | accessibility 30% to 89% | IHME |
"""

CONTACT = "name: Jane Doe\nemail: jane@example.com\nphone: (206) 555-0100\nlocation: Seattle, WA\nlinkedin: linkedin.com/in/janedoe\n"

GOOD_RESUME = {
    "summary": None,
    "experience": [
        {"title": "Senior Technical Program Manager", "company": "INRIX",
         "location": "Kirkland, WA", "start": "Jun 2022", "end": "Nov 2024",
         "current": False,
         "bullets": ["Led delivery across eight engineering teams and reached 99% on-time delivery.",
                     "Reduced platform cost by 40% while holding reliability steady."]},
        {"title": "Technical Program Manager", "company": "IHME",
         "location": "Seattle, WA", "start": "2019", "end": "2022", "current": False,
         "bullets": ["Improved accessibility from 30% to 89% by running user research with clinicians."]},
    ],
    "skills": ["Program management", "SQL", "AWS"],
    "education": [{"credential": "B.S. Computer Science & Software Engineering",
                  "institution": "University of Washington", "year": "2017"}],
    "certifications": [{"name": "PMP", "year": None},
                      {"name": "Certified Scrum Master", "year": "2020"}],
}

JOB_A = {"title": "Senior TPM", "company": "Acme", "location": "Remote",
         "url": "https://example.com/acme-1", "description": "We need a senior TPM for platform work.",
         "status": "shortlisted", "fit_score": 82}
JOB_B = {"title": "Staff TPM", "company": "Beta", "location": "Seattle, WA",
         "url": "https://example.com/beta-2", "description": "Staff TPM, infra.",
         "status": "board_approved", "fit_score": 88}
JOB_C = {"title": "Junior PM", "company": "Gamma", "location": "NYC",
         "url": "https://example.com/gamma-3", "description": "Junior.", "status": "discovered"}


@contextlib.contextmanager
def env(*jobs):
    d = tempfile.mkdtemp()
    qpath = os.path.join(d, "job_queue.json")
    with open(qpath, "w") as f:
        json.dump({"jobs": list(jobs)}, f)
    ppath = os.path.join(d, "profile.md")
    Path(ppath).write_text(PROFILE)
    cpath = os.path.join(d, "contact.txt")
    Path(cpath).write_text(CONTACT)
    patches = {
        "JOB_QUEUE_PATH": qpath,
        "CAREER_PROFILE_PATH": ppath,
        "CONTACT_BLOCK_PATH": cpath,
        "RESUME_OUTPUT_DIR": os.path.join(d, "tailored_resumes"),
        "RESUME_REVIEW_DIR": os.path.join(d, "resume_reviews"),
        "RESUME_REVIEW_RESOLVED_DIR": os.path.join(d, "resume_reviews", "resolved"),
        "RESUME_BUILD_TMP_DIR": os.path.join(d, "tailored_resumes", ".build"),
        "RESUME_SUPERSEDED_DIR": os.path.join(d, "tailored_resumes", "superseded"),
    }
    with contextlib.ExitStack() as st:
        for k, v in patches.items():
            st.enter_context(patch.object(m3, k, v))
        orig_stage = util.PIPELINE_STAGE_LOG_PATH
        util.PIPELINE_STAGE_LOG_PATH = os.path.join(d, "stage.jsonl")
        try:
            yield d, patches
        finally:
            util.PIPELINE_STAGE_LOG_PATH = orig_stage


def fake_models(resume_obj=GOOD_RESUME, sem_issues=None):
    """Returns a dict of patches for the three model steps."""
    return {
        "tailor_resume": MagicMock(return_value=json.dumps(resume_obj)),
        "authenticity_and_format_pass": MagicMock(side_effect=lambda r, p: r),
        "validate_semantic": MagicMock(return_value=list(sem_issues or [])),
    }


def run_cli(argv):
    """Invoke a main.COMMANDS entry with argv (list after 'main.py'). Returns
    (exit_code, stdout)."""
    buf = io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["main.py"] + argv), contextlib.redirect_stdout(buf):
        try:
            cli.COMMANDS[argv[0]]()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, buf.getvalue()


def jid(job):
    return util.job_artifact_key(job)


class TestBareResumeAndRun(unittest.TestCase):
    def test_bare_resume_generates_nothing(self):
        fm = fake_models()
        with patch.multiple(m3, **fm):
            code, out = run_cli(["resume"])
        self.assertEqual(code, 2)
        self.assertIn("resume-one", out)
        fm["tailor_resume"].assert_not_called()

    def test_run_stops_before_resume_generation(self):
        with patch.object(cli, "cmd_discover", MagicMock()), \
             patch.object(cli, "cmd_score", MagicMock()), \
             patch.object(m3, "generate_resume_for_job", MagicMock()) as gen, \
             patch.object(m3, "tailor_resume", MagicMock()) as tr:
            code, out = run_cli(["run"])
        self.assertIn("resume-one", out)
        gen.assert_not_called()
        tr.assert_not_called()


class TestJobIdFailuresNoApi(unittest.TestCase):
    def test_missing_job_id(self):
        fm = fake_models()
        with env(JOB_A), patch.multiple(m3, **fm):
            code, out = run_cli(["resume-one"])
        self.assertEqual(code, 2)
        fm["tailor_resume"].assert_not_called()
        fm["validate_semantic"].assert_not_called()

    def test_invalid_job_id(self):
        fm = fake_models()
        with env(JOB_A), patch.multiple(m3, **fm):
            code, out = run_cli(["resume-one", "no-such-id-xxxx"])
        self.assertEqual(code, 2)
        self.assertIn("no job with id", out)
        fm["tailor_resume"].assert_not_called()

    def test_ineligible_job_id(self):
        fm = fake_models()
        with env(JOB_C), patch.multiple(m3, **fm):
            code, out = run_cli(["resume-one", jid(JOB_C)])
        self.assertEqual(code, 2)
        self.assertIn("status 'discovered'", out)
        fm["tailor_resume"].assert_not_called()

    def test_ambiguous_job_id(self):
        # two identical queue entries -> identical job_artifact_key
        dup = dict(JOB_A)
        fm = fake_models()
        with env(JOB_A, dup), patch.multiple(m3, **fm):
            code, out = run_cli(["resume-one", jid(JOB_A)])
        self.assertEqual(code, 2)
        self.assertIn("matches 2", out)
        fm["tailor_resume"].assert_not_called()


class TestCertificationMatching(unittest.TestCase):
    """Approved cert display names match regardless of the separator between
    the cert and its issuer; unapproved / NOT-held certs still reject."""

    def _issues(self, certs):
        r = {
            "summary": None,
            "experience": [{"title": "Technical Program Manager", "company": "IHME",
                            "location": "Seattle, WA", "start": "2019", "end": "2022",
                            "current": False, "bullets": ["Ran the program and shipped it."]}],
            "skills": ["SQL"],
            "education": [{"credential": "B.S. Computer Science & Software Engineering",
                          "institution": "University of Washington", "year": "2017"}],
            "certifications": certs,
        }
        contact = {"name": "Jane Doe", "location": "Seattle, WA", "email": "", "phone": "",
                   "linkedin": "", "github": "", "website": ""}
        return [i for i in m3.validate_deterministic(
            r, contact, PROFILE, {"title": "x", "company": "y", "description": "d"},
            include_summary=False) if "certification" in i]

    def test_approved_names_with_various_separators_pass(self):
        for nm in [
            "Certified Scrum Master, Scrum Alliance",
            "Certified Scrum Master – Scrum Alliance",
            "Certified Scrum Master — Scrum Alliance",
            "Certified Scrum Master | Scrum Alliance",
            "Certified Scrum Master (Scrum Alliance)",
            "Certified Scrum Master",
            "AI Foundations, OpenAI Academy",
            "AI Foundations | OpenAI Academy",
            "Applied AI Foundations (OpenAI Academy)",
            "PMP",
            "Google Project Manager Certification",
        ]:
            self.assertEqual(self._issues([{"name": nm, "year": None}]), [], nm)

    def test_csm_year_still_checked(self):
        issues = self._issues([{"name": "Certified Scrum Master, Scrum Alliance", "year": "2019"}])
        self.assertTrue(any("year" in i for i in issues))
        ok = self._issues([{"name": "Certified Scrum Master, Scrum Alliance", "year": "2020"}])
        self.assertEqual(ok, [])

    def test_not_held_certs_still_rejected(self):
        for nm in ["SAFe Program Consultant", "SAFe", "Google UX Design Certificate",
                   "Google UX Design"]:
            issues = self._issues([{"name": nm, "year": None}])
            self.assertTrue(any("NOT held" in i for i in issues), nm)

    def test_unapproved_cert_still_rejected(self):
        issues = self._issues([{"name": "AWS Certified Solutions Architect", "year": None}])
        self.assertTrue(any("not an approved certification" in i for i in issues))


class TestSpearheadedAlwaysBanned(unittest.TestCase):
    """"Spearheaded" must be blocked in every bullet, regardless of case, even
    though the fixture PROFILE above quotes an old resume using that exact
    word as source material for a fact (mirroring career_profile_source_of_
    truth.md §4.1). That quote documents what an old resume said -- it is
    not permission to reuse the word, and the general BANNED_PHRASES
    exemption (a phrase is allowed if it appears anywhere in the profile
    text) must not swallow this one."""

    def _issues_for_bullet(self, bullet):
        r = {
            "summary": None,
            "experience": [{"title": "Technical Program Manager", "company": "IHME",
                            "location": "Seattle, WA", "start": "2019", "end": "2022",
                            "current": False, "bullets": [bullet]}],
            "skills": ["SQL"],
            "education": [{"credential": "B.S. Computer Science & Software Engineering",
                          "institution": "University of Washington", "year": "2017"}],
            "certifications": [{"name": "PMP", "year": None}],
        }
        contact = {"name": "Jane Doe", "location": "Seattle, WA", "email": "", "phone": "",
                   "linkedin": "", "github": "", "website": ""}
        return m3.validate_deterministic(r, contact, PROFILE, {"title": "x", "company": "y",
                                         "description": "d"}, include_summary=False)

    def test_lowercase_is_blocked(self):
        issues = self._issues_for_bullet("Spearheaded the migration across three teams.".lower())
        self.assertTrue(any("spearheaded" in i for i in issues), issues)

    def test_capitalized_is_blocked(self):
        issues = self._issues_for_bullet("Spearheaded the migration across three teams.")
        self.assertTrue(any("spearheaded" in i for i in issues), issues)

    def test_allcaps_is_blocked(self):
        issues = self._issues_for_bullet("SPEARHEADED the migration across three teams.")
        self.assertTrue(any("spearheaded" in i for i in issues), issues)

    def test_blocked_even_though_profile_quotes_it_as_source_material(self):
        # The word literally appears in PROFILE's §4.1 (see module docstring
        # above) -- confirming that presence alone does not exempt it, unlike
        # the general BANNED_PHRASES list.
        self.assertIn("spearheaded", PROFILE.lower())
        issues = self._issues_for_bullet("Spearheaded delivery of the new platform.")
        self.assertTrue(any("spearheaded" in i for i in issues), issues)


class TestDashValidation(unittest.TestCase):
    """Em dash always fails. En dash is fine inside a numeric/date range but
    fails as sentence punctuation."""

    def _issues_for_bullet(self, bullet):
        r = {
            "summary": None,
            "experience": [{"title": "Technical Program Manager", "company": "IHME",
                            "location": "Seattle, WA", "start": "2019", "end": "2022",
                            "current": False, "bullets": [bullet]}],
            "skills": ["SQL"],
            "education": [{"credential": "B.S. Computer Science & Software Engineering",
                          "institution": "University of Washington", "year": "2017"}],
            "certifications": [{"name": "PMP", "year": None}],
        }
        contact = {"name": "Jane Doe", "location": "Seattle, WA", "email": "", "phone": "",
                   "linkedin": "", "github": "", "website": ""}
        return m3.validate_deterministic(r, contact, PROFILE, {"title": "x", "company": "y",
                                         "description": "d"}, include_summary=False)

    def test_em_dash_is_flagged(self):
        issues = self._issues_for_bullet("Ran delivery across teams — and shipped on time.")
        self.assertTrue(any("em dash" in i for i in issues))

    def test_en_dash_in_numeric_range_is_allowed(self):
        issues = self._issues_for_bullet("Improved accessibility 30–89% through user research.")
        self.assertFalse(any("dash" in i for i in issues), issues)

    def test_en_dash_in_date_range_is_allowed(self):
        issues = self._issues_for_bullet("Owned the 2019–2022 modernization roadmap end to end.")
        self.assertFalse(any("dash" in i for i in issues), issues)

    def test_en_dash_as_punctuation_is_flagged(self):
        issues = self._issues_for_bullet("Ran delivery across teams – and shipped on time.")
        self.assertTrue(any("en dash used as punctuation" in i for i in issues))


class TestDoNotCombineValidation(unittest.TestCase):
    """Regression coverage for the Databricks Deployment Strategist failure
    (2026-09-05): a generated bullet merged M2 (UK Signals system
    performance, 54.6% -> 99%) and M3 (UK signal analytics accuracy, 99%)
    into one bullet, softened with "separately," and the deterministic
    validator correctly blocked it. These tests lock that behavior in.
    Do NOT add an M2/M3 exception to DO_NOT_COMBINE to make any of these pass."""

    def _issues_for_bullet(self, bullet, company="INRIX"):
        r = {
            "summary": None,
            "experience": [{"title": "Senior Technical Program Manager", "company": company,
                            "location": "Kirkland, WA", "start": "Jun 2022", "end": "Nov 2024",
                            "current": False, "bullets": [bullet]}],
            "skills": ["SQL"],
            "education": [{"credential": "B.S. Computer Science & Software Engineering",
                          "institution": "University of Washington", "year": "2017"}],
            "certifications": [{"name": "PMP", "year": None}],
        }
        contact = {"name": "Jane Doe", "location": "Seattle, WA", "email": "", "phone": "",
                   "linkedin": "", "github": "", "website": ""}
        return m3.validate_deterministic(r, contact, PROFILE, {"title": "x", "company": "y",
                                         "description": "d"}, include_summary=False)

    def test_m2xm3_merge_is_blocked(self):
        # The exact shape of bullet the pipeline generated and blocked.
        bullet = ("Defined and executed three critical milestones for UK Signals, "
                  "bringing system performance from 54.6% to 99%; separately, "
                  "delivered 99% accuracy on UK signal analytics.")
        issues = self._issues_for_bullet(bullet)
        self.assertTrue(any("M2xM3" in i for i in issues), issues)

    def test_separately_wording_does_not_excuse_the_merge(self):
        # "Separately" (or any other connector) does not create a loophole:
        # both metric substrings landing in one bullet string is what trips
        # the check, regardless of the words used to join them.
        for connector in ("separately,", "in addition,", "also,", ";", "and"):
            bullet = (f"System performance rose from 54.6% to 99% {connector} "
                      f"accuracy reached 99% on UK signal analytics.")
            issues = self._issues_for_bullet(bullet)
            self.assertTrue(any("M2xM3" in i for i in issues), (connector, issues))

    def test_m2_alone_is_fine(self):
        bullet = "Defined and executed three milestones, bringing system performance from 54.6% to 99%."
        issues = self._issues_for_bullet(bullet)
        self.assertFalse(any("M2xM3" in i for i in issues), issues)

    def test_m3_alone_is_fine(self):
        bullet = "Delivered 99% accuracy on UK signal analytics."
        issues = self._issues_for_bullet(bullet)
        self.assertFalse(any("M2xM3" in i for i in issues), issues)

    def test_m9xm10_still_requires_s5_solutions_company(self):
        bullet = "Led a team of 6 engineers and drove a 40% improvement in completion speed."
        issues_wrong_company = self._issues_for_bullet(bullet, company="INRIX")
        issues_right_company = self._issues_for_bullet(bullet, company="S5 Solutions")
        self.assertTrue(any("M9xM10" in i for i in issues_wrong_company), issues_wrong_company)
        self.assertFalse(any("M9xM10" in i for i in issues_right_company), issues_right_company)

    def test_m11_influence_combined_with_direct_reports_is_blocked(self):
        bullet = "Mentored and guided 50+ cross-functional partners while owning 3 direct reports."
        issues = self._issues_for_bullet(bullet)
        self.assertTrue(any("M11xM12_13" in i for i in issues), issues)


class TestPromptGuardrailsForKnownFailures(unittest.TestCase):
    """The Databricks Deployment Strategist run (2026-09-05) was blocked for
    an em dash and an M2/M3 merge disguised with "separately." These
    assertions make sure the prompt language added to prevent a repeat can't
    be silently dropped later. No API calls."""

    def test_tailor_prompt_bans_em_dash_explicitly(self):
        self.assertIn("No em dashes", m3.TAILOR_SYSTEM_PROMPT)
        self.assertIn(m3.EM_DASH, m3.TAILOR_SYSTEM_PROMPT)

    def test_tailor_prompt_names_m2_m3_and_rejects_separately(self):
        prompt = m3.TAILOR_SYSTEM_PROMPT
        self.assertIn("M2", prompt)
        self.assertIn("M3", prompt)
        self.assertIn("separately", prompt.lower())

    def test_authenticity_prompt_bans_em_dash_explicitly(self):
        self.assertIn("No em dashes", m3.AUTHENTICITY_SYSTEM_PROMPT)
        self.assertIn(m3.EM_DASH, m3.AUTHENTICITY_SYSTEM_PROMPT)

    def test_authenticity_prompt_names_m2_m3_and_rejects_separately(self):
        prompt = m3.AUTHENTICITY_SYSTEM_PROMPT
        self.assertIn("M2", prompt)
        self.assertIn("M3", prompt)
        self.assertIn("separately", prompt.lower())

    def test_spearheaded_is_split_out_of_the_exemptible_list(self):
        self.assertIn("spearheaded", m3.ALWAYS_BANNED_PHRASES)
        self.assertNotIn("spearheaded", m3.BANNED_PHRASES)

    def test_tailor_and_authenticity_prompts_ban_spearheaded_with_no_exception(self):
        for prompt in (m3.TAILOR_SYSTEM_PROMPT, m3.AUTHENTICITY_SYSTEM_PROMPT):
            self.assertIn("spearheaded", prompt.lower())
            self.assertIn("no exception", prompt.lower())

    def test_tailor_prompt_permits_trimming_less_relevant_bullets(self):
        prompt = m3.TAILOR_SYSTEM_PROMPT.lower()
        self.assertIn("prioritize relevance", prompt)

    def test_tailor_prompt_grounds_summary_in_profile_only(self):
        prompt = m3.TAILOR_SYSTEM_PROMPT.lower()
        self.assertIn("no job-description language", prompt)


class TestResumeListReadOnly(unittest.TestCase):
    def test_lists_eligible_only_and_writes_nothing(self):
        with env(JOB_A, JOB_B, JOB_C) as (d, p):
            qpath = p["JOB_QUEUE_PATH"]
            before = os.stat(qpath).st_mtime_ns
            with patch.object(m3, "tracked_create", MagicMock(side_effect=AssertionError("no API"))):
                code, out = run_cli(["resume-list"])
            after = os.stat(qpath).st_mtime_ns
        self.assertEqual(code, 0)
        self.assertIn(jid(JOB_A), out)
        self.assertIn(jid(JOB_B), out)
        self.assertNotIn(jid(JOB_C), out)          # discovered -> not eligible
        self.assertEqual(before, after)             # queue file untouched


@_needs_docx
class TestExactlyOneJobProcessed(unittest.TestCase):
    def test_only_the_resolved_job_is_tailored(self):
        fm = fake_models()
        with env(JOB_A, JOB_B, JOB_C), patch.multiple(m3, **fm):
            code, out = run_cli(["resume-one", jid(JOB_B)])
        self.assertEqual(code, 0, out)
        self.assertEqual(fm["tailor_resume"].call_count, 1)
        passed_job = fm["tailor_resume"].call_args.args[0]
        self.assertEqual(passed_job["company"], "Beta")


@_needs_docx
class TestSummaryOmittedByDefault(unittest.TestCase):
    def test_default_has_no_summary_heading(self):
        with env(JOB_A), patch.multiple(m3, **fake_models()):
            code, out = run_cli(["resume-one", jid(JOB_A)])
        self.assertEqual(code, 0, out)
        from docx import Document
        headings = [pp.text.strip().lower() for pp in Document(
            out_path_from(out)).paragraphs]
        self.assertNotIn("summary", headings)

    def test_with_summary_flag_adds_the_section(self):
        resume_with_summary = dict(GOOD_RESUME, summary="Ten years leading platform programs.")
        with env(JOB_A), patch.multiple(m3, **fake_models(resume_with_summary)):
            code, out = run_cli(["resume-one", jid(JOB_A), "--with-summary"])
        self.assertEqual(code, 0, out)
        from docx import Document
        headings = [pp.text.strip().lower() for pp in Document(out_path_from(out)).paragraphs]
        self.assertIn("summary", headings)


@_needs_docx
class TestFailedValidationWritesReviewNotResume(unittest.TestCase):
    def test_deterministic_failure_blocks_and_reports(self):
        bad = dict(GOOD_RESUME, certifications=GOOD_RESUME["certifications"] +
                   [{"name": "SAFe Program Consultant", "year": None}])
        fm = fake_models(bad)
        with env(JOB_A), patch.multiple(m3, **fm) as _:
            code, out = run_cli(["resume-one", jid(JOB_A)])
            review = os.path.join(m3.RESUME_REVIEW_DIR, f"{jid(JOB_A)}.review.md")
            final = os.path.join(m3.RESUME_OUTPUT_DIR, f"{jid(JOB_A)}.docx")
            self.assertEqual(code, 3)
            self.assertTrue(os.path.exists(review))
            self.assertFalse(os.path.exists(final))
        fm["validate_semantic"].assert_not_called()   # deterministic failed first


class TestNoSilentOverwrite(unittest.TestCase):
    def test_existing_final_is_not_overwritten(self):
        fm = fake_models()
        with env(JOB_A), patch.multiple(m3, **fm):
            os.makedirs(m3.RESUME_OUTPUT_DIR, exist_ok=True)
            final = os.path.join(m3.RESUME_OUTPUT_DIR, f"{jid(JOB_A)}.docx")
            Path(final).write_bytes(b"EXISTING")
            code, out = run_cli(["resume-one", jid(JOB_A)])
            self.assertEqual(code, 2)
            self.assertIn("--replace", out)
            self.assertEqual(Path(final).read_bytes(), b"EXISTING")
        fm["tailor_resume"].assert_not_called()


class TestNoLegacyBypass(unittest.TestCase):
    def test_module3_has_no_unvalidated_save_helpers(self):
        self.assertFalse(hasattr(m3, "tailor_and_save"))
        self.assertFalse(hasattr(m3, "save_tailored_resume"))

    def test_module3_module_exec_exits_with_instructions(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run([sys.executable, "-m", "modules.module3_resume"],
                           cwd=root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("resume-one", r.stdout)

    def test_module3_direct_file_exec_cannot_run(self):
        # `python modules/module3_resume.py` can't even import config; it
        # certainly can't generate a resume.
        import subprocess
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run([sys.executable, "modules/module3_resume.py"],
                           cwd=root, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_run_job_is_retired(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run([sys.executable, "run_job.py", "Acme", "TPM"],
                           cwd=root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("resume-one", r.stdout)


@_needs_docx
class TestTempCleanupAfterFailure(unittest.TestCase):
    def test_render_crash_leaves_no_partial_file(self):
        fm = fake_models()
        with env(JOB_A), patch.multiple(m3, **fm), \
             patch.object(m3.resume_docx, "render", MagicMock(side_effect=RuntimeError("boom"))):
            code, out = run_cli(["resume-one", jid(JOB_A)])
            build_dir = m3.RESUME_BUILD_TMP_DIR
            leftovers = os.listdir(build_dir) if os.path.isdir(build_dir) else []
            final = os.path.join(m3.RESUME_OUTPUT_DIR, f"{jid(JOB_A)}.docx")
            self.assertEqual(code, 1)
            self.assertEqual(leftovers, [])
            self.assertFalse(os.path.exists(final))


def out_path_from(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("Saved: "):
            return line[len("Saved: "):].strip()
    raise AssertionError(f"no Saved: line in output:\n{stdout}")


if __name__ == "__main__":
    unittest.main()
