"""
Conversation history — Supabase-backed.

Provides:
  load_history(phone, db_client)
  save_history(phone, messages, active_session_code, db_client)
  append_message(messages, role, content, attachments=None)  # pure helper
  archive_session_slice(phone, session_code, messages, db_client)

The `conversations` table (see CREATE TABLE in the migration block of this
module) is the canonical persisted conversation. Each row is one user: the
`messages` jsonb column holds the full Anthropic-shaped message list.

`db_client` here is the parts-bot Supabase Python module (search.db_client).
We go through its `_req` helper to keep the REST surface consistent.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("parts-bot.agent.history")

_TABLE = "conversations"


# ── public helpers ──────────────────────────────────────────────────────────

def load_history(phone: str, db_client) -> tuple[list[dict], str | None]:
    """Load {messages, active_session_code} for `phone`. Never raises.

    Returns (messages, active_session_code). On any error / missing row
    returns ([], None).
    """
    if not phone:
        return [], None
    try:
        rows = db_client._req(  # noqa: SLF001 — intentional REST wrapper
            "GET",
            _TABLE,
            params={
                "user_id": f"eq.{phone}",
                "select": "messages,active_session_code",
                "limit": "1",
            },
        )
        if rows and isinstance(rows, list) and rows:
            row = rows[0]
            return list(row.get("messages") or []), row.get("active_session_code")
    except Exception as e:
        logger.warning(f"load_history error for {phone}: {e}")
    return [], None


def save_history(
    phone: str,
    messages: list[dict],
    active_session_code: str | None,
    db_client,
) -> None:
    """Upsert one `conversations` row for `phone`. Fire-and-forget."""
    if not phone:
        return
    try:
        import urllib.request
        import urllib.parse

        url_base, _ = db_client._env()  # noqa: SLF001
        if not url_base:
            return
        url = f"{url_base}/rest/v1/{_TABLE}?on_conflict=user_id"
        headers = db_client._headers(prefer_return=False)  # noqa: SLF001
        headers["Prefer"] = "resolution=merge-duplicates"
        payload = {
            "user_id": phone,
            "messages": messages,
            "active_session_code": active_session_code,
            "last_activity_at": datetime.now(timezone.utc).isoformat(),
            "total_tokens_estimate": _estimate(messages),
            "turn_count": _count_user_turns(messages),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        data = json.dumps(payload, default=str).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 — internal
            resp.read()
    except Exception as e:
        logger.warning(f"save_history error for {phone}: {e}")


def append_message(
    messages: list[dict],
    role: str,
    content: Any,
    attachments: list[dict] | None = None,
) -> list[dict]:
    """Return a NEW list with one appended message. Does not mutate input.

    `content` can be a string or an Anthropic-style list of content blocks.
    `attachments` is metadata (not API content) — we store it inline on the
    message under a private key that Anthropic ignores, but the agent can
    inspect to know what files came through WhatsApp.
    """
    msg: dict[str, Any] = {"role": role, "content": content}
    if attachments:
        msg["_attachments"] = attachments
    return [*messages, msg]


def archive_session_slice(
    phone: str,
    session_code: str,
    messages: list[dict],
    db_client,
) -> None:
    """Persist the conversation slice for a given session into parts_sessions.history.

    Called when the agent closes a session — we freeze what the agent said/did
    for that session so a later resume has full context even if the rolling
    `conversations.messages` has been compacted since.
    """
    if not (phone and session_code and messages):
        return
    try:
        slice_ = _slice_by_session(messages, session_code)
        db_client._req(  # noqa: SLF001
            "PATCH",
            f"parts_sessions?code=eq.{session_code}",
            body={"history": slice_, "last_activity_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        logger.warning(f"archive_session_slice error: {e}")


# ── internals ───────────────────────────────────────────────────────────────

def _estimate(messages: list[dict]) -> int:
    # Cheap heuristic — same constant as token_strategy so the two agree.
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c) // 4
        else:
            total += len(json.dumps(c, default=str)) // 4
    return total


def _count_user_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "user")


def _slice_by_session(messages: list[dict], session_code: str) -> list[dict]:
    """Return the tail of `messages` from when this session became active.

    We detect session boundaries by scanning backwards for any message or
    tool_result whose serialized body mentions the session code. Everything
    from that point to the end is "this session's slice". If we can't find
    one, return the last 20 messages as a conservative fallback.
    """
    blob_by_index: list[str] = []
    for m in messages:
        blob_by_index.append(json.dumps(m.get("content", ""), default=str))
    start = 0
    for i in range(len(blob_by_index) - 1, -1, -1):
        if session_code in blob_by_index[i]:
            start = i
            break
    if start == 0 and session_code not in (blob_by_index[0] if blob_by_index else ""):
        # Didn't find the code — fall back to a tail slice
        start = max(0, len(messages) - 20)
    return messages[start:]
