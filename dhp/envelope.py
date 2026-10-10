"""DHP v0.1 — the Handoff envelope.

A handoff is one unit of delegated agent work: task + contract +
provenance + execution state, in a single content-addressed JSON document.

Identity covers *intent*, not transient state:
handoff_id = sha256(canonical(spec)) where spec = version, task, contract,
provenance, and the idempotency key. The execution section mutates freely
(status, checkpoints, attempt); each checkpoint blob is individually
content-addressed (state_hash), so history stays tamper-evident per step.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

VERSION = "dhp/0.2"

STATUSES = ("pending", "running", "suspended", "completed", "failed",
            "orphaned")

# v0.2 lease defaults (survey-informed): detection ≈ interval × max_missed.
# Default 2s × 3 = ~6s to detect; aggressive 0.5s × 2 = ~1s with documented
# false-failover risk (duplicate LLM runs on GC pauses / network blips).
LEASE_INTERVAL_S = 2.0
LEASE_MAX_MISSED = 3


def canonical(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnvelopeError(ValueError):
    pass


def build(task_kind: str,
          inputs: Dict[str, Any],
          output_schema: Dict[str, Any],
          budget: Optional[Dict[str, Any]] = None,
          deadline: Optional[str] = None,
          acceptance: Optional[list] = None,
          parent_handoff_id: Optional[str] = None,
          trace_id: Optional[str] = None,
          created_by: str = "dhp/0.1",
          idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Construct a new pending handoff envelope (dict form)."""
    env: Dict[str, Any] = {
        "version": VERSION,
        "task": {
            "kind": task_kind,
            "inputs": inputs,
            "input_hash": sha256_hex(canonical(inputs)),
        },
        "contract": {
            "output_schema": output_schema,
            "acceptance": acceptance or [],
            "deadline": deadline,
            "budget": budget or {"max_steps": 100,
                                 "max_spend_usd": 10.0,
                                 "max_wallclock_s": 3600.0,
                                 "max_attempts": 3},
        },
        "provenance": {
            "parent_handoff_id": parent_handoff_id,
            "trace_id": trace_id or sha256_hex(canonical(
                [task_kind, inputs, utcnow()]))[:16],
            "created_by": created_by,
            "created_at": utcnow(),
        },
        "execution": {
            "status": "pending",
            "attempt": 0,
            "checkpoints": [],
            "runner_id": None,
            "lease": None,  # v0.2: set on claim; see set_lease()
        },
        "delivery": {
            "idempotency_key": idempotency_key or sha256_hex(
                canonical([task_kind, inputs]))[:16],
            "outbox": {"uri": "", "ack": False},
        },
    }
    env["handoff_id"] = handoff_id(env)
    return env


def spec_of(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """The immutable work description covered by the handoff_id."""
    return {
        "version": envelope["version"],
        "task": envelope["task"],
        "contract": envelope["contract"],
        "provenance": envelope["provenance"],
        "idempotency_key": envelope["delivery"]["idempotency_key"],
    }


def handoff_id(envelope: Dict[str, Any]) -> str:
    """Content-addressed id over the immutable spec (intent, not state)."""
    return sha256_hex(canonical(spec_of(envelope)))


def verify_id(envelope: Dict[str, Any]) -> bool:
    return envelope.get("handoff_id") == handoff_id(envelope)


def verify_inputs(envelope: Dict[str, Any]) -> bool:
    """The dispatch-time check: inputs must match their bound hash.

    This is the structural fix for the crossed-wire class — a runner that
    receives inputs that don't match input_hash MUST NOT execute.
    """
    task = envelope.get("task", {})
    return task.get("input_hash") == sha256_hex(canonical(task.get("inputs", {})))


def serialize(envelope: Dict[str, Any]) -> bytes:
    if not verify_id(envelope):
        raise EnvelopeError("handoff_id does not match envelope body")
    return canonical(envelope)


def deserialize(data: bytes) -> Dict[str, Any]:
    envelope = json.loads(data.decode("utf-8"))
    if not verify_id(envelope):
        raise EnvelopeError("handoff_id does not match envelope body "
                            "(tampered or corrupt)")
    return envelope


def validate_result(envelope: Dict[str, Any], result: Any) -> None:
    """Completion-time check: result MUST satisfy the contract's output schema.

    Raises EnvelopeError on violation — invalid results are rejected, never
    silently accepted.
    """
    schema = envelope["contract"]["output_schema"]
    try:
        import jsonschema
    except ImportError as exc:
        raise EnvelopeError("jsonschema package required for validation") from exc
    try:
        jsonschema.validate(instance=result, schema=schema)
    except Exception as exc:
        raise EnvelopeError(f"result violates output_schema: {exc}") from exc


def summarize(envelope: Dict[str, Any]) -> Dict[str, Any]:
    ex = envelope["execution"]
    lease = ex.get("lease") or {}
    return {
        "handoff_id": envelope["handoff_id"][:12],
        "kind": envelope["task"]["kind"],
        "status": ex["status"],
        "attempt": ex["attempt"],
        "checkpoints": len(ex["checkpoints"]),
        "acked": envelope["delivery"]["outbox"]["ack"],
        "trace_id": envelope["provenance"]["trace_id"],
        "lease_holder": lease.get("holder"),
        "lease_expires_in_s": round(lease.get("expires_at", 0) - time.time(), 1)
        if lease else None,
    }


# ---- v0.2 leases ----
# A lease binds a running handoff to exactly one worker. Silence (death,
# stall, or idle — indistinguishable and treated identically) lets the
# lease expire; the supervisor then reassigns. This is the industry-standard
# answer (Temporal heartbeats, SQS visibility timeouts, K8s liveness).

def set_lease(env: Dict[str, Any], holder: str,
              interval_s: float = LEASE_INTERVAL_S,
              max_missed: int = LEASE_MAX_MISSED) -> None:
    now = time.time()
    env["execution"]["lease"] = {
        "holder": holder,
        "heartbeat_interval_s": interval_s,
        "max_missed": max_missed,
        "last_heartbeat": now,
        "expires_at": now + interval_s * max_missed,
    }


def refresh_lease(env: Dict[str, Any]) -> None:
    lease = env["execution"].get("lease")
    if not lease:
        raise EnvelopeError("no lease to refresh")
    now = time.time()
    lease["last_heartbeat"] = now
    lease["expires_at"] = now + lease["heartbeat_interval_s"] * lease["max_missed"]


def lease_expired(env: Dict[str, Any], now: Optional[float] = None) -> bool:
    lease = env["execution"].get("lease")
    if not lease:
        return False
    return (now if now is not None else time.time()) > lease["expires_at"]


if __name__ == "__main__":
    env = build(
        task_kind="math.solve",
        inputs={"inline": {"problem": "2+2", "want": "boxed"}},
        output_schema={"type": "object",
                       "properties": {"answer": {"type": "string"}},
                       "required": ["answer"]},
        budget={"max_steps": 10, "max_spend_usd": 1.0,
                "max_wallclock_s": 300.0},
    )
    assert verify_id(env) and verify_inputs(env)
    # tamper detection
    bad = copy.deepcopy(env)
    bad["task"]["inputs"]["inline"]["problem"] = "2+3"
    assert not verify_inputs(bad)
    # crossed wire: envelope id no longer matches body
    assert not verify_id(bad)
    # result validation
    validate_result(env, {"answer": "4"})
    try:
        validate_result(env, {"wrong": 1})
        raise AssertionError("should have rejected")
    except EnvelopeError:
        pass
    print("envelope self-test OK:", summarize(env)["handoff_id"])
