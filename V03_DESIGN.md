# DHP v0.3 design — no single recovery agent

Target: the user's recursive doubt — *"what if the recovery component is
killed or idle?"* v0.2's supervisor is a single process; kill it and
orphans wait forever. v0.3 removes every single point of failure except
the storage substrate itself (and then replicates that too, as far as
the local prototype can).

## 1. Atomic ownership (CAS)

v0.2 has a TOCTOU race: two workers (or two supervisors) can both pass
the "is it orphaned?" check and both call `recover()`. Last-writer-wins
on the lease. Fix: atomic compare-and-swap on ownership.

- Local primitive: `O_CREAT|O_EXCL` lockfile
  `handoffs/<hid>/owner.<runner_id>.lock` — only one creator wins;
  losers get `OwnershipLost`.
- `claim`/`recover` become: write intent → atomic acquire → re-read →
  verify still orphaned/pending → commit. Standard CAS loop.
- Test: two simultaneous recoverers, exactly one wins (run N=50 races,
  assert single winner every time).

## 2. Replicated supervisors (leader election)

- N supervisor processes; leadership via a **supervisor lease row**
  (same lease mechanics as worker leases, shorter TTL).
- Only the leader scans; followers watch the leader's lease.
  Leader dies → lease expires → a follower self-promotes.
- This is the recursive answer: the watcher is watched by the same
  mechanism that watches workers. Turtles all the way down, one level.
- Test: kill worker AND active supervisor together → follower promotes
  (≤ lease TTL + scan) → orphan detected → standby recovers.

## 3. Transactional storage (SQLite WAL)

- Replace raw JSON files with SQLite (WAL mode): envelopes table,
  checkpoints table, outbox table.
- Atomic multi-row updates (no torn writes); crash-safe; single-writer
  via the CAS lock.
- Rationale: v0.2's files are fine for one host, but the supervisor
  restart and concurrent-recoverer tests need atomicity the filesystem
  doesn't give cheaply.
- Migration must preserve the v0.2 chaos corpus (replay envelopes into
  SQLite and verify identical).

## 4. Provisioning hook (no-worker → worker)

- Supervisor gets `provision(hid)`: default local implementation keeps
  a warm standby pool (N pre-spawned driver processes); hookable to
  external provisioners (K8s Job, process spawn).
- Answers "no agent to pick it up": the protocol maintains the reserve
  pool and queues durably when the pool is exhausted (backpressure:
  orphans wait, DLQ after max_attempts — never lost).
- Test: drain the pool to zero → orphan waits → pool refills → pickup.

## 5. Fencing tokens (stale worker returns)

- v0.2 fencing is holder-equality. v0.3 adds **monotonic fencing tokens**:
  each ownership grant mints `fence = prev + 1`; every mutation carries
  the token; storage rejects stale tokens. A "dead" worker that returns
  and tries to commit gets rejected even if it somehow still holds a
  lease reference (split-brain / partition healing).
- Test: partition a worker (stop its heartbeats but keep it running),
  let a standby take over, heal the partition, old worker tries to
  commit → rejected by fencing token.

## SLOs (v0.3 must measure, not assert)

- Detection latency: kill → orphan (target ≤ lease TTL + scan).
- Takeover latency: orphan → standby running (target ≤ CAS + spawn).
- Recomputed work: must be 0 (checkpoint continuity).
- Duplicate side effects: 0 (fencing-token rejections counted).
- Supervisor failover: leader death → new leader scanning.

## Explicit non-goals (still)

- Sub-second end-to-end (documented no-go; budget stays in seconds).
- Byzantine faults, multi-datacenter consensus (Raft/Paxos noted as the
  production answer; the local prototype demonstrates the mechanism
  with N processes + SQLite, not the geography).
- Trust/reputation of workers.

## Kill criteria for v0.3

- If atomic CAS can't be made race-free in the prototype → kill.
- If supervisor failover exceeds 3× the lease TTL in practice → narrow.
- If SQLite migration loses or corrupts any v0.2 envelope → stop, fix,
  don't proceed.
