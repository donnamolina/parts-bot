# Pieza Finder — v11 Readiness Audit

**Audited build:** v10 production on `/opt/parts-bot/` (DigitalOcean `134.122.115.142`)
**Audit mode:** read-only. No commits, no fixes, no refactors.
**Date:** 2026-04-17

> TL;DR at the end — written last per the audit protocol.

---

## 1. Repo inventory

### Top-level (`/opt/parts-bot/`)

| File | Lines | Purpose | Status |
|---|---|---|---|
| `server.js` | 1787 | Baileys WhatsApp bot — state machine, Sonnet chat/correction dispatch, Python bridge | **Live, core** |
| `ecosystem.config.js` | — | PM2 launch config | Live |
| `pm2.config.js` | — | Second PM2 config (duplicate of the above?) | **Cruft: duplicate** |
| `package.json` | — | Node deps (baileys, anthropic, supabase-js, pino) | Live |
| `requirements.txt` | — | Python deps | Live |
| `.env.example` | — | Env template — contains the single `TODO` hit in the repo | Live |
| `.gitignore` | — | — | Live |
| `engine.py` | 429 | **Root** copy of search engine | **ORPHAN — diverges from `search/engine.py` (512 ln)** |
| `excel_builder.py` | 513 | **Root** copy of Excel generator | **ORPHAN — diverges from `search/excel_builder.py` (563 ln)** |
| `test_7zap.py` | — | Ad-hoc 7zap probe script | **Cruft: dev-only** |
| `test_vins.json` | 5 | 3 hard-coded test VINs (Porsche Macan, Hyundai, Jeep Grand Cherokee) | **Cruft: dev-only** |
| `sessions.json` | 1 | Empty `{}` — vestigial JSON session store | **Cruft: in-memory Map + Supabase already own sessions** |

### `search/` (Python pipeline)

| File | Lines | Purpose | Status |
|---|---|---|---|
| `run_search.py` | 474 | Orchestrator entry — called by server.js subprocess; VIN decode, parallel search, 2 Sonnet passes | Live, core |
| `run_ocr.py` | — | OCR entry — thin CLI around `ocr_extract.py` | Live |
| `run_single_part.py` | — | Single-part re-search entry for the `reviewing` state correction loop | Live |
| `parse_text.py` | — | Parse free-text parts list (no image) via Sonnet | Live |
| `ocr_extract.py` | 445 | Sonnet+Haiku OCR for photos/PDFs | Live, core |
| `engine.py` | 512 | `search_single_part` / `search_all_parts` orchestrator | Live, core |
| `oem_lookup_7zap.py` | 1203 | VIN-exact OEM# lookup via 7zap / TecDoc | Live, core |
| `ebay_search.py` | 548 | eBay Browse API + rate-limit + body-panel floor prices | Live, core |
| `rockauto_search.py` | 551 | RockAuto fallback OEM# (purchase disabled; reference-only) | Live |
| `verify_listing.py` | 120 | Sonnet per-listing verdict (MATCH / WRONG_PART / etc.) | Live, core |
| `vin_decode.py` | 102 | NHTSA vPIC decode + cache | Live |
| `dictionary.py` | 787 | DR Spanish→English + `PART_TO_CATEGORY` | Live, core |
| `cost_calculator.py` | 50 | Landed-cost formula with ClickPack + FX | Live |
| `weight_table.py` | 178 | `PART_WEIGHT_ESTIMATES` for ClickPack pricing | Live |
| `manual_review.py` | 42 | `UNSHIPPABLE_PARTS` / `DEALER_ONLY_PARTS` / `VIN_PROGRAMMED_PARTS` frozensets | Live |
| `db_client.py` | 301 | Supabase REST client — corrections, cache | Live, core |
| `excel_builder.py` | 563 | Final Excel generator | Live, core |
| `regen_excel.py` | — | Rebuild Excel after a correction in review | Live |
| `cache_verified.py` | — | Write `verified_by_correction=true` cache rows when user says "listo" | Live |
| `dictionary.py.bak.20260417` | 633 | Pre-edit backup | **Cruft: manual bak** |
| `dictionary.py.bak.20260417b` | 710 | Second pre-edit backup | **Cruft: manual bak** |
| `oem_lookup_7zap.py.bak.20260417` | — | Pre-edit backup | **Cruft: manual bak** |
| `server.js` | 943 | Pre-v10 leftover — does NOT match root `server.js` | **ORPHAN** |
| `__pycache__/` | — | | Live |

### `whatsapp/`

| File | Lines | Purpose | Status |
|---|---|---|---|
| `correction_handler.py` | 251 | Sonnet-powered correction envelope (ACTION_ENUM dispatcher) | Live, core |
| `__init__.py` | 0 | — | Live |

