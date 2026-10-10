# Protocol Gap Analysis — AI interface/coordination protocols, Oct 2026

*Researched 2026-10-09 via web search. Claims below are sourced; marketing numbers
flagged where they come from vendor-adjacent writeups. Focus: what each protocol
standardized, real adoption, and what is STILL hand-rolled per project.*

---

## 1. MCP — Model Context Protocol (agent ↔ tools/data)

**Standardizes:** how one agent discovers and calls tools / reads resources:
`initialize` capability negotiation, `tools/list`, `tools/call`, `resources/read`
(JSON-RPC 2.0). Transports: stdio (local) and Streamable HTTP (remote).
2026-07-28 spec (largest revision since launch) made the protocol stateless,
replaced server-initiated requests with Multi Round Trip Requests (MRTR), and
added `elicitation` — a typed schema for a server to ask the human a question
mid-tool-call (accept / decline / cancel).

**Adoption reality:** the real deal. Anthropic donated it to the Linux
Foundation's Agentic AI Foundation (AAIF) in Dec 2025 (co-founded by Anthropic,
Block, OpenAI; backed by Google, Microsoft, AWS, Cloudflare). 97M+ monthly SDK
downloads (Mar 2026), 10,000+ public servers, adopted natively by Claude,
ChatGPT, VS Code Copilot, Cursor, Windsurf. 78% of enterprise AI teams report
≥1 MCP-backed agent in production (Apr 2026 survey). This one won.

