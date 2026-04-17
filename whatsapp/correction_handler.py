"""
Bug 6: Sonnet-powered correction handler.

Wiring:
  - Called when the WhatsApp bot is in an "awaiting correction" state and the
    user's message isn't a simple confirm.
  - Called as a Python subprocess from server.js with a JSON payload on stdin;
    returns a JSON envelope on stdout describing the action to take.

Design rule: NO if/elif chains to pre-classify intent in Python. ALL intent
classification happens inside Sonnet. Python only marshals context to Sonnet
and dispatches the returned action.

Supported actions returned by Sonnet (see ACTION_ENUM):
  update_quantity, rename_part, add_part, remove_part,
  re_extract_metadata, re_extract_parts, fix_translation,
  confirm_all, ask_clarification, out_of_scope
"""

import json
import os
import sys
import logging
from pathlib import Path

import anthropic

logger = logging.getLogger("parts-bot.correction")

ACTION_ENUM = (
    "update_quantity",
    "rename_part",
    "add_part",
    "remove_part",
    "re_extract_metadata",
    "re_extract_parts",
    "fix_translation",
    "confirm_all",
    "ask_clarification",
    "out_of_scope",
)

_FAILSAFE_ES = (
    "Disculpa, no pude procesar tu corrección. ¿Puedes reformularla? "
    "Ej: 'el #3 es 2 unidades' o 'el vehículo es Toyota Corolla 2019'."
)


_SYSTEM_PROMPT = """Eres el módulo de corrección del bot Pieza Finder (República Dominicana).
Clasificas mensajes de WhatsApp del usuario dentro de un flujo de confirmación de piezas.

TU TRABAJO: devolver UN SOLO JSON con la acción exacta. Sin explicaciones fuera del JSON, sin markdown.

ACCIONES POSIBLES (action):
- update_quantity: cambiar la cantidad de una pieza. params: {"index": <1-based>, "quantity": <int>}
- rename_part: cambiar el nombre de la pieza (no es traducción, es que era otra pieza). params: {"index": <1-based>, "name_dr": "<nombre en español DR>"}
- add_part: añadir una pieza nueva. params: {"name_dr": "<nombre>", "quantity": <int>, "side": "left|right|null", "position": "front|rear|null"}
- remove_part: quitar una pieza. params: {"index": <1-based>}
- re_extract_metadata: el usuario corrigió el vehículo o VIN. params: {"vehicle": {"year": int|null, "make": str|null, "model": str|null}, "vin": "17 chars|null"}
- re_extract_parts: el usuario pide re-procesar el documento original (lista entera estaba mal). params: {}
- fix_translation: la traducción al inglés era incorrecta. params: {"index": <1-based>, "name_english": "<inglés correcto>"}
- confirm_all: el usuario dice que todo está bien (ok, listo, dale, perfecto, gracias, done). params: {}
- ask_clarification: el mensaje es ambiguo o falta contexto (p.ej. "pantalla" sin delantera/trasera, "módulo" sin especificar, número sin acción clara). params: {"question_es": "<pregunta corta al usuario en español DR>"}
- out_of_scope: el mensaje no es una corrección (saludo suelto, pregunta fuera de tema, charla). params: {}

VOCABULARIO DOMINICANO IMPORTANTE:
- "pantalla" sola → AMBIGUA: pide clarificar si es delantera (headlight) o trasera (tail light).
- "módulo" solo → AMBIGUO: pide clarificar cuál módulo (motor/ECU, transmisión/TCM, BCM, airbag/SRS, etc.)
- "el de alante" / "el del frente" / "delantero" → front
- "el de atrás" / "trasero" → rear
- "el del chofer" / "lado del conductor" / "izquierdo" / "izq" → left
- "el del pasajero" / "derecho" / "der" → right
- "#3 es 2 unidades", "el tres son dos" → update_quantity index=3 quantity=2
- "eso no es un farol, es un bumper" → rename_part
- "añade un capot", "falta el capó" → add_part
- "quita el número 5" → remove_part
- "el vehículo es Toyota Camry 2019" o un VIN de 17 caracteres → re_extract_metadata
- "revisa el documento de nuevo", "todo está mal" → re_extract_parts
- "no es headlight, es fog light" → fix_translation

REGLAS:
- Si el mensaje es una confirmación simple ("ok", "listo", "dale", "perfecto", "done", "gracias") → action=confirm_all.
- Si falta información para ejecutar la acción con seguridad → action=ask_clarification, NO adivines.
- El VIN válido tiene 17 caracteres alfanuméricos, sin I/O/Q.
- Explicación en español DR corta, una oración, lo que vas a hacer.

FORMATO DE RESPUESTA (SOLO JSON, un solo objeto, nada de prosa):
El campo "action" DEBE ser EXACTAMENTE una de estas cadenas literales:
"update_quantity", "rename_part", "add_part", "remove_part", "re_extract_metadata", "re_extract_parts", "fix_translation", "confirm_all", "ask_clarification", "out_of_scope"

NO inventes nuevos nombres de acción. Si no encaja en ninguna, usa "ask_clarification".

{"action": "<una de las exactas>", "params": { ... }, "explanation_es": "<explicación corta>"}"""


