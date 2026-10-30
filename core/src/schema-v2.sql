-- Additive migration: old binaries refuse user_version=2 rather than ignore holds.
CREATE TABLE IF NOT EXISTS model_reservation(
 call_id TEXT PRIMARY KEY,
 turn_id TEXT NOT NULL REFERENCES harness_binding(turn_id),
 task_id TEXT NOT NULL REFERENCES task(id),
 budget_id TEXT NOT NULL REFERENCES resource_budget(id),
 runtime_session_id TEXT NOT NULL REFERENCES session(id),
 reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens>0),
 actual_tokens INTEGER CHECK(actual_tokens>=0),
 state TEXT NOT NULL CHECK(state IN ('Reserved','Unknown','Settled','Overrun')),
 receipt_json TEXT,
 CHECK((state IN ('Reserved','Unknown') AND actual_tokens IS NULL) OR (state IN ('Settled','Overrun') AND actual_tokens IS NOT NULL)));
CREATE INDEX IF NOT EXISTS model_reservation_budget ON model_reservation(budget_id,state);
INSERT OR IGNORE INTO schema_version VALUES(2,unixepoch());
