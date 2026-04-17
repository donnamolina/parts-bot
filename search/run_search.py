#!/usr/bin/env python3
"""
Search bridge script — called by server.js to run the full search pipeline.
Reads extraction JSON from input file, runs parallel search, generates Excel.
Outputs summary JSON to stdout. Writes progress to a sidecar file.
"""

import argparse
import asyncio
import json
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SONNET_MODEL = os.environ.get("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-6")

sys.path.insert(0, str(Path(__file__).parent.parent))

from search.engine import search_all_parts
from search.excel_builder import generate_excel
from search.verify_listing import verify_ebay_listing
from search.dictionary import translate_part
from search.vin_decode import decode_vin
from search.db_client import (
    get_correction_override, get_cached_result, upsert_cached_result_safe
)

# Rotating log: 10 MB per file, keep 5 backups (~50 MB cap)
_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_rotating_handler = RotatingFileHandler(
    _LOG_DIR / "searches.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    handlers=[
        _rotating_handler,
        logging.StreamHandler(sys.stderr),
    ]
)
logger = logging.getLogger("parts-bot.run_search")


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


async def sonnet_verify_results(vehicle_info: dict, results: list) -> list:
    """Single Sonnet call to review the full results table and flag issues.
    Returns list of flagged issue strings, empty if all looks good.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping verification pass")
        return []

    lines = []
    for i, r in enumerate(results, 1):
        part = r.get("part", {})
        best = r.get("best_option")
        name_orig = part.get("name_original", "")
        name_en = part.get("name_english", "")
        side = part.get("side") or "none"
        position = part.get("position") or ""

        # Bug 10: manual-review parts skip eBay pipeline and have price=None
        if r.get("manual_review"):
            pn = (best or {}).get("part_number", "") or "no OEM#"
            lines.append(
                f"{i}. DR:\"{name_orig}\" EN:\"{name_en}\" side:{side} pos:{position} "
                f"| MANUAL REVIEW ({r.get('manual_review')}, {pn})"
            )
        elif best:
            price = best.get("total_price") or best.get("price", 0) or 0
            source = best.get("source", "?")
            pn = best.get("part_number", "") or "no OEM#"
            lines.append(
                f"{i}. DR:\"{name_orig}\" EN:\"{name_en}\" side:{side} pos:{position} "
                f"| best: ${price:.2f} ({source}, {pn})"
            )
        else:
            lines.append(
                f"{i}. DR:\"{name_orig}\" EN:\"{name_en}\" side:{side} pos:{position} | NO RESULTS"
            )

    table = "\n".join(lines)
    prompt = (
        f"Vehicle: {vehicle_info.get('year')} {vehicle_info.get('make')} {vehicle_info.get('model')}\n\n"
        f"Parts search results:\n{table}\n\n"
        f"Review each result. Flag ONLY items that look wrong: wrong part type, wrong side/fitment, "
        f"suspicious price (way too cheap or too expensive for the part), or translation that doesn't "
        f"make sense for this vehicle. "
        f"If everything looks fine, return exactly: OK\n"
        f"Otherwise return a short bulleted list in Spanish, one line per issue, format: "
        f"\"#N: [issue]\". Be concise. Max 1 sentence per flag."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.upper() == "OK":
            return []
        flags = [line.strip().lstrip("•-* ") for line in raw.splitlines() if line.strip()]
        return flags
    except Exception as e:
        logger.warning(f"Sonnet verification failed: {e}")
        return []


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to extraction JSON")
    parser.add_argument("--output", required=True, help="Path for output Excel")
    parser.add_argument("--results-output", help="Optional path to write full results JSON")
    args = parser.parse_args()

    load_env()

    # Read extraction data
    input_data = json.loads(Path(args.input).read_text())
    extraction = input_data.get("extraction", input_data)

    vin = extraction.get("vin", "")
    vehicle_from_ocr = extraction.get("vehicle", {})
    parts_raw = extraction.get("parts", [])
    supplier_total_dop = extraction.get("supplier_total_dop")

    if not parts_raw:
        json.dump({"error": "No parts in extraction data"}, sys.stdout)
        print()
        sys.exit(1)

    # Decode VIN for accurate vehicle data
    vehicle_info = {}
    if vin and len(vin) == 17:
        logger.info(f"Decoding VIN: {vin}")
        decoded = await decode_vin(vin)
        if not decoded.get("error"):
            # NHTSA returns uppercase makes ("TOYOTA", "KIA") — normalize to title case
            # so rockauto_api can match the vehicle catalog correctly.
            raw_make = decoded["make"] or ""
            raw_model = decoded["model"] or ""
            vehicle_info = {
                "vin": vin,
                "year": decoded["year"],
                "make": raw_make.title() if raw_make.isupper() else raw_make,
                "model": raw_model.title() if raw_model.isupper() else raw_model,
                "trim": decoded.get("trim"),
            }
            logger.info(f"VIN decoded: {vehicle_info['year']} {vehicle_info['make']} {vehicle_info['model']}")
        else:
            logger.warning(f"VIN decode failed: {decoded['error']}")

    # Fallback to OCR vehicle data
    if not vehicle_info.get("make"):
        vehicle_info = {
            "vin": vin or "N/A",
            "year": vehicle_from_ocr.get("year", 0),
            "make": vehicle_from_ocr.get("make", "Unknown"),
            "model": vehicle_from_ocr.get("model", "Unknown"),
            "trim": vehicle_from_ocr.get("trim"),
        }

    # Build parts list for search engine — deduplicating by name+side+position
    # (OCR sometimes lists the same part twice; merge into quantity instead)
    seen_parts: dict[tuple, dict] = {}
    for p in parts_raw:
        part = {
            "name_original": p.get("name_original", ""),
            "name_dr": p.get("name_dr", ""),
            "name_english": p.get("name_english", ""),
            "side": p.get("side"),
            "position": p.get("position"),
            "local_price": p.get("local_price", 0) or 0,
            "quantity": p.get("quantity", 1) or 1,
        }

        # Always run translate_part so side/position are extracted from DR Spanish name.
        # OCR sets name_english but often leaves side=null — translate_part's
        # extract_side_position is the reliable source for izquierdo/derecho.
        translated = translate_part(part["name_original"])
        if not part["name_english"] or part["name_english"] == part["name_original"]:
            part["name_english"] = translated["name_english"]
        part["side"] = part["side"] or translated["side"]
        part["position"] = part["position"] or translated["position"]

        dedup_key = (
            part["name_english"].lower().strip(),
            (part["side"] or "").lower(),
            (part["position"] or "").lower(),
        )
        if dedup_key in seen_parts:
            # Same part listed twice — increment quantity, accumulate price
            seen_parts[dedup_key]["quantity"] += part["quantity"]
            seen_parts[dedup_key]["local_price"] += part["local_price"]
            logger.info(f"Deduped '{part['name_english']}' → qty {seen_parts[dedup_key]['quantity']}")
        else:
            seen_parts[dedup_key] = part

    parts_list = list(seen_parts.values())

    # Apply correction overrides from the learning DB (confirmed past corrections)
    make = vehicle_info.get("make", "")
    model = vehicle_info.get("model", "")
    for part in parts_list:
        override = get_correction_override(make, model, part.get("name_english", ""))
        if not override:
            override = get_correction_override(make, model, part.get("name_original", ""))
        if override and override != part.get("name_english"):
            logger.info(f"Correction override applied: '{part['name_english']}' → '{override}'")
            part["name_english"] = override

    # Cache lookup — split into cached vs needs-search
    year = vehicle_info.get("year", 0)
    cached_indices = {}   # index → pre-built result dict
    search_parts = []
    search_indices = []   # original index in parts_list

    for i, part in enumerate(parts_list):
        cached = get_cached_result(make, model, year, part.get("name_english", ""))
        # Invalidate cached results that have RockAuto as source — stored before
        # RockAuto was removed as a purchase source; must re-search via eBay.
        if cached and (cached.get("best_option") or {}).get("source") == "RockAuto":
            cached = None
        if cached:
            cached["part"] = part
            cached_indices[i] = cached
        else:
            search_parts.append(part)
            search_indices.append(i)

    logger.info(
        f"Searching {len(search_parts)} parts for {vehicle_info.get('year')} "
        f"{vehicle_info.get('make')} {vehicle_info.get('model')} "
        f"({len(cached_indices)} from cache)"
    )

    # Progress callback — writes to sidecar file for Node.js to read
    progress_path = args.input.replace(".json", "_progress.json")

    async def on_progress(found, total):
        try:
            Path(progress_path).write_text(json.dumps({
                "found": found,
                "total": total,
            }))
        except Exception:
            pass

    # Run parallel search (only non-cached parts)
    fresh_results = await search_all_parts(
        search_parts, vehicle_info, on_progress=on_progress
    ) if search_parts else []

    # Merge cached + fresh results in original order
    fresh_iter = iter(fresh_results)
    results = []
    for i in range(len(parts_list)):
        if i in cached_indices:
            results.append(cached_indices[i])
        else:
            results.append(next(fresh_iter))

    # ── Per-listing Sonnet verification (concurrent) ──────────────────────────
    # For every part where 7zap found an OEM# AND eBay returned a listing,
    # verify the listing title actually matches the requested part.
    # All calls run concurrently — adds ~2s total, not 20s.
    _verify_enabled = os.getenv("VERIFY_WITH_SONNET", "true").lower() == "true"
    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if _verify_enabled and _api_key:
        _to_verify = []
        for _r in results:
            _oem_src = _r.get("oem_source", "") or ""
            _best = _r.get("best_option") or {}
            _part = _r.get("part", {}) or {}
            if _oem_src.startswith("7zap") and _best.get("price") and _best.get("title"):
                _to_verify.append((_r, _part, _best))

        if _to_verify:
            logger.info(f"Running Sonnet listing verification for {len(_to_verify)} parts...")
            _verify_coros = [
                verify_ebay_listing(
                    part_name_english=_p.get("name_english", ""),
                    year=vehicle_info.get("year", 0),
                    make=vehicle_info.get("make", ""),
                    model=vehicle_info.get("model", ""),
                    oem_number=_b.get("part_number", ""),
                    listing_title=_b.get("title", ""),
                    price=_b.get("price", 0),
                    api_key=_api_key,
                )
                for _r, _p, _b in _to_verify
            ]
            _verdicts = await asyncio.gather(*_verify_coros, return_exceptions=True)
            for (_r, _p, _b), _verdict in zip(_to_verify, _verdicts):
                if isinstance(_verdict, Exception):
                    logger.warning(f"Verification error for '{_p.get('name_english')}': {_verdict}")
                    continue
                _r["sonnet_verify"] = _verdict
                if _verdict["verdict"] != "MATCH":
                    logger.info(
                        f"Listing verify: '{_p.get('name_english')}' → "
                        f"{_verdict['verdict']}: {_verdict['note']}"
                    )

        # ── WRONG_PART retry: discard bad OEM#, retry with name-based eBay search ──
        _wrong_parts = [
            (_r, _p, _b) for (_r, _p, _b) in _to_verify
            if (_r.get("sonnet_verify") or {}).get("verdict") == "WRONG_PART"
        ]
        if _wrong_parts:
            logger.info(f"WRONG_PART retry: {len(_wrong_parts)} part(s) to retry with name search")
            from search.ebay_search import search_ebay, get_ebay_token
            from search.cost_calculator import calculate_landed_cost
            _ebay_token = await get_ebay_token()

            _retry_coros = []
            _retry_refs = []
            for _r, _p, _b in _wrong_parts:
                _part_en = _p.get("name_english", "")
                _side = _p.get("side")
                # Use more specific search terms for certain part types to avoid
                # unrelated products (e.g. "running board" can return hitch steps)
                _STEP_SYNONYMS = {
                    "running board": "running board side step",
                    "step bar": "running board side step bar",
                }
                _search_term = _STEP_SYNONYMS.get(_part_en.lower(), _part_en)
                _nm_q = (
                    f"{vehicle_info.get('year')} {vehicle_info.get('make')} "
                    f"{vehicle_info.get('model')} {_search_term} {_side or ''}"
                ).strip()
                _retry_coros.append(search_ebay(
                    query=_nm_q,
                    side=_side,
                    _token=_ebay_token,
                    part_english=_part_en,
                ))
                _retry_refs.append((_r, _p))

            _retry_ebay = await asyncio.gather(*_retry_coros, return_exceptions=True)

            _reverify_coros = []
            _reverify_refs = []
            for (_r, _p), _ebay_res in zip(_retry_refs, _retry_ebay):
                if isinstance(_ebay_res, Exception) or not _ebay_res:
                    logger.warning(f"WRONG_PART retry: no eBay results for '{_p.get('name_english')}'")
                    continue
                _listing = _ebay_res[0]
                _reverify_coros.append(verify_ebay_listing(
                    part_name_english=_p.get("name_english", ""),
                    year=vehicle_info.get("year", 0),
                    make=vehicle_info.get("make", ""),
                    model=vehicle_info.get("model", ""),
                    oem_number="",
                    listing_title=_listing.get("title", ""),
                    price=_listing.get("price", 0),
                    api_key=_api_key,
                ))
                _reverify_refs.append((_r, _p, _listing))

            if _reverify_coros:
                _reverify_v = await asyncio.gather(*_reverify_coros, return_exceptions=True)
                for (_r, _p, _listing), _v in zip(_reverify_refs, _reverify_v):
                    if isinstance(_v, Exception):
                        continue
                    _part_en = _p.get("name_english", "")
                    # Replace result with name-based listing, clear bad OEM#
                    _listing["part_number"] = ""
                    _r["ebay"] = _listing
                    _r["best_option"] = _listing
                    _r["oem_source"] = "name_fallback"
                    _r["oem_confidence"] = "yellow"
                    _r["sonnet_verify"] = _v
                    if _listing.get("price"):
                        _r["landed_cost"] = calculate_landed_cost(
                            listing_price_usd=_listing["price"],
                            us_shipping_usd=_listing.get("shipping", 0),
                            part_name_english=_part_en,
                        )
                    logger.info(
                        f"WRONG_PART retry '{_part_en}': verdict={_v.get('verdict')} "
                        f"listing='{_listing.get('title','')[:60]}'"
                    )

    # Sonnet end-of-batch verification pass
    sonnet_flags = await sonnet_verify_results(vehicle_info, results)
    if sonnet_flags:
        logger.info(f"Sonnet flagged {len(sonnet_flags)} issue(s): {sonnet_flags}")
    else:
        logger.info("Sonnet verification: all clear")

    # Cache fresh results (fire-and-forget — don't block Excel generation)
    flagged_indices = set()
    for flag in sonnet_flags:
        import re as _re
        m = _re.search(r'#(\d+)', flag)
        if m:
            flagged_indices.add(int(m.group(1)) - 1)  # 0-based

    for i, r in enumerate(results):
        _sv_verdict = (r.get("sonnet_verify") or {}).get("verdict", "")
        _skip_cache = (r.get("from_cache")
                       or not r.get("best_option")
                       or i in flagged_indices
                       or _sv_verdict == "WRONG_PART")
        if not _skip_cache:
            part = r.get("part", {})
            upsert_cached_result_safe(
                make, model, year,
                part.get("name_english", ""),
                r,
                verified_by_correction=False,
            )

    # Generate Excel
    logger.info(f"Generating Excel: {args.output}")
    generate_excel(results, vehicle_info, args.output,
                   supplier_total_dop=supplier_total_dop,
                   sonnet_flags=sonnet_flags)

    # Calculate summary
    found = sum(1 for r in results if r.get("best_option"))
    total_local = sum(r["part"].get("local_price", 0) or 0 for r in results)
    total_landed = sum(
        r["landed_cost"]["total_landed_dop"]
        for r in results
        if r.get("landed_cost") and r["landed_cost"].get("total_landed_dop")
    )
    total_savings = total_local - total_landed if total_local > 0 and total_landed > 0 else 0

    summary = {
        "error": None,
        "summary": {
            "total_parts": len(results),
            "found": found,
            "not_found": len(results) - found,
            "total_local_dop": round(total_local, 2),
            "total_landed_dop": round(total_landed, 2),
            "total_savings_dop": round(total_savings, 2),
            "savings_pct": round(total_savings / total_local * 100, 1) if total_local > 0 else 0,
        },
        "sonnet_flags": sonnet_flags,
        "excel_path": args.output,
    }

    # Write full results JSON if requested (used by post-search correction flow)
    if args.results_output:
        full_output = {
            "vehicle": vehicle_info,
            "results": results,
        }
        Path(args.results_output).write_text(
            json.dumps(full_output, indent=2, ensure_ascii=False, default=str)
        )

    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    print()
    logger.info(f"Search complete: {found}/{len(results)} found, "
                f"savings RD${total_savings:,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
