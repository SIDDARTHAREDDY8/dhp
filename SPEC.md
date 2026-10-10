# DHP — Durable Handoff Protocol, v0.4 (working draft)

*Status: draft for prototyping, 2026-10-09. v0.4 makes failover
portable: the bundle is the transport, and any host can take over.*

## v0.4: portable distributed failover

### Transport log (`transport.py`)

Append-only JSON-lines: envelope header + per-checkpoint lines. The
worker dual-writes (local SQLite commit, then ship). The peer replays
with `INSERT OR IGNORE` — shipping is idempotent. In the prototype the
"network" is a directory; the *format* is the protocol. Failure
semantics are explicit and bounded:
- die between commit and ship → peer misses ≤1 checkpoint;
- die after ship → dedup by seq keeps it consistent;
- host destroyed entirely → peer ingests the log and resumes. No shared
  database. Tested: host A (worker + supervisor + disk) destroyed
  mid-run; host B ingested 190 shipped checkpoints and completed with
  zero rework.

### Conformance suite (`conform.py`)

10 runnable assertions any DHP implementation must pass: no lost
results, no crossed inputs, no untyped results, no silent budget
overrun, no runner lock-in, exactly-one-wins, fencing, leader
election, transport, dead-letter queue. 10/10 green on the reference
implementation.

### Fault-injection harness (`chaos.py`)

Randomized, not scripted: SIGKILLs random workers and supervisors at
random times; a provisioner auto-spawns recoveries. Invariants: every
handoff completes exactly once with a correct result, checkpoint
sequences stay continuous (zero rework), never two owners.

### Measured SLOs (reference implementation, single host)

| metric | budget | measured |
|---|---|---|
| detection: kill → orphan | lease TTL (6 s) + scan (1 s) | ~7 s (v0.2); 6.6 s with leader killed too (v0.3) |
| takeover: orphan → standby running | CAS + spawn | < 2 s (local spawn) |
| recomputed work | 0 | 0 in all chaos runs |
| duplicate side effects | 0 | 0 (fencing rejections counted, never committed) |
| supervisor failover | leadership TTL (5 s) + scan | ~6 s |
| cross-host rework bound | ≤1 checkpoint | 0 observed |
| industry best (Temporal, tuned) | — | ~12 s kill→resume |

Sub-second end-to-end remains a documented no-go for stateful
agent work (heartbeat floor + false-failover cost). DHP's claim is
not speed; it is that agent work becomes as un-losable as a database
write, *portably* — no vendor's checkpoint format required.

### Non-goals (still)

Byzantine faults; multi-datacenter consensus (Raft/Paxos noted);
trust/reputation.

---

## v0.3 (below, unchanged)

## v0.3: no single recovery agent

v0.2's supervisor was one process; kill it and orphans wait forever.
v0.3 replicates the watcher and hardens ownership.

### SQLite WAL store (`store.py`)

Envelopes, checkpoints, ownership, and leadership are tables, not files.
Crash-safe: `kill -9` on an active writer recovers via WAL
(`integrity_check: ok`, zero lost checkpoints — tested). The envelope
JSON stays the wire format; SQLite is the local durability substrate.

### Atomic CAS ownership

`claim`/`recover` win only via conditional INSERT/UPDATE on the
ownership row. Ten simultaneous recoverer threads → exactly one winner,
every time. No last-writer-wins.

### Fencing tokens

Monotonic `fence` per handoff, bumped on every ownership change
*including* orphaning (0 → 1 → 2 …). Mutations present the cached token;
stale tokens are rejected (`FencingError`) even if the holder string
matches. A dead worker that returns cannot commit across
orphan/recover cycles.

### Replicated supervisors, leader election

N supervisors share the store; leadership is a CAS row (5 s TTL). Only
the leader scans; followers watch the leader's lease. Leader death is
just another expired lease — the first follower to CAS wins and
promotes. Tested: `kill -9` on the leader → follower promoted → orphan
detected.

### Provisioning hook

Supervisor takes `provision(hid)`; the local operator is the
provisioner in tests, the hook is ready for a process pool.

### Non-goals (still)

Sub-second end-to-end (budget stays seconds); Byzantine faults;
multi-datacenter consensus (Raft/Paxos noted as the production answer).

---

