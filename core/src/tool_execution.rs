//! Managed-tool admission at the existing Harness dispatch seam. The caller is a
//! trusted host relay, not an authenticated sandbox. Linux isolation is separate.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, Transaction, params};
use serde_json::{Value, json};

pub(crate) fn recover_task_actions(tx: &Transaction<'_>, task: &str) -> Result<()> {
    let ids = {
        let mut q = tx.prepare("SELECT id FROM outbox WHERE task_id=?1 AND state='Dispatching'")?;
        q.query_map([task], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?
    };
    for id in ids {
        unknown(tx, &id, task)?;
    }
    // No dispatch permit was committed for Pending. Old native calls are never replayed.
    let count = tx.execute(
        "UPDATE outbox SET state='NotExecuted' WHERE task_id=?1 AND state='Pending'",
        [task],
    )?;
    if count > 0 {
        event(
            tx,
            "tool_intents_abandoned",
            task,
            json!({"count":count,"automatic_retry":false}),
        )?;
    }
    Ok(())
}
pub(crate) fn unknown(tx: &Transaction<'_>, id: &str, task: &str) -> Result<()> {
    if tx.execute(
        "UPDATE outbox SET state='Unknown' WHERE id=?1 AND state='Dispatching'",
        [id],
    )? == 1
    {
        tx.execute(
            "INSERT INTO reconciliation VALUES(?1,?2,'Unknown',NULL)",
            params![new_id("reconcile"), id],
        )?;
        crate::model_budget::block_model_task(tx, task, "tool result unknown")?;
        event(
            tx,
            "reconcile",
            task,
            json!({"action":id,"status":"Unknown","automatic_retry":false}),
        )?;
    }
    Ok(())
}
impl Registry {
    /// Exact invocation grants, scoped to Agent's Task and Workspace. No wildcard
    /// tools, shell commands or paths. Arguments are hashed rather than logged.
    pub fn tool_target(
        &self,
        actor: &Actor,
        agent: &str,
        tool: &str,
        args: &Value,
    ) -> Result<String> {
        core(actor)?;
        if tool.is_empty() || tool.len() > 128 || !args.is_object() {
            return Err(Error::Invalid("tool specification".into()));
        }
        let a = self.agent(agent)?;
        Ok(format!(
            "tool:{}:{}:{}:{}",
            a.task_id,
            a.workspace_id,
            tool,
            digest(&serde_json::to_vec(args)?)
        ))
    }
    pub fn tool_request(
        &mut self,
        actor: &Actor,
        turn: &str,
        native: &str,
        call: &str,
        request: Value,
    ) -> Result<Value> {
        core(actor)?;
        let op = request["op"].as_str().unwrap_or("").to_owned();
        let result = self.tool_request_inner(actor, turn, native, call, request);
        if result.is_err() {
            let task: Option<String> = self
                .db
                .query_row(
                    "SELECT task_id FROM harness_binding WHERE turn_id=?1",
                    [turn],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(task) = task {
                let tx = self.db.transaction()?;
                event(
                    &tx,
                    "tool_denied",
                    &task,
                    json!({"operation":op,"call_id":call,"reason":"rejected; no permit","automatic_retry":false}),
                )?;
                if op == "tool_file" {
                    let id: Option<String> = tx.query_row(
                        "SELECT id FROM outbox WHERE idempotency_key=?1 AND task_id=?2 AND EXISTS(SELECT 1 FROM ledger WHERE object_id=outbox.id AND kind='native_file_attempt') AND NOT EXISTS(SELECT 1 FROM ledger WHERE object_id=outbox.id AND kind='native_file_commit')",
                        params![format!("tool:{task}:{call}"),task], |r| r.get(0)).optional()?;
                    if let Some(id) = id {
                        unknown(&tx, &id, &task)?;
                    }
                }
                tx.commit()?;
            }
        }
        result
    }
    fn tool_request_inner(
        &mut self,
        actor: &Actor,
        turn: &str,
        native: &str,
        call: &str,
        request: Value,
    ) -> Result<Value> {
        core(actor)?;
        if call.is_empty() || call.len() > 128 {
            return Err(Error::Invalid("tool call id".into()));
        }
        let (task, agent, expected): (String, String, Option<String>) = self.db.query_row(
            "SELECT task_id,agent_id,native_session_ref FROM harness_binding WHERE turn_id=?1",
            [turn],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        if expected.as_deref() != Some(native) {
            return Err(Error::Denied("tool session binding".into()));
        }
        let key = format!("tool:{}:{}", task, call);
        let op = request["op"].as_str().unwrap_or("");
        if op == "tool_prepare" {
            let tool = request["tool"]
                .as_str()
                .ok_or_else(|| Error::Invalid("tool required".into()))?;
            let target = self.tool_target(actor, &agent, tool, &request["arguments"])?;
            let runtime: String = self.db.query_row(
                "SELECT id FROM session WHERE agent_id=?1 AND ended_at IS NULL",
                [&agent],
                |r| r.get(0),
            )?;
            let mut body = json!({"kind":"managed_tool","turn_id":turn,"native_session_ref":native,"runtime_session_id":runtime,"tool":tool,"arguments_hash":digest(&serde_json::to_vec(&request["arguments"])?)});
            if tool == "write"
                && self.runtime_status(&agent)?["config"]["sandbox"]["native_file_proxy"] == true
            {
                let ws = self.workspace(&self.agent(&agent)?.workspace_id)?;
                body["native_file"] = json!({"epoch":ws.lease_epoch,"version":ws.version});
            }
            let action = self.prepare_action(&Actor::Agent(agent), &task, &key, &target, body)?;
            return Ok(serde_json::to_value(action)?);
        }
        let id: String = self.db.query_row(
            "SELECT id FROM outbox WHERE idempotency_key=?1 AND task_id=?2 AND agent_id=?3",
            params![key, task, agent],
            |r| r.get(0),
        )?;
        let action = self.action(&id)?;
        if action.payload["body"]["turn_id"] != turn
            || action.payload["body"]["native_session_ref"] != native
        {
            return Err(Error::Conflict);
        }
        if op == "tool_claim" {
            self.claim_action(actor, &id)?;
            return Ok(json!({"permit":true,"id":id,"call_id":call}));
        }
        if op == "tool_file" {
            return self.native_file_commit(&id, &agent, &request["arguments"]);
        }
        if op != "tool_result" {
            return Err(Error::Denied("tool operation".into()));
        }
        let receipt = request["receipt"].clone();
        let outcome = receipt["outcome"].as_str().unwrap_or("");
        let hash = receipt["result_hash"].as_str().unwrap_or("");
        if !matches!(outcome, "completed" | "unknown")
            || hash.len() != 64
            || !hash.bytes().all(|c| c.is_ascii_hexdigit())
        {
            return Err(Error::Invalid("tool result receipt".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let (state, old): (String, Option<String>) = tx.query_row(
            "SELECT state,result_json FROM outbox WHERE id=?1",
            [&id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if outcome == "completed" && action.payload["body"]["native_file"].is_object() {
            let committed: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='native_file_commit')",
                [&id], |r| r.get(0))?;
            if !committed {
                return Err(Error::Denied("native file commit evidence required".into()));
            }
        }
        if let Some(old) = old {
            if decode::<Value>(&old)? != receipt {
                return Err(Error::Conflict);
            }
        } else {
            if !matches!(state.as_str(), "Dispatching" | "Unknown") {
                return Err(Error::Reconciliation);
            }
            if outcome == "unknown" {
                unknown(&tx, &id, &task)?;
            }
            // A late receipt cannot clear a recovery decision; only Owner reconciliation can.
            tx.execute("UPDATE outbox SET result_json=?1,state=CASE WHEN state='Dispatching' THEN 'Confirmed' ELSE state END WHERE id=?2",params![encode(&receipt)?,id])?;
            event(&tx, "result", &task, json!({"action":id,"receipt":receipt}))?;
        }
        let state: String =
            tx.query_row("SELECT state FROM outbox WHERE id=?1", [&id], |r| r.get(0))?;
        tx.commit()?;
        Ok(json!({"id":id,"state":state}))
    }
}
/// Called while holding the same transaction as the dispatch claim.
pub(crate) fn validate_managed(
    tx: &Transaction<'_>,
    agent: &str,
    task: &str,
    payload: &Value,
) -> Result<()> {
    if payload["body"]["kind"] != "managed_tool" {
        return Ok(());
    }
    if payload["body"]["tool"] != "read" {
        crate::control_workspace::require_fresh_read(tx, task)?;
    }
    let runtime = payload["body"]["runtime_session_id"]
        .as_str()
        .ok_or(Error::Conflict)?;
    let alive:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM session s JOIN agent a ON a.id=s.agent_id WHERE s.id=?1 AND s.agent_id=?2 AND s.task_id=?3 AND s.ended_at IS NULL AND a.state='Running')",params![runtime,agent,task],|r|r.get(0))?;
    let dispatch:Option<String>=tx.query_row("SELECT json_extract(payload_json,'$.runtime_session_id') FROM ledger WHERE kind='prompt_dispatch' AND object_id=?1 ORDER BY seq DESC LIMIT 1",[task],|r|r.get(0)).optional()?;
    if !alive || dispatch.as_deref() != Some(runtime) {
        return Err(Error::Denied("stale runtime tool call".into()));
    }
    Ok(())
}
