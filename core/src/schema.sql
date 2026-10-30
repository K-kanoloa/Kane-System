CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
INSERT OR IGNORE INTO schema_version VALUES(1,unixepoch());
CREATE TABLE IF NOT EXISTS model(id TEXT PRIMARY KEY, spec_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_instance(
 id TEXT PRIMARY KEY,model_id TEXT NOT NULL REFERENCES model(id),node TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_budget(
 id TEXT PRIMARY KEY,token_limit INTEGER NOT NULL CHECK(token_limit>=0),used_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_tokens>=0 AND used_tokens<=token_limit),
 max_concurrency INTEGER NOT NULL CHECK(max_concurrency>0));
CREATE TABLE IF NOT EXISTS workspace(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0),
 lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),holder TEXT,expires_at INTEGER);
CREATE TABLE IF NOT EXISTS task(
 id TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ('Pending','Ready','Running','Blocked','Verifying','Done','Failed','Cancelled','ClosedUnverified')),
 verification_state TEXT NOT NULL DEFAULT 'pending' CHECK(verification_state IN ('pending','passed','failed','unverified')),
 block_reason TEXT, version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0),
 budget_id TEXT NOT NULL REFERENCES resource_budget(id),workspace_id TEXT NOT NULL REFERENCES workspace(id),
 workload_class TEXT NOT NULL CHECK(workload_class IN ('inference','generation','execution','training','realtime')),spec_json TEXT NOT NULL,
 CHECK(state!='Done' OR verification_state='passed'));
CREATE TABLE IF NOT EXISTS agent(
 id TEXT PRIMARY KEY,role TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES task(id),
 workspace_id TEXT NOT NULL REFERENCES workspace(id),budget_id TEXT NOT NULL REFERENCES resource_budget(id),
 model_id TEXT NOT NULL REFERENCES model(id),state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS session(
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agent(id),task_id TEXT NOT NULL REFERENCES task(id),
 runtime_ref TEXT,started_at INTEGER NOT NULL,ended_at INTEGER);
CREATE TABLE IF NOT EXISTS artifact(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),producer_id TEXT NOT NULL REFERENCES agent(id),
 format TEXT NOT NULL,editable INTEGER NOT NULL CHECK(editable IN (0,1)),content TEXT NOT NULL,content_hash TEXT NOT NULL,
 verification_state TEXT NOT NULL DEFAULT 'pending' CHECK(verification_state IN ('pending','passed','failed','unverified')));
CREATE TABLE IF NOT EXISTS context(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),sensitivity TEXT NOT NULL,trust TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 0,expires_at INTEGER,spec_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capability(
 id TEXT PRIMARY KEY,subject TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,
 expires_at INTEGER NOT NULL,revoked_at INTEGER,granted_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_object ON ledger(object_id,seq);
CREATE INDEX IF NOT EXISTS agent_task ON agent(task_id);
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),agent_id TEXT NOT NULL REFERENCES agent(id),
 idempotency_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('Pending','Dispatching','Confirmed','Unknown','NotExecuted')),
 created_at INTEGER NOT NULL,result_json TEXT);
CREATE TABLE IF NOT EXISTS reconciliation(
 id TEXT PRIMARY KEY,outbox_id TEXT NOT NULL REFERENCES outbox(id),status TEXT NOT NULL,evidence_ref TEXT);
CREATE TABLE IF NOT EXISTS workspace_file(
 workspace_id TEXT NOT NULL REFERENCES workspace(id),path TEXT NOT NULL,content BLOB NOT NULL,
 PRIMARY KEY(workspace_id,path));
CREATE TABLE IF NOT EXISTS verification(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES task(id),task_version INTEGER NOT NULL,
 verifier TEXT NOT NULL,passed INTEGER NOT NULL CHECK(passed IN (0,1)),evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harness_binding(
 turn_id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE REFERENCES task(id),
 agent_id TEXT NOT NULL REFERENCES agent(id),harness_agent_ref TEXT NOT NULL,
 native_session_ref TEXT);
CREATE TABLE IF NOT EXISTS harness_receipt(
 message_id TEXT PRIMARY KEY,turn_id TEXT NOT NULL REFERENCES harness_binding(turn_id),
 content_hash TEXT NOT NULL,artifact_id TEXT NOT NULL REFERENCES artifact(id));

