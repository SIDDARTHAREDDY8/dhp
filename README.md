# DHP — Durable Handoff Protocol

**Your AI agent dies mid-run. Another agent picks up exactly where it left off. Zero rework.**

`kill -9`, OOM, spot instance reclaimed, laptop lid closed — the work survives. DHP is the durability layer for agent systems:

- **MCP** = agent ↔ tool
- **A2A** = agent ↔ agent
- **DHP** = agent ↔ time (work outlives the worker)

## The 30-second demo

```bash
pip install dhp
```

```python
import dhp
store = dhp.Store("./demo")

hid = dhp.dispatch(store, task_kind="demo.count",
                   inputs={"target": 50},
                   output_schema={"type": "object"})

# worker (any process, any host):
with dhp.claim(store, hid, worker_id="w1") as ctx:  # auto-heartbeats
    state = dhp.last_checkpoint(store, hid) or {"n": 0}
    while state["n"] < 50:
        state["n"] += 1
        ctx.checkpoint(state)      # every step is a resume point
    ctx.complete({"counted": state["n"]})
```

Now `kill -9` that worker mid-run. Start a supervisor (`dhp-supervisor ./demo sup1`):
it orphans the dead handoff in ~7s. Any standby recovers from the last
checkpoint — steps 1–20 are never recomputed.

**Full kill demo** (`demo/run_mad_demo.py`): 60 real web pages fetched,
`kill -9` at page 20, checkpoints shipped over TCP to a peer host as
they're made, standby resumes at page 20 → 60/60 complete, zero
refetched.

## The five guarantees

1. **No lost results** — every completed handoff's result is durable.
2. **No crossed inputs** — content-addressed envelopes; inputs can't be swapped.
3. **No untyped results** — JSON Schema validated on completion.
4. **No silent budget overrun** — hard caps on steps, spend, wallclock, attempts; DLQ, never silent drop.
5. **No runner lock-in** — suspend → ship → resume on any host, any runner.

## How it works

- **Leases + fencing.** Workers hold time-bounded leases and heartbeat (automatically, in a background thread). A silent worker is declared dead; the supervisor orphans its handoff and bumps a monotonic fence token — the dead worker's late writes are rejected, so two workers can never both believe they own the work.
- **Content-addressed envelopes.** The handoff ID covers the *intent* (task, inputs, schema), not the state — so a resumed handoff is verifiably the same work.
- **Append-only transport.** Checkpoints ship as a JSON-lines log — over TCP (`dhp.net`) or files — verified hash-by-hash on ingest. A destroyed host's work is already on the peer.
- **Pluggable storage.** `StoreBackend` interface; SQLite/WAL is the default, Postgres is a clean extension.

## Measured, not claimed

| Event | Time |
|---|---|
| kill → orphan → standby resumes | ~7s |
| Double kill (worker + supervisor leader) → recovery | 6.6s |
| Host destroyed → peer ingests log → resumes | ~6s |
| Rework after any kill | 0 checkpoints |

Verified by `dhp-conform` (10 protocol assertions), `dhp-chaos`
(randomized SIGKILL fault injection), and a 20-agent MCP soak with 75%
worker death rate → 20/20 completed.

## For agents

- **MCP server**: `dhp-mcp --root <dir>` — eight tools (`dhp_dispatch`, `dhp_claim`, `dhp_checkpoint`, `dhp_heartbeat`, `dhp_complete`, `dhp_recover`, `dhp_status`, `dhp_last_checkpoint`).
- **Skill**: `~/workspace/skills/dhp-durable-work/SKILL.md` teaches agents the pattern.
- **A2A bridge**: `dhp.a2a_bridge` — the A2A Task ID stays stable while DHP swaps dead workers underneath (see `BRIDGE_DEMO.md`).

## Docs

- `QUICKSTART.md` — zero to kill-demo in 5 minutes
- `SPEC.md` — the protocol
- `PRODUCT.md` — positioning and architecture
- `demo/` — the mad demo + network chaos harness

## License

MIT — see LICENSE.
