"""
Estimated shipping weights in pounds for auto parts.
Used for ClickPack courier cost calculation (weight-based pricing).
Conservative estimates — round up when uncertain.
"""

PART_WEIGHT_ESTIMATES = {
    # Body panels
    "hood": 35,
    "fender": 15,
    "bumper cover": 12,
    "front bumper cover": 12,
    "rear bumper cover": 10,
    "door": 50,
    "trunk lid": 30,
    "tailgate": 45,
    "roof panel": 40,
    "rocker panel": 8,
    "fender flare": 3,
    "fender liner": 3,
    "inner fender": 3,

    # Bumper components
    "bumper bracket": 4,
    "bumper reinforcement": 6,
    "bumper absorber": 3,

    # Lighting
    "headlight": 8,
    "headlamp assembly": 8,
    "tail light": 5,
    "tail lamp assembly": 5,
    "fog light": 3,
    "side marker": 1,
    "turn signal": 2,
    "third brake light": 2,
    "backup light": 1,
    "license plate light": 1,

    # Glass / mirrors
    "windshield": 25,
    "rear windshield": 20,
    "rear window": 20,
    "side mirror": 5,
    "mirror": 5,
    "mirror glass": 2,
    "rearview mirror": 1,

    # Grille / trim
    "grille": 6,
    "radiator support": 15,
    "valance": 5,
    "rear valance": 5,
    "spoiler": 8,
    "chrome trim": 3,
    "door molding": 2,

    # Suspension
    "control arm": 8,
    "lower control arm": 8,
    "upper control arm": 6,
    "strut assembly": 12,
    "strut": 12,
    "shock absorber": 6,
    "sway bar link": 2,
    "sway bar": 8,
    "ball joint": 3,
    "tie rod end": 2,
    "tie rod": 2,
    "wheel bearing": 5,
    "wheel hub assembly": 8,
    "hub assembly": 8,
    "cv axle": 10,
    "cv joint": 5,
    "coil spring": 8,
    "strut mount": 3,
    "bushing": 1,
    "subframe": 40,
    "steering rack": 15,
    "power steering pump": 8,

    # Brakes
    "brake rotor": 12,
    "brake disc": 12,
    "brake pad": 3,
    "brake pads": 3,
    "brake caliper": 8,
    "brake shoe": 3,
    "brake master cylinder": 5,
    "brake hose": 1,
    "abs sensor": 1,

    # Engine / cooling
    "radiator": 15,
    "water pump": 5,
    "alternator": 12,
    "starter": 10,
    "ac compressor": 15,
    "ac condenser": 10,
    "engine mount": 5,
    "transmission mount": 5,
    "oil pan": 8,
    "valve cover": 4,
    "valve cover gasket": 1,
    "radiator fan": 8,
    "fan motor": 5,
    "thermostat": 1,
    "radiator hose": 2,
    "radiator cap": 1,
    "intercooler": 10,
    "turbocharger": 15,

    # Belt / timing
    "timing belt": 2,
    "timing belt kit": 5,
    "timing chain": 3,
    "serpentine belt": 1,
    "belt tensioner": 2,

    # Fuel / intake
    "fuel pump": 4,
    "fuel injector": 1,
    "intake manifold": 10,

    # Exhaust
    "catalytic converter": 15,
    "muffler": 12,
    "exhaust manifold": 12,
    "oxygen sensor": 1,

    # Electrical
    "ignition coil": 1,
    "spark plug": 1,
    "speed sensor": 1,
    "temperature sensor": 1,

    # Drivetrain
    "clutch": 12,
    "flywheel": 15,

    # Interior
    "window regulator": 4,
    "window motor": 3,
    "door handle": 1,
    "door lock": 2,
    "blower motor": 4,

    # Small hardware
    "clips": 1,
    "fasteners": 1,
    "clip": 1,
    "fastener": 1,

    # Misc
    "wiper blade": 1,
    "wiper motor": 3,
    "mud flap": 2,
    "light bulb": 1,
}


def estimate_weight(part_name_english: str) -> int:
    """Estimate shipping weight in pounds. Returns conservative estimate."""
    name = part_name_english.lower().strip()

    # Exact match
    if name in PART_WEIGHT_ESTIMATES:
        return PART_WEIGHT_ESTIMATES[name]

    # Partial match — check if any key is contained in the name or vice versa
    for key, weight in PART_WEIGHT_ESTIMATES.items():
        if key in name or name in key:
            return weight

    # Default fallback
    return 8
