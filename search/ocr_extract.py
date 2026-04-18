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
  "supplier_quotes": [
    {
      "supplier_name": "exact name as written",
      "total_dop": number,
      "delivery_days_min": number_or_null,
      "delivery_days_max": number_or_null
    }
  ],
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
- supplier_quotes: look for a "COTIZACIONES RECIBIDAS", "COTIZACIÓN RECIBIDA", or similar section listing multiple suppliers with totals. Extract EACH supplier as a separate entry with their total and delivery days if shown (e.g. "7/7 días" = min:7 max:7). If there is only one supplier total with no named section, put it as a single entry in supplier_quotes. If no supplier quotes at all, use an empty array [].
- supplier_total_dop: set to the MINIMUM total_dop from supplier_quotes (the cheapest quote). If supplier_quotes is empty, set to null."""


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
        def _call_and_parse() -> dict:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8096,
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
            raw = resp.content[0].text.strip()
            # Strip markdown fences (```json ... ``` or ``` ... ```)
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            # Strip any trailing content after the closing brace
            last_brace = raw.rfind("}")
            if last_brace != -1:
                raw = raw[: last_brace + 1]
            return json.loads(raw), raw

        text = ""
        try:
            result, text = _call_and_parse()
            return result
        except json.JSONDecodeError as e:
            logger.warning(f"OCR JSON parse failed (attempt 1): {e} — retrying")
        try:
            result, text = _call_and_parse()
            return result
        except json.JSONDecodeError as e:
            logger.error(f"OCR JSON parse failed (attempt 2): {e}")
            return {"error": f"JSON parse error: {e}", "raw_response": text[:1000]}

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

        # Process ALL pages — multi-page PDFs must not drop any page
        results = []
        all_supplier_quotes: list = []
        page_count = doc.page_count
        for page_num in range(page_count):
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

            # Accumulate supplier quotes from every page (COTIZACIONES may be on last page)
            if not result.get("error"):
                sq = result.get("supplier_quotes") or []
                if sq:
                    all_supplier_quotes.extend(sq)

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

        # Supplier quotes aggregated from all pages win over any single-page value
        if all_supplier_quotes:
            merged["supplier_quotes"] = all_supplier_quotes
            # supplier_total_dop = MIN quote (best price for the customer)
            totals = [q.get("total_dop") for q in all_supplier_quotes if q.get("total_dop")]
            if totals:
                merged["supplier_total_dop"] = min(totals)

        merged["_pages_processed"] = page_count
        merged["_pages_with_parts"] = len(results)

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
