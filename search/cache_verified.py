#!/usr/bin/env python3
"""
Mark all results from a session as verified in parts_cache.
Called when user says "listo" (no corrections needed).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-json", required=True)
    parser.add_argument("--vehicle-make", required=True)
    parser.add_argument("--vehicle-model", required=True)
    parser.add_argument("--vehicle-year", required=True, type=int)
    args = parser.parse_args()

    load_env()

    from search.db_client import upsert_cached_result_safe

    data = json.loads(Path(args.results_json).read_text())
    results = data.get("results", [])

    count = 0
    for r in results:
        part = r.get("part", {})
        part_name = part.get("name_english", "")
        if r.get("best_option") and part_name:
            upsert_cached_result_safe(
                args.vehicle_make,
                args.vehicle_model,
                args.vehicle_year,
                part_name,
                r,
                verified_by_correction=False,
            )
            count += 1

    print(json.dumps({"cached": count}))


if __name__ == "__main__":
    main()
