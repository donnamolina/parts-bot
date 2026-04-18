"""
Main agent loop.

`run_turn(user_id, user_message, attachments, api_key)` is THE entrypoint that
server.js will reach through the `run_agent.py` subprocess.

Design rules (from v11 spec):
  * No `if state == ...` logic here — tools + history carry state.
  * Max 10 tool iterations per turn.
  * On any error, return a canned Spanish apology so server.js can deliver.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("parts-bot.agent.loop")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import history as _history
from agent import token_strategy as _tok
from agent import tools as _tools


_SONNET_MODEL = os.environ.get("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-6")
_MAX_TOOL_ITERS = 10
_MAX_TOKENS_PER_CALL = 4096

_SYSTEM_PROMPT_PATH = _ROOT / "agent" / "system_prompt.md"


def _load_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"system prompt missing: {e}")
        return "Eres Pieza Finder, un asistente de cotización de piezas."


def _build_user_content(user_message: str, attachments: list[dict] | None) -> Any:
    """Build the content block for the user turn.

    Text-only → string. Anything with attachments becomes a content-block list
    with a text block + a reference describing each attachment (paths only; the
    agent calls extract_from_media to actually process them).
    """
    if not attachments:
        # Empty string would cause API 400 "user messages must have non-empty content"
        return user_message or "[mensaje vacío]"
    text = user_message or ""
    blocks: list[dict] = []
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for a in attachments:
        blocks.append({
            "type": "text",
            "text": (
                f"[adjunto] path={a.get('path')} type={a.get('type')} "
                f"mime={a.get('mime', '')}"
            ),
        })
    return blocks or user_message or ""


def _inject_active_session_context(
    system_prompt: str, active_code: str | None
) -> str:
    if not active_code:
        return system_prompt
    return (
        f"{system_prompt}\n\n# Contexto de sesión activa\n"
        f"El usuario tiene una sesión abierta: `{active_code}`. "
        f"Puedes mencionarla si es relevante, pero no fuerces que la retomen."
    )


async def _call_sonnet(client, messages: list[dict], system_prompt: str):
    """Wrap the Anthropic call so tool-use exceptions don't escape the loop."""
    # We strip our private `_attachments` key before sending to the API.
    clean = [{"role": m["role"], "content": m["content"]} for m in messages]
    return client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=_MAX_TOKENS_PER_CALL,
        system=system_prompt,
        tools=_tools.TOOL_DEFINITIONS,
        messages=clean,
    )


def _anthropic_content_to_messages_append(
    messages: list[dict], assistant_content: list[dict]
) -> list[dict]:
    return [*messages, {"role": "assistant", "content": assistant_content}]


def _tool_results_message(results: list[dict]) -> dict:
    """Package tool_result blocks back to Claude under a single user message."""
    return {"role": "user", "content": results}


async def run_turn(
    user_id: str,
    user_message: str,
    attachments: list[dict] | None,
    api_key: str,
) -> dict:
    """Main agentic turn.

    Returns: {"text": str, "files": [{path, name}]}
    """
    failsafe = {
        "text": "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.",
        "files": [],
    }

    if not api_key:
        logger.error("run_turn called without api_key")
        return failsafe

    # Install outbox for this turn so tools (send_document etc) can queue files.
    outbox = _tools.Outbox(phone=user_id)
    _tools.set_outbox(outbox)

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic SDK not installed")
        return failsafe

    try:
        from search import db_client  # type: ignore
    except Exception as e:
        logger.error(f"cannot import db_client: {e}")
        return failsafe

    # 1) Load history
    messages, active_code = _history.load_history(user_id, db_client)

    # 2) Append user message
    messages = _history.append_message(
        messages,
        role="user",
        content=_build_user_content(user_message, attachments),
        attachments=attachments,
    )

    # 3/4) Build system prompt with active-session context
    system_prompt = _inject_active_session_context(_load_system_prompt(), active_code)

    # 5) Truncate if needed
    trimmed = _tok.truncate_history(messages, api_key, phone=user_id)

    # 6/7) Iterate Sonnet + tool calls
    client = anthropic.Anthropic(api_key=api_key)

    iters = 0
    final_text = ""
    assistant_final_content: list[dict] | None = None
    current_messages = trimmed

    while iters < _MAX_TOOL_ITERS:
        iters += 1
        try:
            resp = await _call_sonnet(client, current_messages, system_prompt)
        except Exception as e:
            logger.exception(f"Sonnet call failed: {e}")
            return failsafe

        stop_reason = getattr(resp, "stop_reason", None)
        content_blocks: list[dict] = []
        tool_uses: list[dict] = []
        text_chunks: list[str] = []

        for block in resp.content:
            # Normalize SDK objects into dicts for storage
            btype = getattr(block, "type", None)
            if btype == "text":
                t = getattr(block, "text", "")
                text_chunks.append(t)
                content_blocks.append({"type": "text", "text": t})
            elif btype == "tool_use":
                tu = {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
                tool_uses.append(tu)
                content_blocks.append(tu)
            else:  # pragma: no cover — unknown block type
                content_blocks.append({"type": btype or "unknown"})

        # Record the assistant turn
        current_messages = _anthropic_content_to_messages_append(
            current_messages, content_blocks
        )
        assistant_final_content = content_blocks
        final_text = "\n".join(t for t in text_chunks if t).strip()

        if stop_reason != "tool_use" or not tool_uses:
            # Done — we have a textual response (or an empty one; either way stop)
            break

        # Execute tools in order
        tool_results: list[dict] = []
        for tu in tool_uses:
            name = tu["name"]
            args = tu.get("input") or {}
            result = await _tools.dispatch(name, args)
            # tool_result blocks live in a user turn per the API contract
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": _stringify_result(result),
            })

        current_messages.append(_tool_results_message(tool_results))

    # 8) Save history — we re-use the original pre-trim messages plus whatever
    # the loop produced (but without re-adding the user-appended prefix twice).
    # Simpler: persist the messages we actually sent to Claude on the last
    # iteration. They already contain the full thread for this turn.
    try:
        # Active session code: look for the most recent search_all_parts / save_session
        new_code = _scan_for_session_code(current_messages) or active_code
        _history.save_history(user_id, current_messages, new_code, db_client)
    except Exception as e:
        logger.warning(f"save_history failed: {e}")

    return {
        "text": final_text or "",
        "files": list(outbox.files),
        "typing": list(outbox.typing),
    }


def _stringify_result(result: Any) -> Any:
    """Anthropic wants tool_result.content as a string OR a list of content blocks.

    We always return JSON-stringified for simplicity.
    """
    import json
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        return str(result)


def _scan_for_session_code(messages: list[dict]) -> str | None:
    """Find the latest S-NNNN mentioned in a tool_result."""
    import re
    pat = re.compile(r"\bS-(\d{3,4})\b")
    for m in reversed(messages):
        c = m.get("content")
        if isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content", "")
                    if isinstance(body, str):
                        mm = pat.search(body)
                        if mm:
                            return f"S-{mm.group(1)}"
        elif isinstance(c, str):
            mm = pat.search(c)
            if mm:
                return f"S-{mm.group(1)}"
    return None
