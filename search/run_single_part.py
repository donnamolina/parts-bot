#!/usr/bin/env python3
"""
Re-search a single corrected part.
Called by server.js during the post-delivery review flow.
Outputs the result dict as JSON to stdout.
"""

import argparse
import asyncio
import json
import os
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from search.engine import search_all_parts

logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler(sys.stderr)])
logger = logging.getLogger("parts-bot.run_single_part")


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
    parser.add_argument("--vehicle-json", required=True, help="JSON string of vehicle info")
    parser.add_argument("--part-json", required=True, help="JSON string of part dict")
    args = parser.parse_args()

    load_env()

    vehicle_info = json.loads(args.vehicle_json)
    part = json.loads(args.part_json)

    # Use search_all_parts with a single part for full pipeline (7zap + eBay)
    results = await search_all_parts([part], vehicle_info)
    result = results[0] if results else {"part": part, "best_option": None, "landed_cost": None, "error": "no results"}

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()


if __name__ == "__main__":
    asyncio.run(main())