## v0.2 (below, unchanged)

## v0.2: leases, heartbeats, supervisor

v0.1 proved a killed worker's checkpoints let a fresh worker resume with
zero rework. What it didn't do: *notice* the death. Detection was the
supervisor's eyeballs. v0.2 closes the loop with the industry-standard
answer — leases — informed by an evidence survey
(`worker_death_survey.md`):

- **No agent framework does this.** LangGraph, CrewAI, AutoGen, OpenAI
  Agents SDK, Google ADK: none detect worker death or schedule resume.
  The target is empty.
- **Fastest honest measured stateful failover: ~12 s** (Temporal, aggressive
  heartbeats). Sub-second worker replacement for LLM work exists nowhere
  credible — detection has a heartbeat floor, and false failovers
  double-bill (duplicate LLM runs, duplicate side effects).
- **"No worker available" is solved by durability, not speed**: work waits
  in the queue; autoscaling and dead-letter queues are the converged answer.
- **Celery's cautionary tales**: visibility timeouts measured in
  tens-of-minutes; early-ack *loses* crashed tasks; and re-execution is not
  safe effects — idempotency keys are required for side-effecting steps.

### Lease

`execution.lease = {holder, heartbeat_interval_s, max_missed,
last_heartbeat, expires_at}`. Set on `claim`/`recover`/`resume`; renewed
by `heartbeat`. Defaults: 2 s × 3 misses → **~6 s to detect**.
Aggressive tuning (0.5 s × 2 → ~1 s) is allowed but documented as carrying
false-failover risk. Death, stall, and idle are indistinguishable by
design — silence is the signal.

### Supervisor

`supervisor.scan()`: every running handoff past `expires_at` is declared
**orphaned** (checkpoints intact, lease cleared) and becomes recoverable;
past `max_attempts` (budget, default 3) it goes to **failed/DLQ** instead.
The supervisor never diagnoses — it only notices silence. Honest budget:
detection ≈ interval × max_missed + scan interval (~1 s) + standby pickup;
expect seconds, target ~5–10 s at defaults.

### Fencing

Only the current lease holder may checkpoint, step-count, heartbeat, or
complete. After expiry + reassignment, the old worker's calls raise
`LeaseLost` — an idle-but-not-dead worker learns it has been replaced
instead of forking the work. `worker_cli.py` takes a stable `--runner` id
for this reason.

### Kill vs crash

`release(hid, reason)`: cooperative fast path — a worker that knows it is
stopping requeues instantly instead of waiting out the lease (the
Celery `task_reject_on_worker_lost` lesson). Silent death takes the
lease-expiry path. Both end in the same recoverable state.

### Heartbeats belong to the fast layer (lesson, 2026-10-09)

First v0.2 chaos attempt false-orphaned healthy workers: an LLM doing
sequential CLI calls has 5–15 s action latency, far above the 6 s lease
TTL, so the supervisor declared them dead mid-work. The fix is
architectural, not tuning: **the heartbeat must come from the fast
execution layer, not the slow reasoning layer** — in production, the
framework heartbeats while the model reasons (cf. Temporal's SDK-managed
heartbeats). Lease TTL must still exceed the *observed* inter-heartbeat
interval with margin; the 2 s × 3 default assumes sub-second heartbeats.
Tuning rule: `TTL ≥ 3 × p99(heartbeat interval)`, else false failovers
duplicate LLM work (double billing, double side effects — the survey's
warning, reproduced live).

### Idempotent completion

`complete` on an already-completed handoff is a no-op (same idempotency
key). Re-execution is always possible; duplicate *effects* are the
application's idempotency keys, not the protocol's problem — stated, not
hand-waved (the Temporal lesson).

### Non-goals (still)

Sub-second failover (documented no-go for LLM work); multi-node supervisor
consensus (single supervisor; Erlang-escalation noted as future work);
trust/reputation; memory.

---

## v0.1 (below, unchanged)

## 1. Problem

Multi-agent systems lose work *between* agents. Handoffs are lossy prose,
in-flight state dies with the runner, results have no schema, budgets are
prompt-suggestions. Every framework re-solves this privately
(Temporal / DBOS / LangGraph / Inngest — mutually non-interoperable);
no wire format exists for "a suspended run." Field evidence: Gartner
1,445% surge in multi-agent inquiries; 37% hidden tax on multi-agent
bills; 11-day $47k runaway loop; "nothing detects that the run died and
nothing schedules a resume."

