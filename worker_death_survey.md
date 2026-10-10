# How production systems handle worker death, stall, and idle
*Evidence survey, October 2026. Concrete mechanisms and numbers, not marketing.*

## 1. Failure detection

### Erlang/OTP — link/monitor signals (immediate, in-VM)
Detection is a VM primitive, not polling: when a process dies, linked/monitoring processes receive an exit/DOWN signal effectively immediately (microseconds). There is no heartbeat interval to tune for local supervision. Restart intensity is bounded by `{intensity, period}`, default **1 restart per 5 seconds** — exceed it and the supervisor itself terminates, escalating up the tree. Key caveat: the supervisor restarts the *process*, not the *work*. GenServer state dies with the process unless externalized (ETS, Mnesia, external store).
- http://erlang.org/documentation/doc-5.6/pdf/design_principles.pdf (MaxR/MaxT mechanism)
- https://dev.to/matheuscamarques/building-agentic-systems-with-erlangelixir-and-otp-old-patterns-for-new-hypes-5gnh

### Temporal — heartbeats + timeouts (seconds to minutes)
Temporal's server **does not detect worker crashes directly**. From a practitioner's research notes: "The Temporal Server doesn't detect failures when a Worker loses communication with the Server or crashes. Therefore, the Temporal Server relies on the Start-To-Close Timeout to force Activity retries."
- **Heartbeats**: worker SDKs throttle heartbeat sends to min(0.8 × heartbeat_timeout, 30s default / 60s max). A dead worker is noticed when heartbeats stop arriving within `heartbeat_timeout`. One agent-workload setup used `heartbeat_timeout: 10s` and beat every `timeout/2`.
- Without heartbeats, detection = the full `start_to_close_timeout` (whatever you configured — minutes).
- Cancellation can *only* arrive via heartbeat responses; the server never pushes it.
- https://github.com/az-said/interlock/blob/HEAD/research/temporal-bar.md
- https://github.com/heaven999b/hello-agent-system/blob/HEAD/lessons/27_durable_workflows/README.en.md

### AWS SQS / Celery — visibility timeout (30s to 1h)
No liveness signal at all. A message received by a worker becomes invisible for the **visibility timeout**; if the worker doesn't delete it in time, it reappears for someone else. Detection latency = the timeout you configured:
- SQS default: **30 seconds** (max 12 hours).
- Celery over SQS (kombu): default **30 minutes**.
- Celery over Redis: default **1 hour** (Redis has no native ACK; Celery emulates with a timer + stored copy).
- Production configs seen: 600s, 3600s. Rule of thumb: set it above your longest task, or long tasks get duplicated *while still running*.
- Nasty Celery gotcha (documented): if the worker *process is terminated* (SIGKILL/OOM), Celery **acks the message anyway** even with `acks_late=True` — deliberately, to avoid infinite reruns of segfaulting tasks. You need `task_reject_on_worker_lost=True` to get redelivery on worker loss. And with default early-ack, a crashed task is simply **lost** (at-most-once).
- https://docs.celeryq.dev/en/v5.3.1/getting-started/backends-and-brokers/sqs.html
- https://dev.to/djangomastery/your-celery-task-will-run-twice-heres-how-to-make-that-safe-20mo
- https://medium.com/@k.pranav_22/your-celery-retries-are-quietly-double-billing-your-model-api-510ccbbd934e (Oct 2026: Celery+Redis double-running paid model-API jobs)

### Kubernetes — kubelet PLEG + probes (~30s typical)
- Crash detection: the kubelet's **PLEG** (Pod Lifecycle Event Generator) relists containers from the runtime event-driven — fast for hard crashes (seconds).
- Stall detection: **liveness probe**, defaults `periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3` → **~30s** to declare a deadlocked container dead, then kill + restart.
- Restart is throttled: CrashLoopBackOff **10s → 20s → 40s … capped at 5 min** (resets after 10 min healthy).
- **Jobs**: `backoffLimit` default **6**; failed pods recreated with backoff **10s/20s/40s… capped at 6 min**. The *pod* is replaced, not the *work item* — a Job pod restart reruns the whole job unless the application checkpoints externally.
- https://github.com/glapsfun/cnative-skills/blob/HEAD/plugins/kubernetes-operator/skills/kubernetes-operator/references/workloads-scheduling.md
- https://dev.to/bhagirath00/how-kubernetes-detects-and-restarts-crashing-pods-automatcially-588e

## 2. Who picks up orphaned work (and how long it really takes)

### Erlang/OTP — the supervisor, ~instantly
The linked supervisor receives the DOWN signal and respawns the child in microseconds. This is the fastest failover in the industry — but it only replaces the *process*. In-flight work is gone unless the design externalized it. The "let it crash" model assumes clean-slate restarts are safe.

