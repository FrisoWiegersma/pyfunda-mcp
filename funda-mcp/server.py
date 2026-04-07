#!/usr/bin/env python3
"""Funda MCP server."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from threading import RLock
import time
from typing import Annotated, Any, Literal
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from funda.funda import API_SEARCH, Funda, _make_headers
from woz_client import WozClient

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
ISO_DATE_PREFIX_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")
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
POSTCODE_PATTERN = re.compile(r"^\d{4}[A-Z]{2}$")
POSTCODE_PREFIX_PATTERN = re.compile(r"^\d{4}$")

CITY_ALIAS_MAP = {
    "'s gravenhage": "den-haag",
    "den haag": "den-haag",
    "s gravenhage": "den-haag",
    "the hague": "den-haag",
}

NEIGHBOURHOOD_FALLBACKS = {
    "benoordenhout": {
        "anchor": "2596BG",
        "default_radius_km": 1,
        "notes": [
            "Neighbourhood resolution uses a curated postcode anchor and radius fallback because Funda does not expose stable neighbourhood slugs through this MCP.",
        ],
    },
}


class ListingResponse(BaseModel):
    listing: dict[str, Any] = Field(description="Full listing payload.")


class SearchListingsResponse(BaseModel):
    total_count: int = Field(description="Total number of matching listings across all pages.")
    returned_count: int = Field(description="Number of listings returned in this page.")
    applied_filters: dict[str, Any] = Field(
        description="Normalized search filters used by this MCP server, including defaults."
    )
    search_resolution: dict[str, Any] = Field(
        description="How the MCP resolved the caller's location input into the upstream Funda search request."
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


class WozHistoryResponse(BaseModel):
    resolved_address: dict[str, Any] = Field(description="Normalized address resolved by Kadaster.")
    match: dict[str, Any] = Field(description="Identifiers used for the resolved WOZ object.")
    count: int = Field(description="Number of WOZ history records returned.")
    history: list[dict[str, Any]] = Field(description="Historical WOZ values ordered by peildatum descending.")
    current_woz_value: int | None = Field(description="Most recent WOZ value when available.")
    metadata: dict[str, Any] = Field(description="Additional Kadaster metadata for the resolved object.")


class GrossYieldResponse(BaseModel):
    resolved_address: dict[str, Any] = Field(description="Address used for the WOZ lookup.")
    annual_rent: float = Field(description="Monthly rent multiplied by 12.")
    acquisition_price: float = Field(description="Purchase price used in the yield calculation.")
    gross_yield_pct: float = Field(description="Gross yield percentage based on annual rent and acquisition price.")
    current_woz_value: int | None = Field(description="Most recent WOZ value when available.")
    price_to_current_woz_ratio: float | None = Field(description="Acquisition price divided by the current WOZ value.")
    woz_growth_abs: int | None = Field(description="Absolute WOZ growth between the oldest and newest known values.")
    woz_growth_pct: float | None = Field(description="Percentage WOZ growth between the oldest and newest known values.")
    history_years: int = Field(description="Number of WOZ history rows considered.")
    woz_history: list[dict[str, Any]] = Field(description="WOZ history rows used in the calculation.")


class GrowthRoiResponse(BaseModel):
    resolved_address: dict[str, Any] = Field(description="Address used for the WOZ lookup.")
    acquisition_price: float | None = Field(
        description="Purchase price used for comparison fields when provided or derived from a listing."
    )
    current_woz_value: int = Field(description="Most recent WOZ value in the selected history window.")
    start_woz_value: int = Field(description="Oldest WOZ value in the selected history window.")
    end_woz_value: int = Field(description="Newest WOZ value in the selected history window.")
    start_year: int = Field(description="Oldest WOZ year included in the calculation.")
    end_year: int = Field(description="Newest WOZ year included in the calculation.")
    history_years: int = Field(description="Number of yearly WOZ rows considered.")
    total_growth_abs: int = Field(description="Absolute WOZ growth over the selected period.")
    total_growth_pct: float | None = Field(description="Percentage WOZ growth over the selected period.")
    average_yoy_growth_pct: float | None = Field(description="Arithmetic mean of the year-over-year WOZ growth percentages.")
    cagr_pct: float | None = Field(description="Compound annual growth rate over the selected period.")
    price_to_current_woz_ratio: float | None = Field(description="Acquisition price divided by the current WOZ value.")
    yearly_growth: list[dict[str, Any]] = Field(description="Year-over-year WOZ growth rows ordered by year descending.")
    woz_history: list[dict[str, Any]] = Field(description="WOZ history rows used in the growth calculation.")


_MODEL_TYPES = {"Any": Any}
for model in (
    ListingResponse,
    SearchListingsResponse,
    LatestIdResponse,
    PollNewListingsResponse,
    PriceHistoryResponse,
    WozHistoryResponse,
    GrossYieldResponse,
    GrowthRoiResponse,
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


def _normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.lower()
    normalized = normalized.replace("&", " ")
    normalized = re.sub(r"[’']", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _slugify_search_text(value: str) -> str:
    normalized = _normalize_search_text(value)
    return normalized.replace(" ", "-")


def _normalize_postcode_token(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value).upper()
    if POSTCODE_PATTERN.fullmatch(compact):
        return compact
    return None


def _contains_alias(haystack: str, alias: str) -> bool:
    padded_haystack = f" {haystack} "
    padded_alias = f" {alias} "
    return padded_alias in padded_haystack


@dataclass(frozen=True)
class ResolvedSearch:
    location: list[str] | None
    radius_km: int | None
    strategy: str
    confidence: str
    notes: list[str]
    original_location: list[str] | None

    def as_metadata(self) -> dict[str, Any]:
        mode = "none"
        if self.location:
            mode = "radius_search" if self.radius_km is not None and len(self.location) == 1 else "selected_area"
        return {
            "original_location": self.original_location,
            "resolved_location": self.location,
            "mode": mode,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "radius_km": self.radius_km,
            "notes": list(self.notes),
        }


def _resolve_single_location(token: str, *, radius_km: int | None) -> ResolvedSearch:
    compact_postcode = _normalize_postcode_token(token)
    original = [token]
    if compact_postcode is not None:
        return ResolvedSearch(
            location=[compact_postcode.lower()],
            radius_km=radius_km,
            strategy="exact_postcode",
            confidence="high",
            notes=[],
            original_location=original,
        )

    normalized = _normalize_search_text(token)
    if not normalized:
        return ResolvedSearch(None, radius_km, "empty", "low", [], original)

    if POSTCODE_PREFIX_PATTERN.fullmatch(normalized):
        return ResolvedSearch(
            location=[normalized],
            radius_km=radius_km,
            strategy="postcode_prefix",
            confidence="high",
            notes=[],
            original_location=original,
        )

    for alias, fallback in NEIGHBOURHOOD_FALLBACKS.items():
        if _contains_alias(normalized, alias):
            effective_radius = radius_km or fallback["default_radius_km"]
            notes = list(fallback.get("notes", []))
            if radius_km is None:
                notes.append(f"Used the default {effective_radius} km radius for this neighbourhood fallback.")
            return ResolvedSearch(
                location=[fallback["anchor"].lower()],
                radius_km=effective_radius,
                strategy="neighbourhood_fallback",
                confidence="medium",
                notes=notes,
                original_location=original,
            )

    city_slug = CITY_ALIAS_MAP.get(normalized)
    if city_slug is not None:
        return ResolvedSearch(
            location=[city_slug],
            radius_km=radius_km,
            strategy="city_alias",
            confidence="high",
            notes=[],
            original_location=original,
        )

    if " " in normalized and not any(char.isdigit() for char in normalized):
        slug = _slugify_search_text(token)
        if slug:
            return ResolvedSearch(
                location=[slug],
                radius_km=radius_km,
                strategy="slugified_text",
                confidence="medium",
                notes=["Sent a slugified location token upstream because no curated alias matched."],
                original_location=original,
            )

    passthrough = token.strip().lower()
    return ResolvedSearch(
        location=[passthrough] if passthrough else None,
        radius_km=radius_km,
        strategy="passthrough",
        confidence="low",
        notes=["Sent the location upstream unchanged because no deterministic resolver matched."],
        original_location=original,
    )


def _resolve_search_location(location: list[str] | None, *, radius_km: int | None) -> ResolvedSearch:
    if not location:
        return ResolvedSearch(None, radius_km, "none", "high", [], None)
    if len(location) == 1:
        return _resolve_single_location(location[0], radius_km=radius_km)

    resolved_locations: list[str] = []
    notes: list[str] = []
    for token in location:
        resolved = _resolve_single_location(token, radius_km=None)
        notes.extend(resolved.notes)
        if resolved.location:
            resolved_locations.extend(resolved.location)
    effective_radius_km = None
    if radius_km is not None:
        notes.append("Ignored radius_km because Funda radius search only supports a single resolved location.")

    return ResolvedSearch(
        location=resolved_locations or location,
        radius_km=effective_radius_km,
        strategy="multi_location",
        confidence="medium",
        notes=notes,
        original_location=list(location),
    )


def _augment_search_resolution(metadata: dict[str, Any], *, returned_count: int) -> dict[str, Any]:
    notes = list(metadata.get("notes") or [])
    if returned_count == 0 and metadata.get("strategy") in {"passthrough", "slugified_text", "multi_location"}:
        notes.append(
            "Search returned zero results. If this was meant to be a neighbourhood, prefer a postcode or add a curated alias."
        )
    metadata["notes"] = notes
    return metadata


def _coerce_house_number(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+)", value)
        if match:
            return int(match.group(1))
    return None


def _is_funda_fingerprint_error(exc: Exception) -> bool:
    return "No working fingerprint found" in str(exc)


def _funda_operation_error(operation: str, exc: Exception) -> RuntimeError:
    message = str(exc).strip() or exc.__class__.__name__
    if _is_funda_fingerprint_error(exc):
        return RuntimeError(
            f"Live Funda {operation} is currently unavailable: the upstream fingerprint is outdated. "
            "The MCP is running in degraded mode. Retry later or use direct WOZ tools where possible."
        )
    if "Search failed" in message:
        return RuntimeError(
            f"Live Funda {operation} failed upstream: {message}. "
            "The MCP is running in degraded mode."
        )
    return RuntimeError(f"Live Funda {operation} is currently unavailable: {message}")


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


def _format_euro(value: int | float | None) -> str | None:
    if value is None:
        return None
    return f"€{int(round(float(value))):,}".replace(",", ".")


def _change_year(change: dict[str, Any]) -> int | None:
    for key in ("peildatum", "timestamp", "date"):
        value = change.get(key)
        if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


def _change_sort_key(change: dict[str, Any]) -> str:
    for key in ("timestamp", "peildatum", "date"):
        value = change.get(key)
        if isinstance(value, str) and ISO_DATE_PREFIX_PATTERN.match(value):
            return value[:10]
    return ""


def _kadaster_history_to_changes(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes = []
    for row in history:
        peildatum = row.get("peildatum")
        value = row.get("woz_value")
        if not isinstance(peildatum, str) or not isinstance(value, int):
            continue
        changes.append(
            {
                "date": peildatum,
                "timestamp": f"{peildatum}T00:00:00",
                "price": value,
                "human_price": _format_euro(value),
                "status": "woz",
                "source": "WOZ Waardeloket",
                "peildatum": peildatum,
            }
        )
    return changes


def _merge_price_history_with_woz(changes: list[dict[str, Any]], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kadaster_years = {row["year"] for row in history if isinstance(row.get("year"), int)}
    merged = [
        dict(change)
        for change in changes
        if not (change.get("status") == "woz" and _change_year(change) in kadaster_years)
    ]
    merged.extend(_kadaster_history_to_changes(history))
    return sorted(merged, key=_change_sort_key, reverse=True)


def _select_growth_history(history: list[dict[str, Any]], *, years: int | None = None) -> list[dict[str, Any]]:
    selected = [
        row
        for row in history
        if isinstance(row.get("peildatum"), str)
        and isinstance(row.get("woz_value"), int)
        and isinstance(row.get("year"), int)
    ]
    if years is not None:
        years = _positive_int("years", years, minimum=2)
        selected = selected[:years]
    if len(selected) < 2:
        raise ValueError("Growth ROI requires at least two yearly WOZ values")
    return selected


def _growth_metrics_from_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    newest = history[0]
    oldest = history[-1]
    newest_value = newest["woz_value"]
    oldest_value = oldest["woz_value"]
    total_growth_abs = newest_value - oldest_value
    total_growth_pct = round((total_growth_abs / oldest_value) * 100, 4) if oldest_value > 0 else None

    yearly_growth: list[dict[str, Any]] = []
    yearly_growth_pcts: list[float] = []
    for index in range(len(history) - 1):
        current = history[index]
        previous = history[index + 1]
        growth_abs = current["woz_value"] - previous["woz_value"]
        growth_pct = round((growth_abs / previous["woz_value"]) * 100, 4) if previous["woz_value"] > 0 else None
        if growth_pct is not None:
            yearly_growth_pcts.append(growth_pct)
        yearly_growth.append(
            {
                "year": current["year"],
                "peildatum": current["peildatum"],
                "woz_value": current["woz_value"],
                "previous_year": previous["year"],
                "previous_peildatum": previous["peildatum"],
                "previous_woz_value": previous["woz_value"],
                "growth_abs": growth_abs,
                "growth_pct": growth_pct,
            }
        )

    cagr_pct = None
    if oldest_value > 0:
        intervals = len(history) - 1
        cagr_pct = round((((newest_value / oldest_value) ** (1 / intervals)) - 1) * 100, 4)

    return {
        "current_woz_value": newest_value,
        "start_woz_value": oldest_value,
        "end_woz_value": newest_value,
        "start_year": oldest["year"],
        "end_year": newest["year"],
        "history_years": len(history),
        "total_growth_abs": total_growth_abs,
        "total_growth_pct": total_growth_pct,
        "average_yoy_growth_pct": round(sum(yearly_growth_pcts) / len(yearly_growth_pcts), 4) if yearly_growth_pcts else None,
        "cagr_pct": cagr_pct,
        "yearly_growth": yearly_growth,
    }


def _street_from_listing(listing: dict[str, Any]) -> str | None:
    street_name = listing.get("street_name")
    if isinstance(street_name, str) and street_name.strip():
        return street_name.strip()
    title = listing.get("title")
    house_number = _coerce_house_number(listing.get("house_number"))
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(house_number, int):
        return None
    match = re.match(rf"^(?P<street>.+?)\s+{re.escape(str(house_number))}(?:\b|$)", title.strip())
    if match:
        street = match.group("street").strip()
        return street or None
    return title.strip()


def _listing_woz_address_variants(listing: dict[str, Any]) -> list[dict[str, Any]]:
    street = _street_from_listing(listing)
    house_number = _coerce_house_number(listing.get("house_number"))
    postcode = listing.get("postcode")
    city = listing.get("city")
    if not street or not isinstance(house_number, int) or not isinstance(postcode, str) or not isinstance(city, str):
        return []

    base = {
        "street": street,
        "house_number": house_number,
        "postcode": postcode,
        "city": city,
    }
    variants = [base]
    extension = listing.get("house_number_ext") or listing.get("house_number_suffix")
    if isinstance(extension, str) and extension.strip():
        suffix = extension.strip()
        variants.insert(0, {**base, "house_number_suffix": suffix})
        normalized_extension = re.sub(r"\s+", "", suffix)
        if len(normalized_extension) == 1 and normalized_extension.isalpha():
            variants.insert(1, {**base, "house_letter": normalized_extension})

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for variant in variants:
        key = (
            variant["street"],
            variant["house_number"],
            variant["postcode"],
            variant["city"],
            variant.get("house_letter"),
            variant.get("house_number_suffix"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(variant)
    return deduped


def _listing_address_snapshot(listing: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        "title": listing.get("title"),
        "street": _street_from_listing(listing),
        "house_number": _coerce_house_number(listing.get("house_number")),
        "house_number_ext": listing.get("house_number_ext") or listing.get("house_number_suffix"),
        "postcode": listing.get("postcode"),
        "city": listing.get("city"),
    }
    return {key: value for key, value in snapshot.items() if value is not None}


def _listing_woz_lookup_error(listing: dict[str, Any], exc: Exception) -> LookupError:
    snapshot = _listing_address_snapshot(listing)
    missing = [field for field in ("street", "house_number", "postcode", "city") if snapshot.get(field) in (None, "")]
    details = ", ".join(f"{key}={value!r}" for key, value in snapshot.items()) or "no address fields extracted"
    if missing:
        reason = f"missing normalized fields: {', '.join(missing)}"
    else:
        reason = str(exc).strip() or exc.__class__.__name__
    return LookupError(
        "Could not derive a unique WOZ address from the listing. "
        "Provide street, house_number, postcode, and city explicitly. "
        f"Reason: {reason}. Extracted fields: {details}."
    )


def _listing_price(listing: dict[str, Any]) -> float | None:
    price = listing.get("price")
    if isinstance(price, bool) or price is None:
        return None
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, str):
        try:
            return float(price)
        except ValueError:
            return None
    return None


class FundaService:
    def __init__(self, client: Funda, woz_client: WozClient | None = None):
        self.client = client
        self.woz_client = woz_client or WozClient()
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
        try:
            with self.client_call(timeout_seconds) as client:
                return ListingResponse(listing=_jsonify(client.get_listing(listing_ref)))
        except Exception as exc:
            raise _funda_operation_error("listing access", exc) from exc

    def _get_listing_payload(self, listing_id_or_url: str, *, timeout_seconds: int) -> dict[str, Any]:
        listing_ref = _validate_listing_ref(listing_id_or_url)
        try:
            with self.client_call(timeout_seconds) as client:
                return _jsonify(client.get_listing(listing_ref))
        except Exception as exc:
            raise _funda_operation_error("listing access", exc) from exc

    def _get_woz_history_for_listing(self, listing: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        variants = _listing_woz_address_variants(listing)
        if not variants:
            raise LookupError("Listing did not contain enough address data for WOZ lookup")

        successes: list[dict[str, Any]] = []
        success_ids: set[Any] = set()
        last_error: Exception | None = None
        for variant in variants:
            try:
                payload = self.woz_client.get_woz_history(timeout_seconds=timeout_seconds, **variant)
            except Exception as exc:
                last_error = exc
                continue
            wozobjectnummer = payload.get("match", {}).get("wozobjectnummer")
            if wozobjectnummer in success_ids:
                continue
            success_ids.add(wozobjectnummer)
            successes.append(payload)

        if len(successes) == 1:
            return successes[0]
        if len(successes) > 1:
            raise LookupError("Listing resolved to multiple WOZ objects")
        if last_error is not None:
            raise last_error
        raise LookupError("No WOZ history found for listing address")

    def _get_listing_woz_history(self, listing_id_or_url: str, *, timeout_seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
        listing = self._get_listing_payload(listing_id_or_url, timeout_seconds=timeout_seconds)
        try:
            woz_history = self._get_woz_history_for_listing(listing, timeout_seconds=timeout_seconds)
        except Exception as exc:
            raise _listing_woz_lookup_error(listing, exc) from exc
        return listing, woz_history

    def _get_direct_woz_history(
        self,
        *,
        street: str | None,
        house_number: int | None,
        postcode: str | None,
        city: str | None,
        house_letter: str | None,
        house_number_suffix: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not all(value is not None for value in (street, house_number, postcode, city)):
            raise ValueError("Provide either listing_id_or_url or a full structured address")
        return self.woz_client.get_woz_history(
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )

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
        original_location = _normalize_location(location)
        offering_type = _normalize_choice("offering_type", offering_type, VALID_OFFERING_TYPES) or "buy"
        sort = _normalize_choice("sort", sort, VALID_SORT_VALUES)
        availability = _normalize_list("availability", availability, VALID_AVAILABILITY)
        construction_type = _normalize_list("construction_type", construction_type, VALID_CONSTRUCTION_TYPES)
        object_type = _normalize_list("object_type", object_type)
        energy_label = _normalize_list("energy_label", energy_label)
        resolved_search = _resolve_search_location(original_location, radius_km=radius_km)
        resolved_location = resolved_search.location
        resolved_radius_km = resolved_search.radius_km
        params = self._build_search_params(
            location=resolved_location,
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
            radius_km=resolved_radius_km,
            sort=sort,
            page=page,
        )

        try:
            with self.client_call(timeout_seconds) as client:
                results = client.search_listing(
                    location=resolved_location,
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
                    radius_km=resolved_radius_km,
                    sort=sort,
                    page=page,
                )
            total_count = self._search_total_count(params, timeout_seconds=timeout_seconds)
        except Exception as exc:
            raise _funda_operation_error("search", exc) from exc
        payload = [_jsonify(listing) for listing in results]
        applied_filters = self._applied_search_filters(
            location=resolved_location,
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
            radius_km=resolved_radius_km,
            sort=sort,
            page=page,
        )
        search_resolution = _augment_search_resolution(
            resolved_search.as_metadata(),
            returned_count=len(payload),
        )
        return SearchListingsResponse(
            total_count=total_count,
            returned_count=len(payload),
            applied_filters=applied_filters,
            search_resolution=search_resolution,
            results=payload,
        )

    def get_latest_id(self, *, timeout_seconds: int = DEFAULT_TIMEOUT) -> LatestIdResponse:
        try:
            with self.client_call(timeout_seconds) as client:
                return LatestIdResponse(latest_id=client.get_latest_id())
        except Exception as exc:
            raise _funda_operation_error("latest-id lookup", exc) from exc

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
        try:
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
        except Exception as exc:
            raise _funda_operation_error("new-listing polling", exc) from exc
        return PollNewListingsResponse(count=len(results), results=results, last_seen_id=last_seen_id)

    def get_price_history(self, listing_id_or_url: str, *, timeout_seconds: int = DEFAULT_TIMEOUT) -> PriceHistoryResponse:
        listing_ref = _validate_listing_ref(listing_id_or_url)
        try:
            with self.client_call(timeout_seconds) as client:
                changes = _jsonify(client.get_price_history(listing_ref))
        except Exception as exc:
            if _is_funda_fingerprint_error(exc):
                raise _funda_operation_error("price-history lookup", exc) from exc
            raise
        try:
            listing = self._get_listing_payload(listing_ref, timeout_seconds=timeout_seconds)
            woz_history = self._get_woz_history_for_listing(listing, timeout_seconds=timeout_seconds)
        except Exception:
            pass
        else:
            changes = _merge_price_history_with_woz(changes, woz_history["history"])
        return PriceHistoryResponse(count=len(changes), changes=changes)

    def get_woz_history(
        self,
        *,
        street: str,
        house_number: int,
        postcode: str,
        city: str,
        house_letter: str | None = None,
        house_number_suffix: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> WozHistoryResponse:
        timeout_seconds = _positive_int("timeout_seconds", timeout_seconds, minimum=1, maximum=MAX_TIMEOUT)
        payload = self.woz_client.get_woz_history(
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )
        return WozHistoryResponse(**payload)

    def calculate_gross_yield(
        self,
        *,
        monthly_rent: float,
        listing_id_or_url: str | None = None,
        acquisition_price: float | None = None,
        street: str | None = None,
        house_number: int | None = None,
        postcode: str | None = None,
        city: str | None = None,
        house_letter: str | None = None,
        house_number_suffix: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> GrossYieldResponse:
        timeout_seconds = _positive_int("timeout_seconds", timeout_seconds, minimum=1, maximum=MAX_TIMEOUT)
        if monthly_rent < 0:
            raise ValueError("monthly_rent must be >= 0")

        woz_history: dict[str, Any]
        if listing_id_or_url is not None:
            listing, woz_history = self._get_listing_woz_history(listing_id_or_url, timeout_seconds=timeout_seconds)
            if acquisition_price is None:
                acquisition_price = _listing_price(listing)
                if acquisition_price is None:
                    raise ValueError("Listing price is unavailable; provide acquisition_price explicitly")
        else:
            if acquisition_price is None:
                raise ValueError("acquisition_price is required for direct address calculations")
            woz_history = self._get_direct_woz_history(
                street=street,
                house_number=house_number,
                postcode=postcode,
                city=city,
                house_letter=house_letter,
                house_number_suffix=house_number_suffix,
                timeout_seconds=timeout_seconds,
            )

        if acquisition_price is None or acquisition_price <= 0:
            raise ValueError("acquisition_price must be > 0")

        annual_rent = round(monthly_rent * 12, 2)
        current_woz_value = woz_history["current_woz_value"]
        history = woz_history["history"]
        oldest_value = history[-1]["woz_value"] if history else None
        woz_growth_abs = None
        woz_growth_pct = None
        if isinstance(current_woz_value, int) and isinstance(oldest_value, int):
            woz_growth_abs = current_woz_value - oldest_value
            if oldest_value > 0:
                woz_growth_pct = round((woz_growth_abs / oldest_value) * 100, 4)

        price_to_current_woz_ratio = None
        if isinstance(current_woz_value, int) and current_woz_value > 0:
            price_to_current_woz_ratio = round(acquisition_price / current_woz_value, 4)

        return GrossYieldResponse(
            resolved_address=woz_history["resolved_address"],
            annual_rent=annual_rent,
            acquisition_price=round(acquisition_price, 2),
            gross_yield_pct=round((annual_rent / acquisition_price) * 100, 4),
            current_woz_value=current_woz_value,
            price_to_current_woz_ratio=price_to_current_woz_ratio,
            woz_growth_abs=woz_growth_abs,
            woz_growth_pct=woz_growth_pct,
            history_years=len(history),
            woz_history=history,
        )

    def calculate_growth_roi(
        self,
        *,
        listing_id_or_url: str | None = None,
        acquisition_price: float | None = None,
        years: int | None = None,
        street: str | None = None,
        house_number: int | None = None,
        postcode: str | None = None,
        city: str | None = None,
        house_letter: str | None = None,
        house_number_suffix: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> GrowthRoiResponse:
        timeout_seconds = _positive_int("timeout_seconds", timeout_seconds, minimum=1, maximum=MAX_TIMEOUT)
        if years is not None:
            years = _positive_int("years", years, minimum=2)

        woz_history: dict[str, Any]
        if listing_id_or_url is not None:
            listing, woz_history = self._get_listing_woz_history(listing_id_or_url, timeout_seconds=timeout_seconds)
            if acquisition_price is None:
                acquisition_price = _listing_price(listing)
        else:
            woz_history = self._get_direct_woz_history(
                street=street,
                house_number=house_number,
                postcode=postcode,
                city=city,
                house_letter=house_letter,
                house_number_suffix=house_number_suffix,
                timeout_seconds=timeout_seconds,
            )

        if acquisition_price is not None and acquisition_price <= 0:
            raise ValueError("acquisition_price must be > 0")

        history = _select_growth_history(woz_history["history"], years=years)
        metrics = _growth_metrics_from_history(history)
        price_to_current_woz_ratio = None
        if acquisition_price is not None and metrics["current_woz_value"] > 0:
            price_to_current_woz_ratio = round(acquisition_price / metrics["current_woz_value"], 4)

        return GrowthRoiResponse(
            resolved_address=woz_history["resolved_address"],
            acquisition_price=round(acquisition_price, 2) if acquisition_price is not None else None,
            current_woz_value=metrics["current_woz_value"],
            start_woz_value=metrics["start_woz_value"],
            end_woz_value=metrics["end_woz_value"],
            start_year=metrics["start_year"],
            end_year=metrics["end_year"],
            history_years=metrics["history_years"],
            total_growth_abs=metrics["total_growth_abs"],
            total_growth_pct=metrics["total_growth_pct"],
            average_yoy_growth_pct=metrics["average_yoy_growth_pct"],
            cagr_pct=metrics["cagr_pct"],
            price_to_current_woz_ratio=price_to_current_woz_ratio,
            yearly_growth=metrics["yearly_growth"],
            woz_history=history,
        )


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
            "The MCP auto-resolves common city aliases, postcodes, and some neighbourhood names before querying Funda. "
            "Defaults: offering_type='buy', availability=['available','negotiations'], object_type=['house','apartment']. "
            "Example: location='leiden', price_max=500000."
        ),
        structured_output=True,
    )
    def search_listings(
        ctx: Context,
        location: Annotated[
            str | list[str] | None,
            Field(description="A city, area, postcode, or list of locations. The MCP may auto-resolve aliases or neighbourhood fallbacks before querying Funda."),
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

    @mcp.tool(
        name="get_woz_history",
        description="Fetch historical WOZ values for one exact Dutch address via Kadaster. Example: street='Reehorst', house_number=13, postcode='8105BG', city='Luttenberg'.",
        structured_output=True,
    )
    def get_woz_history(
        ctx: Context,
        street: Annotated[str, Field(description="Street name of the property.")],
        house_number: Annotated[int, Field(description="House number.", ge=1)],
        postcode: Annotated[str, Field(description="Dutch postcode, with or without a space.")],
        city: Annotated[str, Field(description="City or village name.")],
        house_letter: Annotated[str | None, Field(description="Optional house letter, for example 'A'.")] = None,
        house_number_suffix: Annotated[str | None, Field(description="Optional house number suffix, for example '2' or 'bis'.")] = None,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> WozHistoryResponse:
        return _service(ctx).get_woz_history(
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="calculate_growth_roi",
        description="Calculate property value growth metrics from WOZ history for either a Funda listing or a direct address.",
        structured_output=True,
    )
    def calculate_growth_roi(
        ctx: Context,
        listing_id_or_url: Annotated[str | None, Field(description="Optional 7 to 9 digit Funda listing ID or full detail URL.")] = None,
        acquisition_price: Annotated[float | None, Field(description="Optional purchase price in euros, used only for comparison fields such as price_to_current_woz_ratio.", gt=0)] = None,
        years: Annotated[int | None, Field(description="Optional number of most recent annual WOZ records to include; minimum 2.", ge=2)] = None,
        street: Annotated[str | None, Field(description="Street name for direct address lookups.")] = None,
        house_number: Annotated[int | None, Field(description="House number for direct address lookups.", ge=1)] = None,
        postcode: Annotated[str | None, Field(description="Dutch postcode for direct address lookups.")] = None,
        city: Annotated[str | None, Field(description="City or village name for direct address lookups.")] = None,
        house_letter: Annotated[str | None, Field(description="Optional house letter for direct address lookups.")] = None,
        house_number_suffix: Annotated[str | None, Field(description="Optional house number suffix for direct address lookups.")] = None,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> GrowthRoiResponse:
        return _service(ctx).calculate_growth_roi(
            listing_id_or_url=listing_id_or_url,
            acquisition_price=acquisition_price,
            years=years,
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool(
        name="calculate_gross_yield",
        description="Calculate gross rental yield from either a Funda listing or a direct address, enriched with historical WOZ values. This is a rental yield tool, not a property appreciation ROI tool.",
        structured_output=True,
    )
    def calculate_gross_yield(
        ctx: Context,
        monthly_rent: Annotated[float, Field(description="Expected monthly rent in euros.", ge=0)],
        listing_id_or_url: Annotated[str | None, Field(description="Optional 7 to 9 digit Funda listing ID or full detail URL.")] = None,
        acquisition_price: Annotated[float | None, Field(description="Purchase price in euros; defaults to the listing price when listing_id_or_url is provided and is required for direct-address calculations.", gt=0)] = None,
        street: Annotated[str | None, Field(description="Street name for direct address lookups.")] = None,
        house_number: Annotated[int | None, Field(description="House number for direct address lookups.", ge=1)] = None,
        postcode: Annotated[str | None, Field(description="Dutch postcode for direct address lookups.")] = None,
        city: Annotated[str | None, Field(description="City or village name for direct address lookups.")] = None,
        house_letter: Annotated[str | None, Field(description="Optional house letter for direct address lookups.")] = None,
        house_number_suffix: Annotated[str | None, Field(description="Optional house number suffix for direct address lookups.")] = None,
        timeout_seconds: Annotated[int, Field(description="HTTP timeout in seconds.", ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    ) -> GrossYieldResponse:
        return _service(ctx).calculate_gross_yield(
            monthly_rent=monthly_rent,
            listing_id_or_url=listing_id_or_url,
            acquisition_price=acquisition_price,
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )

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