### Other runtime dirs (not audited for code)

`cache/` (ebay_token.json, translation_cache.json, vehicles.json, rate_limit.json), `logs/` (searches.log, ra_debug.log ~86KB), `output/` (ephemeral Excel + result JSONs, 2-hr TTL), `auth/` (Baileys session creds).

### Orphan summary (Phase 5 preview)

Three file-pair divergences from a pre-v10 refactor have never been cleaned up:
- `search/server.js` (943 ln) — not referenced anywhere
- Root `engine.py` (429 ln) — `server.js` only imports `search/engine.py` via Python `PYTHONPATH`
- Root `excel_builder.py` (513 ln) — same

Plus three manual `.bak.20260417*` files, `test_7zap.py`, `test_vins.json`, `sessions.json` (empty), and possibly one of the two PM2 configs.

---

## 2. Conversational state machine map

### Storage

Purely **in-memory** `Map`s in `server.js` (lines 60–120 area):

- `sessions: Map<jid, SessionObj>` — per-user state object
- `mediaQueues: Map<jid, [{mediaPath, ext}]>` — photo/PDF queue for debounced batches
- `chatHistories: Map<jid, [{role, content}]>` — Sonnet conversation history
- `textBuffers: Map<num, {messages, timer}>` — 2.5s text debounce
- `imageTimestamps: Map<num, ts>` — ties VIN-only text msgs to nearby images
- `queueDebounce: Map<num, timeout>` — debounces batched media

All in-memory — **a PM2 restart wipes everything**. Supabase `parts_sessions` acts as the recovery mechanism (see Phase 6).

### The 8 states

Stored on `session.state` strings. No enum, no state table — just string literals tested with `if/else`.

| State | Entry | Exit | Location |
|---|---|---|---|
| `idle` | default | text OR media received | server.js:691, 858 |
| `pending_restore` | idle text msg matched a recent `active`/`reviewing` session in Supabase (within 24 h) | yes/no answered | :858–944 |
| `processing` | OCR or parts-text parse in flight | OCR finishes → awaiting_confirmation or awaiting_vehicle | :585, 972, 1563 |
| `awaiting_vehicle` | `parse_text.py` returned no year/model | user sends vehicle/VIN → re-parse → awaiting_confirmation | :948–1009, 1589 |
| `awaiting_confirmation` | OCR/parse returned ≥1 part + vehicle | user confirms → `searching`; user corrects → re-show list | :636, 1202–1399 |
| `searching` | user confirmed | results delivered → `reviewing` or `idle` | :704, 1012 |
| `reviewing` | results Excel sent + `parts_sessions` row saved | `listo` → `idle` (session `closed`); else re-search one part | :783, 900, 1020–1148, 1438 |
| (implicit `delivering`) | in practice folded into `searching`→`reviewing` transition inside `runSearchPipeline` | — | :752–808 |

### State transitions — diagram (ASCII)

```
 idle ──text──> [auto-restore probe Supabase, <24h]
   │                │
   │                ├── 1 match  ──> pending_restore ──yes──> reviewing
   │                │                                 ──no──> idle
   │                │                                 ──media──> (reset)
   │                └── 0 or many  (fall through)
   │
   │──text (buffered 2.5s)──┬── parts-list-ish ──> processing ──> awaiting_confirmation
   │                         │                                      │
   │                         │                                      │──confirm──> searching
   │                         │                                      │──correction──> awaiting_confirmation (re-shown)
   │                         │                                      │              └── via parseCorrection() OR correction_handler.py
   │                         │                                      │
   │                         │                                      │──Sonnet says vehicle needed──> awaiting_vehicle ──> processing
   │                         └── else ──> handleChat (Sonnet chat)
   │
   │──image/pdf──> debounce 2.5s ──> processSingleMedia ──> processing ──> awaiting_confirmation
   │
   │──"S-0047 …" ──> loadSessionByCode ──> reviewing

 searching ──run_search.py complete──> reviewing (session code assigned, Supabase row INSERT status=active)
 reviewing ──"listo"──> idle (Supabase UPDATE status=closed, cache_verified.py fires)
 reviewing ──correction──> reviewing (run_single_part.py + regen_excel.py + logCorrection)
```

### Rule-based string / text layers

The whole point of v11 is to replace these. Inventory:

