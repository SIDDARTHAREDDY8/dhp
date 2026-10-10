# DHP — the product (v1.0 production-grade)

## What it is

DHP (Durable Handoff Protocol) is **infrastructure for agent work that
must not be lost**. Not a model, not an agent framework — the layer
underneath: when a worker dies mid-task, the work survives and a
standby resumes from the last checkpoint. Seconds, not sub-seconds;
zero rework; no vendor lock-in.

v1.0 (2026-10-09) hardened the v0.4 prototype for production: thread-safe
store, typed errors, input validation, size limits, tamper-verified
transport ingest, env-configurable tuning, structured logging, metrics,
graceful shutdown, schema versioning. See `PROD_AUDIT.md`.

It meets three kinds of users where they are:

| User | Interface | They run |
|---|---|---|
| AI agent (Claude, etc.) | **MCP server** — 8 tools | `dhp-mcp --root <dir>` |
| Developer | **Python SDK** — `pip install dhp` | `import dhp` + `dhp-supervisor` daemon |
| Agent framework | **A2A bridge** | `dhp.a2a_bridge` |

## Is it "a skill for agents"?

Partly — there is a skill (`~/workspace/skills/dhp-durable-work/SKILL.md`)
that teaches an agent *when* to reach for DHP and the operating rules.
But the real answer is better than a skill doc: the **MCP server gives
agents actual tools**. A skill tells an agent what to do; MCP tools let
it do it. Any MCP-capable agent can make its delegated work durable
tonight, with no code changes to the agent itself.

## How an agent uses it (via MCP)

The agent connects to the DHP MCP server and gets eight tools.
The pattern for any work that must survive:

```
1. dhp_dispatch(task_kind, inputs, output_schema) -> handoff_id
   # the durable record exists; the work cannot be lost from here on

2. (worker — you or a subagent)
   dhp_claim(handoff_id, worker_id) -> {fence, resume_from}
   loop:
     do one chunk of work
     dhp_checkpoint(handoff_id, worker_id, state)   # every chunk
     dhp_heartbeat(handoff_id, worker_id)           # frequently
   dhp_complete(handoff_id, worker_id, result)

3. (if the worker dies — any standby, even a different agent)
   dhp_recover(handoff_id, worker_id) -> {fence, resume_from}
   # resume_from is the last checkpoint; zero rework
```

A supervisor daemon (`dhp-supervisor --root <dir>`) watches leases and
orphans silent workers automatically (~7s). The standby doesn't need
to know the dead worker — it just recovers the handoff.

## How a developer uses it (via SDK)

```bash
pip install dhp
```

```python
import dhp
store = dhp.Store("/var/lib/myapp/dhp")

hid = dhp.dispatch(store, task_kind="etl.nightly",
                   inputs={"date": "2026-10-09"},
                   output_schema={...})

# worker process (anywhere):
ctx = dhp.claim(store, hid, worker_id="w1")
state = dhp.last_checkpoint(store, hid) or {"cursor": 0}
while not done:
    state = do_chunk(state)
    ctx.checkpoint(state); ctx.heartbeat()
ctx.complete({"rows": state["rows"]})

# after a death, anywhere else:
ctx = dhp.recover(store, hid, worker_id="w2")  # resumes at last cp
```

Cross-host: ship the transport log (`dhp.transport_ship_checkpoint`
per checkpoint); the peer ingests (`dhp.transport_ingest`) and resumes.
No shared database.

## How a framework uses it (A2A bridge)

`dhp.a2a_bridge.DHPBridge`: the A2A Task ID stays stable while DHP
swaps dead workers underneath. `submit_task()` wraps the task in a
handoff; `get_task()` reflects live state (orphaned still reads as
WORKING — the client never sees the failure). Demo: `BRIDGE_DEMO.md`.

## What's proven (v1.0, not promised)

- pytest suite: 39 passed (`tests/`: lifecycle, thread-safety, transport
  tamper-resistance, supervisor, adversarial fuzzing)
- Conformance suite: 10/10 (`dhp-conform`)
- Randomized SIGKILL chaos: 14 worker + 5 supervisor kills, green
- 20-agent MCP soak (75% mid-run death rate): 20/20 completed, zero lost
- MCP kill-recovery end-to-end through real MCP protocol: green
- Graceful shutdown (SIGTERM): finishes scan, clean exit
- Measured SLOs: kill→orphan ~7s, rework 0 (industry best ~12s)

## Limits (honest)

- Failover is **seconds**, not sub-second — documented no-go for
  stateful agent work (heartbeat floor + false-failover cost).
- Prototype-grade: SQLite backend, single-writer leadership, no
  multi-datacenter consensus, no Byzantine tolerance.
- The 6s lease TTL assumes heartbeats from the fast execution loop;
  slow-reasoning workers must heartbeat from a driver, not the LLM.
