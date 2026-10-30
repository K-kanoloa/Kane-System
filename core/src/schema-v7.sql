-- Phase3 changes recovery semantics, even though its metadata reuses the ledger.
-- Schema-6 readers do not enforce restore review / continuation fences. Refuse
-- such rollback rather than letting an old binary silently bypass those rules.
INSERT OR IGNORE INTO schema_version VALUES(7,unixepoch());
