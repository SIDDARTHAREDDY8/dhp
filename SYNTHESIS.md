# Synthesis — three legs, one target (2026-10-09)

## The three legs

1. **First-hand friction log** (`my_friction_log.md`): ~200 subagent calls.
   Every failure was plumbing between agents — spawn-cap kill with nothing
   on disk, a dropped handoff recovered from an agent record, a critic handed
   the wrong item's solution, an off-task worker, ephemeral state, schemas
   by imitation.
2. **Protocol gaps** (`protocol_gaps.md`): #1 unmet need is durable agent
   execution as a wire protocol. No standard "suspended run" representation;
   Temporal/DBOS/LangGraph/Inngest are platform-locked and non-interoperable.
   "Nothing detects that the run died and nothing schedules a resume."
   "A crash costs you whatever you did not wrap."
3. **Field bottlenecks** (`field_bottlenecks.md`): #1 is multi-agent handoff
   & orchestration state loss — "the most MCP-shaped opening," "pre-MCP
   moment," Gartner 1,445% inquiry surge, 37% hidden tax on multi-agent
   bills, an 11-day $47k runaway loop.

## Ranking (widely-felt × dissolvable-by-infrastructure × buildable-here)

| Candidate | Felt | Dissolvable | Buildable | Verdict |
|---|---|---|---|---|
| Durable, typed agent handoff | ★★★ | ★★★ (pre-MCP moment, convergent demand, no spec) | ★★★ | **THE PICK** |
| Benchmark trust (sealed eval) | ★★★ | ★★ (economic more than technical) | ★★ | adjacent; not the pick |
| Memory/context | ★★★ | ★★ | ★ (most crowded space) | pass |
| MCP 2nd-order gaps | ★★ | ★★ ("contribute to the standard") | ★ (window owned by labs) | pass |
| Inference cost / data wall | ★★★ | ★ (not protocol-shaped) | ★ | pass |
| Safety core | ★★★ | ★ (research-hard core) | ★★ | pass |

## The pick

A **durable handoff protocol**: a wire format for the execution state of
delegated agent work — typed, content-addressed, checkpointed, budgeted,
portable across runners. MCP standardized agent→tool. A2A standardized
agent→agent messaging. Nothing standardizes what happens when the *work
itself* dies halfway. That is the hole, and it is infrastructure-shaped:
a primitive + protocol, not a paper.

Working name: **DHP — Durable Handoff Protocol**. Spec: `SPEC.md`.
