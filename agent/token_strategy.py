"""
Token budget management for the agent loop.

Four tiers (thresholds on estimated prompt tokens):

    Tier 1:  <10,000  → no-op.
    Tier 2:  10k–15k  → strip large binary blobs (base64 images/PDFs already
                         persisted to disk), truncate tool_result bodies
                         longer than ~2,000 tokens.
    Tier 3:  15k–25k  → keep the last 5 turns verbatim. Everything older goes
                         to Haiku for a 1-2 paragraph summary that replaces
                         the older messages.
    Tier 4:  >25k     → emergency: keep last 3 turns only. Log the compaction
                         to `parts_agent_events` so we see it in telemetry.

Token estimation uses a cheap heuristic (chars/4) — good enough for budget
decisions; we don't need to call tiktoken here.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("parts-bot.agent.tokens")

_HAIKU_MODEL = os.environ.get("ANTHROPIC_HAIKU_MODEL", "claude-haiku-4-5-20251001")

# Approx chars-per-token for Anthropic's tokenizer on English+Spanish prose.
_CHARS_PER_TOKEN = 4

# Tier thresholds (see docstring).
TIER_2_THRESHOLD = 10_000
TIER_3_THRESHOLD = 15_000
TIER_4_THRESHOLD = 25_000

# Max tokens we allow a single tool_result body to occupy before we truncate it.
TOOL_RESULT_MAX_TOKENS = 2_000

# Keys that typically carry large base64 blobs we can safely strip once the
# file has been persisted to disk (the OCR/parse tools return a `media_path`
# alongside the bytes).
_BINARY_KEYS = ("image_bytes", "pdf_bytes", "binary", "base64", "bytes", "data_url")


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _content_to_text(content: Any) -> str:
    """Best-effort stringify of a message `content` for estimation."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {}), default=str))
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    parts.append(_content_to_text(c))
                elif block.get("type") == "image":
                    # Count image source string length — approximates the base64 payload
                    src = block.get("source", {})
                    parts.append(json.dumps(src, default=str))
                else:
                    parts.append(json.dumps(block, default=str))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def estimate_tokens(messages: list[dict]) -> int:
    """Return a coarse token count for a list of API-style messages."""
    total = 0
    for msg in messages:
        total += _approx_tokens(_content_to_text(msg.get("content", "")))
    return total


def _strip_binary_blobs(content: Any) -> Any:
    """Walk a message content payload and replace base64 blobs with a stub."""
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image":
                    src = block.get("source") or {}
                    data = src.get("data") or ""
                    if isinstance(data, str) and len(data) > 1024:
                        block = dict(block)
                        block["source"] = {
                            **src,
                            "data": "[image stripped: already persisted to disk]",
                        }
                elif block.get("type") == "tool_result":
                    block = dict(block)
                    block["content"] = _strip_binary_blobs(block.get("content"))
                out.append(block)
            else:
                out.append(block)
        return out
    if isinstance(content, dict):
        clean: dict[str, Any] = {}
        for k, v in content.items():
            if k in _BINARY_KEYS and isinstance(v, str) and len(v) > 1024:
                clean[k] = "[binary stripped]"
            else:
                clean[k] = _strip_binary_blobs(v) if isinstance(v, (list, dict)) else v
        return clean
    return content


