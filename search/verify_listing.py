"""
eBay listing verification using Sonnet.

After 7zap finds an OEM# and eBay returns a listing, this module checks
whether the listing actually matches the requested part type.
Runs concurrently via asyncio.gather — no sequential overhead.
"""

import asyncio
import logging
import os

_SONNET_MODEL = os.environ.get("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-6")

logger = logging.getLogger("parts-bot.verify")

VERIFY_PROMPT = """\
You are an automotive parts verification expert. You know the difference between assemblies and sub-components, between similar-sounding parts (door sill molding vs bumper panel, bracket vs headlight assembly, decal kit vs hood hinge, fender flare vs fender panel).

Does this eBay listing match the requested part?

Requested part: {part_name} for {year} {make} {model}
OEM number searched: {oem_number}
OEM catalog description: {oem_description}
eBay listing title: {listing_title}
eBay listing price: ${price}

Check TWO consistency conditions:
(a) Does the eBay listing match the CLIENT REQUEST (the requested part name)?
(b) Does the OEM catalog description match the client request AND match what the eBay listing is selling?
    — If the OEM describes a sub-component (REINFORCEMENT, ABSORBER, BRACE, SUB-ASSY) but the
      client requested the full assembly, that is an OEM mismatch even if the eBay listing is correct.

Reply with ONLY one line:
MATCH — all three align (client request, OEM description, eBay listing)
OEM_MISMATCH — [5 word explanation] (eBay listing is correct, but OEM describes a different sub-component)
WRONG_PART — [5 word explanation] (eBay listing does not match the client request)
SUSPICIOUS_PRICE — [5 word explanation]

If the OEM description field is empty or "N/A", ignore condition (b) and only check (a).
If unsure about (b), reply MATCH — OEM mismatches should only be flagged when clearly different."""


async def verify_ebay_listing(
    part_name_english: str,
    year: int,
    make: str,
    model: str,
    oem_number: str,
    listing_title: str,
    price: float,
    api_key: str,
    oem_description: str = "",
) -> dict:
    """
    Call Sonnet to verify an eBay listing matches the requested part.

    Returns dict: {"verdict": "MATCH"|"WRONG_PART"|"SUSPICIOUS_PRICE"|"OEM_MISMATCH"|"UNVERIFIED", "note": str}
    """
    _desc = oem_description or "N/A"

    async def _call_once(client):
        prompt = VERIFY_PROMPT.format(
            part_name=part_name_english,
            year=year,
            make=make,
            model=model,
            oem_number=oem_number or "N/A",
            oem_description=_desc,
            listing_title=listing_title,
            price=f"{price:.2f}",
        )
        msg = await client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        raw = None
        last_exc = None
        for attempt in range(2):
            try:
                raw = await _call_once(client)
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    await asyncio.sleep(1)
                # Don't retry on 4xx auth errors — they won't recover
                err_str = str(e)
                if "403" in err_str or "401" in err_str or "400" in err_str:
                    break
                if attempt == 0:
                    await asyncio.sleep(3)

        if raw is None:
            logger.warning(f"verify_ebay_listing failed after retries for '{part_name_english}': {last_exc}")
            return {"verdict": "UNVERIFIED", "note": "Verificación no disponible"}

        logger.info(f"verify '{part_name_english}': {raw[:80]}")

        if raw.startswith("WRONG_PART"):
            note = raw[len("WRONG_PART"):].lstrip(" —-").strip()
            return {"verdict": "WRONG_PART", "note": note or "Wrong part type"}
        elif raw.startswith("OEM_MISMATCH"):
            note = raw[len("OEM_MISMATCH"):].lstrip(" —-").strip()
            return {"verdict": "OEM_MISMATCH", "note": note or "OEM sub-component mismatch"}
        elif raw.startswith("SUSPICIOUS_PRICE"):
            note = raw[len("SUSPICIOUS_PRICE"):].lstrip(" —-").strip()
            return {"verdict": "SUSPICIOUS_PRICE", "note": note or "Suspicious price"}
        else:
            return {"verdict": "MATCH", "note": ""}

    except Exception as e:
        logger.warning(f"verify_ebay_listing error for '{part_name_english}': {e}")
        return {"verdict": "UNVERIFIED", "note": "Verificación no disponible"}
