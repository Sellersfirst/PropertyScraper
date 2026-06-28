import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

import database

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
    iter_sold_pages,
    scrape_property_details,
    scrape_sale_history,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    yield


app = FastAPI(title="Property Comparable Sales", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "https://property-finder-eight-tau.vercel.app",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price_str(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"[\d,]+", s.replace("$", ""))
    return int(m.group().replace(",", "")) if m else None


def _extract_sale_pair(history: list[SaleEvent]) -> dict:
    sold_indices = [i for i, ev in enumerate(history) if "sold" in ev.event.lower()]
    if not sold_indices:
        return {}

    def _price_for_sold(sold_idx: int) -> Optional[int]:
        for j in range(sold_idx + 1, len(history)):
            if "listed" in history[j].event.lower():
                return _parse_price_str(history[j].price)
        return None

    result: dict = {}
    i_sale = sold_indices[0]
    result["sale_date"] = history[i_sale].date
    result["sale_price"] = _price_for_sold(i_sale)

    if len(sold_indices) >= 2:
        i_buy = sold_indices[1]
        result["buy_date"] = history[i_buy].date
        result["buy_price"] = _price_for_sold(i_buy)

        sd = _parse_event_date(result["sale_date"])
        bd = _parse_event_date(result["buy_date"])
        if sd and bd:
            result["hold_days"] = (sd - bd).days

        if result.get("sale_price") and result.get("buy_price"):
            result["spread"] = result["sale_price"] - result["buy_price"]

    return result


def _parse_event_date(s: str) -> Optional[datetime]:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _check_sale_gap(history: list[SaleEvent], max_months: float) -> bool:
    sold_dates = sorted(
        filter(None, (_parse_event_date(ev.date) for ev in history if "sold" in ev.event.lower())),
        reverse=True,
    )
    if len(sold_dates) < 2:
        return True
    gap_months = (sold_dates[0] - sold_dates[1]).days / 30.44
    return gap_months <= max_months


# ─── Core search generator ────────────────────────────────────────────────────
#
# Yields progress event dicts throughout, then a final {"type": "complete", "data": ...}.
# Both endpoints consume this — the SSE endpoint forwards every event,
# the JSON endpoint collects only the "complete" one.
#
# Event types:
#   resolving   — address lookup started
#   resolved    — zip/lat/lng known
#   page        — one Redfin results page processed
#   scraping    — history fetch started for one property
#   qualified   — property passed all filters, added to comparables
#   skipped     — property failed a history filter
#   complete    — all done, full response in "data"
#   error       — fatal error, "message" has detail

async def _search_stream(req: ComparableSalesRequest):
    t_start = time.monotonic()

    # ── 1. Resolve address ───────────────────────────────────────────────────
    yield {"type": "resolving", "message": "Resolving address…"}

    zip_code: Optional[str] = None
    target: Optional[PropertySummary] = None
    try:
        if req.redfin_url:
            target = await scrape_property_details(req.redfin_url)
            if target.lat is None or target.lng is None:
                yield {"type": "error", "message": "Could not determine lat/lng from Redfin URL."}
                return
            zip_code = extract_zip(req.redfin_url) or extract_zip(target.address or "")
        else:
            lat, lng, display_name, zip_code = await geocode_nominatim(req.address)
            target = PropertySummary(address=display_name, lat=lat, lng=lng)
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        return

    if not zip_code:
        yield {"type": "error", "message": "Could not determine ZIP code for this property."}
        return

    yield {
        "type": "resolved",
        "zip": zip_code,
        "address": target.address,
        "lat": target.lat,
        "lng": target.lng,
    }

    # ── helpers (close over req / target / lookback_cutoff) ─────────────────
    lookback_cutoff: Optional[datetime] = None
    if req.lookback_years:
        lookback_cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=req.lookback_years * 365.25)
        )

    async def _fetch_history(prop: dict) -> tuple[dict, list[SaleEvent]]:
        comp_url = prop.get("full_url")
        if not comp_url:
            return prop, []
        try:
            events, extra = await scrape_sale_history(comp_url)
            # Fill in lot_size_sqft, sq_ft, garage, pool if the search card didn't have them
            merged = {**prop, **{k: v for k, v in extra.items() if prop.get(k) is None and v is not None}}
            return merged, events
        except Exception as e:
            log.warning("History scrape failed for %s: %s", comp_url, e)
            return prop, []

    def _passes_basic_filters(c: dict) -> tuple[bool, float]:
        c_lat, c_lng = c.get("lat"), c.get("lng")
        if c_lat is None or c_lng is None:
            return False, 0.0
        dist = haversine_miles(target.lat, target.lng, c_lat, c_lng)
        if dist < 0.001 or dist > req.radius_miles:
            return False, dist
        sq_ft = c.get("sq_ft")
        if req.min_sqft and sq_ft is not None and sq_ft < req.min_sqft:
            return False, dist
        if req.max_sqft and sq_ft is not None and sq_ft > req.max_sqft:
            return False, dist
        price = c.get("price")
        if req.min_price and price is not None and price < req.min_price:
            return False, dist
        if req.max_price and price is not None and price > req.max_price:
            return False, dist
        beds = c.get("bedrooms")
        if req.min_beds and beds is not None and beds < req.min_beds:
            return False, dist
        if req.max_beds and beds is not None and beds > req.max_beds:
            return False, dist
        baths = c.get("bathrooms")
        if req.min_baths and baths is not None and baths < req.min_baths:
            return False, dist
        if req.max_baths and baths is not None and baths > req.max_baths:
            return False, dist
        lot = c.get("lot_size_sqft")
        if req.min_lot_sqft and lot is not None and lot < req.min_lot_sqft:
            return False, dist
        if req.max_lot_sqft and lot is not None and lot > req.max_lot_sqft:
            return False, dist
        return True, dist

    def _passes_history_filters(history: list[SaleEvent], addr: str) -> tuple[bool, str]:
        """Returns (passes, reason_if_failed)."""
        if req.max_sale_gap_months and history:
            if not _check_sale_gap(history, req.max_sale_gap_months):
                return False, f"sale gap > {req.max_sale_gap_months:.0f} mo"
        if lookback_cutoff and history:
            sold_dates = [
                _parse_event_date(ev.date)
                for ev in history if "sold" in ev.event.lower()
            ]
            sold_dates = [d for d in sold_dates if d]
            if len(sold_dates) < 2 or sold_dates[1] < lookback_cutoff:
                return False, "buy+resell pair not within lookback window"
        return True, ""

    # ── 2–4. Page-by-page loop ───────────────────────────────────────────────
    comparables: list[ComparableProperty] = []
    total_candidates_seen: int = 0
    processed_count: int = 0  # total properties whose history has been checked

    async for page_num, page_candidates, available_pages in iter_sold_pages(zip_code, req.sold_within):
        total_candidates_seen += len(page_candidates)

        page_filtered = []
        for c in page_candidates:
            passes, dist = _passes_basic_filters(c)
            if passes:
                page_filtered.append({**c, "distance_miles": round(dist, 3)})

        yield {
            "type": "page",
            "page": page_num,
            "total_pages": available_pages,
            "candidates": len(page_candidates),
            "passed_filters": len(page_filtered),
            "qualified_count": len(comparables),
            "processed_count": processed_count,
        }

        # Batch size scales with max_comparables — bigger target = bigger parallel batches
        batch_size = min(req.max_comparables, 5)
        offset = 0
        while offset < len(page_filtered) and len(comparables) < req.max_comparables:
            batch = page_filtered[offset:offset + batch_size]
            offset += batch_size

            batch_results = await asyncio.gather(*(_fetch_history(p) for p in batch))

            for prop, history in batch_results:
                if len(comparables) >= req.max_comparables:
                    break

                processed_count += 1
                addr = prop.get("address") or ""
                url = prop.get("full_url") or ""
                passes, reason = _passes_history_filters(history, addr)

                if not passes:
                    yield {
                        "type": "skipped",
                        "address": addr,
                        "redfin_url": url,
                        "reason": reason,
                        "processed_count": processed_count,
                        "qualified_count": len(comparables),
                    }
                    continue

                sale_pair = _extract_sale_pair(history)
                comparable = ComparableProperty(
                    redfin_url=url or None,
                    address=addr or None,
                    sq_ft=prop.get("sq_ft"),
                    lot_size_sqft=prop.get("lot_size_sqft"),
                    bedrooms=prop.get("bedrooms"),
                    bathrooms=prop.get("bathrooms"),
                    pool=prop.get("pool"),
                    garage=prop.get("garage"),
                    list_price=prop.get("price"),
                    distance_miles=prop["distance_miles"],
                    sale_date=sale_pair.get("sale_date"),
                    sale_price=sale_pair.get("sale_price"),
                    buy_date=sale_pair.get("buy_date"),
                    buy_price=sale_pair.get("buy_price"),
                    hold_days=sale_pair.get("hold_days"),
                    spread=sale_pair.get("spread"),
                    sale_history=history,
                )
                comparables.append(comparable)
                yield {
                    "type": "qualified",
                    "address": addr,
                    "redfin_url": url,
                    "processed_count": processed_count,
                    "qualified_count": len(comparables),
                    "max": req.max_comparables,
                    "property": comparable.model_dump(),
                }

        if len(comparables) >= req.max_comparables:
            break

    elapsed = time.monotonic() - t_start
    log.info("Done — %d comparable(s) from %d candidates in %.1fs", len(comparables), total_candidates_seen, elapsed)

    response = ComparableSalesResponse(
        target=target,
        comparables=comparables,
        total_candidates_found=total_candidates_seen,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        await database.save_search(req.model_dump(), response.model_dump())
    except Exception as e:
        log.warning("Failed to save search to DB: %s", e)

    yield {
        "type": "complete",
        "elapsed_seconds": round(elapsed, 1),
        "data": response.model_dump(),
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/comparable-sales/stream")
async def comparable_sales_stream(req: ComparableSalesRequest):
    """SSE endpoint — streams progress events then the full result."""
    async def event_stream():
        try:
            async for event in _search_stream(req):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering on Railway
        },
    )


@app.post("/comparable-sales", response_model=ComparableSalesResponse)
async def comparable_sales(req: ComparableSalesRequest):
    """JSON endpoint — runs the same search, returns only the final result."""
    async for event in _search_stream(req):
        if event["type"] == "complete":
            return event["data"]
        if event["type"] == "error":
            raise HTTPException(status_code=422, detail=event["message"])
    raise HTTPException(status_code=500, detail="Search did not complete")


# ── Search history endpoints ───────────────────────────────────────────────────

@app.get("/searches")
async def list_searches(limit: int = 50, offset: int = 0):
    return await database.list_searches(limit=limit, offset=offset)


@app.get("/searches/{search_id}")
async def get_search(search_id: int):
    record = await database.get_search(search_id)
    if not record:
        raise HTTPException(status_code=404, detail="Search not found")
    return record


@app.delete("/searches/{search_id}", status_code=204)
async def delete_search(search_id: int):
    deleted = await database.delete_search(search_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Search not found")


@app.get("/health")
async def health():
    return {"status": "ok"}
