"""
Search Engine Orchestrator — searches ALL parts in PARALLEL.

This is the main entry point called by the WhatsApp bot.
It coordinates VIN decode, dictionary translation, RockAuto + eBay searches,
and landed cost calculation for every part.

Key architecture: asyncio.gather with semaphore for parallel execution.
Target: 2-5 minutes for a 20-part batch.
"""

import asyncio
import json
import os
import logging
import sys
from typing import Optional
from pathlib import Path

from rockauto_api import RockAutoClient

from .dictionary import translate_part, PART_TO_CATEGORY
from .vin_decode import decode_vin
from .ebay_search import search_ebay, get_ebay_token
from .rockauto_search import (
    search_rockauto, resolve_vehicle,
    load_cache as load_ra_cache, save_cache as save_ra_cache,
)
from .cost_calculator import calculate_landed_cost

logger = logging.getLogger("parts-bot.engine")

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SEARCHES", "5"))

# ── Cross-platform OEM# validation ────────────────────────────────────────────
# Maps VIN prefix (3-char first, then 2-char) → brand name
_VIN_BRAND = {
    "WP0": "Porsche", "WP1": "Porsche",    # Porsche (cars + SUVs)
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz",
    "WVW": "Volkswagen", "WV2": "Volkswagen",
    "WAU": "Audi", "WUA": "Audi",
    "1HG": "Honda", "2HG": "Honda", "JHM": "Honda",
    "JN1": "Nissan", "3N1": "Nissan",
    "1G1": "Chevrolet", "2G1": "Chevrolet",
    "1FT": "Ford", "1FA": "Ford", "1FM": "Ford",
    "KMH": "Hyundai", "KNA": "Kia",
}
_VIN_BRAND_2 = {
    "WP": "Porsche",
    "JT": "Toyota", "4T": "Toyota",
}

# OEM# 3-char prefixes that belong to a specific platform NOT shared with the VIN brand.
# Porsche Macan shares the Q5 B8 platform (8R0) — intentionally excluded.
_CROSS_PLATFORM_OEM = {
    "8K0": "Audi A4 (B8)",
    "8T0": "Audi A5 (B8)",
    "4G0": "Audi A6 (C7)",
    "4H0": "Audi A8 (D4)",
    "5Q0": "VW MQB",
    "3Q0": "VW MQB",
    "5K0": "VW Golf Mk6",
    "1K0": "VW Golf Mk5/6",
}


def _check_platform_mismatch(vin: str, oem_number: str) -> str | None:
    """Return a note if the OEM# prefix doesn't match the VIN's platform. None if OK."""
    if not vin or not oem_number or len(oem_number) < 3:
        return None
    # Resolve VIN brand — 3-char prefix first, then 2-char fallback
    vin_brand = _VIN_BRAND.get(vin[:3]) or _VIN_BRAND_2.get(vin[:2])
    if not vin_brand:
        return None
    oem_prefix = oem_number[:3].upper()
    wrong_platform = _CROSS_PLATFORM_OEM.get(oem_prefix)
    if wrong_platform and vin_brand not in wrong_platform:
        return f"cross-platform OEM ({wrong_platform}) — verify fitment"
    return None


