"""Tests for v1.2 features: effect log, budget enforcement,
stall detection, approval gates."""
import time

import pytest

import dhp
from dhp import Store
from dhp.runner3 import Runner3
from dhp.supervisor3 import Supervisor3
from dhp.errors import ApprovalRequired, ApprovalDenied, BudgetExceeded
from dhp.config import DHPConfig


def _handoff(tmp_path, budget=None):
    store = Store(str(tmp_path))
    hid = dhp.dispatch(store, "test", {"x": 1},
                       {"type": "object"}, budget=budget)
    return store, hid


# ---- effect log ----
def test_effect_log_first_write_wins(tmp_path):
    store, hid = _handoff(tmp_path)
    ctx = dhp.claim(store, hid, "w1")
    assert ctx.record_effect("send-email-1", {"sent": True}) is True
    # duplicate (e.g. retried after a kill) does not overwrite
    assert ctx.record_effect("send-email-1", {"sent": False}) is False
    assert ctx.effect_done("send-email-1") == {"sent": True}
    assert ctx.effect_done("never-ran") is None
    ctx.release()


def test_effect_survives_worker_death(tmp_path):
    """The davidjones145 case: worker records the effect, dies before
    checkpointing; the standby sees it and does not re-fire."""
    cfg = DHPConfig(lease_interval_s=0.2, lease_max_missed=2,
                    supervisor_scan_s=0.1)
    store = Store(str(tmp_path), cfg=cfg)
    hid = dhp.dispatch(store, "test", {"x": 1}, {"type": "object"},
                       cfg=cfg)
    r1 = Runner3(str(tmp_path), runner_id="w1", store=store, cfg=cfg)
    ctx1 = r1.claim(hid)
    ctx1.record_effect("charge-card", {"tx": "abc123"})
    # worker dies without checkpointing (no release, no heartbeat stop)
    del ctx1

    # supervisor orphans after lease expiry
    sup = Supervisor3(str(tmp_path), supervisor_id="sup1", cfg=cfg)
    time.sleep(0.6)
    actions = sup.scan()
    assert any(a["action"] == "orphaned" for a in actions)

    # standby recovers and checks the effect log
    r2 = Runner3(str(tmp_path), runner_id="w2", store=store, cfg=cfg)
    ctx2 = r2.recover(hid)
    assert ctx2.effect_done("charge-card") == {"tx": "abc123"}
    ctx2.release()


# ---- budget enforcement ----
def test_budget_kill_stops_spend(tmp_path):
    store, hid = _handoff(tmp_path, budget={"max_spend_usd": 1.0,
                                            "max_steps": 100,
                                            "max_wallclock_s": 3600,
                                            "max_attempts": 1})
    ctx = dhp.claim(store, hid, "w1")
    ctx.note_spend(0.60)
    # worker-level check also fires
    with pytest.raises(BudgetExceeded):
        ctx.note_spend(0.60)
    ctx.release()

    # supervisor-level: fail closed even if the worker never checks
    store2, hid2 = _handoff(tmp_path, budget={"max_spend_usd": 0.5,
                                              "max_steps": 100,
                                              "max_wallclock_s": 3600,
                                              "max_attempts": 1})
    ctx2 = dhp.claim(store2, hid2, "w1")
    # bypass the worker check: write spend directly into the envelope
    env = store2.get_envelope(hid2)
    env["execution"]["spend_usd"] = 99.0
    store2.save_envelope(hid2, env)

    sup = Supervisor3(str(tmp_path), supervisor_id="sup1")
    actions = sup.scan()
    kill = [a for a in actions if a.get("action") == "budget-killed"]
    assert kill, actions
    st = dhp.status(store2, hid2)
    assert st["status"] == "failed"
    assert "budget exhausted" in st["error"]


def test_steps_budget_killed(tmp_path):
    store, hid = _handoff(tmp_path, budget={"max_steps": 2,
                                            "max_spend_usd": 100,
                                            "max_wallclock_s": 3600,
                                            "max_attempts": 1})
    ctx = dhp.claim(store, hid, "w1")
    ctx.note_step(2)
    with pytest.raises(BudgetExceeded):
        ctx.note_step(1)
    ctx.release()