- **`MSG` dict** (server.js:148–179) — ~13 canned Spanish strings + 13 English (EN path is rarely hit because `session.lang = 'es'` is force-set at line 851). Includes `welcome`, `send_photo`, `confirm_prompt`, `searching(n)`, `progress(found,total)`, `done`, `error`, `ocr_error`, `timeout`, `only_photos`, `partial`, `not_allowed`, `processing`.
- **`detectLanguage(text)`** (server.js:140) — keyword vote on 14 Spanish words. Overridden at :851 (always `'es'`). Dead code but still called.
- **`confirmWords` array** (server.js:1204) — `['ok','si','sí','yes','dale','confirmar','buscar','confirmo','adelante','go','search','listo']`. Matched with `startsWith(' ')` tolerance. Only active in `awaiting_confirmation`.
- **`pending_restore` yes/no parser** (:892–893) — hand-rolled word lists for yes/no.
- **Resume-intent regex** (:1406–1410) — `/\bS-(\d{3,4})\b/i` + keyword list (`ayer`, `sesion`, etc.).
- **Vehicle-keyword regex** (:1419) — hardcoded list of ~14 models used to disambiguate multi-session case.
- **Parts-list trigger logic** (:1521–1547) — VIN regex + `PART_KW_RE` (17 DR+EN part keywords) + vehicle-word regex + line-count heuristic. Decides whether to run `parse_text.py` or route to `handleChat`.
- **`confirmMsg` formatting** (:645–666, :983–992, :1332–1389, :1605–1620) — the bulleted part list is rebuilt **four times** in server.js with slight variations (side/pos/qty/price/english suffix). Prime candidate for a single view-builder in v11.

### Sonnet integration points (9 call sites across 5 files)

1. **`server.js:parseCorrection`** (:398–471) — claude-sonnet-4-6, 256 tok, DR vocabulary + part-index parser. Returns `{is_correction, is_vehicle_correction, is_done, ...}`. **Legacy shape — pre-correction_handler.**
2. **`server.js:handleChat`** (:485–497) — claude-sonnet-4-6, 1024 tok, `CHAT_SYSTEM_PROMPT` informal DR voice. Called when text doesn't look like a parts list.
3. **`server.js` awaiting_vehicle parser** (:950–962) — inline claude-sonnet-4-6, 200 tok, returns `{type, vehicle, vin}`.
4. **`whatsapp/correction_handler.py:handle_correction`** — claude-sonnet-4-6, 512 tok, **full ACTION_ENUM envelope**: `update_quantity / rename_part / add_part / remove_part / re_extract_metadata / re_extract_parts / fix_translation / confirm_all / ask_clarification / out_of_scope`. Called as subprocess at server.js:356 only inside `awaiting_confirmation` after `parseCorrection` fails.
5. **`search/run_search.py:sonnet_verify_results`** (:48–117) — claude-sonnet-4-6, 512 tok, reviews full results table, returns `["#N: ...", ...]` flags.
6. **`search/run_search.py` per-listing verifier loop** (:280–310) — calls (7) concurrently.
7. **`search/verify_listing.py:verify_ebay_listing`** — claude-sonnet-4-20250514, returns `{verdict, note}` for a single listing.
8. **`search/ocr_extract.py`** — Sonnet per-page vision for quotes + Haiku fallback.
9. **`search/parse_text.py`** — Sonnet-based parse of free-text parts lists.

**Model pinning drift:** 5 of 9 sites use `claude-sonnet-4-6`, one (`verify_listing.py`) still pins `claude-sonnet-4-20250514`, and OCR/parse_text live inside their own scripts. Unify in v11.

---

## 3. Tool surface map (candidates for agent tools in v11)

Treating each pipeline touchpoint as a **tool** the agentic Sonnet loop could call:

### OCR & parsing
- `extract_from_media(path, ext)` — wraps `run_ocr.py` → `ocr_extract.extract_from_image/pdf`. Returns `{vehicle, vin, parts[], supplier_total_dop}`.
- `extract_from_text(raw_text)` — wraps `parse_text.py`. Same return shape.

### Search pipeline
- `search_all_parts(vehicle, parts[])` — `run_search.py` entry, runs full VIN decode + 7zap + eBay + RockAuto cascade + 2 Sonnet passes + Excel. Currently called once per confirmed extraction.
- `search_single_part(vehicle, part)` — `run_single_part.py`. Used during `reviewing` re-search.
- `regen_excel(results_json_path, supplier_total_dop?)` — `regen_excel.py`.
- `cache_verified(results_json_path, vehicle)` — `cache_verified.py`, writes `parts_cache.verified_by_correction=true`.

