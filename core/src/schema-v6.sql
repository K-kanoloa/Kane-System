-- Phase 2 internal metadata; ModelInstance remains the native object.
CREATE TABLE IF NOT EXISTS model_runtime(
 instance_id TEXT PRIMARY KEY REFERENCES model_instance(id),
 generation TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 endpoint TEXT NOT NULL, backend_model TEXT NOT NULL,
 context_tokens INTEGER NOT NULL CHECK(context_tokens>0),
 parallelism INTEGER NOT NULL CHECK(parallelism BETWEEN 1 AND 16),
 pid INTEGER, resources_json TEXT NOT NULL, held INTEGER NOT NULL CHECK(held IN (0,1)),
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))
);
CREATE TABLE IF NOT EXISTS model_task_priority(
 task_id TEXT PRIMARY KEY REFERENCES task(id), priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100)
);
CREATE TABLE IF NOT EXISTS model_gateway_call(
 call_id TEXT PRIMARY KEY REFERENCES model_queue(call_id),
 token TEXT NOT NULL UNIQUE, instance_id TEXT NOT NULL REFERENCES model_instance(id),
 generation TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('Prepared','Claimed','Completed','Unknown')),
 request_hash TEXT
);
INSERT OR IGNORE INTO schema_version VALUES(6,unixepoch());

-- Old Ready records did not carry any process/health proof. Never migrate them
-- into a verified backend merely because a field was previously called Ready.
INSERT INTO ledger(ts,kind,object_id,payload_json)
 SELECT unixepoch(),'model_legacy_unverified',id,'{"from":"Ready","to":"Loading","reason":"legacy registration is not runtime health proof"}'
 FROM model_instance WHERE state='Ready' AND NOT EXISTS(SELECT 1 FROM model_runtime r WHERE r.instance_id=model_instance.id);
UPDATE model_instance SET state='Loading' WHERE state='Ready' AND NOT EXISTS(SELECT 1 FROM model_runtime r WHERE r.instance_id=model_instance.id);
