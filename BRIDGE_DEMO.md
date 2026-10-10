# DHP ↔ A2A bridge — demo report (2026-10-09)

## The gap it closes

From `protocol_gaps.md`: A2A "exposes state. It does not provide
consensus or global transaction semantics" — when the remote worker
dies mid-task, stock A2A leaves the task hanging in WORKING. The work
is lost; the client times out.

## The design

`dhp/a2a_bridge.py`: the **A2A Task ID is the stable identity**; DHP
handoffs are the execution attempts underneath.

- `submit_task()` wraps the A2A Task in a DHP handoff; the binding
  (`a2a_task_id`) is stored in durable envelope provenance, so any
  bridge instance rebuilds it from the store (no in-memory state is
  load-bearing — this was a real bug found during the demo).
- Lifecycle mapping: DHP pending/running/**orphaned** → A2A WORKING
  (orphaned is transient — a standby is coming; stock A2A would hang
  here forever); DHP completed → A2A COMPLETED + Artifact; DHP
  failed (DLQ) → A2A FAILED.
- Uses the real `a2a-sdk` types (Task/TaskStatus/Artifact/Message).
  Transport is in-process for the prototype; A2A's HTTP/JSON-RPC
  layer is orthogonal to the durability question.

## Live demo (real subagent workers, real kill)

- Client submitted A2A task `task-2fbc6150eb7f` (prime census,
  [5000000, 5160000), 1600 chunks). State: WORKING.
- Agent B claimed via the bridge, worked 214 checkpoints.
- **Agent B killed mid-task** (`subagent.close`).
- DHP supervisor orphaned the handoff (219 checkpoints survived).
  **Client view: still TASK_STATE_WORKING** — no failure visible.
- Agent C recovered via the bridge, resumed from checkpoint 219,
  completed 1600/1600.
- **Client view: TASK_STATE_COMPLETED** with an Artifact carrying
  the result: 10,329 primes, sum 52,472,162,893.
- Independently sieve-verified CORRECT. Checkpoint sequence
  continuous (zero rework). The A2A task ID never changed.

## What this proves

An A2A task can now survive its worker's death with the worker swap
invisible at the A2A layer. DHP is the durability substrate A2A
deliberately leaves out — the two compose instead of competing.

## Files

- `dhp/a2a_bridge.py` — the bridge
- `dhp/bridge_worker.py` — worker driver for the demo
- `bridge_demo/` — demo root, task records, final A2A Task JSON
