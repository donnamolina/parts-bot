"""
Parts that should be skipped in the eBay pipeline and routed to manual review.
"""

UNSHIPPABLE_PARTS = frozenset({
    "windshield", "rear glass", "back glass", "side glass",
    "door glass", "quarter glass",
})

DEALER_ONLY_PARTS = frozenset({
    "airbag", "knee airbag", "curtain airbag", "side airbag",
    "seatbelt", "seat belt", "seatbelt pretensioner",
    "airbag module", "srs module", "srs ecu", "airbag ecu",
})

VIN_PROGRAMMED_PARTS = frozenset({
    "ecu", "pcm", "ecm", "tcm", "bcm", "immobilizer",
    "key fob", "smart key", "cluster", "instrument cluster",
})

def classify_part(part_name_english: str) -> str | None:
    """
    Returns 'unshippable', 'dealer_only', 'vin_programmed', or None.
    Match by checking if any keyword is contained in the part name.
    """
    name = part_name_english.lower().strip()
    for kw in VIN_PROGRAMMED_PARTS:
        if kw in name:
            return "vin_programmed"
    for kw in DEALER_ONLY_PARTS:
        if kw in name:
            return "dealer_only"
    for kw in UNSHIPPABLE_PARTS:
        if kw in name:
            return "unshippable"
    return None

MANUAL_REVIEW_NOTES = {
    "unshippable": "No apto para envío — comprar local DR",
    "dealer_only": "Requiere concesionario — razones de seguridad",
    "vin_programmed": "Requiere concesionario — programación VIN",
}
