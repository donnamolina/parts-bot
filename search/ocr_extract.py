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

_SONNET_MODEL = os.environ.get("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-6")

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
            model=_SONNET_MODEL,
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


# ── Bug 5: VIN validation ─────────────────────────────────────────────────────
import re as _re_vin

_VIN_RE = _re_vin.compile(r'^[A-HJ-NPR-Z0-9]{17}$')

def validate_vin(vin: str) -> bool:
    """VIN must be 17 chars, alphanumeric, no I/O/Q."""
    if not vin:
        return False
    v = vin.strip().upper()
    return bool(_VIN_RE.match(v))


# ── Bug 5: PDF metadata extraction (pass 1) ───────────────────────────────────
# Regex patterns for the supplier-quote / damage-estimate header.
_VEHICLE_LINE_RE = _re_vin.compile(r'Veh[ií]culo[:\s]+(.+?)(?:\n|$)', _re_vin.IGNORECASE)
_CHASIS_RE = _re_vin.compile(r'Chasis\s*No\.?[:\s]+([A-HJ-NPR-Z0-9]{11,17})', _re_vin.IGNORECASE)
_VIN_RE_LINE = _re_vin.compile(r'VIN[:\s]+([A-HJ-NPR-Z0-9]{11,17})', _re_vin.IGNORECASE)
_RECLAMACION_RE = _re_vin.compile(r'Reclamaci[oó]n\s*No\.?[:\s]+(\S+)', _re_vin.IGNORECASE)
_VEHICLE_YMM_RE = _re_vin.compile(r'(\d{4})\s+(\w+)\s+(.+)')


def _extract_pdf_metadata_regex(page_text: str) -> dict:
    """Pass 1: regex-based metadata extraction from page 1 text.
    Returns dict with possible keys: vin, vehicle (year/make/model), claim_number.
    Missing keys indicate regex miss."""
    out: dict = {}

    # VIN — prefer Chasis No. over VIN: if both present
    vin = None
    m = _CHASIS_RE.search(page_text) or _VIN_RE_LINE.search(page_text)
    if m:
        vin_candidate = m.group(1).strip().upper()
        if validate_vin(vin_candidate):
            vin = vin_candidate
    if vin:
        out["vin"] = vin

    # Claim number
    m = _RECLAMACION_RE.search(page_text)
    if m:
        out["claim_number"] = m.group(1).strip()

    # Vehicle line → year/make/model
    m = _VEHICLE_LINE_RE.search(page_text)
    if m:
        raw = m.group(1).strip()
        ymm = _VEHICLE_YMM_RE.match(raw)
        if ymm:
            out["vehicle"] = {
                "year": int(ymm.group(1)),
                "make": ymm.group(2).strip(),
                "model": ymm.group(3).strip(),
                "trim": None,
            }
        else:
            out["vehicle_raw"] = raw

    return out


_METADATA_VISION_PROMPT = """You are looking at the first page of a Dominican Republic auto supplier quote or damage-estimate PDF.

Extract ONLY the vehicle metadata (NOT the parts list). Return raw JSON, no markdown:
{
  "vin": "17-char VIN or null",
  "vehicle": {
    "year": number_or_null,
    "make": "string_or_null",
    "model": "string_or_null",
    "trim": "string_or_null"
  },
  "claim_number": "string_or_null"
}

The VIN is 17 characters, alphanumeric, no letters I/O/Q. It may be labeled "Chasis No.", "VIN", or similar.
The vehicle year/make/model is usually near "Vehículo:" or in the header.
The claim number may be labeled "Reclamación No.", "Claim No.", or "No. Reclamación".
If a field is missing, use null. Do NOT guess."""


