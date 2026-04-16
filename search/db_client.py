"""
Supabase REST client for the parts-bot Python pipeline.
Used by run_search.py for correction overrides, result caching.
All calls are fire-and-forget with graceful fallbacks — never crash the search.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("parts-bot.db")

_TRANSLATION_CACHE_PATH = Path(__file__).parent.parent / "cache" / "translation_cache.json"


def _env():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_ANON_KEY", "")
    return url, key


def _headers(prefer_return=False):
    _, key = _env()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer_return:
        h["Prefer"] = "return=representation"
    return h


def _req(method: str, table: str, body=None, params: dict = None) -> list | dict | None:
    url_base, _ = _env()
    if not url_base:
        return None
    url = f"{url_base}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, headers=_headers(prefer_return=(method in ("POST", "PATCH"))), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        logger.warning(f"Supabase {method} {table} HTTP {e.code}: {e.read()[:200]}")
    except Exception as e:
        logger.warning(f"Supabase {method} {table} error: {e}")
    return None


# ─── Correction override ────────────────────────────────────────────────────

def get_correction_override(vehicle_make: str, vehicle_model: str, part_name_original: str) -> str | None:
    """Return the confirmed corrected English name for this part if we've seen it corrected before.
    Only returns overrides with confidence 'likely' (2+) or 'confirmed' (3+).
    """
    if not part_name_original:
        return None
    try:
        rows = _req("GET", "parts_corrections", params={
            "vehicle_make": f"ilike.{vehicle_make}",
            "vehicle_model": f"ilike.{vehicle_model}",
            "part_name_original": f"ilike.{part_name_original}",
            "correction_confidence": "in.(likely,confirmed)",
            "order": "times_seen.desc",
            "limit": "1",
            "select": "part_name_corrected,times_seen,correction_confidence",
        })
        if rows and isinstance(rows, list) and rows[0].get("part_name_corrected"):
            result = rows[0]["part_name_corrected"]
            logger.info(
                f"Correction override: '{part_name_original}' → '{result}' "
                f"({rows[0]['correction_confidence']}, {rows[0]['times_seen']}x)"
            )
            return result
    except Exception as e:
        logger.warning(f"get_correction_override error: {e}")
    return None


def upsert_correction(vehicle: dict, original_part: dict, corrected_name: str,
                      correction_message: str, part_index: int):
    """Insert or increment a correction record. Promotes auto_promoted at 3+ occurrences."""
    make = (vehicle.get("make") or "").strip()
    model = (vehicle.get("model") or "").strip()
    original = (original_part.get("name_english") or original_part.get("name_original") or "").strip()

    if not (make and model and original and corrected_name):
        return

    # Check if correction already exists
    existing = _req("GET", "parts_corrections", params={
        "vehicle_make": f"ilike.{make}",
        "vehicle_model": f"ilike.{model}",
        "part_name_original": f"ilike.{original}",
        "part_name_corrected": f"ilike.{corrected_name}",
        "select": "id,times_seen",
        "limit": "1",
    })

    if existing and isinstance(existing, list) and existing[0].get("id"):
        # Increment
        row = existing[0]
        new_seen = (row.get("times_seen") or 1) + 1
        confidence = "confirmed" if new_seen >= 3 else ("likely" if new_seen >= 2 else "suggested")
        auto_promoted = new_seen >= 3
        _req("PATCH", f"parts_corrections?id=eq.{row['id']}", body={
            "times_seen": new_seen,
            "correction_confidence": confidence,
            "auto_promoted": auto_promoted,
        })
        logger.info(f"Correction incremented: '{original}' → '{corrected_name}' ({new_seen}x, {confidence})")

        # Promote to translation cache when confirmed
        if auto_promoted and not row.get("auto_promoted"):
            _promote_to_translation_cache(original, corrected_name)
    else:
        # New correction
        _req("POST", "parts_corrections", body={
            "vehicle_year": vehicle.get("year"),
            "vehicle_make": make,
            "vehicle_model": model,
            "vin": vehicle.get("vin"),
            "part_index": part_index,
            "part_name_original": original,
            "part_name_corrected": corrected_name,
            "side_original": original_part.get("side"),
            "side_corrected": None,
            "position_original": original_part.get("position"),
            "position_corrected": None,
            "correction_message": correction_message,
            "times_seen": 1,
            "correction_confidence": "suggested",
            "auto_promoted": False,
        })
        logger.info(f"New correction logged: '{original}' → '{corrected_name}'")


def _promote_to_translation_cache(part_original: str, part_corrected: str):
    """Write a confirmed correction to the local translation_cache.json."""
    try:
        _TRANSLATION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cache = {}
        if _TRANSLATION_CACHE_PATH.exists():
            try:
                cache = json.loads(_TRANSLATION_CACHE_PATH.read_text())
            except Exception:
                pass
        key = part_original.lower().strip()
        if key not in cache:
            cache[key] = part_corrected
            _TRANSLATION_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
            logger.info(f"Auto-promoted to translation cache: '{part_original}' → '{part_corrected}'")
    except Exception as e:
        logger.warning(f"Failed to write translation cache: {e}")


# ─── Results cache ──────────────────────────────────────────────────────────

def get_cached_result(vehicle_make: str, vehicle_model: str, vehicle_year: int,
                      part_name_english: str) -> dict | None:
    """Return cached result if exists and is < 30 days old."""
    if not all([vehicle_make, vehicle_model, vehicle_year, part_name_english]):
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        rows = _req("GET", "parts_cache", params={
            "vehicle_make": f"ilike.{vehicle_make}",
            "vehicle_model": f"ilike.{vehicle_model}",
            "vehicle_year": f"eq.{vehicle_year}",
            "part_name_english": f"ilike.{part_name_english}",
            "last_verified_at": f"gte.{cutoff}",
            "select": "best_price_usd,best_source,best_url,oem_number,result_snapshot,verified_by_correction",
            "limit": "1",
        })
        if rows and isinstance(rows, list) and rows[0].get("best_price_usd"):
            row = rows[0]
            oem = row.get("oem_number") or ""
            verified = row.get("verified_by_correction", False)

            # Reject stale/bad cache entries: N/F or VARIOUS OEM with no human correction.
            # These were cached before RockAuto was working properly — force a fresh search.
            _BAD_OEM = {"N/F", "VARIOUS", "Unknown", ""}
            if not verified and oem.upper() in _BAD_OEM:
                logger.info(
                    f"Cache SKIP (bad OEM={oem!r}): {vehicle_year} {vehicle_make} "
                    f"{vehicle_model} / {part_name_english} — re-searching"
                )
                return None

            logger.info(
                f"Cache hit: {vehicle_year} {vehicle_make} {vehicle_model} / {part_name_english} "
                f"→ ${row['best_price_usd']} ({row['best_source']}, OEM={oem!r})"
            )
            snapshot = row.get("result_snapshot") or {}
            return {
                "from_cache": True,
                "verified_by_correction": verified,
                "best_option": snapshot.get("best_option") or {
                    "price": float(row["best_price_usd"]),
                    "shipping": 0,
                    "total_price": float(row["best_price_usd"]),
                    "part_number": oem,
                    "source": row.get("best_source", "Cache"),
                    "url": row.get("best_url", ""),
                    "condition": "Unknown",
                },
                "landed_cost": snapshot.get("landed_cost"),
                "rockauto": None,
                "ebay": None,
                "error": None,
            }
    except Exception as e:
        logger.warning(f"get_cached_result error: {e}")
    return None


def upsert_cached_result(vehicle_make: str, vehicle_model: str, vehicle_year: int,
                         part_name_english: str, result: dict,
                         verified_by_correction: bool = False):
    """Cache a search result. Upserts on (make, model, year, part_name_english)."""
    if not all([vehicle_make, vehicle_model, vehicle_year, part_name_english]):
        return
    best = result.get("best_option") or {}
    if not best.get("price"):
        return
    try:
        _req("POST", "parts_cache", body={
            "vehicle_make": vehicle_make.strip(),
            "vehicle_model": vehicle_model.strip(),
            "vehicle_year": vehicle_year,
            "part_name_english": part_name_english.lower().strip(),
            "oem_number": best.get("part_number") or None,
            "best_source": best.get("source"),
            "best_price_usd": round(float(best["price"]), 2),
            "best_url": best.get("url"),
            "result_snapshot": {
                "best_option": best,
                "landed_cost": result.get("landed_cost"),
            },
            "verified_by_correction": verified_by_correction,
            "last_verified_at": datetime.now(timezone.utc).isoformat(),
        }, params={
            "on_conflict": "vehicle_make,vehicle_model,vehicle_year,part_name_english",
        })
        # Use upsert via headers
    except Exception as e:
        logger.warning(f"upsert_cached_result error: {e}")


def upsert_cached_result_safe(vehicle_make: str, vehicle_model: str, vehicle_year: int,
                               part_name_english: str, result: dict,
                               verified_by_correction: bool = False):
    """Upsert using PATCH if exists, POST if new."""
    if not all([vehicle_make, vehicle_model, vehicle_year, part_name_english]):
        return
    best = result.get("best_option") or {}
    if not best.get("price"):
        return
    try:
        payload = {
            "vehicle_make": vehicle_make.strip(),
            "vehicle_model": vehicle_model.strip(),
            "vehicle_year": int(vehicle_year),
            "part_name_english": part_name_english.lower().strip(),
            "oem_number": best.get("part_number") or None,
            "best_source": best.get("source"),
            "best_price_usd": round(float(best["price"]), 2),
            "best_url": best.get("url"),
            "result_snapshot": {
                "best_option": best,
                "landed_cost": result.get("landed_cost"),
            },
            "verified_by_correction": verified_by_correction,
            "last_verified_at": datetime.now(timezone.utc).isoformat(),
        }
        # Try POST with upsert header
        url_base, key = _env()
        if not url_base:
            return
        url = f"{url_base}/rest/v1/parts_cache"
        headers = _headers(prefer_return=False)
        headers["Prefer"] = "resolution=merge-duplicates"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
        logger.debug(f"Cached result for {vehicle_year} {vehicle_make} {part_name_english}")
    except Exception as e:
        logger.warning(f"upsert_cached_result_safe error: {e}")
