#!/usr/bin/env python3
"""Funda MCP server."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
import json
from pathlib import Path
import re
from threading import RLock
import time
from typing import Annotated, Any, Literal
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from funda.funda import API_SEARCH, Funda, _make_headers

AvailabilityValue = Literal["available", "negotiations", "sold", "unavailable"]
ConstructionTypeValue = Literal["resale", "newly_built"]
OfferingTypeValue = Literal["buy", "rent"]
SortValue = Literal[
    "newest",
    "oldest",
    "price_asc",
    "price_desc",
    "area_asc",
    "area_desc",
    "plot_desc",
    "city",
    "postcode",
]

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
MAX_POLL_RESULTS = 50

LISTING_ID_PATTERN = re.compile(r"^\d{7,9}$")
FUNDA_URL_PATTERN = re.compile(r"^https?://(?:www\.)?funda\.nl/", re.IGNORECASE)
VALID_AVAILABILITY = {"available", "negotiations", "sold", "unavailable"}
VALID_CONSTRUCTION_TYPES = {"resale", "newly_built"}
VALID_OFFERING_TYPES = {"buy", "rent"}
VALID_SORT_VALUES = {
    "newest",
    "oldest",
    "price_asc",
    "price_desc",
    "area_asc",
    "area_desc",
    "plot_desc",
    "city",
    "postcode",
}
VALID_RADII = [1, 2, 5, 10, 15, 30, 50]


class ListingResponse(BaseModel):
    listing: dict[str, Any] = Field(description="Full listing payload.")


class SearchListingsResponse(BaseModel):
    total_count: int = Field(description="Total number of matching listings across all pages.")
    returned_count: int = Field(description="Number of listings returned in this page.")
    applied_filters: dict[str, Any] = Field(
        description="Normalized search filters used by this MCP server, including defaults."
    )
    results: list[dict[str, Any]] = Field(description="Listing summaries returned by the search API.")


class LatestIdResponse(BaseModel):
    latest_id: int = Field(description="Highest known global listing ID.")


class PollNewListingsResponse(BaseModel):
    count: int = Field(description="Number of listings returned by the poll.")
    results: list[dict[str, Any]] = Field(description="Newly found listing details.")
    last_seen_id: int = Field(description="Highest global listing ID observed while polling.")


class PriceHistoryResponse(BaseModel):
    count: int = Field(description="Number of historical price records returned.")
    changes: list[dict[str, Any]] = Field(description="Historical price events.")


_MODEL_TYPES = {"Any": Any}
for model in (
    ListingResponse,
    SearchListingsResponse,
    LatestIdResponse,
    PollNewListingsResponse,
    PriceHistoryResponse,
):
    model.model_rebuild(_types_namespace=_MODEL_TYPES)


DIRECT_RUN_HELP = """\
Funda MCP is a stdio server. It is meant to be launched by an MCP client, not used interactively in a terminal.

MCP client command:
  ./funda-mcp/run

Manual debug:
  .venv/bin/mcp dev funda-mcp/server.py
