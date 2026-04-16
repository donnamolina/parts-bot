#!/usr/bin/env python3
"""
Manual test script for 7zap OEM lookup.

Usage:
    python test_7zap.py --vin WP1AA2A5XHLB02597 --part "front bumper cover"
    python test_7zap.py --vin WP1AA2A5XHLB02597 --part "left headlight"
    python test_7zap.py --fixture porsche_macan_2017  # runs a standard part suite

Requires: SEVENZAP_COOKIE_* and SEVENZAP_USER_AGENT set in .env
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env before importing the module
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

_load_env()

sys.path.insert(0, str(Path(__file__).parent))
from search.oem_lookup_7zap import lookup_oem_by_vin, OemLookupResult

CONFIDENCE_ICON = {
    "green": "🟢",
    "yellow": "🟡",
    "red": "🔴",
}

STANDARD_PARTS = [
    "front bumper cover",
    "left headlight",
    "right headlight",
    "hood panel",
    "left fender panel",
    "grille assembly",
    "radiator",
    "front end assembly",
]


async def run_single(vin: str, part: str, verbose: bool = False):
    print(f"\n  VIN  : {vin}")
    print(f"  Part : {part}")
    print(f"  ─────────────────────────────────")
    result = await lookup_oem_by_vin(vin, part)
    icon = CONFIDENCE_ICON.get(result.confidence, "❓")
    if result.oem_number:
        print(f"  {icon} {result.oem_number} — {result.part_name}")
        print(f"     source: {result.source}")
    else:
        print(f"  {icon} NO RESULT — {result.error}")
    if verbose and result.candidates:
        print(f"  Candidates:")
        for c in result.candidates[:3]:
            print(f"    {c['score']:.0f}  {c['oem_number']}  {c['part_name']}  (side={c['side']})")
    return result


async def run_fixture(vin: str, label: str, verbose: bool = False):
    print(f"\n{'═' * 55}")
    print(f" Fixture: {label}  ({vin})")
    print(f"{'═' * 55}")
    found = 0
    for part in STANDARD_PARTS:
        r = await run_single(vin, part, verbose=verbose)
        if r.oem_number:
            found += 1
    print(f"\n  Found: {found}/{len(STANDARD_PARTS)} parts")


async def main():
    parser = argparse.ArgumentParser(description="Test 7zap OEM lookup")
    parser.add_argument("--vin", help="Vehicle VIN (17 chars)")
    parser.add_argument("--part", help="English part name")
    parser.add_argument("--fixture", help="Named fixture from test_vins.json")
    parser.add_argument("--all-fixtures", action="store_true", help="Run all fixtures")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show candidate list")
    args = parser.parse_args()

    vins_path = Path(__file__).parent / "test_vins.json"
    vins = json.loads(vins_path.read_text()) if vins_path.exists() else {}

    if args.all_fixtures:
        for label, vin in vins.items():
            await run_fixture(vin, label, verbose=args.verbose)
        return

    if args.fixture:
        if args.fixture not in vins:
            print(f"Unknown fixture '{args.fixture}'. Available: {list(vins.keys())}")
            sys.exit(1)
        await run_fixture(vins[args.fixture], args.fixture, verbose=args.verbose)
        return

    if not args.vin or not args.part:
        parser.print_help()
        sys.exit(1)

    await run_single(args.vin, args.part, verbose=args.verbose)


if __name__ == "__main__":
    asyncio.run(main())
