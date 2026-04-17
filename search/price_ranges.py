"""
Vehicle-class-aware price anomaly flagging (Bug 13).

Provides sanity-check price ranges for common auto body parts across vehicle
tiers. A part picked by eBay verification that falls far outside the expected
range for its vehicle class gets flagged with a note; no part is rejected or
capped based solely on this check.
"""

from __future__ import annotations

from typing import Optional, Tuple


# (category, class) -> (low_usd, typical_usd, high_usd)
# low   = below this is "suspiciously cheap" (likely wrong part / counterfeit)
# high  = above this is "suspiciously expensive" (likely OEM dealer / wrong fitment)
PART_PRICE_RANGES = {
    ("bumper", "economy"): (60, 150, 400),
    ("bumper", "mainstream"): (90, 220, 600),
    ("bumper", "luxury"): (180, 450, 1400),
    ("bumper", "exotic"): (400, 1200, 5000),

    ("hood", "economy"): (80, 200, 500),
    ("hood", "mainstream"): (120, 280, 700),
    ("hood", "luxury"): (250, 600, 1800),
    ("hood", "exotic"): (600, 1800, 6000),

    ("headlight", "economy"): (40, 110, 300),
    ("headlight", "mainstream"): (70, 180, 500),
    ("headlight", "luxury"): (180, 500, 1800),
    ("headlight", "exotic"): (500, 1500, 5000),

    ("taillight", "economy"): (30, 80, 220),
    ("taillight", "mainstream"): (50, 130, 380),
    ("taillight", "luxury"): (120, 320, 1200),
    ("taillight", "exotic"): (300, 900, 3000),

    ("fender", "economy"): (50, 130, 350),
    ("fender", "mainstream"): (80, 180, 500),
    ("fender", "luxury"): (160, 400, 1300),
    ("fender", "exotic"): (400, 1100, 4000),

    ("grille", "economy"): (30, 90, 260),
    ("grille", "mainstream"): (50, 140, 450),
    ("grille", "luxury"): (130, 380, 1500),
    ("grille", "exotic"): (350, 1000, 4000),

    ("mirror", "economy"): (30, 90, 260),
    ("mirror", "mainstream"): (50, 150, 450),
    ("mirror", "luxury"): (120, 350, 1200),
    ("mirror", "exotic"): (300, 900, 3500),

    ("door", "economy"): (150, 400, 900),
    ("door", "mainstream"): (220, 550, 1400),
    ("door", "luxury"): (450, 1100, 3500),
    ("door", "exotic"): (1000, 3000, 10000),

    ("wheel", "economy"): (60, 140, 350),
    ("wheel", "mainstream"): (90, 220, 600),
    ("wheel", "luxury"): (200, 500, 1800),
    ("wheel", "exotic"): (500, 1400, 5000),

    ("tire", "economy"): (40, 95, 220),
    ("tire", "mainstream"): (60, 130, 320),
    ("tire", "luxury"): (120, 260, 700),
    ("tire", "exotic"): (250, 600, 2000),

    ("reflector", "economy"): (8, 25, 80),
    ("reflector", "mainstream"): (12, 35, 120),
    ("reflector", "luxury"): (25, 80, 300),
    ("reflector", "exotic"): (60, 200, 700),

    ("sensor", "economy"): (15, 50, 200),
    ("sensor", "mainstream"): (25, 80, 300),
    ("sensor", "luxury"): (60, 200, 700),
    ("sensor", "exotic"): (150, 500, 2000),

    ("module", "economy"): (40, 130, 450),
    ("module", "mainstream"): (70, 200, 700),
    ("module", "luxury"): (160, 500, 2000),
    ("module", "exotic"): (400, 1200, 5000),

    ("trim", "economy"): (15, 45, 150),
    ("trim", "mainstream"): (25, 70, 250),
    ("trim", "luxury"): (60, 180, 700),
    ("trim", "exotic"): (150, 450, 1500),

    ("step", "economy"): (40, 110, 320),
    ("step", "mainstream"): (60, 160, 500),
    ("step", "luxury"): (140, 400, 1400),
    ("step", "exotic"): (350, 1000, 3500),

    ("bracket", "economy"): (15, 45, 140),
    ("bracket", "mainstream"): (25, 65, 220),
    ("bracket", "luxury"): (55, 160, 600),
    ("bracket", "exotic"): (140, 400, 1400),
}


