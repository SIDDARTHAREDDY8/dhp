"""DHP v1.0 — replicated supervisor with leader election
(production-hardened).

N supervisor processes share the Store. Leadership is a CAS row: only
the holder scans. Followers watch the leader's lease; when it expires,
the first to CAS-acquire wins and becomes leader.

Hardening vs v0.3:
- metrics counters (scans, orphans, DLQ, leadership changes, errors)
- config-driven intervals (env-overridable)
- structured logging with handoff correlation
- graceful shutdown: watch() installs SIGTERM/SIGINT handlers and
  finishes the in-flight scan before exiting
"""
from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .config import DHPConfig, DEFAULT
from .runner3 import Runner3, ORPHANED_HOLDER
from .store import Store
from . import log as dhp_log
from . import validate as V

_logger = dhp_log.get_logger()


class SupervisorMetrics:
    """Counters. Thread-safe; snapshot() for health checks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.scans = 0
        self.orphans = 0
        self.dlq = 0
        self.leadership_changes = 0
        self.errors = 0
        self.started_at = time.time()

    def bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "scans": self.scans,
                "orphans": self.orphans,
                "dlq": self.dlq,
                "leadership_changes": self.leadership_changes,
                "errors": self.errors,
                "uptime_s": round(time.time() - self.started_at, 1),
            }


class Supervisor3:
    def __init__(self, root: str,
                 supervisor_id: Optional[str] = None,
                 on_orphan: Optional[Callable[[str], None]] = None,
                 provision: Optional[Callable[[str], None]] = None,
                 cfg: DHPConfig = DEFAULT):
        self.cfg = cfg
        self.store = Store(root, cfg=cfg)
        import uuid
        if supervisor_id is None:
            supervisor_id = f"sup-{uuid.uuid4().hex[:8]}"
        V.check_worker_id(supervisor_id)
        self.sup_id = supervisor_id
        self.runner = Runner3(root, runner_id=self.sup_id,
                              store=self.store, cfg=cfg)
        self.on_orphan = on_orphan
        self.provision = provision
        self.metrics = SupervisorMetrics()
        self._is_leader = False
        self._stop = threading.Event()

    # ---- lifecycle ----
    def request_stop(self) -> None:
        """Ask watch() to finish the current scan and return."""
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            _logger.info("supervisor %s: signal %d, finishing scan",
                         self.sup_id, signum)
            self.request_stop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not in main thread; caller handles shutdown

    # ---- leadership ----
    def ensure_leadership(self) -> bool:
        was = self._is_leader
        if self.store.renew_leadership(self.sup_id, self.cfg.leader_ttl_s):
            self._is_leader = True
        elif self.store.acquire_leadership(self.sup_id,
                                           self.cfg.leader_ttl_s):
            self._is_leader = True
        else:
            self._is_leader = False
        if self._is_leader and not was:
            self.metrics.bump("leadership_changes")
            _logger.info("supervisor %s: became leader", self.sup_id)
        return self._is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def leader_info(self) -> Optional[Dict]:
        return self.store.get_leadership()

    # ---- scan (leader only) ----
    def orphan(self, hid: str, reason: str = "lease-expired") -> Dict:
        """Force-orphan: CAS the ownership row to __orphaned__ (fence+1).
        The worker's cached fence is now stale — it is fenced out even
        if it is still alive.

        Failure-type-aware: the orphan reason drives recovery.
        - lease-expired → transient/infra → normal retry path.
        - stalled → bad plan → after max_stall_retries, DLQ instead of
          another blind retry (retrying a livelocked plan wastes budget).
        """
        V.check_hid(hid)
        if not self.store.force_orphan(hid, ORPHANED_HOLDER):
            return {"hid": hid, "action": "already-orphaned-or-released"}
        env = self.store.get_envelope(hid)
        ex = env["execution"]
        max_att = env["contract"]["budget"].get("max_attempts", 3)
        if ex["attempt"] >= max_att:
            ex["status"] = "failed"
            ex["error"] = (f"lease expired; max_attempts={max_att} exhausted"
                           f" → DLQ")
            self.store.save_envelope(hid, env)
            self.metrics.bump("dlq")
            _logger.warning("orphan %s: DLQ (attempt %d >= max %d)",
                            hid[:12], ex["attempt"], max_att)
            return {"hid": hid, "action": "dlq", "attempt": ex["attempt"]}
        if reason == "stalled":
            ex["stall_count"] = ex.get("stall_count", 0) + 1
            max_stalls = env["contract"].get("max_stall_retries", 2)
            if ex["stall_count"] > max_stalls:
                ex["status"] = "failed"
                ex["error"] = (
                    f"livelock: stalled {ex['stall_count']}x — bad plan,"
                    f" not transient → DLQ (not another blind retry)")
                self.store.save_envelope(hid, env)
                self.metrics.bump("dlq")
                _logger.warning("orphan %s: DLQ bad-plan (%d stalls)",
                                hid[:12], ex["stall_count"])
                return {"hid": hid, "action": "dlq",
                        "reason": "bad-plan",
                        "stall_count": ex["stall_count"]}
        else:
            # transient/infra failure resets the stall counter
            ex["stall_count"] = 0
        ex["status"] = "orphaned"
        ex["orphaned_at"] = time.time()
        ex["orphan_reason"] = reason
        self.store.save_envelope(hid, env)
        self.metrics.bump("orphans")
        _logger.info("orphan %s: reason=%s attempt=%d checkpoints=%d",
                     hid[:12], reason, ex["attempt"],
                     len(ex["checkpoints"]))
        for cb, name in ((self.on_orphan, "on_orphan"),
                         (self.provision, "provision")):
            if cb:
                try:
                    cb(hid)
                except Exception as e:
                    self.metrics.bump("errors")
                    _logger.exception("orphan %s: %s callback failed: %s",
                                      hid[:12], name, e)
        return {"hid": hid, "action": "orphaned", "attempt": ex["attempt"],
                "checkpoints": len(ex["checkpoints"])}

    def kill_on_budget(self, hid: str, reasons: list) -> Dict:
        """Hard budget enforcement: fail the handoff closed. No silent
        overrun — the spend stops here and the envelope says why."""
        V.check_hid(hid)
        env = self.store.get_envelope(hid)
        ex = env["execution"]
        if ex["status"] not in ("running", "orphaned"):
            return {"hid": hid, "action": "already-terminal"}
        ex["status"] = "failed"
        ex["error"] = "budget exhausted: " + "; ".join(reasons)
        ex["failed_at"] = time.time()
        self.store.save_envelope(hid, env)
        # release ownership so nothing keeps billing against it
        own = self.store.get_ownership(hid)
        if own:
            self.store.release_ownership(hid, own["holder"], own["fence"])
        self.metrics.bump("dlq")
        _logger.warning("budget-kill %s: %s", hid[:12], ex["error"])
        return {"hid": hid, "action": "budget-killed",
                "reasons": reasons}

    def scan(self) -> List[Dict]:
        """One pass. Only the leader scans; followers return []."""
        try:
            if not self.ensure_leadership():
                return []
            self.metrics.bump("scans")
            actions = [self.orphan(hid)
                       for hid in self.store.expired_leases()]
            # hard budget enforcement (fail closed)
            for v in self.store.budget_violations():
                actions.append(self.kill_on_budget(v["hid"], v["reasons"]))
            # stall detection: alive but making no progress
            for hid in self.store.stalled_handoffs(
                    stall_timeout_s=self.cfg.stall_timeout_s,
                    max_digest_repeats=self.cfg.stall_max_repeats):
                actions.append(self.orphan(hid, reason="stalled"))
            # expire stale approval requests
            n_exp = self.store.expire_approvals()
            if n_exp:
                _logger.info("expired %d stale approvals", n_exp)
            return actions
        except Exception as e:
            self.metrics.bump("errors")
            _logger.exception("supervisor %s: scan failed: %s",
                              self.sup_id, e)
            return []

    def watch(self, duration_s: Optional[float] = None) -> List[Dict]:
        """Scan until duration elapses or request_stop() / SIGTERM."""
        self._install_signal_handlers()
        end = time.time() + duration_s if duration_s else float("inf")
        actions: List[Dict] = []
        _logger.info("supervisor %s: watching (scan every %.1fs)",
                     self.sup_id, self.cfg.supervisor_scan_s)
        while time.time() < end and not self._stop.is_set():
            actions.extend(self.scan())
            self._stop.wait(self.cfg.supervisor_scan_s)
        _logger.info("supervisor %s: stopped (%d actions)",
                     self.sup_id, len(actions))
        return actions

    def pending_work(self) -> List[str]:
        out = []
        for hid in self.store.all_hids():
            try:
                st = self.store.get_envelope(hid)["execution"]["status"]
            except Exception:
                continue
            if st in ("orphaned", "pending"):
                out.append(hid)
        return out

    def health(self) -> Dict[str, Any]:
        """Health snapshot for monitoring."""
        lead = self.store.get_leadership()
        return {
            "supervisor_id": self.sup_id,
            "is_leader": self._is_leader,
            "leader": lead,
            "metrics": self.metrics.snapshot(),
            "pending_work": len(self.pending_work()),
        }
