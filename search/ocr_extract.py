"""
OCR extraction using Claude Sonnet API.
Extracts VIN, parts list, and local prices from supplier quote photos/PDFs.
"""

import base64
import json
import os
import logging
from pathlib import Path

import anthropic

logger = logging.getLogger("parts-bot.ocr")

_TRANSLATION_CACHE_PATH = Path(__file__).parent.parent / "cache" / "translation_cache.json"

def _load_translation_cache() -> dict:
    try:
        if _TRANSLATION_CACHE_PATH.exists():
            return json.loads(_TRANSLATION_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}

def _save_translation_cache(cache: dict):
    try:
        _TRANSLATION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TRANSLATION_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Could not save translation cache: {e}")

EXTRACTION_PROMPT = """You are extracting auto parts data from a Dominican Republic supplier quote or damage estimate.

IMPORTANT CONTEXT:
- Documents are in Dominican Spanish — use DR automotive terminology
- VIN is always present somewhere on the document (17 alphanumeric characters)
- Parts may use DR slang: bonete=hood, farol=headlight, guardafango=fender, catre=control arm, piña=wheel hub, violeta=tail light/sway bar link, cran=oil pan, stop=tail light, pantalla=headlight, bumper=bumper, flear=fender flare, guía de bumper=bumper bracket
- Local prices are in Dominican Pesos (RD$ or DOP)
- Side indicators: derecho/der/D = right, izquierdo/izq/I = left, delantero/del = front, trasero/tras = rear

Extract EXACTLY this JSON structure (no markdown, no backticks, just raw JSON):
{
  "vin": "17-char VIN string",
  "vehicle": {
    "year": number,
    "make": "string",
    "model": "string",
    "trim": "string or null"
  },
  "parts": [
    {
      "index": 1,
      "name_original": "exact text as written on document",
      "name_dr": "standardized DR Spanish name",
      "name_english": "English translation",
      "side": "left|right|null",
      "position": "front|rear|null",
      "local_price": number_or_null,
      "local_currency": "DOP",
      "quantity": 1
    }
  ],
  "supplier_name": "supplier name if visible, else null",
  "document_date": "date if visible, else null",
  "supplier_total_dop": number_or_null,
  "extraction_confidence": "high|medium|low"
}

RULES:
- If a part says "der" or "D" that means RIGHT (passenger side)
- If a part says "izq" or "I" that means LEFT (driver side)
- If a part says "del" that means FRONT, "tras" means REAR
- Extract ALL parts listed, even if you're unsure of the translation
- For uncertain translations, set name_english to your best guess and add "?" suffix
- If price has comma as thousands separator (12,500), parse as 12500
- If you cannot read something clearly, include it with extraction_confidence: "low"
- NEVER skip a part — include everything, even if confidence is low
- supplier_total_dop: the grand total at the bottom of the document (what the DR supplier is charging for ALL parts combined). Look for: "TOTAL", "MONTO TOTAL", "TOTAL A PAGAR", "COTIZACIONES RECIBIDAS", "COTIZACIÓN RECIBIDA", or any final dollar/peso amount at the bottom of the parts list. The "COTIZACIONES RECIBIDAS" section often contains a per-supplier total — extract that total number. If no document total exists, set to null."""


async def extract_from_image(image_path: str) -> dict:
    """Extract parts data from an image file using Claude Sonnet.

    Args:
        image_path: Path to the image file (jpg, png, webp)

    Returns:
        Parsed extraction dict, or dict with 'error' key on failure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    path = Path(image_path)
    if not path.exists():
        return {"error": f"Image not found: {image_path}"}

    # Read and encode image
    image_data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")

    # Determine media type
    suffix = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif",
    }
    media_type = media_types.get(suffix, "image/jpeg")

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                    },
                ],
            }],
        )

        # Parse JSON from response
        text = response.content[0].text.strip()
        # Handle potential markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        result = json.loads(text)
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OCR response as JSON: {e}")
        return {"error": f"JSON parse error: {e}", "raw_response": text[:500]}
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return {"error": f"API error: {e}"}
    except Exception as e:
        logger.error(f"OCR extraction failed: {e}")
        return {"error": str(e)}


async def extract_from_pdf(pdf_path: str) -> dict:
    """Extract parts data from a PDF file.
    Converts each page to an image, then extracts from the first page with content.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {"error": "PyMuPDF not installed. Run: pip install PyMuPDF"}

    path = Path(pdf_path)
    if not path.exists():
        return {"error": f"PDF not found: {pdf_path}"}

    try:
        doc = fitz.open(str(path))
        if doc.page_count == 0:
            return {"error": "PDF has no pages"}

        # Convert first 2 pages to images (most quotes are 1-2 pages)
        results = []
        for page_num in range(min(doc.page_count, 2)):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=200)

            # Save as temp PNG
            temp_path = Path(pdf_path).parent / f"_temp_page_{page_num}.png"
            pix.save(str(temp_path))

            result = await extract_from_image(str(temp_path))

            # Clean up temp file
            try:
                temp_path.unlink()
            except OSError:
                pass

            if not result.get("error") and result.get("parts"):
                results.append(result)

        doc.close()

        if not results:
            return {"error": "Could not extract parts from any PDF page"}

        # Merge results from multiple pages (parts from page 2 extend page 1)
        merged = results[0]
        for extra in results[1:]:
            if extra.get("parts"):
                start_idx = len(merged.get("parts", []))
                for p in extra["parts"]:
                    p["index"] = start_idx + p.get("index", 1)
                merged.setdefault("parts", []).extend(extra["parts"])
            # Use VIN from first page that has one
            if not merged.get("vin") and extra.get("vin"):
                merged["vin"] = extra["vin"]
                merged["vehicle"] = extra.get("vehicle")

        return merged

    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return {"error": str(e)}


async def translate_unknown_part(part_name_dr: str, vehicle_context: str) -> str:
    """Fallback translation using Claude Sonnet for terms NOT in the dictionary.
    Results are cached permanently in cache/translation_cache.json.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return part_name_dr

    cache_key = part_name_dr.lower().strip()
    cache = _load_translation_cache()
    if cache_key in cache:
        logger.debug(f"Translation cache hit: '{part_name_dr}' → '{cache[cache_key]}'")
        return cache[cache_key]

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"What is the English name for the Dominican Republic auto part "
                    f"'{part_name_dr}'? Vehicle: {vehicle_context}. "
                    f"Reply with ONLY the English part name, nothing else."
                ),
            }],
        )
        result = response.content[0].text.strip()
        cache[cache_key] = result
        _save_translation_cache(cache)
        logger.info(f"Translated (Sonnet, cached): '{part_name_dr}' → '{result}'")
        return result
    except Exception as e:
        logger.warning(f"Fallback translation failed for '{part_name_dr}': {e}")
        return part_name_dr