"""


def _coerce_list(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else None
    items = [item for item in value if item is not None]
    return items or None


def _normalize_choice(name: str, value: str | None, allowed: set[str]) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _normalize_list(name: str, value: str | Iterable[str] | None, allowed: set[str] | None = None) -> list[str] | None:
    items = _coerce_list(value)
    if not items:
        return None
    if allowed is None:
        return items
    invalid = sorted({item for item in items if item not in allowed})
    if invalid:
        raise ValueError(f"{name} contains invalid values: {', '.join(invalid)}")
    return items


def _normalize_location(value: str | Iterable[str] | None) -> list[str] | None:
    items = _coerce_list(value)
    if not items:
        return None
    normalized = []
    for item in items:
        stripped = str(item).strip()
        if stripped:
            normalized.append(stripped.lower())
    return normalized or None


def _positive_int(name: str, value: int, minimum: int = 1, maximum: int | None = None) -> int:
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _validate_listing_ref(listing_id_or_url: str) -> str:
    value = listing_id_or_url.strip()
    if not value:
        raise ValueError("listing_id_or_url must not be empty")
    if LISTING_ID_PATTERN.fullmatch(value):
        return value
    if FUNDA_URL_PATTERN.match(value):
        if Funda.TINYID_PATTERN.search(value) is None:
            raise ValueError("Funda URLs must include a 7 to 9 digit listing ID segment")
        return value
    raise ValueError("listing_id_or_url must be a 7 to 9 digit ID or a funda.nl detail URL")


def _jsonify(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonify(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    return value


class FundaService:
    def __init__(self, client: Funda):
        self.client = client
        self.lock = RLock()

    @contextmanager
    def client_call(self, timeout_seconds: int):
        timeout_seconds = _positive_int("timeout_seconds", timeout_seconds, minimum=1, maximum=MAX_TIMEOUT)
        with self.lock:
            previous_timeout = self.client.timeout
            self.client.timeout = timeout_seconds
            try:
                yield self.client
            finally:
                self.client.timeout = previous_timeout

    def get_listing(self, listing_id_or_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT) -> ListingResponse:
        listing_ref = _validate_listing_ref(listing_id_or_url)
        with self.client_call(timeout_seconds) as client:
            return ListingResponse(listing=_jsonify(client.get_listing(listing_ref)))

    def _build_search_params(
        self,
        *,
        location: list[str] | None,
        offering_type: str,
        availability: list[str] | None,
        price_min: int | None,
        price_max: int | None,
        area_min: int | None,
        area_max: int | None,
        plot_min: int | None,
        plot_max: int | None,
        object_type: list[str] | None,
        energy_label: list[str] | None,
        construction_type: list[str] | None,
        construction_year_min: int | None,
        construction_year_max: int | None,
        radius_km: int | None,
        sort: str | None,
        page: int,
    ) -> dict[str, Any]:
        availability_values = availability or ["available", "negotiations"]
        availability_values = ["unavailable" if value == "sold" else value for value in availability_values]
        params: dict[str, Any] = {
            "availability": availability_values,
            "type": ["single"],
            "zoning": ["residential"],
            "object_type": object_type or ["house", "apartment"],
            "publication_date": {"no_preference": True},
            "offering_type": offering_type,
            "page": {"from": page * 15},
        }

        if sort in {"price_asc", "price_desc"}:
            price_field = "price.selling_price" if offering_type == "buy" else "price.rent_price"
            params["sort"] = {"field": price_field, "order": "asc" if sort == "price_asc" else "desc"}
        else:
            sort_map = {
                "newest": ("publish_date_utc", "desc"),
                "oldest": ("publish_date_utc", "asc"),
                "area_asc": ("floor_area", "asc"),
                "area_desc": ("floor_area", "desc"),
                "plot_desc": ("plot_area", "desc"),
                "city": ("address.city", "asc"),
                "postcode": ("address.postal_code", "asc"),
            }
            if sort and sort in sort_map:
                field, order = sort_map[sort]
                params["sort"] = {"field": field, "order": order}
            else:
                params["sort"] = {"field": None, "order": None}

        if location and radius_km and len(location) == 1:
            actual_radius = min(VALID_RADII, key=lambda candidate: abs(candidate - radius_km))
            params["radius_search"] = {
                "index": "geo-wonen-alias-prod",
                "id": f"{location[0].replace(' ', '-')}-0",
                "path": f"area_with_radius.{actual_radius}",
            }
        elif location:
            params["selected_area"] = location

        if price_min is not None or price_max is not None:
            price_key = "selling_price" if offering_type == "buy" else "rent_price"
            price_filter: dict[str, Any] = {}
            if price_min is not None:
                price_filter["from"] = price_min
            if price_max is not None:
                price_filter["to"] = price_max
            params["price"] = {price_key: price_filter}

        if area_min is not None or area_max is not None:
            floor_filter: dict[str, Any] = {}
            if area_min is not None:
                floor_filter["from"] = area_min
            if area_max is not None:
                floor_filter["to"] = area_max
            params["floor_area"] = floor_filter

        if plot_min is not None or plot_max is not None:
            plot_filter: dict[str, Any] = {}
            if plot_min is not None:
                plot_filter["from"] = plot_min
            if plot_max is not None:
                plot_filter["to"] = plot_max
            params["plot_area"] = plot_filter

        if energy_label:
            params["energy_label"] = energy_label

        if construction_type:
            params["construction_type"] = construction_type

        if construction_year_min is not None or construction_year_max is not None:
            period_boundaries = [1906, 1931, 1945, 1960, 1971, 1981, 1991, 2001, 2011, 2021]
            all_periods = (
                ["before_1906"]
                + [f"from_{period_boundaries[i]}_to_{period_boundaries[i + 1] - 1}" for i in range(len(period_boundaries) - 1)]
                + ["after_2020"]
            )
            period_ranges = {
                "before_1906": (0, 1905),
                "from_1906_to_1930": (1906, 1930),
                "from_1931_to_1944": (1931, 1944),
                "from_1945_to_1959": (1945, 1959),
                "from_1960_to_1970": (1960, 1970),
                "from_1971_to_1980": (1971, 1980),
                "from_1981_to_1990": (1981, 1990),
                "from_1991_to_2000": (1991, 2000),
                "from_2001_to_2010": (2001, 2010),
                "from_2011_to_2020": (2011, 2020),
                "after_2020": (2021, 9999),
            }
            year_min = construction_year_min or 0
            year_max = construction_year_max or 9999
            selected = [period for period in all_periods if period_ranges[period][1] >= year_min and period_ranges[period][0] <= year_max]
            if selected:
                params["construction_period"] = selected

        return params

    def _search_total_count(self, params: dict[str, Any], *, timeout_seconds: int) -> int:
        index_line = json.dumps({"index": "listings-wonen-searcher-alias-prod"})
        query_line = json.dumps({"id": "search_result_20250805", "params": params})
        query = f"{index_line}\n{query_line}\n"
        with self.client_call(timeout_seconds) as client:
            for attempt in range(3):
                response = client._post(API_SEARCH, _make_headers(for_search=True), data=query, for_search=True)
                if response.status_code == 200:
                    break
                if response.status_code == 400 and attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise RuntimeError(f"Search failed (status {response.status_code})")
        responses = response.json().get("responses", [])
        if not responses:
            return 0
        total = responses[0].get("hits", {}).get("total", {})
        if isinstance(total, dict):
            return int(total.get("value", 0))
        if isinstance(total, int):
            return total
        return 0

    def _applied_search_filters(
        self,
        *,
        location: list[str] | None,
        offering_type: str,
        availability: list[str] | None,
        price_min: int | None,
        price_max: int | None,
        area_min: int | None,
        area_max: int | None,
        plot_min: int | None,
        plot_max: int | None,
        object_type: list[str] | None,
        energy_label: list[str] | None,
        construction_type: list[str] | None,
        construction_year_min: int | None,
        construction_year_max: int | None,
        radius_km: int | None,
        sort: str | None,
        page: int,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "offering_type": offering_type,
            "availability": availability or ["available", "negotiations"],
            "object_type": object_type or ["house", "apartment"],
            "page": page,
        }
        if location is not None:
            filters["location"] = location
        if price_min is not None:
            filters["price_min"] = price_min
        if price_max is not None:
            filters["price_max"] = price_max
        if area_min is not None:
            filters["area_min"] = area_min
        if area_max is not None:
            filters["area_max"] = area_max
        if plot_min is not None:
            filters["plot_min"] = plot_min
        if plot_max is not None:
            filters["plot_max"] = plot_max
        if energy_label is not None:
            filters["energy_label"] = energy_label
        if construction_type is not None:
            filters["construction_type"] = construction_type
        if construction_year_min is not None:
            filters["construction_year_min"] = construction_year_min
        if construction_year_max is not None:
            filters["construction_year_max"] = construction_year_max
        if radius_km is not None:
            filters["radius_km"] = min(VALID_RADII, key=lambda candidate: abs(candidate - radius_km))
        if sort is not None:
            filters["sort"] = sort
        return filters

    def search_listings(
        self,
        *,
        location: str | list[str] | None = None,
        offering_type: str = "buy",
        availability: str | list[str] | None = None,
        price_min: int | None = None,
        price_max: int | None = None,
        area_min: int | None = None,
        area_max: int | None = None,
        plot_min: int | None = None,
        plot_max: int | None = None,
        object_type: str | list[str] | None = None,
        energy_label: str | list[str] | None = None,
        construction_type: str | list[str] | None = None,
        construction_year_min: int | None = None,
        construction_year_max: int | None = None,
        radius_km: int | None = None,
        sort: str | None = None,
        page: int = 0,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> SearchListingsResponse:
        if page < 0:
            raise ValueError("page must be >= 0")
        location = _normalize_location(location)
        offering_type = _normalize_choice("offering_type", offering_type, VALID_OFFERING_TYPES) or "buy"
        sort = _normalize_choice("sort", sort, VALID_SORT_VALUES)
        availability = _normalize_list("availability", availability, VALID_AVAILABILITY)
        construction_type = _normalize_list("construction_type", construction_type, VALID_CONSTRUCTION_TYPES)
        object_type = _normalize_list("object_type", object_type)
        energy_label = _normalize_list("energy_label", energy_label)
        params = self._build_search_params(
            location=location,
            offering_type=offering_type,
            availability=availability,
            price_min=price_min,
            price_max=price_max,
            area_min=area_min,
            area_max=area_max,
            plot_min=plot_min,
            plot_max=plot_max,
            object_type=object_type,
            energy_label=energy_label,
            construction_type=construction_type,
            construction_year_min=construction_year_min,
            construction_year_max=construction_year_max,
            radius_km=radius_km,
            sort=sort,
            page=page,
        )

        with self.client_call(timeout_seconds) as client:
            results = client.search_listing(
                location=location,
                offering_type=offering_type,
                availability=availability,
                price_min=price_min,
                price_max=price_max,
                area_min=area_min,
                area_max=area_max,
                plot_min=plot_min,
                plot_max=plot_max,
                object_type=object_type,
                energy_label=energy_label,
                construction_type=construction_type,
                construction_year_min=construction_year_min,
                construction_year_max=construction_year_max,
                radius_km=radius_km,
                sort=sort,
                page=page,
            )
        total_count = self._search_total_count(params, timeout_seconds=timeout_seconds)
        payload = [_jsonify(listing) for listing in results]
        applied_filters = self._applied_search_filters(
            location=location,
            offering_type=offering_type,
            availability=availability,
            price_min=price_min,
            price_max=price_max,
            area_min=area_min,
            area_max=area_max,
            plot_min=plot_min,
            plot_max=plot_max,
            object_type=object_type,
            energy_label=energy_label,
            construction_type=construction_type,
            construction_year_min=construction_year_min,
            construction_year_max=construction_year_max,
            radius_km=radius_km,
            sort=sort,
            page=page,
        )
        return SearchListingsResponse(
            total_count=total_count,
            returned_count=len(payload),
            applied_filters=applied_filters,
            results=payload,
        )

    def get_latest_id(self, *, timeout_seconds: int = DEFAULT_TIMEOUT) -> LatestIdResponse:
        with self.client_call(timeout_seconds) as client:
            return LatestIdResponse(latest_id=client.get_latest_id())

    def poll_new_listings(
        self,
        *,
        since_id: int,
        max_results: int = 10,
        max_consecutive_404s: int = 20,
        offering_type: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> PollNewListingsResponse:
        if since_id < 0:
            raise ValueError("since_id must be >= 0")
        max_results = _positive_int("max_results", max_results, minimum=1, maximum=MAX_POLL_RESULTS)
        max_consecutive_404s = _positive_int("max_consecutive_404s", max_consecutive_404s, minimum=1)
        offering_type = _normalize_choice("offering_type", offering_type, VALID_OFFERING_TYPES)

        results: list[dict[str, Any]] = []
        last_seen_id = since_id
        with self.client_call(timeout_seconds) as client:
            for listing in client.poll_new_listings(
                since_id=since_id,
                max_consecutive_404s=max_consecutive_404s,
                offering_type=offering_type,
            ):
                item = _jsonify(listing)
                results.append(item)
                global_id = item.get("global_id")
                if isinstance(global_id, int):
                    last_seen_id = max(last_seen_id, global_id)
                if len(results) >= max_results:
                    break
        return PollNewListingsResponse(count=len(results), results=results, last_seen_id=last_seen_id)

    def get_price_history(self, listing_id_or_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT) -> PriceHistoryResponse:
        listing_ref = _validate_listing_ref(listing_id_or_url)
        with self.client_call(timeout_seconds) as client:
            changes = _jsonify(client.get_price_history(listing_ref))
        return PriceHistoryResponse(count=len(changes), changes=changes)


def _service(ctx: Context) -> FundaService:
    service = ctx.request_context.lifespan_context
    if not isinstance(service, FundaService):  # pragma: no cover
        raise RuntimeError("Server state is unavailable")
    return service


@asynccontextmanager
async def lifespan(_: FastMCP[FundaService]):
    client = Funda(timeout=DEFAULT_TIMEOUT)
    service = FundaService(client)
    try:
        yield service
    finally:
        client.close()


def build_server() -> FastMCP[FundaService]:
    mcp = FastMCP(
        name="Funda MCP",
        instructions="Expose the stable pyfunda API as MCP tools for funda.nl listing data.",
        json_response=True,
        lifespan=lifespan,
    )

    @mcp.tool(
        name="get_listing",
        description="Fetch one Funda listing by numeric ID or full funda.nl detail URL. Example: listing_id_or_url='43117443'.",
        structured_output=True,
    )
    def get_listing(
        ctx: Context,
        listing_id_or_url: Annotated[str, Field(description="A 7 to 9 digit listing ID or full funda.nl detail URL.")],
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> ListingResponse:
        return _service(ctx).get_listing(listing_id_or_url, timeout_seconds=timeout_seconds)

    @mcp.tool(
        name="search_listings",
        description=(
            "Search Funda listings by location and filters. "
            "Defaults: offering_type='buy', availability=['available','negotiations'], object_type=['house','apartment']. "
            "Example: location='leiden', price_max=500000."
        ),
        structured_output=True,
    )
    def search_listings(
        ctx: Context,
        location: Annotated[
            str | list[str] | None,
            Field(description="A city, area, postcode, or list of locations. Values are normalized to lowercase."),
        ] = None,
        offering_type: Annotated[OfferingTypeValue, Field(description="Whether to search buy or rent listings.")] = "buy",
        availability: Annotated[AvailabilityValue | list[AvailabilityValue] | None, Field(description="Listing status filter. Supports one value or a list.")] = None,
        price_min: Annotated[int | None, Field(description="Minimum price.", ge=0)] = None,
        price_max: Annotated[int | None, Field(description="Maximum price.", ge=0)] = None,
        area_min: Annotated[int | None, Field(description="Minimum living area in square meters.", ge=0)] = None,
        area_max: Annotated[int | None, Field(description="Maximum living area in square meters.", ge=0)] = None,
        plot_min: Annotated[int | None, Field(description="Minimum plot area in square meters.", ge=0)] = None,
        plot_max: Annotated[int | None, Field(description="Maximum plot area in square meters.", ge=0)] = None,
        object_type: Annotated[str | list[str] | None, Field(description="Property type filter such as house or apartment.")] = None,
        energy_label: Annotated[str | list[str] | None, Field(description="Energy label filter. Supports one value or a list.")] = None,
        construction_type: Annotated[ConstructionTypeValue | list[ConstructionTypeValue] | None, Field(description="Construction type filter. Supports one value or a list.")] = None,
        construction_year_min: Annotated[int | None, Field(description="Minimum construction year.", ge=0)] = None,
        construction_year_max: Annotated[int | None, Field(description="Maximum construction year.", ge=0)] = None,
        radius_km: Annotated[int | None, Field(description="Optional radius search in kilometers.", ge=1)] = None,
        sort: Annotated[SortValue | None, Field(description="Sort order for returned listings.")] = None,
        page: Annotated[int, Field(description="Zero-based page number. Each page returns up to 15 listings.", ge=0)] = 0,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> SearchListingsResponse:
        return _service(ctx).search_listings(
            location=location,
            offering_type=offering_type,
            availability=availability,
            price_min=price_min,
            price_max=price_max,
            area_min=area_min,
            area_max=area_max,
            plot_min=plot_min,
            plot_max=plot_max,
            object_type=object_type,
            energy_label=energy_label,
            construction_type=construction_type,
            construction_year_min=construction_year_min,
            construction_year_max=construction_year_max,
            radius_km=radius_km,
            sort=sort,
            page=page,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="get_latest_id",
        description="Fetch the highest global listing ID currently visible in Funda search.",
        structured_output=True,
    )
    def get_latest_id(
        ctx: Context,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> LatestIdResponse:
        return _service(ctx).get_latest_id(timeout_seconds=timeout_seconds)

    @mcp.tool(
        name="poll_new_listings",
        description="Check for new listings above a known global ID and return a bounded result set. Example: since_id=7852306.",
        structured_output=True,
    )
    def poll_new_listings(
        ctx: Context,
        since_id: Annotated[int, Field(description="Start polling from this global listing ID.", ge=0)],
        max_results: Annotated[int, Field(description="Maximum number of new listings to return.", ge=1, le=MAX_POLL_RESULTS)] = 10,
        max_consecutive_404s: Annotated[int, Field(description="Stop polling after this many consecutive missing IDs.", ge=1)] = 20,
        offering_type: Annotated[OfferingTypeValue | None, Field(description="Optional filter for buy or rent listings.")] = None,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> PollNewListingsResponse:
        return _service(ctx).poll_new_listings(
            since_id=since_id,
            max_results=max_results,
            max_consecutive_404s=max_consecutive_404s,
            offering_type=offering_type,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="get_price_history",
        description="Fetch historical price changes and WOZ data for a listing by ID or funda.nl URL. Example: listing_id_or_url='43117443'.",
        structured_output=True,
    )
    def get_price_history(
        ctx: Context,
        listing_id_or_url: Annotated[str, Field(description="A 7 to 9 digit listing ID or full funda.nl detail URL.")],
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> PriceHistoryResponse:
        return _service(ctx).get_price_history(listing_id_or_url, timeout_seconds=timeout_seconds)

    return mcp


mcp = build_server()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(DIRECT_RUN_HELP, file=sys.stderr)
        raise SystemExit(0)
    if sys.stdin.isatty() and sys.stdout.isatty():
        print(DIRECT_RUN_HELP, file=sys.stderr)
        raise SystemExit(1)
    mcp.run()


if __name__ == "__main__":
    main()
