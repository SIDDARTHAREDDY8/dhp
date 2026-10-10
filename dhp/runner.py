"""DHP v0.1 — LocalRunner: durable execution over a directory.

Layout per handoff <root>/handoffs/<hid>/:
    envelope.json          # the envelope, mutated in place through its lifecycle
    checkpoints/<seq>.json # content-addressed step states
    result.json            # on completion: {"result": ..., "completed_at": ...}

Cross-runner portability: suspend() returns a self-sufficient bundle
(envelope + checkpoint blobs); resume(bundle) on ANY runner root
reconstitutes it. Same-root pickup uses recover().
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any, Callable, Dict, Optional

from . import envelope as E


class BudgetExceeded(Exception):
    pass


class LeaseLost(Exception):
    """Raised when a worker acts without holding a valid lease.

    Fencing: after lease expiry + reassignment, the old worker's
    checkpoint/complete calls are rejected — it learns it is no longer
    the owner. This is what makes idle-but-not-dead safe.
    """


class RunnerError(Exception):
    pass


def _now() -> float:
    return time.time()


class TaskContext:
    """Handle a claimed handoff. The worker's only interface to the runtime."""

    def __init__(self, runner: "LocalRunner", hid: str):
        self._runner = runner
        self.hid = hid

    def _load(self) -> Dict[str, Any]:
        return self._runner._load(self.hid)

    def checkpoint(self, state: Any) -> int:
        """Durably record step state. MUST be called before the next
        billable step — a crash loses only what was not checkpointed."""
        return self._runner.checkpoint(self.hid, state)

    def heartbeat(self) -> Dict[str, Any]:
        """Renew the lease. Call periodically — per chunk is plenty."""
        return self._runner.heartbeat(self.hid)

    def note_step(self, n: int = 1) -> None:
        self._runner._bump(self.hid, steps=n)

    def note_spend(self, usd: float) -> None:
        self._runner._bump(self.hid, spend=usd)

    def last_checkpoint(self) -> Optional[Any]:
        return self._runner.last_checkpoint(self.hid)


