"""
VIN Decoder using NHTSA vPIC API (free, no key needed).
Results are cached permanently — VIN decodes never change.
"""

import json
import os
import aiohttp
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent / "cache"
VIN_CACHE_PATH = CACHE_DIR / "vehicles.json"


def _load_cache() -> dict:
    if VIN_CACHE_PATH.exists():
        try:
            return json.loads(VIN_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VIN_CACHE_PATH.write_text(json.dumps(cache, indent=2))


async def decode_vin(vin: str) -> dict:
    """Decode VIN using NHTSA vPIC API.

    Returns dict with: vin, year, make, model, trim, engine, body_class, error
    Caches permanently (VINs never change).
    """
    vin = vin.strip().upper()

    # Check cache
    cache = _load_cache()
    if vin in cache:
        return cache[vin]

    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return {"vin": vin, "error": f"NHTSA API returned {resp.status}"}

                data = await resp.json()
                results = data.get("Results", [{}])[0]

                # Check for decode errors
                error_code = results.get("ErrorCode", "0")
                if error_code and error_code != "0" and "0" not in error_code.split(","):
                    error_text = results.get("ErrorText", "Unknown error")
                    return {"vin": vin, "error": f"VIN decode error: {error_text}"}

                vehicle = {
                    "vin": vin,
                    "year": int(results.get("ModelYear", 0)) or None,
                    "make": results.get("Make", "").strip() or None,
                    "model": results.get("Model", "").strip() or None,
                    "trim": results.get("Trim", "").strip() or None,
                    "engine": _build_engine_desc(results),
                    "body_class": results.get("BodyClass", "").strip() or None,
                    "drive_type": results.get("DriveType", "").strip() or None,
                    "displacement_l": results.get("DisplacementL", "").strip() or None,
                    "cylinders": results.get("EngineCylinders", "").strip() or None,
                    "fuel_type": results.get("FuelTypePrimary", "").strip() or None,
                    "plant_country": results.get("PlantCountry", "").strip() or None,
                    "plant_city": results.get("PlantCity", "").strip() or None,
                    "error": None,
                }

                # Cache permanently
                cache[vin] = vehicle
                _save_cache(cache)

                return vehicle

    except aiohttp.ClientError as e:
        return {"vin": vin, "error": f"Network error: {e}"}
    except Exception as e:
        return {"vin": vin, "error": f"VIN decode failed: {e}"}


def _build_engine_desc(results: dict) -> str:
    """Build a human-readable engine description from NHTSA fields."""
    parts = []
    displacement = results.get("DisplacementL", "")
    cylinders = results.get("EngineCylinders", "")
    fuel = results.get("FuelTypePrimary", "")

    if displacement:
        parts.append(f"{displacement}L")
    if cylinders:
        parts.append(f"{cylinders}cyl")
    if fuel and fuel.lower() not in ("gasoline", ""):
        parts.append(fuel)

    return " ".join(parts) if parts else "Unknown"
