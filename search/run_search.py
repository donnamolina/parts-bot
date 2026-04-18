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
    supplier_quotes = extraction.get("supplier_quotes") or []

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
            # NHTSA returns uppercase makes ("TOYOTA", "KIA") — normalize to title case.
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

        # Dedup on DR Spanish original name — NOT the English translation.
        # English translations can collide (GUARDALODO + GUARDAFANGO -> same English),
        # falsely inflating quantities. Original Spanish is the faithful key.
        dedup_key = (
            part["name_original"].lower().strip(),
            (part["side"] or "").lower(),
            (part["position"] or "").lower(),
        )
        if dedup_key in seen_parts:
            existing_qty = seen_parts[dedup_key]["quantity"]
            new_qty = existing_qty + part["quantity"]
            _pname = part["name_original"]
            _pqty = part["quantity"]
            logger.warning(
                f"OCR duplicate detected: '{_pname}' appeared twice "
                f"(qty {existing_qty} + {_pqty} = {new_qty}). "
                "Merging — verify source PDF if totals look wrong."
            )
            seen_parts[dedup_key]["quantity"] = new_qty
            seen_parts[dedup_key]["local_price"] += part["local_price"]
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
        # Invalidate legacy cache rows that have RockAuto as source (RockAuto
        # was fully removed in v11 — re-search via eBay instead).
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
            # Bug 16 fix: verify whenever we have a usable best_option (title+price),
            # regardless of OEM source (7zap / name_fallback / Cache).
            # The riskiest paths are the non-7zap ones where name-based search is noisier.
            _best = _r.get("best_option") or {}
            _part = _r.get("part", {}) or {}
            if _best.get("price") and _best.get("title"):
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
                    oem_description=_r.get("oem_description", ""),
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

    # Cross-row OEM uniqueness check (Bug 34/35 guardrail)
    # If the same OEM appears on 2+ rows for different parts, flag both rows.
    # This surfaces catalog ambiguity (e.g. 7zap has a combined radiator+fan assembly)
    # rather than silently sending a quoter the same part number twice.
    _oem_to_rows: dict[str, list[tuple[int, str]]] = {}
    for _i, _r in enumerate(results):
        _oem = (_r.get("best_option") or {}).get("part_number", "") or ""
        if _oem and _oem not in ("N/F", ""):
            _name = (_r.get("part") or {}).get("name_original", "")
            _oem_to_rows.setdefault(_oem, []).append((_i, _name))
    for _oem, _rows in _oem_to_rows.items():
        if len(_rows) > 1:
            _names = {n for _, n in _rows}
            if len(_names) > 1:  # Same OEM, different parts — flag it
                for _i, _ in _rows:
                    _other = [str(ri + 1) for ri, _ in _rows if ri != _i]
                    results[_i]["duplicate_oem_note"] = (
                        f"⚠️ OEM duplicado con fila #{', '.join(_other)} — verificar cuál es correcto"
                    )
                    logger.warning(f"Duplicate OEM {_oem} on rows {[ri+1 for ri,_ in _rows]}: {list(_names)}")

    # Generate Excel
    logger.info(f"Generating Excel: {args.output}")
    generate_excel(results, vehicle_info, args.output,
                   supplier_total_dop=supplier_total_dop,
                   supplier_quotes=supplier_quotes,
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
