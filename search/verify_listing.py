"""
eBay listing verification using Sonnet.

After 7zap finds an OEM# and eBay returns a listing, this module checks
whether the listing actually matches the requested part type.
Runs concurrently via asyncio.gather — no sequential overhead.
"""

import asyncio
import logging
import os

logger = logging.getLogger("parts-bot.verify")

VERIFY_PROMPT = """\
You are an automotive parts verification expert. You know the difference between assemblies and sub-components, between similar-sounding parts (door sill molding vs bumper panel, bracket vs headlight assembly, decal kit vs hood hinge, fender flare vs fender panel).

Does this eBay listing match the requested part?

Requested part: {part_name} for {year} {make} {model}
OEM number searched: {oem_number}
eBay listing title: {listing_title}
eBay listing price: ${price}

Think about:
- Is this the same TYPE of part? (not just similar words)
- Is a bracket being sold as an assembly?
- Is this for the correct vehicle or a platform sibling?
- Is the price reasonable for this type of part?

Reply with ONLY one line:
MATCH — correct part
WRONG_PART — [5 word explanation]
SUSPICIOUS_PRICE — [5 word explanation]

If unsure, reply MATCH — false positives are worse than false negatives."""


async def verify_ebay_listing(
    part_name_english: str,
    year: int,
    make: str,
    model: str,
    oem_number: str,
    listing_title: str,
    price: float,
    api_key: str,
) -> dict:
    """
    Call Sonnet to verify an eBay listing matches the requested part.

    Returns dict: {"verdict": "MATCH"|"WRONG_PART"|"SUSPICIOUS_PRICE", "note": str}
    """
    try:
        import anthropic

        prompt = VERIFY_PROMPT.format(
            part_name=part_name_english,
            year=year,
            make=make,
            model=model,
            oem_number=oem_number,
            listing_title=listing_title,
            price=f"{price:.2f}",
        )

        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        logger.info(f"verify '{part_name_english}': {raw[:80]}")

        if raw.startswith("WRONG_PART"):
            note = raw[len("WRONG_PART"):].lstrip(" —-").strip()
            return {"verdict": "WRONG_PART", "note": note or "Wrong part type"}
        elif raw.startswith("SUSPICIOUS_PRICE"):
            note = raw[len("SUSPICIOUS_PRICE"):].lstrip(" —-").strip()
            return {"verdict": "SUSPICIOUS_PRICE", "note": note or "Suspicious price"}
        else:
            return {"verdict": "MATCH", "note": ""}

    except Exception as e:
        logger.warning(f"verify_ebay_listing failed for '{part_name_english}': {e}")
        return {"verdict": "MATCH", "note": ""}  # fail-open: don't flag if verify fails
