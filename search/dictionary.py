"""
DR Spanish auto parts dictionary + PART_TO_CATEGORY mapping for RockAuto.

This dictionary maps Dominican Spanish slang, standard Spanish, and English
part names to RockAuto category paths. It is the competitive advantage of
this system — 200+ entries covering body, lighting, suspension, brakes,
engine, drivetrain, electrical, cooling, exhaust, and interior parts.
"""

# ─── DR Spanish → English Translation ─────────────────────────────────────────
# Used for display and eBay search query construction

DR_TO_ENGLISH = {
    # Wheels / Tires
    "aro": "wheel rim",
    "neumatico": "tire",
    "neumático": "tire",
    "llanta": "tire",

    # Electrical / Sensors
    "modulo de control de sensor de parqueo trasero": "rear parking sensor control module",
    "modulo de control de sensor de parqueo delantero": "front parking sensor control module",
    "modulo de control de sensor de parqueo": "parking sensor control module",
    "sensor de parqueo": "parking sensor",
    "sensor de parqueo trasero": "rear parking sensor",

    # Body / Exterior
    "bonete": "hood panel",
    "capó": "hood panel",
    "capo": "hood panel",
    "guardafango": "fender panel",
    "guardalodo": "fender panel",
    "guardabarro": "fender panel",
    "parachoque delantero": "front bumper",
    "parachoque trasero": "rear bumper",
    "parachoques delantero": "front bumper",
    "parachoques trasero": "rear bumper",
    "parachoque": "bumper",
    "parachoques": "bumper",
    "bumper": "bumper",
    "bumper delantero": "front bumper",
    "bumper trasero": "rear bumper",
    "forro de bumper delantero": "front bumper cover",
    "forro de bumper trasero": "rear bumper cover",
    "forro de bumper": "bumper cover",
    "forro bumper": "bumper cover",
    "facia de bumper delantero": "front bumper cover",
    "facia de bumper trasero": "rear bumper cover",
    "facia de bumper": "bumper cover",
    "fascia de bumper delantero": "front bumper cover",
    "fascia de bumper trasero": "rear bumper cover",
    "fascia de bumper": "bumper cover",
    "bumper trasero inferior": "rear bumper lower panel",
    "bumper inferior": "lower bumper panel",
    "reflector bumper trasero": "rear bumper reflector",
    "reflector bumper delantero": "front bumper reflector",
    "reflector bumper": "bumper reflector",
    "reflector de bumper": "bumper reflector",
    "guias de bumper": "bumper guide bracket",
    "guías de bumper": "bumper guide bracket",
    "guia de bumper": "bumper guide bracket",
    "guía de bumper": "bumper guide bracket",
    "guia bumper": "bumper guide bracket",
    "guía bumper": "bumper guide bracket",
    "refuerzo de bumper": "bumper reinforcement",
    "absorbedor de bumper": "bumper absorber",
    "parrilla": "grille assembly",
    "rejilla": "grille assembly",
    "marco de radiador": "radiator support",
    "compuerta": "tailgate",
    "tapa de baul": "trunk lid",
    "tapa de baúl": "trunk lid",
    "tapa maletero": "trunk lid",
    "tapa del maletero": "trunk lid",
    "tapa de maletero": "trunk lid",
    "falda": "rocker panel",
    "spoiler": "spoiler",
    "difusor": "rear valance",
    "flear guardalodo": "fender flare",   # "flear guardalodo" before "guardalodo" so it wins longest-match
    "flear guardafango": "fender flare",
    "flear guardabarro": "fender flare",
    "flear": "fender flare",

    # Engine undertray / splash shield
    "cover inferior de motor completo": "engine splash shield",
    "cover inferior de motor": "engine splash shield",
    "cover inferior motor": "engine splash shield",
    "protector inferior de motor": "engine splash shield",
    "protector inferior motor": "engine splash shield",
    "guarda inferior de motor": "engine splash shield",
    "guarda inferior motor": "engine splash shield",

    # Decals / stickers (produce local in DR)
    "calcomania de cama 4x4": "4x4 decal",
    "calcomania compuerta trasera": "tailgate decal",
    "calcomania de cama": "bed decal",
    "calcomania 4x4": "4x4 decal",
    "calcomanía de cama 4x4": "4x4 decal",
    "calcomanía compuerta trasera": "tailgate decal",
    "calcomanía de cama": "bed decal",
    "calcomanía 4x4": "4x4 decal",
    "calcomania": "decal",
    "calcomanía": "decal",

    # Hooks / hardware (dealer-only)
    "gancho compuerta lateral": "side panel hook",
    "gancho de compuerta lateral": "side panel hook",
    "gancho lateral": "side panel hook",
    "gancho": "hook",

    # Hinges (dealer-only hardware)
    "visagra de compuerta trasera": "tailgate hinge",
    "visagra compuerta trasera": "tailgate hinge",
    "visagras de compuerta trasera": "tailgate hinges",
    "visagra superior": "upper hinge",
    "bisagra de compuerta trasera": "tailgate hinge",
    "bisagra compuerta trasera": "tailgate hinge",
    "bisagras de compuerta trasera": "tailgate hinges",
    "bisagra superior": "upper hinge",
    "visagra": "hinge",
    "bisagra": "hinge",

    # Bumper end caps / flares
    "aleta bumper delantero": "front bumper end cap",
    "aleta de bumper": "bumper end cap",
    "aleta bumper": "bumper end cap",

    # Grille variants (lower grille, not bumper cover)
    "parilla inferior bumper delantero": "front lower bumper grille",
    "parilla inferior bumper trasero": "rear lower bumper grille",
    "parilla inferior bumper": "lower bumper grille",
    "parilla inferior": "lower grille",
    "parrilla inferior bumper delantero": "front lower bumper grille",
    "parrilla inferior bumper": "lower bumper grille",
    "parrilla inferior": "lower grille",

    # Bumper lip (not bumper cover)
    "lip bumper delantero": "front bumper lip",
    "lip bumper trasero": "rear bumper lip",
    "lip de bumper delantero": "front bumper lip",
    "lip de bumper trasero": "rear bumper lip",
    "lip de bumper": "bumper lip",
    "lip bumper": "bumper lip",

    # Fog light / halogen (not bumper cover)
    "halogeno bumper delantero": "front fog light",
    "halógeno bumper delantero": "front fog light",
    "halogeno bumper": "fog light",
    "halógeno bumper": "fog light",
    "halogeno superior e inferior": "upper and lower fog light",
    "halogeno superior": "upper fog light",
    "halogeno inferior": "lower fog light",
    "halogeno delantero": "halogen headlight",
    "halógeno delantero": "halogen headlight",
    "halogeno": "halogen headlight",
    "halógeno": "halogen headlight",

    # Headlight lens cover / bezel (not full headlight)
    "cover pantalla": "headlight lens cover",
    "cover de pantalla": "headlight lens cover",
    "mica de pantalla": "headlight lens",
    "mica pantalla": "headlight lens",
    "bezel pantalla": "headlight bezel",

    # Intercooler duct (not the intercooler itself)
    "ducto del intercooler": "intercooler hose",
    "ducto de intercooler": "intercooler hose",
    "ducto intercooler": "intercooler hose",

    # Front-end subcomponents
    "frentil central": "center bumper support",
    "frentil central delantero": "front center bumper support",

    # Engine mount variants
    "soporte electronico de motor": "electronic engine mount",
    "soporte electrónico de motor": "electronic engine mount",
    "soporte electronico del motor": "electronic engine mount",

    "tina": "fender liner",
    "lodero": "mud flap",
    "chapaleta": "mud flap",
    "chapaleta trasera": "rear mud flap",
    "chapaleta delantera": "front mud flap",
    "puerta": "door assembly",
    "moldura de puerta": "door molding",
    "platina": "chrome trim",
    "tolva": "truck bed",
    "techo": "roof panel",

    # Glass (see also Bug 10 manual-review entries near airbags section)
    "cristal": "windshield",
    "parabrisas": "windshield",
    "medallon": "rear window",
    "medallón": "rear window",

    # Lighting
    "faros traseros": "tail light",
    "faros delanteros": "headlight",
    "faro trasero": "tail light",
    "faro delantero": "headlight",
    "faros": "headlight",
    "faro": "headlight",
    "farol trasero": "tail light",
    "farol delantero": "headlight",
    "farol": "headlight",
    # Bug 9: pantalla context-awareness — explicit positional forms map; bare "pantalla"
    # is intentionally NOT mapped so Bug 6 correction handler can ask for clarification.
    "pantalla delantera": "headlight",  # pantalla = headlight in DR; bumper cover uses "bumper delantero"
    "pantalla trasera": "tail light",
    "pantalla del": "headlight",
    "pantalla tras": "tail light",
    "pantalla lh del": "headlight",
    "pantalla rh del": "headlight",
    "stop": "tail light",
    "violeta": "tail light",
    "cocuyo": "side marker",
    "cocuyo trasero": "rear side marker",
    "neblinero": "fog light",
    "direccional": "turn signal",
    "flasher": "turn signal",
    "foco": "light bulb",
    "tercera luz de freno": "third brake light",
    "luz de placa": "license plate light",
    "luz de retroceso": "backup light",

    # Mirrors
    "espejo": "side mirror",
    "retrovisor": "side mirror",
    "espejo interior": "rearview mirror",
    "luna del espejo": "mirror glass",

    # Suspension / Steering
    "catre": "control arm",
    "catre de abajo": "lower control arm",
    "catre de arriba": "upper control arm",
    "bola esferica": "ball joint",
    "bola esférica": "ball joint",
    "rotula": "tie rod end",
    "rótula": "tie rod end",
    "terminal": "tie rod end",
    "puntera": "tie rod end",
    "amortiguador": "shock absorber",
    "estru": "strut assembly",
    "espring": "coil spring",
    "piña": "wheel hub assembly",
    "caja de bolas": "wheel bearing",
    "rolinera": "wheel bearing",
    "palier": "cv axle",
    "tripoide": "cv joint",
    "cremallera": "steering rack",
    "cuna": "subframe",
    "bomba de direccion": "power steering pump",
    "bomba de dirección": "power steering pump",
    "buje": "bushing",
    "base de amortiguador": "strut mount",
    "bieleta": "sway bar link",
    "barra estabilizadora": "sway bar",

    # Brakes
    "disco de freno": "brake rotor",
    "pastilla de freno": "brake pad",
    "pastilla": "brake pad",
    "zapata": "brake shoe",
    "cilindro maestro": "brake master cylinder",
    "caliper": "brake caliper",
    "manguera de freno": "brake hose",
    "sensor de abs": "abs sensor",

    # Engine / Drivetrain
    "cran": "oil pan",
    "abanico": "radiator fan",
    "motor de abanico": "fan motor",
    "bomba de agua": "water pump",
    "bomba de gasolina": "fuel pump",
    "bomba de aceite": "oil pump",
    "motor de arranque": "starter",
    "alternador": "alternator",
    "correa de tiempo": "timing belt",
    "kit de tiempo": "timing belt kit",
    "cadena de tiempo": "timing chain",
    "correa de accesorios": "serpentine belt",
    "tensor de correa": "belt tensioner",
    "soporte del motor": "engine mount",
    "soporte de transmision": "transmission mount",
    "soporte de transmisión": "transmission mount",
    "tapa de valvula": "valve cover",
    "tapa de válvula": "valve cover",
    "empaque de tapa": "valve cover gasket",
    "bobina": "ignition coil",
    "bujia": "spark plug",
    "bujía": "spark plug",
    "inyector": "fuel injector",
    "multiple de admision": "intake manifold",
    "múltiple de admisión": "intake manifold",
    "multiple de escape": "exhaust manifold",
    "múltiple de escape": "exhaust manifold",
    "catalitico": "catalytic converter",
    "catalítico": "catalytic converter",
    "mofle": "muffler",
    "silenciador": "muffler",
    "cloch": "clutch",
    "clutch": "clutch",
    "volante del motor": "flywheel",
    "condensador": "ac condenser",
    "compresor de aire": "ac compressor",
    "termostato": "thermostat",
    # A/C hoses (Bug 7) — placed BEFORE generic "manguera" for longest-match
    "manguera alta de a/c": "a/c discharge hose",
    "manguera baja de a/c": "a/c suction hose",
    "manguera a/c alta": "a/c discharge hose",
    "manguera a/c baja": "a/c suction hose",
    "manguera alta ac": "a/c discharge hose",
    "manguera baja ac": "a/c suction hose",
    "mangera": "radiator hose",
    "manguera": "radiator hose",
    "manguera de radiador": "radiator hose",
    "intercooler": "intercooler",
    "turbo": "turbocharger",

    # Electrical
    "sensor de oxigeno": "oxygen sensor",
    "sensor de oxígeno": "oxygen sensor",
    "sensor de velocidad": "speed sensor",
    "sensor de temperatura": "temperature sensor",

    # Interior
    "elevador de cristal": "window regulator",
    "motor de cristal": "window motor",
    "manigueta": "door handle",
    "cerradura": "door lock",
    "parilla de aire": "blower motor",

    # Cooling
    "tapa de radiador": "radiator cap",
    "radiador": "radiator",

    # Spelling variants common in DR
    "shoc": "shock absorber",
    "choque": "shock absorber",
    "breik": "brake pad",

    # Airbags / Safety (Bug 10: manual-review keywords)
    "bolsa de aire de rodilleras":  "knee airbag",
    "bolsa de aire cortina":        "curtain airbag",
    "bolsa de aire techo":          "curtain airbag",
    "bolsa de aire rodilla":        "knee airbag",
    "bolsa de aire guia":           "side curtain airbag",
    "bolsa de aire guía":           "side curtain airbag",
    "bolsa de aire lateral":        "side airbag",
    "bolsa de aire pasajero":       "passenger airbag",
    "bolsa de aire asiento":        "seat airbag",
    "bolsa de aire volante":        "driver airbag",
    "bolsa de aire":                "airbag",
    "modulo de bolsa de aire":      "airbag module",
    "módulo de bolsa de aire":      "airbag module",
    "modulo srs":                   "srs module",
    "módulo srs":                   "srs module",
    "cinturon de seguridad":        "seatbelt",
    "cinturón de seguridad":        "seatbelt",
    "cinturon trasero":             "rear seatbelt",
    "cinturón trasero":             "rear seatbelt",
    "cinturon delantero":           "front seatbelt",
    "cinturón delantero":           "front seatbelt",
    "cinturon":                     "seatbelt",
    "cinturón":                     "seatbelt",
    "pretensor":                    "seatbelt pretensioner",
    # Glass (Bug 10 manual review keywords)
    "cristal delantero":            "windshield",
    "cristal trasero":              "rear glass",
    # Modules / ECUs (Bug 10 dealer-only / VIN-programmed keywords)
    # NOTE: bare "modulo" is intentionally NOT mapped — Bug 6 asks for clarification.
    "computadora de motor":         "ecu",
    "computadora del motor":        "ecu",
    "modulo de transmision":        "tcm",
    "modulo de transmisión":        "tcm",
    "módulo de transmision":        "tcm",
    "módulo de transmisión":        "tcm",

    # Front-end assembly
    "patas de bonete":              "hood hinges",
    "patas bonete":                 "hood hinges",   # elided "de"
    "pata de bonete":               "hood hinge",    # singular
    "pata bonete":                  "hood hinge",    # singular elided
    "cerradura de bonete":          "hood latch",
    "cerradura bonete":             "hood latch",    # elided "de"
    "frentil completo":             "front end assembly",
    "frentil":                      "front clip",
    "base de pantallas":            "headlight mounting bracket",
    "base de pantalla":             "headlight mounting bracket",   # singular
    "deslizador de pantalla delantera": "front headlight slider",
    "deslizador de pantalla":       "headlight slider",
    "deslizador":                   "headlight slider",
    "base de bumper":               "bumper reinforcement",
    "base de búmper":               "bumper reinforcement",
    "base de parachoque":           "bumper reinforcement",
    "base de parachoques":          "bumper reinforcement",
    "absorbedor de parachoque":     "bumper energy absorber",
    "absorbedor de parachoques":    "bumper energy absorber",
    "clips":                        "retaining clips",
    "estribo":                      "running board",
    "estribo plastico":             "running board",
    "estribo plastico trasero":     "rear running board",
    "estribo plastico delantero":   "front running board",
    "reflector":                    "reflector",
}

