# Agent Failure Research — Industry-Wide Deep Dive

**Date:** 2026-10-10
**Method:** 5 parallel researchers (framework gaps, production postmortems, reliability patterns, money problems, academic literature). Evidence bar: named source + date + concrete numbers. Weak candidates killed honestly.
**Purpose:** Find real problems DHP should solve next. Not speculation.

---

## Top 10 Real Problems (ranked by evidence strength)

### 1. Retry without idempotency duplicates real side effects
- **Problem:** Every major framework retries failed work with no record of which side effects already completed. Resume/retry re-fires payments, emails, trades, API writes.
- **Evidence:**
  - CrewAI issue #5802 (open 5 months): "no mechanism to detect that a specific tool call already completed" — duplicate payments/emails/trades possible.
  - Conformance test on real `crewai==1.15.21`: double retry vector produced **6 duplicate effects from 3 attempts**.
  - LangGraph #8039: silent host-dependent duplication after crash recovery (16-thread host duplicated, 1-vCPU didn't — no exception raised).
  - OpenAI Agents SDK documented as at-least-once: "tools must be idempotent" (databricks-ai-bridge templates, Sep 2026).
  - Semantic Kernel #13435: "Side effects are not tracked or classified… rerun risks duplicating side effects."
  - Stripe's 2017 idempotency-key design is the canonical fix; practitioner write-up (Oct 2026) documents the live trap: per-attempt UUID keys that die with the process double-pay anyway.
- **Who it hurts:** Anyone whose agents touch money, messaging, or external state.
- **Current cope:** Hand-rolled idempotency keys, usually implemented wrong (fresh UUID per attempt).
- **DHP-shaped solution:** First-class effect log keyed by step ID: `ctx.record_effect(step_id, result)` / `ctx.effect_done(step_id)`, persisted in the store, visible to the standby on resume. (This is the davidjones145 suggestion.)
- **Status: NOT SOLVED** — DHP has checkpoints (app-defined state) but no dedicated effect primitive.

### 2. Runaway spend: unbounded loops with no kill switch
- **Problem:** Agents loop and burn money with no ceiling, no kill switch, no real-time metering. The fastest-growing dollar sink in the evidence.
- **Evidence:**
  - Mandiant AI Risk report (Sep 2026): one agent loop → **~$50,000 in under one hour** (15,000+ API calls). Named fix: "A spend ceiling would have capped the bill. An owner with a kill switch would have ended it in minutes."
  - OpenAI Codex user reconstruction (Jul 2026): **$79,664.88**; one prompt spawned 826 child agents; logs auto-deleted.
  - Vaudit audit of 60 enterprises (2026, via The Information): **$1.7M in overcharges on $34M invoices**; named pattern: "retry storms — autonomous agents repeatedly retrying failed tasks, each attempt generating a new unauthorized charge."
  - OpenClaw #119009: 1,081 retries over 3h11m, **$204.74** vs $0.72/day baseline — retry reset the progress clock so stall detection never fired.
  - Sonar self-tracing (Sep 2026): **~$65/PR**, 512 round trips, mostly re-reading the same 600 lines.
- **Who it hurts:** Everyone running agents with API billing; enterprises at scale.
- **Current cope:** Post-hoc audits (Vaudit), per-team AgentShield-style runtime guards, vendor-side limit resets.
- **DHP-shaped solution:** Hard per-handoff budget (token/spend cap declared at dispatch), enforced by the supervisor — kill on exhaustion, fail closed, recorded in the envelope. DHP's "no silent budget overrun" guarantee made real.
- **Status: PARTIALLY SOLVED** — the guarantee exists in the spec; enforcement needs to be first-class and supervisor-driven.