async def _extract_pdf_metadata_vision(image_path: str) -> dict:
    """Pass 1 fallback: Haiku vision call on page-1 image for metadata only."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {}

    try:
        path = Path(image_path)
        image_data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": image_data,
                    }},
                    {"type": "text", "text": _METADATA_VISION_PROMPT},
                ],
            }],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        data = json.loads(text)
        # Validate VIN if present
        if data.get("vin") and not validate_vin(data["vin"]):
            data["vin"] = None
        return data
    except Exception as e:
        logger.warning(f"Haiku metadata vision fallback failed: {e}")
        return {}


async def extract_from_pdf(pdf_path: str) -> dict:
    """Extract parts data from a PDF file.

    Bug 5 two-pass:
      Pass 1 — metadata extraction from page 1 text (pdfplumber) with regex,
               then Haiku vision fallback if regex missed VIN or vehicle.
      Pass 2 — existing parts extraction (Sonnet vision per page, unchanged).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {"error": "PyMuPDF not installed. Run: pip install PyMuPDF"}

    path = Path(pdf_path)
    if not path.exists():
        return {"error": f"PDF not found: {pdf_path}"}

    # ── Pass 1: metadata extraction ──────────────────────────────────────────
    metadata: dict = {}
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf_doc:
            if pdf_doc.pages:
                page1_text = pdf_doc.pages[0].extract_text() or ""
        metadata = _extract_pdf_metadata_regex(page1_text)
        logger.info(
            f"PDF metadata pass 1 (regex): vin={metadata.get('vin')} "
            f"vehicle={metadata.get('vehicle')} claim={metadata.get('claim_number')}"
        )
    except ImportError:
        logger.warning("pdfplumber not installed — skipping regex metadata pass")
    except Exception as e:
        logger.warning(f"pdfplumber metadata pass failed: {e}")

    # ── Pass 2: per-page parts extraction via Sonnet vision ──────────────────
    try:
        doc = fitz.open(str(path))
        if doc.page_count == 0:
            return {"error": "PDF has no pages"}

        # Convert first 2 pages to images (most quotes are 1-2 pages)
        results = []
        page1_image_path = None
        for page_num in range(min(doc.page_count, 2)):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=200)

            # Save as temp PNG
            temp_path = Path(pdf_path).parent / f"_temp_page_{page_num}.png"
            pix.save(str(temp_path))
            if page_num == 0:
                page1_image_path = str(temp_path)

            result = await extract_from_image(str(temp_path))

            # Clean up temp file AFTER we may have used page1_image_path for fallback
            if page_num != 0:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

            if not result.get("error") and result.get("parts"):
                results.append(result)

        doc.close()

        # Haiku fallback for metadata if regex missed it — uses page 1 image
        if page1_image_path:
            need_vision = (not metadata.get("vin") or not metadata.get("vehicle"))
            if need_vision:
                logger.info("Regex metadata incomplete — trying Haiku vision fallback")
                vision_meta = await _extract_pdf_metadata_vision(page1_image_path)
                if vision_meta.get("vin") and not metadata.get("vin"):
                    metadata["vin"] = vision_meta["vin"]
                if vision_meta.get("vehicle") and not metadata.get("vehicle"):
                    metadata["vehicle"] = vision_meta["vehicle"]
                if vision_meta.get("claim_number") and not metadata.get("claim_number"):
                    metadata["claim_number"] = vision_meta["claim_number"]
            # Clean up page 1 temp file now
            try:
                Path(page1_image_path).unlink()
            except OSError:
                pass

        if not results:
            # No parts extracted — still return metadata if we have it
            if metadata.get("vin") or metadata.get("vehicle"):
                return {
                    "vin": metadata.get("vin"),
                    "vehicle": metadata.get("vehicle"),
                    "claim_number": metadata.get("claim_number"),
                    "parts": [],
                    "error": "Could not extract parts from any PDF page (metadata only)",
                }
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

        # Bug 5: metadata pass 1 wins — it's more reliable than vision-OCR for VIN/vehicle.
        if metadata.get("vin") and validate_vin(metadata["vin"]):
            merged["vin"] = metadata["vin"]
        if metadata.get("vehicle"):
            merged["vehicle"] = metadata["vehicle"]
        if metadata.get("claim_number"):
            merged["claim_number"] = metadata["claim_number"]

        # Final VIN validation
        if merged.get("vin") and not validate_vin(merged["vin"]):
            logger.warning(f"VIN '{merged['vin']}' failed validation (len/charset) — clearing")
            merged["vin"] = None

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
            model=_SONNET_MODEL,
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
