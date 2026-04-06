"""Tests for the flat Funda MCP server."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "funda-mcp"
for path in (PACKAGE_ROOT, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from server import FundaService, build_server


class FakeListing:
    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeFundaClient:
    def __init__(self):
        self.timeout = 30
        self.closed = False
        self.get_listing_calls: list[str] = []
        self.get_price_history_calls: list[object] = []
        self.search_kwargs: dict | None = None
        self.poll_kwargs: dict | None = None
        self.latest_id = 7852306

    def close(self) -> None:
        self.closed = True

    def get_listing(self, listing_id_or_url: str):
        self.get_listing_calls.append(listing_id_or_url)
        return FakeListing(
            {
                "global_id": 7852307,
                "tiny_id": "43117443",
                "title": "Reehorst 13",
                "city": "Luttenberg",
                "coordinates": (52.0, 6.4),
            }
        )

    def search_listing(self, **kwargs):
        self.search_kwargs = kwargs
        return [
            FakeListing({"global_id": 1, "title": "A", "city": "Amsterdam"}),
            FakeListing({"global_id": 2, "title": "B", "city": "Rotterdam"}),
        ]

    def _post(self, url, headers, data=None, for_search=False):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"responses": [{"hits": {"total": {"value": 504}, "hits": []}}]}

        return FakeResponse()

    def get_latest_id(self):
        return self.latest_id

    def poll_new_listings(self, **kwargs):
        self.poll_kwargs = kwargs
        for global_id in (7852307, 7852308, 7852309):
            yield FakeListing({"global_id": global_id, "title": f"Listing {global_id}"})

    def get_price_history(self, listing):
        self.get_price_history_calls.append(listing)
        return [
            {"date": "2026-01-15", "price": 400000, "status": "asking_price"},
            {"date": "2025-01-01", "price": 376000, "status": "woz"},
        ]


class FundaServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeFundaClient()
        self.service = FundaService(self.client)

    def test_get_listing_returns_json_safe_payload(self) -> None:
        listing = self.service.get_listing("43117443")
        self.assertEqual(self.client.get_listing_calls, ["43117443"])
        self.assertEqual(listing.listing["coordinates"], [52.0, 6.4])

    def test_get_listing_rejects_invalid_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "listing_id_or_url"):
            self.service.get_listing("not-a-funda-id")

    def test_search_listings_coerces_scalar_filters(self) -> None:
        response = self.service.search_listings(
            location="Amsterdam",
            object_type="house",
            energy_label="A",
            construction_type="resale",
            availability="sold",
        )

        self.assertEqual(response.total_count, 504)
        self.assertEqual(response.returned_count, 2)
        self.assertEqual(response.applied_filters["location"], ["amsterdam"])
        self.assertEqual(response.applied_filters["availability"], ["sold"])
        self.assertEqual(response.applied_filters["object_type"], ["house"])
        self.assertEqual(self.client.search_kwargs["location"], ["amsterdam"])
        self.assertEqual(self.client.search_kwargs["object_type"], ["house"])
        self.assertEqual(self.client.search_kwargs["energy_label"], ["A"])
        self.assertEqual(self.client.search_kwargs["construction_type"], ["resale"])
        self.assertEqual(self.client.search_kwargs["availability"], ["sold"])

    def test_search_listings_rejects_invalid_sort(self) -> None:
        with self.assertRaisesRegex(ValueError, "sort"):
            self.service.search_listings(sort="unknown_sort")

    def test_get_latest_id_wraps_response(self) -> None:
        response = self.service.get_latest_id()
        self.assertEqual(response.latest_id, 7852306)

    def test_poll_new_listings_is_bounded(self) -> None:
        response = self.service.poll_new_listings(since_id=7852306, max_results=2, offering_type="buy")
        self.assertEqual(response.count, 2)
        self.assertEqual(response.last_seen_id, 7852308)
        self.assertEqual(self.client.poll_kwargs["offering_type"], "buy")

    def test_poll_new_listings_validates_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_results"):
            self.service.poll_new_listings(since_id=1, max_results=0)

    def test_get_price_history_by_url_uses_direct_lookup(self) -> None:
        url = "https://www.funda.nl/detail/koop/luttenberg/reehorst-13/43117443/"
        response = self.service.get_price_history(url)
        self.assertEqual(response.count, 2)
        self.assertEqual(self.client.get_price_history_calls, [url])

    def test_get_price_history_by_id_passes_id_through(self) -> None:
        response = self.service.get_price_history("43117443")
        self.assertEqual(response.count, 2)
        self.assertEqual(self.client.get_price_history_calls, ["43117443"])

    def test_timeout_is_restored_after_call(self) -> None:
        self.assertEqual(self.client.timeout, 30)
        self.service.get_latest_id(timeout_seconds=12)
        self.assertEqual(self.client.timeout, 30)


class ServerSmokeTests(unittest.TestCase):
    def test_server_registers_expected_tools(self) -> None:
        server = build_server()
        tool_names = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertEqual(
            tool_names,
            {
                "get_listing",
                "search_listings",
                "get_latest_id",
                "poll_new_listings",
                "get_price_history",
            },
        )


if __name__ == "__main__":
    unittest.main()
