-- Auxiliary contracts for existing Task / Context objects; no new native object.
CREATE TABLE IF NOT EXISTS collaboration (
 root TEXT PRIMARY KEY REFERENCES task(id), spec_json TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 0, max_agents INTEGER NOT NULL CHECK(max_agents>0),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
 token_limit INTEGER NOT NULL CHECK(token_limit>=0));
CREATE TABLE IF NOT EXISTS collaboration_node (
 task_id TEXT PRIMARY KEY REFERENCES task(id), root TEXT NOT NULL REFERENCES collaboration(root),
 agent_id TEXT NOT NULL REFERENCES agent(id), role TEXT NOT NULL,
 claimed INTEGER NOT NULL DEFAULT 0 CHECK(claimed IN(0,1)), generation INTEGER NOT NULL DEFAULT 0,
 UNIQUE(root,agent_id));
CREATE TABLE IF NOT EXISTS task_dependency (
 task_id TEXT NOT NULL REFERENCES collaboration_node(task_id), prerequisite TEXT NOT NULL REFERENCES task(id),
 PRIMARY KEY(task_id,prerequisite), CHECK(task_id<>prerequisite));
CREATE TABLE IF NOT EXISTS allocation_sample (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES task(id),
 scenario TEXT NOT NULL, environment TEXT NOT NULL, mode TEXT NOT NULL,
 metrics_json TEXT NOT NULL, evidence_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS context_share (
 context_id TEXT NOT NULL REFERENCES context(id), agent_id TEXT NOT NULL REFERENCES agent(id),
 version INTEGER NOT NULL, model_id TEXT NOT NULL REFERENCES model(id),
 ceiling TEXT NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER,
 PRIMARY KEY(context_id,agent_id));
CREATE TABLE IF NOT EXISTS context_reference (
 task_id TEXT NOT NULL REFERENCES task(id), context_id TEXT NOT NULL REFERENCES context(id),
 version INTEGER NOT NULL, agent_id TEXT NOT NULL REFERENCES agent(id),
 PRIMARY KEY(task_id,context_id));
CREATE INDEX IF NOT EXISTS collaboration_by_root ON collaboration_node(root);
INSERT OR IGNORE INTO schema_version(version,applied_at) VALUES(9,unixepoch());
CREATE TABLE IF NOT EXISTS collaboration_harness_profile(
 agent_id TEXT PRIMARY KEY REFERENCES agent(id), capabilities_json TEXT NOT NULL,
 evidence_hash TEXT NOT NULL, expires_at INTEGER NOT NULL);