### Sub-tools under the pipeline (worth exposing to the agent)
- `decode_vin(vin)` — NHTSA vPIC. Cached.
- `lookup_oem_7zap(vin, vehicle, part_english, side, position)` — `oem_lookup_7zap.py`. Returns `{oem_number, confidence: green|yellow|red, source: "7zap.relay" | "7zap.direct"}`.
- `lookup_oem_rockauto(vehicle, part_english)` — reference-only.
- `search_ebay(query, side?, part_english?)` — `ebay_search.search_ebay`. Respects rate limit, OAuth cache, body-panel min prices, `BODY_PART_TITLE_EXCLUDES`.
- `verify_listing(part_en, vehicle, oem, title, price)` → `{verdict, note}`.
- `classify_manual_review(part_english)` → `None | "unshippable" | "dealer_only" | "vin_programmed"`.
- `translate_part(dr_name)` — `dictionary.translate_part` reading `dictionary.py` + `cache/translation_cache.json`.
- `calculate_landed_cost(listing_price_usd, us_shipping_usd, part_english)` — ClickPack + FX.

### Delivery
- `send_text(jid, text)` (server.js helper)
- `send_document(jid, excel_path, file_name)` (server.js helper)

### Session & history (DB-backed)
- `get_next_session_code()` — monotonic `S-NNNN`.
- `save_session_to_db(code, jid, extraction, results, excel_filename)` — `parts_sessions` INSERT.
- `update_session_status(code, status)` — `active` / `reviewing` / `closed`.
- `load_session_by_code(code)` — direct lookup.
- `find_recent_session(phone, vehicle_hint?)` — 24h list, ambiguity handling.
- `append_history(jid, role, content)` / `get_history(jid)` / `clear_history(jid)`.

### Corrections & learning
- `log_correction(vehicle, original_part, corrected_part, msg, idx)` — server.js:503, Supabase `parts_corrections` insert/increment + auto-promote to `cache/translation_cache.json` at 3+.
- `get_correction_override(make, model, part_original)` — `db_client.get_correction_override` → confirmed `likely`/`confirmed` overrides.

### Router / intent (the layer v11 is replacing)
- `parse_correction(text, parts)` — legacy, server.js
- `handle_correction_py(text, parts, vehicle, history)` — the envelope dispatcher

---

## 4. Known bugs 12–19

Bugs 1–11 are already addressed in v10. The numbering below continues from where the prior audit left off — open issues for v11. Sizing key: **trivial** (≤1 h, single file), **medium** (1–4 h, cross-file), **large** (≥half-day, schema/pipeline change).

### Bug 12 — Two PM2 configs of unclear precedence
**Where:** `/opt/parts-bot/ecosystem.config.js` and `/opt/parts-bot/pm2.config.js`
**Symptom:** Deploy-time ambiguity. Whichever PM2 loads first wins. No comments explain which is canonical.
**Size:** **trivial** — delete one after confirming which PM2 actually reads.

### Bug 13 — eBay body-panel price floor, no ceiling
**Where:** `search/ebay_search.py:245–274` (`BODY_PANEL_MIN_PRICES`); used at `:385, 405`.
**Symptom:** A $1,400 hood gets accepted because only a `min_price` check exists. High-variance categories (full LED headlamp assemblies, OEM bumper covers) occasionally return dealer-price listings that blow up landed cost — no upper bound filter, no outlier detection before Sonnet verify (which runs post-selection on one listing).
**Size:** **medium** — add a per-keyword ceiling dict OR switch to IQR/median pruning over the top-N listings before picking best.

### Bug 14 — Verify-listing model pin drift
**Where:** `search/verify_listing.py` pins `claude-sonnet-4-20250514`. Every other site uses `claude-sonnet-4-6`.
**Symptom:** Two Sonnet generations in one pipeline — inconsistent verdict behavior and extra cost surface when old model gets sunset.
**Size:** **trivial** — change the string.

### Bug 15 — Divergent orphan files can be imported by accident
**Where:** root `engine.py` (429 ln), root `excel_builder.py` (513 ln), `search/server.js` (943 ln, pre-v10).
**Symptom:** `run_search.py` imports `search.engine` via `sys.path.insert(0, parent)` — correct. But any future developer who does `from engine import …` at the top level silently loads a stale version. Risk amplifies under v11 refactor pressure.
**Size:** **trivial** (delete) but scoped as **medium** because you need to verify no dev scripts reference them.

### Bug 16 — Per-listing Sonnet verify only fires when oem_source starts with `7zap`
**Where:** `search/run_search.py:286` — condition `if _oem_src.startswith("7zap") and …`.
**Symptom:** For RockAuto-sourced OEMs, "name_fallback" retries (line 382 sets `oem_source = "name_fallback"`), and cache hits (`"Cache"` source), per-listing verification is **skipped**. This is the path where WRONG_PART listings are most likely to slip through, because name-based eBay search is noisier than OEM-based.
**Size:** **medium** — relax the guard to verify whenever `best_option` has a non-trivial title+price, regardless of OEM source. Requires re-thinking the WRONG_PART retry cycle to avoid recursion.