## 2. The primitive: the Handoff envelope

One unit of delegated work, self-contained: task + contract + provenance
+ execution state, in a single JSON document. Content-addressed:
`handoff_id = sha256(canonical(spec))` where spec = version, task,
contract, provenance, idempotency key. Identity covers *intent*, not
transient state — the execution section mutates freely while the id stays
stable; each checkpoint blob is individually content-addressed
(`state_hash`), keeping history tamper-evident per step.

## 3. Envelope fields (v0.1)

- `version`: `"dhp/0.1"`
- `task`:
  - `kind`: namespaced string, e.g. `"math.solve"`, `"code.review"`
  - `inputs`: map of name → `{"inline": value}` or `{"ref": uri, "sha256": hex}`
  - `input_hash`: sha256 over canonical inputs (runner MUST verify on dispatch)
- `contract`:
  - `output_schema`: JSON Schema (draft 2020-12) the result MUST validate against
  - `acceptance`: optional list of check ids (e.g. `"tests-pass"`)
  - `deadline`: RFC 3339 timestamp
  - `budget`: `{"max_steps": int, "max_spend_usd": float, "max_wallclock_s": float}`
- `provenance`: `parent_handoff_id` (nullable), `trace_id`, `created_by`, `created_at`
- `execution`:
  - `status`: `pending | running | suspended | completed | failed`
  - `attempt`: int (increments on resume)
  - `checkpoints`: list of `{"seq": int, "at": ts, "state_hash": hex, "state_uri": uri}`
  - `runner_id`: nullable string
- `delivery`: `idempotency_key`, `outbox`: `{"uri": str, "ack": bool}`

## 4. Operations (verbs)

- `dispatch(envelope) -> handoff_id`
  Runner MUST verify `input_hash` before accepting; mismatches are
  rejected, not run. (Kills the crossed-wire class: wrong inputs can never
  execute.)
- `checkpoint(id, state) -> seq`
  Append-only. State stored content-addressed at `state_uri`. MUST be
  durable before the runner takes its next billable step.
- `suspend(id) -> envelope`
  Serializes full execution state. Any DHP-conformant runner MUST be able
  to `resume` it — portability is the wire format's job, not the runner's.
- `resume(envelope) -> id`
  Continues from the last checkpoint; `attempt += 1`.
- `complete(id, result)`
  Runner validates `result` against `output_schema`; invalid results are
  REJECTED (never silently accepted). Valid results go to the durable
  outbox; the parent acknowledges → `delivery.outbox.ack = true`.
  At-least-once execution, idempotent completion: re-delivery of the same
  `idempotency_key` is a no-op.
- `fail(id, error, retryable)`
  Terminal, or requeue as `pending` with `attempt` preserved.

## 5. Guarantees (the point of the thing)

1. **No lost results** — durable outbox + acknowledgement (at-least-once,
   idempotent).
2. **No crossed inputs** — hash-bound inputs verified at dispatch.
3. **No untyped results** — schema validation at completion, enforced.
4. **No silent budget overrun** — the envelope's budget is enforced by the
   runtime, not suggested in a prompt.
5. **No runner lock-in** — suspend/resume is portable across any conformant
   runner.

## 6. Non-goals (v0.1)

- Not a messaging layer: A2A composes — carry the envelope as an A2A task payload.
- Not a tool protocol: MCP composes — tools are invoked *within* a task.
- Not trust/reputation, not a memory system, not a payment rail.

## 7. Wire format

JSON. Canonical form = sorted keys, no insignificant whitespace, UTF-8;
SHA-256 over the canonical bytes. Transport-agnostic (file, HTTP, queue):
the envelope does not care how it moves.

## 8. Prototype plan (this repo)

Python `dhp` package: `Envelope` (canonicalize/hash/verify), `LocalRunner`
(dispatch/checkpoint/suspend/resume/complete/fail over a directory
outbox+inbox), JSON-Schema validation, budget enforcement. Dogfood: run a
real fan-out job through it, kill a worker mid-run, resume from checkpoint
on a fresh runner, show zero work lost and every result schema-valid.
