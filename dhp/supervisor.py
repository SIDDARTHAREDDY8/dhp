"""DHP v0.2 — Supervisor: the watcher that makes failover automatic.

Loop: scan running handoffs; any lease past expires_at (death, stall, or
idle — indistinguishable by design) is declared orphaned and requeued.
After max_attempts, the handoff goes to the dead-letter state instead.

The supervisor does NOT diagnose. Silence is the signal — this is the
industry-standard answer (Temporal heartbeats, SQS visibility timeouts,
K8s liveness probes), applied to agent work.

Honest latency budget (survey-backed):
  detection ≈ heartbeat_interval × max_missed   (default 2s × 3 = ~6s)
  + scan interval (~1s) + standby pickup.
  Best measured stateful-agent failover in the industry: ~12s (Temporal,
  aggressive heartbeats). Sub-second is a documented no-go for LLM work:
  detection has a heartbeat floor and false failovers double-bill.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from . import envelope as E
from .runner import LocalRunner


class Supervisor:
    def __init__(self, root: str,
                 supervisor_id: Optional[str] = None,
                 on_orphan: Optional[Callable[[str], None]] = None,
                 scan_interval_s: float = 1.0):
        self.runner = LocalRunner(root,
                                  runner_id=supervisor_id or "supervisor")
        self.root = os.path.abspath(root)
        self.on_orphan = on_orphan
        self.scan_interval_s = scan_interval_s

    def _all_hids(self) -> List[str]:
        hdir = os.path.join(self.root, "handoffs")
        if not os.path.isdir(hdir):
            return []
        return sorted(os.listdir(hdir))

    def scan(self) -> List[Dict[str, Any]]:
        """One pass. Returns [{hid, action}] for action in
        {orphaned, dlq}. Idempotent: already-orphaned/failed handoffs
        are skipped."""
        actions = []
        now = time.time()
        for hid in self._all_hids():
            try:
                env = self.runner._load(hid)
            except Exception:
                continue
            ex = env["execution"]
            if ex["status"] != "running":
                continue
            if not E.lease_expired(env, now):
                continue
            max_att = env["contract"]["budget"].get("max_attempts", 3)
            if ex["attempt"] >= max_att:
                ex["status"] = "failed"
                ex["error"] = ("lease expired with no heartbeat; "
                               f"max_attempts={max_att} exhausted → DLQ")
                self.runner._save(env)
                actions.append({"hid": hid, "action": "dlq",
                                "attempt": ex["attempt"]})
            else:
                ex["status"] = "orphaned"
                ex["lease"] = None
                ex["orphaned_at"] = now
                self.runner._save(env)
                actions.append({"hid": hid, "action": "orphaned",
                                "attempt": ex["attempt"],
                                "checkpoints": len(ex["checkpoints"])})
                if self.on_orphan:
                    try:
                        self.on_orphan(hid)
                    except Exception:
                        pass
        return actions

    def watch(self, duration_s: float) -> List[Dict[str, Any]]:
        """Run the scan loop for duration_s. Returns all actions taken."""
        end = time.time() + duration_s
        all_actions = []
        while time.time() < end:
            all_actions.extend(self.scan())
            time.sleep(self.scan_interval_s)
        return all_actions

    def pending_orphans(self) -> List[str]:
        """Handoffs waiting for a worker: orphaned or never claimed."""
        out = []
        for hid in self._all_hids():
            try:
                env = self.runner._load(hid)
            except Exception:
                continue
            if env["execution"]["status"] in ("orphaned", "pending"):
                out.append(hid)
        return out

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for hid in self._all_hids():
            try:
                st = self.runner._load(hid)["execution"]["status"]
            except Exception:
                st = "unreadable"
            counts[st] = counts.get(st, 0) + 1
        return counts