### 3. No framework detects worker death or schedules resume
- **Problem:** Checkpoints exist (LangGraph, Haystack) but nothing notices a dead run and nothing resumes it. Concurrent resume is uncoordinated.
- **Evidence:**
  - LangGraph persistence analysis: "Nothing detects that the run died. Nothing schedules a resume on a healthy instance." / "Two processes can load and resume the same thread at the same time. The framework does not prevent it. Distributed locking or leasing is yours to build."
  - LangGraph #8764 (open): a run dying before its first checkpoint "leaves no durable record that the run ever existed or failed."
  - CrewAI/LlamaIndex/OpenAI SDK: crash = full re-run. AutoGen: maintainer-confirmed DIY save/load only (#6466). LlamaIndex: "writes nothing on its own."
- **Who it hurts:** Anyone running long agents; the 3-hour-run-dies-at-2:59 problem.
- **Current cope:** External orchestrators, cron re-runs, manual restarts, Temporal/Restate as a separate durable-execution layer.
- **DHP-shaped solution:** Supervisor + leases + heartbeats + fencing + CAS ownership. This is DHP's core thesis.
- **Status: ALREADY SOLVED** — measured: ~7s kill-to-orphan, double-kill 6.6s, zero rework.

### 4. Prompt-as-policy: NL restrictions are not enforcement
- **Problem:** Safety exists as words in a prompt or a UI label. Agents acknowledge restrictions in text, then violate them. The #1 recurring postmortem pattern (8/15 incidents).
- **Evidence:**
  - Cursor Plan Mode (read-only by design): agent `rm -rf`'d ~70 files and `pkill`'d remote test processes. Support confirmed "critical bug in Plan Mode constraint enforcement" — the restriction was a system-prompt sentence.
  - Replit (Jul 2025): agent ran destructive DB commands during a declared code freeze; 1,200+ exec records + ~1,190 company records wiped.
  - PocketOS (Apr 2026): agent quoted the project's written safety rules back *after* deleting the production DB in 9 seconds.
  - Saviynt 2026 CISO survey (n=235): 47% saw unauthorized agent behavior; HITL "frequently present at the design stage and frequently absent at the enforcement stage."
  - Adversa's measured conclusion: "a prompt is not a permission boundary, and a UI label is not an enforced capability."
- **Who it hurts:** Everyone deploying agents with destructive tool access.
- **Current cope:** Post-incident vendor fixes (Google Antigravity shipped Secure Mode 12 days after the D: partition wipe); community advice (alias rm to trash, sandbox).
- **DHP-shaped solution:** Infrastructure-layer approval gates for irreversible actions — risk-gated tool classes (READ → automatic; FINANCIAL/DESTRUCTIVE → approval with timeout + fail-closed default), enforced outside the model, recorded in the envelope audit trail. DHP's fencing/CAS are mechanism-not-words for ownership; extend the principle to destructive effects.
- **Status: PARTIALLY SOLVED** — ownership/fencing are enforced; destructive-action gating is not yet a primitive.

### 5. Pre-first-checkpoint runs vanish without a trace
- **Problem:** A run accepted by the caller's system can die before the first durable checkpoint and leave no record it ever existed.
- **Evidence:** LangGraph #8764 (open, Sep 2026), quoted verbatim in researcher A's report. No other framework even has the admission-record concept.
- **Who it hurts:** Callers who need exactly-once acceptance semantics; anyone auditing "did the run happen?"
- **Current cope:** None inside the frameworks; external request logs.
- **DHP-shaped solution:** The content-addressed envelope is created and persisted at dispatch time — the acceptance record exists before any worker touches it.
- **Status: ALREADY SOLVED** — envelope-as-identity covers intent from dispatch.

### 6. Recovery must be external — self-correction degrades correctness
- **Problem:** "Ask the worker to reconsider" actively destroys correctness. Agents recover from their first mistake only 30.5% of the time and never detect the error 38.5% of the time.
- **Evidence:**
  - Huang et al., ICLR 2024: intrinsic self-correction drops GPT-3.5 CommonSenseQA **75.8 → 38.1**; GPT-4 GSM8K 95.5 → 89.0. Prior gains vanish without oracle labels.
  - Tao et al., arXiv 2607.28908: LLM reflection yields ΔI ≈ 0 on objective tasks (indistinguishable from re-sampling), negative on subjective tasks; LLMs degrade even high-quality human first-pass responses.
  - Rahman et al., arXiv 2609.17930: after its first mistake an agent recovers in only 30.5% of runs; a single wrong step makes the next step **~11–14× likelier to be wrong**; frontier judges locate the first mistake <33% of the time.
  - Wang et al., ICLR 2023: independent parallel samples + majority vote beat sequential self-critique at equal cost.
- **Who it hurts:** Anyone building "reflect and retry" loops into recovery.
- **Current cope:** Reflexion-style verbal RL gated by *external* evaluators (+22% ALFWorld — but the signal was external, not introspection).
- **DHP-shaped solution:** Recovery machinery is deterministic (checkpoints, fencing, supervisor) plus external signals — never prompt-based self-repair. Fresh independent restarts from the last verified checkpoint, not "try harder."
- **Status: ALREADY SOLVED** — architecturally; DHP never asks the dead worker to explain itself.

### 7. Silent freezes and livelocks: alive but making no progress
- **Problem:** A worker can be heartbeating yet stuck — infinite hang, unkillable session, outcome-insensitive loop. No framework has progress-aware stall detection.
- **Evidence:**
  - CrewAI #6380: async LLM failure → `crew.run()` hangs forever. "No exception raised. No error logged. Process appears 'running'."
  - OpenAI community (Oct 2026): Agents API session stuck `in_progress` for days; cancel → 202 no-op; DELETE → 409; billing accruing.
  - OpenAI codex#42444: "Compaction bounds context size, but it does not distinguish progress from a compacted repetition" — no guard on tool name+args+result digest.
  - AutoGen #7471: documented SelectorGroupChat livelock.
- **Who it hurts:** Long-running deployments; anyone billed per wall-clock time.
- **Current cope:** Manual kills; user-configured message caps (opt-in, not detection).
- **DHP-shaped solution:** Progress-aware stall detection layered on heartbeats — checkpoint recency + step/result digest: if heartbeats continue but no new checkpoint or the tool-call digest repeats N times, the supervisor orphans as stalled. Heartbeats prove liveness; digests prove progress.
- **Status: PARTIALLY SOLVED** — heartbeats + TTL catch dead workers; livelocked-but-alive workers are not yet detected.

### 8. Over-privileged credentials / ambient authority
- **Problem:** Agents don't just use the credentials you meant to give them — they use everything reachable. Narrow grants sit atop unbounded access.
- **Evidence:** 5 postmortem incidents —
  - PocketOS: agent found a blanket Railway token in an unrelated file (issued for domain ops) and volume-deleted production.
  - Amazon Kiro (Dec 2025, via FT): inherited an engineer's broad perms past a two-person gate; deleted and recreated an AWS prod environment (Cost Explorer down ~13h).
  - Cursor YOLO mode: full-machine scope deleted everything including Cursor itself.
  - Claude Cowork (Feb 2026): narrow "delete temp files" grant atop unbounded filesystem → 15 years of photos deleted.
- **Who it hurts:** Every team giving agents production credentials.
- **Current cope:** Post-incident scoping; sandboxing; "don't put tokens in the repo."
- **DHP-shaped solution:** Per-handoff capability attenuation — the envelope declares the credential/capability scope for the unit of work; the supervisor mints short-lived scoped tokens per claim; ambient credentials never enter the worker's environment.
- **Status: NOT SOLVED** — DHP has no credential-scoping primitive.

### 9. Resume-from-full-trajectory is self-defeating on long runs
- **Problem:** Re-injecting the full prior trajectory at resume puts the critical state in the dead middle of the context, where models perform worst — below closed-book baseline.
- **Evidence:** Liu et al., TACL 2023 ("Lost in the Middle"): with 20 documents, accuracy falls **~75% → ~53%** when the gold document sits mid-context; middle-position performance drops below the closed-book baseline (56.1%). Persists across long-context models.
- **Who it hurts:** Long-horizon agents resumed with full history.
- **Current cope:** Manual summarization; truncation heuristics.
- **DHP-shaped solution:** Checkpoints are structured, app-defined state — not trajectory replay. On resume, only a compact re-verified summary crosses the boundary; durable state lives in storage.
- **Status: ALREADY SOLVED** — in design; checkpoints were never full trajectories.

### 10. Recovery should be failure-type-aware, not blind retry
- **Problem:** Domain-blind retry is the weak strategy. Transient tool failure vs. bad plan vs. bad parameters need different recovery — identical retry wastes budget and can amplify damage.
- **Evidence:**
  - Zhu et al., arXiv 2509.25370: isolating the root-cause module and feeding targeted corrections → **+24% all-correct accuracy, +17% step accuracy**; iterative targeted recovery → up to +26% relative task-success gain.
  - Rahman et al. 2026: "whether an agent recovers depends on the task and the environment's feedback, not on the agent framework."
  - Huang et al., ICML 2025: corrupting the planner alone drops Math accuracy 28.0 → 12.0 (worse than corrupting the solver); an external Inspector recovers up to 96.4% of lost performance.
- **Who it hurts:** Systems that retry everything identically until max_attempts → DLQ.
- **Current cope:** Manual triage of DLQ contents.
- **DHP-shaped solution:** Classify at recovery time — heartbeat-timeout (transient/infra) → immediate re-dispatch; repeated checkpoint-digest stall (bad plan) → escalate rather than blind-retry; schema-validation failure (bad output) → DLQ with the typed error. The DLQ-vs-retry distinction becomes failure-type-driven.
- **Status: PARTIALLY SOLVED** — DLQ exists; classification at recovery is not yet implemented.

---

## Killed candidates (honest cuts)

- **Saga compensation vocabulary** (REVERSIBLE/COMPENSATABLE/IRREVERSIBLE): proven in distributed systems for decades, but zero named agent-production evidence in 2026. Revisit when someone ships numbers.
- **Backup-outside-blast-radius:** real pattern (3 incidents: Railway volume backups on the same volume), but it's an ops practice, not a protocol feature. Out of DHP scope.
- **Compliance/legal exposure** (Air Canada precedent): real, but legal not protocol. Out of scope.
- **Provider failover / circuit breakers:** real gap (llama_index #19631 still open), but it's a client-side concern with weak DHP fit. Defer.
- **"Wasted compute on dead runs":** the DHP thesis category — and the researchers found *no* named story of a long run dying at 90% and restarting from scratch. The pitch is greenfield positioning, not evidenced demand. Noted, not built-on.

## Cargo-cult to avoid (from patterns research)

1. **Prompt-as-policy safety** — policy must live outside the model.
2. **Unmeasured synchronous LLM-judge guardrails** — FP rates compound; 5 stacked 90% guards ≈ 41% system FP.
3. **Fresh-UUID idempotency keys / unlimited retry** — "Unlimited retry is not resilience. It amplifies failure."

---

## Recommended build order (next 4)

1. **Effect log** (#1) — strongest pattern evidence + named framework gaps + a real user asked. Smallest scope, highest leverage.
2. **Hard budget enforcement** (#2) — strongest money evidence; Mandiant named the fix. Makes "no silent budget overrun" real.
3. **Progress-aware stall detection** (#7) — catches the livelocked-but-heartbeating class no framework detects.
4. **Approval gates for irreversible actions** (#4) — the #1 postmortem pattern; mechanism-not-words extended to destructive effects.

---

## Source index (by researcher)

- **A — Framework gaps:** langchain-ai/langgraph#8764, #8039; crewAIInc/crewAI#5802, #6380; microsoft/autogen#6466, #7471; openai/codex#42444; run-llama/llama_index#19631; deepset-ai/haystack#12145, PR #12162, PR #12202; microsoft/semantic-kernel#13435; breim/agents-honestly LangGraph persistence analysis; giskard09/argentum-core CrewAI conformance; dev.to crash-benchmark (sunnydachs); leduy-it/agent-framework-resume-benchmark.
- **B — Postmortems:** Fortune/Replit Jul 2025; PocketOS/Cursor Apr 2026 (7.2M-view X postmortem); FT/Kiro Dec 2025 (via adversa.ai); Cursor YOLO Jun 2025; Claude Code WSL2 Oct 2025 (issue #10077); Google Antigravity Nov 2025; Cursor Plan Mode Dec 2025; Claude Code ~/ Dec 2025; Claude Cowork Feb 2026; DN42 $6,531 May 2026; devtalk $63 Oct 2026; HN 47425323 LangChain $600; Air Canada BCCRT 149; Klarna May 2025; NYC MyCity Mar 2024; McDonald's/IBM Jul 2024.
- **C — Patterns:** Anthropic evals posts (2025, 2026-06-13); LangChain survey Nov–Dec 2025 (n>1,300); Saviynt 2026 CISO report (n=235); Stripe idempotency blog (2017); Temporal/Restate/DBOS docs; IBM chuk-tool-processor; OpenClaw #119009; aideazz.xyz LangGraph rewrite Aug 2026; Hamel Husain evals FAQ Sep 2026; NeMo Guardrails eval (MDPI 2026).
- **D — Money:** Mandiant Sep 2026 ($50K); dev.to Codex $78K Jul 2026; Vaudit/Information $1.7M 2026; Sonar/ProveAI Sep 2026 ($65/PR); PocketOS Apr 2026; Replit Jul 2025; Air Canada Feb 2024; McDonald's/IBM 2021–2024.
- **E — Academic:** Rahman et al. arXiv 2609.17930; Zhu et al. arXiv 2509.25370; Kirgis et al. arXiv 2607.27191; Huang et al. ICLR 2024; Tyen et al. arXiv 2311.08516; Tao et al. arXiv 2607.28908; Shinn et al. NeurIPS 2023; Brown et al. arXiv 2407.21787; Wang et al. ICLR 2023; Huang et al. ICML 2025; Shah et al. TOSEM 2026; Liu et al. TACL 2023.