### Temporal — the task queue, ~12s at best (measured)
When an activity times out (heartbeat or start-to-close), the server re-queues it per the retry policy; any live worker polling that task queue picks it up. **Measured** kill→resume on an agent workload (dev.to, Oct 2026):
- No heartbeats, startToCloseTimeout 3 min → **>180s** (sat the whole timeout)
- heartbeatTimeout 12s → **11.4–12.0s**
- heartbeatTimeout 5s → **11.9–12.5s** (tighter workflowTaskTimeout didn't help; the floor was heartbeat detection + retry scheduling)
- Retry policy in that setup: initialInterval 400ms, backoff ×2, max 5s, max 8 attempts.
- Important: re-execution is **from the beginning of the activity** — heartbeats can carry progress details so the *next attempt* resumes mid-way, but only if the activity code was written to do that. And re-execution ≠ safe effects: "Durable execution gets you re-execution. It does not get you don't-send-two-emails." Exactly-once *effects* need idempotency keys derived from durable state (run_id, step_index, action).
- https://dev.to/harishkotra/your-agent-is-a-long-running-process-with-side-effects-kill-it-and-watch-what-happens-2p0
- https://github.com/derpx06/webgenie/blob/HEAD/reports/research/05-runtime-and-platform.md (Diagrid critique, TianPan idempotency analysis)

### Celery + SQS/Redis — the broker, after visibility timeout
Orphaned task reappears when the visibility timeout expires and any worker picks it up. Real-world pickup latency = **timeout (30s–1h) + poll interval (~1s)**. No state is preserved — the task **restarts from zero**. With `task_reject_on_worker_lost`, a killed worker's task goes straight back to the queue (faster than waiting out the timeout).

### Kubernetes Jobs — the Job controller, minutes
Failed pod → controller creates a replacement pod with backoff (10s→…→6min cap), up to backoffLimit (default 6) then the Job is marked failed. Realistic kill→replacement: **tens of seconds for the first retry, minutes for later ones** — plus image pull and app startup. Again: whole-job restart unless the app checkpoints.

### Redis Sentinel (simplest possible failover, for calibration)
Measured: outage tracks `down-after-milliseconds` almost perfectly — **~2.5s at 1s, ~7.3s at 5s, ~12.1s at 10s**, plus a 1.5–2.3s promotion tax regardless. "The floor isn't zero." Tighter detection = more false failovers from GC pauses/network blips.
- https://dev.to/sunnysahijwani/how-long-does-a-redis-sentinel-failover-actually-take-i-measured-it-31ho

## 3. The "no worker available" story

This is the question the user asked hardest — *what if nobody picks it up?* The industry's converged answer:

- **Durable queues absorb it.** SQS, Temporal task queues, Celery+Redis: work waits safely with no workers alive. Nothing is lost; pickup latency becomes *scale-up latency*.
- **KEDA autoscaling from zero**: `pollingInterval` default **30s**, `cooldownPeriod` default **300s**. Cold start after idle: **10–30s** for simple containers (image pull + init); **~84s** cold / **~7s** warm for GPU inference stubs; **8–18 min** for real 7B model loads (measured May 2026, g4dn.xlarge). Guidance everywhere: keep `minReplicaCount: 1` for anything latency-sensitive.
- **Dead-letter queues**: SQS `maxReceiveCount` → DLQ after N failed receives — the standard poison-message escape hatch. Celery has no built-in DLQ; teams hand-roll one. After DLQ, work waits for a human or a replay job — it does *not* disappear.
- **Temporal with zero workers**: workflows survive indefinitely (durable timers, signals, event history in the DB). Activities sit in the task queue until a worker appears. This is the strongest "nobody home" story in the industry.
- **Erlang**: if the supervisor itself dies past its restart intensity, it escalates up the tree; if the top supervisor dies, the application dies. Multi-node/distributed Erlang is the answer — no single-box story.
- https://github.com/nitinjain999/platform-skills/blob/HEAD/references/keda.md
- https://github.com/tazmainiandevil/eks-gpu-inference-autoscaling (cold-start measurements)

## 4. Agent frameworks: what actually happens when a worker agent dies mid-run

- **LangGraph**: checkpoints persist (Postgres etc.), but **nothing detects that the run died and nothing schedules a resume** — recovery is manual. Worse (Diagrid critique): *"if two processes try to resume the same thread_id simultaneously… LangGraph has no built-in coordination."* Checkpointing is "I saved your state. You take it from here." `recursion_limit` default 25 is a loop cap, not crash recovery.
- **CrewAI**: persists only **completed tasks/flows**, not mid-ReAct iterations — a crash mid-task loses the task. Flows have `@persist` (SQLite) for state recovery across restarts. `max_iter` 20, `max_retry_limit` 2.
- **AutoGen/AG2**: **no built-in checkpointing at all** — Microsoft's own migration guide says persistence/recovery "must be implemented externally, often requiring complex state management." (Microsoft Agent Framework, the successor, adds FileCheckpointStorage.)
- **OpenAI Agents SDK**: `max_turns` default 5, input/output/tool guardrails — but guardrails are per-call tripwires with **no cumulative ledger** across the run, no crash resume, no cross-handoff state. A 200× `send_email` loop looks identical on call 1 and call 200.
- **Google ADK**: default `InMemorySessionService` **loses everything** on crash. `DatabaseSessionService` keeps conversation history but has **no mechanism to resume the workflow from step N** — `SequentialAgent` doesn't checkpoint between sub-agents; you re-run from step 1 (one analysis priced the re-execution waste at ~$1,500/yr for a modest claims workload). Crash-recovery checkpointing is an open feature request (adk-python #5485). The standard production answer is bolting on Temporal.
- https://github.com/derpx06/webgenie/blob/HEAD/reports/research/05-runtime-and-platform.md
- https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/
- https://github.com/jamjet-labs/jamjet.dev/blob/HEAD/src/content/blog/google-adk-vs-jamjet.md
- https://github.com/google/adk-python/issues/5485
- https://github.com/runcycles/cycles-docs/blob/HEAD/blog/openai-agents-sdk-has-guardrails-for-content-but-nothing-for-actions.md
- https://github.com/physercoe/termipod/blob/HEAD/docs/discussions/agent-execution-reliability-and-failure-caps.md (framework knob table)

## 5. Fast-failover reality check: can anyone do sub-second worker replacement?

**Short answer: for stateful LLM-agent work, no. The honest floor is seconds, not fractions of a second.**

Decompose kill→productive-replacement:
`detection + scheduling/cold-start + state reload`

- **Detection floor**: heartbeat_interval × missed_beats. Erlang in-VM: ~instant. Redis Sentinel measured: ~2.5s at 1s down-after (+1.5–2.3s promotion tax always). K8s liveness: ~30s. Temporal: ~heartbeat_timeout (5–12s tuned). SQS/Celery: visibility timeout (30s–1h). You can push heartbeats to ~1s, but every source stresses the false-positive trade-off: GC pauses and network blips cause false failovers, and for agents a false failover means a duplicate LLM run (double billing, double side effects).
- **Cold start**: 10–30s simple containers; minutes for model-loaded workers. Only hot standbys avoid this — which is a cost decision, not a protocol feature.
- **State reload**: replaying checkpoints / event history. Fast if checkpoints are small and local; slow across network.
- **LLM-specific**: providers aren't interchangeable (different tokenizers, tool schemas, rate limits), requests run seconds–minutes, and in-flight token state can't migrate. What the industry *does* do fast is **request-level** failover — gateways route to a backup provider in microseconds-to-ms on 429/5xx (Salesforce Agentforce: per-request soft failover + circuit breaker at 40% failures/60s, reset after 20 min). That is *not* worker replacement; the failed request restarts from zero.
- **Best measured stateful-agent number found**: Temporal at **~12s** kill→resume with aggressive heartbeats. Nobody credible publishes sub-second *stateful* agent failover; sub-millisecond numbers that exist (LLM gateway routing decisions at 11µs–1ms) are the routing decision only.

**What "fraction of a second" would actually require**: in-process or same-host supervision (Erlang-style, no network in the detection path) + pre-warmed standby workers + externalized small state. That is achievable architecturally — it is how telecom does it — but no agent framework ships it, and it costs you standby capacity.

## Bottom line

| Mechanism | Typical detection latency | Typical pickup latency | Zero workers alive |
|---|---|---|---|
| Erlang/OTP supervisor | ~instant (VM signal) | ~instant (respawn µs) | Escalates up tree; app dies without multi-node |
| Temporal (tuned heartbeats) | heartbeat_timeout (5–12s typical) | **~12s measured** kill→resume | Work waits safely in task queue + DB indefinitely |
| Temporal (no heartbeats) | start_to_close_timeout (minutes) | minutes | same as above |
| Celery + SQS | visibility timeout (30m default!) | 30m default; ~1s poll after | Work waits in queue; redelivery on timeout |
| Celery + Redis | visibility timeout (1h default) | up to 1h; immediate with task_reject_on_worker_lost | Work waits in Redis |
| K8s liveness + restart | ~30s (10s×3) | + container restart; CrashLoopBackOff 10s→5min | Pod stays down; Job retries ×6 then fails |
| K8s Job controller | pod failure (fast via PLEG) | 10s→6min backoff per retry | Job waits; fails after backoffLimit |
| KEDA scale-from-zero | 30s poll | 10–30s simple; 8–18 min GPU models | **This is the designed answer**: work queues, workers scale up |
| SQS DLQ | maxReceiveCount receives | human/replay job | waits in DLQ indefinitely |
| LangGraph/CrewAI/AutoGen/OpenAI SDK/ADK | **none built in** | **manual** | work is lost (LangGraph: checkpoint sits un-resumed; ADK: session gone) |

**Uncertainty flags**: Temporal's 10s "default" heartbeat timeout comes from a practitioner's agent setup, not a universal default — heartbeat timeout is per-activity config. Erlang "microsecond" restart is architectural (spawn cost), not a published benchmark I could cite. K8s numbers are well-documented defaults; production tuning varies widely. The ~12s Temporal measurement is one practitioner's rig (n=3 builds) — directionally solid, not a universal constant.
