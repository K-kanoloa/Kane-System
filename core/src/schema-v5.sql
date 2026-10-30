-- Internal runtime ownership metadata; not another native object.
CREATE TABLE IF NOT EXISTS runtime_control(
 agent_id TEXT PRIMARY KEY REFERENCES agent(id),
 owner_token TEXT,
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0),
 phase TEXT NOT NULL DEFAULT 'idle' CHECK(phase IN ('idle','launching','running','cancelling','closed'))
);
-- Preserve existing lifetime launch consumption, even when no owner is alive.
INSERT OR IGNORE INTO runtime_control(agent_id,attempt)
 SELECT a.id,COALESCE((SELECT MAX(CAST(json_extract(l.payload_json,'$.detail.attempt') AS INTEGER))
 FROM ledger l WHERE l.object_id=a.id AND l.kind='runtime_lifecycle'
 AND json_type(l.payload_json,'$.detail.attempt')='integer'),0)
 FROM agent a WHERE EXISTS(SELECT 1 FROM ledger c WHERE c.object_id=a.id AND c.kind='runtime_configured');
INSERT OR IGNORE INTO schema_version VALUES(5,unixepoch());
