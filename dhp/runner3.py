"""DHP v1.0 — runner on the SQLite store (production-hardened).

Same wire protocol as v0.3/v0.4 (envelope JSON unchanged). Hardening:
- every public method validates hid/worker_id/payload sizes first
- typed errors (dhp.errors) instead of ad-hoc RunnerError strings
- lease intervals from DHPConfig (env-overridable), not module constants
- jsonschema required at import (fail fast, not at complete-time)

Ownership model unchanged: CAS rows, monotonic fencing tokens,
supervisor orphans via force-acquire to '__orphaned__'.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from . import envelope as E
from .config import DHPConfig, DEFAULT
from .errors import (
    DHPError, StateError, BudgetExceeded, FencingError, TamperError,
    UnknownHandoff, ValidationError, ApprovalRequired, ApprovalDenied,
    StallDetected,
)
from .store import Store, OwnershipLost, LeaseLost
from . import log as dhp_log
from . import validate as V

# backward-compat alias
RunnerError = StateError

ORPHANED_HOLDER = "__orphaned__"

_logger = dhp_log.get_logger()

try:
    import jsonschema  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "dhp requires the 'jsonschema' package for result validation: "
        "pip install jsonschema") from exc


def _now() -> float:
    return time.time()


class TaskContext:
    """What the worker holds. Caches (holder, fence); every mutation
    re-validates against the store.

    Use as a context manager for automatic heartbeats::

        with dhp.claim(store, hid, "w1") as ctx:
            while ...:
                ctx.checkpoint(state)   # heartbeats happen in background
            ctx.complete(result)
        # heartbeat thread stops on exit; complete/release stop it too
    """

    def __init__(self, runner: "Runner3", hid: str, fence: int):
        self._runner = runner
        self.hid = hid
        self.fence = fence
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None

    def __enter__(self) -> "TaskContext":
        self.start_heartbeat()
        return self

    def __exit__(self, *exc) -> None:
        self.stop_heartbeat()

    def start_heartbeat(self,
                        interval_s: Optional[float] = None) -> "TaskContext":
        """Start a background heartbeat thread. Idempotent."""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return self
        interval = interval_s or self._runner.cfg.lease_interval_s / 2
        self._hb_stop.clear()

        def _beat() -> None:
            while not self._hb_stop.wait(interval):
                try:
                    self.heartbeat()
                except Exception:
                    # lease lost / handoff done: stop quietly; the next
                    # explicit call will surface the real error
                    break

        self._hb_thread = threading.Thread(target=_beat, daemon=True,
                                           name=f"dhp-hb-{self.hid[:8]}")
        self._hb_thread.start()
        return self

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        t, self._hb_thread = self._hb_thread, None
        if t is not None:
            t.join(timeout=5.0)

    def checkpoint(self, state: Any) -> int:
        return self._runner.checkpoint(self.hid, state, self.fence)

    def heartbeat(self) -> Dict[str, Any]:
        return self._runner.heartbeat(self.hid, self.fence)

    def note_step(self, n: int = 1) -> None:
        self._runner._bump(self.hid, self.fence, steps=n)

    def note_spend(self, usd: float) -> None:
        """Record real spend (LLM API cost). Supervisor kills on
        budget exhaustion — fail closed, no silent overrun."""
        if usd < 0:
            raise ValidationError("spend cannot be negative")
        self._runner._bump(self.hid, self.fence, spend=usd)

    # ---- effect log: idempotency keyed by step_id ----
    def record_effect(self, step_id: str, result: Any) -> bool:
        """Record that a side effect happened. First write wins:
        returns True if this call recorded it, False if a previous
        attempt (possibly a dead worker) already did. Check
        effect_done() before re-executing a side effect."""
        self._runner._check_owner(self.hid, self.fence)
        return self._runner.store.record_effect(self.hid, step_id, result)

    def effect_done(self, step_id: str) -> Optional[Any]:
        """Return the recorded result if this step already executed,
        else None. The standby calls this on resume instead of
        blindly re-firing the side effect."""
        self._runner._check_owner(self.hid, self.fence)
        return self._runner.store.get_effect(self.hid, step_id)

    # ---- progress reporting (stall detection) ----
    def note_activity(self, digest: str) -> int:
        """Report an activity digest (e.g. sha256 of tool name + args).
        Returns the repeat count: a high count with no new checkpoint
        means livelock, and the supervisor will orphan as stalled."""
        self._runner._check_owner(self.hid, self.fence)
        return self._runner.store.note_progress(
            self.hid, digest)["repeat_count"]

    # ---- approval gates for irreversible actions ----
    def request_approval(self, action_class: str, details: Dict[str, Any],
                         timeout_s: float = 300.0) -> str:
        """Create a pending approval record and raise ApprovalRequired
        (fail-closed). An external approver (human/policy engine) decides
        via dhp.approve()/dhp.deny(); the worker then calls
        await_approval()."""
        self._runner._check_owner(self.hid, self.fence)
        if action_class in ("financial", "destructive"):
            aid = self._runner.store.request_approval(
                self.hid, action_class, details, timeout_s)
            raise ApprovalRequired(
                f"{action_class} action needs approval: {aid}")
        return ""

    def await_approval(self, approval_id: str,
                       poll_s: float = 1.0) -> bool:
        """Block until the approval is decided or expires. Returns True
        if approved; raises ApprovalDenied on deny/timeout (fail-closed)."""
        self._runner._check_owner(self.hid, self.fence)
        import time as _time
        while True:
            ap = self._runner.store.get_approval(approval_id)
            if ap is None:
                raise ValidationError("unknown approval_id")
            if ap["status"] == "approved":
                return True
            if ap["status"] in ("denied", "expired"):
                raise ApprovalDenied(
                    f"approval {approval_id} {ap['status']}")
            if _time.time() > ap["expires_at"]:
                self._runner.store.expire_approvals()
                raise ApprovalDenied(
                    f"approval {approval_id} timed out")
            _time.sleep(poll_s)

    # ---- capability attenuation ----
    def require_capability(self, domain: str, resource: str) -> None:
        """Fail-closed: raise CapabilityDenied unless the handoff's
        contract declares this (domain, resource). Call before touching
        network, filesystem, or external services — ambient credentials
        never enter the worker's scope undeclared."""
        self._runner._check_owner(self.hid, self.fence)
        if not self._runner.store.check_capability(
                self.hid, domain, resource):
            from .errors import CapabilityDenied
            raise CapabilityDenied(
                f"capability denied: {domain}:{resource} not in"
                f" handoff {self.hid[:12]} contract")

    def complete(self, result: Any) -> None:
        try:
            self._runner.complete(self.hid, result, self.fence)
        finally:
            self.stop_heartbeat()

    def release(self, reason: str = "") -> None:
        try:
            self._runner.release(self.hid, self.fence, reason)
        finally:
            self.stop_heartbeat()


