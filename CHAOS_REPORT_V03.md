# DHP v0.3 — chaos report: no single recovery agent

2026-10-09. v0.2 proved automatic detection; v0.3 removes the last
single point of failure — the supervisor itself.

## What v0.3 added

- **SQLite WAL store** (`store.py`): envelopes, checkpoints, ownership,
  and leadership as tables. Crash-safe; `kill -9` mid-write recovers
  (integrity_check: ok after violently killing the leader supervisor).
- **Atomic CAS ownership**: `claim`/`recover` win only via conditional
  INSERT/UPDATE. Self-test: 10 simultaneous recoverer threads →
  exactly one winner, every time.
- **Fencing tokens**: monotonic `fence` per handoff, bumped on every
  ownership change (including orphaning). Mutations present the cached
  token; stale tokens rejected (`FencingError`) even if the holder
  string matches.
- **Replicated supervisors + leader election**: N supervisors share the
  store; leadership is a CAS row (5 s TTL). Only the leader scans.
  Leader dies → a follower promotes. The watcher is watched by the same
  lease mechanism that watches workers.
- **Provisioning hook**: supervisor takes `provision(hid)`; the local
  operator (this test's human) is the provisioner, the hook is ready
  for a process pool.

## Chaos: the double kill

The user's recursive doubt, tested literally — kill the worker AND the
active supervisor at the same instant:

- Worker w3: 185 checkpoints, heartbeats flowing, pace 0.4 s/chunk.
- Leader supervisor supB: scanning every 1 s, leadership TTL 5 s.
- **07:45:30 UTC: both killed** — `subagent.close` on the worker,
  `kill -9` on the supervisor. No warning.
- Follower supA: noticed supB's leadership lease expire, **promoted
  itself**, scanned, found w3's expired worker lease, **orphaned the
  handoff** — fence 0 → 1, 190 checkpoints intact.
- **Double-kill → orphan: 6.6 seconds.** Killing the recovery component
  added ~1 s to the v0.2 detection budget, not minutes.
- Standby s3: recovered via CAS (attempt 1), resumed from checkpoint
  190 — sequence verified continuous, zero rework — working the
  remaining ~1410 chunks. [final below]

## What this proves

1. There is no single recovery agent. The supervisor is replicated;
   leadership is leased; death of the leader is just another expired
   lease.
2. Exactly-one-wins: CAS ownership, not last-writer-wins.
3. Fencing is monotonic: a dead worker that returns cannot commit,
   even across orphan/recover cycles.
4. Storage survives violent death: `kill -9` on an active writer →
   WAL recovery → integrity ok, zero lost checkpoints.

## Numbers (final)

| test | result |
|---|---|
| 10-thread recover race | exactly 1 winner |
| stale fence after orphan | `FencingError`, rejected |
| leader election (2 sups) | exactly 1 leader |
| leader `kill -9` | follower promoted |
| double kill (worker + leader) | orphan in 6.6 s, 190/190 checkpoints kept |
| storage `kill -9` mid-write | integrity ok |
| s3 recovery | completed: 1600/1600 cps, 10696 primes, sieve-verified CORRECT, acked |
