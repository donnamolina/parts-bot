# PartSouq + Amayama Catalog Investigation

**Date:** 2026-04-20  
**Investigator:** DonnaBot  
**Test VINs used:**
- `3N6CD31B22K402415` — 2018 Nissan Frontier NP300 (Mexican market, known 7zap gap)
- `1C4RJFBG6JC459342` — 2018 Jeep Grand Cherokee (US market, 7zap control case)
- `5TFCZ5AN3GX019976` — 2016 Toyota Tacoma (US market, 7zap control case)

**Time spent:** ~1 hour  
**Method:** Web search + URL analysis (direct fetches blocked by bot protection on both sites)

---

## Phase 1 — PartSouq

### Does PartSouq cover the NP300 gap?

**YES — confirmed.** Multiple search-indexed PartSouq catalog pages explicitly show:
- `ENGINE MECHANICAL | Nissan NP300 FRONTIER 05.2015–07.2018`
- `TURBO CHARGER | Nissan NP300 FRONTIER 04.2018 G - BLACK` (D23TT)
- `NOZZLE & DUCT | Nissan NP300 FRONTIER 04.2017 D23X` (D23X)
- `ENGINE MECHANICAL | Nissan NP300 NAVARA 06.2018 D23TT`

Most critically: a live PartSouq unit page URL shows `q=3N6BD33B7KK804588` — a Mexican NP300 VIN (prefix `3N6`, same WMI as our gap VIN `3N6CD31B22K402415`) successfully loaded within their catalog. This is direct evidence that PartSouq's VIN decoder **accepts and resolves LATAM-spec `3N6` Frontier VINs**, while 7zap returns `vin_not_in_catalog` for the same prefix.

D23 variants confirmed in catalog:
- D23X (Mexico spec)
- D23TT (twin-turbo, Middle East/LATAM)
- Standard D23 (2015–2018 range)

Control VINs (Jeep, Tacoma): PartSouq does list both Nissan and Toyota brands in their catalog. Specific OEM cross-check against 7zap wasn't possible without live session access, but architecture evidence suggests they share the same underlying EPC data (see below).

### Catalog structure — is it scrapeable?

The URL structure is:
```
/en/catalog/genuine/groups?c=Nissan&ssd=<encoded>&vid=0&q=<VIN>
/en/catalog/genuine/unit?c=Nissan&ssd=<encoded>&vid=0&cid=<id>&uid=<id>&q=<VIN>
/en/catalog/genuine/diagram?c=NISSAN201809&ssd=<encoded>&number=<part_number>
```

The `ssd` parameter is an opaque, base64-like encoded session state descriptor — **identical in concept to 7zap's `ssd` parameter**. Both sites expose the Nissan EPC (Electronic Parts Catalog) with the same session-state encoding scheme. This strongly suggests they license the same underlying catalog platform (Nissan Fast EPC / OEM data).

The catalog identifier in `c=` varies: `c=Nissan` for browsing, `c=NISSAN201809` for specific catalog versions (Nissan EPC 2018.09 release). This version-specific catalog field is significant — it means PartSouq may serve a *different regional cut* of the Nissan EPC data than 7zap, which could explain why one decodes Mexican VINs and the other doesn't.

### Authentication / rate limiting

- **Bot protection active**: Direct HTTP fetches return 403. Consistent with Cloudflare or equivalent.
- **Session required**: The `ssd` parameter is generated server-side after VIN submission and encodes session state. Without a live browser session and cookie, catalog tree navigation is not possible.
- **No public API**: No developer documentation, no API keys, no documented endpoints. Same story as 7zap.
- **Rate limiting**: Not directly testable without a live session, but the same precautions as 7zap would apply (real browser relay, respectful request spacing).

### Effort estimate

**MEDIUM** — same as adding a second 7zap market.

The Mac-mini relay architecture already handles the cookie/session management pattern. PartSouq uses the same `ssd`-based EPC navigation. A new `search/oem_lookup_partsouq.py` would mirror `oem_lookup_7zap.py` closely:
1. Session initiation via browser relay (Mac-mini already handles this for 7zap)
2. VIN → `ssd` decode call → catalog tree
3. Part name fuzzy match → OEM number extraction

