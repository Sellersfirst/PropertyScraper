import math
import re


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def lot_size_to_sqft(raw: str | None) -> float | None:
    if not raw or raw.strip() in ("—", "-", ""):
        return None
    text = raw.lower().replace(",", "")
    m = re.search(r"([\d.]+)\s*acres?", text)
    if m:
        return round(float(m.group(1)) * 43_560, 1)
    m = re.search(r"([\d.]+)\s*sq", text)
    if m:
        return float(m.group(1))
    m = re.search(r"([\d.]+)", text)
    if m:
        return float(m.group(1))
    return None
