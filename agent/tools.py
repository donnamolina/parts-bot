"""
Agent tools.

Every tool is an async Python function that takes JSON-serializable inputs and
returns a JSON-serializable dict. `TOOL_DEFINITIONS` is the Anthropic tool
schema list — what we pass to `client.messages.create(tools=...)`.

Tools never send WhatsApp directly. `send_document` and `send_typing_indicator`
enqueue side-effect requests into a shared `OUTBOX` that the agent loop
returns to server.js, which owns the Baileys socket. This keeps the Python
subprocess standalone-testable.

All tools log a row into `parts_agent_events` (fire-and-forget) so we have
telemetry on tool usage / latency / errors.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("parts-bot.agent.tools")

_ROOT = Path(__file__).resolve().parent.parent

# Make the search/ and whatsapp/ packages importable
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Side-effect outbox shared with the agent loop ──────────────────────────

class Outbox:
    """Accumulates files to send and typing events for the current turn."""

    def __init__(self, phone: str):
        self.phone = phone
        self.files: list[dict] = []        # [{path, name}]
        self.typing: list[dict] = []       # [{duration_seconds}]

    def reset(self):
        self.files.clear()
        self.typing.clear()


_current_outbox: Outbox | None = None


def set_outbox(outbox: Outbox) -> None:
    global _current_outbox
    _current_outbox = outbox


def get_outbox() -> Outbox | None:
    return _current_outbox


# ── Telemetry ──────────────────────────────────────────────────────────────

def _log_event(
    event_type: str,
    tool_name: str | None = None,
    args: Any = None,
    result: Any = None,
    latency_ms: int | None = None,
    session_code: str | None = None,
) -> None:
    """Fire-and-forget insert into parts_agent_events."""
    try:
        from search import db_client  # type: ignore
        phone = _current_outbox.phone if _current_outbox else ""
        db_client._req("POST", "parts_agent_events", body={  # noqa: SLF001
            "phone_number": phone,
            "session_code": session_code,
            "event_type": event_type,
            "tool_name": tool_name,
            "args": _redact(args) if args is not None else None,
            "result": _redact(result) if result is not None else None,
            "latency_ms": latency_ms,
        })
    except Exception as e:  # pragma: no cover
        logger.debug(f"agent_events write failed: {e}")


def _redact(value: Any) -> Any:
    """Strip things we don't want to shove into Supabase telemetry."""
    try:
        s = json.dumps(value, default=str)
        if len(s) > 8_000:
            return {"_truncated": True, "preview": s[:2000]}
        return json.loads(s)
    except Exception:
        return {"_unserializable": True}


def tool(fn: Callable):
    """Decorator: wraps a tool fn with timing + telemetry + error shielding."""

    async def _wrap(*args, **kwargs):
        start = time.perf_counter()
        name = fn.__name__
        try:
            result = await fn(*args, **kwargs)
            latency = int((time.perf_counter() - start) * 1000)
            _log_event(
                "tool_call",
                tool_name=name,
                args={"args": args, "kwargs": kwargs},
                result=result,
                latency_ms=latency,
            )
            return result
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            logger.error(f"tool {name} raised: {e}")
            _log_event(
                "tool_error",
                tool_name=name,
                args={"args": args, "kwargs": kwargs},
                result={"error": str(e)},
                latency_ms=latency,
            )
            return {"error": str(e), "tool": name}

    _wrap.__name__ = fn.__name__
    _wrap.__doc__ = fn.__doc__
    return _wrap


# ─────────────────────────────────────────────────────────────────────────────
# Extraction tools
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def extract_from_media(media_path: str, media_type: str) -> dict:
    """OCR a photo or PDF into {vehicle, vin, parts, supplier_total_dop}."""
    from search import ocr_extract  # type: ignore
    ext = (media_type or "").lower().strip(".")
    p = Path(media_path)
    if not p.exists():
        return {"error": f"media_path not found: {media_path}"}

    if ext == "pdf" or ext == "application/pdf" or media_path.lower().endswith(".pdf"):
        data = await ocr_extract.extract_from_pdf(str(p))
    else:
        data = await ocr_extract.extract_from_image(str(p))
    return data