# ─── SIDE / POSITION EXTRACTION ───────────────────────────────────────────────

import re as _re

# Ordered longest-first to avoid partial matches (e.g. "del" inside "delantero")
SIDE_INDICATORS = [
    ("izquierdo", "left"), ("izquierda", "left"), ("derecho", "right"), ("derecha", "right"),
    ("passenger", "right"), ("driver", "left"),
    ("right", "right"), ("left", "left"),
    ("izq", "left"), ("der", "right"),
    ("rh", "right"), ("lh", "left"),
]

POSITION_INDICATORS = [
    ("delantero", "front"), ("delantera", "front"), ("trasero", "rear"), ("trasera", "rear"),
    ("front", "front"), ("rear", "rear"),
    ("tras", "rear"),
    # NOTE: "del" removed — it means "of the" in Spanish, not "front".
    # "delantero"/"delantera" already catch front position.
]


def extract_side_position(text: str) -> tuple:
    """Extract side (left/right) and position (front/rear) from part description.
    Returns (cleaned_text, side, position)."""
    lower = text.lower().strip()
    side = None
    position = None

    # Use word-boundary regex to avoid matching inside other words
    # Check longest indicators first
    for indicator, value in SIDE_INDICATORS:
        pattern = r'\b' + _re.escape(indicator) + r'\b'
        if _re.search(pattern, lower):
            side = value
            lower = _re.sub(pattern, '', lower).strip()
            break

    for indicator, value in POSITION_INDICATORS:
        pattern = r'\b' + _re.escape(indicator) + r'\b'
        if _re.search(pattern, lower):
            position = value
            lower = _re.sub(pattern, '', lower).strip()
            break

    # Clean up extra spaces
    cleaned = " ".join(lower.split())
    return cleaned, side, position


