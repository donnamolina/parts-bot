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
    "parking sensor control module", "parking sensor module",
    "rear parking sensor module", "front parking sensor control module",
    "backup camera module", "pdc module", "sensor control module",
})

LOCAL_ONLY_PARTS = frozenset({
    "decal", "sticker", "emblem decal", "bed decal",
    "tailgate decal", "hood decal", "4x4 decal",
    "truck decal", "manufacturer decal",
})

DEALER_HARDWARE_PARTS = frozenset({
    "hook", "latch hook", "tailgate hook", "side panel hook",
    "tailgate hinge", "tailgate hinges", "door hinge upper", "door hinge lower",
    "side door hinge",
    "bed hook", "tie down hook",
    "clip", "retainer clip", "mounting clip",
    "end cap", "bumper end cap", "bumper flare", "bumper trim",
    "molding", "bumper molding", "door molding",
    "headlight bracket", "headlight slider", "headlight guide", "headlight mount",
    "headlamp bracket", "headlamp slider",
    "bumper guide", "bumper bracket", "bumper slider",
})


def classify_part(part_name_english: str) -> str | None:
    """
    Returns 'unshippable' | 'dealer_only' | 'vin_programmed'
            | 'local_only' | 'dealer_hardware' | None.
    """
    name = (part_name_english or "").lower().strip()
    for kw in VIN_PROGRAMMED_PARTS:
        if kw in name:
            return "vin_programmed"
    for kw in DEALER_ONLY_PARTS:
        if kw in name:
            return "dealer_only"
    for kw in UNSHIPPABLE_PARTS:
        if kw in name:
            return "unshippable"
    for kw in LOCAL_ONLY_PARTS:
        if kw in name:
            return "local_only"
    for kw in DEALER_HARDWARE_PARTS:
        if kw in name:
            return "dealer_hardware"
    # Fuzzy: electronic modules with sensor/ecu/control keywords → vin_programmed
    if "module" in name:
        if any(w in name for w in ["sensor", "ecu", "control", "bcm", "pcm", "tcm", "srs", "abs", "airbag"]):
            return "vin_programmed"
    # Fuzzy: headlight/headlamp structural hardware → dealer_hardware
    if ("headlight" in name or "headlamp" in name) and any(w in name for w in ["bracket", "slider", "guide", "mount"]):
        return "dealer_hardware"
    return None


MANUAL_REVIEW_NOTES = {
    "unshippable": "No apto para envío — comprar local DR",
    "dealer_only": "Requiere concesionario — razones de seguridad",
    "vin_programmed": "Requiere concesionario — programación VIN",
    "local_only": "Producir/comprar local DR — no se consigue en eBay",
    "dealer_hardware": "Hardware de dealer — cotizar con concesionario",
}
