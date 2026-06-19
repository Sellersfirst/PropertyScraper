import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from models import (
    ComparableSalesRequest,
    ComparableSalesResponse,
    ComparableProperty,
    PropertySummary,
    SaleEvent,
)
from services.geo import haversine_miles
from services.redfin import (
    extract_zip,
    geocode_nominatim,
    scrape_property_details,
    scrape_sale_history,
    search_sold_by_zip,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Property Comparable Sales", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://property-finder-eight-tau.vercel.app",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Sale gap helper ───────────────────────────────────────────────────────────

def _parse_event_date(s: str) -> Optional[datetime]:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _check_sale_gap(history: list[SaleEvent], max_months: float) -> bool:
    """True if the most-recent buy→sell cycle is within max_months (or gap can't be determined)."""
    sold_dates = sorted(
        filter(None, (_parse_event_date(ev.date) for ev in history if "sold" in ev.event.lower())),
        reverse=True,
    )
    if len(sold_dates) < 2:
        return True
    gap_months = (sold_dates[0] - sold_dates[1]).days / 30.44
    log.debug("Sale gap: %.1f months (limit: %.1f)", gap_months, max_months)
    return gap_months <= max_months


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@app.post("/comparable-sales", response_model=ComparableSalesResponse)
async def comparable_sales(req: ComparableSalesRequest):
    t_start = time.monotonic()
    log.info(
        "Request — address=%r, redfin_url=%r, radius=%.1f mi, max=%d",
        req.address, req.redfin_url, req.radius_miles, req.max_comparables,
    )
    log.info(
        "Filters — sqft=%s–%s, lot=%s–%s, price=%s–%s, beds=%s–%s, baths=%s–%s, "
        "lookback=%s yr, sale_gap=%s mo",
        req.min_sqft, req.max_sqft,
        req.min_lot_sqft, req.max_lot_sqft,
        req.min_price, req.max_price,
        req.min_beds, req.max_beds,
        req.min_baths, req.max_baths,
        req.lookback_years, req.max_sale_gap_months,
    )

    # ── 1. Resolve target property (lat/lng + ZIP) ───────────────────────────
    zip_code: Optional[str] = None

    if req.redfin_url:
        log.info("Step 1 — scraping target details from Redfin URL")
        target = await scrape_property_details(req.redfin_url)
        if target.lat is None or target.lng is None:
            raise HTTPException(status_code=422, detail="Could not determine lat/lng.")
        # ZIP from Redfin URL or scraped address
        zip_code = extract_zip(req.redfin_url) or extract_zip(target.address or "")
        log.info(
            "Step 1 done — %r | sq_ft=%s | zip=%s | (%.6f, %.6f)",
            target.address, target.sq_ft, zip_code, target.lat, target.lng,
        )
    else:
        log.info("Step 1 — geocoding address via Nominatim")
        try:
            lat, lng, display_name, zip_code = await geocode_nominatim(req.address)
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e))
        target = PropertySummary(address=display_name, lat=lat, lng=lng)
        log.info("Step 1 done — %r | zip=%s | (%.6f, %.6f)", display_name, zip_code, lat, lng)

    if not zip_code:
        raise HTTPException(
            status_code=422,
            detail="Could not determine ZIP code for this property.",
        )

    # ── 2. Scrape Redfin sold listings for this ZIP ──────────────────────────
    log.info("Step 2 — scraping Redfin sold listings in ZIP %s via ScrapingBee", zip_code)
    candidates = await search_sold_by_zip(
        zipcode=zip_code,
        min_sqft=req.min_sqft,
        max_sqft=req.max_sqft,
        min_price=req.min_price,
        max_price=req.max_price,
        min_beds=req.min_beds,
        max_beds=req.max_beds,
        min_baths=req.min_baths,
        lookback_years=req.lookback_years,
        max_results=min(req.max_comparables * 10, 100),
    )
    log.info("Step 2 done — %d candidate(s) from Redfin search", len(candidates))

    # ── 3. Post-filter: distance + geocode missing lat/lng ───────────────────
    log.info("Step 3 — distance-filtering %d candidates (radius=%.1f mi)", len(candidates), req.radius_miles)
    filtered: list[dict] = []
    for c in candidates:
        c_lat = c.get("lat")
        c_lng = c.get("lng")

        # If lat/lng not in page JSON, geocode via Nominatim (1 req/sec rate limit)
        if (c_lat is None or c_lng is None) and c.get("address"):
            try:
                await asyncio.sleep(1.1)
                c_lat, c_lng, _, _ = await geocode_nominatim(c["address"])
                c["lat"], c["lng"] = c_lat, c_lng
            except Exception as e:
                log.warning("Could not geocode %r: %s", c.get("address"), e)
                continue

        if c_lat is None or c_lng is None:
            continue

        dist = haversine_miles(target.lat, target.lng, c_lat, c_lng)
        if dist < 0.001:
            continue  # skip the target property itself

        if dist > req.radius_miles:
            continue

        # sqft
        sq_ft = c.get("sq_ft")
        if req.min_sqft and sq_ft is not None and sq_ft < req.min_sqft:
            continue
        if req.max_sqft and sq_ft is not None and sq_ft > req.max_sqft:
            continue

        # price
        price = c.get("price")
        if req.min_price and price is not None and price < req.min_price:
            continue
        if req.max_price and price is not None and price > req.max_price:
            continue

        # beds
        beds = c.get("bedrooms")
        if req.min_beds and beds is not None and beds < req.min_beds:
            continue
        if req.max_beds and beds is not None and beds > req.max_beds:
            continue

        # baths
        baths = c.get("bathrooms")
        if req.min_baths and baths is not None and baths < req.min_baths:
            continue
        if req.max_baths and baths is not None and baths > req.max_baths:
            continue

        # lot size
        lot = c.get("lot_size_sqft")
        if req.min_lot_sqft and lot is not None and lot < req.min_lot_sqft:
            continue
        if req.max_lot_sqft and lot is not None and lot > req.max_lot_sqft:
            continue

        filtered.append({**c, "distance_miles": round(dist, 3)})

    filtered.sort(key=lambda x: x["distance_miles"])
    selected = filtered[: req.max_comparables]
    log.info(
        "Step 3 done — %d within radius, selecting top %d by distance",
        len(filtered), len(selected),
    )

    # ── 4. Scrape sale history in parallel + apply sale gap filter ───────────
    log.info("Step 4 — scraping sale history for %d comparable(s) in parallel", len(selected))

    async def _fetch_history(prop: dict) -> tuple[dict, list[SaleEvent]]:
        comp_url = prop.get("full_url")
        if not comp_url:
            return prop, []
        try:
            return prop, await scrape_sale_history(comp_url)
        except Exception as e:
            log.warning("History scrape failed for %s: %s", comp_url, e)
            return prop, []

    history_results = await asyncio.gather(*(_fetch_history(p) for p in selected))

    lookback_cutoff: Optional[datetime] = None
    if req.lookback_years:
        lookback_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=req.lookback_years * 365.25)

    comparables: list[ComparableProperty] = []
    for prop, history in history_results:
        addr = prop.get("address") or ""

        if req.max_sale_gap_months and history:
            if not _check_sale_gap(history, req.max_sale_gap_months):
                log.info("Excluded %r — sale gap > %.1f mo", addr, req.max_sale_gap_months)
                continue

        if lookback_cutoff and history:
            sold_dates = [
                _parse_event_date(ev.date)
                for ev in history
                if "sold" in ev.event.lower()
            ]
            sold_dates = [d for d in sold_dates if d]
            if sold_dates and max(sold_dates) < lookback_cutoff:
                log.info("Excluded %r — most recent sale outside lookback window", addr)
                continue

        comparables.append(
            ComparableProperty(
                redfin_url=prop.get("full_url"),
                address=addr or None,
                sq_ft=prop.get("sq_ft"),
                lot_size_sqft=prop.get("lot_size_sqft"),
                bedrooms=prop.get("bedrooms"),
                bathrooms=prop.get("bathrooms"),
                list_price=prop.get("price"),
                distance_miles=prop["distance_miles"],
                sale_history=history,
            )
        )

    elapsed = time.monotonic() - t_start
    log.info("Done — %d comparable(s) in %.1fs", len(comparables), elapsed)

    return ComparableSalesResponse(
        target=target,
        comparables=comparables,
        total_candidates_found=len(filtered),
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
