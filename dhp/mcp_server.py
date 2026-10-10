"""DHP MCP server — durable agent execution as MCP tools (v1.0).

Run:  dhp-mcp --root /var/lib/myapp/dhp
   or: python3 -m dhp.mcp_server --root /var/lib/myapp/dhp

Tools:
  dhp_dispatch(task_kind, inputs, output_schema) -> handoff_id
  dhp_claim(handoff_id, worker_id)               -> lease + resume point
  dhp_checkpoint(handoff_id, worker_id, state)   -> seq
  dhp_heartbeat(handoff_id, worker_id)           -> ok
  dhp_complete(handoff_id, worker_id, result)    -> ok
  dhp_recover(handoff_id, worker_id)             -> lease + resume point
  dhp_status(handoff_id)                        -> status summary
  dhp_last_checkpoint(handoff_id)               -> last state or null
  dhp_health()                                  -> server health + metrics

Hardening: every input validated (hid format, worker_id charset,
payload sizes) before touching the store. Errors are typed JSON,
never tracebacks. Deploy behind socket permissions or a reverse proxy
for auth — the server itself trusts its local caller.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dhp as D  # noqa: E402
from dhp import errors as E  # noqa: E402
from dhp import validate as V  # noqa: E402
from dhp import log as dhp_log  # noqa: E402
from dhp.config import DHPConfig  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

_logger = dhp_log.get_logger()

mcp = MCPServer("dhp")
_STORE: D.Store | None = None
_CFG = DHPConfig()


def _store() -> D.Store:
    assert _STORE is not None, "server not initialised"
    return _STORE


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw})


def _err(e: Exception) -> str:
    if isinstance(e, E.DHPError):
        return json.dumps({"ok": False, "error": str(e),
                           "code": type(e).__name__})
    _logger.exception("unexpected error: %s", e)
    return json.dumps({"ok": False, "error": "internal error",
                       "code": "InternalError"})


def _runner(worker_id: str) -> D.Runner3:
    V.check_worker_id(worker_id)
    return D.Runner3(_store().root, runner_id=worker_id,
                     store=_store(), cfg=_CFG)


def _fence(hid: str, worker_id: str) -> int:
    own = _store().get_ownership(V.check_hid(hid))
    if not own or own["holder"] != worker_id:
        raise E.FencingError(
            f"{worker_id} does not hold {hid[:12]}")
    return own["fence"]


@mcp.tool(description="Dispatch durable work. Returns a handoff_id. "
          "The work survives worker death from here on.")
def dhp_dispatch(task_kind: str, inputs: Dict[str, Any],
                 output_schema: Dict[str, Any]) -> str:
    try:
        if not isinstance(task_kind, str) or not task_kind or \
                len(task_kind) > 128:
            raise E.ValidationError("malformed task_kind")
        V.check_inputs(inputs, _CFG)
        if not isinstance(output_schema, dict):
            raise E.ValidationError("output_schema must be an object")
        hid = D.dispatch(_store(), task_kind, inputs, output_schema,
                         cfg=_CFG)
        _logger.info("mcp dispatch %s kind=%s", hid[:12], task_kind)
        return _ok(handoff_id=hid)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Claim a handoff for a worker. Returns the lease "
          "and the last checkpointed state (resume point).")
def dhp_claim(handoff_id: str, worker_id: str) -> str:
    try:
        ctx = D.claim(_store(), V.check_hid(handoff_id),
                      V.check_worker_id(worker_id), cfg=_CFG)
        state = D.last_checkpoint(_store(), handoff_id)
        return _ok(fence=ctx.fence, resume_from=state)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Checkpoint worker progress. Call per chunk of work.")
def dhp_checkpoint(handoff_id: str, worker_id: str,
                   state: Dict[str, Any]) -> str:
    try:
        V.check_hid(handoff_id)
        V.check_worker_id(worker_id)
        V.check_checkpoint_state(state, _CFG)
        r = _runner(worker_id)
        seq = r.checkpoint(handoff_id, state, _fence(handoff_id, worker_id))
        r.heartbeat(handoff_id, _fence(handoff_id, worker_id))
        return _ok(seq=seq)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Heartbeat: prove the worker is alive. "
          "Call frequently; a silent worker is assumed dead.")
def dhp_heartbeat(handoff_id: str, worker_id: str) -> str:
    try:
        V.check_hid(handoff_id)
        V.check_worker_id(worker_id)
        r = _runner(worker_id)
        r.heartbeat(handoff_id, _fence(handoff_id, worker_id))
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Complete the handoff with a schema-validated result.")
def dhp_complete(handoff_id: str, worker_id: str,
                 result: Dict[str, Any]) -> str:
    try:
        V.check_hid(handoff_id)
        V.check_worker_id(worker_id)
        V.check_result(result, _CFG)
        r = _runner(worker_id)
        r.complete(handoff_id, result, _fence(handoff_id, worker_id))
        _logger.info("mcp complete %s by %s", handoff_id[:12], worker_id)
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Recover an orphaned handoff (worker died). Returns "
          "the lease and the last checkpointed state to resume from.")
def dhp_recover(handoff_id: str, worker_id: str) -> str:
    try:
        ctx = D.recover(_store(), V.check_hid(handoff_id),
                        V.check_worker_id(worker_id), cfg=_CFG)
        state = D.last_checkpoint(_store(), handoff_id)
        _logger.info("mcp recover %s by %s", handoff_id[:12], worker_id)
        return _ok(fence=ctx.fence, resume_from=state)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Status of a handoff: state, checkpoints, owner, "
          "result if completed.")
def dhp_status(handoff_id: str) -> str:
    try:
        return _ok(**D.status(_store(), V.check_hid(handoff_id)))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Last checkpointed state, or null if none yet.")
def dhp_last_checkpoint(handoff_id: str) -> str:
    try:
        return _ok(state=D.last_checkpoint(_store(),
                                           V.check_hid(handoff_id)))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Server health: uptime, store stats.")
def dhp_health() -> str:
    try:
        s = _store()
        return _ok(handoffs=s.handoff_count(),
                   lease_ttl_s=_CFG.lease_ttl_s)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Record a side effect by step_id. First write wins; "
          "returns recorded=true if this call stored it, false if a "
          "previous attempt already did (do NOT re-fire the effect).")
def dhp_record_effect(handoff_id: str, worker_id: str,
                      step_id: str, result: Dict[str, Any]) -> str:
    try:
        r = _runner(worker_id)
        recorded = r.store.record_effect(V.check_hid(handoff_id),
                                         step_id, result)
        return _ok(recorded=recorded)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Check whether a side effect already happened. "
          "Returns the recorded result, or null. Call before re-executing "
          "a side effect after recovery.")
def dhp_effect_done(handoff_id: str, step_id: str) -> str:
    try:
        return _ok(result=_store().get_effect(V.check_hid(handoff_id),
                                              step_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Record spend in USD against the handoff budget. "
          "The supervisor fails the handoff closed on budget breach.")
def dhp_note_spend(handoff_id: str, worker_id: str,
                   usd: float) -> str:
    try:
        r = _runner(worker_id)
        hid = V.check_hid(handoff_id)
        fence = _fence(hid, worker_id)
        r._bump(hid, fence, spend=usd)
        return _ok()
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Report an activity digest (hash of tool name + args). "
          "The supervisor orphans the handoff as stalled if the digest "
          "repeats with no new checkpoint (livelock detection).")
def dhp_note_activity(handoff_id: str, worker_id: str,
                      digest: str) -> str:
    try:
        r = _runner(worker_id)
        hid = V.check_hid(handoff_id)
        fence = _fence(hid, worker_id)
        n = r.store.note_progress(hid, digest)["repeat_count"]
        return _ok(repeat_count=n)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Request approval for a destructive/financial action. "
          "Fail-closed: returns an approval_id; the action must NOT run "
          "until dhp_await_approval reports approved.")
def dhp_request_approval(handoff_id: str, worker_id: str,
                         action_class: str,
                         details: Dict[str, Any]) -> str:
    try:
        r = _runner(worker_id)
        hid = V.check_hid(handoff_id)
        fence = _fence(hid, worker_id)
        aid = r.store.request_approval(hid, action_class, details)
        return _ok(approval_id=aid)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Check an approval's status: pending/approved/"
          "denied/expired. Only approved means the action may run.")
def dhp_approval_status(approval_id: str) -> str:
    try:
        ap = _store().get_approval(approval_id)
        if ap is None:
            return _err(ValueError("unknown approval_id"))
        return _ok(status=ap["status"], action_class=ap["action_class"])
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Approve a pending approval request "
          "(human / policy engine).")
def dhp_approve(approval_id: str) -> str:
    try:
        return _ok(decided=_store().decide_approval(approval_id, True))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(description="Deny a pending approval request "
          "(human / policy engine).")
def dhp_deny(approval_id: str) -> str:
    try:
        return _ok(decided=_store().decide_approval(approval_id, False))
    except Exception as e:  # noqa: BLE001
        return _err(e)


def main() -> None:
    ap = argparse.ArgumentParser(description="DHP MCP server")
    ap.add_argument("--root", required=True,
                    help="DHP store directory (created if missing)")
    args = ap.parse_args()
    dhp_log.configure()
    global _STORE
    _STORE = D.Store(args.root, cfg=_CFG)
    _logger.info("dhp-mcp serving root=%s", args.root)
    mcp.run()


if __name__ == "__main__":
    main()
