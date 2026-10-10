# DHP — Durable Handoff Protocol (v0.1 prototype)

**The problem.** Multi-agent systems lose work *between* agents: handoffs are
lossy prose, in-flight state dies with the runner, results have no schema,
budgets are prompt-suggestions. Every framework re-solves this privately;
no wire format exists for "a suspended run."

**The primitive.** A *handoff envelope*: one unit of delegated work as a
single content-addressed JSON document (task + contract + provenance +
execution state). Five guarantees:

1. No lost results — durable outbox + acknowledgement (at-least-once, idempotent)
2. No crossed inputs — hash-bound inputs verified at dispatch
3. No untyped results — JSON-Schema validation enforced at completion
4. No silent budget overrun — runtime-enforced caps, not prompt suggestions
5. No runner lock-in — suspend/resume is portable across conformant runners

Composes, doesn't compete: carry the envelope as an A2A task payload;
invoke MCP tools *inside* the task.

## Layout

- `SPEC.md` — the protocol spec (v0.1 draft)
- `SYNTHESIS.md` — why this target (three evidence legs converged here)
- `my_friction_log.md`, `protocol_gaps.md`, `field_bottlenecks.md` — the evidence
- `dhp/envelope.py` — envelope: canonicalization, content addressing,
  input-hash verification, result schema validation
- `dhp/runner.py` — `LocalRunner`: dispatch / claim / checkpoint / suspend /
  resume / recover / complete / ack / fail, with budget enforcement
- `dhp/demo.py` — the dogfood: fan-out, kill a worker mid-run, resume on a
  fresh runner, prove zero loss

## Run it

```bash
cd ~/workspace/inventions/bottleneck-hunt
python3 -m dhp.demo          # v0.1: portable resume, kill-a-worker demo
python3 -m dhp.conform       # v0.4: 10 conformance assertions, all green
python3 -m dhp.chaos --seed 42  # v0.4: randomized SIGKILL fault injection
```

v0.2 (leases + supervisor + fencing), v0.3 (SQLite, CAS, replicated
supervisors), and v0.4 (transport log, cross-host failover) are
exercised through `dhp/work_driver3.py`, `dhp/work_driver4.py`, and
`dhp/supervisor3_watch.py` — see `CHAOS_REPORT_V02.md`,
`CHAOS_REPORT_V03.md`, `CHAOS_REPORT_V04.md` for the kill-by-kill
accounts.

## A2A bridge

`dhp/a2a_bridge.py` makes DHP the durability substrate under A2A
tasks: the A2A Task ID is the stable identity, DHP handoffs are the
execution attempts underneath. `dhp/bridge_worker.py` is the worker
driver. Demo in `BRIDGE_DEMO.md`: Agent B killed mid-task → Agent C
recovered → A2A client saw WORKING → COMPLETED, worker swap invisible.
Requires `a2a-sdk`.

## Status

v0.4, chaos-proven through host destruction. **Chaos-tested
2026-10-09** (see `CHAOS_REPORT.md`, `CHAOS_REPORT_V02.md`,
`CHAOS_REPORT_V03.md`, `CHAOS_REPORT_V04.md`, `BRIDGE_DEMO.md`):
29/29 v0.2 envelopes correct; v0.3 double-kill (worker + lead
supervisor) → 6.6 s orphan → zero-rework recovery; v0.4 host
destruction → cross-host resume from the transport log → correct;
randomized SIGKILL harness green; A2A task survived worker death with
the swap invisible at the A2A layer. Measured failover in seconds
(~7 s kill→orphan); sub-second remains a documented no-go.
