"""
RockAuto catalog search via rockauto_api library.
Migrated from existing code with improvements:
- Removed unnecessary sleep delays
- Strict subcategory matching (no fuzzy fallback)
- EXCLUDE_PATTERNS to avoid bracket/retainer confusion
- Better part number extraction and brand classification
"""

import os
import re
import json
import time
import logging
import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any
from urllib.parse import unquote_plus

import os as _os
import httpx as _httpx
from rockauto_api import RockAutoClient
from rockauto_api.client.base import BaseClient as _BaseClient

# ─── Proxy Injection (sync, at import time) ───────────────────────────────────
# Patch BaseClient.__init__ so every RockAutoClient is created with the proxy
# already wired in — before any requests, no connection-pool contamination.
_original_base_init = _BaseClient.__init__

def _patched_base_init(self, *args, **kwargs):
    _original_base_init(self, *args, **kwargs)
    proxy_url = _os.getenv('ROCKAUTO_PROXY', '').strip()
    if proxy_url:
        old_headers = dict(self.session.headers)
        old_cookies = dict(self.session.cookies)
        self.session = _httpx.AsyncClient(
            proxy=proxy_url,
            headers=old_headers,
            timeout=30.0,
            follow_redirects=True,
            cookies=_httpx.Cookies(),
        )
        for name, value in old_cookies.items():
            self.session.cookies.set(name, value, domain='www.rockauto.com')
        self._proxy_applied = True
        try:
            from urllib.parse import urlparse as _up
            _p = _up(proxy_url)
            logger.info(f'RockAuto proxy active: {_p.scheme}://{_p.hostname}:{_p.port}')
        except Exception:
            pass

_BaseClient.__init__ = _patched_base_init

from .dictionary import PART_TO_CATEGORY

logger = logging.getLogger("parts-bot.rockauto")

# ─── RockAuto debug logging (gated by RA_DEBUG env var) ───────────────────────
# Set RA_DEBUG=true in .env to enable (default: off). Writes to logs/ra_debug.log
# with 10 MB rotation, 5 backups.
_RA_DEBUG_ACTIVE = os.environ.get("RA_DEBUG", "false").lower() == "true"
_DEBUG_LOG_PATH = Path(__file__).parent.parent / "logs" / "ra_debug.log"

