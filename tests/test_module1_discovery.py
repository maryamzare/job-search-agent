"""
Tests for modules.module1_discovery._fetch_linkedin_description's job-ID
extraction: LinkedIn serves both bare-ID URLs (.../view/1234567890/) and
slugged URLs (.../view/some-title-1234567890) from its search results, and
the regex must extract the ID from either shape. A bug that matched digits
only immediately after "/view/" silently returned "" (no exception, no
error - see modules.util.parse_llm_json-style graceful degradation) for
every slugged URL, which is most of what LinkedIn's search actually
returns - discovered when 28 of 36 board-approved/shortlisted jobs turned
out to have an empty description field, which fed an empty job posting
into resume tailoring and resume board review with no error at any stage.

No real network calls - requests.get is mocked throughout.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.module1_discovery import _fetch_linkedin_description

DESCRIPTION_HTML = (
    '<div class="show-more-less-html__markup">Real job description text.</div>'
)


def _fake_response(html: str, status_code: int = 200):
    response = MagicMock()
    response.status_code = status_code
    response.text = html
    return response


class TestFetchLinkedinDescriptionIdExtraction(unittest.TestCase):
    def test_slugged_url_extracts_trailing_id_and_fetches(self):
        # The bug case: a title slug sits between "/view/" and the numeric ID.
        with patch("modules.module1_discovery.requests.get", return_value=_fake_response(DESCRIPTION_HTML)) as mock_get:
            result = _fetch_linkedin_description(
                "https://www.linkedin.com/jobs/view/technical-program-manager-at-meta-4425376195"
            )
        self.assertEqual(result, "Real job description text.")
        called_url = mock_get.call_args.args[0]
        self.assertIn("4425376195", called_url)

    def test_bare_id_url_still_works(self):
        with patch("modules.module1_discovery.requests.get", return_value=_fake_response(DESCRIPTION_HTML)) as mock_get:
            result = _fetch_linkedin_description("https://www.linkedin.com/jobs/view/1234567890")
        self.assertEqual(result, "Real job description text.")
        self.assertIn("1234567890", mock_get.call_args.args[0])

    def test_bare_id_with_trailing_slash_still_works(self):
        with patch("modules.module1_discovery.requests.get", return_value=_fake_response(DESCRIPTION_HTML)) as mock_get:
            result = _fetch_linkedin_description("https://www.linkedin.com/jobs/view/1234567890/")
        self.assertEqual(result, "Real job description text.")
        self.assertIn("1234567890", mock_get.call_args.args[0])

    def test_international_subdomain_slugged_url_works(self):
        with patch("modules.module1_discovery.requests.get", return_value=_fake_response(DESCRIPTION_HTML)):
            result = _fetch_linkedin_description(
                "https://in.linkedin.com/jobs/view/senior-technical-program-manager-at-netradyne-4445140269"
            )
        self.assertEqual(result, "Real job description text.")

    def test_url_with_no_digits_returns_empty_string_not_a_crash(self):
        result = _fetch_linkedin_description("https://www.linkedin.com/jobs/view/no-id-here")
        self.assertEqual(result, "")

    def test_non_200_response_returns_empty_string(self):
        with patch("modules.module1_discovery.requests.get", return_value=_fake_response("", status_code=404)):
            result = _fetch_linkedin_description(
                "https://www.linkedin.com/jobs/view/technical-program-manager-at-meta-4425376195"
            )
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
