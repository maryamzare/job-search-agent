"""
Per-source tests for modules.module1_discovery.

Fixtures are shaped to the *live* responses observed 2026-09 (Apple's search
+ detail hydration JSON, Oracle's list + per-requisition detail endpoints,
Ashby's job-board API). Beyond the happy path they cover the review findings:
Apple's obsolete endpoint replacement, Oracle's full description fields,
URL-based deduplication, the location-matcher false positives, pagination,
and missing-ID rejection. A final test proves one source raising does not
stop the others.

No real network: requests.get / requests.post are patched throughout.

Run: python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util
import modules.module1_discovery as d


def resp(*, status=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if json_data is not None:
        r.json = MagicMock(return_value=json_data)
    else:
        r.json = MagicMock(side_effect=json.JSONDecodeError("no json", "", 0))
    return r


def apple_html(obj: dict) -> str:
    """Build a page carrying window.__staticRouterHydrationData = JSON.parse("...")."""
    return "window.__staticRouterHydrationData = JSON.parse(" + json.dumps(json.dumps(obj)) + ");"


# --------------------------------------------------------------------------- #
# Location matcher — finding 4 (no bare "us" substring)
# --------------------------------------------------------------------------- #
class TestLocationMatcher(unittest.TestCase):
    def test_accepts_us_forms(self):
        for loc in ["Houston, TX, United States", "Austin, TX", "Seattle, WA",
                    "Greater Seattle Area", "Remote - US", "Remote, USA",
                    "New York, NY, United States", ""]:
            self.assertTrue(d._location_matches(loc), loc)

    def test_rejects_non_us_that_merely_contains_the_letters_us(self):
        for loc in ["Sydney, NSW, Australia", "Minsk, Belarus", "London, UK",
                    "Bengaluru, Karnataka, India", "Prussia Street, Dublin"]:
            self.assertFalse(d._location_matches(loc), loc)

    def test_country_field_overrides_ambiguous_city(self):
        self.assertFalse(d._location_matches("San Francisco"))
        self.assertTrue(d._location_matches("San Francisco", country="United States"))
        self.assertTrue(d._location_matches("anything", country="US"))


# --------------------------------------------------------------------------- #
# deduplicate — finding 3 (canonical URL is the identity)
# --------------------------------------------------------------------------- #
class TestDeduplicate(unittest.TestCase):
    def test_same_title_different_url_are_both_kept(self):
        jobs = [
            {"company": "Oracle", "title": "Program Manager", "url": "https://careers.oracle.com/job/1"},
            {"company": "Oracle", "title": "Program Manager", "url": "https://careers.oracle.com/job/2"},
        ]
        self.assertEqual(len(d.deduplicate(jobs)), 2)

    def test_identical_url_collapses(self):
        jobs = [{"company": "A", "title": "TPM", "url": "https://x/1"},
                {"company": "A", "title": "TPM", "url": "https://x/1"}]
        self.assertEqual(len(d.deduplicate(jobs)), 1)

    def test_url_less_records_fall_back_to_company_title_description(self):
        jobs = [{"company": "A", "title": "TPM", "description": "same"},
                {"company": "A", "title": "TPM", "description": "same"},
                {"company": "A", "title": "TPM", "description": "different"}]
        self.assertEqual(len(d.deduplicate(jobs)), 2)


# --------------------------------------------------------------------------- #
# Greenhouse — Anthropic
# --------------------------------------------------------------------------- #
GH_PAYLOAD = {"jobs": [
    {"title": "Technical Program Manager, Inference",
     "location": {"name": "Seattle, WA"},
     "absolute_url": "https://job-boards.greenhouse.io/anthropic/jobs/111",
     "content": "&lt;p&gt;Own the inference roadmap.&lt;/p&gt;"},
    {"title": "Chef", "location": {"name": "Seattle, WA"},
     "absolute_url": "https://x/222", "content": "cook"},
    {"title": "Program Manager, Data Center", "location": {"name": "Tokyo, Japan"},
     "absolute_url": "https://x/333", "content": "not US/remote"},
    {"title": "Program Manager, No URL", "location": {"name": "Remote"},
     "absolute_url": "", "content": "x"},
]}


class TestGreenhouseAnthropic(unittest.TestCase):
    def test_filters_and_captures(self):
        with patch.object(d.requests, "get", return_value=resp(json_data=GH_PAYLOAD)):
            jobs = d.search_jobs_greenhouse("anthropic", "Anthropic")
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["company"], "Anthropic")
        self.assertEqual(j["url"], "https://job-boards.greenhouse.io/anthropic/jobs/111")
        self.assertIn("inference roadmap", j["description"].lower())

    def test_http_error_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp(status=500)):
            self.assertEqual(d.search_jobs_greenhouse("anthropic", "Anthropic"), [])

    def test_bad_json_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp()):
            self.assertEqual(d.search_jobs_greenhouse("anthropic", "Anthropic"), [])


# --------------------------------------------------------------------------- #
# Ashby — OpenAI  (finding 4: city-only location + country field)
# --------------------------------------------------------------------------- #
ASHBY_PAYLOAD = {"jobs": [
    {"title": "Technical Program Manager, Compute",
     "location": "San Francisco", "isRemote": False,
     "address": {"postalAddress": {"addressCountry": "United States"}},
     "descriptionPlain": "Coordinate compute launches end to end.",
     "jobUrl": "https://jobs.ashbyhq.com/openai/abc-123",
     "applyUrl": "https://jobs.ashbyhq.com/openai/abc-123/application"},
    {"title": "Program Manager, Anywhere", "location": "Planet Earth", "isRemote": True,
     "address": {}, "descriptionHtml": "<p>Fully <b>remote</b> role.</p>",
     "jobUrl": "https://jobs.ashbyhq.com/openai/rem"},
    {"title": "Product Manager, EU", "location": "London",
     "address": {"postalAddress": {"addressCountry": "United Kingdom"}},
     "descriptionPlain": "non-US", "jobUrl": "https://jobs.ashbyhq.com/openai/eu"},
    {"title": "Program Manager, No URL", "location": "Remote",
     "address": {}, "descriptionPlain": "x", "jobUrl": "", "applyUrl": ""},
]}


class TestAshbyOpenAI(unittest.TestCase):
    def test_city_only_location_accepted_via_country_field(self):
        with patch.object(d.requests, "get", return_value=resp(json_data=ASHBY_PAYLOAD)):
            jobs = d.search_jobs_ashby("openai", "OpenAI")
        titles = [j["title"] for j in jobs]
        self.assertIn("Technical Program Manager, Compute", titles)   # SF + country US
        self.assertIn("Program Manager, Anywhere", titles)            # isRemote
        self.assertNotIn("Product Manager, EU", titles)               # UK
        self.assertNotIn("Program Manager, No URL", titles)           # no URL

    def test_html_description_flattened(self):
        with patch.object(d.requests, "get", return_value=resp(json_data=ASHBY_PAYLOAD)):
            jobs = d.search_jobs_ashby("openai", "OpenAI")
        rem = next(j for j in jobs if j["title"] == "Program Manager, Anywhere")
        self.assertNotIn("<", rem["description"])
        self.assertIn("remote", rem["description"].lower())

    def test_http_error_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp(status=404)):
            self.assertEqual(d.search_jobs_ashby("openai", "OpenAI"), [])


# --------------------------------------------------------------------------- #
# Apple Careers — finding 1 (search-page hydration), 5 (pagination),
# 6 (missing id), full description from the detail page
# --------------------------------------------------------------------------- #
def _apple_search_obj(results, total):
    return {"loaderData": {"search": {"searchResults": results, "totalRecords": total, "page": 1}}}


APPLE_RESULT_OK = {
    "postingTitle": "Engineering Program Manager, Siri",
    "positionId": 200500100, "transformedPostingTitle": "engineering-program-manager-siri",
    "locations": [{"name": "Seattle, Washington, United States", "countryName": "United States"}],
    "jobSummary": "<p>Short teaser summary.</p>",
}
APPLE_RESULT_NO_ID = {
    "postingTitle": "Program Manager, Ghost", "positionId": "", "transformedPostingTitle": "ghost",
    "locations": [{"name": "Cupertino, CA", "countryName": "United States"}],
    "jobSummary": "no id here",
}
APPLE_DETAIL_OBJ = {"loaderData": {"jobDetails": {"jobsData": {
    "jobSummary": "<p>Lead Siri program delivery across orgs.</p>",
    "description": "<p>Own the roadmap and cross-functional execution.</p>",
    "minimumQualifications": "<ul><li>8 years program management</li></ul>",
    "preferredQualifications": "<p>ML platform exposure.</p>",
}}}}


class TestAppleCareers(unittest.TestCase):
    def test_search_hydration_plus_detail_full_description(self):
        def fake_get(url, *a, **k):
            if "/details/" in url:
                return resp(text=apple_html(APPLE_DETAIL_OBJ))
            return resp(text=apple_html(_apple_search_obj([APPLE_RESULT_OK], total=1)))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_apple("program manager")
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["company"], "Apple")
        self.assertEqual(
            j["url"],
            "https://jobs.apple.com/en-us/details/200500100/engineering-program-manager-siri")
        self.assertIn("program delivery across orgs", j["description"])
        self.assertIn("8 years program management", j["description"])
        self.assertIn("cross-functional execution", j["description"])

    def test_pagination_follows_total_records(self):
        pages_seen = []

        def fake_get(url, *a, **k):
            if "/details/" in url:
                return resp(text=apple_html(APPLE_DETAIL_OBJ))
            page = (k.get("params") or {}).get("page")
            pages_seen.append(page)
            r = dict(APPLE_RESULT_OK, positionId=200000000 + page,
                     transformedPostingTitle=f"epm-{page}")
            return resp(text=apple_html(_apple_search_obj([r], total=45)))  # 45 -> 3 pages
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_apple("program manager")
        self.assertEqual(pages_seen, [1, 2, 3])
        self.assertEqual({j["url"].split("/")[-2] for j in jobs},
                         {"200000001", "200000002", "200000003"})

    def test_missing_position_id_is_skipped_not_urld(self):
        def fake_get(url, *a, **k):
            if "/details/" in url:
                return resp(text=apple_html(APPLE_DETAIL_OBJ))
            return resp(text=apple_html(_apple_search_obj([APPLE_RESULT_OK, APPLE_RESULT_NO_ID], 2)))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_apple("program manager")
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("/details//", jobs[0]["url"])

    def test_detail_failure_falls_back_to_search_summary(self):
        def fake_get(url, *a, **k):
            if "/details/" in url:
                return resp(status=500)
            return resp(text=apple_html(_apple_search_obj([APPLE_RESULT_OK], 1)))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_apple("program manager")
        self.assertEqual(jobs[0]["description"], "Short teaser summary.")

    def test_search_http_error_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp(status=503)), \
             patch.object(d.time, "sleep"):
            self.assertEqual(d.search_jobs_apple("program manager"), [])

    def test_no_hydration_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp(text="<html>nope</html>")), \
             patch.object(d.time, "sleep"):
            self.assertEqual(d.search_jobs_apple("program manager"), [])


# --------------------------------------------------------------------------- #
# Oracle Careers — finding 2 (full description from detail endpoint),
# 5 (offset pagination), 6 (missing id)
# --------------------------------------------------------------------------- #
def _oracle_list_obj(reqs, total):
    return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}


ORACLE_REQ_OK = {
    "Title": "Senior Technical Program Manager, OCI", "Id": "344099",
    "PrimaryLocation": "Seattle, WA, United States", "PrimaryLocationCountry": "US",
    "secondaryLocations": [{"Name": "Austin, TX, United States"}],
    "ShortDescriptionStr": "Teaser: lead OCI platform programs.",
}
ORACLE_REQ_NO_ID = {
    "Title": "Program Manager, Ghost", "Id": "", "PrimaryLocation": "Remote, US",
    "ShortDescriptionStr": "no id",
}
ORACLE_DETAIL_OBJ = {"items": [{
    "ShortDescriptionStr": "Teaser: lead OCI platform programs.",
    "ExternalDescriptionStr": "<p>Own the OCI platform program portfolio.</p>",
    "ExternalResponsibilitiesStr": "<p>Drive quarterly delivery across eight teams.</p>",
    "ExternalQualificationsStr": "<p>10+ years technical program management.</p>",
}]}


class TestOracleCareers(unittest.TestCase):
    def test_description_built_from_summary_responsibilities_qualifications(self):
        def fake_get(url, *a, **k):
            if "RequisitionDetails" in url:
                return resp(json_data=ORACLE_DETAIL_OBJ)
            return resp(json_data=_oracle_list_obj([ORACLE_REQ_OK], total=1))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_oracle("program manager")
        self.assertEqual(len(jobs), 1)
        desc = jobs[0]["description"]
        self.assertIn("OCI platform program portfolio", desc)       # ExternalDescriptionStr
        self.assertIn("quarterly delivery across eight teams", desc)  # ExternalResponsibilitiesStr
        self.assertIn("10+ years technical program management", desc)  # ExternalQualificationsStr
        self.assertEqual(jobs[0]["url"], "https://careers.oracle.com/en/sites/jobsearch/job/344099")
        self.assertIn("Austin", jobs[0]["location"])

    def test_offset_pagination_follows_total(self):
        offsets = []

        def fake_get(url, *a, **k):
            if "RequisitionDetails" in url:
                return resp(json_data=ORACLE_DETAIL_OBJ)
            finder = (k.get("params") or {}).get("finder", "")
            offsets.append(finder)
            n = "0" if "offset=0" in finder else finder.split("offset=")[1].split(",")[0]
            r = dict(ORACLE_REQ_OK, Id=f"req{n}")
            return resp(json_data=_oracle_list_obj([r], total=120))  # 120 / 50 -> 3 pages
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_oracle("program manager")
        self.assertEqual([("offset=0" in f) for f in offsets][:1], [True])
        self.assertTrue(any("offset=50" in f for f in offsets))
        self.assertTrue(any("offset=100" in f for f in offsets))
        self.assertEqual(len(jobs), 3)

    def test_missing_requisition_id_is_skipped(self):
        def fake_get(url, *a, **k):
            if "RequisitionDetails" in url:
                return resp(json_data=ORACLE_DETAIL_OBJ)
            return resp(json_data=_oracle_list_obj([ORACLE_REQ_OK, ORACLE_REQ_NO_ID], total=2))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_oracle("program manager")
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("/job/\n", jobs[0]["url"])
        self.assertTrue(jobs[0]["url"].endswith("344099"))

    def test_detail_failure_falls_back_to_short_description(self):
        def fake_get(url, *a, **k):
            if "RequisitionDetails" in url:
                return resp(status=500)
            return resp(json_data=_oracle_list_obj([ORACLE_REQ_OK], total=1))
        with patch.object(d.requests, "get", side_effect=fake_get), \
             patch.object(d.time, "sleep"):
            jobs = d.search_jobs_oracle("program manager")
        self.assertEqual(jobs[0]["description"], "Teaser: lead OCI platform programs.")

    def test_list_http_error_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp(status=500)), \
             patch.object(d.time, "sleep"):
            self.assertEqual(d.search_jobs_oracle("program manager"), [])

    def test_list_bad_json_returns_empty(self):
        with patch.object(d.requests, "get", return_value=resp()), \
             patch.object(d.time, "sleep"):
            self.assertEqual(d.search_jobs_oracle("program manager"), [])


# --------------------------------------------------------------------------- #
# Isolation: one source failing must not stop the others
# --------------------------------------------------------------------------- #
class TestSafeWrapper(unittest.TestCase):
    def test_safe_swallows_any_exception(self):
        def boom():
            raise RuntimeError("schema changed")
        self.assertEqual(d._safe("x", boom), [])

    def test_safe_passes_through_results(self):
        self.assertEqual(d._safe("x", lambda: [{"a": 1}]), [{"a": 1}])


class TestDiscoverJobsIsolation(unittest.TestCase):
    def setUp(self):
        self._orig = util.PIPELINE_STAGE_LOG_PATH
        util.PIPELINE_STAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

    def tearDown(self):
        util.PIPELINE_STAGE_LOG_PATH = self._orig

    def test_one_failing_source_does_not_stop_the_rest(self):
        li = [{"company": "Meta", "title": "TPM", "url": "https://li/1", "description": "d"}]
        ap = [{"company": "Apple", "title": "Engineering Program Manager", "url": "https://ap/1", "description": "d"}]
        ash = [{"company": "OpenAI", "title": "Program Manager", "url": "https://oa/1", "description": "d"}]
        orc = [{"company": "Oracle", "title": "Technical Program Manager", "url": "https://or/1", "description": "d"}]
        with patch.object(d, "search_jobs_linkedin", MagicMock(return_value=li)), \
             patch.object(d, "search_jobs_apple", MagicMock(return_value=ap)), \
             patch.object(d, "search_jobs_greenhouse", MagicMock(side_effect=RuntimeError("greenhouse down"))) as gh, \
             patch.object(d, "search_jobs_ashby", MagicMock(return_value=ash)), \
             patch.object(d, "search_jobs_oracle", MagicMock(return_value=orc)), \
             patch.object(d.time, "sleep"):
            jobs = d.discover_jobs()
        self.assertEqual({j["company"] for j in jobs}, {"Meta", "Apple", "OpenAI", "Oracle"})
        gh.assert_called()

    def test_dedup_by_url_across_sources(self):
        a = {"company": "Oracle", "title": "Program Manager", "url": "https://o/1", "description": "d"}
        b = {"company": "Oracle", "title": "Program Manager", "url": "https://o/2", "description": "d"}
        with patch.object(d, "search_jobs_linkedin", MagicMock(return_value=[a])), \
             patch.object(d, "search_jobs_apple", MagicMock(return_value=[b])), \
             patch.object(d, "search_jobs_greenhouse", MagicMock(return_value=[])), \
             patch.object(d, "search_jobs_ashby", MagicMock(return_value=[])), \
             patch.object(d, "search_jobs_oracle", MagicMock(return_value=[dict(a)])), \
             patch.object(d.time, "sleep"):
            jobs = d.discover_jobs()
        self.assertEqual(len(jobs), 2)  # o/1 and o/2 kept; the duplicate o/1 dropped


if __name__ == "__main__":
    unittest.main()