def translate_part(name_original: str) -> dict:
    """Translate a DR Spanish part name to English with side/position extraction.

    Returns dict with: name_original, name_dr, name_english, side, position, quantity
    """
    import re as _re2

    # Detect leading quantity (e.g. "68 CLIPS" → qty=68)
    qty_match = _re2.match(r'^(\d+)\s+', name_original.strip())
    quantity = int(qty_match.group(1)) if qty_match else 1

    # Detect "RH Y LH" or "LH Y RH" — means both sides, quantity x2
    both_sides = bool(_re2.search(r'\bRH\s*[Yy]\s*LH\b|\bLH\s*[Yy]\s*RH\b', name_original, _re2.IGNORECASE))
    if both_sides:
        quantity = max(quantity, 2)

    # Strip leading qty and both-sides markers before further processing
    stripped = _re2.sub(r'^(\d+)\s+', '', name_original.strip())
    stripped = _re2.sub(r'\bRH\s*[Yy]\s*LH\b|\bLH\s*[Yy]\s*RH\b', '', stripped, flags=_re2.IGNORECASE).strip()

    cleaned, side, position = extract_side_position(stripped)

    # Build a side-only-stripped version (position words kept) for compound lookups.
    # This lets "farol trasero" match before it degrades to just "farol".
    side_stripped = stripped
    for _indicator, _ in SIDE_INDICATORS:
        side_stripped = _re.sub(r"\b" + _re.escape(_indicator) + r"\b",
                                "", side_stripped, flags=_re.IGNORECASE)
    side_stripped = " ".join(side_stripped.lower().split())

    # Lookup priority: exact(side-stripped) > exact(cleaned) > partial(side-stripped) > partial(cleaned)
    english = DR_TO_ENGLISH.get(side_stripped) or DR_TO_ENGLISH.get(cleaned.lower())

    if not english:
        for dr_term, en_term in sorted(DR_TO_ENGLISH.items(), key=lambda x: -len(x[0])):
            if dr_term in side_stripped:
                english = en_term
                break

    if not english:
        for dr_term, en_term in sorted(DR_TO_ENGLISH.items(), key=lambda x: -len(x[0])):
            if dr_term in cleaned.lower():
                english = en_term
                break

    if not english:
        # Fallback: use the cleaned text as-is (might already be English)
        english = cleaned

    if both_sides:
        english = f"{english} (L+R set)"

    return {
        "name_original": name_original,
        "name_dr": cleaned,
        "name_english": english,
        "side": side,
        "position": position,
        "quantity": quantity,
    }


