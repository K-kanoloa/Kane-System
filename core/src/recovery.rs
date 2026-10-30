//! Owner-evidenced recovery. Restoring local bytes is never permission to replay.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

fn evidence(value: &Value) -> Result<()> {
    if value["source_ref"]
        .as_str()
        .is_none_or(|s| s.trim().is_empty() || s.len() > 4096)
        || value["sha256"]
            .as_str()
            .is_none_or(|s| s.len() != 64 || !s.bytes().all(|c| c.is_ascii_hexdigit()))
    {
        return Err(Error::Invalid(
            "bounded source reference and evidence SHA256 required".into(),
        ));
    }
    Ok(())
}
pub(crate) fn review_required(db: &rusqlite::Connection, task: &str) -> Result<bool> {
    Ok(db.query_row("SELECT EXISTS(SELECT 1 FROM ledger q WHERE q.object_id=?1 AND q.kind='restore_quarantine' AND q.seq>COALESCE((SELECT MAX(v.seq) FROM ledger v WHERE v.object_id=q.object_id AND v.kind='restore_owner_review'),0))",[task],|r|r.get(0))?)
}
impl Registry {
    pub fn recovery_queue(&self, actor: &Actor) -> Result<Value> {
        owner(actor)?;
        let mut actions = self.db.prepare("SELECT id,task_id,state,payload_json,result_json FROM outbox WHERE state IN ('Pending','Dispatching','Unknown') ORDER BY created_at,id")?;
        let tools = actions.query_map([], |r| Ok(json!({"id":r.get::<_,String>(0)?,"task_id":r.get::<_,String>(1)?,"state":r.get::<_,String>(2)?,"request":serde_json::from_str::<Value>(&r.get::<_,String>(3)?).map_err(|e|rusqlite::Error::FromSqlConversionFailure(3,rusqlite::types::Type::Text,Box::new(e)))?,"receipt":r.get::<_,Option<String>>(4)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let mut q=self.db.prepare("SELECT r.call_id,r.task_id,r.state,r.reserved_tokens,r.receipt_json,q.model_id,q.state FROM model_reservation r LEFT JOIN model_queue q ON q.call_id=r.call_id WHERE r.state IN ('Reserved','Unknown','Overrun') ORDER BY r.call_id")?;
        let models=q.query_map([], |r|Ok(json!({"call_id":r.get::<_,String>(0)?,"task_id":r.get::<_,String>(1)?,"state":r.get::<_,String>(2)?,"reserved_tokens":r.get::<_,u64>(3)?,"receipt":r.get::<_,Option<String>>(4)?,"model_id":r.get::<_,Option<String>>(5)?,"queue_state":r.get::<_,Option<String>>(6)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(json!({"tools":tools,"models":models,"automatic_replay":false,"automatic_refund":false}))
    }

    /// Explicit trusted Owner evidence, not an Agent receipt or automatic retry.
    /// The Owner must actually check the referenced receipt/target; a hash alone
    /// does not prove an arbitrary remote target's result.
    pub fn reconcile_model_call(
        &mut self,
        actor: &Actor,
        call: &str,
        result: Value,
        proof: Value,
    ) -> Result<Value> {
        owner(actor)?;
        evidence(&proof)?;
        if result["call_id"] != call
            || !matches!(
                result["outcome"].as_str(),
                Some("Confirmed" | "NotExecuted")
            )
        {
            return Err(Error::Invalid(
                "bound reconciliation outcome required".into(),
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (task, reserved, state): (String, u64, String) = tx.query_row(
            "SELECT task_id,reserved_tokens,state FROM model_reservation WHERE call_id=?1",
            [call],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        let previous:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='model_owner_reconciliation' ORDER BY seq DESC LIMIT 1",[call],|r|r.get(0)).optional()?;
        let payload = json!({"result":result,"evidence":proof,"authorized_by":"Owner","automatic_retry":false});
        if let Some(previous) = previous {
            if decode::<Value>(&previous)? != payload {
                return Err(Error::Conflict);
            }
            return Ok(json!({"call_id":call,"state":state,"idempotent":true}));
        }
        if state != "Unknown" {
            return Err(Error::Reconciliation);
        }
        let binding:Option<(String,String)>=tx.query_row("SELECT generation,COALESCE(request_hash,'') FROM model_gateway_call WHERE call_id=?1",[call],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if let Some((generation, hash)) = binding
            && (result["generation"] != generation || result["request_hash"] != hash)
        {
            return Err(Error::Denied(
                "receipt route/generation/request binding".into(),
            ));
        }
        let actual = if result["outcome"] == "NotExecuted" {
            if !result["usage"].is_null() {
                return Err(Error::Invalid("NotExecuted must not contain usage".into()));
            }
            0
        } else {
            let input = result["usage"]["inputTokens"]
                .as_u64()
                .ok_or_else(|| Error::Invalid("input usage".into()))?;
            let output = result["usage"]["outputTokens"]
                .as_u64()
                .ok_or_else(|| Error::Invalid("output usage".into()))?;
            let total = result["usage"]["totalTokens"]
                .as_u64()
                .ok_or_else(|| Error::Invalid("total usage".into()))?;
            if input.checked_add(output) != Some(total) || total > reserved {
                return Err(Error::Invalid(
                    "unbounded/inconsistent reconciliation usage".into(),
                ));
            }
            total
        };
        tx.execute("UPDATE model_reservation SET state='Settled',actual_tokens=?1 WHERE call_id=?2 AND state='Unknown'",params![actual,call])?;
        tx.execute(
            "UPDATE model_queue SET state=?1,finished_ms=?2 WHERE call_id=?3 AND state='Unknown'",
            params![
                if result["outcome"] == "NotExecuted" {
                    "Cancelled"
                } else {
                    "Completed"
                },
                now() * 1000,
                call
            ],
        )?;
        // Terminalize the old permit; it can never be claimed again.
        tx.execute(
            "UPDATE model_gateway_call SET state='Completed' WHERE call_id=?1 AND state='Unknown'",
            [call],
        )?;
        event(&tx, "model_owner_reconciliation", call, payload)?;
        event(
            &tx,
            "reconcile",
            &task,
            json!({"call_id":call,"outcome":result["outcome"],"authorized_by":"Owner","task_automatically_resumed":false}),
        )?;
        tx.commit()?;
        Ok(
            json!({"call_id":call,"state":"Settled","actual_tokens":actual,"task_automatically_resumed":false}),
        )
    }

    /// Only a trusted offline coordinator may attest physical quiescence. Core
    /// still checks durable lifecycle/resource state and every declared mapping.
    pub fn checkpoint_bindings(&self, actor: &Actor) -> Result<Value> {
        owner(actor)?;
        let active:bool=self.db.query_row("SELECT EXISTS(SELECT 1 FROM runtime_control WHERE phase IN ('launching','running','cancelling')) OR EXISTS(SELECT 1 FROM sandbox_reservation WHERE held=1) OR EXISTS(SELECT 1 FROM model_runtime WHERE held=1) OR EXISTS(SELECT 1 FROM agent WHERE state IN ('Running','Cancelling')) OR EXISTS(SELECT 1 FROM session WHERE ended_at IS NULL)",[],|r|r.get(0))?;
        if active {
            return Err(Error::Denied(
                "stop and reconcile managed runtimes/models before checkpoint".into(),
            ));
        }
        let mut q = self.db.prepare("SELECT id FROM agent ORDER BY id")?;
        let ids = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let mut agents = vec![];
        for id in ids {
            agents.push(self.runtime_status(&id)?);
        }
        let cut: i64 = self
            .db
            .query_row("SELECT COALESCE(MAX(seq),0) FROM ledger", [], |r| r.get(0))?;
        Ok(
            json!({"agents":agents,"ledger_cut":cut,"schema":self.db.pragma_query_value::<i64,_>(None,"user_version",|r|r.get(0))?}),
        )
    }
    pub fn record_checkpoint(&mut self, actor: &Actor, manifest: &Value) -> Result<()> {
        owner(actor)?;
        let bindings = self.checkpoint_bindings(actor)?;
        if manifest["bindings"] != bindings || manifest["id"].as_str().is_none_or(str::is_empty) {
            return Err(Error::Conflict);
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let cut: i64 = tx.query_row("SELECT COALESCE(MAX(seq),0) FROM ledger", [], |r| r.get(0))?;
        if manifest["bindings"]["ledger_cut"] != cut {
            return Err(Error::Conflict);
        }
        event(
            &tx,
            "checkpoint",
            manifest["id"].as_str().unwrap(),
            json!({"manifest_sha256":digest(&serde_json::to_vec(manifest)?),"ledger_cut":cut,"scope":"quiescent registry and declared native roots","publication_is_separate":true}),
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn validate_checkpoint(&self, actor: &Actor, manifest: &Value) -> Result<()> {
        owner(actor)?;
        let raw: String = self.db.query_row(
            "SELECT payload_json FROM ledger WHERE kind='checkpoint' AND object_id=?1",
            [manifest["id"]
                .as_str()
                .ok_or_else(|| Error::Invalid("checkpoint id".into()))?],
            |r| r.get(0),
        )?;
        let recorded: Value = decode(&raw)?;
        if recorded["manifest_sha256"] != digest(&serde_json::to_vec(manifest)?)
            || recorded["ledger_cut"] != manifest["bindings"]["ledger_cut"]
        {
            return Err(Error::Conflict);
        }
        Ok(())
    }
    /// Invoked after checked local restoration. Every nonterminal Task is fenced
    /// until Owner review; restoring an old snapshot cannot authorize old calls.
    pub fn quarantine_restore(&mut self, actor: &Actor, manifest: &Value) -> Result<()> {
        owner(actor)?;
        self.validate_checkpoint(actor, manifest)?;
        let restored: bool = self.db.query_row(
            "SELECT EXISTS(SELECT 1 FROM ledger WHERE kind='checkpoint_restored' AND object_id=?1)",
            [manifest["id"].as_str()],
            |r| r.get(0),
        )?;
        if restored {
            return Ok(());
        }
        self.recover_actions(&Actor::Core)?;
        self.recover_model_queue(&Actor::Core)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        crate::control_auth::invalidate_restored_auth(&tx)?;
        let tasks = {
            let mut q=tx.prepare("SELECT id,state FROM task WHERE state NOT IN ('Done','Failed','Cancelled','ClosedUnverified')")?;
            q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for (id, old) in tasks {
            crate::tool_execution::recover_task_actions(&tx, &id)?;
            tx.execute("UPDATE task SET state='Blocked',block_reason='restore-review',version=version+1 WHERE id=?1",[&id])?;
            event(
                &tx,
                "restore_quarantine",
                &id,
                json!({"checkpoint_id":manifest["id"],"previous_state":old,"automatic_replay":false,"external_history_after_checkpoint":"requires Owner review"}),
            )?;
        }
        event(
            &tx,
            "checkpoint_restored",
            manifest["id"].as_str().unwrap(),
            json!({"manifest_sha256":digest(&serde_json::to_vec(manifest)?),"tasks_fenced":true}),
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Complete a restored, already produced Artifact, or resume local context.
    /// No prompt is sent by this operation. Fresh work needs separate admission.
    pub fn review_restored_task(
        &mut self,
        actor: &Actor,
        task: &str,
        version: u64,
        proof: Value,
    ) -> Result<Task> {
        owner(actor)?;
        evidence(&proof)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (state, _reason, v): (String, Option<String>, u64) = tx.query_row(
            "SELECT state,block_reason,version FROM task WHERE id=?1",
            [task],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        if state != "Blocked" || !review_required(&tx, task)? || v != version {
            return Err(Error::Conflict);
        }
        let pending:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown'))",[task],|r|r.get(0))?;
        if pending || crate::model_budget::unresolved_model_calls(&tx, task)? {
            return Err(Error::Reconciliation);
        }
        if crate::cancellation::cancelled(&tx, task)? {
            return Err(Error::Denied("cancelled Task cannot resume".into()));
        }
        let raw:String=tx.query_row("SELECT payload_json FROM ledger WHERE kind='restore_quarantine' AND object_id=?1 ORDER BY seq DESC LIMIT 1",[task],|r|r.get(0))?;
        let previous = decode::<Value>(&raw)?;
        if proof["checkpoint_id"] != previous["checkpoint_id"]
            || proof["post_checkpoint_effects_checked"] != true
        {
            return Err(Error::Denied(
                "checkpoint and post-checkpoint external history review required".into(),
            ));
        }
        let count: u64 = tx.query_row(
            "SELECT COUNT(*) FROM artifact WHERE task_id=?1",
            [task],
            |r| r.get(0),
        )?;
        let next = if proof["continuation"].is_object() {
            let c = &proof["continuation"];
            for field in ["turn_id", "message_id", "content", "native_session_ref"] {
                if c[field]
                    .as_str()
                    .is_none_or(|s| s.is_empty() || s.len() > 1_000_000)
                {
                    return Err(Error::Invalid(
                        "explicit new continuation fields required".into(),
                    ));
                }
            }
            let (turn, native): (String, Option<String>) = tx.query_row(
                "SELECT turn_id,native_session_ref FROM harness_binding WHERE task_id=?1",
                [task],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            if c["turn_id"] != turn || c["native_session_ref"].as_str() != native.as_deref() {
                return Err(Error::Denied(
                    "continuation must preserve native context identity".into(),
                ));
            }
            let reused:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch' AND json_extract(payload_json,'$.message_id')=?2)",params![task,c["message_id"].as_str()],|r|r.get(0))?;
            if reused || count > 0 {
                return Err(Error::Denied(
                    "fresh message required; existing outcome must be verified first".into(),
                ));
            }
            event(
                &tx,
                "recovery_continuation_authorized",
                task,
                json!({"turn_id":turn,"message_id":c["message_id"],"content_hash":digest(c["content"].as_str().unwrap().as_bytes()),"native_session_ref":native,"checkpoint_id":previous["checkpoint_id"],"automatic_send":false}),
            )?;
            "Ready"
        } else {
            if count == 0 || previous["previous_state"] != "Verifying" {
                return Err(Error::Denied(
                    "no verifiable outcome; explicit new continuation or remain Blocked".into(),
                ));
            }
            "Verifying"
        };
        tx.execute("UPDATE task SET state=?1,block_reason=NULL,version=version+1 WHERE id=?2 AND version=?3",params![next,task,version])?;
        event(
            &tx,
            "restore_owner_review",
            task,
            json!({"evidence_sha256":proof["sha256"],"source_ref":proof["source_ref"],"to":next,"counts_as_success":false,"automatic_prompt":false}),
        )?;
        tx.commit()?;
        self.task(task)
    }
}

impl Registry {
    /// Fence observations after a recovery continuation by the actual execution Session.
    pub fn check_recovery_observation(&self, turn: &str, runtime: Option<&str>) -> Result<()> {
        let required:bool=self.db.query_row("SELECT EXISTS(SELECT 1 FROM ledger l JOIN harness_binding h ON h.task_id=l.object_id WHERE h.turn_id=?1 AND l.kind IN ('recovery_continuation_authorized','handoff_continuation_authorized'))",[turn],|r|r.get(0))?;
        if required {
            let current:Option<String>=self.db.query_row("SELECT s.id FROM session s JOIN harness_binding h ON h.agent_id=s.agent_id WHERE h.turn_id=?1 AND s.ended_at IS NULL",[turn],|r|r.get(0)).optional()?;
            if runtime.is_none() || runtime != current.as_deref() {
                return Err(Error::Denied("stale recovery execution Session".into()));
            }
        }
        Ok(())
    }
    pub fn current_runtime_session(&self, agent: &str) -> Result<String> {
        Ok(self.db.query_row(
            "SELECT id FROM session WHERE agent_id=?1 AND ended_at IS NULL",
            [agent],
            |r| r.get(0),
        )?)
    }
}

#[cfg(target_os = "linux")]
impl Registry {
    /// Owner explicitly asks to compare a native create intent with the actual
    /// no-follow file bytes. This never executes or republishes the write.
    pub fn reconcile_native_file(&mut self, actor: &Actor, action: &str) -> Result<Value> {
        use std::io::Read;
        use std::os::unix::fs::MetadataExt;
        owner(actor)?;
        let record = self.action(action)?;
        let attempt:String=self.db.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='native_file_attempt' ORDER BY seq DESC LIMIT 1",[action],|r|r.get(0))?;
        let intent: Value = decode(&attempt)?;
        let name = intent["path"]
            .as_str()
            .ok_or_else(|| Error::Invalid("native intent path".into()))?;
        if name.is_empty() || name.contains(['/', '\\', '\0']) || name.starts_with('.') {
            return Err(Error::Denied("native intent path".into()));
        }
        let agent: String =
            self.db
                .query_row("SELECT agent_id FROM outbox WHERE id=?1", [action], |r| {
                    r.get(0)
                })?;
        let state = self.runtime_status(&agent)?;
        let root = state["config"]["cwd"]
            .as_str()
            .ok_or_else(|| Error::Invalid("native root".into()))?;
        let dir = rustix::fs::open(
            root,
            rustix::fs::OFlags::RDONLY
                | rustix::fs::OFlags::DIRECTORY
                | rustix::fs::OFlags::NOFOLLOW,
            rustix::fs::Mode::empty(),
        )
        .map_err(|e| Error::Io(e.into()))?;
        let fd = rustix::fs::openat(
            &dir,
            name,
            rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW,
            rustix::fs::Mode::empty(),
        )
        .map_err(|e| Error::Io(e.into()))?;
        let file = std::fs::File::from(fd);
        let m = file.metadata()?;
        if !m.is_file() || m.nlink() != 1 || m.len() > 4096 {
            return Err(Error::Denied(
                "bounded regular native evidence file required".into(),
            ));
        }
        let mut bytes = vec![];
        file.take(4097).read_to_end(&mut bytes)?;
        if bytes.len() > 4096 || intent["hash"] != digest(&bytes) {
            return Err(Error::Reconciliation);
        }
        let a = self.agent(&agent)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE kind='native_file_reconciled' AND object_id=?1)",[action],|r|r.get(0))?;
        if prior {
            return Ok(json!({"id":action,"state":"Confirmed","idempotent":true}));
        }
        if record.state != "Unknown" {
            return Err(Error::Reconciliation);
        }
        let version = record.payload["body"]["native_file"]["version"]
            .as_u64()
            .ok_or_else(|| Error::Invalid("native version".into()))?;
        if tx.execute(
            "UPDATE workspace SET version=version+1 WHERE id=?1 AND version=?2",
            params![a.workspace_id, version],
        )? != 1
        {
            return Err(Error::Conflict);
        }
        tx.execute(
            "UPDATE outbox SET state='Confirmed' WHERE id=?1 AND state='Unknown'",
            [action],
        )?;
        let proof = json!({"kind":"actual-file-matches-durable-intent","path":name,"sha256":digest(&bytes),"authorized_by":"Owner","republished":false});
        tx.execute(
            "UPDATE reconciliation SET status='Resolved',evidence_ref=?1 WHERE outbox_id=?2",
            params![encode(&proof)?, action],
        )?;
        event(&tx, "native_file_reconciled", action, proof.clone())?;
        event(
            &tx,
            "file_change",
            &a.workspace_id,
            json!({"action":action,"path":name,"hash":digest(&bytes),"version":version+1,"source":"Owner reconciliation; not a repeated write"}),
        )?;
        tx.commit()?;
        Ok(json!({"id":action,"state":"Confirmed","evidence":proof}))
    }
}

impl Registry {
    /// Core supplies the actual Linux boot_id only after its exclusive service
    /// lock. A new boot invalidates process liveness, not unknown effects/holds.
    pub fn recover_host_boot(&mut self, actor: &Actor, boot: &str) -> Result<()> {
        core(actor)?;
        if boot.is_empty() || boot.len() > 128 {
            return Err(Error::Invalid("kernel boot identity".into()));
        }
        let prior: Option<String> = self
            .db
            .query_row(
                "SELECT payload_json FROM ledger WHERE kind='host_boot' ORDER BY seq DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .optional()?;
        let previous = prior.as_deref().map(decode::<Value>).transpose()?;
        if previous.as_ref().is_some_and(|p| p["boot_id"] == boot) {
            return Ok(());
        }
        if previous.is_some() {
            let rows = self.model_instances(&Actor::Core)?;
            for row in rows
                .as_array()
                .ok_or_else(|| Error::Invalid("model state".into()))?
            {
                if row["held"] == true
                    && matches!(
                        row["state"].as_str(),
                        Some("Loading" | "Ready" | "Draining")
                    )
                {
                    self.model_lifecycle(&Actor::Core,row["id"].as_str().unwrap(),row["generation"].as_str().unwrap(),"Failed",None,"different Linux boot identity; process liveness invalidated, resource holds retained")?;
                }
            }
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        if previous.is_some() {
            let agents = {
                let mut q=tx.prepare("SELECT id,task_id,state FROM agent WHERE state NOT IN ('Exited','Failed','Terminated')")?;
                q.query_map([], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?
            };
            for (id, task, old) in agents {
                tx.execute(
                    "UPDATE session SET ended_at=?1 WHERE agent_id=?2 AND ended_at IS NULL",
                    params![now(), id],
                )?;
                tx.execute(
                    "UPDATE runtime_control SET owner_token=NULL,phase='idle' WHERE agent_id=?1",
                    [&id],
                )?;
                if matches!(old.as_str(), "Running" | "Blocked" | "Cancelling") {
                    tx.execute(
                        "UPDATE agent SET state=?1,version=version+1 WHERE id=?2",
                        params![
                            if old == "Cancelling" {
                                "Terminated"
                            } else {
                                "Ready"
                            },
                            id
                        ],
                    )?;
                    event(
                        &tx,
                        "runtime_lifecycle",
                        &id,
                        json!({"from":old,"to":if old=="Cancelling"{"Terminated"}else{"Ready"},"detail":{"event":"host_reboot_observed","boot_id":boot,"automatic_launch":false,"restart_budget_reset":false}}),
                    )?;
                }
                crate::tool_execution::recover_task_actions(&tx, &task)?;
                let holds=tx.execute("UPDATE model_reservation SET state='Unknown' WHERE task_id=?1 AND state='Reserved'",[&task])?;
                let changed=tx.execute("UPDATE task SET state='Blocked',block_reason='reconciliation',version=version+1 WHERE id=?1 AND state='Running'",[&task])?;
                if holds > 0 || changed > 0 {
                    event(
                        &tx,
                        "host_recovery_required",
                        &task,
                        json!({"unknown_model_calls":holds,"automatic_replay":false,"refund":false}),
                    )?;
                }
            }
        }
        event(
            &tx,
            "host_boot",
            "host",
            json!({"boot_id":boot,"previous_boot_id":previous.as_ref().map(|p|p["boot_id"].clone()),"automatic_launch":false,"physical_reservations_retained":true}),
        )?;
        tx.commit()?;
        Ok(())
    }
}

impl Registry {
    /// Read-only receipt lookup after a lost dispatch reply. It is NOT a permit
    /// to resend; it only identifies the exact execution for safe interruption.
    pub fn dispatch_receipt(&self, actor: &Actor, turn: &str, message: &str) -> Result<Value> {
        core(actor)?;
        let raw:String=self.db.query_row("SELECT payload_json FROM ledger l JOIN harness_binding h ON h.task_id=l.object_id WHERE h.turn_id=?1 AND l.kind='prompt_dispatch' AND json_extract(l.payload_json,'$.message_id')=?2 ORDER BY seq DESC LIMIT 1",params![turn,message],|r|r.get(0))?;
        let mut receipt: Value = decode(&raw)?;
        receipt["resend_permit"] = json!(false);
        Ok(receipt)
    }
}
