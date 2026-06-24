from typing import Literal, Optional
from pydantic import BaseModel, model_validator

SoldWithin = Literal[
    "sold-1wk", "sold-1mo", "sold-3mo", "sold-6mo",
    "sold-1yr", "sold-2yr", "sold-3yr", "sold-5yr",
]


class ComparableSalesRequest(BaseModel):
    redfin_url: Optional[str] = None
    address: Optional[str] = None

    # Geographic
    radius_miles: float = 2.0

    # Sold-listing time window — maps directly to Redfin's URL filter
    sold_within: SoldWithin = "sold-3yr"

    # Home living area (sq ft)
    min_sqft: Optional[int] = None
    max_sqft: Optional[int] = None

    # Lot size (sq ft)
    min_lot_sqft: Optional[float] = None
    max_lot_sqft: Optional[float] = None

    # Sale price ($)
    min_price: Optional[int] = None
    max_price: Optional[int] = None

    # Post-filter: exclude comps whose most recent sale is older than N years
    lookback_years: Optional[float] = None

    # Maximum months between a buy event and the following sell event (flip detection)
    max_sale_gap_months: Optional[float] = None

    # Bedrooms
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None

    # Bathrooms (supports fractions, e.g. 2.5)
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None

    max_comparables: int = 10

    @model_validator(mode="after")
    def require_one_of(self):
        if not self.redfin_url and not self.address:
            raise ValueError("Provide either 'redfin_url' or 'address'")
        return self


class SaleEvent(BaseModel):
    date: str
    event: str
    price: Optional[str] = None
    price_per_sqft: Optional[str] = None
    source: Optional[str] = None


class PropertySummary(BaseModel):
    redfin_url: Optional[str] = None
    address: Optional[str] = None
    sq_ft: Optional[int] = None
    lot_size_raw: Optional[str] = None
    lot_size_sqft: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class ComparableProperty(BaseModel):
    redfin_url: Optional[str] = None
    address: Optional[str] = None
    sq_ft: Optional[int] = None
    lot_size_sqft: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    pool: Optional[bool] = None
    garage: Optional[str] = None
    list_price: Optional[int] = None
    distance_miles: Optional[float] = None
    sale_date: Optional[str] = None
    sale_price: Optional[int] = None
    buy_date: Optional[str] = None
    buy_price: Optional[int] = None
    hold_days: Optional[int] = None
    spread: Optional[int] = None
    sale_history: list[SaleEvent] = []


class ComparableSalesResponse(BaseModel):
    target: PropertySummary
    comparables: list[ComparableProperty]
    total_candidates_found: int
    scraped_at: str
