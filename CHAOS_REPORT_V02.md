# DHP v0.2 — chaos report: automatic failover

2026-10-09. v0.1 proved *resume* works; v0.2 proves *detection* works —
no human in the loop between death and reassignment.

## What v0.2 added

- **Leases**: `claim`/`recover`/`resume` bind the handoff to one holder,
  TTL = heartbeat_interval × max_missed (default 2 s × 3 = ~6 s).
- **Heartbeats**: workers renew per chunk; silence (death/stall/idle —
  indistinguishable by design) expires the lease.
- **Supervisor**: 1 s scan loop; expired lease → `orphaned` (checkpoints
  intact); past `max_attempts` (3) → `failed`/DLQ.
- **Fencing**: only the lease holder may checkpoint/step/heartbeat/
  complete; stale workers get `LeaseLost`/`RunnerError`.
- **Kill-vs-crash**: `release()` = cooperative instant requeue;
  silent death = lease-expiry path.
- **Idempotent complete**: completing twice is a no-op.

Self-test: 10/10 green (lease set, heartbeat, fencing, CLI, expiry→orphan,
orphan fencing, recover re-lease, idempotent complete, release, DLQ).

## Chaos design (real subagents, unannounced kills)

Four cases mapped to the user's doubts:
1. **kill**: worker heartbeating per chunk, killed mid-run via `subagent.close`.
2. **idle**: worker stops *all* protocol calls after 25 chunks, sleeps 60 s
   (alive but silent), then probes once.
3. **late / no-worker**: envelope dispatched, zero workers assigned — waits.
4. **control**: normal worker, no chaos.

Driver: `work_driver.py` — fast in-process chunk loop with per-chunk
heartbeats (see lesson below).

## Finding 1: heartbeats must come from the fast layer

First attempt used LLM workers doing sequential CLI calls (5–15 s action
latency) against the 6 s lease TTL. The supervisor false-orphaned healthy
workers — the survey's false-failover warning reproduced live. Fix:
heartbeat from the fast execution layer (the driver), not the reasoning
layer. Documented in SPEC.md as a tuning rule:
`TTL ≥ 3 × p99(heartbeat interval)`.

## Finding 2: idle worker — automatic detect, recover, fence

- wY worked 25 chunks (heartbeats flowing), then went silent at ~07:25:40.
- Supervisor (watching, no human) declared it **orphaned** — 25 checkpoints
  intact.
- Standby s1 recovered at attempt 1, worked the remaining 135 chunks,
  **completed**: 616 primes, sum 254377730.
- wY woke at ~07:26:40, tried one checkpoint → **rejected**
  (`RunnerError: not running (status=completed)` — s1 had already finished;
  the stale write could not fork the work).
- Zero chunks recomputed. Zero human intervention.

## Finding 3: real kill — automatic detect, recovering

- wP worked 222 chunks (pace 0.4 s/chunk, heartbeats per chunk).
- **Killed at 07:29:53 UTC via subagent.close — no warning.**
  Heartbeats stopped dead; checkpoint count froze at 222.
- Supervisor declared it **orphaned**; 222 checkpoints intact.
- Detection latency: 22.8 s end-to-end, of which ~15 s was test-harness
  downtime (my supervisor watch expired seconds before the kill and had to
  be restarted). Protocol budget under a live supervisor: lease TTL (6 s) +
  scan (1 s) ≈ **7 s**. Honest, not sub-second — as the survey established.
- Standby sP recovered (attempt 1) and is working the remaining chunks.
  [final numbers below]

## Finding 4: no worker available — safe wait, then pickup

- Late envelope sat `pending` with zero workers for minutes. Nothing lost,
  nothing timed out.
- Worker wL claimed it later and completed normally (160 chunks).
- The user's hardest question answered empirically: no agent available =
  *delayed* work, not *lost* work.

## Also covered

- v02 slow-idle envelope (wB, 20 checkpoints, idle): orphaned twice
  (worker recovered once itself, then idled) — standby s2 recovering.
- v02 late envelope: picked up by wL2.
- Kill-vs-crash: `release()` tested in self-test (instant requeue).
- DLQ: tested in self-test (max_attempts exhausted → failed).

## Numbers (final)

| case | kill/idle → orphan | checkpoints kept | recomputed | result |
|---|---|---|---|---|
| idle (wY→s1) | automatic, ~7 s budget | 25/25 | 0 | completed, 616 primes, sieve-verified |
| kill (wP→sP) | automatic, ~7 s budget (22.8 s w/ harness gap) | 222/222 | 0 | completed, 11029 primes, sieve-verified |
| late (→wL) | n/a (safe wait) | n/a | 0 | completed, 623 primes, sieve-verified |
| v02-idle (wB→s2) | automatic | 20/20 | 0 | completed, 160 cps, sieve-verified |
| v02-late (→wL2) | n/a (safe wait) | n/a | 0 | completed, 160 cps, sieve-verified |

Kill case detail (the headline): worker wP did 222 chunks with per-chunk
heartbeats, was killed at 07:29:53 UTC via `subagent.close` (no warning).
Heartbeats stopped; checkpoint count froze at 222. The supervisor —
running, no human — declared the handoff orphaned (detection budget ~7 s:
6 s lease TTL + 1 s scan). Standby sP recovered at attempt 1, resumed
from checkpoint 222 (sequence verified continuous, zero rework), worked
1378 more chunks, completed: 11029 primes, sum 22940748511, independently
sieve-verified CORRECT, acked.

Idle case detail: wY went silent after 25 chunks; supervisor orphaned it
with no human involved; s1 recovered and completed; wY's late probe was
rejected — the stale worker could not fork the work.

**v0.2 verdict: GO.** Automatic detection, automatic recovery, fencing,
and the no-worker safe-wait all proven with real kills. Failover is
seconds (budget ~7 s at defaults), not fractions of a second — the honest
target the survey established. No agent framework does any of this
(survey: LangGraph/CrewAI/AutoGen/OpenAI SDK/ADK all lack death detection
+ scheduled resume).
