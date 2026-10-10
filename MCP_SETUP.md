# Using DHP from MCP clients

`dhp-mcp` exposes DHP as 8 MCP tools. Any MCP-capable client —
Claude Desktop, Cursor, Windsurf, Zed, or your own agent loop —
can dispatch durable work, checkpoint it, and recover it after death.

## Install

```bash
pip install "dhp-protocol[mcp]"
```

## Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "dhp": {
      "command": "dhp-mcp",
      "args": ["--root", "/var/lib/dhp"],
      "env": {
        "DHP_LEASE_INTERVAL_S": "2.0",
        "DHP_LEASE_MAX_MISSED": "3"
      }
    }
  }
}
```

Restart Claude Desktop. The 8 `dhp_*` tools appear in the tool list.

## Cursor / Windsurf / Zed

Add to `.muse/mcp.json` (Cursor), `~/.codeium/windsurf/mcp_config.json`
(Windsurf), or the equivalent MCP settings file:

```json
{
  "mcpServers": {
    "dhp": {
      "command": "dhp-mcp",
      "args": ["--root", "/var/lib/dhp"]
    }
  }
}
```

## What the agent gets

| Tool | Purpose |
|---|---|
| `dhp_dispatch` | Create durable work (returns handoff ID) |
| `dhp_claim` | Claim a handoff as a worker |
| `dhp_checkpoint` | Save a resume point |
| `dhp_heartbeat` | Prove liveness (or use the SDK's auto-heartbeat) |
| `dhp_complete` | Finish with a schema-validated result |
| `dhp_recover` | Recover an orphaned handoff from its last checkpoint |
| `dhp_status` | Status, owner, checkpoint count |
| `dhp_last_checkpoint` | The resume point |
| `dhp_health` | Server + store health |

## Recommended agent prompt

Give your agent the skill at
[`skills/dhp-durable-work/SKILL.md`](https://github.com/SIDDARTHAREDDY8/dhp),
or this one-liner:

> For any task that takes minutes: dispatch it with `dhp_dispatch`,
> claim it, checkpoint every chunk, and if you die, the supervisor
> orphans your handoff in ~7s and any standby recovers it from your
> last checkpoint with zero rework.

## Running the supervisor

MCP tools don't replace the supervisor — something has to watch the
leases. Run it alongside (same `--root`):

```bash
# bare metal
dhp-supervisor /var/lib/dhp sup1 86400

# or docker (see docker-compose.yml)
docker compose up -d
```

Without a supervisor, dead handoffs wait instead of being orphaned —
still durable, just not automatic.
