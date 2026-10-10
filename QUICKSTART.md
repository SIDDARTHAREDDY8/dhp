# DHP Quickstart — from zero to "my worker died and nobody cared" in 5 minutes

## Install

```bash
pip install dhp
```

## 1. Dispatch durable work (30 seconds)

```python
import dhp
store = dhp.Store("./demo-dhp")

hid = dhp.dispatch(
    store,
    task_kind="demo.count",
    inputs={"target": 50},
    output_schema={"type": "object",
                   "properties": {"counted": {"type": "integer"}},
                   "required": ["counted"]},
)
print("handoff:", hid[:12])
```

## 2. Start the supervisor (one terminal)

```bash
dhp-supervisor ./demo-dhp sup1 3600
```

It watches leases. A worker that goes silent for ~6s gets orphaned
automatically — no human in the loop.

## 3. Work it — then kill it (the wow)

Worker terminal — the context manager runs heartbeats in a background
thread, so slow work never false-orphans you:

```python
import dhp, time
store = dhp.Store("./demo-dhp")
hid = open("HID.txt").read().strip()   # from step 1
with dhp.claim(store, hid, worker_id="w1") as ctx:
    state = dhp.last_checkpoint(store, hid) or {"n": 0}
    while state["n"] < 50:
        state["n"] += 1
        ctx.checkpoint(state)     # every step is a resume point
        time.sleep(0.2)
        if state["n"] == 20:
            raise SystemExit("simulating death")   # <-- kill it here
    ctx.complete({"counted": state["n"]})
```

Watch the supervisor terminal: it orphans the handoff within ~7s.

## 4. Any standby resumes — zero rework

New worker (or the same script, restarted):

```python
import dhp, time
store = dhp.Store("./demo-dhp")
hid = open("HID.txt").read().strip()
with dhp.recover(store, hid, worker_id="w2") as ctx:
    state = dhp.last_checkpoint(store, hid)   # {"n": 20} — picks up here
    print("resuming from", state)
    while state["n"] < 50:
        state["n"] += 1
        ctx.checkpoint(state); time.sleep(0.2)
    ctx.complete({"counted": state["n"]})
print("done:", dhp.status(store, hid)["result"])
```

Steps 1–20 were never recomputed. That's the whole product.

## 5. Cross-host: ship checkpoints over TCP as they're made

Peer terminal (the "other datacenter"):

```python
from dhp import TransportServer
TransportServer("./peer-logs", port=8471).serve_forever()
```

Worker ships every checkpoint live:

```python
from dhp import NetShipper
shipper = NetShipper("peer.example.com", 8471,
                     local_logpath="./ship-w1.log")
# ... inside the work loop, after each ctx.checkpoint(state):
shipper.ship_checkpoint(hid, seq, state)
```

If this host dies entirely, the peer already has the checkpoints:
ingest its log and recover there. `demo/run_mad_demo.py` shows the
full flow: 60 real web pages, `kill -9` at page 20, resume on the
peer from page 20, 60/60 complete, zero refetched.

## 6. For AI agents (MCP)

```bash
dhp-mcp --root ./demo-dhp
```

Point any MCP client at it: eight tools (`dhp_dispatch`, `dhp_claim`,
`dhp_checkpoint`, `dhp_heartbeat`, `dhp_complete`, `dhp_recover`,
`dhp_status`, `dhp_last_checkpoint`). The skill at
`~/workspace/skills/dhp-durable-work/SKILL.md` teaches the pattern.

## Tuning

```bash
export DHP_LEASE_INTERVAL_S=2.0   # heartbeat every 2s
export DHP_LEASE_MAX_MISSED=3     # dead after 3 misses (~6s)
export DHP_MAX_CHECKPOINT_BYTES=1000000
```

## Verify it yourself

```bash
dhp-conform   # 10 protocol assertions
```
