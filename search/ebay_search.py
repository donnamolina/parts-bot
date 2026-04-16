"""
eBay Browse API search — async version with OAuth token caching,
rate limiting, pickup-only filtering, and US seller preference.
"""

import os
import json
import base64
import time
import re
import logging
import aiohttp
from pathlib import Path
from datetime import datetime, timezone

CACHE_DIR = Path(__file__).parent.parent / "cache"
TOKEN_CACHE = CACHE_DIR / "ebay_token.json"
RATE_LIMIT_FILE = CACHE_DIR / "rate_limit.json"
LOG_DIR = Path(__file__).parent.parent / "logs"

logger = logging.getLogger("parts-bot.ebay")


def _get_daily_limit() -> int:
    return int(os.getenv("EBAY_DAILY_LIMIT", "5000"))


def _check_rate_limit() -> tuple:
    """Returns (allowed: bool, count: int)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {}
    if RATE_LIMIT_FILE.exists():
        try:
            data = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    limit = _get_daily_limit()
    return data["count"] < limit, data["count"]


def _increment_rate_limit():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {}
    if RATE_LIMIT_FILE.exists():
        try:
            data = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] += 1
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RATE_LIMIT_FILE.write_text(json.dumps(data))


async def _get_oauth_token() -> str:
    """Get eBay OAuth token, using file cache if still valid."""
    # Check cache
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("access_token") and time.time() < (cached.get("expires_at", 0) - 60):
                return cached["access_token"]
        except (json.JSONDecodeError, OSError):
            pass

    app_id = os.getenv("EBAY_APP_ID", "")
    app_secret = os.getenv("EBAY_APP_SECRET", "")
    if not app_id or not app_secret:
        raise ValueError("Missing EBAY_APP_ID or EBAY_APP_SECRET in environment")

    credentials = f"{app_id}:{app_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded}",
    }
    data = "grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope"

    async with aiohttp.ClientSession() as session:
        for attempt in range(2):
            try:
                async with session.post(url, headers=headers, data=data,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        token = result["access_token"]
                        expires_in = result.get("expires_in", 7200)

                        # Cache to file
                        CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        TOKEN_CACHE.write_text(json.dumps({
                            "access_token": token,
                            "expires_at": time.time() + expires_in,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }))
                        return token
                    elif resp.status >= 500 and attempt == 0:
                        continue
                    else:
                        text = await resp.text()
                        raise ValueError(f"Token error {resp.status}: {text[:200]}")
            except aiohttp.ClientError:
                if attempt == 0:
                    continue
                raise

    raise ValueError("Token request failed after retries")


def _is_pickup_only(item: dict) -> bool:
    """Check if listing is pickup-only (no shipping)."""
    shipping_options = item.get("shippingOptions", [])
    if not shipping_options:
        return True  # No shipping info = assume pickup
    for opt in shipping_options:
        code = opt.get("shippingServiceCode", "").lower()
        stype = opt.get("type", "").lower()
        if "pickup" in code or "local" in code or "pickup" in stype:
            return True
    return False


def _extract_shipping_cost(item: dict) -> float:
    """Extract shipping cost from eBay item. Returns 0.0 for free shipping."""
    shipping_options = item.get("shippingOptions", [])
    if shipping_options:
        cost_info = shipping_options[0].get("shippingCost", {})
        if cost_info:
            return float(cost_info.get("value", "0.00"))
    return 0.0


def _matches_side(title: str, side: str | None) -> bool:
    """Check if listing title is compatible with requested side."""
    if not side:
        return True
    lower = title.lower()

    if side == "left":
        wrong = ["right only", "rh only", "passenger only", "right passenger"]
        return not any(w in lower for w in wrong)
    elif side == "right":
        wrong = ["left only", "lh only", "driver only", "left driver"]
        return not any(w in lower for w in wrong)
    return True



# ─── OEM Part Number Extraction from eBay Titles ─────────────────────────────
# When RockAuto is unavailable, eBay titles often contain the OEM part number.
# Patterns cover Toyota (81550-04170), Honda (33500-TLA-A01), Kia/Hyundai
# (92401-D3000), GM (84234973), Ford (BL3Z-13405-C), and generic alphanumeric.

_OEM_PATTERNS = [
    # Toyota/Lexus: 5 digits, dash, 2 uppercase+3 digits, optional suffix
    re.compile(r'\b(\d{5}-[A-Z0-9]{2}\d{3}\w*)\b'),
    # Toyota: 5 digits, dash, 5 digits
    re.compile(r'\b(\d{5}-\d{5})\b'),
    # Kia/Hyundai: 5 digits, dash, 5 alphanum
    re.compile(r'\b(\d{5}-[A-Z0-9]{5})\b'),
    # Honda: 5 digits, dash, 3 uppercase, dash, 3 alphanum
    re.compile(r'\b(\d{5}-[A-Z]{3}-[A-Z0-9]{3})\b'),
    # Ford: 1-2 letters, digit, 1-2 letters, dash, 5 digits, dash, 1 letter
    re.compile(r'\b([A-Z]{1,2}\d[A-Z]{1,2}-\d{5}-[A-Z])\b'),
    # GM/Mopar: 7-10 digit numeric
    re.compile(r'\b(\d{7,10})\b'),
    # Generic OEM-style: letters+digits combo 7-15 chars with mandatory digit
    re.compile(r'\b([A-Z0-9]{3,5}-[A-Z0-9]{4,8})\b'),
]

# Words that indicate the matched string is NOT a part number
_PN_FALSE_POSITIVES = {
    'shipping', 'warranty', 'left', 'right', 'front', 'rear',
    'driver', 'passenger', 'door', 'hood', 'fender',
}


def _extract_pn_from_title(title: str) -> str:
    """Try to extract an OEM-style part number from an eBay listing title.
    Returns the first plausible match, or empty string if none found.
    """
    upper = title.upper()
    # Strip common noise
    for noise in ['OEM', 'NEW', 'GENUINE', 'REPLACEMENT', 'FITS', 'FOR']:
        upper = upper.replace(noise, ' ')

    for pattern in _OEM_PATTERNS:
        m = pattern.search(upper)
        if m:
            candidate = m.group(1)
            # Skip pure-numeric unless it looks like a GM/Mopar number (7+ digits)
            if candidate.isdigit() and len(candidate) < 7:
                continue
            # Skip very short
            if len(candidate) < 6:
                continue
            return candidate
    return ''


# eBay category IDs for automotive parts (Body & Frame → most body panels live here)
EBAY_CATEGORY_BODY_PARTS = "33609"   # Body & Frame Parts
EBAY_CATEGORY_AUTO_PARTS = "6030"    # Car & Truck Parts & Accessories (broad)

# Body parts that should be restricted to the body parts eBay category
# to avoid cheap accessories (clips, liners, seals) crowding out real panels.
# Maps part keyword → minimum realistic USD price for that part.
# Items below the floor are filtered out — they're clips/bolts/decals, not the part.
BODY_PANEL_MIN_PRICES: dict[str, float] = {
    "hood": 80.0,
    "fender": 50.0,
    "bumper cover": 90.0,       # raised: real covers cost $90-300, $65 = accessory
    "front bumper cover": 90.0,
    "rear bumper cover": 70.0,
    "door": 100.0,
    "trunk lid": 80.0,
    "tailgate": 100.0,
    "roof panel": 120.0,
    "rocker panel": 30.0,
    "grille": 25.0,
    "headlight": 30.0,
    "headlamp assembly": 30.0,
    "tail light": 20.0,
    "tail lamp assembly": 20.0,
    "fog light": 15.0,
    "side mirror": 20.0,
    "outside mirror": 20.0,
    "windshield": 80.0,
    "rear windshield": 60.0,
    "spoiler": 40.0,
    "radiator support": 60.0,
}
# Title keywords that indicate a listing is NOT the main body part — it's a small
# accessory, emblem, clip, or hardware piece that slips through the category filter.
BODY_PART_TITLE_EXCLUDES = {
    "emblem", "badge", "decal", "sticker", "logo", "nameplate",
    "clip", "clips", "bolt", "bolts", "nut ", "nuts", "screw", "screws",
    "fastener", "retainer clip", "push pin",
    "insert only", "screen only", "mesh only",
    "overlay", "cover overlay", "vinyl",
    "tow hook", "tow eye",
    # Bumper accessories that are NOT a full bumper cover
    "guard", "step pad", "protector", "applique", "end cap",
    "air dam", "skid plate", "deflector", "trim piece", "filler panel",
    "tow cover", "license bracket", "license plate bracket",
    "chrome", "chrome trim", "chrome cover",
}

BODY_PANEL_KEYWORDS = set(BODY_PANEL_MIN_PRICES.keys())


def _body_category_id(query: str, part_english: str = "") -> str | None:
    """Return eBay category ID if the query is for a body panel / lighting part."""
    combined = (query + " " + part_english).lower()
    for kw in BODY_PANEL_KEYWORDS:
        if kw in combined:
            return EBAY_CATEGORY_BODY_PARTS
    return None


def _min_price_for_part(part_english: str, query: str) -> float:
    """Return minimum plausible USD price for this part type. Returns 0 if unknown."""
    combined = (part_english + " " + query).lower()
    # Check longer keywords first (front bumper cover before bumper cover)
    for kw in sorted(BODY_PANEL_MIN_PRICES, key=len, reverse=True):
        if kw in combined:
            return BODY_PANEL_MIN_PRICES[kw]
    return 0.0


async def search_ebay(
    query: str,
    side: str | None = None,
    limit: int = 20,
    _token: str | None = None,
    part_english: str = "",
) -> list:
    """Search eBay Browse API for auto parts.

    Returns list of dicts with: title, price, shipping, currency, condition,
    url, item_id, location, source, part_number (if extractable).

    Filters out pickup-only listings.
    Prefers US sellers, allows international if >30% cheaper.
    For body panel queries, restricts to eBay's Body & Frame category (33609)
    to prevent cheap accessories from appearing instead of actual panels.
    """
    # Rate limit check
    allowed, count = _check_rate_limit()
    if not allowed:
        logger.warning(f"eBay daily limit reached: {count}")
        return []

    try:
        token = _token or await _get_oauth_token()
    except Exception as e:
        logger.error(f"eBay token error: {e}")
        return []

    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": query,
        "limit": min(limit, 50),
        "filter": "buyingOptions:{FIXED_PRICE|AUCTION},deliveryCountry:US,conditions:{NEW|USED|REFURBISHED}",
    }

    # Restrict to body parts category for panel/lighting searches to avoid
    # cheap clips, liners, and accessories appearing as results
    cat_id = _body_category_id(query, part_english)
    if cat_id:
        params["category_ids"] = cat_id

    try:
        _increment_rate_limit()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"eBay API {resp.status}: {text[:200]}")
                    return []

                data = await resp.json()
                items = data.get("itemSummaries", [])

    except Exception as e:
        logger.error(f"eBay search error: {e}")
        return []

    # Process results
    us_results = []
    intl_results = []
    min_price = _min_price_for_part(part_english, query)

    for item in items:
        # Skip pickup-only
        if _is_pickup_only(item):
            continue

        title = item.get("title", "")

        # Side filter
        if not _matches_side(title, side):
            continue

        price_info = item.get("price", {})
        price = float(price_info.get("value", "0.00"))
        if price <= 0:
            continue

        # Skip items below the minimum plausible price for this part type.
        # Protects against clips/bolts/decals ranking first in body part searches.
        if min_price > 0 and price < min_price:
            logger.debug(f"eBay: skipping '{title[:50]}' at ${price:.2f} (min ${min_price:.2f})")
            continue

        # Skip listings whose title contains known non-part keywords (emblems,
        # badges, clips, etc.) — these slip through category/price filters but
        # are clearly not the main body panel or lighting assembly.
        if min_price > 0:
            title_lower = title.lower()
            if any(excl in title_lower for excl in BODY_PART_TITLE_EXCLUDES):
                logger.debug(f"eBay: skipping accessory listing '{title[:60]}'")
                continue

        condition = item.get("condition", "Unknown")
        if isinstance(condition, dict):
            condition = condition.get("conditionDisplayName", "Unknown")

        location = item.get("itemLocation", {}).get("country", "")
        shipping = _extract_shipping_cost(item)

        pn_from_title = _extract_pn_from_title(title)
        result = {
            "title": title,
            "price": price,
            "shipping": shipping,
            "total_price": round(price + shipping, 2),
            "currency": price_info.get("currency", "USD"),
            "condition": condition,
            "url": item.get("itemWebUrl", ""),
            "item_id": item.get("itemId", ""),
            "location": location,
            "source": "eBay",
            "seller": item.get("seller", {}).get("username", ""),
            "part_number": pn_from_title,
        }

        if location == "US":
            us_results.append(result)
        else:
            intl_results.append(result)

    # Prefer US sellers, allow international if >30% cheaper
    all_results = []
    if us_results:
        us_results.sort(key=lambda x: x["total_price"])
        all_results = us_results

        if intl_results:
            intl_results.sort(key=lambda x: x["total_price"])
            best_us = us_results[0]["total_price"]
            best_intl = intl_results[0]["total_price"]
            if best_intl < best_us * 0.7:
                # International is >30% cheaper — include at top
                all_results = intl_results[:2] + us_results
    else:
        intl_results.sort(key=lambda x: x["total_price"])
        all_results = intl_results

    return all_results[:limit]


async def get_ebay_token() -> str:
    """Public helper to get a reusable eBay OAuth token."""
    return await _get_oauth_token()


async def check_compatibility(
    item_id: str,
    year: int,
    make: str,
    model: str,
    token: str,
) -> str:
    """Check eBay item vehicle compatibility. Returns COMPATIBLE, NOT_COMPATIBLE, or UNDETERMINED."""
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}/check_compatibility"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    payload = {
        "compatibilityProperties": [
            {"name": "Year", "value": str(year)},
            {"name": "Make", "value": make},
            {"name": "Model", "value": model},
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            compat = data.get("compatibilityStatus", "").upper()
            if compat == "COMPATIBLE":
                return "COMPATIBLE"
            elif compat == "NOT_COMPATIBLE":
                return "NOT_COMPATIBLE"
        return "UNDETERMINED"
    except Exception:
        return "UNDETERMINED"