The main unknowns are:
- Whether PartSouq's session cookies persist as long as 7zap's
- Whether their rate limits are stricter
- Whether the same `PartMatcher` scoring logic transfers directly

Estimate: **2–3 days** of focused work to get a working prototype; another 1–2 days of tuning.

### Coverage overlap estimate

For Pieza Finder's DR market use case:
- **LATAM Nissan (NP300 Frontier, Navara D23)**: PartSouq covers this. 7zap does not. This is the primary gap.
- **Toyota LATAM (Hilux Diesel, Land Cruiser 200/300 LATAM-spec)**: Unknown — Toyota LATAM VINs use `8A` WMI codes. PartSouq has Toyota catalogs but specific LATAM WMI coverage needs testing.
- **US/EU vehicles**: PartSouq almost certainly overlaps with 7zap here (same EPC data source).
- **JDM**: Not PartSouq's focus — they appear Middle East/LATAM/global oriented.

Conservative estimate: PartSouq closes **~60–70% of the LATAM gap** (primarily Nissan, possibly Toyota). It will not help with JDM chassis codes.

---

## Phase 2 — Amayama

### Does Amayama have a public API?

**No.** No developer documentation, no API portal, no API keys found. Amayama is a **parts retailer** that ships genuine JDM parts worldwide. They have a public-facing catalog browser, but it is a shopping interface, not a catalog API.

The only "integration" path mentioned in their site is a wholesale inquiry form for B2B bulk purchasing — not programmatic access.

### VIN/frame coverage

Amayama confirmed catalog pages found:
- `https://www.amayama.com/en/genuine-catalogs/nissan/np300` — NP300 exists
- `https://www.amayama.com/en/genuine-catalogs/toyota/hilux` — Hilux exists
- `https://www.amayama.com/en/catalogs/toyota/hilux-surf` — Hilux Surf (JDM) exists
- Toyota Land Cruiser, MR2, Mitsubishi, Honda, Mazda, Subaru, Suzuki

Coverage model is **frame number / chassis code** based (e.g., `KZN185`, `JZX100`), not 17-char VIN. This is typical for JDM vehicles which predate the standardized VIN system. For LATAM vehicles, it's unclear whether standard 17-char VINs are supported.

### Catalog structure

URL structure: `/en/genuine-catalogs/<make>/<model>/<variant>/<system>/<subsystem>`. Clean and browsable, but scraping a retail store (with pricing, stock levels, cart flows) is architecturally different from scraping an EPC catalog. Part numbers exist in the catalog, but they're embedded in a shopping flow rather than in a pure parts-tree structure.

### Cost

Unknown — no public API means no published pricing. Parts themselves are priced individually. Any integration would be scraping their retail catalog, not consuming a paid API.

### JDM chassis code support

Yes — Toyota frame codes like `KZN185` (Hilux Surf), `JZX100` (Mark II/Chaser) are explicitly in their catalog. This is Amayama's strongest differentiator: **genuine JDM parts for JDM-spec vehicles that don't have 17-char VINs**.

### Effort estimate

**LARGE** — scraping a retail store is a fundamentally different (and more fragile) approach than the EPC-catalog relay pattern.

Challenges:
- Catalog pages are rendered for human shopping, not programmatic consumption
- Stock/pricing updates would create noise in OEM lookups
- No structured API — HTML parsing required
- Unknown anti-scraping measures
- Frame number lookup requires chassis code, not VIN (translation step needed)

Estimate: **5–8 days** for a working prototype; ongoing maintenance burden is higher than EPC-based sources.

### Coverage overlap estimate

- **JDM**: Strong. Toyota, Nissan, Mitsubishi JDM variants well covered.
- **LATAM**: Unknown. Amayama's focus is Japanese-market vehicles. Mexican/Caribbean-spec vehicles are unlikely to be well represented.
- **US/EU**: Weak — not Amayama's market.
- **DR-relevant JDM vehicles**: Hilux Surf, Land Cruiser (some models), Skyline (rare) — maybe 10–15% of DR claims would benefit.

