//! Trusted Harness execution admission and idempotent observations. No Agent loop.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

impl Registry {
    /// A response lost after this commit is NOT permission to resend the prompt.
    pub fn dispatch_harness(
        &mut self,
        actor: &Actor,
        agent_id: &str,
        turn: &str,
        message: &str,
        content: &str,
        native: &str,
    ) -> Result<Task> {
        core(actor)?;
        if [turn, message, content, native]
            .iter()
            .any(|s| s.is_empty())
        {
            return Err(Error::Invalid("dispatch fields required".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (task_id, state): (String, String) = tx.query_row(
            "SELECT task_id,state FROM agent WHERE id=?1",
            [agent_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        crate::collaboration::admission(&tx, &task_id, Some(agent_id))?;
        if state != "Running" {
            return Err(Error::Conflict);
        }
        let task_state: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [&task_id], |r| {
                r.get(0)
            })?;
        if task_state != "Pending" && task_state != "Ready" {
            return Err(Error::Conflict);
        }
        // One dispatch per Phase-0 Task. No implicit resend on duplicate receipt.
        let prior: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch')",
            [&task_id],
            |r| r.get(0),
        )?;
        if prior {
            let authorized:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind IN ('recovery_continuation_authorized','handoff_continuation_authorized') ORDER BY seq DESC LIMIT 1",[&task_id],|r|r.get(0)).optional()?;
            let permit = authorized
                .map(|raw| decode::<Value>(&raw))
                .transpose()?
                .ok_or(Error::Reconciliation)?;
            let consumed:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch' AND json_extract(payload_json,'$.message_id')=?2)",params![task_id,message],|r|r.get(0))?;
            if consumed
                || permit["turn_id"] != turn
                || permit["message_id"] != message
                || permit["content_hash"] != digest(content.as_bytes())
                || permit["native_session_ref"] != native
            {
                return Err(Error::Reconciliation);
            }
        }
        let web_grant:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind IN ('control_dispatch_authorized','handoff_continuation_authorized','recovery_continuation_authorized') ORDER BY seq DESC LIMIT 1",[&task_id],|r|r.get(0)).optional()?;
        if let Some(raw) = web_grant {
            let grant: Value = decode(&raw)?;
            // A later private Owner recovery grant supersedes an older Web grant.
            // Its existing recovery proof was already checked by Rust, not a Web caller.
            if let Some(sid) = grant["owner_session_id"].as_str() {
                if grant["message_id"] != message
                    || grant["content_hash"] != digest(content.as_bytes())
                {
                    return Err(Error::Conflict);
                }
                crate::control_auth::require_session_id(&tx, sid)?;
            }
        }
        let runtime:String=tx.query_row("SELECT id FROM session WHERE agent_id=?1 AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",[agent_id],|r|r.get(0))?;
        if !prior {
            tx.execute(
                "INSERT INTO harness_binding VALUES(?1,?2,?3,?4,?5)",
                params![turn, task_id, agent_id, "supervised-acp", native],
            )?;
        } else {
            let bound:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM harness_binding WHERE turn_id=?1 AND task_id=?2 AND agent_id=?3 AND native_session_ref=?4)",params![turn,task_id,agent_id,native],|r|r.get(0))?;
            if !bound {
                return Err(Error::Conflict);
            }
        }
        if task_state == "Pending" {
            event(
                &tx,
                "state_change",
                &task_id,
                json!({"from":"Pending","to":"Ready","reason":"Owner dispatch admission"}),
            )?;
        }
        tx.execute(
            "UPDATE task SET state='Running',version=version+1 WHERE id=?1",
            [&task_id],
        )?;
        event(
            &tx,
            "state_change",
            &task_id,
            json!({"from":"Ready","to":"Running"}),
        )?;
        event(
            &tx,
            "prompt_dispatch",
            &task_id,
            json!({"agent_id":agent_id,"turn_id":turn,"message_id":message,"content_hash":digest(content.as_bytes()),"native_session_ref":native,"runtime_session_id":runtime,"stage":"authorized-before-send","automatic_retry":false}),
        )?;
        tx.commit()?;
        self.task(&task_id)
    }

    /// Observations are not tool authorization and cannot mark Task Done.
    pub fn harness_fact(
        &mut self,
        actor: &Actor,
        turn: &str,
        receipt: &str,
        kind: &str,
        payload: Value,
    ) -> Result<()> {
        core(actor)?;
        if receipt.is_empty()
            || receipt.len() > 256
            || !matches!(
                kind,
                "llm_call_start" | "llm_call_result" | "tool_call" | "context_usage"
            )
        {
            return Err(Error::Invalid("fact kind/receipt".into()));
        }
        self.check_recovery_observation(turn, payload["runtime_session_id"].as_str())?;
        if kind.starts_with("llm_call") && payload["call_id"].as_str().is_none_or(str::is_empty) {
            return Err(Error::Invalid("call id required".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (task, native): (String, Option<String>) = tx.query_row(
            "SELECT task_id,native_session_ref FROM harness_binding WHERE turn_id=?1",
            [turn],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if kind.starts_with("llm_call") {
            let protected:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='model_budget_enabled')",[&task],|r|r.get(0))?;
            if protected {
                let reserved:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM model_reservation WHERE turn_id=?1 AND call_id=?2)",params![turn,payload["call_id"].as_str()],|r|r.get(0))?;
                if !reserved {
                    return Err(Error::Denied(
                        "model observation without reservation".into(),
                    ));
                }
            }
        }
        if payload["native_session_ref"].as_str() != native.as_deref() {
            return Err(Error::Conflict);
        }
        let data = json!({"receipt":receipt,"turn_id":turn,"fact":payload,"trust":"observed-adapter-fact; not tool enforcement"});
        let previous:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind=?2 AND json_extract(payload_json,'$.receipt')=?3",params![task,kind,receipt],|r|r.get(0)).optional()?;
        if let Some(previous) = previous {
            if serde_json::from_str::<Value>(&previous)? != data {
                return Err(Error::Conflict);
            }
            return Ok(());
        }
        let state: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [&task], |r| r.get(0))?;
        if !matches!(state.as_str(), "Running" | "Blocked") {
            return Err(Error::Conflict);
        }
        event(&tx, kind, &task, data)?;
        tx.commit()?;
        Ok(())
    }
}
