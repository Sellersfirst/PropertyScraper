import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from models import PropertySummary, SaleEvent
from services.geo import lot_size_to_sqft

load_dotenv()

log = logging.getLogger(__name__)

SCRAPINGBEE_KEY = os.environ["SCRAPINGBEE_API_KEY"]
REDFIN_BASE = "https://www.redfin.com"
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"

_SB_SEMAPHORE = asyncio.Semaphore(5)

def _strip_xssi(text: str) -> str:
    # Redfin prefixes JSON responses with `{}&&` as XSSI prevention.
    # Find `&&` and take everything after it; fall through if absent.
    idx = text.find("&&")
    return text[idx + 2:] if idx >= 0 else text


async def _scrapingbee(
    url: str,
    *,
    render_js: bool = True,
    stealth_proxy: bool = True,
    wait_for: str | None = None,
    extra_wait_ms: int = 0,
) -> str:
    params: dict[str, str] = {
        "api_key": SCRAPINGBEE_KEY,
        "url": url,
        "render_js": "true" if render_js else "false",
        "stealth_proxy": "true" if stealth_proxy else "false",
        "country_code": "us",
    }
    if wait_for:
        params["wait_for"] = wait_for
    if extra_wait_ms:
        params["wait"] = str(extra_wait_ms)

    log.info("ScrapingBee → %s (render_js=%s)", url, render_js)
    t0 = time.monotonic()
    async with _SB_SEMAPHORE:
        async with httpx.AsyncClient(timeout=120) as client:
            for attempt in range(1, 3):
                resp = await client.get(SCRAPINGBEE_URL, params=params)
                if resp.status_code < 500:
                    break
                log.warning("ScrapingBee returned %s on attempt %d — retrying", resp.status_code, attempt)
            resp.raise_for_status()
    elapsed = time.monotonic() - t0
    credits = resp.headers.get("Spb-cost", "?")
    log.info("ScrapingBee ← %s in %.1fs | credits used: %s", resp.status_code, elapsed, credits)
    return resp.text


async def _scrapingbee_json(url: str) -> dict:
    """Fetch a Redfin JSON API endpoint via ScrapingBee (no JS rendering needed).

    Using render_js=False: bare JSON endpoints don't need a headless browser,
    it's cheaper (1 credit vs 75) and avoids 500s from the browser crashing on raw JSON.
    """
    text = await _scrapingbee(url, render_js=False, stealth_proxy=False)
    return json.loads(_strip_xssi(text.strip()))


# ─── Geocoding ────────────────────────────────────────────────────────────────

