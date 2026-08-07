import json
import unittest
from pathlib import Path
from urllib.parse import urlencode

from scraper.ultimate_central import (
    RequestBudgetExceeded,
    UltimateCentralClient,
    UltimateCentralEnvelopeError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "euf" / "ultimate_central.json"


class FakeResponse:
    def __init__(self, payload, url="https://fixture.test/api/games", status=200):
        self._payload = payload
        self.content = json.dumps(payload, sort_keys=True).encode()
        self.url = url
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        response.url = url + ("?" + urlencode(params) if params else "")
        return response


class UltimateCentralContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text())

    def test_pagination_preserves_pages_and_urls(self):
        session = FakeSession([
            FakeResponse(self.fixture["games_page_1"]),
            FakeResponse(self.fixture["games_page_2"]),
        ])
        client = UltimateCentralClient(session, base_url="https://fixture.test")
        result = client.list_games(7, per_page=1)
        self.assertEqual([101, 102], [row["id"] for row in result.items])
        self.assertEqual(2, len(result.pages))
        self.assertIn("page=2", result.pages[1].source_url)
        self.assertTrue(all(page.payload_hash for page in result.pages))

    def test_retries_only_transient_status(self):
        transient = FakeResponse({}, status=503)
        session = FakeSession([transient, FakeResponse(self.fixture["event"])])
        sleeps = []
        client = UltimateCentralClient(
            session, base_url="https://fixture.test", backoff=0,
            sleep=sleeps.append,
        )
        response = client.get("/api/events", {"id": 7})
        self.assertEqual("ok", response.state)
        self.assertEqual(2, client.requests_made)
        self.assertEqual([0], sleeps)

    def test_rate_limit_without_header_waits_before_retry(self):
        session = FakeSession([
            FakeResponse({}, status=429),
            FakeResponse(self.fixture["event"]),
        ])
        sleeps = []
        client = UltimateCentralClient(
            session, base_url="https://fixture.test", backoff=0,
            sleep=sleeps.append,
        )
        self.assertEqual("ok", client.get("/api/events").state)
        self.assertEqual([10.0], sleeps)

    def test_restricted_is_not_empty(self):
        response = FakeResponse(self.fixture["restricted"], status=403)
        client = UltimateCentralClient(FakeSession([response]))
        result = client.list_public_persons(7)
        self.assertEqual("restricted", result.state)
        self.assertEqual([], result.items)

    def test_envelope_and_budget_fail_closed(self):
        client = UltimateCentralClient(FakeSession([FakeResponse({"status": 200})]))
        with self.assertRaises(UltimateCentralEnvelopeError):
            client.get("/api/events")
        client = UltimateCentralClient(
            FakeSession([FakeResponse(self.fixture["event"])]), request_budget=1
        )
        client.get("/api/events")
        with self.assertRaises(RequestBudgetExceeded):
            client.get("/api/events")

    def test_page_size_is_bounded(self):
        client = UltimateCentralClient(FakeSession([]))
        with self.assertRaises(ValueError):
            client.list_games(7, per_page=101)


if __name__ == "__main__":
    unittest.main()
