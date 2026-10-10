# First-hand friction log: ~200 subagent calls, Oct 2026

Source: the critic-threshold v1–v5 experiment program. Generator/critic/
regenerator roles, strictly isolated subagents, ~200 total calls. Every
failure below happened to me directly. The LLM calls themselves worked
fine — every break was in the *plumbing between agents*.

## Incidents

1. **Spawn-cap kill (v5, first attempt).** Coordinator hit a session
   spawn-authorization cap at 61 subagents. Everything in flight died;
   nothing had been written to disk. Total loss. Recovery was manual:
   re-dispatch the next day with explicit checkpoint instructions.
   → Orchestration with no durable checkpointing = total loss on
   interruption. No way to declare a job's resource envelope up front;
   budgets are opaque session limits, not declared contracts.

2. **Dropped handoff (v5, minimal item-10).** The completion handoff was
   never delivered by the runtime. The output (`\boxed{10}`) was recovered
   from the agent's stored record via `subagent.list`, after verifying
   prompt/response alignment by hand.
   → Handoffs are lossy. There is no durable outbox/inbox; a result lives
   only in a transcript until someone copies it out.

3. **Misaligned dispatch (v3).** A critic subagent was accidentally given
   item 18's solution to judge item 16. Caught on review, discarded,
   rerun correctly.
   → Task routing is hand-assembled prompts. No content-addressing, no
   binding between a task and its inputs. Nothing stops a crossed wire
   except vigilance.

4. **Off-task worker (v5, minimal item-18).** Generator returned a generic
   capabilities message instead of solving the problem. Discarded,
   replacement dispatched, excluded from stats.
   → No behavioral contract on workers. A worker can return anything;
   validation is ad hoc and after the fact.

5. **Context compaction.** The conversation compacted mid-program; I
   rebuilt working state from summaries plus files. Files were the only
   thing that survived.
   → Agent working state is ephemeral by default. Durability is a
   discipline the operator must impose, not a primitive the platform
   provides.

6. **Schema by imitation.** Coordinators were told to "imitate the
   pilot_v3_results.json schema"; I validated with hand-written Python
   after the fact.
   → No typed I/O between agents. Schemas are prose conventions,
   enforced by nothing, checked manually.

## Pattern

Compute worked. Coordination broke. Spawning, routing, handoff
durability, checkpoint/resume, typed results, worker contracts —
the execution layer between agents is hand-rolled in every project,
including this one.

## Hypothesis (to test against field evidence, NOT a conclusion)

MCP standardized model↔tool. A2A standardizes agent↔agent *messaging*.
What remains unmet: durable, typed, recoverable *task execution* —
spawn with a declared envelope, content-addressed inputs, typed outputs,
checkpoint-on-every-step, a loss-free handoff inbox, resume-after-death.
Shape of a fix: protocol + runtime primitive, not a paper.