# ─── PART_TO_CATEGORY — RockAuto Navigation Map ──────────────────────────────
# Maps part queries (DR Spanish, standard Spanish, English) to:
#   (rockauto_category_group_name, [subcategory_keywords])

PART_TO_CATEGORY = {
    # Body & lamp
    "bumper cover": ("body & lamp assembly", ["bumper cover"]),
    "bumper fascia": ("body & lamp assembly", ["bumper cover"]),
    "front bumper fascia": ("body & lamp assembly", ["bumper cover"]),
    "rear bumper fascia": ("body & lamp assembly", ["bumper cover"]),
    "bumper": ("body & lamp assembly", ["bumper cover"]),
    "front bumper": ("body & lamp assembly", ["bumper cover"]),
    "front bumper cover": ("body & lamp assembly", ["bumper cover"]),
    "rear bumper": ("body & lamp assembly", ["bumper cover"]),
    "rear bumper cover": ("body & lamp assembly", ["bumper cover"]),
    "rear bumper lower panel": ("body & lamp assembly", ["bumper cover", "lower"]),
    "lower bumper panel": ("body & lamp assembly", ["bumper cover", "lower"]),
    "rear bumper reflector": ("body & lamp assembly", ["reflector"]),
    "front bumper reflector": ("body & lamp assembly", ["reflector"]),
    "bumper reflector": ("body & lamp assembly", ["reflector"]),
    "reflector": ("body & lamp assembly", ["reflector"]),
    "bumper guide bracket": ("body & lamp assembly", ["bumper cover support", "bumper bracket"]),
    "front bumper guide bracket": ("body & lamp assembly", ["bumper cover support", "bumper bracket"]),
    "headlight mounting bracket": ("body & lamp assembly", ["headlamp bracket", "headlight bracket"]),
    "headlight adjuster": ("body & lamp assembly", ["headlamp adjuster", "headlight adjuster"]),
    "running board": ("body & lamp assembly", ["running board", "step"]),
    "side step": ("body & lamp assembly", ["running board", "step"]),
    "fender": ("body & lamp assembly", ["fender"]),
    "fender panel": ("body & lamp assembly", ["fender"]),
    "hood": ("body & lamp assembly", ["hood"]),
    "hood panel": ("body & lamp assembly", ["hood"]),
    "hood assembly": ("body & lamp assembly", ["hood"]),
    "grille": ("body & lamp assembly", ["grille"]),
    "grille assembly": ("body & lamp assembly", ["grille"]),
    "headlight": ("body & lamp assembly", ["headlamp assembly"]),
    "headlamp": ("body & lamp assembly", ["headlamp assembly"]),
    "headlight assembly": ("body & lamp assembly", ["headlamp assembly"]),
    "tail light": ("body & lamp assembly", ["tail lamp assembly"]),
    "tail lamp": ("body & lamp assembly", ["tail lamp assembly"]),
    "tail light assembly": ("body & lamp assembly", ["tail lamp assembly"]),
    "fog light": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "fog lamp": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "side mirror": ("body & lamp assembly", ["outside mirror"]),
    "mirror": ("body & lamp assembly", ["outside mirror"]),
    "door handle": ("body & lamp assembly", ["outside door handle"]),
    "valance": ("body & lamp assembly", ["valance panel"]),
    "radiator support": ("body & lamp assembly", ["radiator support"]),
    "side marker": ("body & lamp assembly", ["side marker"]),
    "turn signal": ("body & lamp assembly", ["turn signal"]),
    "door": ("body & lamp assembly", ["door"]),
    "door assembly": ("body & lamp assembly", ["door"]),
    "door molding": ("body & lamp assembly", ["door molding"]),
    "door lock": ("body & lamp assembly", ["door lock"]),
    "fender liner": ("body & lamp assembly", ["inner fender"]),
    "fender flare": ("body & lamp assembly", ["wheel housing molding", "fender flare"]),
    "trunk lid": ("body & lamp assembly", ["trunk"]),
    "tailgate": ("body & lamp assembly", ["tailgate"]),
    "spoiler": ("body & lamp assembly", ["spoiler"]),
    "rocker panel": ("body & lamp assembly", ["rocker panel"]),
    "bumper bracket": ("body & lamp assembly", ["bumper cover support", "bumper cover retainer"]),
    "bumper reinforcement": ("body & lamp assembly", ["bumper reinforcement"]),
    "bumper absorber": ("body & lamp assembly", ["bumper energy absorber"]),
    "chrome trim": ("body & lamp assembly", ["body side molding"]),
    "backup light": ("body & lamp assembly", ["back up lamp"]),
    "license plate light": ("body & lamp assembly", ["license lamp"]),
    "third brake light": ("body & lamp assembly", ["center high mount stop lamp"]),
    "rear valance": ("body & lamp assembly", ["valance panel"]),
    "roof panel": ("body & lamp assembly", ["roof panel"]),
    "inner fender": ("body & lamp assembly", ["inner fender"]),

    # Interior
    "window regulator": ("interior", ["window regulator"]),
    "window motor": ("interior", ["window motor"]),
    "rearview mirror": ("interior", ["inside rearview mirror"]),
    "mirror glass": ("body & lamp assembly", ["outside mirror glass"]),
    "blower motor": ("heat & air conditioning", ["blower motor"]),

    # Suspension
    "control arm": ("suspension", ["control arm"]),
    "lower control arm": ("suspension", ["control arm"]),
    "upper control arm": ("suspension", ["control arm"]),
    "ball joint": ("suspension", ["ball joint"]),
    "sway bar link": ("suspension", ["sway bar link", "stabilizer"]),
    "sway bar": ("suspension", ["sway bar", "stabilizer bar"]),
    "stabilizer link": ("suspension", ["sway bar link", "stabilizer"]),
    "strut assembly": ("suspension", ["strut assembly"]),
    "strut": ("suspension", ["strut assembly", "strut"]),
    "shock absorber": ("suspension", ["shock absorber"]),
    "coil spring": ("suspension", ["coil spring", "spring"]),
    "strut mount": ("suspension", ["strut mount", "shock mount"]),
    "bushing": ("suspension", ["bushing"]),
    "subframe": ("suspension", ["subframe"]),

    # Steering
    "tie rod end": ("steering", ["tie rod"]),
    "tie rod": ("steering", ["tie rod"]),
    "steering rack": ("steering", ["steering rack", "rack and pinion"]),
    "power steering pump": ("steering", ["power steering pump"]),

    # Brake & wheel hub
    "wheel hub assembly": ("brake & wheel hub", ["wheel bearing", "hub"]),
    "wheel bearing": ("brake & wheel hub", ["wheel bearing"]),
    "hub assembly": ("brake & wheel hub", ["wheel bearing", "hub"]),
    "brake rotor": ("brake & wheel hub", ["brake rotor", "rotor"]),
    "brake disc": ("brake & wheel hub", ["brake rotor", "rotor"]),
    "brake pad": ("brake & wheel hub", ["brake pad"]),
    "brake pads": ("brake & wheel hub", ["brake pad"]),
    "brake caliper": ("brake & wheel hub", ["brake caliper", "caliper"]),
    "brake shoe": ("brake & wheel hub", ["brake shoe"]),
    "brake master cylinder": ("brake & wheel hub", ["brake master cylinder"]),
    "brake hose": ("brake & wheel hub", ["brake hose"]),
    "abs sensor": ("brake & wheel hub", ["abs", "wheel speed sensor"]),

    # Drivetrain
    "cv axle": ("drivetrain", ["cv axle", "axle shaft"]),
    "cv joint": ("drivetrain", ["cv joint"]),
    "axle shaft": ("drivetrain", ["cv axle", "axle shaft"]),
    "clutch": ("drivetrain", ["clutch"]),
    "flywheel": ("drivetrain", ["flywheel"]),

    # Engine
    "oil pan": ("engine", ["oil pan"]),
    "water pump": ("engine", ["water pump"]),
    "engine mount": ("engine", ["engine mount"]),
    "transmission mount": ("engine", ["transmission mount"]),
    "valve cover": ("engine", ["valve cover"]),
    "valve cover gasket": ("engine", ["valve cover gasket"]),
    "timing chain": ("engine", ["timing chain"]),
    "oil pump": ("engine", ["oil pump"]),

    # Cooling
    "radiator": ("cooling system", ["radiator"]),
    "thermostat": ("cooling system", ["thermostat"]),
    "radiator fan": ("cooling system", ["radiator fan", "fan"]),
    "fan motor": ("cooling system", ["fan motor"]),
    "cooling fan": ("cooling system", ["radiator fan", "fan"]),
    "radiator hose": ("cooling system", ["radiator hose"]),
    "radiator cap": ("cooling system", ["radiator cap"]),
    "intercooler": ("cooling system", ["intercooler"]),

    # Fuel & Air
    "fuel pump": ("fuel & air", ["fuel pump"]),
    "fuel injector": ("fuel & air", ["fuel injector"]),
    "intake manifold": ("fuel & air", ["intake manifold"]),
    "turbocharger": ("fuel & air", ["turbocharger", "turbo"]),

    # Electrical
    "alternator": ("electrical", ["alternator"]),
    "starter": ("electrical", ["starter"]),
    "speed sensor": ("electrical", ["speed sensor"]),
    "temperature sensor": ("electrical", ["temperature sensor"]),

    # Ignition
    "ignition coil": ("ignition", ["ignition coil"]),
    "spark plug": ("ignition", ["spark plug"]),

    # Belt drive
    "timing belt": ("belt drive", ["timing belt"]),
    "timing belt kit": ("belt drive", ["timing belt", "timing kit"]),
    "serpentine belt": ("belt drive", ["serpentine belt", "drive belt"]),
    "belt tensioner": ("belt drive", ["belt tensioner"]),

    # Exhaust & Emission
    "catalytic converter": ("exhaust & emission", ["catalytic converter"]),
    "muffler": ("exhaust & emission", ["muffler"]),
    "exhaust manifold": ("exhaust & emission", ["exhaust manifold"]),
    "oxygen sensor": ("exhaust & emission", ["oxygen sensor"]),

    # Heat & AC
    "ac compressor": ("heat & air conditioning", ["a/c compressor", "compressor"]),
    "ac condenser": ("heat & air conditioning", ["a/c condenser", "condenser"]),

    # Wiper
    "wiper blade": ("wiper & washer", ["wiper blade"]),
    "wiper motor": ("wiper & washer", ["wiper motor"]),

    # ─── DR Spanish aliases (map to same categories as English above) ─────
    "wheel rim": ("brake & wheel hub", ["wheel", "rim"]),
    "18 inch wheel rim": ("brake & wheel hub", ["wheel", "rim"]),
    "tire": ("brake & wheel hub", ["tire"]),
    "parking sensor control module": ("electrical", ["parking sensor", "control module"]),
    "rear parking sensor control module": ("electrical", ["parking sensor", "control module"]),
    "front parking sensor control module": ("electrical", ["parking sensor", "control module"]),
    "parking sensor": ("electrical", ["parking sensor"]),
    "rear parking sensor": ("electrical", ["parking sensor"]),
    "bonete": ("body & lamp assembly", ["hood"]),
    "guardafango": ("body & lamp assembly", ["fender"]),
    "guardalodo": ("body & lamp assembly", ["fender"]),
    "parrilla": ("body & lamp assembly", ["grille"]),
    "rejilla": ("body & lamp assembly", ["grille"]),
    "farol": ("body & lamp assembly", ["headlamp assembly"]),
    "pantalla": ("body & lamp assembly", ["headlamp assembly"]),
    "stop": ("body & lamp assembly", ["tail lamp assembly"]),
    "violeta": ("body & lamp assembly", ["tail lamp assembly"]),
    "cocuyo": ("body & lamp assembly", ["side marker"]),
    "neblinero": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "direccional": ("body & lamp assembly", ["turn signal"]),
    "espejo": ("body & lamp assembly", ["outside mirror"]),
    "retrovisor": ("body & lamp assembly", ["outside mirror"]),
    "luna del espejo": ("body & lamp assembly", ["outside mirror glass"]),
    "catre": ("suspension", ["control arm"]),
    "catre de abajo": ("suspension", ["control arm"]),
    "catre de arriba": ("suspension", ["control arm"]),
    "bola esferica": ("suspension", ["ball joint"]),
    "rotula": ("steering", ["tie rod"]),
    "terminal": ("steering", ["tie rod"]),
    "puntera": ("steering", ["tie rod"]),
    "amortiguador": ("suspension", ["shock absorber", "strut"]),
    "estru": ("suspension", ["strut assembly", "strut"]),
    "espring": ("suspension", ["coil spring", "spring"]),
    "piña": ("brake & wheel hub", ["wheel bearing", "hub"]),
    "caja de bolas": ("brake & wheel hub", ["wheel bearing"]),
    "rolinera": ("brake & wheel hub", ["wheel bearing"]),
    "palier": ("drivetrain", ["cv axle", "axle shaft"]),
    "tripoide": ("drivetrain", ["cv joint"]),
    "cremallera": ("steering", ["steering rack", "rack and pinion"]),
    "cuna": ("suspension", ["subframe"]),
    "bomba de direccion": ("steering", ["power steering pump"]),
    "buje": ("suspension", ["bushing"]),
    "base de amortiguador": ("suspension", ["strut mount", "shock mount"]),
    "bieleta": ("suspension", ["sway bar link", "stabilizer"]),
    "barra estabilizadora": ("suspension", ["sway bar", "stabilizer bar"]),
    "disco de freno": ("brake & wheel hub", ["brake rotor", "rotor"]),
    "pastilla de freno": ("brake & wheel hub", ["brake pad"]),
    "pastilla": ("brake & wheel hub", ["brake pad"]),
    "zapata": ("brake & wheel hub", ["brake shoe"]),
    "cilindro maestro": ("brake & wheel hub", ["brake master cylinder"]),
    "caliper": ("brake & wheel hub", ["brake caliper", "caliper"]),
    "manguera de freno": ("brake & wheel hub", ["brake hose"]),
    "sensor de abs": ("brake & wheel hub", ["abs", "wheel speed sensor"]),
    "cran": ("engine", ["oil pan"]),
    "abanico": ("cooling system", ["radiator fan", "fan"]),
    "motor de abanico": ("cooling system", ["fan motor"]),
    "bomba de agua": ("engine", ["water pump"]),
    "bomba de gasolina": ("fuel & air", ["fuel pump"]),
    "bomba de aceite": ("engine", ["oil pump"]),
    "motor de arranque": ("electrical", ["starter"]),
    "alternador": ("electrical", ["alternator"]),
    "correa de tiempo": ("belt drive", ["timing belt"]),
    "kit de tiempo": ("belt drive", ["timing belt", "timing kit"]),
    "cadena de tiempo": ("engine", ["timing chain"]),
    "correa de accesorios": ("belt drive", ["serpentine belt", "drive belt"]),
    "tensor de correa": ("belt drive", ["belt tensioner"]),
    "soporte del motor": ("engine", ["engine mount"]),
    "soporte de transmision": ("engine", ["transmission mount"]),
    "tapa de valvula": ("engine", ["valve cover"]),
    "empaque de tapa": ("engine", ["valve cover gasket"]),
    "bobina": ("ignition", ["ignition coil"]),
    "bujia": ("ignition", ["spark plug"]),
    "inyector": ("fuel & air", ["fuel injector"]),
    "multiple de admision": ("fuel & air", ["intake manifold"]),
    "multiple de escape": ("exhaust & emission", ["exhaust manifold"]),
    "catalitico": ("exhaust & emission", ["catalytic converter"]),
    "mofle": ("exhaust & emission", ["muffler"]),
    "silenciador": ("exhaust & emission", ["muffler"]),
    "cloch": ("drivetrain", ["clutch"]),
    "volante del motor": ("drivetrain", ["flywheel"]),
    "condensador": ("heat & air conditioning", ["a/c condenser", "condenser"]),
    "compresor de aire": ("heat & air conditioning", ["a/c compressor", "compressor"]),
    "termostato": ("cooling system", ["thermostat"]),
    "mangera": ("cooling system", ["radiator hose", "hose"]),
    "manguera": ("cooling system", ["radiator hose", "hose"]),
    "manguera de radiador": ("cooling system", ["radiator hose"]),
    "intercooler": ("cooling system", ["intercooler"]),
    "turbo": ("fuel & air", ["turbocharger", "turbo"]),
    "sensor de oxigeno": ("exhaust & emission", ["oxygen sensor"]),
    "sensor de velocidad": ("electrical", ["speed sensor"]),
    "sensor de temperatura": ("electrical", ["temperature sensor"]),
    "elevador de cristal": ("interior", ["window regulator"]),
    "motor de cristal": ("interior", ["window motor"]),
    "manigueta": ("body & lamp assembly", ["outside door handle"]),
    "cerradura": ("body & lamp assembly", ["door lock"]),
    "parilla de aire": ("heat & air conditioning", ["blower motor"]),
    "tapa de radiador": ("cooling system", ["radiator cap"]),
    "radiador": ("cooling system", ["radiator"]),
    "shoc": ("suspension", ["shock absorber"]),
    "choque": ("suspension", ["shock absorber"]),
    "breik": ("brake & wheel hub", ["brake pad"]),
    "marco de radiador": ("body & lamp assembly", ["radiator support"]),
    "guia de bumper": ("body & lamp assembly", ["bumper cover support", "bumper bracket"]),
    "refuerzo de bumper": ("body & lamp assembly", ["bumper reinforcement"]),
    "absorbedor de bumper": ("body & lamp assembly", ["bumper energy absorber"]),
    "compuerta": ("body & lamp assembly", ["trunk", "tailgate"]),
    "tapa de baul": ("body & lamp assembly", ["trunk"]),
    "tapa maletero": ("body & lamp assembly", ["trunk"]),
    "tapa del maletero": ("body & lamp assembly", ["trunk"]),
    "falda": ("body & lamp assembly", ["rocker panel", "side skirt"]),
    "difusor": ("body & lamp assembly", ["valance panel"]),
    "flear": ("body & lamp assembly", ["wheel housing molding", "fender flare"]),
    "tina": ("body & lamp assembly", ["inner fender"]),
    "puerta": ("body & lamp assembly", ["door"]),
    "moldura de puerta": ("body & lamp assembly", ["door molding"]),

    # ─── New: grille / lip / fog / lens / duct / mount categories ────
    "front lower bumper grille": ("body & lamp assembly", ["grille", "lower"]),
    "rear lower bumper grille": ("body & lamp assembly", ["grille", "lower"]),
    "lower bumper grille": ("body & lamp assembly", ["grille", "lower"]),
    "lower grille": ("body & lamp assembly", ["grille", "lower"]),
    "front bumper lip": ("body & lamp assembly", ["bumper cover", "valance panel"]),
    "rear bumper lip": ("body & lamp assembly", ["bumper cover", "valance panel"]),
    "bumper lip": ("body & lamp assembly", ["bumper cover", "valance panel"]),
    "halogen headlight": ("body & lamp assembly", ["headlamp assembly"]),
    "front fog light": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "upper fog light": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "lower fog light": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "upper and lower fog light": ("body & lamp assembly", ["fog / driving lamp assembly"]),
    "headlight lens cover": ("body & lamp assembly", ["headlamp assembly"]),
    "headlight lens": ("body & lamp assembly", ["headlamp assembly"]),
    "headlight bezel": ("body & lamp assembly", ["headlamp assembly"]),
    "intercooler hose": ("cooling system", ["intercooler", "charge air", "turbo hose"]),
    "center bumper support": ("body & lamp assembly", ["bumper reinforcement", "bumper support"]),
    "front center bumper support": ("body & lamp assembly", ["bumper reinforcement", "bumper support"]),
    "electronic engine mount": ("engine", ["engine mount"]),
}
