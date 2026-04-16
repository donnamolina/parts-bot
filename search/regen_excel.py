#!/usr/bin/env python3
"""
Regenerate Excel from stored results JSON without re-searching.
Called by server.js after a correction is applied.
"""

import argparse
import json
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from search.excel_builder import generate_excel

logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler(sys.stderr)])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-json", required=True, help="Path to stored results JSON")
    parser.add_argument("--output", required=True, help="Path for output Excel")
    parser.add_argument("--supplier-total", type=float, default=None, help="Supplier total DOP")
    args = parser.parse_args()

    data = json.loads(Path(args.results_json).read_text())
    vehicle_info = data["vehicle"]
    results = data["results"]

    generate_excel(results, vehicle_info, args.output,
                   supplier_total_dop=args.supplier_total,
                   sonnet_flags=[])

    print(json.dumps({"excel_path": args.output, "error": None}))


if __name__ == "__main__":
    main()
