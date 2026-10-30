CREATE TABLE IF NOT EXISTS host_resource_policy(
 id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
 policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sandbox_reservation(
 agent_id TEXT PRIMARY KEY REFERENCES agent(id), task_id TEXT NOT NULL REFERENCES task(id),
 token TEXT NOT NULL, resources_json TEXT NOT NULL, held INTEGER NOT NULL CHECK(held IN (0,1)));
INSERT OR IGNORE INTO schema_version VALUES(3,unixepoch());