### Bug 17 — Parts-list trigger regex misses common DR vocabulary
**Where:** `server.js:1524` `PART_KW_RE = /\b(bonete|farol|guardafango|bumper|catre|piña|violeta|stop|espejo|parrilla|bolsa|cinturon|frentil|clips|fender|hood|headlight|grille|control arm|brake|rotor|mirror|airbag|seatbelt)\b/i`.
**Symptom:** Messages that legitimately contain a parts list but use words like `pantalla`, `aleta`, `cran`, `piña`, `flear`, `defensa`, `bonette` (variant), `módulo`, `amortiguador`, `rótula`, `bujia`, `embrague`, `disco`, `tapón` are misrouted to `handleChat` instead of `parse_text.py`. User has to send an image instead of text. The existing correction-handler prompt (`whatsapp/correction_handler.py` :66–80) documents a much richer DR vocabulary that the router itself can't see.
**Size:** **medium** — either expand the regex dramatically OR (v11 approach) let Sonnet decide intent, removing the regex entirely.

### Bug 18 — `rockauto_search.py` leaves `_RA_DEBUG_ACTIVE=True` and writes ~86 KB/day
**Where:** `search/rockauto_search.py` global flag + `_debug_log` calls to `logs/ra_debug.log`. Global monkey-patches `BaseClient.__init__` for proxy injection.
**Symptom:** Disk growth over time (no rotation), plus the monkey-patch makes `rockauto_api` behavior non-obvious. Also it runs even though RockAuto is purchase-disabled (reference-only).
**Size:** **medium** — gate by `RA_DEBUG=false`, rotate logs, consider removing RockAuto entirely if it's not useful enough as reference.

### Bug 19 — `verified_by_correction` caching can be set by SILENCE, not confirmation
**Where:** `search/cache_verified.py` fires from `server.js:1038` on `listo`. But `listo` in the `reviewing` path can mean "I'm done here" rather than "every row is correct". Users have been observed saying `listo` on results with 1–2 `NO RESULTS` rows, which causes those N/F rows to be re-cached as `verified_by_correction=true`.
**Where in code:** cache-write logic in `cache_verified.py` doesn't inspect per-row `best_option` — it trusts the JSON blob.
**Size:** **medium** — filter rows before writing cache; skip rows whose `best_option` is null/error; consider a stricter "all good?" prompt before closing.

> Soft bug (call it 19b, not counted): **OCR side/position disagreements with image EXIF orientation** cause occasional left/right flips. Not reproduced in this audit; carrying over as a known v10 concern. Sizing if touched: medium (add image orientation normalization in `ocr_extract.py`).

---

## 5. Cruft & cleanup candidates

Grouped by type.

### Orphan/dead files (no imports found)
- `/opt/parts-bot/search/server.js` (943 ln, pre-v10) — delete.
- `/opt/parts-bot/engine.py` (429 ln, root copy) — delete. Only `search/engine.py` is imported.
- `/opt/parts-bot/excel_builder.py` (513 ln, root copy) — delete. Only `search/excel_builder.py` is imported.
- `/opt/parts-bot/test_7zap.py` — dev probe. Move to scratch or delete.
- `/opt/parts-bot/test_vins.json` — 3 hardcoded test VINs. Move to scratch or delete.
- `/opt/parts-bot/sessions.json` — empty `{}` vestige. Delete.
- `/opt/parts-bot/search/dictionary.py.bak.20260417` (633 ln) — delete.
- `/opt/parts-bot/search/dictionary.py.bak.20260417b` (710 ln) — delete.
- `/opt/parts-bot/search/oem_lookup_7zap.py.bak.20260417` — delete.
- Possibly `pm2.config.js` OR `ecosystem.config.js` (keep one, see Bug 12).

### Duplicated logic inside live code
- **Confirmation list renderer** repeated 4× in `server.js`: `:645–666`, `:983–992`, `:1332–1389`, `:1605–1620`. All format "N. name (Izq/Der) Del/Tras → en x qty — RD$price". Extract to `formatPartsList(parts, vehicle, opts)`.
- **Pending-restore re-load block** appears almost identically in the `pending_restore` yes branch (`:899–929`) and the `S-NNNN` code resume branch (`:1436–1475`). Could be one helper.
- **Sonnet correction parser** vs **Python correction_handler** — two different JSON shapes (legacy `{is_correction,is_done}` vs new ACTION_ENUM). Only one is needed in v11.

### TODO/FIXME
- Repo grep: the only `TODO` is in `.env.example`. No `FIXME` in owned code.
  (So the "invisible tech debt" lives entirely in structure and duplication, not comments.)

