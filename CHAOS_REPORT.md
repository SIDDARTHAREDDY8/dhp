# DHP chaos test — real subagent workers through envelopes (2026-10-09)

**Question:** when a real worker is killed without warning mid-run, does the
protocol actually hold? Do fresh workers pick up orphans, resume from
checkpoints, and produce correct, schema-valid, acknowledged results?

## Setup

- Workload: `prime.census` — count/sum primes over a range, checkpointed per
  chunk. Deterministic, independently verifiable by sieve.
- 20 envelopes across 4 waves, executed by real subagent workers through
  `worker_cli.py` only (never touching envelope.json by hand).
- Chaos: genuine unannounced `subagent.close` kills mid-run (not simulated).

## Waves

| Wave | Envelopes | Size | Chunks | Outcome |
|---|---|---|---|---|
| 1 | 8 | 1,600 nos | 8 | 8/8 correct — too fast to kill; became a compliance test |
| 2 | 6 | 8,000 nos | 40 | 6/6 correct — still too fast (40 chunks in ~3 min) |
| 3 | 4 | 8,000 nos | 40 | 4/4 correct — 40 chunks in 75 s; kill window evaporated |
| 4 | 2 | 8,000 nos | 160 | **kill landed** — see below |

Waves 1–3 taught us the workers are far faster than expected (~5 chunks/s
at stride), so the kill had to land within ~60 s of spawn. Wave 4 used
160-chunk envelopes and a tight kill loop.

## The kill (wave 4, envelope 6a9939138c9a, range [209000, 217000))

- Worker claimed the envelope, checkpointed 37 chunks (through n=210850).
- **Killed without warning** via runtime close at 07:08:06Z, mid-run.
- Fresh recovery worker spawned; **`recover` claimed the orphan 15.4 s
  after the kill** (attempt 0 → 1).
- Recovery resumed from checkpoint 37 (`next_n=210850`) — **zero chunks
  recomputed**; 123 new checkpoints appended sequentially (37 → 160).
- `complete` validated the result against the output schema; supervisor
  acked. Independent sieve: **correct** (644 primes, sum 137113504).

## Incidents the protocol absorbed along the way

- A worker crashed its own driver *after* claiming (zero checkpoints);
  the retry hit `claim rejected: status is running` and fell back to the
  protocol-correct `recover` path — attempt incremented, no duplication.
- A worker used `recover` instead of `claim` on a fresh envelope; this
  exposed a verb-semantics hole — `recover` now rejects unclaimed handoffs.
- Workers never edited envelope state by hand in 20 envelopes; every state
  change went through the CLI.

## Verdict

20/20 envelopes correct, schema-valid, acknowledged, no crossed inputs.
One genuine mid-run kill → 15.4 s pickup → zero rework → correct result.
The five guarantees held under real worker death not just simulated death.
DHP v0.1 earns the name "protocol" for the single-runner, shared-filesystem
case. Open for v0.2: multi-runner races (two runners, one orphan), network
transport for the suspend/resume bundle, and the human-in-the-loop verbs.