**Biggest remaining gaps:**
- **Security / trust is unstandardized and actively bad.** MCP "cannot enforce
  these security principles at the protocol level — permissions belong to the
  host." Tool poisoning: MCPTox benchmark found >72.8% attack success on
  frontier models; ~5.5% of public MCP servers already contain poisoned
  metadata; 66% severely misconfigured. 40+ CVEs Jan–Apr 2026 (incl. a 9.6
  command-injection in `mcp-remote`). Knostic scanned 1,862 internet-exposed
  MCP servers: zero had any authentication. OAuth 2.1 was formalized in the
  Nov 2025 spec but the installed base mostly doesn't implement it.
  ([startupfortune](https://startupfortune.com/security-researchers-say-anthropics-mcp-protocol-puts-200000-servers-at-risk/),
  [forkast](https://forkast.news/glossary/mcp-security/),
  [securityboulevard](https://securityboulevard.com/2026/08/the-mcp-security-implementation-playbook-enterprise-authorization-patterns-that-actually-work/))
- **No official registry / discovery at scale; context exhaustion.**
  Static tool enumeration burns context windows at production scale ("selection
  collapse in large tool registries"); teams hand-roll private registries,
  allowlists, namespace isolation. The 2026 roadmap names an official registry
  as still-to-come.
  ([andrew.ooo](https://andrew.ooo/answers/mcp-model-context-protocol-enterprise-adoption-july-2026/))

---

## 2. A2A — Agent2Agent Protocol (agent ↔ agent)

**Standardizes:** cross-vendor agent discovery and delegation. Agent Card at
`/.well-known/agent-card.json` (capabilities/skills advertisement, signed in
v1.0 for verifiable identity), task lifecycle (submit → working →
input-required / completed / failed / canceled), streaming/push updates,
artifacts. Deliberately opaque: the caller gets task state and results, never
the callee's prompts, memory, or tools.

**Adoption reality:** real but earlier than MCP. Google announced Apr 2025,
donated to Linux Foundation Jun 2025, **v1.0 Mar 2026** (v1.0.1 May 2026),
joined AAIF Aug 2026. 150+ supporting organizations; production deployments in
Azure AI Foundry, Amazon Bedrock AgentCore, Google Cloud, Microsoft Copilot
Studio. The emerging division of labor: MCP = vertical (agent→tools), A2A =
horizontal (agent→agents you don't control).

**Biggest remaining gaps** (best single source is the May 2026 architecture
deep-dive):
- **No global registry / discovery beyond the card.** "The Agent Card solves
  discovery after the client has an endpoint. It does not answer how an agent
  finds all trustworthy peers in a large enterprise, how registry entries are
  approved, or how stale endpoints are removed."
- **No conflict resolution or transaction semantics.** "A planner may mark a
  subtask failed because its deadline expired while the remote agent later
  posts a completed artifact… A2A exposes state. It does not provide consensus
  or global transaction semantics."
- **Trust bootstrapping.** Signed cards prove origin/integrity, "but they do
  not prove that the agent is safe to call, that its declared skill quality
  is accurate, or that its output should be trusted. The trust policy itself
  remains outside A2A."
- **Deadlines, timeouts, escalation, and the human side of handoff are out
  of scope.** `input-required` signals a stall; rendering the request,
  deadlines, and routing to a human are the application's problem. (This is
  exactly the hole MA2H — see §6 — tries to fill.)
  ([turingpost](https://www.turingpost.com/p/a2a),
  [adamarant design guide](https://adamarant.com/en/blog/a2a-protocol-how-to-design-an-agent-api-other-agents-can-call),
  [Marcus Chen deep-dive](https://medium.com/@xitvali/agent-to-agent-protocol-architecture-communication-design-and-the-future-of-agentic-systems-2ef3dcd87d26))
- Field evidence of the sharp edges: a 2026 post-mortem of a four-agent
  LangChain pipeline over A2A entered a handoff loop and burned ~$47k in
  tokens before detection — no cycle detection or hop budgets in the protocol.
  ([dev.to](https://dev.to/alex_aslam/your-agents-cant-collaborate-if-they-only-speak-tool-calls-the-a2a-protocol-that-fixes-peer-4o0d))

---

## 3. Agent payments — AP2 / ACP / x402 / MPP / UCP

**Standardizes (each a different layer):**
- **AP2** (Google → FIDO Alliance, v0.2 Apr 2026): *authorization* — signed
  mandates/receipts (Verifiable Credentials / SD-JWT) proving a user authorized
  an agent's purchase. Rail-agnostic; moves no money.
- **ACP** (OpenAI + Stripe): *checkout rail* — agent↔merchant programmatic
  checkout; Shared Payment Token so the agent never sees card credentials.
  Powers ChatGPT Instant Checkout.
- **x402** (Coinbase + Cloudflare → x402 Foundation, Linux Foundation,
  operational Jul 2026): *settlement* — revives HTTP 402; agent signs a USDC
  transaction per request. 75–100M+ cumulative transactions on Base by 2026.
- **MPP** (Stripe + Tempo): *session payments* — spending limits and streamed
  micropayments without an on-chain tx per interaction; suited to metered agent
  workloads (data, compute).
- **UCP** (Google + retailers, Jan 2026): *commerce discovery* — catalog, cart,
  checkout, order management.

**Adoption reality:** fragmented but converging — all moved to open governance
in 2026, membership overlaps heavily (nobody expects one winner), and the
specs increasingly compose (AP2 documents an A2A/x402 extension). This seam is
the *most* standardized of the ones surveyed.

**Remaining gap:** dispute resolution and liability are out of scope
everywhere (AP2: "evidence only"; x402: none in spec); product-data quality
upstream is assumed by all and validated by none. But as a *protocol* problem,
payments are largely covered — do not reinvent here.
([dev.to comparison](https://dev.to/tomwangcn/ap2-vs-acp-vs-x402-who-holds-the-credential-4j8f),
[inriver guide](https://www.inriver.com/resources/ap2-mpp-x402-payment-protocols-agent-commerce/))

---

## 4. AGENTS.md (repo ↔ coding agent)

**Standardizes:** a markdown convention telling a coding agent how a repository
wants to be treated (build/test instructions, conventions). Donated by OpenAI
as a founding AAIF contribution alongside MCP.

**Adoption reality:** widespread as a *convention*, near-zero as a protocol —
it's a file format, not a wire standard. Useful, not infrastructure.

---

## 5. Durable execution — NO protocol exists (framework-only today)

**The landscape:** LangGraph checkpointers, Temporal (event-sourced replay),
DBOS (Postgres-backed steps), Inngest (step replay), Dapr Workflow, Restate.
All are platforms/libraries, none is a wire standard, and none interoperate:
a LangGraph checkpoint cannot be resumed by Temporal.

**Why it hurts (documented, not armchair):**
- LangGraph persists checkpoints but *nothing detects that the run died and
  nothing schedules a resume*; multi-replica coordination is DIY ("making that
  supervisor reliable is its own distributed-systems project").
- "A crash costs you whatever you did not wrap": LLM calls made outside a
  step/activity boundary are not saved anywhere — and that is where the money
  goes. Every framework has this boundary problem with different semantics.
- Two persistence systems for one run = two authorities for progress (Temporal
  history vs. DBOS/LangGraph checkpoints); the current guidance is just "pick
  one owner," hand-rolled per project.
  ([hackernoon comparison](https://hackernoon.com/durable-execution-for-ai-agents-langgraph-dbos-inngest-and-temporal-compared),
  [techbullion](https://techbullion.com/how-to-make-langgraph-and-crewai-agents-crash-proof-without-rewriting-them/),
  [dzone](https://dzone.com/articles/recoverable-langgraph-agent-temporal))

---

## 6. Human-in-the-loop — fragmented per framework

**Today:** LangGraph `interrupt()`, OpenAI `interruptions`, Anthropic
`PermissionRequest` hooks, MCP `elicitation` (2026-07-28, stateless via
MRTR), A2A `input-required` / `authorization-required` states. Each solves
HITL *within its own boundary*. MCP's spec is explicit that the server can
ask but only the host can decide; A2A explicitly leaves the human-hub layer
to the application.

**Early attempt at the missing layer:** **MA2H** (Multi-agent to Human
Protocol, autnmy, 2026) — a vendor-neutral hub with three verbs
(notify / ask / task) so a heterogeneous agent fleet shares one human-triage
inbox instead of N incompatible ones. Tiny, new, unproven — but it exists
precisely because the gap is real.
([dev.to approval gates](https://dev.to/corsairdev/how-to-add-approval-gates-to-mcp-server-actions-before-they-touch-production-data-kh2),
[MA2H](https://github.com/autnmy/ma2h-protocol),
[marktechpost layer analysis](https://www.marktechpost.com/2026/09/14/agent-harness-vs-agent-framework-vs-mcp-which-layer-owns-the-loop-state-tools-permissions-and-recovery/))

---

## 7. Audit / replay — no standard; regulation is forcing the issue

**Today:** point tools only (agentsense: MCP-proxy trace capture + replay/diff;
agentops-replay: hash-chained tamper-evident logs; vendor logs). MCP spec
"expects clients to log tool use" — a client log is not a tamper-evident
record. EU AI Act Article 12 enforcement (Aug 2026) mandates reconstructable
recordkeeping for high-risk systems; "operator-written logs do not meet this
bar." Survey data: 88% of enterprises had agent security incidents in the
prior year; only 21% had runtime visibility; 33% had no audit trail at all.
No standard format for signed, replayable agent-run evidence exists.
([agentsense](https://github.com/rahul06x1/agentsense),
[agentops-replay](https://github.com/jayprakash122003/agentops-replay))

---

## 8. Shared memory / cross-agent state — explicitly unfilled

"Both protocols move information. Neither keeps it." MCP defines nothing
about memory; A2A deliberately keeps agents opaque, so context built across
tasks survives only if something outside the protocol keeps it. A 2026
position paper names "cache sharing across agents and structured memory
access control" as the gap and multi-agent memory consistency "the most
pressing open challenge." Products (Mnemoverse, MCP memory servers) and
proposals (SAMEP) exist; no standard.
([dev.to](https://dev.to/izgorodin/do-i-need-a2a-if-my-agents-already-use-mcp-37im))

---

## Ranked: top 5 UNMET protocol needs

### 1. Durable agent execution as a wire protocol (suspend / resume / migrate a run anywhere)
- **Who feels it:** every team running agents longer than ~60s in production;
  anyone doing multi-step plans with side effects.
- **Why nothing covers it:** durability today is a *platform* choice
  (Temporal / DBOS / Inngest / Dapr / LangGraph), each with its own state
  format and none interoperable. There is no standard representation of "a
  suspended agent run" that any runner can pick up — no equivalent of what
  MCP did for tools. The boundary problem (LLM calls outside step boundaries
  re-execute and re-bill) is unsolved in every framework with different
  semantics each time. This is the closest thing to an MCP-shaped hole:
  everyone hand-rolls the same supervisor/resume logic per stack.

### 2. Cross-agent shared memory with access control
- **Who feels it:** builders of supervisor/worker, pipeline, and peer
  multi-agent systems; anyone whose agents must share more than one task's
  artifacts.
- **Why nothing covers it:** MCP has no memory semantics; A2A is opaque by
  design ("a shared task, not a shared history"). Products fill it
  proprietarily; the 2026 literature explicitly calls multi-agent memory
  consistency the most pressing open challenge. A standard memory-exchange
  layer (namespaced, permissioned, portable across frameworks) does not exist.

### 3. Trust, capability attestation, and discovery at scale for agents
- **Who feels it:** anyone doing cross-org or cross-vendor delegation; agent
  marketplace / ecosystem builders.
- **Why nothing covers it:** A2A v1.0 gives you signed Agent Cards — proof of
  *origin*, not of *quality, safety, or honesty of declared skills*. There is
  no global registry ("how an agent finds all trustworthy peers… how stale
  endpoints are removed" is unanswered), no reputation/attestation layer, no
  standard way to verify "this agent is good at what its card claims." The
  payment protocols solved money-trust; *work*-trust has no protocol.

### 4. Vendor-neutral human-in-the-loop coordination (one inbox for a heterogeneous fleet)
- **Who feels it:** enterprises running many agents from different
  frameworks under human supervision; platform teams building agent fleets.
- **Why nothing covers it:** every framework has its own interrupt primitive;
  MCP elicitation and A2A input-required are per-boundary signals, and both
  specs punt the human side to the application. Result: N incompatible
  approval inboxes. MA2H is the only neutral attempt and is brand-new/small.
  A standard notify/ask/task/delegate-back protocol with timeouts, escalation,
  and signed human decisions is missing.

### 5. Tamper-evident, replayable audit records for agent runs
- **Who feels it:** regulated industries (finance, healthcare), enterprise
  platform teams, anyone facing EU AI Act Art. 12 (enforced Aug 2026).
- **Why nothing covers it:** only point tools exist; each writes its own
  format, and operator-written logs fail the "don't trust the operator"
  requirement by construction. No standard envelope for signed, hash-chained,
  replayable run evidence that a regulator or counterparty can verify without
  trusting the runner. (Adjacent to #1: a durable-execution protocol would
  naturally emit these records; today nobody's records interoperate.)

---

## Explicitly NOT gaps (do not reinvent)
- **Agent→tool/data connectivity:** MCP won. Build on it.
- **Agent→agent messaging/delegation envelope:** A2A v1.0 covers the wire
  format; its gaps are registry, trust, durability, memory — not messaging.
- **Agent payments:** AP2 (authorization) + ACP (checkout) + x402/MPP
  (settlement) + UCP (commerce discovery) cover the layers; they compose.
  Remaining work is disputes/liability, which is legal, not protocol.
- **Model routing/fallback/caching:** AI gateways (Kong, LiteLLM, Portkey,
  Cloudflare, Azure, AWS) — crowded, solved enough.
- **Typed messages between agents:** A2A's JSON-RPC envelope + task objects
  cover the transport; contract versioning discipline is a practice gap, not
  a protocol hole.
