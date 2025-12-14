# Funda

Python API for [Funda.nl](https://www.funda.nl) real estate listings.

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

```python
from funda import Funda

f = Funda()

# Get a listing by ID
listing = f.get_listing(43117443)
print(listing['title'], listing['city'])
# Reehorst 13 Luttenberg

# Get a listing by URL
listing = f.get_listing('https://www.funda.nl/detail/koop/amsterdam/appartement-123/43117443/')

# Search listings
results = f.search_listing('amsterdam', price_max=500000)
for r in results:
    print(r['title'], r['price'])
```

## API Reference

### Funda

Main entry point for the API.

```python
from funda import Funda

f = Funda(timeout=30)
```

#### get_listing(listing_id)

Get a single listing by ID or URL.

```python
# By numeric ID (tinyId or globalId)
listing = f.get_listing(43117443)

# By URL
listing = f.get_listing('https://www.funda.nl/detail/koop/city/house-name/43117443/')
```

#### search_listing(location, ...)

Search for listings with filters.

```python
results = f.search_listing(
    location='amsterdam',           # City or area name
    offering_type='buy',            # 'buy' or 'rent'
    price_min=200000,               # Minimum price
    price_max=500000,               # Maximum price
    area_min=50,                    # Minimum living area (m²)
    area_max=150,                   # Maximum living area (m²)
    object_type=['house'],          # Property types (default: house, apartment)
    page=0,                         # Page number (15 results per page)
)
```

Search multiple locations:

```python
results = f.search_listing(['amsterdam', 'rotterdam', 'utrecht'])
```

### Listing

Listing objects support dict-like access with convenient aliases.

```python
listing['title']        # Property title/address
listing['city']         # City name
listing['price']        # Numeric price
listing['price_formatted']  # Formatted price string
listing['bedrooms']     # Number of bedrooms
listing['living_area']  # Living area
listing['energy_label'] # Energy label (A, B, C, etc.)
listing['object_type']  # House, Apartment, etc.
listing['coordinates']  # (lat, lng) tuple
listing['photos']       # List of photo IDs
listing['url']          # Funda URL
```

**Key aliases** - these all work:

| Alias | Canonical Key |
|-------|---------------|
| `name`, `address` | `title` |
| `location`, `locality` | `city` |
| `area`, `size` | `living_area` |
| `type`, `property_type` | `object_type` |
| `images`, `pictures`, `media` | `photos` |
| `agent`, `realtor`, `makelaar` | `broker` |
| `zip`, `zipcode`, `postal_code` | `postcode` |

#### Methods

```python
listing.summary()       # Text summary of the listing
listing.to_dict()       # Convert to plain dictionary
listing.keys()          # List available keys
listing.get('key')      # Get with default (like dict.get)
listing.getID()         # Get listing ID
```

## Examples

### Find apartments in Amsterdam under €400k

```python
from funda import Funda

f = Funda()
results = f.search_listing('amsterdam', price_max=400000)

for listing in results:
    print(f"{listing['title']}")
    print(f"  Price: €{listing['price']:,}")
    print(f"  Area: {listing.get('living_area', 'N/A')}")
    print(f"  Bedrooms: {listing.get('bedrooms', 'N/A')}")
    print()
```

### Get detailed listing information

```python
from funda import Funda

f = Funda()
listing = f.get_listing(43117443)

print(listing.summary())

# Access all characteristics
for key, value in listing['characteristics'].items():
    print(f"{key}: {value}")
```

### Search rentals in multiple cities

```python
from funda import Funda

f = Funda()
results = f.search_listing(
    location=['amsterdam', 'rotterdam', 'den-haag'],
    offering_type='rent',
    price_max=2000,
)

print(f"Found {len(results)} rentals")
```

## How It Works

This library uses Funda's undocumented mobile app API, which provides clean JSON responses unlike the website that embeds data in Nuxt.js/JavaScript bundles.

### Discovery Process

The API was reverse engineered by intercepting and analyzing HTTPS traffic from the official Funda Android app:

1. **Traffic Interception**: Configured an Android device to route traffic through an intercepting proxy with a trusted CA certificate installed
2. **App Analysis**: Used the Funda app normally - browsing listings, searching, opening shared URLs
3. **Endpoint Mapping**: Identified the `*.funda.io` API infrastructure separate from the `www.funda.nl` website
4. **Parameter Discovery**: Analyzed request/response patterns to understand the query format and available filters
5. **ID Resolution**: Discovered how the app resolves URL-based IDs (`tinyId`) to internal IDs (`globalId`) by opening shared Funda links in the app

### API Architecture

The mobile app communicates with a separate API at `*.funda.io`:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `listing-detail-page.funda.io/api/v4/listing/object/nl/{globalId}` | GET | Fetch listing by internal ID |
| `listing-detail-page.funda.io/api/v4/listing/object/nl/tinyId/{tinyId}` | GET | Fetch listing by URL ID |
| `listing-search-wonen.funda.io/_msearch/template` | POST | Search listings |

### ID System

Funda uses two ID systems:
- **globalId**: Internal numeric ID (7 digits), used in the database
- **tinyId**: Public-facing ID (8-9 digits), appears in URLs like `funda.nl/detail/koop/amsterdam/.../{tinyId}/`

The `tinyId` endpoint was key - it allows fetching any listing directly from a Funda URL without needing to know the internal ID.

### Search API

Search uses Elasticsearch's [Multi Search Template API](https://www.elastic.co/guide/en/elasticsearch/reference/current/multi-search-template.html) with NDJSON format:

```
{"index":"listings-wonen-searcher-alias-prod"}
{"id":"search_result_20250805","params":{...}}
```

**Search parameters:**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `selected_area` | Location filter | `["amsterdam"]` |
| `offering_type` | Buy or rent | `"buy"` or `"rent"` |
| `price.selling_price` | Price range (buy) | `{"from": 200000, "to": 500000}` |
| `price.rent_price` | Price range (rent) | `{"from": 500, "to": 2000}` |
| `object_type` | Property types | `["house", "apartment"]` |
| `floor_area` | Living area m² | `{"from": 50, "to": 150}` |
| `page.from` | Pagination offset | `0`, `15`, `30`... |

Results are paginated with 15 listings per page.

### Required Headers

```
User-Agent: Dart/3.9 (dart:io)
X-Funda-App-Platform: android
Content-Type: application/json
```

### Response Data

Listing responses include:
- **Identifiers** - globalId, tinyId
- **AddressDetails** - title, city, postcode, province, neighbourhood
- **Price** - numeric and formatted prices (selling or rental)
- **FastView** - bedrooms, living area, energy label
- **Media** - photo IDs, floorplans, videos
- **KenmerkSections** - detailed property characteristics
- **Coordinates** - latitude/longitude
- **ObjectInsights** - view and save counts

## License

AGPL-3.0
