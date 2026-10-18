import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker  # noqa: E402
from crustdata_api import Crustdata, CrustdataError, normalize_domain  # noqa: E402


def role(name, title, website, start, end=None, default=False, headcount="51-200"):
    return {"name": name, "title": title, "company_website": website, "start_date": start,
            "end_date": end, "is_default": default, "company_headcount_range": headcount}


def person(pid, current, past, url="https://example.com/in/p"):
    return {"crustdata_person_id": pid, "basic_profile": {"name": f"Person {pid}"},
            "social_handles": {"professional_network_identifier": {"profile_url": url}},
            "experience": {"employment_details": {"current": current, "past": past}}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class DomainTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_domain("https://www.Stripe.com/about?x=1"), "stripe.com")
        self.assertEqual(normalize_domain("gong.io"), "gong.io")
        self.assertEqual(normalize_domain(None), "")


class DiscoverTests(unittest.TestCase):
    since = "2026-07-01"

    def test_real_move_detected(self):
        p = person(1, [role("NewCo", "Head of RevOps", "https://newco.ai", "2026-08-01T00:00:00", default=True),
                       role("RevOps Club", "Member", "https://club.org", "2020-01-01T00:00:00")],
                   [role("Acme", "Director, Revenue Operations", "https://www.acme.com", "2021-01-01", "2026-07-01T00:00:00")])
        m = tracker.find_move(p, ["acme.com"], ["Revenue Operations"], self.since)
        self.assertIsNotNone(m)
        self.assertEqual(m["new_company"], "NewCo")
        self.assertEqual(m["old_company"], "Acme")
        self.assertEqual(m["new_start"], "2026-08-01")

    def test_internal_move_ignored(self):
        p = person(2, [role("Acme", "VP Revenue Operations", "https://acme.com", "2026-08-01")],
                   [role("Acme", "Director, Revenue Operations", "https://acme.com", "2022-01-01", "2026-08-01")])
        self.assertIsNone(tracker.find_move(p, ["acme.com"], ["Revenue Operations"], self.since))

    def test_old_departure_ignored(self):
        p = person(3, [role("NewCo", "RevOps Lead", "https://newco.ai", "2026-08-01")],
                   [role("Acme", "Revenue Operations Manager", "https://acme.com", "2020-01-01", "2025-01-01")])
        self.assertIsNone(tracker.find_move(p, ["acme.com"], ["Revenue Operations"], self.since))

    def test_membership_is_not_a_new_job(self):
        p = person(4, [role("Modern Sales Pros", "Member", "https://msp.com", "2026-08-01")],
                   [role("Acme", "Revenue Operations Manager", "https://acme.com", "2020-01-01", "2026-08-01")])
        self.assertIsNone(tracker.find_move(p, ["acme.com"], ["Revenue Operations"], self.since))

    def test_title_must_match(self):
        p = person(5, [role("NewCo", "Engineer", "https://newco.ai", "2026-08-01")],
                   [role("Acme", "Software Engineer", "https://acme.com", "2020-01-01", "2026-08-01")])
        self.assertIsNone(tracker.find_move(p, ["acme.com"], ["Revenue Operations"], self.since))

    def test_discover_builds_documented_filters_and_dedupes(self):
        p = person(1, [role("NewCo", "Head of RevOps", "https://newco.ai", "2026-08-01")],
                   [role("Acme", "Revenue Operations Lead", "https://acme.com", "2021-01-01", "2026-07-15")])
        fake = FakeTransport([{"profiles": [p, p], "next_cursor": None}])
        moves = tracker.discover(Crustdata(api_key="test", transport=fake), ["acme.com"], ["Revenue Operations"], self.since, 50)
        self.assertEqual(len(moves), 1)
        url, body, headers = fake.calls[0]
        self.assertTrue(url.endswith("/person/search"))
        self.assertEqual(headers["x-api-version"], "2025-11-01")
        fields = json.dumps(body["filters"])
        self.assertIn("experience.employment_details.past.company_website_domain", fields)
        self.assertIn("experience.employment_details.current.start_date", fields)


class TrackTests(unittest.TestCase):
    def test_detect_change(self):
        p = person(1, [role("NewCo", "COO", "https://newco.ai", "2026-09-01")], [])
        self.assertEqual(tracker.detect_change(p, "acme.com", "Acme")["new_company"], "NewCo")

    def test_still_there(self):
        p = person(1, [role("Acme", "VP Sales", "https://www.acme.com/", "2024-01-01")], [])
        self.assertIsNone(tracker.detect_change(p, "acme.com", "Acme"))

    def test_left_without_new_role(self):
        p = person(1, [], [])
        self.assertEqual(tracker.detect_change(p, "acme.com", "Acme")["status"], "left_no_new_role")

    def test_track_uses_person_enrich(self):
        p = person(9, [role("NewCo", "COO", "https://newco.ai", "2026-09-01")], [])
        fake = FakeTransport([[{"matched_on": "https://example.com/in/p9", "match_status": "matched",
                                "matches": [{"confidence_score": 1.0, "person_data": p}]},
                               {"matched_on": "https://example.com/in/gone", "match_status": "not_found", "matches": []}]])
        champs = [{"profile_url": "https://example.com/in/p9", "company_domain": "acme.com", "company_name": "Acme"},
                  {"profile_url": "https://example.com/in/gone", "company_domain": "acme.com"}]
        moves = tracker.track(Crustdata(api_key="test", transport=fake), champs)
        self.assertEqual(len(moves), 1)
        self.assertTrue(fake.calls[0][0].endswith("/person/enrich"))
        self.assertEqual(len(fake.calls[0][1]["professional_network_profile_urls"]), 2)


class StateAndOutputTests(unittest.TestCase):
    def test_state_dedupes_alerts(self):
        m = {"person_id": 1, "new_domain": "newco.ai"}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            self.assertEqual(len(tracker.only_new([m], path)), 1)
            tracker.save_state([m], path)
            self.assertEqual(len(tracker.only_new([m], path)), 0)

    def test_slack_text(self):
        m = tracker.move_record(person(1, [], []), {"name": "Acme", "title": "VP Sales"},
                                role("NewCo", "CRO", "https://newco.ai", "2026-09-01"))
        text = tracker.slack_text([m])
        self.assertIn("1 champion job change", text)
        self.assertIn("NewCo", text)


class ClientTests(unittest.TestCase):
    def test_retries_on_429(self):
        fake = FakeTransport([CrustdataError(429, "slow down"), {"profiles": [], "next_cursor": None}])
        c = Crustdata(api_key="test", transport=fake)
        c.max_retries = 1
        import crustdata_api
        orig = crustdata_api.time.sleep
        crustdata_api.time.sleep = lambda s: None
        try:
            self.assertEqual(list(c.person_search({}, [], 5)), [])
        finally:
            crustdata_api.time.sleep = orig
        self.assertEqual(len(fake.calls), 2)

    def test_does_not_retry_400(self):
        fake = FakeTransport([CrustdataError(400, "bad")])
        with self.assertRaises(CrustdataError):
            Crustdata(api_key="test", transport=fake).post("/person/search", {})


if __name__ == "__main__":
    unittest.main()
