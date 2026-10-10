# DHP production-gap audit (2026-10-09)

## CRITICAL (correctness / safety)

1. **Store is not thread-safe.** Single `self.cx` shared across threads
   with `check_same_thread=False`. SQLite connections are not safe for
   concurrent use. The chaos harness uses threads — this is a live bug.
   Fix: per-thread connections or a lock.
2. **No handoff-ID validation.** `hid[:12]` assumes str; MCP takes
   handoff_id from the network unvalidated. Malformed IDs crash or
   confuse. Fix: strict 64-hex validation at every entry point.
3. **No size limits.** Checkpoint state unbounded — a buggy/malicious
   agent can stuff GBs into a checkpoint (DoS). Fix: configurable
   max checkpoint/state/envelope bytes, enforced on write.
4. **Transport ingest trusts the log.** `ingest_log` does not
   `verify_id` the envelope and does not recompute checkpoint hashes.
   Tampered logs ingested silently. `runner3.resume` verifies; transport
   must too. Fix: verify on ingest, reject tampered lines.
5. **Transport `read_log` dies on one bad line.** `json.loads` with no
   handling — a partial line (crash mid-write) kills the whole ingest.
   Fix: skip-and-count malformed lines, verify hashes.
6. **MCP server: no input validation, no auth.** All inputs trusted.
   Fix: validate hid format, bound sizes; document auth as deployment
   concern (socket permissions), add optional token.

## HIGH (robustness / operability)

7. **No config.** Lease TTL, intervals, limits hardcoded. Fix:
   `DHPConfig` with env-var overrides (`DHP_LEASE_INTERVAL_S`, ...).
8. **No structured logging.** Fix: stdlib logging, correlation via
   handoff_id, levels.
9. **No graceful shutdown.** Supervisor loops forever; SIGTERM mid-scan.
   Fix: signal handling, finish current scan.
10. **No metrics/health.** Fix: counters (orphans, recoveries, fencing
    rejections, DLQ), `dhp_health` tool / status.
11. **jsonschema ImportError at complete-time.** Fix: fail fast at
    import/startup with a clear message.
12. **Schema migrations.** `CREATE TABLE IF NOT EXISTS` with no version.
    Fix: `schema_version` table, explicit migration check.

## MEDIUM (deferred, documented)

- `expired_leases` full-table JSON scan per second — needs status column
  or index at scale. Documented; fine for v1.0 scale.
- Transport `_append_line` open/close per checkpoint — batch in future.
- No fsync on transport log — OS-crash bound documented (≤1 checkpoint).
- `all_hids` loads all — paginate later.
- Old-leader partition: scan re-checks leadership each pass; acceptable.

## Out of scope (documented non-goals)

Multi-datacenter consensus, Byzantine faults, multi-writer SQLite
beyond WAL, authn/z beyond deployment guidance.
