import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

TARGET_URL = (
    "https://www.redfin.com/IL/Chicago/233-E-Erie-St-60611/unit-1301/home/14097065"
)

# Redfin renders property history after a delayed XHR; tell ScrapingBee to
# wait until this selector appears before returning the HTML.
WAIT_FOR_SELECTOR = (
    "#property-history-transition-node"
    ", [data-rf-test-id='property-history']"
    ", .PropertyHistoryEventRow"
)


def build_scrapingbee_url(api_key: str) -> str:
    params = {
        "api_key": api_key,
        "url": TARGET_URL,
        "render_js": "true",
        "stealth_proxy": "true",
        "country_code": "us",
        "wait_for": WAIT_FOR_SELECTOR,
        "wait": "3000",  # extra 3 s after selector appears for tail XHRs
    }
    return f"https://app.scrapingbee.com/api/v1/?{urlencode(params)}"


def parse_sale_history(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    current_source: str | None = None

    table = soup.select_one(".PropertyHistoryEventTable")
    if not table:
        print("Warning: .PropertyHistoryEventTable not found in HTML")
        return rows

    for el in table.select(".BasicTable__row"):
        classes: list[str] = el.get("class", [])

        if "BasicTable__headerRow" in classes:
            continue

        # Source row (MLS attribution) — capture and move on
        if "mlsAttr" in classes:
            subtext = el.select_one(".subtext")
            current_source = subtext.get_text(strip=True) if subtext else None
            continue

        if "photoSection" in classes or "remarksSection" in classes:
            continue

        date_el = el.select_one(".BasicTable__col.date")
        date = date_el.get_text(strip=True) if date_el else ""

        event_el = el.select_one(".BasicTable__col.event")
        event = event_el.get_text(strip=True) if event_el else ""

        if not date and not event:
            continue

        price_el = el.select_one(".BasicTable__col.price")
        price: str | None = None
        price_per_sqft: str | None = None
        if price_el:
            subtext = price_el.select_one(".subtext")
            price_per_sqft = subtext.get_text(strip=True) if subtext else None
            if subtext:
                subtext.decompose()
            raw_price = price_el.get_text(strip=True)
            price = raw_price if raw_price not in ("—", "*", "") else None

        rows.append(
            {
                "date": date,
                "event": event,
                "price": price,
                "pricePerSqft": price_per_sqft,
                "source": current_source,
            }
        )

    print(f"Parsed {len(rows)} sale history row(s)")
    return rows


def run() -> None:
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        print(
            "Missing SCRAPINGBEE_API_KEY. Add it to .env and run via `python scraper.py`.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Calling ScrapingBee (stealth proxy, JS render)... ~75 credits")
    start = time.monotonic()

    resp = requests.get(build_scrapingbee_url(api_key), timeout=120)
    elapsed = time.monotonic() - start

    print(f"Response : {resp.status_code} in {elapsed:.1f}s")
    print(f"Cost     : {resp.headers.get('Spb-cost', 'n/a')} credits")
    print(f"Remaining: {resp.headers.get('Spb-remaining-api-calls', 'n/a')}")
    print(f"Resolved : {resp.headers.get('Spb-resolved-url', 'n/a')}")

    html = resp.text

    if not resp.ok:
        Path("/tmp/scrapingbee_error.html").write_text(html, encoding="utf-8")
        print(
            "ScrapingBee returned non-2xx. Body saved to /tmp/scrapingbee_error.html",
            file=sys.stderr,
        )
        print(html[:500], file=sys.stderr)
        sys.exit(2)

    Path("/tmp/redfin_rendered.html").write_text(html, encoding="utf-8")
    print("Raw HTML saved: /tmp/redfin_rendered.html")

    sale_history = parse_sale_history(html)

    result = {
        "url": TARGET_URL,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "saleHistory": sale_history,
    }

    Path("sale_history.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\n========== RESULT ==========")
    print(json.dumps(result, indent=2))
    print("\nSaved to sale_history.json")

    if not sale_history:
        print(
            "\nNo rows parsed. Open /tmp/redfin_rendered.html and grep for 'Sold' or 'Listed' — "
            "Redfin probably changed selectors. Share what wrapper class the rows use "
            "and I will update parse_sale_history()."
        )


if __name__ == "__main__":
    run()
