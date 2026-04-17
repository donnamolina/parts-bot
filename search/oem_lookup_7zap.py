"""
7zap OEM Lookup — VIN-exact part number lookup via 7zap.com (TecDoc-powered).

Architecture:
  SevenZapClient  — async HTTP client, cookie auth, 3-attempt backoff on 5xx
  CatalogCache    — per-VIN JSON file, 7-day TTL for tree, 24h negative cache
  PartMatcher     — maps English part name → tree keywords → node IDs → fuzzy match
  lookup_oem_by_vin() — main entry point called by engine.py

Requires in .env:
  SEVENZAP_COOKIE_SESSION=<value of "7zap" cookie>
  SEVENZAP_COOKIE_REMEMBER=<name>=<value>  e.g. remember_web_abc123=<token>
  SEVENZAP_COOKIE_CF=<cf_clearance value>
  SEVENZAP_USER_AGENT=<Chrome UA string from DevTools>
  OEM_LOOKUP_SOURCE=7zap  # set to "rockauto" to disable
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger("parts-bot.7zap")

# ── Cache config ──────────────────────────────────────────────────────────────
_CACHE_DIR = Path(__file__).parent.parent / "cache" / "oem"
_VIN_CACHE_TTL_DAYS = 90
_NEGATIVE_TTL_HOURS = 24

# ── Fuzzy matching thresholds ─────────────────────────────────────────────────
_SCORE_GREEN = 85   # confident VIN-exact match
_SCORE_YELLOW = 75  # plausible but verify

# ── Korean/abbreviation normalization ────────────────────────────────────────
# Many catalogs (Hyundai, Kia, Toyota) use abbreviated part names.
# Expand before fuzzy scoring so "GRILLE ASSY-RADIATOR" scores well against
# "grille assembly" and "PANEL ASSY-FNDR APRON,LH" matches "left fender panel".
import re as _re_norm

_ABBREV = [
    # ── Chrysler / Jeep comma-structured names (must come before generic rules) ──
    # 7zap uses "LAMP, HEAD", "FASCIA, FRONT" etc. for FCA/Stellantis vehicles
    (r'\bFASCIA,\s*FRONT\b',  'front bumper cover'),
    (r'\bFASCIA,\s*REAR\b',   'rear bumper cover'),
    (r'\bFASCIA\b',           'bumper cover'),
    (r'\bLAMP,\s*HEAD\b',     'headlamp'),
    (r'\bLAMP,\s*FOG\b',      'fog lamp'),
    (r'\bLAMP,\s*TAIL\b',     'tail lamp'),
    (r'\bLAMP,\s*PARK\b',     'parking lamp'),
    (r'\bLAMP,\s*TURN\b',     'turn signal'),
    (r'\bLAMP,\s*BACKUP\b',   'backup lamp'),
    (r'\bLAMP,\s*STOP\b',     'stop lamp'),
    (r'\bLAMP,\s*DAYTIME\b',  'daytime running lamp'),
    (r'\bGRILL\b',            'grille'),          # Chrysler spells it GRILL
    (r'\bBEZEL,\s*HEAD\s*LAMP\b', 'headlamp bezel'),

    # Word-boundary replacements (order matters — longer first)
    (r'\bASSY\b',    'assembly'),
    (r'\bASSM\b',    'assembly'),
    (r'\bSUBASY\b',  'subassembly'),
    (r'\bHDLMP\b',   'headlamp'),
    (r'\bHDLAMP\b',  'headlamp'),
    (r'\bHEADLAMP\b','headlamp'),       # already English but normalise case
    (r'\bHEADLIGHT\b','headlamp'),     # normalise query "headlight" → "headlamp" to match catalog names
    (r'\bCOMB\b',    'combination'),
    (r'\bCOMBN\b',   'combination'),
    (r'\bFNDR\b',    'fender'),
    (r'\bFNDER\b',   'fender'),
    (r'\bBNPR\b',    'bumper'),
    (r'\bBMPR\b',    'bumper'),
    (r'\bMLDG\b',    'molding'),
    (r'\bMOLDG\b',   'molding'),
    (r'\bGARN\b',    'garnish'),
    (r'\bGRNSH\b',   'garnish'),
    (r'\bRADI\b',    'radiator'),
    (r'\bRAD\b',     'radiator'),
    (r'\bSUPT\b',    'support'),
    (r'\bSUPPT\b',   'support'),
    (r'\bBRKT\b',    'bracket'),
    (r'\bBRACKT\b',  'bracket'),
    (r'\bRINFCMT\b', 'reinforcement'),
    (r'\bRINF\b',    'reinforcement'),
    (r'\bREINF\b',   'reinforcement'),
    (r'\bSPRT\b',    'support'),
    (r'\bCVR\b',     'cover'),
    (r'\bCOVR\b',    'cover'),
    (r'\bPNL\b',     'panel'),
    (r'\bPANL\b',    'panel'),
    (r'\bHOOD\b',    'hood'),           # already English; covers HOOD in caps
    (r'\bINNR\b',    'inner'),
    (r'\bOUTR\b',    'outer'),
    (r'\bUPPR\b',    'upper'),
    (r'\bLWR\b',     'lower'),
    (r'\bFRT\b',     'front'),
    (r'\bFR\b',      'front'),
    (r'\bRR\b',      'rear'),
    (r'\bREAR\b',    'rear'),           # already English; normalise case
    (r'\bCTR\b',     'center'),
    (r'\bCENTR\b',   'center'),
    (r'\b LH\b',     ' left'),
    (r'\b RH\b',     ' right'),
    (r'\bLH\b',      'left'),
    (r'\bRH\b',      'right'),
    (r'\bLEFT\b',    'left'),
    (r'\bRIGHT\b',   'right'),
    (r'\bINSUL\b',   'insulator'),
    (r'\bINST\b',    'instrument'),
    (r'\bABS\b',     'abs'),            # keep as-is (brake system)
    (r'\bSENSR\b',   'sensor'),
    (r'\bSENS\b',    'sensor'),
    (r'\bMTG\b',     'mounting'),
    (r'\bMNT\b',     'mounting'),
    (r'\bHSG\b',     'housing'),
    (r'\bHOUSG\b',   'housing'),
    (r'\bEXHST\b',   'exhaust'),
    (r'\bEXH\b',     'exhaust'),
    (r'\bXMBR\b',    'crossmember'),
    (r'\bCROSSMBR\b','crossmember'),
    (r'\bSTRG\b',    'steering'),
    (r'\bSUSP\b',    'suspension'),
    (r'\bSHCKR\b',   'shocker'),
    (r'\bSPRG\b',    'spring'),
    (r'\bCLMP\b',    'clamp'),
]
_ABBREV_RE = [(_re_norm.compile(pat, _re_norm.IGNORECASE), repl) for pat, repl in _ABBREV]

def _normalize(name: str) -> str:
    """Expand Korean-spec and common automotive abbreviations before scoring."""
    s = name
    for rx, repl in _ABBREV_RE:
        s = rx.sub(repl, s)
    # Strip trailing punctuation, hyphens, slashes, codes like ",LH" after expansion
    s = _re_norm.sub(r'[-/,]+$', '', s).strip()
    return s


try:
    from rapidfuzz import fuzz as _rfuzz
    def _score(query: str, candidate: str) -> float:
        return _rfuzz.WRatio(_normalize(query).lower(), _normalize(candidate).lower())
except ImportError:
    logger.warning("rapidfuzz not installed — using word-overlap scoring")
    def _score(query: str, candidate: str) -> float:  # type: ignore[misc]
        q = set(_normalize(query).lower().split())
        c = set(_normalize(candidate).lower().split())
        if not q:
            return 0.0
        return len(q & c) / max(len(q), len(c)) * 100


# ── Hardware blocklist ────────────────────────────────────────────────────────
# When a query is for a main assembly (cover, panel, lamp, grille…), candidates
# whose normalized name contains these words are hardware/mounting parts and
# should be rejected — they'll score deceptively high on shared words like
# "bumper" or "front".
_HARDWARE_WORDS: frozenset[str] = frozenset({
    "bracket", "support", "retainer", "clip", "bolt", "nut", "screw",
    "washer", "seal", "gasket", "hose", "wire", "harness", "sensor",
    "switch", "relay", "module",
    # Additional hardware/trim types often confused with assemblies:
    "molding",    # trim strip — not a bumper cover or fender panel
    "isolator",   # rubber mount — not a radiator
    "insulator",  # heat/noise insulator
    "decal",      # sticker
    "rivet",      # fastener
    "plug",       # drain plug / body plug
    "label",      # informational sticker
    "grommet",    # rubber grommet
    "pin",        # push pin / body pin
    "pad",          # step pad, insulating pad — not the part assembly
    "step",         # step pad / step bar
    "rail",         # structural rail — not a front-end assembly
    "shield",       # heat shield / exhaust shield
    "skid",         # skid plate
    "applique",     # decorative applique
    "graphic",      # graphics kit
    "hitch",        # tow hitch cover — not a bumper cover
    "crossmember",  # structural crossmember — not a front-end assembly
    "draincock",    # radiator draincock
    "spat",         # FCA "SPAT, REAR" = exterior ornamentation trim, not a bumper panel
    "kit",          # decal kit, hardware kit — not a main assembly
    "nameplate",    # nameplate / emblem — not a panel or cover
})

# Query keywords that mark an assembly/main-component query → apply blocklist
_ASSEMBLY_QUERY_KW: frozenset[str] = frozenset({
    "cover", "assembly", "panel", "lamp", "headlight", "headlamp",
    "grille", "hood", "fascia", "fender", "door", "bumper",
    "mirror", "windshield", "tailgate", "trunk", "spoiler",
    "light", "fog light", "fog lamp",
    "radiator",   # prevents ISOLATOR/PLUG/LABEL from scoring above the actual radiator
})

# Query keywords for hardware parts → blocklist does NOT apply (reverse blocklist)
_HARDWARE_QUERY_KW: frozenset[str] = frozenset({
    "bracket", "support", "reinforcement", "retainer", "clip", "hinge",
    "latch", "absorber", "bushing", "mount", "gasket", "seal", "hose",
    "sensor", "switch", "harness", "wire", "bolt",
    "molding",    # "door molding", "body molding" queries should not be blocked
    "flare",      # "fender flare" query should not block moldings
    "slider",     # "headlight slider" is a sub-component, not a full headlamp
    "adjuster",   # "headlight adjuster" is a sub-component, not a full headlamp
    "guide",      # "bumper guide" is a sub-component, not a full bumper cover
    "lip",        # "bumper lip" is a sub-component, not a full bumper cover
})


def _is_assembly_query(part_name: str) -> bool:
    """True if query is for a main assembly — blocklist should apply."""
    lower = part_name.lower()
    # If query is explicitly for a hardware part, don't apply blocklist
    if any(kw in lower for kw in _HARDWARE_QUERY_KW):
        return False
    return any(kw in lower for kw in _ASSEMBLY_QUERY_KW)


def _apply_hardware_blocklist(candidates: list[dict], part_name: str) -> list[dict]:
    """Remove hardware/fastener candidates when the query is for an assembly.
    Falls back to the full list if filtering would leave zero candidates."""
    if not _is_assembly_query(part_name):
        return candidates

    def _is_hardware(c: dict) -> bool:
        norm = _normalize(c.get("part_name", "")).lower()
        # Strip all non-alpha chars (commas, hyphens, etc.) before splitting so
        # "MOLDING, WHEEL..." → {"molding", "wheel"...} instead of {"molding,", ...}
        words = set(_re_norm.sub(r'[^a-z]', ' ', norm).split())
        return bool(words & _HARDWARE_WORDS)

    filtered = [c for c in candidates if not _is_hardware(c)]
    if filtered:
        return filtered
    # All candidates were hardware — the main assembly isn't catalogued for this VIN.
    # Return empty so the threshold check yields NO RESULT instead of a false positive.
    logger.debug(
        f"7zap blocklist: all candidates were hardware for '{part_name}' — returning empty (no false positive)"
    )
    return []


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class OemLookupResult:
    oem_number: str | None = None
    part_name: str | None = None
    confidence: Literal["green", "yellow", "red"] = "red"
    source: Literal["7zap_vin_exact", "7zap_fuzzy", "rockauto_fallback", "name_only_fallback"] = "name_only_fallback"
    candidates: list[dict] = field(default_factory=list)
    error: str | None = None


class SevenZapAuthError(Exception):
    """Raised on 401/403 — cookies likely expired. Caller should fall back."""
    pass


# ── Part → 7zap category mapping ─────────────────────────────────────────────
# Each value is (top_level_keyword, subcategory_keyword) matched case-insensitively
# against the tree node names returned by vin_tree. Longest key matched first.

# Values are lists of (top_kw, sub_kw) pairs — tried in order until candidates found.
# This handles brand differences: Porsche fenders live under "Bumpers", Hyundai under "Panels".
PART_TO_7ZAP_CATEGORY: dict[str, list[tuple[str, str]]] = {
    # ── Body/Exterior → "Bumpers/Body Kit/Guards" ─────────────────────────────
    "front bumper cover":       [("body", "bumper")],
    "front bumper fascia":      [("body", "bumper")],
    "rear bumper fascia":       [("body", "bumper")],
    "bumper fascia":            [("body", "bumper")],
    "rear bumper cover":        [("body", "bumper")],
    "front bumper":             [("body", "bumper")],
    "rear bumper":              [("body", "bumper")],
    "bumper reinforcement":     [("body", "bumper")],
    "bumper absorber":          [("body", "bumper")],
    "bumper bracket":           [("body", "bumper")],
    "bumper cover":             [("body", "bumper")],
    "bumper":                   [("body", "bumper")],
    "rear valance":             [("body", "bumper")],
    "valance":                  [("body", "bumper")],
    "spoiler":                  [("body", "bumper")],

    # ── Running boards / step bars — accessory, often not in 7zap catalog ─────
    "running board":             [("body", "panel"), ("accessories", "accessories")],
    "step bar":                  [("body", "panel"), ("accessories", "accessories")],
    "side step":                 [("body", "panel"), ("accessories", "accessories")],

    # ── Fender: Porsche=bumper section, Hyundai=panel section ────────────────
    "fender panel":             [("body", "bumper"), ("body", "panel")],
    "fender liner":             [("body", "bumper"), ("body", "panel")],
    "fender flare":             [("body", "bumper"), ("body", "panel")],
    "inner fender":             [("body", "bumper"), ("body", "panel")],
    "fender":                   [("body", "bumper"), ("body", "panel")],

    # ── Front end / grille: bumper section, fallback to panel ─────────────────
    "front end assembly":       [("body", "bumper"), ("body", "panel")],
    "front clip":               [("body", "bumper"), ("body", "panel")],
    "grille assembly":          [("body", "bumper"), ("body", "panel")],
    "grille":                   [("body", "bumper"), ("body", "panel")],
    "radiator support":         [("body", "panel"), ("body", "bumper")],

    # ── Body/Exterior → "Panels/Structural Elements" ──────────────────────────
    "hood hinges":              [("body", "panel")],
    "hood latch":               [("body", "panel")],
    "hood panel":               [("body", "panel")],
    "hood assembly":            [("body", "panel")],
    "hood":                     [("body", "panel")],
    "roof panel":               [("body", "panel")],
    "rocker panel":             [("body", "panel")],

    # ── Body/Exterior → "Doors/Locks/Windows" ────────────────────────────────
    "door assembly":            [("body", "door")],
    "door handle":              [("body", "door")],
    "door molding":             [("body", "door")],
    "door lock":                [("body", "door")],
    "door":                     [("body", "door")],
    "window regulator":         [("body", "door")],
    "trunk lid":                [("body", "door")],
    "tailgate":                 [("body", "door")],

    # ── Headlights: exterior lighting section, fallback electrical lighting ───
    "headlight assembly":       [("body", "lighting"), ("electrical", "lighting")],
    "headlight":                [("body", "lighting"), ("electrical", "lighting")],
    "headlamp":                 [("body", "lighting"), ("electrical", "lighting")],
    "headlight bracket":        [("body", "lighting")],
    "tail light assembly":      [("body", "lighting"), ("electrical", "lighting")],
    "tail light":               [("body", "lighting"), ("electrical", "lighting")],
    "third brake light":        [("body", "lighting"), ("electrical", "lighting")],
    "license plate light":      [("body", "lighting"), ("electrical", "lighting")],
    "backup light":             [("body", "lighting"), ("electrical", "lighting")],
    "fog light":                [("body", "lighting"), ("electrical", "lighting")],
    "fog lamp":                 [("body", "lighting"), ("electrical", "lighting")],
    "turn signal":              [("body", "lighting"), ("electrical", "lighting")],
    "side marker":              [("body", "lighting"), ("electrical", "lighting")],

    # ── Body/Exterior → "Glass/Mirrors/Seals" ────────────────────────────────
    "windshield":               [("body", "glass")],
    "side mirror":              [("body", "glass"), ("body", "door")],
    "mirror glass":             [("body", "glass")],
    "mirror":                   [("body", "glass")],
    "rearview mirror":          [("body", "glass")],

    # ── Interior/Safety → "Airbags/SRS" ──────────────────────────────────────
    "curtain airbag":           [("interior", "airbag")],
    "driver airbag":            [("interior", "airbag")],
    "passenger airbag":         [("interior", "airbag")],
    "steering wheel airbag":    [("interior", "airbag")],
    "side airbag":              [("interior", "airbag")],
    "seat airbag":              [("interior", "airbag")],
    "knee airbag":              [("interior", "airbag")],
    "roof airbag":              [("interior", "airbag")],
    "airbag":                   [("interior", "airbag")],

    # ── Interior/Safety → "Seats/Seatbelts" ──────────────────────────────────
    "seatbelt pretensioner":    [("interior", "seat")],
    "pretensioner":             [("interior", "seat")],
    "front seatbelt":           [("interior", "seat")],
    "rear seatbelt":            [("interior", "seat")],
    "seatbelt":                 [("interior", "seat")],

    # ── Engine → "Cooling System" ─────────────────────────────────────────────
    "radiator hose":            [("engine", "cool")],
    "radiator fan":             [("engine", "cool")],
    "cooling fan":              [("engine", "cool")],
    "thermostat":               [("engine", "cool")],
    "water pump":               [("engine", "cool")],
    "intercooler":              [("engine", "cool")],
    "radiator":                 [("engine", "cool")],
    "ac condenser":             [("engine", "cool")],

    # ── Engine → "Exhaust/Emission Control" ──────────────────────────────────
    "catalytic converter":      [("engine", "exhaust")],
    "exhaust manifold":         [("engine", "exhaust")],
    "oxygen sensor":            [("engine", "exhaust")],
    "muffler":                  [("engine", "exhaust")],

    # ── Engine → "Intake/Turbocharging" ──────────────────────────────────────
    "turbocharger":             [("engine", "intake")],
    "intake manifold":          [("engine", "intake")],

    # ── Engine → misc ─────────────────────────────────────────────────────────
    "timing chain":             [("engine", "cylinder")],
    "timing belt":              [("engine", "cylinder")],
    "valve cover":              [("engine", "cylinder")],
    "oil pan":                  [("engine", "lubric")],
    "oil pump":                 [("engine", "lubric")],
    "engine mount":             [("engine", "engine")],
    "ac compressor":            [("electrical", "climate")],

    # ── Chassis Systems → "Suspension" ───────────────────────────────────────
    "lower control arm":        [("chassis", "suspension")],
    "upper control arm":        [("chassis", "suspension")],
    "control arm":              [("chassis", "suspension")],
    "strut assembly":           [("chassis", "suspension")],
    "strut mount":              [("chassis", "suspension")],
    "strut":                    [("chassis", "suspension")],
    "shock absorber":           [("chassis", "suspension")],
    "sway bar link":            [("chassis", "suspension")],
    "sway bar":                 [("chassis", "suspension")],
    "stabilizer link":          [("chassis", "suspension")],
    "coil spring":              [("chassis", "suspension")],
    "wheel hub assembly":       [("chassis", "wheel")],
    "wheel bearing":            [("chassis", "wheel")],
    "ball joint":               [("chassis", "suspension")],
    "bushing":                  [("chassis", "suspension")],
    "subframe":                 [("chassis", "suspension")],
    "hub assembly":             [("chassis", "wheel")],

    # ── Chassis Systems → "Brake System" ─────────────────────────────────────
    "brake master cylinder":    [("chassis", "brake")],
    "brake caliper":            [("chassis", "brake")],
    "brake rotor":              [("chassis", "brake")],
    "brake disc":               [("chassis", "brake")],
    "brake pad":                [("chassis", "brake")],
    "brake hose":               [("chassis", "brake")],
    "brake shoe":               [("chassis", "brake")],
    "abs sensor":               [("chassis", "brake")],

    # ── Chassis Systems → "Steering" ─────────────────────────────────────────
    "power steering pump":      [("chassis", "steering")],
    "steering rack":            [("chassis", "steering")],
    "tie rod end":              [("chassis", "steering")],
    "tie rod":                  [("chassis", "steering")],

    # ── Transmission/Drivetrain ───────────────────────────────────────────────
    "cv axle":                  [("transmission", "axle")],
    "axle shaft":               [("transmission", "axle")],
    "cv joint":                 [("transmission", "axle")],
    "flywheel":                 [("transmission", "gearbox")],
    "clutch":                   [("transmission", "clutch")],

    # ── Electrical/Electronic → "12V Power/Starting/Charging" ────────────────
    "alternator":               [("electrical", "power")],
    "starter":                  [("electrical", "power")],
    "ignition coil":            [("electrical", "wiring")],
    "spark plug":               [("electrical", "wiring")],

    # ── Electrical/Electronic → "Visibility" (wipers) ────────────────────────
    "wiper blade":              [("electrical", "visib")],
    "wiper motor":              [("electrical", "visib")],

    # ── New: grille / lip / fog / lens / duct / mount ────────────────────────
    "front lower bumper grille": [("body", "bumper")],
    "rear lower bumper grille":  [("body", "bumper")],
    "lower bumper grille":       [("body", "bumper")],
    "lower grille":              [("body", "bumper"), ("body", "panel")],
    "front bumper lip":          [("body", "bumper")],
    "rear bumper lip":           [("body", "bumper")],
    "bumper lip":                [("body", "bumper")],
    "bumper guide bracket":      [("body", "bumper")],
    "front fog light":           [("body", "lighting"), ("electrical", "lighting")],
    "upper fog light":           [("body", "lighting"), ("electrical", "lighting")],
    "lower fog light":           [("body", "lighting"), ("electrical", "lighting")],
    "halogen headlight":         [("body", "lighting"), ("electrical", "lighting")],
    "headlight lens cover":      [("body", "lighting")],
    "headlight lens":            [("body", "lighting")],
    "headlight bezel":           [("body", "lighting")],
    "headlight adjuster":        [("body", "lighting")],
    "headlight mounting bracket": [("body", "lighting")],
    "intercooler hose":          [("engine", "cool"), ("engine", "intake")],
    "center bumper support":     [("body", "bumper")],
    "electronic engine mount":   [("engine", "engine")],
    "running board":             [("body", "bumper"), ("body", "panel")],
    "rear bumper reflector":     [("body", "lighting"), ("body", "bumper")],
    "front bumper reflector":    [("body", "lighting"), ("body", "bumper")],
    "bumper reflector":          [("body", "lighting"), ("body", "bumper")],
}

# Sorted by key length descending for longest-match-first lookup
_SORTED_CATEGORY_KEYS = sorted(PART_TO_7ZAP_CATEGORY, key=len, reverse=True)


def _map_to_categories(part_name: str) -> list[tuple[str, str]]:
    """Return ordered list of (top_kw, sub_kw) to try for this part name."""
    lower = part_name.lower().strip()
    for key in _SORTED_CATEGORY_KEYS:
        if key in lower:
            return PART_TO_7ZAP_CATEGORY[key]
    return []


# ── Catalog cache ─────────────────────────────────────────────────────────────

class CatalogCache:
    """Per-VIN JSON file cache. 7-day TTL for tree data; 24h negative cache."""

    def __init__(self, vin: str):
        self.vin = vin
        self.path = _CACHE_DIR / f"{vin}.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self):
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))

    def is_tree_fresh(self) -> bool:
        ts = self._data.get("fetched_at")
        if not ts:
            return False
        try:
            age = datetime.now() - datetime.fromisoformat(ts)
            return age.days < _VIN_CACHE_TTL_DAYS
        except Exception:
            return False

    def get_tree(self) -> dict | None:
        return self._data.get("vin_tree")

    def store_tree(self, tree: dict):
        self._data["fetched_at"] = datetime.now().isoformat()
        self._data["vin_tree"] = tree
        self._save()

    def get_node_parts(self, node_id: str) -> list | None:
        return self._data.get("nodes", {}).get(str(node_id))

    def store_node_parts(self, node_id: str, parts: list):
        self._data.setdefault("nodes", {})[str(node_id)] = parts
        self._save()

    def is_negative(self, part_key: str) -> bool:
        ts = self._data.get("negative_cache", {}).get(part_key)
        if not ts:
            return False
        try:
            age = datetime.now() - datetime.fromisoformat(ts)
            return age.total_seconds() < _NEGATIVE_TTL_HOURS * 3600
        except Exception:
            return False

    def store_negative(self, part_key: str):
        self._data.setdefault("negative_cache", {})[part_key] = datetime.now().isoformat()
        self._save()


# ── HTTP client ───────────────────────────────────────────────────────────────

class SevenZapClient:
    _BASE = "https://7zap.com/api/catalog"

    def __init__(self):
        session_val = os.getenv("SEVENZAP_COOKIE_SESSION", "")
        cf_val = os.getenv("SEVENZAP_COOKIE_CF", "")
        remember_raw = os.getenv("SEVENZAP_COOKIE_REMEMBER", "")
        ua = os.getenv(
            "SEVENZAP_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36",
        )
        cookies: dict[str, str] = {}
        if session_val:
            cookies["7zap"] = session_val
        if cf_val:
            cookies["cf_clearance"] = cf_val
        if remember_raw:
            if remember_raw.startswith("remember_web_") and "=" in remember_raw:
                # Full "remember_web_<hash>=<value>" format
                name, _, val = remember_raw.partition("=")
                if name and val:
                    cookies[name.strip()] = val.strip()
            else:
                # Value-only format — use generic cookie name
                cookies["remember_web"] = remember_raw
        self._cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        self._headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://7zap.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": self._cookie_str,
        }

    async def _get(self, endpoint: str, params: dict) -> dict:
        relay_url = os.getenv("SEVENZAP_RELAY_URL", "").rstrip("/")
        relay_token = os.getenv("SEVENZAP_RELAY_TOKEN", "")

        if relay_url:
            # Route through Mac mini relay — avoids Cloudflare IP binding on DO server
            target = f"https://7zap.com/api/catalog/{endpoint}"
            relay_params = {"_url": target, **params}
            relay_headers = {"X-Relay-Token": relay_token} if relay_token else {}
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.get(
                            f"{relay_url}/proxy",
                            params=relay_params,
                            headers=relay_headers,
                        )
                    if resp.status_code in (401, 403):
                        raise SevenZapAuthError(
                            f"Relay returned {resp.status_code} — cf_clearance on Mac mini expired"
                        )
                    if resp.status_code >= 500:
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise RuntimeError(f"Relay error {resp.status_code} after 3 attempts")
                    resp.raise_for_status()
                    return resp.json()
                except SevenZapAuthError:
                    raise
                except Exception as exc:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"Relay request to {endpoint} failed: {exc}") from exc
            return {}

        # Direct path (only works if this machine's IP has a valid cf_clearance)
        url = f"{self._BASE}/{endpoint}"
        proxy = os.getenv("SEVENZAP_PROXY") or os.getenv("ROCKAUTO_PROXY")
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=15.0,
                    follow_redirects=True,
                    proxy=proxy or None,
                ) as client:
                    resp = await client.get(url, params=params, headers=self._headers)
                if resp.status_code in (401, 403):
                    raise SevenZapAuthError(
                        f"7zap returned {resp.status_code} — cookies expired or invalid"
                    )
                if resp.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"7zap server error {resp.status_code} after 3 attempts")
                resp.raise_for_status()
                return resp.json()
            except SevenZapAuthError:
                raise
            except Exception as exc:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"7zap request to {endpoint} failed: {exc}") from exc
        return {}

    async def get_vin_tree(self, vin: str) -> dict:
        return await self._get("vin_tree", {
            "language": "en",
            "vin": vin,
            "modification_number": "-",
            "cc": "0",
            "page": "1",
        })

    async def get_parts_by_node(self, vin: str, node_id: str) -> list:
        data = await self._get("vin_parts_by_id", {
            "language": "en",
            "vin": vin,
            "node_id": node_id,
            "modification_number": "-",
            "page": "1",
        })
        return data.get("parts", [])


# ── Tree traversal ────────────────────────────────────────────────────────────

def _find_node_ids(tree: dict, top_kw: str, sub_kw: str) -> list[str]:
    """Return node IDs whose ancestor names contain the given keywords."""
    ids: list[str] = []
    for top in tree.get("tree", []):
        if top_kw not in top.get("name", "").lower():
            continue
        for child in top.get("children", []):
            if sub_kw not in child.get("name", "").lower():
                continue
            for node in child.get("nodes", []):
                if node.get("id"):
                    ids.append(str(node["id"]))
    return ids


# ── Part variant selection ────────────────────────────────────────────────────

def _resolve_replacement(part: dict) -> dict:
    """If part has a replacement_history, substitute the replacement part_code."""
    rh = part.get("replacement_history")
    if not rh:
        return part
    replacements = rh.get("replacements", []) if isinstance(rh, dict) else []
    if replacements and replacements[0].get("part_number"):
        return {**part, "part_code": replacements[0]["part_number"]}
    return part


def _pick_variant(variants: list[dict]) -> dict | None:
    """Select the best variant from a group sharing the same part_number_in_scheme.

    Priority:
      1. Follow replacement_history if set
      2. Skip discontinued unless nothing else exists
      3. Prefer variant with no option-code annotation (most universal)
      4. Among annotated, prefer the one with more codes (broader compatibility)
      5. Prefer non-primed variant (no G2X suffix) over primed
    """
    if not variants:
        return None

    resolved = [_resolve_replacement(p) for p in variants]

    # Filter discontinued
    active = [p for p in resolved if "discontinued" not in (p.get("part_name") or "").lower()]
    if not active:
        active = resolved

    # Prefer no-annotation
    no_ann = [p for p in active if not (p.get("annotation") or "").strip()]
    pool = no_ann if no_ann else sorted(
        active,
        key=lambda p: len((p.get("annotation") or "").split(",")),
        reverse=True,
    )

    # Prefer non-primed (no G2X suffix)
    non_primed = [p for p in pool if not (p.get("part_code") or "").upper().endswith("G2X")]
    return (non_primed or pool)[0]


def _parse_side(info: str | None) -> str | None:
    lower = (info or "").lower()
    if "left" in lower:
        return "left"
    if "right" in lower:
        return "right"
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def lookup_oem_by_vin(
    vin: str,
    part_name_english: str,
    make_hint: str | None = None,
) -> OemLookupResult:
    """
    VIN-exact OEM part number lookup via 7zap.com.

    Returns OemLookupResult. Never raises — errors are returned in result.error.
    Raises SevenZapAuthError if cookies are expired (caller should fall back to RockAuto).
    """
    if not vin or len(vin) != 17:
        return OemLookupResult(error=f"Invalid VIN: '{vin}'")

    if os.getenv("OEM_LOOKUP_SOURCE", "7zap").lower() != "7zap":
        return OemLookupResult(error="7zap disabled by OEM_LOOKUP_SOURCE=rockauto")

    # Relay mode doesn't need local cookies — relay server holds them
    _using_relay = bool(os.getenv("SEVENZAP_RELAY_URL"))
    if not _using_relay and not os.getenv("SEVENZAP_COOKIE_SESSION"):
        return OemLookupResult(error="SEVENZAP_COOKIE_SESSION not configured")

    cache = CatalogCache(vin)
    part_key = part_name_english.lower().strip()

    if cache.is_negative(part_key):
        return OemLookupResult(error=f"No 7zap match (negative cache): '{part_name_english}'")

    categories = _map_to_categories(part_name_english)
    if not categories:
        logger.debug(f"7zap: no category mapping for '{part_name_english}'")
        cache.store_negative(part_key)
        return OemLookupResult(error=f"No 7zap category for '{part_name_english}'")

    client = SevenZapClient()

    try:
        # ── Load or fetch VIN tree ──
        if not cache.is_tree_fresh():
            logger.info(f"7zap: fetching VIN tree for {vin}")
            tree = await client.get_vin_tree(vin)
            if not tree.get("tree"):
                return OemLookupResult(error=f"7zap returned empty tree for VIN {vin}")
            cache.store_tree(tree)
        else:
            tree = cache.get_tree() or {}

        # ── Collect node IDs — try each category in order, merge unique IDs ──
        node_ids: list[str] = []
        tried_categories: list[str] = []
        for top_kw, sub_kw in categories:
            ids = _find_node_ids(tree, top_kw, sub_kw)
            for nid in ids:
                if nid not in node_ids:
                    node_ids.append(nid)
            tried_categories.append(f"({top_kw}, {sub_kw})")

        if not node_ids:
            logger.debug(f"7zap: no nodes for {tried_categories} — '{part_name_english}'")
            cache.store_negative(part_key)
            return OemLookupResult(error=f"No 7zap tree nodes for {tried_categories}")

        # ── Determine requested side/position ──
        lower = part_name_english.lower()
        req_side: str | None = None
        if any(w in lower for w in ("left", "izquierdo", " lh", "driver side")):
            req_side = "left"
        elif any(w in lower for w in ("right", "derecho", " rh", "passenger side")):
            req_side = "right"

        # ── Fetch node parts and collect scored candidates ──
        candidates: list[dict] = []

        for node_id in node_ids[:20]:  # cap nodes to avoid hammering
            node_parts = cache.get_node_parts(node_id)
            if node_parts is None:
                try:
                    node_parts = await client.get_parts_by_node(vin, node_id)
                    cache.store_node_parts(node_id, node_parts)
                except Exception as e:
                    logger.warning(f"7zap node {node_id} fetch error: {e}")
                    continue

            # Keep only real parts (not annotation rows)
            real = [p for p in node_parts if p.get("type") == "part"]

            # Group by position number, pick best variant per position
            by_position: dict[str, list] = {}
            for p in real:
                pos = str(p.get("part_number_in_scheme") or p.get("part_code", ""))
                by_position.setdefault(pos, []).append(p)

            for _, variants in by_position.items():
                # If variants in this position have different sides (e.g. left/right
                # headlamps share a diagram position), keep each side as its own
                # candidate so the side filter can correctly select left or right.
                variant_sides = [_parse_side(v.get("info")) for v in variants]
                distinct_sides = set(s for s in variant_sides if s is not None)
                if len(distinct_sides) > 1:
                    # Multi-side group — emit a candidate for each distinct side
                    for v, side in zip(variants, variant_sides):
                        v = _resolve_replacement(v)
                        code = v.get("part_code", "")
                        name = v.get("part_name", "")
                        if not code or not name:
                            continue
                        candidates.append({
                            "oem_number": code,
                            "part_name": name,
                            "side": side,
                            "score": _score(part_name_english, name),
                            "node_id": node_id,
                            "annotation": v.get("annotation", ""),
                            "other_variants": [],
                        })
                else:
                    pick = _pick_variant(variants)
                    if not pick:
                        continue
                    code = pick.get("part_code", "")
                    name = pick.get("part_name", "")
                    if not code or not name:
                        continue
                    candidates.append({
                        "oem_number": code,
                        "part_name": name,
                        "side": _parse_side(pick.get("info")),
                        "score": _score(part_name_english, name),
                        "node_id": node_id,
                        "annotation": pick.get("annotation", ""),
                        "other_variants": [v.get("part_code") for v in variants if v.get("part_code") != code],
                    })

        if not candidates:
            cache.store_negative(part_key)
            return OemLookupResult(error=f"No parts returned from 7zap nodes for '{part_name_english}'")

        # ── Hardware blocklist: reject brackets/fasteners for assembly queries ──
        candidates = _apply_hardware_blocklist(candidates, part_name_english)
        if not candidates:
            cache.store_negative(part_key)
            return OemLookupResult(
                error=f"No 7zap match after hardware filter for '{part_name_english}' "
                      f"(main assembly not catalogued for this VIN)"
            )

        # ── Lighting pre-filter: keep only front or rear lights to avoid confusion ──
        # The "Exterior Lighting" tree node contains both headlights and tail lights.
        # Without filtering, a right-headlight query scores tail light parts higher
        # because they share "light" and "right" tokens.
        _req_lower = part_name_english.lower()
        _FRONT_LIGHT_KW = ("headlight", "headlamp", "led headlight", "halogen headlight",
                           "fog light", "fog lamp", "turn signal", "side marker",
                           "license plate light", "backup light", "daytime running")
        _REAR_LIGHT_KW  = ("tail light", "tail lamp", "rear light", "stop light",
                           "brake light", "reversing light", "reverse light")

        _is_front_light_query = any(kw in _req_lower for kw in _FRONT_LIGHT_KW)
        _is_rear_light_query  = any(kw in _req_lower for kw in _REAR_LIGHT_KW)

        if _is_front_light_query or _is_rear_light_query:
            def _candidate_name_lower(c: dict) -> str:
                return (c.get("part_name") or "").lower()

            if _is_front_light_query:
                # Keep candidates whose name suggests a front/forward light.
                # Check both raw name and normalized name to catch "HDLMP ASSY" → "headlamp assembly".
                _filtered = [
                    c for c in candidates
                    if any(kw in _candidate_name_lower(c) for kw in
                           ("headlight", "headlamp", "led headlight", "halogen",
                            "fog light", "fog lamp", "turn signal", "marker light",
                            "daytime", "running light", "front light", "spotlight",
                            "bracket for headlight", "repair set", "head lamp",
                            "head light", "drl", "combination lamp front"))
                    or any(kw in _normalize(_candidate_name_lower(c)) for kw in
                           ("headlamp", "headlight"))
                ]
            else:
                # Keep candidates whose name suggests a rear light.
                # Explicitly exclude side turn signals / side markers — they live in
                # the same lighting node but are NOT tail lights.
                _TAIL_INCLUDE = ("tail light", "tail lamp", "rear light", "stop light",
                                 "brake light", "reversing light", "reverse light",
                                 "rear lamp", "reflector", "combination lamp rear",
                                 "rear combination")
                _TAIL_EXCLUDE = ("turn signal", "side turn", "side marker",
                                 "side lamp", "marker lamp", "front turn",
                                 "side light", "side repeater")
                _filtered = [
                    c for c in candidates
                    if (any(kw in _candidate_name_lower(c) for kw in _TAIL_INCLUDE)
                        and not any(kw in _candidate_name_lower(c) for kw in _TAIL_EXCLUDE))
                ]

            # Only apply filter if it leaves at least one candidate
            if _filtered:
                candidates = _filtered
            else:
                logger.info(
                    f"7zap: lighting pre-filter returned 0 for '{part_name_english}' — "
                    "keeping all candidates"
                )

        # ── Lighting penalty: cross-type penalty after pre-filter ──
        # If query is headlight but a surviving candidate still looks like a rear light,
        # knock -30 off its score (and vice-versa).
        if _is_front_light_query:
            _REAR_PENALTY_KW = ("rear light", "tail light", "tail lamp", "stop light",
                                "reversing light", "reverse light", "brake light", "rear lamp")
            for c in candidates:
                if any(kw in (c.get("part_name") or "").lower() for kw in _REAR_PENALTY_KW):
                    c["score"] = max(0.0, c["score"] - 30)
        elif _is_rear_light_query:
            _FRONT_PENALTY_KW = ("headlight", "headlamp", "led headlight", "halogen",
                                 "fog light", "fog lamp", "daytime running")
            for c in candidates:
                if any(kw in (c.get("part_name") or "").lower() for kw in _FRONT_PENALTY_KW):
                    c["score"] = max(0.0, c["score"] - 30)

        # ── Front/rear position mismatch penalty ──
        # If the query explicitly says "front", penalise candidates whose original
        # (pre-normalisation) name clearly says "rear" (and vice-versa).
        _ql = part_name_english.lower()
        _query_is_front = any(w in _ql for w in ("front", "delantero", " fr ", "frt"))
        _query_is_rear  = any(w in _ql for w in ("rear", "trasero", " rr "))
        if _query_is_front or _query_is_rear:
            for c in candidates:
                orig = (c.get("part_name") or "").lower()
                norm_c = _normalize(orig).lower()
                # Check both original name and normalised name for direction words
                _cand_is_rear  = any(w in orig for w in ("rear", "rr", "tail", "back")) or \
                                  "rear" in norm_c
                _cand_is_front = any(w in orig for w in ("front", "fr ", "frt", "head")) or \
                                  "front" in norm_c
                if _query_is_front and _cand_is_rear and not _cand_is_front:
                    c["score"] = max(0.0, c["score"] - 35)
                elif _query_is_rear and _cand_is_front and not _cand_is_rear:
                    c["score"] = max(0.0, c["score"] - 35)

        # ── Context mismatch penalties ──
        for c in candidates:
            cname = (c.get("part_name") or "").lower()
            # "license plate" candidate for non-license queries
            if "license" not in _req_lower and "plate" not in _req_lower:
                if "license" in cname:
                    c["score"] = max(0.0, c["score"] - 50)
            # "door" molding candidate when query is for a fender/flare/bumper part
            if "fender" in _req_lower or "flare" in _req_lower:
                if "door" in cname and "fender" not in cname and "flare" not in cname:
                    c["score"] = max(0.0, c["score"] - 50)

        # ── Hardware query / assembly candidate mismatch penalty ──
        # If the query is for a sub-component (bracket, adjuster, slider, mount)
        # but the top candidate is the full assembly (no hardware words in its name),
        # penalise -50 so it falls below threshold and eBay falls back to name search.
        _hw_query_words = {"bracket", "adjuster", "slider", "mount", "mounting",
                           "retainer", "clip", "hinge", "latch", "absorber", "reflector"}
        _query_hw_tokens = set(_re_norm.sub(r'[^a-z]', ' ', part_name_english.lower()).split())
        if _query_hw_tokens & _hw_query_words:
            for c in candidates:
                cand_norm_words = set(
                    _re_norm.sub(r'[^a-z]', ' ', _normalize(c.get("part_name", "")).lower()).split()
                )
                if not (cand_norm_words & _hw_query_words):
                    c["score"] = max(0.0, c["score"] - 50)

        # ── Bug 1: Assembly-query preference — hard-exclude structural sub-components,
        # soft-prefer ASSY/COMPLETE candidates when the query is for the whole assembly.
        # Covers bumper, hood, door, fender, grille, mirror, tail light, headlight, etc.
        # ────────────────────────────────────────────────────────────────────────────────
        _FULL_ASSEMBLY_QUERIES: frozenset[str] = frozenset({
            "bumper", "rear bumper", "front bumper",
            "hood", "door", "fender", "grille",
            "tail light", "tail lamp", "headlight", "headlamp",
            "mirror", "running board", "side step", "fog light",
            "turn signal", "trunk", "tailgate",
        })
        # Parts that signal a structural sub-component — HARD exclude when querying for assembly
        _SUBCOMP_HARD_EXCLUDE: tuple[str, ...] = (
            "reinforcement", "rebar", "absorber", "energy absorber",
            "brace", "stay", "mounting", "sub-assy", "subassy",
            "filler", "spoiler", "moulding", "emblem", "badge", "guard",
        )
        # Assembly indicator keywords — SOFT prefer (+15)
        _ASSY_PREFER_KW: tuple[str, ...] = ("assy", "assembly", "complete", "comp")

        _ql_lower = part_name_english.lower()
        _is_full_assy_query = any(kw == _ql_lower or _ql_lower.endswith(" " + kw) or _ql_lower.startswith(kw + " ")
                                  for kw in _FULL_ASSEMBLY_QUERIES) or \
                              any(kw in _ql_lower for kw in _FULL_ASSEMBLY_QUERIES)
        # Don't apply if query is explicitly for a sub-component
        if any(kw in _ql_lower for kw in _HARDWARE_QUERY_KW):
            _is_full_assy_query = False

        if _is_full_assy_query:
            _filtered_cands = []
            for c in candidates:
                cname = (c.get("part_name") or "").lower()
                cnorm = _normalize(cname).lower()
                # Hard-exclude structural sub-components.
                # Check BOTH original name and normalized name — normalization expands
                # "SUB-ASSY" → "SUB-assembly", so "sub-assy" only matches in the original.
                if (any(kw in cname for kw in _SUBCOMP_HARD_EXCLUDE) or
                        any(kw in cnorm for kw in _SUBCOMP_HARD_EXCLUDE)):
                    logger.debug(f"7zap assy filter: hard-excluding '{c['part_name']}' (sub-component)")
                    continue
                # Soft-prefer assembly-level parts
                if any(kw in cnorm for kw in _ASSY_PREFER_KW):
                    c["score"] = min(100.0, c["score"] + 15)
                    logger.debug(f"7zap assy filter: +15 boost for '{c['part_name']}' score→{c['score']:.0f}")
                _filtered_cands.append(c)

            # Cross-word check: for specific assembly queries, also require the candidate
            # name to contain the primary assembly noun. Prevents "STRIPE, REAR BODY" or
            # "GATE SUB-ASSY" from winning a "rear bumper" query when the actual assembly
            # is not in the catalog for this VIN.
            _ASSEMBLY_PRIMARY_WORDS: dict[str, tuple[str, ...]] = {
                "bumper": ("bumper",),
                "hood": ("hood", "bonnet"),
                "fender": ("fender", "wing"),
                "grille": ("grille", "grill", "grp"),
                "headlight": ("headlight", "headlamp", "lamp"),
                "tail light": ("tail", "lamp", "light"),
                "mirror": ("mirror",),
            }
            for _assy_kw, _primary_words in _ASSEMBLY_PRIMARY_WORDS.items():
                if _assy_kw in _ql_lower:
                    _cross_filtered = [
                        c for c in _filtered_cands
                        if any(pw in (c.get("part_name") or "").lower() for pw in _primary_words)
                    ]
                    if _cross_filtered:
                        _filtered_cands = _cross_filtered
                        logger.debug(f"7zap cross-word filter: {len(_filtered_cands)} candidates kept for '{_assy_kw}'")
                    break  # only apply one cross-filter

            if _filtered_cands:
                candidates = _filtered_cands
            else:
                # No valid assembly candidates — return empty so engine falls back to
                # name-based eBay search instead of returning a wrong sub-component.
                cache.store_negative(part_key)
                return OemLookupResult(
                    error=f"7zap: no full assembly found for '{part_name_english}' "
                          "(only sub-components in catalog for this VIN)"
                )

        # ── Bumper assembly: also penalise cosmetic sub-components (cover/cap/trim) ──
        # Softer penalty (-40) rather than hard-exclude — these may be the only option.
        _bumper_assembly_query = (
            "bumper" in _ql_lower
            and "cover" not in _ql_lower
            and "fascia" not in _ql_lower
            and "cap" not in _ql_lower
        )
        if _bumper_assembly_query:
            _SUB_KW = ("cover", "cap", "upr", "lower", "upper", "plate",
                       "trim", "extension", "end", "insert", "deflector",
                       "absorber step", "step pad")
            for c in candidates:
                cname = (c.get("part_name") or "").lower()
                cnorm = _normalize(cname).lower()
                _is_sub = any(kw in cname for kw in _SUB_KW)
                # Only consider it an assembly if "bumper" is the primary noun
                # (first word before any comma).
                _first_word = cnorm.split(",")[0].strip().split()
                _first_word = _first_word[0] if _first_word else ""
                _is_assy = (
                    _first_word == "bumper"
                    or "bumper assy" in cnorm
                    or "bumper assembly" in cnorm
                )
                if _is_sub and not _is_assy:
                    c["score"] = max(0.0, c["score"] - 40)
                    logger.debug(
                        f"7zap bumper cover penalty: '{c['part_name']}' score→{c['score']:.0f}"
                    )

        # ── Running board / step bar: reject candidates without step-related keywords ──
        # 7zap catalogs running boards as accessories; if only structural parts were found,
        # return no result so the pipeline falls back to name-based eBay search.
        _step_query_kw = ("running board", "step bar", "side step", "nerf bar")
        if any(kw in part_name_english.lower() for kw in _step_query_kw):
            _STEP_NAME_KW = ("board", "step", "bar", "nerf", "sill", "tube", "running")
            _step_cands = [
                c for c in candidates
                if any(kw in (c.get("part_name") or "").lower() for kw in _STEP_NAME_KW)
            ]
            if _step_cands:
                candidates = _step_cands
            else:
                cache.store_negative(part_key)
                return OemLookupResult(
                    error=f"7zap: no running board/step parts found for this VIN (catalog may not include accessories)"
                )

        # ── Side filter ──
        if req_side:
            side_specific = [c for c in candidates if c["side"] == req_side]
            if side_specific:
                candidates = side_specific
            else:
                logger.info(
                    f"7zap: no {req_side}-specific part for '{part_name_english}' — using generic"
                )

        # ── Rank by score ──
        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        score = best["score"]

        if score < _SCORE_YELLOW:
            cache.store_negative(part_key)
            return OemLookupResult(
                error=f"7zap best match score {score:.0f} < threshold for '{part_name_english}'",
                candidates=candidates[:3],
            )

        confidence: Literal["green", "yellow", "red"] = "green" if score >= _SCORE_GREEN else "yellow"
        source: Literal["7zap_vin_exact", "7zap_fuzzy", "rockauto_fallback", "name_only_fallback"] = (
            "7zap_vin_exact" if confidence == "green" else "7zap_fuzzy"
        )

        logger.info(
            f"7zap: '{part_name_english}' → {best['oem_number']} "
            f"({best['part_name']}, score={score:.0f}, {source})"
        )
        return OemLookupResult(
            oem_number=best["oem_number"],
            part_name=best["part_name"],
            confidence=confidence,
            source=source,
            candidates=candidates[:5],
        )

    except SevenZapAuthError:
        raise  # caller falls back to RockAuto
    except Exception as exc:
        logger.error(f"7zap lookup error for '{part_name_english}': {exc}")
        return OemLookupResult(error=str(exc))
