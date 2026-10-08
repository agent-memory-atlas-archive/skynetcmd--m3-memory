-- 051_agent_liveness.up.sql
--
-- Record the agent's HOST and CLIENT PROCESS so team membership can be derived:
-- an agent is a member while it has touched the registry within the window OR
-- its client process is still alive on its node (decision c6aebe9e).
--
--   hostname         the machine name as reported, for people and diagnostics.
--                    node_id (048) stays the identity used for comparisons; the
--                    two differ when M3_NODE_ID is set (containers sharing a host).
--   pid              the agent's CLIENT process (Claude Code, Antigravity, ...),
--                    found by walking up past m3's own launch chain
--                    (m3_core.process.agent_client) — not the MCP server, which
--                    restarts on every reconnect.
--   pid_create_time  epoch seconds; pairs with pid so a reused PID is not
--                    mistaken for the agent (m3_core.process.pid_is_live_owner).
--
-- NOT BACKFILLED, following 047/048: NULL means "has not touched since the column
-- existed". A PID is only checkable on its own node, so a remote agent is judged
-- by its last touch alone.

ALTER TABLE agents ADD COLUMN hostname TEXT DEFAULT NULL;
ALTER TABLE agents ADD COLUMN pid INTEGER DEFAULT NULL;
ALTER TABLE agents ADD COLUMN pid_create_time REAL DEFAULT NULL;
