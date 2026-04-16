#!/usr/bin/env python3
"""
Parse a text-based parts list from WhatsApp message.

Handles flexible formats like:
  "sonata 2018
   bonete
   farol derecho
   bumper delantero"

  "VIN: 5NPE34AF5JH123456
   guardafango izq
   catre de abajo der"

  "tucson 2018 bonete, farol der, guardafango izq"

Outputs same JSON structure as OCR extraction.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from search.dictionary import translate_part
from search.vin_decode import decode_vin

# Common makes — used to detect vehicle line from text
KNOWN_MAKES = {
    "hyundai": "Hyundai", "kia": "Kia", "toyota": "Toyota", "honda": "Honda",
    "nissan": "Nissan", "mazda": "Mazda", "mitsubishi": "Mitsubishi",
    "chevrolet": "Chevrolet", "chevy": "Chevrolet", "ford": "Ford",
    "dodge": "Dodge", "jeep": "Jeep", "ram": "Ram",
    "mercedes": "Mercedes-Benz", "mercedes-benz": "Mercedes-Benz",
    "bmw": "BMW", "audi": "Audi", "volkswagen": "Volkswagen", "vw": "Volkswagen",
    "subaru": "Subaru", "lexus": "Lexus", "acura": "Acura", "infiniti": "Infiniti",
    "suzuki": "Suzuki", "volvo": "Volvo", "porsche": "Porsche",
    "buick": "Buick", "cadillac": "Cadillac", "gmc": "GMC",
    "chrysler": "Chrysler", "lincoln": "Lincoln",
}

# Common models — helps detect model even without make
KNOWN_MODELS = {
    "sonata": "Hyundai", "tucson": "Hyundai", "elantra": "Hyundai",
    "santa fe": "Hyundai", "accent": "Hyundai", "venue": "Hyundai",
    "kona": "Hyundai", "palisade": "Hyundai", "ioniq": "Hyundai",
    "sportage": "Kia", "sorento": "Kia", "forte": "Kia", "k5": "Kia",
    "optima": "Kia", "soul": "Kia", "seltos": "Kia", "telluride": "Kia",
    "rio": "Kia", "carnival": "Kia", "stinger": "Kia",
    "corolla": "Toyota", "camry": "Toyota", "rav4": "Toyota",
    "highlander": "Toyota", "tacoma": "Toyota", "4runner": "Toyota",
    "tundra": "Toyota", "prius": "Toyota", "yaris": "Toyota",
    "civic": "Honda", "accord": "Honda", "cr-v": "Honda", "crv": "Honda",
    "hr-v": "Honda", "pilot": "Honda", "odyssey": "Honda", "fit": "Honda",
    "sentra": "Nissan", "altima": "Nissan", "maxima": "Nissan",
    "rogue": "Nissan", "pathfinder": "Nissan", "frontier": "Nissan",
    "murano": "Nissan", "versa": "Nissan", "kicks": "Nissan",
    "cx-5": "Mazda", "cx5": "Mazda", "mazda3": "Mazda", "mazda6": "Mazda",
    "cx-9": "Mazda", "cx-30": "Mazda",
    "outlander": "Mitsubishi", "eclipse": "Mitsubishi", "lancer": "Mitsubishi",
    "wrangler": "Jeep", "cherokee": "Jeep", "grand cherokee": "Jeep",
    "compass": "Jeep", "renegade": "Jeep", "gladiator": "Jeep",
    "charger": "Dodge", "challenger": "Dodge", "durango": "Dodge",
    "journey": "Dodge",
    "explorer": "Ford", "f-150": "Ford", "f150": "Ford", "escape": "Ford",
    "fusion": "Ford", "mustang": "Ford", "ranger": "Ford", "edge": "Ford",
    "equinox": "Chevrolet", "malibu": "Chevrolet", "silverado": "Chevrolet",
    "traverse": "Chevrolet", "cruze": "Chevrolet", "trax": "Chevrolet",
    "gle": "Mercedes-Benz", "glc": "Mercedes-Benz", "c-class": "Mercedes-Benz",
    "e-class": "Mercedes-Benz", "gle350": "Mercedes-Benz",
}


def parse_text_list(text: str) -> dict:
    """Parse a text message into vehicle info + parts list.

    Returns same structure as OCR extraction:
    {vin, vehicle: {year, make, model}, parts: [...]}
    """
    lines = [l.strip() for l in text.replace(",", "\n").splitlines() if l.strip()]

    vin = None
    year = None
    make = None
    model = None
    part_lines = []

    for line in lines:
        lower = line.lower().strip()

        # Check for VIN
        vin_match = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', line.upper())
        if vin_match and not vin:
            vin = vin_match.group(1)
            # Remove VIN from line for further parsing
            remaining = line[:vin_match.start()] + line[vin_match.end():]
            remaining = re.sub(r'(?i)vin\s*:?\s*', '', remaining).strip()
            if remaining:
                lines.append(remaining)  # re-process the rest
            continue

        # Check for year (4 digits, 1990-2030)
        year_match = re.search(r'\b(19\d{2}|20[0-3]\d)\b', line)
        found_year = int(year_match.group(1)) if year_match else None

        # Check for make
        found_make = None
        for key, val in KNOWN_MAKES.items():
            if re.search(r'\b' + re.escape(key) + r'\b', lower):
                found_make = val
                break

        # Check for model (try multi-word first like "santa fe", "grand cherokee")
        found_model = None
        for key, val in sorted(KNOWN_MODELS.items(), key=lambda x: -len(x[0])):
            if re.search(r'\b' + re.escape(key) + r'\b', lower):
                found_model = key.title()
                if not found_make:
                    found_make = val
                break

        # If this line has vehicle info, extract it
        if found_year or found_make or found_model:
            if found_year and not year:
                year = found_year
            if found_make and not make:
                make = found_make
            if found_model and not model:
                model = found_model

            # Check if line ALSO has parts after the vehicle info
            # e.g. "tucson 2018 bonete, farol" — bonete and farol are parts
            # Only if we found both year+model on same line and there's more text
            remainder = lower
            if found_year:
                remainder = remainder.replace(str(found_year), "")
            if found_model:
                remainder = remainder.replace(found_model.lower(), "")
            if found_make:
                for key in KNOWN_MAKES:
                    remainder = remainder.replace(key, "")
            remainder = remainder.strip(" ,-/")
            if remainder and len(remainder) > 2:
                part_lines.append(remainder)
        else:
            # This line is a part
            if lower and len(lower) > 1:
                part_lines.append(line)

    # Build parts list using dictionary translation
    parts = []
    for i, raw in enumerate(part_lines, 1):
        translated = translate_part(raw)
        parts.append({
            "index": i,
            "name_original": raw,
            "name_dr": translated["name_dr"],
            "name_english": translated["name_english"],
            "side": translated["side"],
            "position": translated["position"],
            "local_price": None,
            "local_currency": "DOP",
            "quantity": 1,
        })

    return {
        "vin": vin,
        "vehicle": {
            "year": year,
            "make": make,
            "model": model,
            "trim": None,
        },
        "parts": parts,
        "supplier_name": None,
        "document_date": None,
        "extraction_confidence": "high" if (year and model and parts) else "medium",
    }


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True, help="Raw text message")
    args = parser.parse_args()

    load_env()

    result = parse_text_list(args.text)

    # If we got a VIN, decode it to fill in missing vehicle info
    if result["vin"] and len(result["vin"]) == 17:
        decoded = await decode_vin(result["vin"])
        if not decoded.get("error"):
            if not result["vehicle"]["year"]:
                result["vehicle"]["year"] = decoded["year"]
            if not result["vehicle"]["make"]:
                result["vehicle"]["make"] = decoded["make"]
            if not result["vehicle"]["model"]:
                result["vehicle"]["model"] = decoded["model"]

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    asyncio.run(main())