def _build_user_prompt(parts: list, vehicle: dict, history: list, user_message: str) -> str:
    parts_lines = []
    for i, p in enumerate(parts, start=1):
        side = p.get("side") or "null"
        pos = p.get("position") or "null"
        qty = p.get("quantity") or 1
        parts_lines.append(
            f"{i}. {p.get('name_original') or p.get('name_dr') or ''} "
            f"→ {p.get('name_english') or ''} "
            f"(qty={qty}, side={side}, pos={pos})"
        )
    parts_str = "\n".join(parts_lines) if parts_lines else "(ninguna pieza)"

    vehicle_str = (
        f"{vehicle.get('year', '?')} {vehicle.get('make', '?')} {vehicle.get('model', '?')} "
        f"VIN={vehicle.get('vin', '?')}"
    )

    history_str = ""
    if history:
        recent = history[-5:] if len(history) > 5 else history
        history_str = "\n".join(f"{h.get('role', '?')}: {h.get('content', '')}" for h in recent)

    return (
        f"VEHÍCULO ACTUAL:\n{vehicle_str}\n\n"
        f"PIEZAS ACTUALES (1-based):\n{parts_str}\n\n"
        f"CONVERSACIÓN RECIENTE:\n{history_str or '(sin historial)'}\n\n"
        f"MENSAJE NUEVO DEL USUARIO:\n\"{user_message}\""
    )


def handle_correction(
    parts: list,
    vehicle: dict,
    history: list,
    user_message: str,
    api_key: str | None = None,
) -> dict:
    """Call Sonnet and return the parsed envelope: {action, params, explanation_es}.

    On any error, returns a fail-safe ask_clarification envelope so server.js
    can show the canonical Spanish fallback message.
    """
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _failsafe_envelope("ANTHROPIC_API_KEY missing")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        user_prompt = _build_user_prompt(parts, vehicle, history, user_message)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip potential markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        # Extract FIRST JSON object using a brace-depth scanner — Sonnet can emit
        # multiple objects back-to-back; we only want the first.
        env = _parse_first_json_object(raw)
        if env is None:
            raise json.JSONDecodeError("no valid JSON object in response", raw, 0)
        action = env.get("action")
        if action not in ACTION_ENUM:
            logger.warning(f"Sonnet returned unknown action '{action}' — failsafe")
            return _failsafe_envelope(f"unknown action {action}")
        env.setdefault("params", {})
        env.setdefault("explanation_es", "")
        return env
    except json.JSONDecodeError as e:
        logger.error(f"Sonnet returned non-JSON: {e}")
        return _failsafe_envelope(f"json parse: {e}")
    except Exception as e:
        logger.error(f"Correction handler error: {e}")
        return _failsafe_envelope(str(e))


def _parse_first_json_object(text: str) -> dict | None:
    """Extract and parse the first top-level JSON object from text.
    Handles multiple objects back-to-back and prose leakage around the JSON."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def _failsafe_envelope(reason: str) -> dict:
    return {
        "action": "ask_clarification",
        "params": {"question_es": _FAILSAFE_ES},
        "explanation_es": _FAILSAFE_ES,
        "_failsafe_reason": reason,
    }


# ── CLI entry point for server.js subprocess call ─────────────────────────────
# Expected stdin: JSON object with keys: parts, vehicle, history, message
# stdout: JSON envelope from handle_correction()
def _cli_main():
    # Load .env for ANTHROPIC_API_KEY if not already in env
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps(_failsafe_envelope(f"stdin json: {e}")))
        return

    env = handle_correction(
        parts=payload.get("parts", []),
        vehicle=payload.get("vehicle", {}),
        history=payload.get("history", []),
        user_message=payload.get("message", ""),
    )
    print(json.dumps(env, ensure_ascii=False))


if __name__ == "__main__":
    _cli_main()