async def search_single_part(
    part: dict,
    vehicle_info: dict,
    ebay_token: str,
    ra_client: RockAutoClient,
    ra_cache: dict,
    on_progress: Optional[callable] = None,
) -> dict:
    """Search one part across RockAuto + eBay. Called in parallel for all parts."""

    result = {
        "part": part,
        "rockauto": None,
        "ebay": None,
        "best_option": None,
        "landed_cost": None,
        "error": None,
        "oem_platform_mismatch": None,
    }

    part_english = part.get("name_english", "")
    side = part.get("side")
    position = part.get("position")

    # Build position-aware query
    query_parts = []
    if position:
        query_parts.append(position)
    query_parts.append(part_english)
    full_query = " ".join(query_parts)

    def _is_real_oem(pn: str) -> bool:
        """Return True only for genuine OEM-style part numbers.
        Filters out brand names (all-alpha), placeholder strings, and too-short codes."""
        if not pn or pn in ("Unknown", "N/F"):
            return False
        import re as _re_oem
        if not _re_oem.search(r'\d', pn):   # must contain at least one digit
            return False
        if not (5 <= len(pn) <= 18):
            return False
        if pn.isalpha():                      # purely alphabetic = brand name
            return False
        return True

    try:
        # ── Step 1: Resolve OEM# ─────────────────────────────────────────────
        # Priority: part dict → 7zap (VIN-exact) → RockAuto fallback
        oem_number = part.get("part_number") or part.get("oem_number") or ""
        vin = vehicle_info.get("vin", "")

        import os as _os
        _use_7zap = _os.getenv("OEM_LOOKUP_SOURCE", "7zap").lower() == "7zap" and bool(vin)

        if _use_7zap:
            try:
                from .oem_lookup_7zap import lookup_oem_by_vin, SevenZapAuthError
                _zap = await lookup_oem_by_vin(vin, part_english, make_hint=vehicle_info.get("make"))
                if _zap.oem_number:
                    oem_number = _zap.oem_number
                    result["oem_source"] = _zap.source
                    result["oem_confidence"] = _zap.confidence
                    logger.info(
                        f"7zap OEM# for '{part_english}': {oem_number} "
                        f"({_zap.source}, score from candidates)"
                    )
                else:
                    logger.debug(f"7zap no result for '{part_english}': {_zap.error}")
                    _use_7zap = False  # trigger RockAuto fallback below
            except SevenZapAuthError as _e:
                logger.error(f"7zap cookies expired — falling back to RockAuto: {_e}")
                _use_7zap = False

        # RockAuto: fallback when 7zap is disabled/failed/returned nothing.
        # Still used for fitment data even when 7zap returns an OEM#.
        # Its results are NEVER shown as a purchase source.
        if not _use_7zap or not oem_number:
            ra_results, ra_error = await search_rockauto(
                client=ra_client,
                make=vehicle_info["make"],
                year=vehicle_info["year"],
                model=vehicle_info["model"],
                carcode=vehicle_info.get("carcode", ""),
                part_query=full_query,
                side=side,
            )
            if ra_results:
                result["rockauto"] = ra_results[0]
                ra_oem = ra_results[0].get("part_number", "")
                if _is_real_oem(ra_oem) and not oem_number:
                    oem_number = ra_oem
            elif ra_error and not oem_number:
                result["error"] = f"RockAuto: {ra_error}"

            # Platform validation only on RockAuto-sourced OEM# (7zap is VIN-exact)
            if oem_number and not result.get("oem_source", "").startswith("7zap"):
                mismatch = _check_platform_mismatch(vin, oem_number)
                if mismatch:
                    result["oem_platform_mismatch"] = mismatch
                    logger.warning(f"Platform mismatch for '{part_english}': OEM# {oem_number} → {mismatch}")

        # ── Step 2: Build eBay query — OEM# first, name-based as fallback ──
        name_query = (f"{vehicle_info['year']} {vehicle_info['make']} "
                      f"{vehicle_info['model']} {full_query} "
                      f"{side or ''}").strip()

        if _is_real_oem(oem_number):
            # Primary: OEM part number + make (tight, specific)
            ebay_query = f"{oem_number} {vehicle_info['make']}"
            ebay_results = await search_ebay(
                query=ebay_query,
                side=side,
                _token=ebay_token,
                part_english=part_english,
            )
            # Fallback: name-based if OEM# search returned nothing
            if not ebay_results:
                logger.info(f"OEM# '{oem_number}' returned no eBay results — falling back to name query")
                ebay_results = await search_ebay(
                    query=name_query,
                    side=side,
                    _token=ebay_token,
                    part_english=part_english,
                )
        else:
            # No valid OEM# — search by description only
            ebay_results = await search_ebay(
                query=name_query,
                side=side,
                _token=ebay_token,
                part_english=part_english,
            )

        if ebay_results:
            result["ebay"] = ebay_results[0]  # Cheapest valid eBay result

        # Step 3: Pick best option (cheapest across sources)
        result["best_option"] = _pick_best_option(
            result["rockauto"], result["ebay"], part
        )

        # Step 4: Calculate landed cost if we have a price
        best = result["best_option"]
        if best and best.get("price"):
            shipping = best.get("shipping", 0)
            result["landed_cost"] = calculate_landed_cost(
                listing_price_usd=best["price"],
                us_shipping_usd=shipping,
                part_name_english=part_english,
            )

    except Exception as e:
        logger.error(f"Error searching '{part_english}': {e}")
        result["error"] = str(e)

    return result


def _pick_best_option(rockauto: dict | None, ebay: dict | None, part: dict) -> dict | None:
    """Pick the best eBay listing. RockAuto is used only for OEM# — never as a purchase source."""

    if not (ebay and ebay.get("price")):
        return None

    best = {
        "price": ebay["price"],
        "shipping": ebay.get("shipping", 0),
        "total_price": ebay.get("total_price", ebay["price"]),
        "part_number": "",
        "brand": "",
        "condition": ebay.get("condition", "Unknown"),
        "source": "eBay",
        "url": ebay.get("url", ""),
        "tier": "",
        "availability": "In Stock",
        "delivery_days_min": ebay.get("delivery_days_min"),
        "delivery_days_max": ebay.get("delivery_days_max"),
    }

    # Carry RockAuto OEM# forward so it shows in the results table
    if rockauto and rockauto.get("part_number"):
        best["part_number"] = rockauto["part_number"]

    return best