LUXURY_MAKES = {
    "porsche", "audi", "bmw", "mercedes-benz", "mercedes", "lexus",
    "infiniti", "acura", "cadillac", "lincoln", "volvo", "jaguar",
    "land rover", "range rover", "genesis", "alfa romeo", "maserati",
    "tesla",
}

EXOTIC_MAKES = {
    "ferrari", "lamborghini", "bentley", "rolls-royce", "rolls royce",
    "mclaren", "aston martin", "bugatti", "pagani", "koenigsegg",
    "lotus",
}


# Keywords -> canonical category (order matters; first match wins)
_CATEGORY_KEYWORDS = [
    ("bumper", ["bumper", "fascia"]),
    ("hood", ["hood", "bonnet", "bonete"]),
    ("headlight", ["headlight", "head light", "headlamp", "farol"]),
    ("taillight", ["taillight", "tail light", "taillamp", "tail lamp"]),
    ("fender", ["fender", "guardafango", "guardalodo", "quarter panel"]),
    ("grille", ["grille", "grill"]),
    ("mirror", ["mirror", "retrovisor", "espejo"]),
    ("door", ["door shell", "door panel", "door assembly", " door "]),
    ("wheel", ["wheel", "rim", "aro"]),
    ("tire", ["tire", "tyre", "neumatico", "neumático"]),
    ("reflector", ["reflector"]),
    ("sensor", ["sensor", "parking sensor", "park assist"]),
    ("module", ["module", "control unit", "ecu"]),
    ("step", ["running board", "side step", "estribo", "step bar"]),
    ("trim", ["trim", "molding", "moulding", "emblem"]),
    ("bracket", ["bracket", "support", "soporte", "pata"]),
]


def get_vehicle_class(make: str) -> str:
    """Return one of: economy, mainstream, luxury, exotic."""
    if not make:
        return "mainstream"
    m = make.strip().lower()
    if m in EXOTIC_MAKES:
        return "exotic"
    if m in LUXURY_MAKES:
        return "luxury"
    # Economy vs mainstream — not critical; default to mainstream so we err
    # toward wider ranges (fewer false-positive anomaly notes).
    return "mainstream"


def _classify_part(part_name: str) -> Optional[str]:
    if not part_name:
        return None
    n = part_name.lower()
    for cat, kws in _CATEGORY_KEYWORDS:
        for kw in kws:
            if kw in n:
                return cat
    return None


def check_price_anomaly(
    part_name: str,
    make: str,
    price_usd: Optional[float],
    shipping_usd: float = 0.0,
) -> Optional[dict]:
    """Return an anomaly descriptor if price is far outside expected range.

    Returns None when no anomaly (either price looks reasonable, or we don't
    have a range for this part category). Never rejects or caps — callers
    should attach the returned note to the result for human review.

    Descriptor shape:
        {
          "severity": "low" | "high",       # cheap vs expensive anomaly
          "magnitude": "mild" | "extreme",  # mild = outside band, extreme = 2x past it
          "category": str,
          "vehicle_class": str,
          "expected": (low, typical, high),
          "price_usd": float,
          "note": str,                       # short human-readable message
        }
    """
    if price_usd is None:
        return None
    try:
        p = float(price_usd) + float(shipping_usd or 0.0)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None

    cat = _classify_part(part_name)
    if cat is None:
        return None

    vclass = get_vehicle_class(make)
    key = (cat, vclass)
    rng = PART_PRICE_RANGES.get(key)
    if rng is None:
        return None

    low, typical, high = rng
    severity = None
    magnitude = None

    if p < low:
        severity = "low"
        magnitude = "extreme" if p < (low / 2.0) else "mild"
    elif p > high:
        severity = "high"
        magnitude = "extreme" if p > (high * 2.0) else "mild"

    if severity is None:
        return None

    if severity == "low":
        note = (
            f"Precio sospechosamente bajo ({cat}, {vclass}): "
            f"${p:.0f} vs. esperado ${low:.0f}–${high:.0f}. "
            f"Posible pieza incorrecta o falsificada."
        )
    else:
        note = (
            f"Precio sospechosamente alto ({cat}, {vclass}): "
            f"${p:.0f} vs. esperado ${low:.0f}–${high:.0f}. "
            f"Posible OEM de concesionario o ajuste incorrecto."
        )

    return {
        "severity": severity,
        "magnitude": magnitude,
        "category": cat,
        "vehicle_class": vclass,
        "expected": (low, typical, high),
        "price_usd": p,
        "note": note,
    }