### Config scattering
- 4 places define/read env: `.env` (loaded by node + python separately), `ecosystem.config.js`, `pm2.config.js`, plus hardcoded defaults throughout (`MAX_CONCURRENT_SEARCHES=5`, `EBAY_DAILY_LIMIT=5000`, `SEARCH_TIMEOUT_SECONDS=300`, `VERIFY_WITH_SONNET=true`, RockAuto behavior flags).
- Price thresholds hardcoded in `ebay_search.py` (`BODY_PANEL_MIN_PRICES`), scoring thresholds hardcoded in `oem_lookup_7zap.py` (`_SCORE_GREEN=85`, `_SCORE_YELLOW=75`), FX rate + ClickPack per-lb rate hardcoded in `cost_calculator.py`. Consolidate into one config module.

### Error handling gaps ("silent except Exception")
Not a crisis but notable volume:
- `db_client.py` — 8 `except Exception` blocks, all log-and-return-`None`. Correct pattern for fire-and-forget DB writes, but no metric/alarm if Supabase goes down for hours.
- `oem_lookup_7zap.py` — several `except Exception` inside scrapers silently returning empty lists.
- `ebay_search.py` — `except (JSONDecodeError, OSError)` in cache reads resets to `{}`.
- `server.js` catch blocks frequently `consoleLog` and move on; no retry, no user notification on some paths.

### Logging & observability
- `pino` logger in server.js; `logging` in Python — different formats, no correlation IDs. A single request spans Node→Python subprocess→Anthropic→eBay with no trace link.
- `logs/searches.log` grows unbounded (no rotation observed).
- `logs/ra_debug.log` 86 KB so far — see Bug 18.
- No metrics export. Bot success/fail rate is inferred by reading logs manually.

### Other
- Multi-language (`MSG.en`) scaffolding still exists but `session.lang = 'es'` is force-set at `:851`. Consider deleting EN strings or properly routing language detection.
- `detectLanguage` function is dead (called but output ignored).
- `parse_text.py` in the `search/` folder, while `correction_handler.py` lives in `whatsapp/` — no clear naming convention for "Sonnet routers" vs "Sonnet extractors".

---

## 6. Database / state schema

All tables live in Supabase (`parts-bot` project). Python uses urllib REST; Node uses `@supabase/supabase-js`.

### `parts_sessions`
From `server.js:283–295` INSERT and `:1440–1451` read:

| Column | Type | Source / notes |
|---|---|---|
| `code` | text (unique) | `S-NNNN` via `getNextSessionCode()` |
| `phone_number` | text | digits only |
| `vehicle_vin` | text null | |
| `vehicle_year` | int null | |
| `vehicle_make` | text null | |
| `vehicle_model` | text null | |
| `parts_list` | jsonb | array of extracted parts |
| `results` | jsonb | `search_all_parts` output |
| `supplier_total` | numeric null | DOP total from quote |
| `excel_filename` | text null | |
| `status` | text | `'active'` / `'reviewing'` / `'closed'` |
| `created_at` | timestamptz | (implied server default) |
| `closed_at` | timestamptz null | set on `update status='closed'` |

**Auto-restore query** (:861–868): `status IN ('active','reviewing') AND created_at >= now() - 24h ORDER BY created_at DESC LIMIT 1`.

### `parts_corrections`
From `server.js:552–568` INSERT and `db_client.py:130–147`:

| Column | Type | Notes |
|---|---|---|
| `id` | uuid (assumed) | |
| `vehicle_year` | int null | |
| `vehicle_make` | text | ilike matched |
| `vehicle_model` | text | ilike matched |
| `vin` | text null | |
| `part_index` | int null | 1-based from original list |
| `part_name_original` | text | ilike matched |
| `part_name_corrected` | text | |
| `side_original` / `side_corrected` | text null | |
| `position_original` / `position_corrected` | text null | |
| `correction_message` | text | raw user msg |
| `times_seen` | int | increments |
| `correction_confidence` | text | `'suggested'` / `'likely'` (2+) / `'confirmed'` (3+) |
| `auto_promoted` | bool | true at 3+; triggers translation-cache write |

Uniqueness is enforced **by search**, not DB constraint — the code ilike-matches and increments. Collisions possible on capitalization/whitespace variants.

### `parts_cache`
From `db_client.py:171–226` read, `:262–299` upsert:

| Column | Type | Notes |
|---|---|---|
| `vehicle_make` / `vehicle_model` / `vehicle_year` | text/int | ilike for make/model, eq for year |
| `part_name_english` | text | lowered-stripped |
| `oem_number` | text null | |
| `best_source` | text | `"eBay"` / `"RockAuto"` / `"Cache"` / `"name_fallback"` |
| `best_price_usd` | numeric | |
| `best_url` | text | |
| `result_snapshot` | jsonb | `{best_option, landed_cost}` |
| `verified_by_correction` | bool | true iff user said `listo` with no corrections on that part |
| `last_verified_at` | timestamptz | 30-day TTL on reads |

