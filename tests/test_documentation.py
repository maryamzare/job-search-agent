"""
Documentation consistency tests.

Not testing prose content (that's a human judgment call), just the
structural facts that are cheap to get wrong and easy to verify: the
evaluation report exists and isn't empty, README actually links to it
rather than just mentioning its name, and the core docs cross-reference
each other rather than silently drifting apart.

Run: python3 -m unittest discover -s tests -v
"""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

README = REPO_ROOT / "README.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
EVALUATION_REPORT = REPO_ROOT / "docs" / "EVALUATION_REPORT.md"


class TestCoreDocsExist(unittest.TestCase):
    def test_all_core_docs_exist_and_are_nonempty(self):
        for path in (README, ARCHITECTURE, CHANGELOG, EVALUATION_REPORT):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"{path} does not exist")
                self.assertGreater(path.stat().st_size, 0, f"{path} is empty")


class TestReadmeLinksToEvaluationReport(unittest.TestCase):
    def test_readme_contains_a_markdown_link_to_the_report(self):
        readme_text = README.read_text()
        self.assertIn(
            "docs/EVALUATION_REPORT.md", readme_text,
            "README.md should link to docs/EVALUATION_REPORT.md, not just describe it elsewhere",
        )

    def test_link_is_a_real_markdown_link_not_just_a_bare_mention(self):
        readme_text = README.read_text()
        self.assertIn("(docs/EVALUATION_REPORT.md)", readme_text)


class TestEvaluationReportCrossReferences(unittest.TestCase):
    def test_report_has_all_six_required_sections(self):
        text = EVALUATION_REPORT.read_text()
        required_sections = [
            "Executive Summary",
            "Before vs. After Metrics",
            "AI Agent Architecture Evaluation",
            "Engineering Decisions",
            "Current Limitations",
            "Future Roadmap",
        ]
        for section in required_sections:
            with self.subTest(section=section):
                self.assertIn(section, text, f"missing required section: {section}")

    def test_report_references_architecture_and_changelog(self):
        text = EVALUATION_REPORT.read_text()
        self.assertIn("ARCHITECTURE.md", text)
        self.assertIn("CHANGELOG.md", text)


if __name__ == "__main__":
    unittest.main()