@tool
async def extract_from_text(raw_text: str) -> dict:
    """Parse a free-text parts list message into the extraction envelope.

    Uses search/parse_text.py which may call Sonnet for vehicle disambiguation.
    """
    from search import parse_text  # type: ignore
    # parse_text exposes `parse_text_list(text)` for the sync pathway.
    data = parse_text.parse_text_list(raw_text)

    # Fill in VIN decode if we got one
    vin = data.get("vin") or ""
    if vin and len(vin) == 17:
        try:
            from search.vin_decode import decode_vin  # type: ignore
            decoded = await decode_vin(vin)
            if not decoded.get("error"):
                v = data.get("vehicle") or {}
                v["year"] = v.get("year") or decoded.get("year")
                v["make"] = v.get("make") or decoded.get("make")
                v["model"] = v.get("model") or decoded.get("model")
                data["vehicle"] = v
        except Exception as e:
            logger.warning(f"parse_text VIN decode failed: {e}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Search pipeline
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def search_all_parts(
    vehicle: dict,
    parts: list[dict],
    session_code: str | None = None,
    supplier_total_dop: float | None = None,
    supplier_quotes: list | None = None,
) -> dict:
    """Run the full search pipeline. Returns {results, excel_path, session_code, summary}."""
    from search import engine  # type: ignore
    from search.excel_builder import generate_excel  # type: ignore
    from search.vin_decode import decode_vin  # type: ignore

    vehicle = dict(vehicle or {})
    vin = vehicle.get("vin", "")
    if vin and len(vin) == 17 and not vehicle.get("make"):
        decoded = await decode_vin(vin)
        if not decoded.get("error"):
            vehicle["year"] = decoded.get("year")
            vehicle["make"] = decoded.get("make")
            vehicle["model"] = decoded.get("model")

    if not parts:
        return {"error": "parts list is empty"}

    results = await engine.search_all_parts(parts, vehicle)

    # Excel
    code = session_code or _next_session_code()
    ts = int(time.time())
    excel_filename = f"pieza_finder_{code}_{ts}.xlsx"
    excel_path = str(_ROOT / "output" / excel_filename)
    Path(excel_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        generate_excel(results, vehicle, excel_path, supplier_total_dop=supplier_total_dop, supplier_quotes=supplier_quotes or [], sonnet_flags=[])
    except Exception as e:
        logger.error(f"excel gen failed: {e}")
        excel_path = None

    found = sum(1 for r in results if r.get("best_option") and r["best_option"].get("price"))
    review = sum(1 for r in results if r.get("manual_review") or (r.get("sonnet_verify") or {}).get("verdict") == "WRONG_PART")
    total_landed = 0.0
    for r in results:
        lc = r.get("landed_cost") or {}
        if lc.get("total_landed_dop"):
            total_landed += float(lc["total_landed_dop"])

    summary = {
        "total": len(results),
        "found": found,
        "review": review,
        "not_found": len(results) - found - review,
        "total_landed_dop": round(total_landed, 2),
    }

    # Persist session row
    _save_session_row(code, vehicle, parts, results, excel_filename)

    return {
        "session_code": code,
        "results": results,
        "excel_path": excel_path,
        "excel_filename": excel_filename,
        "summary": summary,
        "vehicle": vehicle,
    }


@tool
async def search_single_part(
    session_code: str,
    part_index: int,
    updated_part: dict | None = None,
) -> dict:
    """Re-run the pipeline for one part on an existing session.

    Returns {result, excel_path, session_code}. The caller is expected to
    invoke `regen_excel` separately if they want the spreadsheet updated;
    we do it here for them to keep round-trips tight.
    """
    from search import engine  # type: ignore
    from search.excel_builder import generate_excel  # type: ignore

    session = await load_session_by_code(session_code)  # type: ignore
    if not session or session.get("error"):
        return {"error": f"session {session_code} not found"}

    vehicle = session.get("vehicle") or {}
    results = list(session.get("results") or [])
    if part_index < 1 or part_index > len(results):
        return {"error": f"part_index {part_index} out of range (1..{len(results)})"}

    i = part_index - 1
    part = updated_part or results[i].get("part") or {}
    new_results = await engine.search_all_parts([part], vehicle)
    if not new_results:
        return {"error": "search returned nothing"}
    results[i] = new_results[0]

    ts = int(time.time())
    excel_filename = f"pieza_finder_{session_code}_{ts}.xlsx"
    excel_path = str(_ROOT / "output" / excel_filename)
    try:
        generate_excel(results, vehicle, excel_path, supplier_total_dop=None, sonnet_flags=[])
    except Exception as e:
        logger.error(f"excel regen failed: {e}")
        excel_path = None

    # Persist
    _update_session_results(session_code, results, excel_filename)

    return {
        "session_code": session_code,
        "result": results[i],
        "excel_path": excel_path,
        "excel_filename": excel_filename,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def save_session(
    phone: str,
    vehicle: dict,
    parts_list: list[dict],
    results: list[dict],
    excel_filename: str,
) -> dict:
    """Upsert a parts_sessions row AND cache any final listings."""
    from search import db_client  # type: ignore

    code = _next_session_code()
    year = vehicle.get("year") or 0
    db_client._req("POST", "parts_sessions", body={  # noqa: SLF001
        "code": code,
        "phone_number": phone,
        "vehicle_vin": vehicle.get("vin"),
        "vehicle_year": year,
        "vehicle_make": vehicle.get("make"),
        "vehicle_model": vehicle.get("model"),
        "parts_list": parts_list,
        "results": results,
        "supplier_total": vehicle.get("supplier_total"),
        "excel_filename": excel_filename,
        "status": "active",
    })
    for r in results:
        if not r.get("best_option") or not r["best_option"].get("price"):
            continue
        db_client.upsert_cached_result_safe(
            vehicle.get("make", ""),
            vehicle.get("model", ""),
            year,
            (r.get("part") or {}).get("name_english", ""),
            r,
            verified_by_correction=False,
        )
    return {"session_code": code}


@tool
async def load_session_by_code(code: str) -> dict:
    """Fetch a parts_sessions row by S-code."""
    from search import db_client  # type: ignore
    if not code:
        return {"error": "code required"}
    rows = db_client._req("GET", "parts_sessions", params={  # noqa: SLF001
        "code": f"eq.{code}",
        "limit": "1",
    })
    if not rows or not isinstance(rows, list):
        return {"error": f"no session {code}"}
    row = rows[0]
    return {
        "session_code": code,
        "phone_number": row.get("phone_number"),
        "vehicle": {
            "vin": row.get("vehicle_vin"),
            "year": row.get("vehicle_year"),
            "make": row.get("vehicle_make"),
            "model": row.get("vehicle_model"),
        },
        "parts_list": row.get("parts_list") or [],
        "results": row.get("results") or [],
        "excel_filename": row.get("excel_filename"),
        "status": row.get("status"),
        "supplier_total": row.get("supplier_total"),
    }


@tool
async def search_past_sessions(phone: str, query: str, limit: int = 5) -> dict:
    """ILIKE-search a user's past sessions by vehicle make/model/code."""
    from search import db_client  # type: ignore
    if not phone:
        return {"error": "phone required"}
    q = (query or "").strip()
    params = {
        "phone_number": f"eq.{phone}",
        "order": "created_at.desc",
        "limit": str(limit),
        "select": "code,vehicle_year,vehicle_make,vehicle_model,status,created_at,excel_filename",
    }
    if q:
        # Supabase `or=` filter
        params["or"] = (
            f"(code.ilike.*{q}*,vehicle_make.ilike.*{q}*,vehicle_model.ilike.*{q}*)"
        )
    rows = db_client._req("GET", "parts_sessions", params=params)  # noqa: SLF001
    return {"sessions": rows or []}


@tool
async def close_session(session_code: str) -> dict:
    """Mark a session closed. Returns {session_code, status}."""
    from search import db_client  # type: ignore
    from datetime import datetime, timezone
    db_client._req(  # noqa: SLF001
        "PATCH",
        f"parts_sessions?code=eq.{session_code}",
        body={
            "status": "closed",
            "closed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"session_code": session_code, "status": "closed"}


@tool
async def cache_verified_results(session_code: str) -> dict:
    """Wrap search/cache_verified.py — marks results as verified_by_correction."""
    session = await load_session_by_code(session_code)  # type: ignore
    if not session or session.get("error"):
        return {"error": f"session {session_code} not found"}

    import tempfile
    v = session.get("vehicle") or {}
    results = session.get("results") or []
    if not results:
        return {"cached": 0, "skipped": 0}

    # Write a temp results JSON file for cache_verified.py CLI
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"vehicle": v, "results": results}, tmp)
    tmp.close()

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_ROOT / "search" / "cache_verified.py"),
        "--results-json", tmp.name,
        "--vehicle-make", v.get("make", ""),
        "--vehicle-model", v.get("model", ""),
        "--vehicle-year", str(v.get("year") or 0),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    try:
        Path(tmp.name).unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        pass
    try:
        return json.loads((out or b"").decode())
    except Exception:
        return {"stdout": (out or b"").decode(errors="replace"),
                "stderr": (err or b"").decode(errors="replace")}


# ─────────────────────────────────────────────────────────────────────────────
# Correction helpers (in-memory parts manipulation)
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def update_part_quantity(
    parts: list[dict],
    part_index: int,
    new_quantity: int,
) -> dict:
    parts = [dict(p) for p in (parts or [])]
    i = part_index - 1
    if not (0 <= i < len(parts)):
        return {"error": f"index out of range", "parts": parts}
    parts[i]["quantity"] = int(new_quantity or 1)
    return {"parts": parts, "updated_index": part_index}


@tool
async def rename_part(
    parts: list[dict],
    part_index: int,
    new_name_original: str,
    new_side: str | None = None,
    new_position: str | None = None,
) -> dict:
    from search.dictionary import translate_part  # type: ignore
    parts = [dict(p) for p in (parts or [])]
    i = part_index - 1
    if not (0 <= i < len(parts)):
        return {"error": "index out of range", "parts": parts}
    tr = translate_part(new_name_original)
    parts[i].update({
        "name_original": new_name_original,
        "name_dr": tr.get("name_dr", new_name_original),
        "name_english": tr.get("name_english", new_name_original),
        "side": new_side if new_side is not None else tr.get("side"),
        "position": new_position if new_position is not None else tr.get("position"),
    })
    return {"parts": parts, "updated_index": part_index}


@tool
async def add_part(
    parts: list[dict],
    name_original: str,
    quantity: int = 1,
    side: str | None = None,
    position: str | None = None,
) -> dict:
    from search.dictionary import translate_part  # type: ignore
    parts = [dict(p) for p in (parts or [])]
    tr = translate_part(name_original)
    parts.append({
        "name_original": name_original,
        "name_dr": tr.get("name_dr", name_original),
        "name_english": tr.get("name_english", name_original),
        "side": side if side is not None else tr.get("side"),
        "position": position if position is not None else tr.get("position"),
        "quantity": quantity,
        "local_price": 0,
    })
    return {"parts": parts, "added_index": len(parts)}


@tool
async def remove_part(parts: list[dict], part_index: int) -> dict:
    parts = [dict(p) for p in (parts or [])]
    i = part_index - 1
    if not (0 <= i < len(parts)):
        return {"error": "index out of range", "parts": parts}
    removed = parts.pop(i)
    return {"parts": parts, "removed": removed}


@tool
async def update_vehicle(
    vehicle: dict,
    year: int | None = None,
    make: str | None = None,
    model: str | None = None,
    vin: str | None = None,
) -> dict:
    v = dict(vehicle or {})
    if year is not None:
        v["year"] = int(year)
    if make is not None:
        v["make"] = make
    if model is not None:
        v["model"] = model
    if vin is not None:
        v["vin"] = vin
        if len(vin) == 17:
            try:
                from search.vin_decode import decode_vin  # type: ignore
                decoded = await decode_vin(vin)
                if not decoded.get("error"):
                    v["year"] = v.get("year") or decoded.get("year")
                    v["make"] = v.get("make") or decoded.get("make")
                    v["model"] = v.get("model") or decoded.get("model")
            except Exception as e:
                logger.warning(f"vin decode failed: {e}")
    return {"vehicle": v}


# ─────────────────────────────────────────────────────────────────────────────
# Learning
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def log_correction(
    vehicle: dict,
    part_original: dict,
    part_corrected: str,
    correction_message: str,
    part_index: int,
) -> dict:
    from search import db_client  # type: ignore
    db_client.upsert_correction(
        vehicle or {},
        part_original or {},
        part_corrected,
        correction_message,
        part_index,
    )
    return {"logged": True}


# ─────────────────────────────────────────────────────────────────────────────
# Platform (outbox — server.js actually sends)
# ─────────────────────────────────────────────────────────────────────────────

@tool
async def send_document(phone: str, file_path: str, filename: str) -> dict:
    """Queue an Excel/PDF for server.js to send via Baileys. Returns queued=True.

    The agent loop returns these files in its result payload; server.js sends
    them out-of-band. We do NOT call /internal/send-document during the loop
    to keep the Python subprocess runnable in isolation (Phase 4 test).
    """
    outbox = get_outbox()
    if outbox is None:
        return {"error": "no outbox set"}
    if not (phone and file_path and filename):
        return {"error": "phone, file_path, filename all required"}
    outbox.files.append({"path": file_path, "name": filename})
    return {"queued": True, "path": file_path, "name": filename}


@tool
async def send_typing_indicator(phone: str, duration_seconds: int = 3) -> dict:
    """Queue a typing indicator for server.js."""
    outbox = get_outbox()
    if outbox is None:
        return {"error": "no outbox set"}
    outbox.typing.append({"duration_seconds": int(duration_seconds)})
    return {"queued": True}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _next_session_code() -> str:
    """Allocate a new S-NNNN. Uses parts_sessions COUNT +1 as a cheap sequence."""
    try:
        from search import db_client  # type: ignore
        rows = db_client._req("GET", "parts_sessions", params={  # noqa: SLF001
            "select": "code",
            "order": "id.desc",
            "limit": "1",
        })
        if rows and isinstance(rows, list) and rows:
            last = rows[0].get("code") or ""
            n = int((last.split("-") or ["0", "0"])[-1] or 0)
            return f"S-{n + 1:04d}"
    except Exception as e:
        logger.warning(f"_next_session_code failed: {e}")
    return f"S-{int(time.time()) % 10000:04d}"


def _save_session_row(
    code: str,
    vehicle: dict,
    parts: list[dict],
    results: list[dict],
    excel_filename: str | None,
) -> None:
    try:
        from search import db_client  # type: ignore
        phone = _current_outbox.phone if _current_outbox else ""
        db_client._req("POST", "parts_sessions", body={  # noqa: SLF001
            "code": code,
            "phone_number": phone,
            "vehicle_vin": vehicle.get("vin"),
            "vehicle_year": vehicle.get("year"),
            "vehicle_make": vehicle.get("make"),
            "vehicle_model": vehicle.get("model"),
            "parts_list": parts,
            "results": results,
            "excel_filename": excel_filename,
            "status": "active",
        })
    except Exception as e:
        logger.warning(f"_save_session_row failed: {e}")


def _update_session_results(code: str, results: list[dict], excel_filename: str | None) -> None:
    try:
        from search import db_client  # type: ignore
        db_client._req(  # noqa: SLF001
            "PATCH",
            f"parts_sessions?code=eq.{code}",
            body={"results": results, "excel_filename": excel_filename},
        )
    except Exception as e:
        logger.warning(f"_update_session_results failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic tool-definitions manifest
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "extract_from_media",
        "description": (
            "OCR a photo or PDF of a supplier quote. Returns vehicle, vin, and a "
            "parts list. Call this when the user sends an image or PDF."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "media_path": {"type": "string"},
                "media_type": {"type": "string", "description": "Extension or MIME type (e.g. 'jpg', 'pdf')"},
            },
            "required": ["media_path", "media_type"],
        },
    },
    {
        "name": "extract_from_text",
        "description": (
            "Parse a free-text parts list (DR Spanish or English). Returns the "
            "same shape as extract_from_media. Call this when the user types parts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_text": {"type": "string"},
            },
            "required": ["raw_text"],
        },
    },
    {
        "name": "search_all_parts",
        "description": (
            "Run the full pipeline for a vehicle + parts list. Returns results, "
            "an Excel path, and a session_code. Creates a parts_sessions row."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle": {"type": "object"},
                "parts": {"type": "array", "items": {"type": "object"}},
                "session_code": {"type": "string"},
            },
            "required": ["vehicle", "parts"],
        },
    },
    {
        "name": "search_single_part",
        "description": (
            "Re-search one part (by 1-based index) on an existing session. "
            "Regenerates the Excel. Returns the updated result + new excel_path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_code": {"type": "string"},
                "part_index": {"type": "integer"},
                "updated_part": {"type": "object"},
            },
            "required": ["session_code", "part_index"],
        },
    },
    {
        "name": "save_session",
        "description": "Persist a new parts_sessions row. Returns its S-code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "vehicle": {"type": "object"},
                "parts_list": {"type": "array"},
                "results": {"type": "array"},
                "excel_filename": {"type": "string"},
            },
            "required": ["phone", "vehicle", "parts_list", "results", "excel_filename"],
        },
    },
    {
        "name": "load_session_by_code",
        "description": "Fetch a parts_sessions row by S-code.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "search_past_sessions",
        "description": "ILIKE-search a phone's past sessions. Returns up to `limit` rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["phone", "query"],
        },
    },
    {
        "name": "close_session",
        "description": "Mark a session closed.",
        "input_schema": {
            "type": "object",
            "properties": {"session_code": {"type": "string"}},
            "required": ["session_code"],
        },
    },
    {
        "name": "cache_verified_results",
        "description": (
            "Mark all priced rows in a session as verified_by_correction. "
            "Skips N/F rows automatically. Call after close_session when user "
            "confirms everything is correct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"session_code": {"type": "string"}},
            "required": ["session_code"],
        },
    },
    {
        "name": "update_part_quantity",
        "description": "Change a part's quantity (1-based index).",
        "input_schema": {
            "type": "object",
            "properties": {
                "parts": {"type": "array"},
                "part_index": {"type": "integer"},
                "new_quantity": {"type": "integer"},
            },
            "required": ["parts", "part_index", "new_quantity"],
        },
    },
    {
        "name": "rename_part",
        "description": "Replace a part's name (and optionally side/position).",
        "input_schema": {
            "type": "object",
            "properties": {
                "parts": {"type": "array"},
                "part_index": {"type": "integer"},
                "new_name_original": {"type": "string"},
                "new_side": {"type": "string"},
                "new_position": {"type": "string"},
            },
            "required": ["parts", "part_index", "new_name_original"],
        },
    },
    {
        "name": "add_part",
        "description": "Append a part to the in-memory list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parts": {"type": "array"},
                "name_original": {"type": "string"},
                "quantity": {"type": "integer"},
                "side": {"type": "string"},
                "position": {"type": "string"},
            },
            "required": ["parts", "name_original"],
        },
    },
    {
        "name": "remove_part",
        "description": "Drop a part by 1-based index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parts": {"type": "array"},
                "part_index": {"type": "integer"},
            },
            "required": ["parts", "part_index"],
        },
    },
    {
        "name": "update_vehicle",
        "description": "Patch the vehicle dict. If vin is 17-char, decodes VIN.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle": {"type": "object"},
                "year": {"type": "integer"},
                "make": {"type": "string"},
                "model": {"type": "string"},
                "vin": {"type": "string"},
            },
            "required": ["vehicle"],
        },
    },
    {
        "name": "log_correction",
        "description": (
            "Record that the user corrected part_original → part_corrected. "
            "Feeds the learning loop (parts_corrections + translation_cache)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle": {"type": "object"},
                "part_original": {"type": "object"},
                "part_corrected": {"type": "string"},
                "correction_message": {"type": "string"},
                "part_index": {"type": "integer"},
            },
            "required": ["vehicle", "part_original", "part_corrected", "correction_message", "part_index"],
        },
    },
    {
        "name": "send_document",
        "description": (
            "Queue a file (Excel/PDF) for delivery via WhatsApp. server.js handles "
            "the actual send — this just enqueues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "file_path": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["phone", "file_path", "filename"],
        },
    },
    {
        "name": "send_typing_indicator",
        "description": "Show a typing indicator for `duration_seconds`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "duration_seconds": {"type": "integer"},
            },
            "required": ["phone"],
        },
    },
]


# ── dispatcher — used by loop.py to execute a tool call by name ────────────

_DISPATCH: dict[str, Callable] = {
    "extract_from_media": extract_from_media,
    "extract_from_text": extract_from_text,
    "search_all_parts": search_all_parts,
    "search_single_part": search_single_part,
    "save_session": save_session,
    "load_session_by_code": load_session_by_code,
    "search_past_sessions": search_past_sessions,
    "close_session": close_session,
    "cache_verified_results": cache_verified_results,
    "update_part_quantity": update_part_quantity,
    "rename_part": rename_part,
    "add_part": add_part,
    "remove_part": remove_part,
    "update_vehicle": update_vehicle,
    "log_correction": log_correction,
    "send_document": send_document,
    "send_typing_indicator": send_typing_indicator,
}


async def dispatch(name: str, arguments: dict) -> Any:
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    args = arguments or {}
    try:
        return await fn(**args)
    except TypeError as e:
        return {"error": f"bad args for {name}: {e}"}
