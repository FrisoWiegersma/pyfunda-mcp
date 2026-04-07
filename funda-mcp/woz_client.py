"""Private Kadaster WOZ client for the local MCP server."""

from __future__ import annotations

from collections.abc import Callable
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WOZ_API_BASE = "https://api.kadaster.nl/lvwoz/wozwaardeloket-api/v1"

POSTCODE_PATTERN = re.compile(r"^\d{4}[A-Z]{2}$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WHITESPACE_PATTERN = re.compile(r"\s+")
NON_ALNUM_PATTERN = re.compile(r"[^0-9a-z]+")

MATCH_FIELD_PRIORITY = (
    "street",
    "house_number",
    "postcode",
    "city",
    "house_letter",
    "house_number_suffix",
)
MATCH_FIELD_WEIGHTS = {
    "street": 8,
    "house_number": 16,
    "postcode": 16,
    "city": 4,
    "house_letter": 2,
    "house_number_suffix": 2,
}
CITY_ALIAS_CANONICALS = {
    "denhaag": "sgravenhage",
    "sgravenhage": "sgravenhage",
    "thehague": "sgravenhage",
}


JsonFetcher = Callable[[str, int], dict[str, Any]]


def normalize_postcode(value: str) -> str:
    normalized = re.sub(r"\s+", "", value or "").upper()
    if not POSTCODE_PATTERN.fullmatch(normalized):
        raise ValueError("postcode must be a Dutch postcode like 1234AB")
    return normalized


def normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", (value or "").strip()).casefold()


def canonicalize_city(value: str) -> str:
    normalized = normalize_text(value)
    collapsed = NON_ALNUM_PATTERN.sub("", normalized)
    return CITY_ALIAS_CANONICALS.get(collapsed, collapsed)


def normalize_house_letter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value.strip()).casefold()
    return normalized or None


def normalize_house_number_suffix(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value.strip()).casefold()
    return normalized or None


