-- Owner-selected Mock route only. One shared in-flight slot per logical model.
CREATE TABLE IF NOT EXISTS model_queue(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 call_id TEXT NOT NULL UNIQUE,
 model_id TEXT NOT NULL REFERENCES model(id),
 task_id TEXT NOT NULL REFERENCES task(id),
 agent_id TEXT NOT NULL REFERENCES agent(id),
 runtime_session_id TEXT NOT NULL REFERENCES session(id),
 binding_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('Queued','Dispatched','Completed','Denied','Cancelled','Unknown')),
 enqueued_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
 dispatched_ms INTEGER, finished_ms INTEGER
);
CREATE INDEX IF NOT EXISTS model_queue_route ON model_queue(model_id,state,seq);
INSERT OR IGNORE INTO schema_version VALUES(4,unixepoch());
