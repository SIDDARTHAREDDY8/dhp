"""DHP input validation. Every entry point validates before touching
the store. Fail fast, fail typed."""
from __future__ import annotations

import json
import re
from typing import Any, Dict

from .config import DHPConfig, DEFAULT
from .errors import ValidationError, OversizePayload

_HID_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ID_LEN = 128


def check_hid(hid: Any) -> str:
    """Strict handoff-ID validation. Returns the hid, else raises."""
    if not isinstance(hid, str) or not _HID_RE.match(hid):
        raise ValidationError(
            f"malformed handoff_id (want 64 lowercase hex chars)")
    return hid


def check_worker_id(worker_id: Any) -> str:
    if not isinstance(worker_id, str) or not worker_id or \
            len(worker_id) > _MAX_ID_LEN:
        raise ValidationError("malformed worker_id")
    if not re.match(r"^[A-Za-z0-9_.\-]+$", worker_id):
        raise ValidationError("worker_id has illegal characters")
    return worker_id


def _json_size(obj: Any, cfg: DHPConfig) -> int:
    try:
        blob = json.dumps(obj, sort_keys=True)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"payload is not JSON-serializable: {e}")
    return len(blob.encode("utf-8"))


def check_checkpoint_state(state: Any,
                           cfg: DHPConfig = DEFAULT) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise ValidationError("checkpoint state must be a JSON object")
    size = _json_size(state, cfg)
    if size > cfg.max_checkpoint_bytes:
        raise OversizePayload(
            f"checkpoint {size} bytes > limit {cfg.max_checkpoint_bytes}")
    return state


def check_result(result: Any, cfg: DHPConfig = DEFAULT) -> None:
    size = _json_size(result, cfg)
    if size > cfg.max_result_bytes:
        raise OversizePayload(
            f"result {size} bytes > limit {cfg.max_result_bytes}")


def check_inputs(inputs: Any, cfg: DHPConfig = DEFAULT) -> Dict[str, Any]:
    if not isinstance(inputs, dict):
        raise ValidationError("inputs must be a JSON object")
    size = _json_size(inputs, cfg)
    if size > cfg.max_envelope_bytes:
        raise OversizePayload(
            f"inputs {size} bytes > limit {cfg.max_envelope_bytes}")
    return inputs
