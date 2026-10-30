//! Projection of existing Harness facts. This module never runs an Agent or tool.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, params};
use serde_json::json;

impl Registry {
    pub fn harness_bindings(&self) -> Result<Vec<serde_json::Value>> {
        let mut q=self.db.prepare("SELECT turn_id,task_id,harness_agent_ref,native_session_ref FROM harness_binding ORDER BY rowid")?;
        Ok(q.query_map([],|r|Ok(json!({"turn_id":r.get::<_,String>(0)?,"task_id":r.get::<_,String>(1)?,"harness_agent_ref":r.get::<_,String>(2)?,"native_session_ref":r.get::<_,Option<String>>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?)
    }
    pub fn bound_task(&self, turn_id: &str) -> Result<Option<Task>> {
        let id: Option<String> = self
            .db
            .query_row(
                "SELECT task_id FROM harness_binding WHERE turn_id=?1",
                [turn_id],
                |r| r.get(0),
            )
            .optional()?;
        id.map(|id| self.task(&id)).transpose()
    }

    /// Bind only after the trusted host has resolved and authenticated the actual Turn.
    pub fn bind_harness(
        &mut self,
        actor: &Actor,
        turn_id: &str,
        harness_agent_ref: &str,
        spec: TaskSpec,
    ) -> Result<Task> {
        owner(actor)?;
        spec.validate()?;
        if turn_id.is_empty() || harness_agent_ref.is_empty() {
            return Err(Error::Invalid("Harness identity required".into()));
        }
        if let Some(task) = self.bound_task(turn_id)? {
            let previous: String = self.db.query_row(
                "SELECT harness_agent_ref FROM harness_binding WHERE turn_id=?1",
                [turn_id],
                |r| r.get(0),
            )?;
            if previous != harness_agent_ref || encode(&task.spec)? != encode(&spec)? {
                return Err(Error::Conflict);
            }
            return Ok(task);
        }
        let task_id = new_id("task");
        let agent_id = new_id("agent");
        let model_id = new_id("model");
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute(
            "INSERT INTO model VALUES(?1,?2)",
            params![
                model_id,
                encode(
                    &json!({"kind":"external-harness-binding","verified_model_identity":false})
                )?
            ],
        )?;
        tx.execute("INSERT INTO task(id,state,budget_id,workspace_id,workload_class,spec_json) VALUES(?1,'Running',?2,?3,?4,?5)",
            params![task_id,spec.budget_id,spec.workspace_id,encode(&spec.workload.class)?.trim_matches('"'),encode(&spec)?])?;
        tx.execute("INSERT INTO agent(id,role,task_id,workspace_id,budget_id,model_id,state) VALUES(?1,'harness-worker',?2,?3,?4,?5,'Running')",params![agent_id,task_id,spec.workspace_id,spec.budget_id,model_id])?;
        tx.execute(
            "INSERT INTO harness_binding VALUES(?1,?2,?3,?4,NULL)",
            params![turn_id, task_id, agent_id, harness_agent_ref],
        )?;
        event(
            &tx,
            "task_created",
            &task_id,
            json!({"state":"Pending","source":"existing Harness Turn","turn_id":turn_id}),
        )?;
        event(
            &tx,
            "state_change",
            &task_id,
            json!({"from":"Pending","to":"Ready","source":"host admission"}),
        )?;
        event(
            &tx,
            "state_change",
            &task_id,
            json!({"from":"Ready","to":"Running","source":"already started Harness work"}),
        )?;
        event(
            &tx,
            "agent_registered",
            &agent_id,
            json!({"task":task_id,"harness_ref":harness_agent_ref,"enforcement":"Partial"}),
        )?;
        event(
            &tx,
            "allocation_decision",
            &task_id,
            json!({"mode":"Direct","reason":"existing single-Agent workload"}),
        )?;
        tx.commit()?;
        self.task(&task_id)
    }

    /// Host snapshots are reconciled without replaying prompt, tool or native execution.
    pub fn ingest_harness(
        &mut self,
        actor: &Actor,
        turn_id: &str,
        status: &str,
        native_session_ref: Option<&str>,
        message: Option<(&str, &str)>,
    ) -> Result<Task> {
        core(actor)?;
        if !matches!(
            status,
            "running" | "waiting_user" | "finished" | "interrupted" | "failed"
        ) {
            return Err(Error::Invalid("unknown Harness status".into()));
        }
        let mut t = self
            .bound_task(turn_id)?
            .ok_or_else(|| Error::NotFound(turn_id.into()))?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let (agent, old_session): (String, Option<String>) = tx.query_row(
            "SELECT agent_id,native_session_ref FROM harness_binding WHERE turn_id=?1",
            [turn_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if let Some(reference) = native_session_ref.filter(|s| !s.is_empty())
            && old_session.as_deref() != Some(reference)
        {
            tx.execute(
                "UPDATE harness_binding SET native_session_ref=?1 WHERE turn_id=?2",
                params![reference, turn_id],
            )?;
            let managed:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='runtime_configured')",[&agent],|r|r.get(0))?;
            if managed {
                // Native ACP reference is not a replacement for the Supervisor process Session.
                return Err(Error::Conflict);
            }
            tx.execute(
                "UPDATE session SET ended_at=?1 WHERE agent_id=?2 AND ended_at IS NULL",
                params![now(), agent],
            )?;
            let session = new_id("session");
            tx.execute(
                "INSERT INTO session VALUES(?1,?2,?3,?4,?5,NULL)",
                params![session, agent, t.id, reference, now()],
            )?;
            event(
                &tx,
                "session_bound",
                &t.id,
                json!({"turn":turn_id,"session":session,"native_ref":reference}),
            )?;
        }
        if crate::cancellation::cancelled(&tx, &t.id)? {
            event(
                &tx,
                "harness_after_cancel",
                &t.id,
                json!({"status":status,"ignored":true}),
            )?;
            tx.commit()?;
            return self.task(&t.id);
        }
        if crate::recovery::review_required(&tx, &t.id)? {
            event(
                &tx,
                "harness_during_restore",
                &t.id,
                json!({"status":status,"ignored":true}),
            )?;
            tx.commit()?;
            return self.task(&t.id);
        }
        if crate::control_workspace::human_fenced(&tx, &t.id)? {
            event(
                &tx,
                "harness_during_human_control",
                &t.id,
                json!({"status":status,"ignored":true}),
            )?;
            tx.commit()?;
            return self.task(&t.id);
        }
        if t.state.terminal() {
            tx.commit()?;
            return Ok(t);
        }
        if t.state == TaskState::Blocked
            && t.block_reason.as_deref() == Some("human")
            && matches!(status, "running" | "finished")
        {
            if tx.execute("UPDATE task SET state='Running',block_reason=NULL,version=version+1 WHERE id=?1 AND version=?2",params![t.id,t.version])?!=1{return Err(Error::Conflict);}
            event(
                &tx,
                "state_change",
                &t.id,
                json!({"from":"Blocked","to":"Running","reason":"existing Harness resumed after human input"}),
            )?;
            t.state = TaskState::Running;
            t.version += 1;
            t.block_reason = None;
        }
        match status {
            "finished" => {
                let (message_id, content) = message
                    .filter(|(id, c)| !id.is_empty() && !c.is_empty())
                    .ok_or_else(|| {
                        Error::Invalid(
                            "explicit final Message required; silence is not completion".into(),
                        )
                    })?;
                let hash = digest(content.as_bytes());
                let prior = tx
                    .query_row(
                        "SELECT turn_id,content_hash FROM harness_receipt WHERE message_id=?1",
                        [message_id],
                        |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
                    )
                    .optional()?;
                if let Some((old_turn, old_hash)) = prior {
                    if old_turn != turn_id || old_hash != hash {
                        return Err(Error::Conflict);
                    }
                    tx.commit()?;
                    return self.task(&t.id);
                }
                // A new reply during verification cannot silently replace the acceptance candidate.
                if t.state != TaskState::Running {
                    return Err(Error::Conflict);
                }
                let stale:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch') AND NOT EXISTS(SELECT 1 FROM ledger l JOIN session s ON s.id=json_extract(l.payload_json,'$.runtime_session_id') WHERE l.object_id=?1 AND l.kind='prompt_dispatch' AND s.ended_at IS NULL)",[&t.id],|r|r.get(0))?;
                if stale {
                    return Err(Error::Conflict);
                }
                let missing_observation:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch') AND NOT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='llm_call_start')",[&t.id],|r|r.get(0))?;
                if missing_observation {
                    return Err(Error::Reconciliation);
                }
                let unknown_calls:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger s WHERE s.object_id=?1 AND s.kind='llm_call_start' AND s.seq>COALESCE((SELECT MAX(b.seq) FROM ledger b WHERE b.object_id=s.object_id AND b.kind IN ('recovery_continuation_authorized','handoff_continuation_authorized')),0) AND NOT EXISTS(SELECT 1 FROM ledger e WHERE e.object_id=s.object_id AND e.kind='llm_call_result' AND json_extract(e.payload_json,'$.fact.call_id')=json_extract(s.payload_json,'$.fact.call_id') AND json_extract(e.payload_json,'$.fact.outcome')='completed'))",[&t.id],|r|r.get(0))?;
                let unknown_tools:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger s WHERE s.object_id=?1 AND s.kind='tool_call' AND s.seq>COALESCE((SELECT MAX(b.seq) FROM ledger b WHERE b.object_id=s.object_id AND b.kind IN ('recovery_continuation_authorized','handoff_continuation_authorized')),0) AND json_extract(s.payload_json,'$.fact.status') IN ('pending','in_progress') AND NOT EXISTS(SELECT 1 FROM ledger e WHERE e.object_id=s.object_id AND e.kind='tool_call' AND json_extract(e.payload_json,'$.fact.tool_call_id')=json_extract(s.payload_json,'$.fact.tool_call_id') AND json_extract(e.payload_json,'$.fact.status') IN ('completed','failed','cancelled')))",[&t.id],|r|r.get(0))?;
                if unknown_calls || unknown_tools {
                    return Err(Error::Reconciliation);
                }
                let unresolved_budget:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='model_budget_enabled') AND (NOT EXISTS(SELECT 1 FROM model_reservation WHERE task_id=?1) OR EXISTS(SELECT 1 FROM model_reservation WHERE task_id=?1 AND state!='Settled'))",[&t.id],|r|r.get(0))?;
                if unresolved_budget {
                    return Err(Error::Reconciliation);
                }
                let unresolved:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown'))",[&t.id],|r|r.get(0))?;
                if unresolved {
                    return Err(Error::Reconciliation);
                }
                crate::control_workspace::require_fresh_read(&tx, &t.id)?;
                let artifact = new_id("artifact");
                crate::resources::check_task_storage(
                    &tx,
                    &t.id,
                    &t.spec.workspace_id,
                    content.len() as u64,
                )?;
                tx.execute("INSERT INTO artifact(id,task_id,producer_id,format,editable,content,content_hash) VALUES(?1,?2,?3,'text/markdown',1,?4,?5)",params![artifact,t.id,agent,content,hash])?;
                crate::control_workspace::link_artifact(&tx, &artifact, &t.id)?;
                tx.execute(
                    "INSERT INTO harness_receipt VALUES(?1,?2,?3,?4)",
                    params![message_id, turn_id, hash, artifact],
                )?;
                if tx.execute("UPDATE task SET state='Verifying',verification_state='pending',block_reason=NULL,version=version+1 WHERE id=?1 AND version=?2",params![t.id,t.version])?!=1{return Err(Error::Conflict);}
                event(
                    &tx,
                    "artifact_submitted",
                    &artifact,
                    json!({"task":t.id,"message_id":message_id,"hash":hash,"source":"existing Harness explicit completion"}),
                )?;
                event(
                    &tx,
                    "state_change",
                    &t.id,
                    json!({"from":"Running","to":"Verifying","version":t.version+1,"agent_completion_is_not_done":true}),
                )?;
            }
            "interrupted" | "failed" | "waiting_user" => {
                // Failed/interrupted native work can contain unknown external effects. Never guess.
                let reason = if status == "waiting_user" {
                    "human"
                } else {
                    "reconciliation"
                };
                if t.state == TaskState::Running
                    || (t.state == TaskState::Blocked
                        && t.block_reason.as_deref() == Some("human")
                        && status != "waiting_user")
                {
                    tx.execute("UPDATE task SET state='Blocked',block_reason=?1,version=version+1 WHERE id=?2 AND version=?3",params![reason,t.id,t.version])?;
                    event(
                        &tx,
                        "state_change",
                        &t.id,
                        json!({"from":t.state,"to":"Blocked","reason":reason,"native_status":status,"automatic_retry":false}),
                    )?;
                }
                if status != "waiting_user" {
                    crate::model_queue::recover_task(&tx, &t.id)?;
                    crate::tool_execution::recover_task_actions(&tx, &t.id)?;
                }
            }
            "running" => {}
            _ => unreachable!(),
        }
        tx.commit()?;
        self.task(&t.id)
    }
}
