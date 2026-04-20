"""
PartSouq OEM Lookup — secondary VIN-exact catalog for LATAM-spec vehicles.

Architecture:
  PartSouqClient  — async HTTP client via Mac-mini relay (HTML responses)
  PartSouqParser  — parses PartSouq HTML pages (VIN search, groups, unit)
  lookup_oem_partsouq() — main entry point called by engine.py cascade

Called only when 7zap returns source="vin_not_in_catalog".

Requires in .env:
  PARTSOUQ_RELAY_URL=https://<ngrok-subdomain>.ngrok-free.dev
  PARTSOUQ_RELAY_TOKEN=pieza2026
  PARTSOUQ_ENABLED=true   # set false to disable temporarily
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

logger = logging.getLogger("parts-bot.partsouq")

# ── Cache config ──────────────────────────────────────────────────────────────
_CACHE_DIR = Path(__file__).parent.parent / "cache" / "oem_partsouq"
_VIN_CACHE_TTL_DAYS = 30    # shorter than 7zap since PartSouq SSDs can expire
_NEGATIVE_TTL_HOURS = 12

# ── Fuzzy matching thresholds ─────────────────────────────────────────────────
_SCORE_GREEN = 80
_SCORE_YELLOW = 68


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class PartSouqResult:
    oem_number: str | None = None
    part_name: str | None = None
    confidence: Literal["green", "yellow", "red"] = "red"
    source: Literal["partsouq_vin_exact", "partsouq_fuzzy", "vin_not_in_catalog", "no_result"] = "no_result"
    notes: str | None = None
    error: str | None = None


class PartSouqAuthError(Exception):
    """Raised on 401/403 — cookies likely expired."""
    pass


# ── Part name → PartSouq catalog keyword mapping ──────────────────────────────
# Maps English part names (lowercase) to (group_keyword, unit_keyword) pairs
# to navigate PartSouq's catalog tree (HTML scraping).
# Expand iteratively as real claims come in.
PART_TO_PARTSOUQ_CATEGORY: dict[str, list[tuple[str, str]]] = {
    # Exterior / Body
    "headlight":        [("lighting", "head"), ("front lights", "head"), ("headlamp", "head")],
    "headlamp":         [("lighting", "head"), ("front lights", "head")],
    "tail light":       [("lighting", "tail"), ("rear lights", ""), ("taillamp", "")],
    "tail lamp":        [("lighting", "tail"), ("rear lights", "")],
    "fog light":        [("lighting", "fog"), ("front lights", "fog")],
    "front bumper":     [("bumper", "front"), ("body", "front bumper")],
    "rear bumper":      [("bumper", "rear"), ("body", "rear bumper")],
    "bumper":           [("bumper", ""), ("body", "bumper")],
    "hood":             [("body", "hood"), ("bonnet", ""), ("engine hood", "")],
    "fender":           [("body", "fender"), ("panels", "fender")],
    "front fender":     [("body", "fender"), ("panels", "front fender")],
    "door":             [("body", "door"), ("doors", "")],
    "front door":       [("body", "front door"), ("doors", "front")],
    "rear door":        [("body", "rear door"), ("doors", "rear")],
    "tailgate":         [("body", "tailgate"), ("rear", "tailgate"), ("cargo", "tailgate")],
    "trunk lid":        [("body", "trunk"), ("rear", "trunk lid")],
    "grille":           [("body", "grille"), ("bumper", "grille"), ("front", "grille")],
    "mirror":           [("mirrors", ""), ("exterior mirror", ""), ("door mirror", "")],
    "side mirror":      [("mirrors", ""), ("door mirror", ""), ("exterior mirror", "")],
    "running board":    [("body", "running board"), ("step", ""), ("side step", "")],
    "side step":        [("body", "step"), ("running board", "")],
    "molding":          [("body", "molding"), ("trim", "molding")],
    "bumper cover":     [("bumper", "cover"), ("body", "bumper cover")],

    # Suspension / Steering
    "control arm":      [("suspension", "control arm"), ("front suspension", "arm")],
    "sway bar link":    [("suspension", "stabilizer"), ("suspension", "link")],
    "stabilizer link":  [("suspension", "stabilizer"), ("suspension", "link")],
    "tie rod":          [("steering", "tie rod"), ("steering", "")],
    "wheel hub":        [("suspension", "hub"), ("wheel", "hub")],
    "hub":              [("suspension", "hub"), ("wheel", "hub")],
    "shock absorber":   [("suspension", "shock"), ("front suspension", ""), ("rear suspension", "")],
    "strut":            [("suspension", "strut"), ("front suspension", "strut")],

    # Cooling
    "radiator":         [("cooling", "radiator"), ("engine cooling", "radiator")],
    "fan":              [("cooling", "fan"), ("radiator fan", "")],
    "radiator fan":     [("cooling", "fan"), ("radiator fan", "")],
    "condenser":        [("cooling", "condenser"), ("ac", "condenser")],

    # Engine
    "oil pan":          [("engine", "oil pan"), ("lubrication", "pan")],
    "valve cover":      [("engine", "valve cover"), ("engine", "cylinder head cover")],

    # Electrical
    "battery":          [("electrical", "battery"), ("power supply", "battery")],

    # Wheels / Tires
    "rim":              [("wheels", ""), ("wheel", ""), ("alloy wheel", "")],
    "wheel":            [("wheels", ""), ("alloy wheel", "")],
    "tire":             [("wheels", "tire"), ("tyre", "")],
}

# Sub-component strings to deprioritize (prefer assemblies)
_SUB_COMPONENT_BLOCKLIST = {
    "REINFORCEMENT", "REBAR", "ABSORBER", "BRACE", "STAY",
    "MOUNTING", "BRACKET", "BOLT", "CLIP", "NUT", "SCREW",
    "SEAL", "GASKET", "PLUG", "PIN", "WASHER",
}

# Part names that are explicitly sub-components (don't prefer assemblies for these)
_ASSEMBLY_QUERIES = {
    "bumper", "hood", "door", "fender", "tail light", "headlight",
    "tailgate", "trunk lid", "grille", "mirror", "radiator",
    "fan", "condenser", "side mirror", "running board",
}


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def _map_to_categories(part_english: str) -> list[tuple[str, str]]:
    """Return list of (group_kw, unit_kw) pairs for a part name."""
    lower = _normalize(part_english)
    # Strip side/position words for matching
    lower_stripped = re.sub(
        r'\b(left|right|front|rear|upper|lower|inner|outer|driver|passenger|lh|rh)\b',
        '', lower
    ).strip()

    # Try longest match first
    keys = sorted(PART_TO_PARTSOUQ_CATEGORY.keys(), key=len, reverse=True)
    for key in keys:
        if key in lower_stripped or key in lower:
            return PART_TO_PARTSOUQ_CATEGORY[key]
    return []


# ── Cache ─────────────────────────────────────────────────────────────────────

class PartSouqCache:
    """Per-VIN JSON cache for catalog data."""

    def __init__(self, vin: str):
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._path = _CACHE_DIR / f"{vin}.json"
        self._data: dict = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception:
                pass

    def _save(self):
        try:
            self._path.write_text(json.dumps(self._data, indent=2, default=str))
        except Exception as e:
            logger.warning(f"PartSouq cache save failed: {e}")

    def is_negative(self, part_key: str) -> bool:
        neg = self._data.get("negatives", {}).get(part_key)
        if not neg:
            return False
        try:
            ts = datetime.fromisoformat(neg["ts"])
            return datetime.utcnow() - ts < timedelta(hours=_NEGATIVE_TTL_HOURS)
        except Exception:
            return False

    def store_negative(self, part_key: str):
        self._data.setdefault("negatives", {})[part_key] = {
            "ts": datetime.utcnow().isoformat()
        }
        self._save()

    def is_ssd_fresh(self) -> bool:
        ssd_meta = self._data.get("ssd_meta")
        if not ssd_meta:
            return False
        try:
            ts = datetime.fromisoformat(ssd_meta["ts"])
            return datetime.utcnow() - ts < timedelta(days=_VIN_CACHE_TTL_DAYS)
        except Exception:
            return False

    def store_ssd(self, ssd: str, make: str):
        self._data["ssd_meta"] = {
            "ssd": ssd,
            "make": make,
            "ts": datetime.utcnow().isoformat(),
        }
        self._save()

    def get_ssd(self) -> tuple[str, str] | None:
        meta = self._data.get("ssd_meta")
        if meta:
            return meta.get("ssd", ""), meta.get("make", "")
        return None

    def store_groups(self, groups: list[dict]):
        self._data["groups"] = groups
        self._data["groups_ts"] = datetime.utcnow().isoformat()
        self._save()

    def get_groups(self) -> list[dict] | None:
        return self._data.get("groups")

    def store_unit_parts(self, unit_id: str, parts: list[dict]):
        self._data.setdefault("units", {})[unit_id] = {
            "parts": parts,
            "ts": datetime.utcnow().isoformat(),
        }
        self._save()

    def get_unit_parts(self, unit_id: str) -> list[dict] | None:
        entry = self._data.get("units", {}).get(unit_id)
        if not entry:
            return None
        try:
            ts = datetime.fromisoformat(entry["ts"])
            if datetime.utcnow() - ts < timedelta(days=_VIN_CACHE_TTL_DAYS):
                return entry.get("parts")
        except Exception:
            pass
        return None


# ── HTTP client ───────────────────────────────────────────────────────────────

class PartSouqClient:
    """Async client that calls the Mac-mini PartSouq relay."""

    def __init__(self):
        self._relay_url = os.getenv("PARTSOUQ_RELAY_URL", "").rstrip("/")
        self._relay_token = os.getenv("PARTSOUQ_RELAY_TOKEN", "")

    def _relay_headers(self) -> dict:
        h = {"X-Relay-Token": self._relay_token} if self._relay_token else {}
        return h

    async def get(self, partsouq_url: str, params: dict | None = None) -> tuple[int, str, str]:
        """Fetch a PartSouq URL via relay. Returns (status, body_text, final_url)."""
        if not self._relay_url:
            raise PartSouqAuthError("PARTSOUQ_RELAY_URL not configured")

        req_params = {"_url": partsouq_url}
        if params:
            req_params.update(params)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self._relay_url}/proxy",
                params=req_params,
                headers=self._relay_headers(),
            )

        if resp.status_code in (401, 403):
            raise PartSouqAuthError(f"PartSouq relay returned {resp.status_code} — cookies expired")

        final_url = resp.headers.get("X-Final-URL", partsouq_url)
        return resp.status_code, resp.text, final_url

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self._relay_url}/health")
        return resp.json()


# ── HTML Parser ───────────────────────────────────────────────────────────────

class PartSouqParser:
    """Parses PartSouq HTML pages to extract catalog data."""

    @staticmethod
    def extract_ssd(html: str, url: str = "") -> str | None:
        """
        Extract ssd= session token from HTML links or the page URL itself.
        PartSouq encodes EPC session state in the ssd parameter.
        """
        # From URL first (most reliable if we followed a redirect)
        m = re.search(r'[?&]ssd=([^&"\'>\s]{10,})', url)
        if m:
            return m.group(1)

        # From HTML links
        matches = re.findall(r'[?&]ssd=([^&"\'>\s]{10,})', html)
        if matches:
            # Return the most common (longest unique) SSD
            from collections import Counter
            counts = Counter(matches)
            return counts.most_common(1)[0][0]

        # Look for ssd in JSON-like data attributes
        m2 = re.search(r'"ssd"\s*:\s*"([^"]{10,})"', html)
        if m2:
            return m2.group(1)

        return None

    @staticmethod
    def is_vin_not_found(html: str) -> bool:
        """Return True if the page indicates the VIN is not in catalog."""
        lower = html.lower()
        return any(phrase in lower for phrase in [
            "not found", "invalid parameter", "error 404",
            "no results", "vehicle not found", "vin not found",
        ])

    @staticmethod
    def parse_groups(html: str) -> list[dict]:
        """
        Parse the catalog groups page into a list of group dicts.
        Each group has: {name, id, url, subgroups: [{name, id, url}]}
        """
        soup = BeautifulSoup(html, "html.parser")
        groups = []

        # PartSouq groups are typically in a list with links
        # Look for group/category links in the page
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "/catalog/genuine/groups" in href or "/catalog/genuine/unit" in href:
                name = _normalize(a_tag.get_text(strip=True))
                if not name or len(name) < 2:
                    continue

                # Extract group/unit parameters from href
                uid_m = re.search(r'uid=(\d+)', href)
                cid_m = re.search(r'cid=(\d+)', href)
                ssd_m = re.search(r'ssd=([^&"\'>\s]{10,})', href)

                groups.append({
                    "name": name,
                    "url": href,
                    "uid": uid_m.group(1) if uid_m else None,
                    "cid": cid_m.group(1) if cid_m else None,
                    "ssd": ssd_m.group(1) if ssd_m else None,
                })

        return groups

    @staticmethod
    def parse_unit_parts(html: str) -> list[dict]:
        """
        Parse a unit page to extract individual parts with OEM numbers.
        Returns list of {name, oem_number, description}.
        """
        soup = BeautifulSoup(html, "html.parser")
        parts = []

        # PartSouq part tables typically have columns: number, name, OEM#, qty, etc.
        # Look for part number patterns in table cells
        tables = soup.find_all("table")
        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                row_text = [_normalize(c.get_text(separator=" ")) for c in cells]

                # Find cells that look like OEM part numbers
                for i, text in enumerate(row_text):
                    # OEM number pattern: mix of letters + digits, 6-18 chars
                    oem_candidates = re.findall(
                        r'\b([A-Z0-9]{2,6}[-]?[A-Z0-9]{3,12}[-]?[A-Z0-9]{0,6})\b',
                        text.upper()
                    )
                    for oem in oem_candidates:
                        if (
                            re.search(r'\d', oem) and
                            re.search(r'[A-Z]', oem) and
                            5 <= len(oem.replace('-', '')) <= 18 and
                            oem not in _SUB_COMPONENT_BLOCKLIST
                        ):
                            # Get part name from adjacent cell
                            part_name = row_text[i - 1] if i > 0 else row_text[0]
                            parts.append({
                                "oem_number": oem,
                                "name": part_name,
                                "row_text": " | ".join(row_text),
                            })

        # Also look for parts in div/list structures (PartSouq uses both layouts)
        for div in soup.find_all(class_=re.compile(r'part|item|product|row', re.I)):
            text = _normalize(div.get_text(separator=" "))
            oem_matches = re.findall(
                r'\b([A-Z0-9]{2,6}[-]?[A-Z0-9]{3,12}[-]?[A-Z0-9]{0,6})\b',
                text.upper()
            )
            for oem in oem_matches:
                if (
                    re.search(r'\d', oem) and
                    re.search(r'[A-Z]', oem) and
                    5 <= len(oem.replace('-', '')) <= 18
                ):
                    parts.append({
                        "oem_number": oem,
                        "name": text[:80],
                        "row_text": text[:200],
                    })

        # Deduplicate by OEM number
        seen = set()
        unique_parts = []
        for p in parts:
            if p["oem_number"] not in seen:
                seen.add(p["oem_number"])
                unique_parts.append(p)

        return unique_parts


# ── Fuzzy matching ────────────────────────────────────────────────────────────

def _score_part(query: str, candidate: dict) -> float:
    """Score a candidate part against the query. Returns 0–100."""
    name = candidate.get("name", "") or candidate.get("row_text", "")
    if not name:
        return 0.0

    # Boost assemblies for assembly queries
    is_assembly_q = any(q in query.lower() for q in _ASSEMBLY_QUERIES)
    is_subcomp = any(block in name.upper() for block in _SUB_COMPONENT_BLOCKLIST)

    base_score = fuzz.token_sort_ratio(query.lower(), _normalize(name))

    if is_assembly_q and is_subcomp:
        base_score = max(0, base_score - 25)  # deprioritize sub-components

    return float(base_score)


def _find_best_match(
    query: str,
    parts: list[dict],
    req_side: str | None,
) -> tuple[dict | None, float]:
    """Find the best-matching part for the query. Returns (part, score)."""
    if not parts:
        return None, 0.0

    scored = []
    for p in parts:
        score = _score_part(query, p)
        if score > 0:
            # Side filter: if a specific side is requested, require it in the name
            name = _normalize(p.get("name", "") + " " + p.get("row_text", ""))
            if req_side == "left" and any(w in name for w in ("right", " rh", "passenger")):
                score -= 30
            elif req_side == "right" and any(w in name for w in ("left", " lh", "driver")):
                score -= 30
            scored.append((p, score))

    if not scored:
        return None, 0.0

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0]


# ── Main entry point ──────────────────────────────────────────────────────────

async def lookup_oem_partsouq(
    vin: str,
    part_name_english: str,
    make_hint: str | None = None,
) -> PartSouqResult:
    """
    Secondary OEM lookup via PartSouq. Called when 7zap returns vin_not_in_catalog.
    Never raises — errors returned in result.error.
    Raises PartSouqAuthError if cookies expired (caller degrades gracefully).
    """
    if not vin or len(vin) != 17:
        return PartSouqResult(error=f"Invalid VIN: '{vin}'")

    if os.getenv("PARTSOUQ_ENABLED", "true").lower() != "true":
        return PartSouqResult(error="PartSouq disabled by PARTSOUQ_ENABLED=false")

    relay_url = os.getenv("PARTSOUQ_RELAY_URL", "")
    if not relay_url:
        return PartSouqResult(error="PARTSOUQ_RELAY_URL not configured")

    cache = PartSouqCache(vin)
    part_key = _normalize(part_name_english)

    if cache.is_negative(part_key):
        return PartSouqResult(
            source="no_result",
            error=f"No PartSouq match (negative cache): '{part_name_english}'",
        )

    categories = _map_to_categories(part_name_english)
    if not categories:
        logger.debug(f"partsouq: no category mapping for '{part_name_english}'")
        cache.store_negative(part_key)
        return PartSouqResult(error=f"No PartSouq category for '{part_name_english}'")

    # Determine make for PartSouq's `c=` parameter
    make = (make_hint or "").strip()
    # PartSouq uses title-case make names: "Nissan", "Toyota", etc.
    if make:
        make = make.title()
    if not make:
        # Derive from VIN WMI if no hint
        make = _make_from_vin(vin)

    # Determine requested side
    lower = part_name_english.lower()
    req_side: str | None = None
    if any(w in lower for w in ("left", " lh", "izquierdo", "driver")):
        req_side = "left"
    elif any(w in lower for w in ("right", " rh", "derecho", "passenger")):
        req_side = "right"

    client = PartSouqClient()

    try:
        # ── Step 1: VIN search → get ssd ──────────────────────────────────────
        ssd: str | None = None
        if cache.is_ssd_fresh():
            cached_ssd = cache.get_ssd()
            if cached_ssd:
                ssd, make = cached_ssd[0], cached_ssd[1] or make
                logger.debug(f"partsouq: using cached ssd for VIN {vin}")

        if not ssd:
            logger.info(f"partsouq: VIN search for {vin} (make={make})")
            status, html, final_url = await client.get(
                "https://partsouq.com/en/catalog/genuine/search",
                params={"c": make, "q": vin, "vid": "0"},
            )

            if status != 200:
                return PartSouqResult(error=f"PartSouq VIN search returned HTTP {status}")

            if PartSouqParser.is_vin_not_found(html):
                logger.info(f"partsouq: VIN {vin} not found in catalog")
                return PartSouqResult(
                    source="vin_not_in_catalog",
                    error=f"PartSouq catalog no disponible para VIN {vin}",
                )

            ssd = PartSouqParser.extract_ssd(html, final_url)
            if not ssd:
                return PartSouqResult(error=f"PartSouq: no ssd extracted for VIN {vin}")

            cache.store_ssd(ssd, make)
            logger.info(f"partsouq: got ssd for {vin}: {ssd[:30]}...")

        await asyncio.sleep(0.5)  # be polite

        # ── Step 2: Get catalog groups ─────────────────────────────────────────
        groups = cache.get_groups()
        if not groups:
            logger.info(f"partsouq: fetching catalog groups for {vin}")
            status, html, _ = await client.get(
                "https://partsouq.com/en/catalog/genuine/groups",
                params={"c": make, "ssd": ssd, "vid": "0"},
            )
            if status != 200:
                return PartSouqResult(error=f"PartSouq groups returned HTTP {status}")

            groups = PartSouqParser.parse_groups(html)
            if groups:
                cache.store_groups(groups)
                logger.info(f"partsouq: {len(groups)} catalog groups found")
            else:
                logger.warning(f"partsouq: no groups parsed from catalog page")

        if not groups:
            return PartSouqResult(error="PartSouq: empty catalog groups")

        # ── Step 3: Find relevant groups for this part ─────────────────────────
        matching_groups = _find_matching_groups(categories, groups, ssd, make)
        if not matching_groups:
            logger.debug(f"partsouq: no matching groups for '{part_name_english}'")
            cache.store_negative(part_key)
            return PartSouqResult(error=f"No PartSouq groups for '{part_name_english}'")

        # ── Step 4: Get parts from matching units ──────────────────────────────
        all_parts: list[dict] = []
        for grp in matching_groups[:3]:  # limit to 3 groups to avoid hammering
            unit_id = grp.get("uid") or grp.get("cid") or grp.get("url", "")
            if not unit_id:
                continue

            cached_parts = cache.get_unit_parts(str(unit_id))
            if cached_parts is not None:
                all_parts.extend(cached_parts)
                continue

            await asyncio.sleep(0.5)
            logger.info(f"partsouq: fetching unit '{grp.get('name')}' ({unit_id})")

            # Build unit URL — use the group's own URL if it's already a unit URL
            unit_url = grp.get("url", "")
            if not unit_url.startswith("http"):
                unit_url = f"https://partsouq.com{unit_url}"

            try:
                status, html, _ = await client.get(unit_url)
                if status == 200:
                    parts = PartSouqParser.parse_unit_parts(html)
                    cache.store_unit_parts(str(unit_id), parts)
                    all_parts.extend(parts)
                    logger.info(f"partsouq: {len(parts)} parts in unit '{grp.get('name')}'")
            except Exception as e:
                logger.warning(f"partsouq: unit fetch failed for {unit_url}: {e}")
                continue

        if not all_parts:
            cache.store_negative(part_key)
            return PartSouqResult(error=f"No parts found in PartSouq units for '{part_name_english}'")

        # ── Step 5: Fuzzy-match part name against parts list ──────────────────
        best_part, score = _find_best_match(part_name_english, all_parts, req_side)

        if best_part and score >= _SCORE_GREEN:
            return PartSouqResult(
                oem_number=best_part["oem_number"],
                part_name=best_part.get("name"),
                confidence="green",
                source="partsouq_vin_exact",
                notes=f"PartSouq match score {score:.0f}",
            )
        elif best_part and score >= _SCORE_YELLOW:
            return PartSouqResult(
                oem_number=best_part["oem_number"],
                part_name=best_part.get("name"),
                confidence="yellow",
                source="partsouq_fuzzy",
                notes=f"PartSouq fuzzy score {score:.0f} — verify fitment",
            )
        else:
            cache.store_negative(part_key)
            return PartSouqResult(
                source="no_result",
                error=f"PartSouq: best match score {score:.0f} below threshold for '{part_name_english}'",
            )

    except PartSouqAuthError:
        raise
    except Exception as e:
        logger.error(f"partsouq lookup failed: {e}", exc_info=True)
        return PartSouqResult(error=f"PartSouq lookup error: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_matching_groups(
    categories: list[tuple[str, str]],
    groups: list[dict],
    ssd: str,
    make: str,
) -> list[dict]:
    """Find catalog groups that match any of the category (group_kw, unit_kw) pairs."""
    matched = []
    seen_urls = set()

    for group_kw, unit_kw in categories:
        for grp in groups:
            name = grp.get("name", "").lower()
            url = grp.get("url", "")
            if url in seen_urls:
                continue
            if group_kw in name or (unit_kw and unit_kw in name):
                matched.append(grp)
                seen_urls.add(url)

    return matched


def _make_from_vin(vin: str) -> str:
    """Derive make from VIN WMI (first 3 chars). Covers common LATAM/DR vehicles."""
    wmi = vin[:3].upper()
    WMI_MAP = {
        "3N6": "Nissan", "3N1": "Nissan", "JN1": "Nissan", "JN8": "Nissan",
        "1N4": "Nissan", "5N1": "Nissan",
        "3VW": "Volkswagen", "9BW": "Volkswagen",
        "MHF": "Toyota", "JTD": "Toyota", "5TD": "Toyota", "3TM": "Toyota",
        "5TF": "Toyota", "4T1": "Toyota",
        "3HM": "Honda", "JHM": "Honda", "1HG": "Honda",
        "1C4": "Chrysler", "3C4": "Chrysler",
        "ML3": "Mitsubishi", "JA3": "Mitsubishi",
        "MM8": "Mitsubishi",
    }
    return WMI_MAP.get(wmi, "")
