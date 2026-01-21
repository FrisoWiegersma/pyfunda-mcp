"""
Funda - Python API for Funda.nl real estate listings.

Example usage:
    >>> from funda import Funda
    >>> f = Funda()
    >>> listing = f.get_listing(43117443)
    >>> print(listing['title'], listing['price'])
    Reehorst 13 695000

    >>> results = f.search_listing('amsterdam', price_max=500000)
    >>> for r in results[:3]:
    ...     print(r['title'], r['city'])
"""

from funda.funda import Funda, FundaAPI
from funda.listing import Listing

__version__ = "2.1.0"
__all__ = ["Funda", "FundaAPI", "Listing", "__version__"]