async def _nominatim_lookup(address: str) -> list:
    """Raw Nominatim search. Returns parsed JSON list."""
    from urllib.parse import quote as url_quote
    url = (
        f"https://nominatim.openstreetmap.org/search"
        f"?q={url_quote(address)}&format=json&limit=1&addressdetails=1"
    )
    async with httpx.AsyncClient(
        timeout=10,
        headers={"User-Agent": "PropertyScraper/1.0"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.json()


async def geocode_nominatim(address: str) -> tuple[float, float, str, str | None]:
    """Geocode a US address via Nominatim. Returns (lat, lng, display_name, postcode).

    If the full address fails (common with unit/apt suffixes), retries with just
    the street + city + state portion.
    """
    log.info("Nominatim geocoding: %r", address)
    results = await _nominatim_lookup(address)

    # Retry without unit/apt qualifier when the first attempt returns nothing
    if not results:
        stripped = re.sub(
            r"(?i)\s*(unit|apt|suite|#|bldg|ste|floor|fl)\s*[\w&/-]+", "", address
        ).strip().strip(",").strip()
        if stripped != address:
            log.info("Nominatim retry (stripped unit): %r", stripped)
            results = await _nominatim_lookup(stripped)

    if not results:
        raise ValueError(f"Nominatim could not geocode: {address!r}")

    r = results[0]
    lat, lng = float(r["lat"]), float(r["lon"])
    display = r.get("display_name", address)
    postcode = r.get("address", {}).get("postcode")
    log.info("Nominatim → (%.6f, %.6f) zip=%s | %s", lat, lng, postcode, display)
    return lat, lng, display, postcode


# ─── Redfin URL resolution (used only when a redfin_url is NOT provided) ─────

async def resolve_property_url(address: str) -> str:
    """Try Redfin autocomplete to get a canonical property URL from an address.

    Used for resolving comparable property URLs so we can scrape their history.
    Tries direct HTTP first (0 credits), falls back to ScrapingBee plain proxy.
    """
    log.info("Redfin autocomplete: %r", address)
    url = f"{REDFIN_BASE}/stingray/do/location-autocomplete?location={quote(address)}&v=2"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.redfin.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = json.loads(_strip_xssi(resp.text.strip()))
    except Exception as e:
        log.warning("Direct autocomplete failed (%s) — trying ScrapingBee", e)
        data = await _scrapingbee_json(url)

    for section in data.get("payload", {}).get("sections", []):
        for row in section.get("rows", []):
            if row.get("type") == "1":
                resolved = REDFIN_BASE + row["url"]
                log.info("Redfin URL: %s", resolved)
                return resolved
    raise ValueError(f"No Redfin listing found for: {address!r}")


# ─── Sold property search (ZIP-code based) ────────────────────────────────────

def _parse_redfin_search_html(html: str, max_results: int = 100) -> list[dict]:
    """Extract sold property records from a Redfin recently-sold search page.

    Each property card embeds a <script type="application/ld+json"> block containing
    GeoCoordinates (lat/lng), floorSize, numberOfRooms, price, and URL — no Nominatim needed.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[dict] = []

    cards = (
        soup.select(".HomeCardContainer") or
        soup.select("[data-rf-test-id='mapHomeCard']") or
        soup.select(".V8ListingCard")
    )

    if not cards:
        log.warning("No property cards found — falling back to href scan")
        for a in soup.find_all("a", href=re.compile(r"^/[A-Z]{2}/.+/home/\d+$")):
            url = REDFIN_BASE + a["href"]
            if url not in seen:
                seen.add(url)
                results.append({"full_url": url, "address": None, "sq_ft": None,
                                 "lot_size_sqft": None, "bedrooms": None, "bathrooms": None,
                                 "price": None, "lat": None, "lng": None})
                if len(results) >= max_results:
                    break
        return results

    for card in cards[:max_results]:
        # ── Primary: JSON-LD within the card (lat/lng + full data) ──────────
        ld_tag = card.find("script", {"type": "application/ld+json"})
        if ld_tag and ld_tag.string:
            try:
                ld = json.loads(ld_tag.string)
                if not isinstance(ld, list):
                    ld = [ld]

                geo_item = next((x for x in ld if x.get("geo")), None)
                price_item = next((x for x in ld if x.get("@type") == "Product"), None)

                if not geo_item:
                    raise ValueError("no geo")

                url = geo_item.get("url", "")
                if not url.startswith("http"):
                    url = REDFIN_BASE + url
                if url in seen:
                    continue
                seen.add(url)

                geo = geo_item.get("geo", {})
                lat = geo.get("latitude")
                lng = geo.get("longitude")

                addr = geo_item.get("address", {})
                address = (
                    f"{addr.get('streetAddress', '')}, "
                    f"{addr.get('addressLocality', '')}, "
                    f"{addr.get('addressRegion', '')} "
                    f"{addr.get('postalCode', '')}"
                ).strip(", ")

                floor = geo_item.get("floorSize", {})
                sq_ft = int(floor["value"]) if floor.get("value") else None
                beds = geo_item.get("numberOfRooms")

                price = None
                if price_item:
                    try:
                        price = int(float(price_item.get("offers", {}).get("price", 0))) or None
                    except (ValueError, TypeError):
                        pass

                # Baths: in stats bar (.bp-Homecard__Stats--baths)
                baths = None
                baths_el = card.select_one(".bp-Homecard__Stats--baths")
                if baths_el:
                    bm = re.search(r"([\d.]+)", baths_el.get_text())
                    if bm:
                        baths = float(bm.group(1))

                # KeyFacts-item: lot size, pool, garage
                lot_sqft = None
                pool = None
                garage = None
                for kf in card.select(".KeyFacts-item"):
                    text = kf.get_text(strip=True)
                    if lot_sqft is None:
                        km = re.search(r"([\d,]+)\s*sq\s*ft\s*lot", text, re.I)
                        if km:
                            lot_sqft = float(km.group(1).replace(",", ""))
                    if pool is None and re.search(r"\bpool\b", text, re.I):
                        pool = True
                    if garage is None:
                        gm = re.search(r"(\d+-car\s+\w+)", text, re.I)
                        if gm:
                            garage = gm.group(1)

                results.append({
                    "full_url": url,
                    "address": address or None,
                    "sq_ft": sq_ft,
                    "lot_size_sqft": lot_sqft,
                    "bedrooms": beds,
                    "bathrooms": baths,
                    "pool": pool,
                    "garage": garage,
                    "price": price,
                    "lat": float(lat) if lat is not None else None,
                    "lng": float(lng) if lng is not None else None,
                })
                continue  # successfully parsed via JSON-LD, move to next card

            except Exception as e:
                log.debug("JSON-LD parse failed for card: %s", e)

        # ── Fallback: raw HTML scraping (no lat/lng) ─────────────────────────
        a = card.find("a", href=re.compile(r"/[A-Z]{2}/.+/home/\d+"))
        if not a:
            continue
        url = REDFIN_BASE + a["href"]
        if url in seen:
            continue
        seen.add(url)

        card_text = card.get_text(" ", strip=True)
        price = None
        pm = re.search(r"\$([\d,]+)", card_text)
        if pm:
            price = int(pm.group(1).replace(",", ""))

        sq_ft = None
        sqm = re.search(r"([\d,]+)\s*sq\.?\s*ft", card_text, re.I)
        if sqm:
            sq_ft = int(sqm.group(1).replace(",", ""))

        beds = baths = None
        bm = re.search(r"(\d+)\s*beds?\b", card_text, re.I)
        if bm:
            beds = int(bm.group(1))
        bam = re.search(r"([\d.]+)\s*baths?\b", card_text, re.I)
        if bam:
            baths = float(bam.group(1))

        results.append({"full_url": url, "address": None, "sq_ft": sq_ft,
                         "lot_size_sqft": None, "bedrooms": beds, "bathrooms": baths,
                         "price": price, "lat": None, "lng": None})

    json_ld_count = sum(1 for r in results if r.get("lat") is not None)
    log.info(
        "Parsed %d cards: %d with lat/lng (JSON-LD), %d need geocoding",
        len(results), json_ld_count, len(results) - json_ld_count,
    )
    return results


async def _fetch_redfin_page(url: str) -> str:
    """Fetch one Redfin search page via ScrapingBee, plain HTTP first then JS fallback."""
    html = await _scrapingbee(url, render_js=False, stealth_proxy=False)
    if '<script type="application/ld+json">' not in html:
        log.warning("No JSON-LD in plain response for %s — retrying with render_js=True", url)
        html = await _scrapingbee(
            url,
            render_js=True,
            stealth_proxy=True,
            wait_for=".HomeCardContainer, [data-rf-test-id='mapHomeCard'], .V8ListingCard",
            extra_wait_ms=3000,
        )
    return html


def _parse_max_page(html: str) -> int:
    """Extract the highest page number from the Redfin pagination widget."""
    soup = BeautifulSoup(html, "lxml")
    page_nums = [
        int(a.get_text(strip=True))
        for a in soup.select("a.PageNumbers__page")
        if a.get_text(strip=True).isdigit()
    ]
    return max(page_nums) if page_nums else 1


async def iter_sold_pages(
    zipcode: str,
    sold_within: str = "sold-3yr",
):
    """Async generator that yields one page of Redfin sold listings at a time.

    Yields (page_number, results, total_available_pages) so the caller can stop
    pagination as soon as it has collected enough qualified results.
    """
    base = f"{REDFIN_BASE}/zipcode/{zipcode}/filter/include={sold_within}"
    seen_urls: set[str] = set()
    max_pages = 10

    html = await _fetch_redfin_page(base)
    available_pages = min(_parse_max_page(html), max_pages)
    log.info("Redfin sold search — %d page(s) available for %s %s", available_pages, zipcode, sold_within)

    page_results = _parse_redfin_search_html(html)
    new = [r for r in page_results if r["full_url"] not in seen_urls]
    for r in new:
        seen_urls.add(r["full_url"])
    log.info("Page 1: %d new properties", len(new))
    yield 1, new, available_pages

    for page in range(2, available_pages + 1):
        url = f"{base}/page-{page}"
        log.info("Redfin sold search page %d → %s", page, url)
        html = await _fetch_redfin_page(url)
        page_results = _parse_redfin_search_html(html)
        new = [r for r in page_results if r["full_url"] not in seen_urls]
        for r in new:
            seen_urls.add(r["full_url"])
        log.info("Page %d: %d new properties", page, len(new))
        if not new:
            break
        yield page, new, available_pages


# ─── ZIP extraction helper ────────────────────────────────────────────────────

def extract_zip(text: str) -> str | None:
    """Pull the first 5-digit US ZIP code from a string."""
    m = re.search(r'\b(\d{5})(?:-\d{4})?\b', text)
    return m.group(1) if m else None


# ─── Property details ──────────────────────────────────────────────────────────

def _parse_sq_ft(soup: BeautifulSoup) -> int | None:
    # Hero stat bar: <div>850 <div class="statsLabel">sq ft</div></div>
    for label in soup.select(".statsLabel"):
        if "sq ft" in label.get_text(strip=True).lower():
            parent = label.find_parent()
            if parent:
                m = re.search(r"([\d,]+)\s*sq", parent.get_text(" ", strip=True), re.I)
                if m:
                    return int(m.group(1).replace(",", ""))
    return None


def _parse_lot_size(soup: BeautifulSoup) -> str | None:
    for row in soup.select(".table-row"):
        label = row.select_one(".table-label")
        if label and "lot size" in label.get_text(strip=True).lower():
            val = row.select_one(".table-value")
            if val:
                raw = val.get_text(strip=True)
                return raw if raw and raw != "—" else None
    return None


def _parse_lat_lng(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(
            r'"latitude"\s*:\s*([\d.\-]+).*?"longitude"\s*:\s*([\d.\-]+)', text, re.S
        )
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def _parse_address(soup: BeautifulSoup) -> str | None:
    title = soup.find("title")
    if title:
        return title.get_text().split("|")[0].strip()
    return None


async def scrape_property_details(redfin_url: str) -> PropertySummary:
    log.info("Scraping property details: %s", redfin_url)
    html = await _scrapingbee(redfin_url, wait_for=".statsLabel", extra_wait_ms=2000)
    soup = BeautifulSoup(html, "lxml")

    sq_ft = _parse_sq_ft(soup)
    lot_size_raw = _parse_lot_size(soup)
    lat, lng = _parse_lat_lng(soup)
    address = _parse_address(soup)

    log.info(
        "Property details parsed: sq_ft=%s, lot_size=%r, lat=%.6f, lng=%.6f",
        sq_ft, lot_size_raw, lat or 0, lng or 0,
    )
    return PropertySummary(
        redfin_url=redfin_url,
        address=address,
        sq_ft=sq_ft,
        lot_size_raw=lot_size_raw,
        lot_size_sqft=lot_size_to_sqft(lot_size_raw),
        lat=lat,
        lng=lng,
    )


# ─── Sale history ──────────────────────────────────────────────────────────────

def _parse_sale_history(html: str) -> list[SaleEvent]:
    soup = BeautifulSoup(html, "lxml")
    events: list[SaleEvent] = []
    current_source: str | None = None

    table = soup.select_one(".PropertyHistoryEventTable")
    if not table:
        return events

    for row in table.select(".BasicTable__row"):
        classes = row.get("class", [])

        if "BasicTable__headerRow" in classes:
            continue

        if "mlsAttr" in classes:
            sub = row.select_one(".subtext")
            current_source = sub.get_text(strip=True) if sub else None
            continue

        if "photoSection" in classes or "remarksSection" in classes:
            continue

        date_el = row.select_one(".BasicTable__col.date")
        event_el = row.select_one(".BasicTable__col.event")
        date = date_el.get_text(strip=True) if date_el else ""
        event = event_el.get_text(strip=True) if event_el else ""
        if not date and not event:
            continue

        price_el = row.select_one(".BasicTable__col.price")
        price = price_per_sqft = None
        if price_el:
            sub = price_el.select_one(".subtext")
            price_per_sqft = sub.get_text(strip=True) if sub else None
            if sub:
                sub.decompose()
            raw = price_el.get_text(strip=True)
            price = raw if raw not in ("—", "*", "") else None

        events.append(
            SaleEvent(
                date=date,
                event=event,
                price=price,
                price_per_sqft=price_per_sqft,
                source=current_source,
            )
        )

    return events


async def scrape_sale_history(redfin_url: str) -> tuple[list[SaleEvent], dict]:
    log.info("Scraping sale history: %s", redfin_url)
    wait_for = (
        "#property-history-transition-node"
        ", [data-rf-test-id='property-history']"
        ", .PropertyHistoryEventRow"
    )
    html = await _scrapingbee(redfin_url, wait_for=wait_for, extra_wait_ms=3000)
    soup = BeautifulSoup(html, "lxml")
    events = _parse_sale_history(html)
    log.info("Sale history: %d event(s) found", len(events))

    # Pull lot size + sq_ft from the property page while we have the HTML
    lot_size_raw = _parse_lot_size(soup)
    extra = {
        "lot_size_sqft": lot_size_to_sqft(lot_size_raw),
        "sq_ft": _parse_sq_ft(soup),
    }
    return events, extra
