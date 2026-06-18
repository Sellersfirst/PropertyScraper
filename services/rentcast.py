import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
RENTCAST_KEY = os.environ["RENTCAST_API_KEY"]
RENTCAST_BASE = "https://api.rentcast.io/v1"


def _range_param(lo, hi, lo_floor=0, hi_ceil=9_999_999) -> str | None:
    """Return 'min-max' range string, or None if both bounds are absent."""
    if lo is None and hi is None:
        return None
    return f"{lo if lo is not None else lo_floor}-{hi if hi is not None else hi_ceil}"


async def search_sold_listings(
    *,
    lat: float,
    lng: float,
    radius_miles: float,
    min_sqft: Optional[int] = None,
    max_sqft: Optional[int] = None,
    min_lot_sqft: Optional[float] = None,
    max_lot_sqft: Optional[float] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_beds: Optional[int] = None,
    max_beds: Optional[int] = None,
    min_baths: Optional[float] = None,
    max_baths: Optional[float] = None,
    max_results: int = 500,
) -> list[dict]:
    """Search inactive (sold) sale listings near a point using Rentcast."""
    params: dict = {
        "latitude": lat,
        "longitude": lng,
        "radius": radius_miles,
        "status": "Inactive",
        "limit": min(max_results, 500),
    }

    for api_key, lo, hi in [
        ("squareFootage", min_sqft, max_sqft),
        ("lotSize", min_lot_sqft, max_lot_sqft),
        ("price", min_price, max_price),
        ("bedrooms", min_beds, max_beds),
        ("bathrooms", min_baths, max_baths),
    ]:
        r = _range_param(lo, hi)
        if r:
            params[api_key] = r

    filter_summary = {k: v for k, v in params.items() if k not in ("latitude", "longitude")}
    log.info("Rentcast /listings/sale — lat=%.4f lng=%.4f | %s", lat, lng, filter_summary)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{RENTCAST_BASE}/listings/sale",
            params=params,
            headers={"X-Api-Key": RENTCAST_KEY},
        )
        resp.raise_for_status()

    listings = resp.json()
    log.info("Rentcast returned %d listing(s)", len(listings))
    return listings