class WozClient:
    """Small HTTP client for address-based WOZ history lookups."""

    def __init__(self, fetch_json: JsonFetcher | None = None):
        self._fetch_json = fetch_json or self._default_fetch_json

    def _default_fetch_json(self, url: str, timeout_seconds: int) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "pyfunda-mcp/woz",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                raise LookupError("WOZ record not found") from exc
            raise RuntimeError(f"WOZ lookup failed (status {exc.code})") from exc
        except URLError as exc:
            raise ConnectionError(f"Could not reach WOZ service: {exc.reason}") from exc
        return json.loads(payload)

    def _get_json(self, path: str, *, timeout_seconds: int, query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{WOZ_API_BASE}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return self._fetch_json(url, timeout_seconds)

    def _candidate_matches_address(
        self,
        candidate: dict[str, Any],
        *,
        street: str,
        house_number: int,
        postcode: str,
        city: str,
        canonical_city: str,
        house_letter: str | None,
        house_number_suffix: str | None,
    ) -> dict[str, Any]:
        candidate_streets = {
            normalize_text(candidate_street)
            for candidate_street in (candidate.get("straatnaam"), candidate.get("openbareruimtenaam"))
            if candidate_street
        }
        try:
            candidate_house_number = int(candidate.get("huisnummer") or 0)
        except (TypeError, ValueError):
            candidate_house_number = 0
        try:
            candidate_postcode = normalize_postcode(candidate.get("postcode") or "")
        except ValueError:
            candidate_postcode = None
        candidate_city = normalize_text(candidate.get("woonplaatsnaam") or "")
        candidate_city_canonical = canonicalize_city(candidate.get("woonplaatsnaam") or "")

        candidate_letter = normalize_house_letter(candidate.get("huisletter"))
        candidate_suffix = normalize_house_number_suffix(candidate.get("huisnummertoevoeging"))
        return {
            "street": street in candidate_streets,
            "house_number": candidate_house_number == house_number,
            "postcode": candidate_postcode == postcode,
            "city": candidate_city_canonical == canonical_city,
            "city_exact": candidate_city == city,
            "house_letter": house_letter == candidate_letter,
            "house_number_suffix": house_number_suffix == candidate_suffix,
        }

    def _candidate_score(self, evaluation: dict[str, Any]) -> int:
        return sum(MATCH_FIELD_WEIGHTS[field] for field in MATCH_FIELD_PRIORITY if evaluation.get(field))

    def _first_mismatch(self, evaluation: dict[str, Any]) -> str | None:
        for field in MATCH_FIELD_PRIORITY:
            if not evaluation.get(field):
                return field
        return None

    def _format_candidate_address(self, candidate: dict[str, Any]) -> str:
        street = candidate.get("straatnaam") or candidate.get("openbareruimtenaam") or "unknown street"
        house_number = candidate.get("huisnummer")
        number = str(house_number) if house_number is not None else "?"
        suffix = "".join(
            str(value)
            for value in (candidate.get("huisletter") or "", candidate.get("huisnummertoevoeging") or "")
            if value
        )
        house_part = f"{number}{suffix}"
        postcode = candidate.get("postcode")
        city = candidate.get("woonplaatsnaam")
        line = " ".join(part for part in (street, house_part) if part)
        tail = " ".join(str(part) for part in (postcode, city) if part)
        return ", ".join(part for part in (line, tail) if part)

    def resolve_address(
        self,
        *,
        street: str,
        house_number: int,
        postcode: str,
        city: str,
        house_letter: str | None = None,
        house_number_suffix: str | None = None,
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], str]:
        normalized_street = normalize_text(street)
        normalized_city = normalize_text(city)
        canonical_city = canonicalize_city(city)
        normalized_postcode = normalize_postcode(postcode)
        normalized_house_letter = normalize_house_letter(house_letter)
        normalized_house_number_suffix = normalize_house_number_suffix(house_number_suffix)

        if not normalized_street:
            raise ValueError("street must not be empty")
        if not normalized_city:
            raise ValueError("city must not be empty")
        if house_number < 1:
            raise ValueError("house_number must be >= 1")

        suggest = self._get_json("/suggest", timeout_seconds=timeout_seconds, query={"straat": street})
        docs = suggest.get("docs")
        if not isinstance(docs, list):
            raise LookupError("WOZ suggest response did not contain candidates")

        evaluations = [
            (
                candidate,
                self._candidate_matches_address(
                    candidate,
                    street=normalized_street,
                    house_number=house_number,
                    postcode=normalized_postcode,
                    city=normalized_city,
                    canonical_city=canonical_city,
                    house_letter=normalized_house_letter,
                    house_number_suffix=normalized_house_number_suffix,
                ),
            )
            for candidate in docs
            if isinstance(candidate, dict)
        ]

        exact_matches = [
            (
                candidate,
                "strict_exact" if evaluation.get("city_exact") else "city_alias_exact",
            )
            for candidate, evaluation in evaluations
            if all(evaluation.get(field) for field in MATCH_FIELD_PRIORITY)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            raise LookupError("Address resolved to multiple WOZ objects")

        relaxed_matches = [
            (candidate, "primary_fields_unique")
            for candidate, evaluation in evaluations
            if all(evaluation.get(field) for field in MATCH_FIELD_PRIORITY if field != "city")
        ]
        if len(relaxed_matches) == 1:
            return relaxed_matches[0]
        if len(relaxed_matches) > 1:
            raise LookupError("Address resolved to multiple WOZ objects")

        normalized_address = (
            f"street={normalized_street!r}, house_number={house_number}, postcode={normalized_postcode!r}, "
            f"city={normalized_city!r}, house_letter={normalized_house_letter!r}, "
            f"house_number_suffix={normalized_house_number_suffix!r}"
        )
        if not evaluations:
            raise LookupError(f"No exact WOZ match found for the address after normalization ({normalized_address})")

        closest_candidate, closest_evaluation = max(evaluations, key=lambda item: self._candidate_score(item[1]))
        mismatch_field = self._first_mismatch(closest_evaluation) or "unknown field"
        candidate_summary = self._format_candidate_address(closest_candidate)
        raise LookupError(
            "No exact WOZ match found for the address after normalization "
            f"({normalized_address}). Closest candidate mismatched on {mismatch_field}: {candidate_summary}"
        )

    def get_woz_history(
        self,
        *,
        street: str,
        house_number: int,
        postcode: str,
        city: str,
        house_letter: str | None = None,
        house_number_suffix: str | None = None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        candidate, strategy = self.resolve_address(
            street=street,
            house_number=house_number,
            postcode=postcode,
            city=city,
            house_letter=house_letter,
            house_number_suffix=house_number_suffix,
            timeout_seconds=timeout_seconds,
        )

        detail = self._get_json(
            f"/wozwaarde/wozobjectnummer/{candidate['wozobjectnummer']}",
            timeout_seconds=timeout_seconds,
        )

        woz_object = detail.get("wozObject")
        if not isinstance(woz_object, dict):
            raise LookupError("WOZ detail response did not contain a WOZ object")

        normalized_history = []
        for row in detail.get("wozWaarden", []):
            if not isinstance(row, dict):
                continue
            peildatum = row.get("peildatum")
            value = row.get("vastgesteldeWaarde")
            if not isinstance(peildatum, str) or not ISO_DATE_PATTERN.fullmatch(peildatum):
                continue
            if not isinstance(value, int):
                continue
            normalized_history.append(
                {
                    "peildatum": peildatum,
                    "woz_value": value,
                    "year": int(peildatum[:4]),
                }
            )
        normalized_history.sort(key=lambda row: row["peildatum"], reverse=True)

        return {
            "resolved_address": {
                "street": woz_object.get("straatnaam") or woz_object.get("openbareruimtenaam"),
                "house_number": woz_object.get("huisnummer"),
                "house_letter": woz_object.get("huisletter"),
                "house_number_suffix": woz_object.get("huisnummertoevoeging"),
                "postcode": normalize_postcode(woz_object.get("postcode") or ""),
                "city": woz_object.get("woonplaatsnaam"),
            },
            "match": {
                "strategy": strategy,
                "wozobjectnummer": woz_object.get("wozobjectnummer"),
                "adresseerbaarobjectid": woz_object.get("adresseerbaarobjectid"),
                "nummeraanduidingid": woz_object.get("nummeraanduidingid"),
            },
            "count": len(normalized_history),
            "history": normalized_history,
            "current_woz_value": normalized_history[0]["woz_value"] if normalized_history else None,
            "metadata": {
                "grondoppervlakte": woz_object.get("grondoppervlakte"),
                "panden": detail.get("panden", []),
                "kadastrale_objecten": detail.get("kadastraleObjecten", []),
            },
        }
