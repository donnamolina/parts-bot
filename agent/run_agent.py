#!/usr/bin/env python3
"""
CLI entrypoint for the v11 agent loop.

server.js pipes a JSON payload on stdin. We read it, call `run_turn`, and print
a single JSON object on stdout.

Input shape:
    {"user_id": "18091234567",
     "message": "hola",
     "attachments": [{"path": "/abs/...", "type": "image/jpeg", "mime": "image/jpeg"}]}

Output shape:
    {"text": "...", "files": [{"path": "...", "name": "..."}]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_env():
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def _setup_logging():
    log_dir = _ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "agent.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stderr)],
    )


async def _main():
    _load_env()
    _setup_logging()

    from agent.loop import run_turn

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({
            "text": "Disculpa, hubo un error procesando tu mensaje. Intenta de nuevo.",
            "files": [],
            "_error": f"stdin json: {e}",
        }))
        return

    user_id = payload.get("user_id") or ""
    message = payload.get("message") or ""
    attachments = payload.get("attachments") or []
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    result = await run_turn(
        user_id=user_id,
        user_message=message,
        attachments=attachments,
        api_key=api_key,
    )

    json.dump(result, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    asyncio.run(_main())