# ---- stall detection ----
def test_stall_detection_orphans_livelocked_worker(tmp_path):
    cfg = DHPConfig(lease_interval_s=0.2, lease_max_missed=20,
                    supervisor_scan_s=0.1,
                    stall_timeout_s=0.5, stall_max_repeats=3)
    store = Store(str(tmp_path), cfg=cfg)
    hid = dhp.dispatch(store, "test", {"x": 1}, {"type": "object"},
                       cfg=cfg)
    ctx = dhp.claim(store, hid, "w1", cfg=cfg)
    ctx.checkpoint({"n": 0})  # one checkpoint, then livelock
    # worker keeps heartbeating (lease alive) but repeats the same digest
    for _ in range(4):
        n = ctx.note_activity("digest-same-tool-same-args")
        ctx.heartbeat()
        time.sleep(0.15)
    assert n >= 3

    sup = Supervisor3(str(tmp_path), supervisor_id="sup1", cfg=cfg)
    actions = sup.scan()
    stalled = [a for a in actions
               if a.get("action") == "orphaned"
               and dhp.status(store, hid).get("status") == "orphaned"]
    assert stalled, actions
    ctx.stop_heartbeat()


def test_healthy_worker_not_flagged_as_stalled(tmp_path):
    cfg = DHPConfig(lease_interval_s=0.2, lease_max_missed=20,
                    supervisor_scan_s=0.1,
                    stall_timeout_s=30.0, stall_max_repeats=10)
    store = Store(str(tmp_path), cfg=cfg)
    hid = dhp.dispatch(store, "test", {"x": 1}, {"type": "object"},
                       cfg=cfg)
    ctx = dhp.claim(store, hid, "w1", cfg=cfg)
    for i in range(3):
        ctx.checkpoint({"n": i})
        ctx.note_activity(f"digest-{i}")
        time.sleep(0.05)
    sup = Supervisor3(str(tmp_path), supervisor_id="sup1", cfg=cfg)
    actions = sup.scan()
    assert dhp.status(store, hid)["status"] == "running", actions
    ctx.release()


# ---- approval gates ----
def test_approval_gate_fail_closed(tmp_path):
    store, hid = _handoff(tmp_path)
    ctx = dhp.claim(store, hid, "w1")
    # destructive action without approval -> fail closed
    with pytest.raises(ApprovalRequired) as exc:
        ctx.request_approval("destructive", {"cmd": "rm -rf /"})
    approval_id = str(exc.value).split(": ")[-1]

    # read actions pass through
    assert ctx.request_approval("read", {"path": "/tmp"}) == ""

    # approver decides externally
    assert dhp.approve(store, approval_id) is True
    assert ctx.await_approval(approval_id, poll_s=0.05) is True

    # denied approval -> fail closed
    with pytest.raises(ApprovalRequired) as exc2:
        ctx.request_approval("financial", {"amount": 100})
    aid2 = str(exc2.value).split(": ")[-1]
    assert dhp.deny(store, aid2) is True
    with pytest.raises(ApprovalDenied):
        ctx.await_approval(aid2, poll_s=0.05)
    ctx.release()


def test_approval_timeout_fail_closed(tmp_path):
    store, hid = _handoff(tmp_path)
    ctx = dhp.claim(store, hid, "w1")
    with pytest.raises(ApprovalRequired) as exc:
        ctx.request_approval("destructive", {"cmd": "drop table"},
                             timeout_s=0.2)
    aid = str(exc.value).split(": ")[-1]
    with pytest.raises(ApprovalDenied):
        ctx.await_approval(aid, poll_s=0.05)
    ctx.release()


def test_approvals_visible_in_status(tmp_path):
    store, hid = _handoff(tmp_path)
    ctx = dhp.claim(store, hid, "w1")
    try:
        ctx.request_approval("destructive", {"cmd": "x"})
    except ApprovalRequired:
        pass
    st = dhp.status(store, hid)
    assert len(st["approvals"]) == 1
    assert st["approvals"][0]["action_class"] == "destructive"
    ctx.release()