class LocalRunner:
    def __init__(self, root: str, runner_id: Optional[str] = None):
        self.root = os.path.abspath(root)
        self.runner_id = runner_id or f"runner-{uuid.uuid4().hex[:8]}"
        os.makedirs(os.path.join(self.root, "handoffs"), exist_ok=True)

    # ---- internal IO ----
    def _hdir(self, hid: str) -> str:
        return os.path.join(self.root, "handoffs", hid)

    def _epath(self, hid: str) -> str:
        return os.path.join(self._hdir(hid), "envelope.json")

    def _load(self, hid: str) -> Dict[str, Any]:
        p = self._epath(hid)
        if not os.path.exists(p):
            raise RunnerError(f"unknown handoff {hid[:12]}")
        with open(p, "rb") as f:
            return E.deserialize(f.read())

    def _save(self, env: Dict[str, Any]) -> None:
        hid = env["handoff_id"]
        os.makedirs(self._hdir(hid), exist_ok=True)
        with open(self._epath(hid), "wb") as f:
            f.write(E.serialize(env))

    def _check_holder(self, env: Dict[str, Any]) -> None:
        """Fencing: only the current lease holder may mutate a running handoff."""
        ex = env["execution"]
        if ex["status"] != "running":
            raise RunnerError(f"not running (status={ex['status']})")
        lease = ex.get("lease") or {}
        if lease.get("holder") != self.runner_id:
            raise LeaseLost(
                f"lease held by {lease.get('holder')!r}, not {self.runner_id!r}")
        if E.lease_expired(env):
            raise LeaseLost("lease expired — supervisor owns this handoff now")

    # ---- lifecycle ----
    def dispatch(self, env: Dict[str, Any]) -> str:
        """Accept a handoff for execution. Verifies id + input hash first —
        crossed inputs are rejected, never run."""
        if not E.verify_id(env):
            raise RunnerError("dispatch rejected: handoff_id mismatch (tampered?)")
        if not E.verify_inputs(env):
            raise RunnerError("dispatch rejected: inputs do not match input_hash")
        if env["execution"]["status"] != "pending":
            raise RunnerError("dispatch rejected: envelope not pending")
        env = json.loads(json.dumps(env))  # detach
        hid = env["handoff_id"]
        if os.path.exists(self._epath(hid)):
            return hid  # idempotent re-dispatch
        self._save(env)
        return hid

    def claim(self, hid: str) -> TaskContext:
        env = self._load(hid)
        if env["execution"]["status"] != "pending":
            raise RunnerError(f"claim rejected: status is {env['execution']['status']}")
        env["execution"]["status"] = "running"
        env["execution"]["runner_id"] = self.runner_id
        env["execution"]["claimed_at"] = _now()
        env["execution"].setdefault("steps", 0)
        env["execution"].setdefault("spend_usd", 0.0)
        E.set_lease(env, self.runner_id)
        self._save(env)
        self._check_budget(env)
        return TaskContext(self, hid)

    def heartbeat(self, hid: str) -> Dict[str, Any]:
        """Renew the lease. Workers call this periodically (e.g. per chunk).
        Raises LeaseLost if the worker is no longer the holder."""
        env = self._load(hid)
        self._check_holder(env)
        E.refresh_lease(env)
        self._save(env)
        return {"ok": True,
                "expires_in_s": round(env["execution"]["lease"]["expires_at"] - _now(), 1)}

    def release(self, hid: str, reason: str = "") -> None:
        """Cooperative fast path: a worker that knows it is stopping (or is
        done-early) releases the handoff immediately instead of letting the
        lease expire. The kill-vs-crash distinction: explicit release =
        instant requeue; silent death = lease-expiry path."""
        env = self._load(hid)
        self._check_holder(env)
        env["execution"]["status"] = "pending"
        env["execution"]["lease"] = None
        env["execution"]["release_reason"] = reason
        self._save(env)

    def checkpoint(self, hid: str, state: Any) -> int:
        env = self._load(hid)
        self._check_holder(env)
        seq = len(env["execution"]["checkpoints"])
        blob = E.canonical({"seq": seq, "state": state})
        cdir = os.path.join(self._hdir(hid), "checkpoints")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, f"{seq}.json"), "wb") as f:
            f.write(blob)
        env["execution"]["checkpoints"].append({
            "seq": seq,
            "at": E.utcnow(),
            "state_hash": E.sha256_hex(blob),
            "state_uri": f"checkpoints/{seq}.json",
        })
        self._save(env)
        self._check_budget(env)
        return seq

    def last_checkpoint(self, hid: str) -> Optional[Any]:
        env = self._load(hid)
        cps = env["execution"]["checkpoints"]
        if not cps:
            return None
        with open(os.path.join(self._hdir(hid), cps[-1]["state_uri"]), "rb") as f:
            return json.loads(f.read().decode("utf-8"))["state"]

    def suspend(self, hid: str) -> bytes:
        """Serialize a portable bundle: envelope + all checkpoint blobs.
        Any conformant runner can resume() it."""
        env = self._load(hid)
        if env["execution"]["status"] in ("completed", "failed"):
            raise RunnerError("suspend rejected: handoff is terminal")
        env["execution"]["status"] = "suspended"
        self._save(env)
        blobs = {}
        cdir = os.path.join(self._hdir(hid), "checkpoints")
        if os.path.isdir(cdir):
            for name in sorted(os.listdir(cdir)):
                with open(os.path.join(cdir, name), "rb") as f:
                    blobs[name] = f.read().decode("utf-8")
        bundle = {"envelope": env, "checkpoints": blobs, "dhp_bundle": "0.1"}
        return E.canonical(bundle)

    def resume(self, bundle: bytes) -> str:
        """Reconstitute a suspended bundle on this runner. Continues from the
        last checkpoint with attempt+1."""
        data = json.loads(bundle.decode("utf-8"))
        if data.get("dhp_bundle") != "0.1":
            raise RunnerError("resume rejected: unknown bundle version")
        env = data["envelope"]
        if not E.verify_id(env):
            raise RunnerError("resume rejected: bundle tampered")
        hid = env["handoff_id"]
        if os.path.exists(self._epath(hid)) and \
                self._load(hid)["execution"]["status"] in ("completed", "failed"):
            raise RunnerError("resume rejected: already terminal here")
        os.makedirs(os.path.join(self._hdir(hid), "checkpoints"), exist_ok=True)
        for name, blob in data["checkpoints"].items():
            with open(os.path.join(self._hdir(hid), "checkpoints", name), "w") as f:
                f.write(blob)
        env["execution"]["status"] = "running"
        env["execution"]["attempt"] += 1
        env["execution"]["runner_id"] = self.runner_id
        env["execution"]["claimed_at"] = _now()
        E.set_lease(env, self.runner_id)
        self._save(env)
        return hid

    def recover(self, hid: str) -> TaskContext:
        """Same-root pickup of a non-terminal handoff (e.g. previous worker
        died). Portable equivalent of suspend()+resume() without the bytes."""
        env = self._load(hid)
        if env["execution"]["status"] in ("completed", "failed"):
            raise RunnerError("recover rejected: handoff is terminal")
        if env["execution"]["status"] == "pending":
            raise RunnerError("recover rejected: handoff never claimed; use claim")
        env["execution"]["status"] = "running"
        env["execution"]["attempt"] += 1
        env["execution"]["runner_id"] = self.runner_id
        env["execution"]["claimed_at"] = _now()
        E.set_lease(env, self.runner_id)
        self._save(env)
        return TaskContext(self, hid)

    def complete(self, hid: str, result: Any) -> None:
        env = self._load(hid)
        if env["execution"]["status"] == "completed":
            return  # idempotent: same work completing twice is a no-op
        self._check_holder(env)
        E.validate_result(env, result)  # raises: invalid results rejected
        with open(os.path.join(self._hdir(hid), "result.json"), "w") as f:
            json.dump({"result": result, "completed_at": E.utcnow(),
                       "idempotency_key": env["delivery"]["idempotency_key"]}, f)
        env["execution"]["status"] = "completed"
        env["execution"]["lease"] = None
        env["delivery"]["outbox"] = {
            "uri": os.path.join(self._hdir(hid), "result.json"), "ack": False}
        self._save(env)

    def ack(self, hid: str) -> None:
        """Parent acknowledges receipt. Until ack, the result sits in the
        durable outbox — no lost results even if the parent dies first."""
        env = self._load(hid)
        if env["execution"]["status"] != "completed":
            raise RunnerError("ack rejected: handoff not completed")
        env["delivery"]["outbox"]["ack"] = True
        self._save(env)

    def result(self, hid: str) -> Any:
        env = self._load(hid)
        if env["execution"]["status"] != "completed":
            raise RunnerError("no result yet")
        with open(os.path.join(self._hdir(hid), "result.json")) as f:
            return json.load(f)["result"]

    def fail(self, hid: str, error: str, retryable: bool = False) -> None:
        env = self._load(hid)
        env["execution"]["status"] = "failed" if not retryable else "pending"
        env["execution"]["error"] = error
        self._save(env)

    # ---- budget ----
    def _bump(self, hid: str, steps: int = 0, spend: float = 0.0) -> None:
        env = self._load(hid)
        self._check_holder(env)
        env["execution"]["steps"] = env["execution"].get("steps", 0) + steps
        env["execution"]["spend_usd"] = env["execution"].get("spend_usd", 0.0) + spend
        self._save(env)
        self._check_budget(env)

    def _check_budget(self, env: Dict[str, Any]) -> None:
        b = env["contract"]["budget"]
        ex = env["execution"]
        if ex.get("steps", 0) > b["max_steps"]:
            raise BudgetExceeded(f"max_steps {b['max_steps']} exceeded")
        if ex.get("spend_usd", 0.0) > b["max_spend_usd"]:
            raise BudgetExceeded(f"max_spend_usd {b['max_spend_usd']} exceeded")
        wall = _now() - ex.get("claimed_at", _now())
        if wall > b["max_wallclock_s"]:
            raise BudgetExceeded(f"max_wallclock_s {b['max_wallclock_s']} exceeded")
        dl = env["contract"].get("deadline")
        if dl and E.utcnow() > dl:
            raise BudgetExceeded("deadline passed")