_ra_debug_logger = logging.getLogger("parts-bot.rockauto.debug")
if _RA_DEBUG_ACTIVE and not _ra_debug_logger.handlers:
    _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ra_handler = RotatingFileHandler(
        _DEBUG_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    _ra_handler.setFormatter(logging.Formatter("%(asctime)s [DEBUG-RA] %(message)s"))
    _ra_debug_logger.addHandler(_ra_handler)
    _ra_debug_logger.setLevel(logging.DEBUG)
    _ra_debug_logger.propagate = False

def _debug_log(msg: str):
    if not _RA_DEBUG_ACTIVE:
        return
    _ra_debug_logger.debug(msg)

CACHE_PATH = Path(__file__).parent.parent / "cache" / "rockauto_cache.json"
CACHE_TTL_HOURS = 24

# ─── Brand Classification ─────────────────────────────────────────────────────

ECONOMY_BRANDS = {
    "TRQ", "EVAN FISCHER", "EVAN-FISCHER", "GARAGE-PRO", "GARAGE PRO",
    "REPLACE", "ACTION CRASH", "KEYSTONE", "PRONTO", "NEEDA",
    "TRIM PARTS", "REPLACEMENT", "DEPO", "EAGLE EYES", "MAXZONE",
    "K-METAL", "KOOL-VUE", "KOOL VUE", "MIRROR LINK", "TECHPRO",
    "CRASH PARTS", "CRASH PARTS PLUS",
}
PREMIUM_BRANDS = {
    "GENUINE", "OEM", "MOPAR", "MOTORCRAFT", "ACDELCO", "AC DELCO",
    "BOSCH", "DENSO", "AISIN", "CONTINENTAL", "ZF", "BREMBO",
    "BILSTEIN", "SACHS", "VALEO", "HELLA", "MAHLE", "NGK",
    "KOYO", "NSK", "NTN", "TIMKEN", "FAG",
}

KNOWN_BRANDS = [
    "VARIOUS MFR", "TECHPRO", "MOOG", "MEVOTECH", "DORMAN", "DEPO",
    "EAGLE EYES", "TRQ", "EVAN-FISCHER", "EVAN FISCHER", "GARAGE-PRO",
    "GARAGE PRO", "REPLACE", "ACTION CRASH", "KEYSTONE", "PRONTO",
    "CRASH PARTS PLUS", "CRASH PARTS", "K-METAL", "KOOL-VUE", "KOOL VUE",
    "BOSCH", "DENSO", "AISIN", "CONTINENTAL", "ZF", "BREMBO",
    "BILSTEIN", "SACHS", "VALEO", "HELLA", "MAHLE", "NGK",
    "KOYO", "NTN", "TIMKEN", "FAG", "KYB", "MONROE",
    "GENUINE", "MOPAR", "MOTORCRAFT", "ACDELCO", "AC DELCO",
    "DELPHI", "FVP", "CENTRIC", "CARDONE", "STANDARD MOTOR",
    "BECK ARNLEY", "BECK/ARNLEY", "GATES", "DAYCO", "SKF",
    "SPECTRA", "SPECTRA PREMIUM", "TYC", "PARTSLINK", "NTK",
    "API", "ASTA", "BROCK", "MAXZONE", "NSK", "SMP", "WAI",
]

# ─── Subcategory Exclusion Patterns ───────────────────────────────────────────
# Prevents returning brackets/retainers/reinforcements when searching for main parts

EXCLUDE_PATTERNS = {
    "bumper cover": ["retainer", "bracket", "reinforcement", "absorber", "support",
                     "filler", "step pad", "guard", "molding", "trim"],
    "fender": ["liner", "flare", "molding", "bracket", "brace", "shield", "seal"],
    "hood": ["hinge", "latch", "strut", "insulator", "scoop", "prop"],
    "headlamp assembly": ["bracket", "filler", "bezel", "adjuster", "bulb",
                          "socket", "wiring", "retainer"],
    "grille": ["bracket", "shutter", "screen", "guard", "insert"],
    "tail lamp assembly": ["bracket", "filler", "socket", "bulb", "wiring",
                           "gasket", "bezel"],
    "outside mirror": ["bracket", "cover", "cap", "glass only", "motor"],
    "door": ["hinge", "check", "weatherstrip", "seal", "glass", "handle",
             "lock", "latch", "striker"],
    "trunk": ["hinge", "latch", "strut", "seal", "weatherstrip"],
    "tailgate": ["hinge", "latch", "strut", "seal", "handle"],
    "spoiler": ["bracket", "bolt", "clip"],
    "control arm": ["bushing only", "bolt"],
    "strut assembly": ["mount", "bearing", "boot", "bumper"],
}


def classify_tier(brand: str) -> str:
    upper = brand.upper().strip()
    for e in ECONOMY_BRANDS:
        if e in upper:
            return "Economy"
    for p in PREMIUM_BRANDS:
        if p in upper:
            return "Premium"
    return "Daily Driver"


def parse_price(price_str: Optional[str]) -> Optional[float]:
    if not price_str:
        return None
    match = re.search(r'\$?([\d,]+\.?\d*)', price_str.replace(',', ''))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


# ─── Name Parsing ─────────────────────────────────────────────────────────────

def extract_brand_from_name(raw_name: str) -> Tuple[str, str]:
    """Extract brand name from raw concatenated name field."""
    upper = raw_name.upper()
    keep_upper = {"TRQ", "FVP", "KYB", "TYC", "SMP", "WAI", "NTK", "API", "SKF", "NGK", "NSK", "NTN", "ZF"}

    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if upper.startswith(brand):
            rest = raw_name[len(brand):].strip()
            display = brand if brand in keep_upper else brand.title()
            return display, rest

    return "Unknown", raw_name


def clean_description(raw: str) -> str:
    """Extract meaningful description from messy concatenated string."""
    cleaned = re.sub(r'[A-Z]{2}\d{5,}\w*', ' ', raw)
    cleaned = re.sub(r'\b\d{5}[A-Z]\d+\b', ' ', cleaned)
    cleaned = re.sub(r'[A-Z0-9]{8,}', ' ', cleaned)
    cleaned = re.sub(r'\[.*?\]', ' ', cleaned)
    for noise in ["Intentionally blank", "+ Sold in packs of", "Non-stockitem",
                   "shipping delayed", "Day Delay", "OE New", "CAPA Certified",
                   "Non-stock", "-Non-stockitem-", "business days"]:
        cleaned = cleaned.replace(noise, " ")
    cleaned = re.sub(r'^[\s,;\-\d]+', '', cleaned)
    cleaned = re.sub(r'[\s,;\-]+$', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else raw


def extract_availability(raw_name: str) -> str:
    lower = raw_name.lower()
    if "only" in lower and "remaining" in lower:
        m = re.search(r'only (\d+) remaining', lower)
        if m:
            return f"Low Stock ({m.group(1)} left)"
    if "non-stockitem" in lower or "non-stock" in lower:
        return "Non-Stock (delayed)"
    if "shipping delayed" in lower:
        return "In Stock (shipping delayed)"
    return "In Stock"


def extract_part_number(raw_name: str, library_pn: str) -> str:
    """Extract clean part number from raw name and library-provided PN."""
    pn = library_pn or "Unknown"
    make_names = {"UNKNOWN", "VARIOUS", "HYUNDAI", "TOYOTA", "HONDA", "KIA",
                  "NISSAN", "MERCEDES", "MERCEDES-BENZ", "BMW", "SUBARU",
                  "MAZDA", "MITSUBISHI", "SUZUKI", "CHEVROLET", "FORD",
                  "DODGE", "JEEP", "VOLKSWAGEN", "AUDI"}

    if pn.upper() in make_names:
        pn_match = re.search(r'([A-Z]{2}\d{5,}\w*)', raw_name)
        if not pn_match:
            pn_match = re.search(r'(\d{5}[A-Z0-9]+)', raw_name)
        if pn_match:
            pn = pn_match.group(1)

    # Clean appended description words from partslink codes
    pn_clean = re.match(r'^([A-Z]{2}\d{5,7}[A-Z]?)([A-Z][a-z].*)?$', pn)
    if pn_clean and pn_clean.group(2):
        pn = pn_clean.group(1)
    elif not re.match(r'^[A-Z0-9]{4,15}$', pn):
        pn_extract = re.match(r'^(\d{5}[A-Z]\d{3,5})', pn)
        if pn_extract:
            pn = pn_extract.group(1)

    return pn


# ─── Side / Position Filtering ────────────────────────────────────────────────

def matches_side(text: str, side: Optional[str]) -> bool:
    if not side:
        return True
    lower = text.lower()

    if side.lower() in ("left", "l", "lh", "driver"):
        wanted = {"left", " lh ", "driver", "izq", "izquierdo"}
        excluded = {"right", " rh ", "passenger", "derecho"}
    elif side.lower() in ("right", "r", "rh", "passenger"):
        wanted = {"right", " rh ", "passenger", "derecho"}
        excluded = {"left", " lh ", "driver", "izq", "izquierdo"}
    else:
        return True

    has_wanted = any(w in lower for w in wanted)
    has_excluded = any(w in lower for w in excluded)

    if not has_wanted and not has_excluded:
        return True
    if has_wanted and not has_excluded:
        return True
    return False


def matches_position(text: str, part_query: str) -> bool:
    lower = text.lower()
    query_lower = part_query.lower()
    if "front" in query_lower:
        if "rear" in lower and "front" not in lower:
            return False
    elif "rear" in query_lower:
        if "front" in lower and "rear" not in lower:
            return False
    return True


# ─── Category Matching ────────────────────────────────────────────────────────

def find_matching_category(part_query: str) -> Optional[Tuple[str, List[str]]]:
    """Look up part query in PART_TO_CATEGORY. Exact match only — no fuzzy."""
    q = part_query.lower().strip()

    if q in PART_TO_CATEGORY:
        return PART_TO_CATEGORY[q]

    # Try substring match: known key contained in query (e.g. "fender" in "rear fender assembly")
    # Sort longest-first so "bumper bracket" matches before "bumper" when query is "bumper bracket/guide".
    # Do NOT match q-in-key — that causes "valve" to match "valve cover", etc.
    for key in sorted(PART_TO_CATEGORY.keys(), key=len, reverse=True):
        if key in q:
            return PART_TO_CATEGORY[key]

    return None


def find_best_subcategory(subcategories: list, keywords: List[str], part_query: str) -> list:
    """STRICT matching — no fuzzy fallback. Returns empty rather than wrong part."""
    # Build exclusion list
    excludes = []
    for key, excl in EXCLUDE_PATTERNS.items():
        if any(kw.lower() in key for kw in keywords):
            excludes = excl
            break

    exact = []
    partial = []

    for sub in subcategories:
        sub_lower = sub.name.lower()

        # Check exclusions FIRST
        if any(excl in sub_lower for excl in excludes):
            continue

        for kw in keywords:
            kw_lower = kw.lower()
            if sub_lower == kw_lower:
                exact.append(sub)
                break
            elif kw_lower in sub_lower:
                partial.append(sub)
                break

    if exact:
        return exact
    if partial:
        return partial

    # NO fuzzy fallback — return empty rather than wrong part
    return []


# ─── Cache ────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            data = json.loads(CACHE_PATH.read_text())
            now = time.time()
            return {k: v for k, v in data.items()
                    if now - v.get("cached_at", 0) < CACHE_TTL_HOURS * 3600}
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def vehicle_cache_key(make: str, year: int, model: str) -> str:
    return f"{make.upper()}|{year}|{model.upper()}"






# ─── Vehicle Resolution ──────────────────────────────────────────────────────

async def resolve_vehicle(client: RockAutoClient, make: str, year: int, model: str,
                          cache: dict) -> Tuple[str, str, bool]:
    """Resolve vehicle to carcode + engine. Uses cache if available."""
    key = vehicle_cache_key(make, year, model)
    if key in cache:
        entry = cache[key]
        return entry["carcode"], entry["engine_desc"], True

    try:
        # Wrap with a 12-second timeout so a blocked/slow RockAuto fails fast
        # instead of hanging for the full 30-second httpx default.
        import asyncio as _asyncio
        engines = await _asyncio.wait_for(
            client.get_engines_for_vehicle(make, year, model),
            timeout=12.0
        )
    except _asyncio.TimeoutError:
        logger.warning(
            f"RockAuto timeout resolving {year} {make} {model} — "
            "server IP may be blocked or rate-limited by RockAuto."
        )
        raise RuntimeError(f"Vehicle not found: {year} {make} {model}. (timeout — RockAuto unreachable)")
    except Exception as e:
        exc_type = type(e).__name__
        logger.warning(f"RockAuto vehicle lookup failed [{exc_type}]: {year} {make} {model}: {e}")
        raise RuntimeError(f"Vehicle not found: {year} {make} {model}. {e}")

    if not engines.engines:
        raise RuntimeError(f"No engines found for {year} {make} {model}")

    # Pick first engine — for body parts this doesn't matter.
    # For mechanical parts, log when multiple exist.
    engine = engines.engines[0]
    if len(engines.engines) > 1:
        logger.info(f"Multiple engines for {year} {make} {model}: "
                     f"{len(engines.engines)} options, using '{engine.description}'")

    carcode = engine.carcode
    engine_desc = engine.description

    cache[key] = {
        "carcode": carcode,
        "engine_desc": engine_desc,
        "make": make.upper(),
        "year": year,
        "model": model.upper(),
        "cached_at": time.time(),
    }
    save_cache(cache)
    return carcode, engine_desc, False


# ─── Main Search ─────────────────────────────────────────────────────────────

async def search_rockauto(
    client: RockAutoClient,
    make: str,
    year: int,
    model: str,
    carcode: str,
    part_query: str,
    side: Optional[str] = None,
    limit: int = 15,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Search RockAuto for parts. Returns (results_list, error_message)."""

    _debug = _RA_DEBUG_ACTIVE  # gated by RA_DEBUG env var

    # Map part query to category
    cat_match = find_matching_category(part_query)
    if not cat_match:
        if _debug:
            _debug_log(f"❌ find_matching_category('{part_query}') → no match")
        return [], f"No category mapping for '{part_query}'"

    target_category, subcategory_keywords = cat_match
    if _debug:
        _debug_log(f"✅ find_matching_category('{part_query}') → category={target_category!r} keywords={subcategory_keywords}")

    # Get vehicle categories
    try:
        vehicle_cats = await client.get_part_categories(make, year, model, carcode)
    except Exception as e:
        return [], f"Failed to get categories: {e}"

    # Find matching category
    matching_cat = None
    for vc in vehicle_cats.categories:
        if vc.name.lower() == target_category.lower():
            matching_cat = vc
            break
    if not matching_cat:
        for vc in vehicle_cats.categories:
            if target_category.lower() in vc.name.lower() or vc.name.lower() in target_category.lower():
                matching_cat = vc
                break

    if not matching_cat:
        if _debug:
            available_cats = [vc.name for vc in vehicle_cats.categories]
            _debug_log(f"❌ category '{target_category}' not found for vehicle. Available: {available_cats[:15]}")
        return [], f"Category '{target_category}' not available for this vehicle"

    # Get subcategories
    group_name = unquote_plus(matching_cat.group_name)

    try:
        subcats = await client.get_parts_by_category(make, year, model, carcode, group_name)
    except Exception as e:
        if _debug:
            _debug_log(f"❌ get_parts_by_category('{group_name}') EXCEPTION: {e}")
        return [], f"Failed to get subcategories: {e}"

    if not subcats.parts:
        if _debug:
            _debug_log(f"❌ get_parts_by_category('{group_name}') returned 0 subcategories")
        return [], f"No subcategories in '{matching_cat.name}'"

    # Find matching subcategory (STRICT — no fuzzy)
    best_subs = find_best_subcategory(subcats.parts, subcategory_keywords, part_query)
    if not best_subs:
        available = [p.name for p in subcats.parts]
        if _debug:
            _debug_log(f"❌ find_best_subcategory → no match. Available subcats: {available[:10]}")
        return [], f"No matching subcategory. Available: {', '.join(available[:10])}"
    if _debug:
        _debug_log(f"✅ find_best_subcategory → matched: {[s.name for s in best_subs[:2]]}")

    # Get individual parts from top 2 matching subcategories
    all_parts = []
    sub_urls = {}  # subcat_name → page URL (fallback when part.url is None)
    for sub in best_subs[:2]:
        if not sub.url:
            continue
        # Build full subcategory URL for linking
        sub_page_url = (
            f"https://www.rockauto.com/en/catalog/"
            f"{make.lower()},{year},{model.lower().replace(' ', '+')},"
            f"{carcode},{sub.url}"
        ) if not sub.url.startswith("http") else sub.url
        sub_urls[sub.name] = sub_page_url
        try:
            result = await client.get_individual_parts_from_subcategory(
                make, year, model, carcode, sub.url
            )
            if result.parts:
                all_parts.extend([(sub.name, p) for p in result.parts])
        except Exception:
            continue

    if not all_parts:
        if _debug:
            _debug_log(f"❌ subcategory search returned 0 parts for subs: {[s.name for s in best_subs[:2]]}")
        return [], "Found category but failed to retrieve individual parts"
    if _debug:
        _debug_log(f"✅ subcategory search returned {len(all_parts)} raw parts from subs: {[s.name for s in best_subs[:2]]}")

    # Parse, filter, and sort results
    results = []
    seen_prices = set()

    for subcat_name, part in all_parts:
        price = parse_price(part.price)
        if price is None:
            continue

        raw_name = part.name or ""

        if not matches_side(raw_name, side):
            continue
        if not matches_position(raw_name, part_query):
            continue
        if re.search(r'>\d{4}>', raw_name):
            continue
        if "intentionally blank" in raw_name.lower():
            continue

        brand, desc_rest = extract_brand_from_name(raw_name)
        pn = extract_part_number(raw_name, part.part_number or "Unknown")

        dedup_key = (pn, price)
        if dedup_key in seen_prices:
            continue
        seen_prices.add(dedup_key)

        description = clean_description(desc_rest)
        availability = extract_availability(raw_name)

        # Use part URL if available, fall back to subcategory page URL
        part_url = part.url or sub_urls.get(subcat_name, "")

        results.append({
            "brand": brand if brand != "Unknown" else (part.brand or "Unknown"),
            "part_number": pn,
            "description": description,
            "subcategory": subcat_name,
            "price": price,
            "tier": classify_tier(brand if brand != "Unknown" else (part.brand or "")),
            "availability": availability,
            "url": part_url,
            "source": "RockAuto",
        })

    results.sort(key=lambda x: x["price"])
    if _debug:
        oem_numbers = [r["part_number"] for r in results[:limit] if r.get("part_number")]
        _debug_log(f"✅ {len(results)} results after filter. OEM#s extracted: {oem_numbers[:10]}")
    return results[:limit], None
