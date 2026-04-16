"""
Landed cost calculator for auto parts shipped from US to Dominican Republic.

Formula: (listing_price + US_shipping) * exchange_rate + (weight * ClickPack_rate)

ClickPack rate: RD$246/lb (to Miami warehouse → DR delivery)
Exchange rate: RD$63 = $1 USD (configurable via .env)
"""

import os
from .weight_table import estimate_weight


def get_exchange_rate() -> float:
    return float(os.getenv("EXCHANGE_RATE_DOP_USD", "63"))


def get_clickpack_rate() -> float:
    return float(os.getenv("CLICKPACK_RATE_DOP_PER_LB", "246"))


def calculate_landed_cost(
    listing_price_usd: float,
    us_shipping_usd: float,
    part_name_english: str,
) -> dict:
    """Calculate total landed cost in DOP for a part shipped from US to DR.

    Returns dict with all cost components for Excel display.
    """
    exchange_rate = get_exchange_rate()
    clickpack_rate = get_clickpack_rate()

    weight_lbs = estimate_weight(part_name_english)
    item_cost_usd = float(listing_price_usd) + float(us_shipping_usd)
    courier_cost_dop = weight_lbs * clickpack_rate
    total_dop = (item_cost_usd * exchange_rate) + courier_cost_dop

    return {
        "listing_price_usd": round(float(listing_price_usd), 2),
        "us_shipping_usd": round(float(us_shipping_usd), 2),
        "item_cost_usd": round(item_cost_usd, 2),
        "weight_lbs": weight_lbs,
        "courier_cost_dop": round(courier_cost_dop, 2),
        "courier_cost_usd": round(courier_cost_dop / exchange_rate, 2),
        "total_landed_dop": round(total_dop, 2),
        "total_landed_usd": round(total_dop / exchange_rate, 2),
        "exchange_rate": exchange_rate,
        "clickpack_rate": clickpack_rate,
    }