async def search_all_parts(
    parts_list: list,
    vehicle_info: dict,
    on_progress: Optional[callable] = None,
) -> list:
    """Search ALL parts in parallel. Main entry point.

    Args:
        parts_list: List of dicts from OCR extraction, each with:
            name_original, name_english, side, position, local_price
        vehicle_info: Dict with: vin, year, make, model (from VIN decode or OCR)
        on_progress: Optional async callback(found_count, total_count) for progress updates

    Returns:
        List of result dicts, one per part.
    """
    # Setup — configure proxy before creating client so httpx picks it up
    proxy_url = os.getenv("ROCKAUTO_PROXY")
    if proxy_url:
        os.environ.setdefault("HTTPS_PROXY", proxy_url)
        os.environ.setdefault("HTTP_PROXY", proxy_url)
    ra_client = RockAutoClient(enable_caching=True)
    ra_cache = load_ra_cache()

    # Resolve vehicle on RockAuto (done once, cached)
    try:
        carcode, engine_desc, from_cache = await resolve_vehicle(
            ra_client,
            vehicle_info["make"],
            vehicle_info["year"],
            vehicle_info["model"],
            ra_cache,
        )
        vehicle_info["carcode"] = carcode
        vehicle_info["engine"] = engine_desc
        logger.info(f"Vehicle resolved: {vehicle_info['year']} {vehicle_info['make']} "
                     f"{vehicle_info['model']} | carcode={carcode} | "
                     f"engine={engine_desc} | cached={from_cache}")
    except Exception as e:
        logger.warning(f"RockAuto vehicle resolution failed: {e}. "
                        "Will search eBay only.")
        vehicle_info["carcode"] = ""
        vehicle_info["engine"] = ""

    # Get eBay token once for all searches
    try:
        ebay_token = await get_ebay_token()
    except Exception as e:
        logger.warning(f"eBay token failed: {e}. eBay searches will be skipped.")
        ebay_token = ""

    # Search ALL parts in parallel with concurrency limit
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    found_count = 0
    total = len(parts_list)

    async def limited_search(part):
        nonlocal found_count
        async with semaphore:
            result = await search_single_part(
                part, vehicle_info, ebay_token, ra_client, ra_cache, on_progress
            )
            found_count += 1
            if on_progress:
                try:
                    await on_progress(found_count, total)
                except Exception:
                    pass
            return result

    results = await asyncio.gather(
        *[limited_search(part) for part in parts_list],
        return_exceptions=True,
    )

    # Convert exceptions to error results
    final_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final_results.append({
                "part": parts_list[i],
                "rockauto": None,
                "ebay": None,
                "best_option": None,
                "landed_cost": None,
                "error": str(r),
            })
        else:
            final_results.append(r)

    return final_results


# ─── CLI entry point for testing ──────────────────────────────────────────────

async def _cli_main():
    """Run a search from command line for testing."""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Parts search engine")
    parser.add_argument("--vin", required=True, help="Vehicle VIN")
    parser.add_argument("--parts", required=True, help="Comma-separated parts (DR Spanish or English)")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    # Load .env
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    # Decode VIN
    print(f"Decoding VIN: {args.vin}...")
    vehicle = await decode_vin(args.vin)
    if vehicle.get("error"):
        print(f"VIN error: {vehicle['error']}")
        sys.exit(1)
    print(f"Vehicle: {vehicle['year']} {vehicle['make']} {vehicle['model']}")

    # Translate parts
    raw_parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    parts_list = []
    for p in raw_parts:
        translated = translate_part(p)
        translated["local_price"] = 0
        parts_list.append(translated)
        print(f"  {p} → {translated['name_english']} (side={translated['side']}, pos={translated['position']})")

    # Search
    async def progress(found, total):
        print(f"  Progress: {found}/{total}")

    print(f"\nSearching {len(parts_list)} parts...")
    results = await search_all_parts(parts_list, vehicle, on_progress=progress)

    # Output
    output = {
        "vehicle": vehicle,
        "results": results,
        "summary": {
            "total_parts": len(results),
            "found": sum(1 for r in results if r.get("best_option")),
            "not_found": sum(1 for r in results if not r.get("best_option")),
            "errors": sum(1 for r in results if r.get("error")),
        }
    }

    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2, default=str))
        print(f"\nResults saved to {args.output}")
    else:
        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(_cli_main())