class Runner3:
    def __init__(self, root: str, runner_id: Optional[str] = None,
                 store: Optional[Store] = None,
                 cfg: DHPConfig = DEFAULT):
        self.store = store or Store(root, cfg=cfg)
        self.cfg = cfg
        import uuid
        if runner_id is None:
            runner_id = f"r3-{uuid.uuid4().hex[:8]}"
        V.check_worker_id(runner_id)
        self.runner_id = runner_id

    # ---- internal ----
    def _load(self, hid: str) -> Dict[str, Any]:
        return self.store.get_envelope(V.check_hid(hid))

    def _save(self, env: Dict[str, Any]) -> None:
        self.store.save_envelope(env["handoff_id"], env)

    def _check_owner(self, hid: str, fence: int) -> Dict[str, Any]:
        V.check_hid(hid)
        if not isinstance(fence, int) or fence < 0:
            raise ValidationError("malformed fence token")
        own = self.store.get_ownership(hid)
        if not own or own["holder"] != self.runner_id or own["fence"] != fence:
            raise FencingError(
                f"stale ownership for {hid[:12]}: "
                f"holder={own['holder'] if own else None}, "
                f"fence={own['fence'] if own else None}, mine={fence}")
        if own["lease_expires_at"] < _now():
            raise LeaseLost("lease expired — supervisor owns this handoff now")
        env = self._load(hid)
        if env["execution"]["status"] != "running":
            raise StateError(
                f"not running (status={env['execution']['status']})")
        return env

    def _check_budget(self, env: Dict[str, Any]) -> None:
        ex, b = env["execution"], env["contract"]["budget"]
        if ex.get("steps", 0) > b["max_steps"]:
            raise BudgetExceeded(
                f"steps {ex['steps']} > {b['max_steps']}")
        if ex.get("spend_usd", 0.0) > b["max_spend_usd"]:
            raise BudgetExceeded(
                f"spend ${ex['spend_usd']:.2f} > ${b['max_spend_usd']:.2f}")
        if _now() - ex.get("claimed_at", _now()) > b["max_wallclock_s"]:
            raise BudgetExceeded("wallclock exceeded")

    # ---- lifecycle ----
    def dispatch(self, env: Dict[str, Any]) -> str:
        if not isinstance(env, dict):
            raise ValidationError("envelope must be a dict")
        if not E.verify_id(env):
            raise TamperError(
                "dispatch rejected: handoff_id mismatch (tampered?)")
        if not E.verify_inputs(env):
            raise TamperError(
                "dispatch rejected: inputs do not match input_hash")
        if env["execution"]["status"] != "pending":
            raise StateError("dispatch rejected: envelope not pending")
        hid = env["handoff_id"]
        V.check_hid(hid)
        self.store.create_handoff(hid, env)
        _logger.info("dispatch %s kind=%s", hid[:12],
                     env["task"]["kind"])
        return hid

    def claim(self, hid: str) -> TaskContext:
        V.check_hid(hid)
        env = self._load(hid)
        if env["execution"]["status"] != "pending":
            raise StateError(
                f"claim rejected: status is {env['execution']['status']}")
        fence = self.store.acquire_ownership(
            hid, self.runner_id,
            self.cfg.lease_interval_s, self.cfg.lease_max_missed,
            expected_fence=None)
        env["execution"]["status"] = "running"
        env["execution"]["runner_id"] = self.runner_id
        env["execution"]["claimed_at"] = _now()
        env["execution"]["fence"] = fence
        env["execution"].setdefault("steps", 0)
        env["execution"].setdefault("spend_usd", 0.0)
        self._save(env)
        self._check_budget(env)
        _logger.info("claim %s by %s fence=%d", hid[:12], self.runner_id,
                     fence)
        return TaskContext(self, hid, fence)

    def recover(self, hid: str) -> TaskContext:
        V.check_hid(hid)
        env = self._load(hid)
        if env["execution"]["status"] in ("completed", "failed"):
            raise StateError("recover rejected: handoff is terminal")
        if env["execution"]["status"] == "pending":
            raise StateError("recover rejected: handoff never claimed;"
                             " use claim")
        own = self.store.get_ownership(hid)
        if not own or own["holder"] != ORPHANED_HOLDER:
            raise StateError(
                "recover rejected: not orphaned "
                f"(holder={own['holder'] if own else None})")
        # CAS: exactly one simultaneous recoverer wins.
        fence = self.store.acquire_ownership(
            hid, self.runner_id,
            self.cfg.lease_interval_s, self.cfg.lease_max_missed,
            expected_fence=own["fence"])
        env["execution"]["status"] = "running"
        env["execution"]["attempt"] += 1
        env["execution"]["runner_id"] = self.runner_id
        env["execution"]["claimed_at"] = _now()
        env["execution"]["fence"] = fence
        self._save(env)
        _logger.info("recover %s by %s fence=%d attempt=%d", hid[:12],
                     self.runner_id, fence, env["execution"]["attempt"])
        return TaskContext(self, hid, fence)

    def heartbeat(self, hid: str, fence: int) -> Dict[str, Any]:
        self._check_owner(hid, fence)
        expires = self.store.refresh_lease(hid, self.runner_id, fence)
        return {"ok": True, "expires_in_s": round(expires - _now(), 1)}

    def checkpoint(self, hid: str, state: Any, fence: int) -> int:
        self._check_owner(hid, fence)
        V.check_checkpoint_state(state, self.cfg)
        seq = self.store.add_checkpoint(hid, state)
        # progress counts: a checkpoint resets the stall clock
        self.store.note_checkpoint_progress(hid)
        return seq

    def _bump(self, hid: str, fence: int, steps: int = 0,
              spend: float = 0.0) -> None:
        env = self._check_owner(hid, fence)
        env["execution"]["steps"] = env["execution"].get("steps", 0) + steps
        env["execution"]["spend_usd"] = \
            env["execution"].get("spend_usd", 0.0) + spend
        self._save(env)
        self._check_budget(env)

    def release(self, hid: str, fence: int, reason: str = "") -> None:
        env = self._check_owner(hid, fence)
        env["execution"]["status"] = "pending"
        env["execution"]["release_reason"] = reason
        self._save(env)
        self.store.release_ownership(hid, self.runner_id, fence)
        _logger.info("release %s by %s", hid[:12], self.runner_id)

    def complete(self, hid: str, result: Any, fence: int) -> None:
        V.check_hid(hid)
        env = self._load(hid)
        if env["execution"]["status"] == "completed":
            return  # idempotent
        self._check_owner(hid, fence)
        V.check_result(result, self.cfg)
        try:
            E.validate_result(env, result)
        except E.EnvelopeError as e:
            # Bad output is a failure type, not a retry candidate:
            # DLQ with the typed error instead of leaving it running.
            env["execution"]["status"] = "failed"
            env["execution"]["error"] = f"schema validation: {e}"
            env["execution"]["failed_at"] = E.utcnow()
            self._save(env)
            self.store.release_ownership(hid, self.runner_id, fence)
            _logger.warning("complete %s: schema failure → DLQ: %s",
                            hid[:12], e)
            raise ValidationError(str(e)) from e
        env["execution"]["status"] = "completed"
        env["execution"]["completed_at"] = E.utcnow()
        env["delivery"]["outbox"] = {"ack": False}
        env["delivery"]["result"] = result
        self._save(env)
        self.store.release_ownership(hid, self.runner_id, fence)
        _logger.info("complete %s by %s", hid[:12], self.runner_id)

    def last_checkpoint(self, hid: str) -> Optional[Any]:
        return self.store.last_checkpoint_state(V.check_hid(hid))

    def ack(self, hid: str) -> None:
        V.check_hid(hid)
        env = self._load(hid)
        if env["execution"]["status"] != "completed":
            raise StateError("ack rejected: not completed")
        env["delivery"]["outbox"]["ack"] = True
        self._save(env)

    # ---- suspend / resume (portable bundle) ----
    def suspend(self, hid: str) -> bytes:
        V.check_hid(hid)
        env = self._load(hid)
        if env["execution"]["status"] not in ("running", "suspended"):
            raise StateError("suspend rejected: not active")
        env["execution"]["status"] = "suspended"
        self._save(env)
        bundle = {"dhp_bundle": "1.0", "envelope": env,
                  "checkpoints": self.store.get_checkpoints(hid)}
        blob = E.canonical(bundle)
        if len(blob) > self.cfg.max_envelope_bytes:
            from .errors import OversizePayload
            raise OversizePayload("bundle exceeds max_envelope_bytes")
        return blob

    def resume(self, bundle: bytes) -> str:
        """Import a suspended bundle onto this root. The handoff arrives
        as pending (fresh); the worker claims it. Checkpoints included."""
        if not isinstance(bundle, (bytes, bytearray)):
            raise ValidationError("bundle must be bytes")
        if len(bundle) > self.cfg.max_envelope_bytes:
            from .errors import OversizePayload
            raise OversizePayload("bundle exceeds max_envelope_bytes")
        try:
            data = json.loads(bytes(bundle).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValidationError(f"bundle is not valid JSON: {e}")
        if data.get("dhp_bundle") not in ("0.3", "1.0"):
            raise ValidationError("resume rejected: unknown bundle version")
        env = data["envelope"]
        if not E.verify_id(env):
            raise TamperError("resume rejected: bundle tampered")
        hid = V.check_hid(env["handoff_id"])
        try:
            self._load(hid)
            raise StateError("resume rejected: handoff already exists here")
        except UnknownHandoff:
            pass
        # verify checkpoint hashes before importing
        for s in data.get("checkpoints", []):
            blob = json.dumps(s["state"], sort_keys=True)
            if E.sha256_hex(blob.encode()) != s.get("state_hash", "") and \
                    "state_hash" in s:
                raise TamperError(
                    f"resume rejected: checkpoint {s.get('seq')} hash mismatch")
        env["execution"]["status"] = "pending"
        env["execution"]["attempt"] += 1
        self.store.create_handoff(hid, env)
        n = self.store.import_checkpoints(hid, data["checkpoints"])
        _logger.info("resume %s (%d checkpoints imported)", hid[:12], n)
        return hid
