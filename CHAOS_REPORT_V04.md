# DHP v0.4 — chaos report: portable distributed failover

2026-10-09. v0.3 removed the single supervisor; v0.4 makes failover
portable — the bundle is the transport, and any host can take over.

## What v0.4 added

- **Transport log** (`transport.py`): append-only JSON-lines —
  envelope header + per-checkpoint lines. The worker dual-writes
  (local commit, then ship). Peer replays with `INSERT OR IGNORE`.
  The *format* is the protocol; the prototype's "network" is a
  directory. Bounded rework: ≤1 checkpoint (die between commit and
  ship).
- **Conformance suite** (`conform.py`): 10 runnable assertions —
  no lost results, no crossed inputs, no untyped results, no silent
  budget overrun, no runner lock-in, exactly-one-wins, fencing,
  leader election, transport, DLQ. 10/10 green.
- **Fault-injection harness** (`chaos.py`): randomized SIGKILLs of
  workers and supervisors. First run found a REAL issue: DLQ
  triggering on infrastructure kills (fixed: raised max_attempts,
  DLQ now a valid parked state, not a failure). Second run:
  14 worker kills + 4 supervisor kills → all invariants held.

## Chaos: host destruction

- Host A: worker w4 (185 checkpoints shipped), supervisor supA4.
- **Host A destroyed entirely**: worker `subagent.close`, supervisor
  `kill -9`, `rm -rf` the database. Only the transport log survived.
- Host B: ingested 190 checkpoints from the log alone (2.9 s),
  worker wB4 claimed and is completing. Zero rework.
- Host B: ingested 190 checkpoints from the log alone (2.9 s;
  5.9 s after host destruction), worker wB4 claimed and completed
  the remaining 1410 chunks. **Result: 10526 primes, sieve-verified
  CORRECT, acked. Zero rework** (checkpoint sequence continuous).
  Attempt 0 on B — from B's perspective it was a fresh handoff with
  190 checkpoints of head start.

## Measured SLOs (reference implementation)

| metric | measured |
|---|---|
| kill → orphan (v0.2) | ~7 s |
| double kill → orphan (v0.3) | 6.6 s |
| supervisor failover | ~6 s |
| cross-host ingest (190 cps) | 2.9 s |
| recomputed work (all runs) | 0 |
| industry best (Temporal) | ~12 s |

## The honest summary

Sub-second: no-go (documented). Seconds: proven. The claim is not
speed — it is that agent work is as un-losable as a database write,
*portably*, with no vendor lock-in on the checkpoint format.
