"""
Brain–Body Protocol (BBP) — envelopes and schema validation.

The JSON Schemas in the repo-root protocol/ folder are the machine-readable spec
(docs/protocol/BBP_v0.1.md). Every message is validated in two steps:

  1. the whole message against protocol/envelope.schema.json
  2. its payload against protocol/session/<kind>.schema.json for session kinds
     (hello/welcome/ping/pong), otherwise protocol/topics/<topic>.schema.json

A topic with no schema file is unknown. No network code lives here, so a later
client (phone, web) can reuse it.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

PROTOCOL_VERSION = 1
SESSION_KINDS = frozenset({"hello", "welcome", "ping", "pong"})


class BBPError(Exception):
    """A protocol violation, carrying a spec error code (E_BAD_PAYLOAD, …)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def protocol_dir() -> Path:
    """Repo-root protocol/ folder (override with JARVIS_PROTOCOL_DIR)."""
    override = os.environ.get("JARVIS_PROTOCOL_DIR")
    if override:
        return Path(override)
    # core_v3/adapters/bbp_protocol.py → parents[3] is the repo root
    return Path(__file__).resolve().parents[3] / "protocol"


@cache
def _validator(rel_path: str) -> Draft202012Validator | None:
    path = protocol_dir() / rel_path
    if not path.is_file():
        return None
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _payload_schema(kind: str, topic: str) -> str:
    return f"session/{kind}.schema.json" if kind in SESSION_KINDS else f"topics/{topic}.schema.json"


def has_schema(kind: str, topic: str) -> bool:
    return _validator(_payload_schema(kind, topic)) is not None


def now_ts() -> str:
    """ISO 8601 with milliseconds and the local UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def envelope(kind: str, topic: str, payload: dict[str, Any], corr: str | None = None) -> dict[str, Any]:
    """Build a message. For session kinds pass topic == kind."""
    return {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "kind": kind,
        "topic": topic,
        "ts": now_ts(),
        "corr": corr,
        "payload": payload,
    }


def error_response(command_id: str, code: str, message: str) -> dict[str, Any]:
    """Reply to a command the Brain can't accept (spec: response, topic=error)."""
    return envelope("response", "error", {"ok": False, "error": {"code": code, "message": message}},
                    corr=command_id)


def _check(validator: Draft202012Validator, instance: Any, where: str) -> None:
    err = best_match(validator.iter_errors(instance))
    if err is not None:
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        raise BBPError("E_BAD_PAYLOAD", f"{where} {path}: {err.message}")


def validate(message: Any) -> None:
    """Validate a full message. Raises BBPError(E_BAD_PAYLOAD) if the envelope or
    payload is invalid, BBPError(E_UNKNOWN_TOPIC) if no schema exists for it."""
    env = _validator("envelope.schema.json")
    if env is None:
        raise RuntimeError(f"BBP schemas not found in {protocol_dir()}")
    _check(env, message, "envelope")
    payload_validator = _validator(_payload_schema(message["kind"], message["topic"]))
    if payload_validator is None:
        raise BBPError("E_UNKNOWN_TOPIC", f"no handler for topic {message['topic']!r}")
    _check(payload_validator, message["payload"], f"payload[{message['topic']}]")


def parse(raw: str | bytes) -> dict[str, Any]:
    """Decode a text frame and validate its envelope (payload is checked by the
    caller once it knows whether it handles the topic)."""
    if isinstance(raw, bytes):
        raise BBPError("E_BAD_PAYLOAD", "binary frames are reserved in v0.1")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BBPError("E_BAD_PAYLOAD", f"not JSON: {e}") from None
    env = _validator("envelope.schema.json")
    if env is None:
        raise RuntimeError(f"BBP schemas not found in {protocol_dir()}")
    _check(env, message, "envelope")
    return message


def validate_payload(message: dict[str, Any]) -> None:
    """Payload-only check for a message whose envelope already passed parse()."""
    payload_validator = _validator(_payload_schema(message["kind"], message["topic"]))
    if payload_validator is None:
        raise BBPError("E_UNKNOWN_TOPIC", f"no handler for topic {message['topic']!r}")
    _check(payload_validator, message["payload"], f"payload[{message['topic']}]")


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