Upsert key: `(vehicle_make, vehicle_model, vehicle_year, part_name_english)` via `on_conflict` + `Prefer: resolution=merge-duplicates` header.

### Local file state (non-DB)
- `cache/translation_cache.json` — auto-promoted DR→EN translations (3+ correction threshold).
- `cache/vehicles.json` — VIN decode results.
- `cache/ebay_token.json` — OAuth token.
- `cache/rate_limit.json` — daily eBay counter (`{date, count}`).
- `output/*.xlsx`, `output/results_*.json`, `output/input_*.json` — 2-hour TTL.
- `auth/` — Baileys WA session creds.

### Schema concerns flagged for v11
- `parts_cache` has no `vin` column → two different sub-trims of same year/make/model share cache entries.
- `parts_corrections` uniqueness is ilike-matched in app code, not DB-enforced. Risk of `"bonete"` vs `"Bonete"` drift over time.
- `parts_sessions.status='active'` never advances to `'reviewing'` in code — only `'closed'`. The distinction exists in the auto-restore filter but is never written. Minor but misleading.
- No `sessions.history` column; `chatHistories` is in-memory only → session resume forgets conversation.

---

## 7. Readiness assessment for v11

### Keep as-is (stable, good interfaces)
- `search/oem_lookup_7zap.py` — 1203 ln of hard-earned matching logic (fuzzy scoring, hardware blocklist, assembly filters, relay fallback). Expose as a single async tool `lookup_oem_7zap()`.
- `search/ebay_search.py` — rate limit, OAuth cache, body-panel floors are correct shape. Keep as tool.
- `search/verify_listing.py` — tool-ready once model pin is unified (Bug 14).
- `search/cost_calculator.py`, `search/weight_table.py`, `search/vin_decode.py`, `search/manual_review.py` — small pure functions; wrap directly.
- `search/excel_builder.py` — keep; tool call `build_excel(results, output_path)`.
- `search/db_client.py` — Python Supabase layer is fine. Consider extending with `get_session(code)` / `save_session()` equivalents so the agent can do its own persistence without going through server.js.
- `cache/translation_cache.json` + `parts_corrections` learning loop — keep. This is the most valuable data asset the bot has built.

### Decouple (still needed, but move out of `server.js`)
- **Session Map + state transitions** — should live in a `Session` class backed by Supabase, not a JS `Map` that dies on PM2 restart. In-memory cache OK as write-through.
- **Python subprocess bridge** (`runPython`, `handleCorrectionPy`) — in v11 the agent should call these as structured tool calls (or port the pipeline to the same process as the agent loop).
- **Media queue + debounce + text buffer** (5 Maps, timers) — the agent can handle "user sent two images 2s apart" natively; the Node layer should be a thin adapter to Baileys that just forwards `{user_id, text?, media[]?}` events.

### Delete (replaced by agentic loop)
- `MSG` dict (server.js:148–179) — let the agent produce the strings.
- `detectLanguage` + `session.lang` plumbing — agent handles language.
- `confirmWords` (:1204–1207) — intent classification is Sonnet's job.
- `parseCorrection` (server.js:398–471) — superseded by `correction_handler.py`'s ACTION_ENUM. Don't carry both into v11.
- `pending_restore` state + yes/no word-list parser (:889–945) — agent can ask "pick up where we left off?" conversationally.
- `awaiting_vehicle` state + inline Sonnet parser (:948–1009) — agent handles missing vehicle naturally.
- Parts-list trigger regex (`PART_KW_RE`, line-count thresholds) — agent decides intent.
- Session-resume regex / vehicle-keyword regex (:1406–1477) — agent handles "el Tucson de ayer".
- Confirmation-list renderer copies — one view function; better yet, let the agent format the summary.

### Schema migrations suggested for v11
1. Add `parts_sessions.history jsonb` — persist chat history per session so resumes are coherent.
2. Add `parts_sessions.state text` mirroring the JS field so crash recovery is precise.
3. Add `parts_cache.vin text null` — allow per-sub-trim caching; fall back to (make,model,year) when null.
4. Add unique index on `parts_corrections(lower(vehicle_make), lower(vehicle_model), lower(part_name_original), lower(part_name_corrected))`.
5. Optional: `parts_agent_events` (jid, ts, tool_name, args, result, latency_ms) — traces for the agentic loop.

### Hidden risks before flipping to v11

