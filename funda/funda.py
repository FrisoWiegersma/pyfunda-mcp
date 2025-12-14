"""Main Funda API class."""

import re
from typing import Any

import requests

from funda.listing import Listing


# API endpoints
API_BASE = "https://listing-detail-page.funda.io/api/v4/listing/object/nl"
API_LISTING = f"{API_BASE}/{{listing_id}}"
API_LISTING_TINY = f"{API_BASE}/tinyId/{{tiny_id}}"
API_SEARCH = "https://listing-search-wonen.funda.io/_msearch/template"

# Headers for mobile API
HEADERS = {
    "user-agent": "Dart/3.9 (dart:io)",
    "x-funda-app-platform": "android",
    "content-type": "application/json",
}

SEARCH_HEADERS = {
    "user-agent": "Dart/3.9 (dart:io)",
    "content-type": "application/x-ndjson",
    "referer": "https://www.funda.nl/",
}


class Funda:
    """Main interface to Funda API.

    Example:
        >>> from funda import Funda
        >>> f = Funda()
        >>> listing = f.get_listing(43117443)
        >>> print(listing['title'], listing['city'])
        Reehorst 13 Luttenberg
        >>> results = f.search_listing('amsterdam', price_max=500000)
        >>> for r in results[:3]:
        ...     print(r['title'], r['price'])
    """

    TINYID_PATTERN = re.compile(r"/(\d{7,9})/?(?:\?|$|#)")

    def __init__(self, timeout: int = 30):
        """Initialize Funda API client.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout
        self._session: requests.Session | None = None

    @property
    def session(self) -> requests.Session:
        """Lazily create HTTP session."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(HEADERS)
        return self._session

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self) -> "Funda":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # -------------------------------------------------------------------------
    # Listing methods
    # -------------------------------------------------------------------------

    def get_listing(self, listing_id: int | str) -> Listing:
        """Get a listing by ID or URL.

        Args:
            listing_id: Numeric ID (globalId or tinyId) or full URL

        Returns:
            Listing object with property data

        Example:
            >>> f.get_listing(43117443)
            >>> f.get_listing('https://www.funda.nl/detail/koop/city/house-123/43117443/')
        """
        # If it's a URL, extract tinyId
        if isinstance(listing_id, str) and 'funda.nl' in listing_id:
            match = self.TINYID_PATTERN.search(listing_id)
            if not match:
                raise ValueError(f"Could not extract listing ID from URL: {listing_id}")
            listing_id = match.group(1)

        # Try tinyId endpoint first (8-9 digits), then globalId (7 digits)
        listing_id_str = str(listing_id)
        if len(listing_id_str) >= 8:
            url = API_LISTING_TINY.format(tiny_id=listing_id_str)
        else:
            url = API_LISTING.format(listing_id=listing_id_str)

        response = self.session.get(url, timeout=self.timeout)

        # If tinyId fails, try as globalId
        if response.status_code == 404 and len(listing_id_str) >= 8:
            url = API_LISTING.format(listing_id=listing_id_str)
            response = self.session.get(url, timeout=self.timeout)

        if response.status_code != 200:
            raise LookupError(f"Listing {listing_id} not found")

        data = response.json()
        return self._parse_listing(data)

    def search_listing(
        self,
        location: str | list[str] | None = None,
        offering_type: str = "buy",
        price_min: int | None = None,
        price_max: int | None = None,
        area_min: int | None = None,
        area_max: int | None = None,
        object_type: list[str] | None = None,
        page: int = 0,
    ) -> list[Listing]:
        """Search for listings.

        Args:
            location: City/area name(s) to search in
            offering_type: "buy" or "rent"
            price_min: Minimum price
            price_max: Maximum price
            area_min: Minimum living area in m²
            area_max: Maximum living area in m²
            object_type: Property types (e.g. ["house", "apartment"])
            page: Page number (0-indexed, 15 results per page)

        Returns:
            List of Listing objects (max 15 per page)

        Example:
            >>> f.search_listing('amsterdam', price_max=500000)
            >>> f.search_listing(['amsterdam', 'rotterdam'], offering_type='rent')
        """
        import json

        # Normalize location to list
        locations = None
        if location:
            locations = [location] if isinstance(location, str) else list(location)

        # Build search params
        params: dict[str, Any] = {
            "availability": ["available", "negotiations"],
            "type": ["single"],
            "zoning": ["residential"],
            "object_type": object_type or ["house", "apartment"],
            "publication_date": {"no_preference": True},
            "offering_type": offering_type,
            "page": {"from": page * 15},
            "sort": {"field": None, "order": None},
        }

        if locations:
            params["selected_area"] = locations

        # Price filter - format depends on offering type
        if price_min is not None or price_max is not None:
            price_key = "selling_price" if offering_type == "buy" else "rent_price"
            price_filter: dict[str, int] = {}
            if price_min:
                price_filter["from"] = price_min
            if price_max:
                price_filter["to"] = price_max
            params["price"] = {price_key: price_filter}

        if area_min is not None or area_max is not None:
            floor_filter: dict[str, int] = {}
            if area_min:
                floor_filter["from"] = area_min
            if area_max:
                floor_filter["to"] = area_max
            params["floor_area"] = floor_filter

        # Build NDJSON query
        index_line = json.dumps({"index": "listings-wonen-searcher-alias-prod"})
        query_line = json.dumps({"id": "search_result_20250805", "params": params})
        query = f"{index_line}\n{query_line}\n"

        response = requests.post(
            API_SEARCH,
            headers=SEARCH_HEADERS,
            data=query,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise RuntimeError(f"Search failed (status {response.status_code})")

        data = response.json()
        return self._parse_search_results(data)

    # -------------------------------------------------------------------------
    # Parsing methods
    # -------------------------------------------------------------------------

    def _parse_listing(self, data: dict) -> Listing:
        """Parse API response into Listing object."""
        identifiers = data.get("Identifiers", {})
        address = data.get("AddressDetails", {})
        price_data = data.get("Price", {})
        coords = data.get("Coordinates", {})
        media = data.get("Media", {})
        fast_view = data.get("FastView", {})

        # Build listing data
        listing_data = {
            "global_id": identifiers.get("GlobalId"),
            "tiny_id": identifiers.get("TinyId"),
            "title": address.get("Title"),
            "city": address.get("City"),
            "postcode": address.get("PostCode"),
            "province": address.get("Province"),
            "neighbourhood": address.get("NeighborhoodName"),
            "price": price_data.get("NumericSellingPrice") or price_data.get("NumericRentalPrice"),
            "price_formatted": price_data.get("SellingPrice") or price_data.get("RentalPrice"),
            "offering_type": data.get("OfferingType"),
            "object_type": data.get("ObjectType"),
            "construction_type": data.get("ConstructionType"),
            "status": "sold" if data.get("IsSoldOrRented") else "available",
            "energy_label": fast_view.get("EnergyLabel"),
            "living_area": fast_view.get("LivingArea"),
            "plot_area": fast_view.get("PlotArea"),
            "bedrooms": fast_view.get("NumberOfBedrooms"),
            "description": data.get("ListingDescription", {}).get("Description"),
        }

        # Coordinates
        if coords.get("Latitude") and coords.get("Longitude"):
            listing_data["latitude"] = float(coords["Latitude"])
            listing_data["longitude"] = float(coords["Longitude"])
            listing_data["coordinates"] = (listing_data["latitude"], listing_data["longitude"])

        # Photos
        photos = media.get("Photos", {}).get("Items", [])
        listing_data["photos"] = [p.get("Id") for p in photos if p.get("Id")]
        listing_data["photo_count"] = len(listing_data["photos"])

        # Floorplans
        floorplans = media.get("LegacyFloorPlan", {}).get("Items", [])
        listing_data["floorplans"] = [f.get("Id") for f in floorplans if f.get("Id")]

        # Videos
        videos = media.get("Videos", {})
        video_items = videos.get("Items", [])
        listing_data["videos"] = [v.get("Id") for v in video_items if v.get("Id")]

        # URL
        city_slug = address.get("City", "").lower().replace(" ", "-")
        title_slug = address.get("Title", "").lower().replace(" ", "-")
        tiny_id = identifiers.get("TinyId")
        offering = "koop" if data.get("OfferingType") == "Sale" else "huur"
        listing_data["url"] = f"https://www.funda.nl/detail/{offering}/{city_slug}/{title_slug}/{tiny_id}/"

        # Characteristics
        characteristics = {}
        for section in data.get("KenmerkSections", []):
            for item in section.get("KenmerkenList", []):
                if item.get("Label") and item.get("Value"):
                    characteristics[item["Label"]] = item["Value"]
        listing_data["characteristics"] = characteristics

        # Broker
        tracking = data.get("Tracking", {}).get("Values", {})
        brokers = tracking.get("brokers", [])
        if brokers:
            listing_data["broker_id"] = brokers[0].get("broker_id")
            listing_data["broker_association"] = brokers[0].get("broker_association")

        # Insights
        insights = data.get("ObjectInsights", {})
        if insights:
            listing_data["views"] = insights.get("Views")
            listing_data["saves"] = insights.get("Saves")

        return Listing(
            listing_id=identifiers.get("TinyId") or identifiers.get("GlobalId"),
            data=listing_data
        )

    def _parse_search_results(self, data: dict) -> list[Listing]:
        """Parse search API response into list of Listings."""
        listings = []
        responses = data.get("responses", [])

        if not responses:
            return listings

        hits = responses[0].get("hits", {}).get("hits", [])

        for hit in hits:
            source = hit.get("_source", {})
            address = source.get("address", {})

            # Price
            price_data = source.get("price", {})
            if isinstance(price_data, dict):
                price = price_data.get("selling_price", [None])[0]
                if not price:
                    price = price_data.get("rent_price", [None])[0] if price_data.get("rent_price") else None
            else:
                price = price_data

            listing_data = {
                "global_id": int(hit.get("_id", 0)),
                "title": f"{address.get('street_name', '')} {address.get('house_number', '')}".strip(),
                "city": address.get("city"),
                "postcode": address.get("postal_code"),
                "province": address.get("province"),
                "neighbourhood": address.get("neighbourhood"),
                "price": price,
                "living_area": source.get("floor_area", [None])[0] if source.get("floor_area") else None,
                "bedrooms": source.get("number_of_bedrooms"),
                "energy_label": source.get("energy_label"),
                "object_type": source.get("object_type"),
                "construction_type": source.get("construction_type"),
                "photos": source.get("thumbnail_id", [])[:5],
            }

            listings.append(Listing(
                listing_id=hit.get("_id"),
                data=listing_data
            ))

        return listings


# Convenience alias
FundaAPI = Funda
