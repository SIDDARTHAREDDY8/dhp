"""DHP — Durable Handoff Protocol.

Agent work that survives worker death. Dispatch a handoff, claim it
from any worker, checkpoint as you go; if the worker dies, a
supervisor orphans the handoff and a standby recovers from the last
checkpoint. No lost results, no crossed inputs, no runner lock-in.

Quick start::

    import dhp

    # one-time: create a store + supervisor for your app
    store = dhp.Store("/var/lib/myapp/dhp")

    # dispatch durable work
    hid = dhp.dispatch(
        store,
        task_kind="prime.census",
        inputs={"lo": 0, "hi": 16000, "chunk": 100},
        output_schema={...},
    )

    # in the worker (any process, any host with the transport log):
    ctx = dhp.claim(store, hid, worker_id="w1")
    state = dhp.last_checkpoint(store, hid) or {"next_n": 0}
    for ...:
        ... do a chunk of work ...
        ctx.checkpoint(state); ctx.heartbeat()
    ctx.complete(result)

    # if the worker dies: supervisor orphans, standby recovers
    ctx2 = dhp.recover(store, hid, worker_id="w2")  # resumes at last cp
"""
from .envelope import build as _build_envelope
from .store import Store
from .backend import StoreBackend
from .runner3 import Runner3, TaskContext
from .supervisor3 import Supervisor3
from .config import DHPConfig, DEFAULT
from .transport import (
    ship_envelope as transport_ship_envelope,
    ship_checkpoint as transport_ship_checkpoint,
    ingest_log as transport_ingest,
)
from .net import TransportServer, NetShipper
from . import validate as _validate

__version__ = "1.1.1"
__all__ = [
    "Store", "StoreBackend", "Runner3", "TaskContext", "Supervisor3",
    "TransportServer", "NetShipper",
    "dispatch", "claim", "recover", "last_checkpoint", "status",
    "transport_ship_envelope", "transport_ship_checkpoint",
    "transport_ingest",
    "__version__",
]

_DEFAULT_BUDGET = {
    "max_steps": 10000,
    "max_spend_usd": 500.0,
    "max_wallclock_s": 5400.0,
    "max_attempts": 5,
}


def _runner(store: StoreBackend, worker_id: str = "api",
            cfg: DHPConfig = DEFAULT) -> Runner3:
    return Runner3(store.root, runner_id=worker_id, store=store, cfg=cfg)


def dispatch(store: StoreBackend, task_kind: str, inputs: dict,
             output_schema: dict, budget: dict | None = None,
             created_by: str = "dhp-api",
             cfg: DHPConfig = DEFAULT) -> str:
    """Create a durable handoff. Returns the handoff id."""
    _validate.check_inputs(inputs, cfg)
    env = _build_envelope(
        task_kind=task_kind,
        inputs={"inline": inputs},
        output_schema=output_schema,
        budget={**_DEFAULT_BUDGET, **(budget or {})},
        created_by=created_by)
    return _runner(store, cfg=cfg).dispatch(env)


def claim(store: StoreBackend, hid: str, worker_id: str,
          cfg: DHPConfig = DEFAULT) -> TaskContext:
    """Claim a pending handoff for a worker. Returns a TaskContext
    with .checkpoint(state), .heartbeat(), .complete(result)."""
    return _runner(store, worker_id, cfg).claim(_validate.check_hid(hid))


def recover(store: StoreBackend, hid: str, worker_id: str,
            cfg: DHPConfig = DEFAULT) -> TaskContext:
    """Recover an orphaned handoff. Resumes from the last checkpoint."""
    return _runner(store, worker_id, cfg).recover(_validate.check_hid(hid))


def last_checkpoint(store: Store, hid: str):
    """Return the last checkpointed state, or None."""
    return _runner(store).last_checkpoint(_validate.check_hid(hid))


def status(store: StoreBackend, hid: str) -> dict:
    """Return a summary: status, checkpoints, owner, result if done."""
    hid = _validate.check_hid(hid)
    r = _runner(store)
    env = r._load(hid)
    ex = env["execution"]
    own = store.get_ownership(hid)
    out = {
        "handoff_id": hid,
        "status": ex["status"],
        "checkpoints": len(ex["checkpoints"]),
        "attempt": ex.get("attempt", 0),
        "owner": own["holder"] if own else None,
    }
    if ex["status"] == "completed":
        out["result"] = env["delivery"]["result"]
    return out