# ---- failure-type-aware recovery ----
def test_repeated_stalls_dlq_as_bad_plan(tmp_path):
    """A livelocked plan must not blind-retry forever: after
    max_stall_retries, the supervisor DLQs it as a bad plan."""
    cfg = DHPConfig(lease_interval_s=0.2, lease_max_missed=30,
                    supervisor_scan_s=0.1,
                    stall_timeout_s=0.4, stall_max_repeats=2)
    store = Store(str(tmp_path), cfg=cfg)
    hid = dhp.dispatch(store, "test", {"x": 1}, {"type": "object"},
                       max_stall_retries=1, cfg=cfg)
    sup = Supervisor3(str(tmp_path), supervisor_id="sup1", cfg=cfg)

    for attempt in range(2):
        ctx = dhp.claim(store, hid, f"w{attempt}", cfg=cfg) \
            if attempt == 0 else dhp.recover(store, hid, f"w{attempt}",
                                             cfg=cfg)
        ctx.checkpoint({"n": 0})
        for _ in range(3):  # livelock: same digest, heartbeats alive
            ctx.note_activity("same-digest")
            ctx.heartbeat()
            time.sleep(0.1)
        ctx.stop_heartbeat()
        actions = sup.scan()
        st = dhp.status(store, hid)["status"]
        if attempt == 0:
            assert st == "orphaned", (actions, st)
        else:
            # second stall exceeds max_stall_retries=1 -> DLQ, not retry
            assert st == "failed", (actions, st)
            assert "bad plan" in dhp.status(store, hid)["error"]


def test_transient_failure_resets_stall_count(tmp_path):
    """A heartbeat-timeout (transient) after a stall resets the
    bad-plan counter — infra blips don't poison the plan."""
    cfg = DHPConfig(lease_interval_s=0.2, lease_max_missed=30,
                    supervisor_scan_s=0.1,
                    stall_timeout_s=0.4, stall_max_repeats=2)
    store = Store(str(tmp_path), cfg=cfg)
    hid = dhp.dispatch(store, "test", {"x": 1}, {"type": "object"},
                       max_stall_retries=5, cfg=cfg)
    sup = Supervisor3(str(tmp_path), supervisor_id="sup1", cfg=cfg)

    # stall once
    ctx = dhp.claim(store, hid, "w0", cfg=cfg)
    ctx.checkpoint({"n": 0})
    for _ in range(3):
        ctx.note_activity("same-digest")
        ctx.heartbeat()
        time.sleep(0.1)
    ctx.stop_heartbeat()
    sup.scan()
    assert dhp.status(store, hid)["status"] == "orphaned"

    # recover, then die transiently (no heartbeat, no digest loop)
    ctx2 = dhp.recover(store, hid, "w1", cfg=cfg)
    ctx2.checkpoint({"n": 1})  # real progress
    del ctx2  # dies without heartbeating
    time.sleep(0.7)  # lease 0.2*30=6s won't expire; force via orphan path
    # simulate transient orphan directly
    env = store.get_envelope(hid)
    assert env["execution"]["stall_count"] == 1
    # transient orphan resets the counter
    sup.orphan(hid, reason="lease-expired")
    env = store.get_envelope(hid)
    assert env["execution"]["stall_count"] == 0


def test_schema_failure_dlq_with_typed_error(tmp_path):
    """Bad output is a failure type, not a retry candidate: the handoff
    is marked failed with the typed error instead of stuck running."""
    store = Store(str(tmp_path))
    hid = dhp.dispatch(store, "test", {"x": 1},
                       {"type": "array", "items": {"type": "string"}})
    ctx = dhp.claim(store, hid, "w1")
    with pytest.raises(Exception):
        ctx.complete({"not": "a list"})
    st = dhp.status(store, hid)
    assert st["status"] == "failed"
    assert "schema validation" in st["error"]
    ctx.stop_heartbeat()


# ---- capability attenuation ----
def test_capability_fail_closed(tmp_path):
    from dhp.errors import CapabilityDenied
    store = Store(str(tmp_path))
    hid = dhp.dispatch(
        store, "test", {"x": 1}, {"type": "object"},
        capabilities={"network": ["api.stripe.com"],
                      "filesystem": ["/tmp/work"]})
    ctx = dhp.claim(store, hid, "w1")
    # declared: granted
    ctx.require_capability("network", "api.stripe.com")
    # not declared: denied, fail-closed
    with pytest.raises(CapabilityDenied):
        ctx.require_capability("network", "evil.example.com")
    with pytest.raises(CapabilityDenied):
        ctx.require_capability("filesystem", "/etc/passwd")
    # dhp's own primitives always allowed
    ctx.require_capability("dhp", "checkpoint")
    # audit trail records both
    audit = store.capability_audit(hid)
    assert len(audit) == 4
    assert sum(1 for a in audit if a["granted"]) == 2
    assert sum(1 for a in audit if not a["granted"]) == 2
    ctx.release()


def test_no_capabilities_denies_everything(tmp_path):
    from dhp.errors import CapabilityDenied
    store, hid = _handoff(tmp_path)
    ctx = dhp.claim(store, hid, "w1")
    with pytest.raises(CapabilityDenied):
        ctx.require_capability("network", "api.stripe.com")
    ctx.release()
