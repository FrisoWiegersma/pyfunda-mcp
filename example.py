from funda import Funda

f = Funda()

# Get a listing by ID
listing = f.get_listing(43117443)
print(listing.summary())
print()

# Search for listings
results = f.search_listing('amsterdam', price_max=500000, results=5)
for r in results:
    print(f"{r['title']} ({r['city']}) - €{r['price']:,}")