1. **Baileys session creds are in `/opt/parts-bot/auth/`** — any refactor that clears the directory logs the bot out and requires QR re-pairing. Back up before cutover.
2. **WhatsApp LID migration** — server.js:199 already special-cases `@lid` users and bypasses the allow-list. If v11 changes auth logic, LID users who relied on the bypass will silently lose access.
3. **7zap cookie rotation** — `SEVENZAP_RELAY_URL` is in `.env`; direct-mode cookies live in `cache/` and expire. v11 must preserve whichever mode is active.
4. **eBay OAuth token cache** under `cache/ebay_token.json` — migrate, don't clobber, or cold-start hits rate-limit surface.
5. **Outstanding open sessions** — on cutover any `status='active'` or `'reviewing'` rows should be either migrated or explicitly closed. Users who had open review threads will get orphaned.
6. **Hardcoded FX + ClickPack rates** in `cost_calculator.py` — if v11 surface changes pricing display, customers will see new numbers on old cached results (`parts_cache.result_snapshot.landed_cost`). Either invalidate cache on cutover or leave the old values visibly dated.
7. **Learning loop cache** (`cache/translation_cache.json`) — must be migrated with the codebase. Losing it resets months of user corrections.
8. **Subprocess timeout** — server.js hard-codes 300 s (`SEARCH_TIMEOUT_SECONDS`). If v11 replaces the subprocess with in-process async, review any load tests that assumed process-level isolation.

### Small pre-v11 PRs worth landing first (low risk, high signal)

1. **Delete orphans** (Bug 15): root `engine.py`, root `excel_builder.py`, `search/server.js`, `test_7zap.py`, `test_vins.json`, `sessions.json`, three `.bak` files. One PR, pure deletes, verified by rerunning a known quote end-to-end.
2. **Unify Sonnet model pin** (Bug 14): make `claude-sonnet-4-6` the single constant, read from env.
3. **Dedupe PM2 configs** (Bug 12): decide, delete the other.
4. **Filter cache writes** (Bug 19): skip `best_option=null` rows in `cache_verified.py`.
5. **Log rotation + kill ra_debug by default** (Bug 18).
6. **Extract `formatPartsList()`** — the four-copy renderer in `server.js`. Makes subsequent v11 work much easier to read.

Landing 1–6 first keeps v11 focused on the conversational surface rather than janitorial churn.

---

## TL;DR

v10 is **functionally solid** on the pipeline side (7zap + eBay + Sonnet verification + Excel + ClickPack costing + Supabase learning loop) and **fragile on the conversational surface**. The Python side is modular and tool-shaped — most of `search/*.py` can be exposed to an agentic loop almost unchanged. The pain is in `server.js` (1787 ln) where an 8-state string-based machine mixes keyword regex, canned strings (`MSG`/`confirmWords`), two different Sonnet correction parsers (`parseCorrection` legacy + `correction_handler.py` ACTION_ENUM), and four copies of the confirmation-list renderer.

**9 Sonnet call sites across 5 files**; one still pins an older model name (Bug 14). **3 divergent orphan files** (root `engine.py`, root `excel_builder.py`, `search/server.js`) plus 3 `.bak` manual backups sit next to live code — a liability under refactor pressure (Bug 15). Session state lives in **in-memory JS Maps that die on PM2 restart** — only partly recoverable via the `parts_sessions` auto-restore query, which itself can't restore chat history (no column for it). Per-listing Sonnet verification is **gated to 7zap-sourced OEMs only** (Bug 16), so the riskiest path (name-fallback) is unverified. The parts-list router regex is DR-vocabulary-poor (Bug 17) and misroutes legitimate typed quotes to casual chat. eBay has a per-keyword **price floor but no ceiling** (Bug 13).

**Keep** the entire `search/` Python core — it's tool-shaped already. **Decouple** session + state + queue machinery from `server.js` into a proper session store backed by Supabase (add `history` and `state` columns). **Delete** `MSG`, `confirmWords`, `detectLanguage`, `parseCorrection`, the three state-specific text parsers (`pending_restore` yes/no, `awaiting_vehicle` Sonnet, `PART_KW_RE` trigger), and the four renderer copies — the agentic Sonnet loop replaces all of them.

Before cutover: back up `auth/`, migrate `cache/translation_cache.json` (months of user corrections), close or migrate open `parts_sessions`, preserve 7zap cookies / relay URL, and confirm FX + ClickPack rates are still current. Land the 6 small janitorial PRs listed in §7 first — they clear the runway without touching the conversational layer.

v11 readiness: **green on the pipeline, yellow on data persistence, red on the conversational surface** — which is exactly the surface v11 is meant to replace. Proceed.