def _truncate_tool_results(messages: list[dict]) -> list[dict]:
    """For each tool_result block whose text is > TOOL_RESULT_MAX_TOKENS, trim it."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content", "")
                    body_text = _content_to_text(body)
                    if _approx_tokens(body_text) > TOOL_RESULT_MAX_TOKENS:
                        cutoff_chars = TOOL_RESULT_MAX_TOKENS * _CHARS_PER_TOKEN
                        truncated = body_text[:cutoff_chars] + (
                            f"\n\n…[truncated — original was ~{_approx_tokens(body_text)} tokens]"
                        )
                        block = dict(block)
                        block["content"] = truncated
                new_blocks.append(block)
            out.append({**msg, "content": new_blocks})
        else:
            out.append(msg)
    return out


def _haiku_summarize(older_messages: list[dict], api_key: str) -> str:
    """Ask Haiku to summarize older turns into a terse paragraph.

    Returns the summary text (or a failsafe string on any error — never raises).
    """
    if not older_messages:
        return ""
    try:
        import anthropic  # lazy import
    except ImportError:
        return "[summary unavailable: anthropic package missing]"

    convo = []
    for msg in older_messages:
        text = _content_to_text(msg.get("content", ""))
        convo.append(f"{msg.get('role', '?').upper()}: {text[:2000]}")
    joined = "\n\n".join(convo)

    prompt = (
        "Resume esta conversación de Pieza Finder en 2-4 oraciones en español DR. "
        "Menciona: vehículo, piezas que pidieron, acciones hechas (búsquedas, correcciones), "
        "estado actual (esperando confirmación, ya entregué Excel, cerrado, etc). "
        "NO incluyas precios exactos — solo 'encontré X, faltaron Y'. Sé breve.\n\n"
        f"CONVERSACIÓN:\n{joined}"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Haiku summarization failed: {e}")
        return f"[resumen no disponible — {len(older_messages)} turnos previos]"


def _emergency_log(phone: str, tier: int, before: int, after: int):
    """Record a tier-4 compaction to parts_agent_events (fire-and-forget)."""
    try:
        from search import db_client as _db  # type: ignore
        # db_client doesn't have a direct agent-events helper, so use the REST _req
        _db._req("POST", "parts_agent_events", body={
            "phone_number": phone or "",
            "event_type": "token_tier4_compaction",
            "args": {"tier": tier, "tokens_before": before, "tokens_after": after},
        })
    except Exception as e:  # pragma: no cover — telemetry is best-effort
        logger.debug(f"parts_agent_events write failed: {e}")


def truncate_history(
    messages: list[dict],
    api_key: str,
    phone: str | None = None,
) -> list[dict]:
    """Apply the tiered compaction strategy to `messages`.

    `messages` is the OpenAI-style list of `{role, content}` dicts — the same
    shape Anthropic's Python SDK accepts.

    Return value is always safe to pass straight to Anthropic. On any error
    inside Haiku summarization we fall back to the tier-4 behavior (keep the
    last 3 turns).
    """
    if not messages:
        return messages

    before = estimate_tokens(messages)

    # Tier 1 — free pass.
    if before < TIER_2_THRESHOLD:
        return messages

    # Tier 2 — strip blobs, truncate bloated tool_results.
    if before < TIER_3_THRESHOLD:
        stripped = [
            {**m, "content": _strip_binary_blobs(m.get("content"))} for m in messages
        ]
        out = _truncate_tool_results(stripped)
        logger.info(
            f"token tier 2: {before} → {estimate_tokens(out)} tokens (stripped blobs)"
        )
        return out

    # Tier 3 — keep last 5 turns verbatim, Haiku-summarize the rest.
    if before < TIER_4_THRESHOLD:
        if len(messages) <= 5:
            return _truncate_tool_results(
                [{**m, "content": _strip_binary_blobs(m.get("content"))} for m in messages]
            )
        older = messages[:-5]
        recent = messages[-5:]
        summary = _haiku_summarize(older, api_key)
        out = [
            {"role": "user", "content": f"[Resumen de turnos anteriores]\n{summary}"},
            *recent,
        ]
        out = _truncate_tool_results(out)
        logger.info(
            f"token tier 3: {before} → {estimate_tokens(out)} tokens "
            f"(summarized {len(older)} older turns)"
        )
        return out

    # Tier 4 — emergency.
    recent = messages[-3:] if len(messages) > 3 else messages
    out = _truncate_tool_results(
        [{**m, "content": _strip_binary_blobs(m.get("content"))} for m in recent]
    )
    after = estimate_tokens(out)
    logger.warning(f"token tier 4 EMERGENCY: {before} → {after} (kept last 3 turns)")
    if phone:
        _emergency_log(phone, 4, before, after)
    return out