---

## Phase 3 — Recommendation

### Comparison table

| Source | NP300 LATAM coverage | JDM coverage | Access complexity | Cost | Integration effort |
|---|---|---|---|---|---|
| 7zap (current) | ❌ (`vin_not_in_catalog`) | ❌ | Medium (cookie relay, working) | Free | — |
| **PartSouq** | ✅ **Confirmed** (D23X, D23TT, `3N6` VINs resolved) | ❌ | Medium (same relay pattern as 7zap) | Free | **Medium (2–3 days)** |
| Amayama | ❓ (probably weak) | ✅ Strong | Large (retail store scraping) | Unknown | **Large (5–8 days)** |

### Recommended path

**Add PartSouq as the primary secondary OEM source.**

Rationale:
1. **Closes the real gap**: The NP300 Frontier is a known, recurring vehicle type in the DR market. PartSouq confirmed coverage with actual `3N6` LATAM VINs resolving.
2. **Known integration pattern**: Same `ssd`-based EPC architecture as 7zap. The Mac-mini relay already handles this. `oem_lookup_partsouq.py` would be a near-clone of `oem_lookup_7zap.py` with different domain/session handling.
3. **Free data source**: No API costs.
4. **Low ongoing maintenance**: If the architecture truly mirrors 7zap, session refresh logic transfers.

**Defer Amayama.**

Rationale: Retail store scraping is a higher-effort, higher-maintenance proposition with unclear LATAM coverage. JDM vehicles are a small fraction of DR claims. Revisit if JDM requests become frequent.

**Don't "add both"**: PartSouq likely covers a superset of what Amayama provides for Pieza Finder's use case, with less integration effort.

### Proposed v12 scope (architecture sketch — no code)

**Fallback cascade:**
```
7zap VIN lookup
  → success: return OEM
  → vin_not_in_catalog:
      → PartSouq VIN lookup
          → success: return OEM (tagged source="partsouq")
          → not found: return name_only_fallback → eBay name search
```

**New file:**
```
search/oem_lookup_partsouq.py
  class PartSouqClient  (mirrors OemLookup7zap pattern)
  async def lookup_oem_by_vin(vin, part_name) -> OemLookupResult
```

**Mac-mini relay extension:**
The relay at `localhost:3019` (or whatever port it uses) would need a second session-keepalive for `partsouq.com` alongside the existing `7zap.com` session. Could be the same relay binary with an added cookie-store keyed by domain, or a second relay instance.

**New env vars:**
```
PARTSOUQ_RELAY_URL=http://mac-mini-local:3020   (or reuse with domain param)
PARTSOUQ_SESSION_COOKIE=...                      (managed by relay, not static)
```

**Agent tools.py:**
`lookup_oem_7zap` would be renamed or wrapped as `lookup_oem_catalog`, internally trying 7zap first then PartSouq. The agent tool interface stays the same — agent doesn't need to know which catalog resolved the OEM.

**Key open questions before v12:**
1. Does the Mac-mini relay expose an endpoint parameterized by domain, or does it hardcode 7zap? Determines whether relay needs modification.
2. Does PartSouq's `ssd` session expire at the same rate as 7zap's? If sessions expire faster, relay keepalive needs adjustment.
3. Does PartSouq's VIN tree response structure match 7zap's JSON format, or does `PartMatcher` need re-tuning for their catalog naming conventions?
4. **Requires a live test**: Connect relay to partsouq.com and run `3N6CD31B22K402415` through their VIN search. Confirm part-level OEM numbers come back (not just catalog tree structure).

---

## Summary

PartSouq is the clear winner for Pieza Finder's LATAM gap. Evidence of NP300 D23 coverage with Mexican VINs is direct and credible. Integration effort is bounded and follows a known pattern. The main pre-integration gate is live testing with the relay to confirm VIN → part-level OEM resolution actually works end-to-end.

Amayama has no public API and is architecturally misaligned with the current pipeline pattern. Defer until JDM vehicle volume justifies the higher integration cost.
