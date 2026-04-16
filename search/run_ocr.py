#!/usr/bin/env python3
"""
OCR bridge script — called by server.js to extract parts from an image/PDF.
Outputs JSON to stdout.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add parent dir to path so search package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from search.ocr_extract import extract_from_image, extract_from_pdf
from search.dictionary import translate_part


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
    parser.add_argument("--input", required=True, help="Path to image or PDF")
    args = parser.parse_args()

    load_env()

    input_path = args.input
    ext = Path(input_path).suffix.lower()

    if ext == ".pdf":
        result = await extract_from_pdf(input_path)
    else:
        result = await extract_from_image(input_path)

    if result.get("error"):
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
        sys.exit(1)

    # Enrich parts with dictionary translations
    # (OCR may have translated, but let's ensure consistency with our dictionary)
    parts = result.get("parts", [])
    for part in parts:
        name_original = part.get("name_original", "") or part.get("name_dr", "")
        if name_original:
            translated = translate_part(name_original)
            # Only override if our dictionary has a match
            if translated["name_english"] != translated["name_dr"]:
                part["name_english"] = translated["name_english"]
            part["side"] = part.get("side") or translated["side"]
            part["position"] = part.get("position") or translated["position"]

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    asyncio.run(main())
