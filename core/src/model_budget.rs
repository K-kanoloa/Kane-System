//! Durable model-call admission. No provider, tokenizer or Agent implementation.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, Transaction, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

// JSON integers cross a JavaScript boundary. Reject rather than round.
const MAX_TOKENS: u64 = 9_007_199_254_740_991;
fn amount(v: &Value) -> Result<u64> {
    v.as_u64()
        .filter(|n| *n <= MAX_TOKENS)
        .ok_or_else(|| Error::Invalid("exact nonnegative token integer required".into()))
}
pub(crate) fn totals(db: &rusqlite::Connection, id: &str) -> Result<Value> {
    let (limit, legacy, concurrency): (u64, u64, u32) = db.query_row(
        "SELECT token_limit,used_tokens,max_concurrency FROM resource_budget WHERE id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let mut used = legacy;
    let mut held = 0u64;
    let mut active = 0u64;
    let mut frozen = false;
    let mut q = db.prepare(
        "SELECT state,reserved_tokens,actual_tokens FROM model_reservation WHERE budget_id=?1",
    )?;
    for row in q.query_map([id], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, u64>(1)?,
            r.get::<_, Option<u64>>(2)?,
        ))
    })? {
        let (state, reserved, actual) = row?;
        if matches!(state.as_str(), "Reserved" | "Unknown") {
            held = held.checked_add(reserved).ok_or(Error::Budget)?;
            active += 1;
        } else {
            used = used
                .checked_add(actual.ok_or(Error::Reconciliation)?)
                .ok_or(Error::Budget)?;
        }
        frozen |= state == "Overrun";
    }
    Ok(
        json!({"id":id,"token_limit":limit,"used_tokens":used,"reserved_tokens":held,"available_tokens":limit.saturating_sub(used).saturating_sub(held),"active_calls":active,"max_concurrency":concurrency,"frozen":frozen}),
    )
}
pub(crate) fn unresolved_model_calls(db: &rusqlite::Connection, task: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM model_reservation WHERE task_id=?1 AND state!='Settled') OR EXISTS(SELECT 1 FROM model_queue WHERE task_id=?1 AND state IN ('Queued','Dispatched','Unknown'))",
        [task],
        |r| r.get(0),
    )?)
}
pub(crate) fn block_model_task(tx: &Transaction<'_>, task: &str, reason: &str) -> Result<()> {
    let old: String = tx.query_row("SELECT state FROM task WHERE id=?1", [task], |r| r.get(0))?;
    if !matches!(
        old.as_str(),
        "Done" | "Failed" | "Cancelled" | "ClosedUnverified"
    ) {
        tx.execute("UPDATE task SET state='Blocked',block_reason='reconciliation',version=version+1 WHERE id=?1",[task])?;
        event(
            tx,
            "state_change",
            task,
            json!({"from":old,"to":"Blocked","reason":"reconciliation","detail":reason}),
        )?;
    }
    Ok(())
}
// All identities and route policy come from the admitted Turn and Owner model.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct ModelBinding {
    pub task: String,
    pub budget: String,
    pub session: String,
    pub agent: String,
    pub workspace: String,
    pub model_id: String,
    pub profile: Value,
    pub fifo: bool,
}
pub(crate) fn model_binding(
    db: &rusqlite::Connection,
    turn: &str,
    native: &str,
    provider: &str,
    model: &str,
) -> Result<ModelBinding> {
    let (task,budget,session,agent,workspace,model_id,spec):(String,String,String,String,String,String,String)=db.query_row(
        "SELECT t.id,t.budget_id,s.id,a.id,a.workspace_id,m.id,m.spec_json FROM harness_binding b JOIN task t ON t.id=b.task_id JOIN agent a ON a.id=b.agent_id JOIN model m ON m.id=a.model_id JOIN session s ON s.agent_id=a.id AND s.ended_at IS NULL WHERE b.turn_id=?1 AND b.native_session_ref=?2 AND t.state='Running' AND a.state='Running'",
        params![turn,native],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
    crate::collaboration::admission(db, &task, Some(&agent))?;
    let admitted: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch' AND json_extract(payload_json,'$.runtime_session_id')=?2)",params![task,session],|r|r.get(0))?;
    if !admitted {
        return Err(Error::Denied(
            "model request belongs to an obsolete execution Session".into(),
        ));
    }
    let profile: String = db.query_row(
        "SELECT payload_json FROM ledger WHERE kind='model_budget_enabled' AND object_id=?1",
        [&task],
        |r| r.get(0),
    )?;
    let profile: Value = serde_json::from_str(&profile)?;
    if profile["provider"] != provider || profile["model"] != model {
        return Err(Error::Denied("model route not approved".into()));
    }
    let spec: Value = serde_json::from_str(&spec)?;
    // Locality is explicit. No legacy unclassified/cloud route may bypass the
    // new gateway policy by omitting scheduling metadata.
    if spec["cloud"] != false {
        return Err(Error::Denied(
            "unclassified/cloud route unavailable; no implicit fallback".into(),
        ));
    }
    let fifo = match spec.get("scheduling") {
        None => false,
        Some(p)
            if p["mode"] == "mock_fifo"
                && p["backend_kind"] == "mock"
                && spec["cloud"] == false =>
        {
            true
        }
        Some(p)
            if p["mode"] == "gateway_fifo"
                && p["backend_kind"] == "local_http"
                && spec["cloud"] == false =>
        {
            true
        }
        _ => {
            return Err(Error::Unsupported(
                "only explicit local Mock or gateway FIFO scheduling is implemented".into(),
            ));
        }
    };
    Ok(ModelBinding {
        task,
        budget,
        session,
        agent,
        workspace,
        model_id,
        profile,
        fifo,
    })
}
pub(crate) fn reserve_bound(
    tx: &Transaction<'_>,
    b: &ModelBinding,
    turn: &str,
    call: &str,
) -> Result<Value> {
    let existing: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM model_reservation WHERE call_id=?1)",
        [call],
        |r| r.get(0),
    )?;
    if existing {
        return Err(Error::Reconciliation);
    }
    let reserved = amount(&b.profile["upper_bound_tokens"])?;
    crate::collaboration::group_budget(tx, &b.task, reserved)?;
    let state = totals(tx, &b.budget)?;
    if state["frozen"] == true
        || reserved > state["available_tokens"].as_u64().unwrap_or(0)
        || state["active_calls"].as_u64() >= state["max_concurrency"].as_u64()
    {
        event(
            tx,
            "model_budget_denied",
            &b.task,
            json!({"call_id":call,"budget_id":b.budget,"requested_tokens":reserved,"reason":"quota or concurrency"}),
        )?;
        return Err(Error::Budget);
    }
    tx.execute("INSERT INTO model_reservation(call_id,turn_id,task_id,budget_id,runtime_session_id,reserved_tokens,state) VALUES(?1,?2,?3,?4,?5,?6,'Reserved')",params![call,turn,b.task,b.budget,b.session,reserved])?;
    event(
        tx,
        "model_reserved",
        &b.task,
        json!({"call_id":call,"budget_id":b.budget,"reserved_tokens":reserved,"runtime_session_id":b.session,"stage":"authorized-before-provider-call"}),
    )?;
    Ok(json!({"call_id":call,"reserved_tokens":reserved,"permit":true}))
}
impl Registry {
    pub fn model_budget(&self, actor: &Actor, id: &str) -> Result<Value> {
        core(actor)?;
        let tx = self.db.unchecked_transaction()?;
        let result = totals(&tx, id)?;
        tx.commit()?;
        Ok(result)
    }

    /// Pin the Owner-registered backend bound to this dispatched Task. No guessing.
    pub fn enable_model_budget(&mut self, actor: &Actor, turn: &str) -> Result<Value> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (task,spec):(String,String)=tx.query_row("SELECT b.task_id,m.spec_json FROM harness_binding b JOIN agent a ON a.id=b.agent_id JOIN model m ON m.id=a.model_id JOIN task t ON t.id=b.task_id WHERE b.turn_id=?1 AND t.state='Running'",[turn],|r|Ok((r.get(0)?,r.get(1)?)))?;
        let spec: Value = serde_json::from_str(&spec)?;
        let profile = spec["budget_profile"].clone();
        if amount(&profile["upper_bound_tokens"])? == 0
            || ["provider", "model", "bound_evidence"]
                .iter()
                .any(|key| profile[*key].as_str().is_none_or(str::is_empty))
        {
            return Err(Error::Invalid(
                "approved backend total-token upper bound required".into(),
            ));
        }
        let old:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE kind='model_budget_enabled' AND object_id=?1",[&task],|r|r.get(0)).optional()?;
        if let Some(old) = old {
            if serde_json::from_str::<Value>(&old)? != profile {
                return Err(Error::Conflict);
            }
        } else {
            event(&tx, "model_budget_enabled", &task, profile.clone())?;
        }
        tx.commit()?;
        Ok(profile)
    }

    /// Single-use permit. Owner-selected Mock FIFO routes enqueue here instead.
    pub fn reserve_model(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        native: &str,
        provider: &str,
        model: &str,
    ) -> Result<Value> {
        core(actor)?;
        if call.is_empty() || call.len() > 128 {
            return Err(Error::Invalid("call id".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let binding = match model_binding(&tx, turn, native, provider, model) {
            Ok(binding) => binding,
            Err(error) => {
                tx.rollback()?;
                if matches!(&error, Error::Denied(_) | Error::Unsupported(_)) {
                    let audit = self.db.transaction()?;
                    event(
                        &audit,
                        "model_policy_denied",
                        turn,
                        json!({"call_id":call,"reason":"data/locality/route policy","provider_called":false}),
                    )?;
                    audit.commit()?;
                }
                return Err(error);
            }
        };
        if binding.fifo {
            let result =
                crate::model_queue::enqueue(&tx, binding, turn, call, native, provider, model);
            if result.is_ok() || matches!(result, Err(Error::Budget)) {
                tx.commit()?;
            }
            return result;
        }
        let result = reserve_bound(&tx, &binding, turn, call);
        // Preserve the existing durable quota-denial event; all other failures roll back.
        if result.is_ok() || matches!(result, Err(Error::Budget)) {
            tx.commit()?;
        }
        result
    }

    /// Usage must be the provider's final cumulative usage, never ACP context size.
    /// Unknown usage holds the entire reservation across restarts; no timeout refund.
    pub fn settle_model(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        receipt: Value,
    ) -> Result<Value> {
        core(actor)?;
        let gateway: bool = self.db.query_row(
            "SELECT EXISTS(SELECT 1 FROM model_gateway_call WHERE call_id=?1)",
            [call],
            |r| r.get(0),
        )?;
        if gateway {
            return Err(Error::Denied(
                "gateway owns final usage; Agent receipt cannot settle".into(),
            ));
        }
        self.settle_model_trusted(actor, turn, call, receipt)
    }
    pub(crate) fn settle_model_trusted(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        receipt: Value,
    ) -> Result<Value> {
        core(actor)?;
        let outcome = receipt["outcome"]
            .as_str()
            .ok_or_else(|| Error::Invalid("outcome".into()))?;
        if !matches!(outcome, "completed" | "failed" | "unknown") {
            return Err(Error::Invalid("outcome".into()));
        }
        let actual = if receipt["usage"].is_null() {
            None
        } else {
            let u = &receipt["usage"];
            let input = amount(&u["inputTokens"])?;
            let output = amount(&u["outputTokens"])?;
            let total = amount(&u["totalTokens"])?;
            if input.checked_add(output) != Some(total) {
                return Err(Error::Invalid("inconsistent final usage".into()));
            }
            Some(total)
        };
        if outcome == "unknown" && actual.is_some() {
            return Err(Error::Invalid(
                "partial usage cannot settle unknown call".into(),
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (task,budget,reserved,old,state):(String,String,u64,Option<String>,String)=tx.query_row("SELECT task_id,budget_id,reserved_tokens,receipt_json,state FROM model_reservation WHERE call_id=?1 AND turn_id=?2",params![call,turn],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
        if let Some(old) = old {
            if serde_json::from_str::<Value>(&old)? != receipt {
                return Err(Error::Conflict);
            }
            return Ok(json!({"call_id":call,"state":state,"budget":totals(&tx,&budget)?}));
        }
        // Cancellation/recovery made dispatch ambiguous. A late provider receipt is
        // evidence for Owner reconciliation, never an automatic refund or route unfreeze.
        let actual = if state == "Unknown" { None } else { actual };
        let next = match actual {
            None => "Unknown",
            Some(n) if n > reserved => "Overrun",
            _ => "Settled",
        };
        tx.execute("UPDATE model_reservation SET state=?1,actual_tokens=?2,receipt_json=?3 WHERE call_id=?4",params![next,actual,encode(&receipt)?,call])?;
        event(
            &tx,
            "model_settled",
            &task,
            json!({"call_id":call,"budget_id":budget,"state":next,"reserved_tokens":reserved,"actual_tokens":actual,"receipt":receipt}),
        )?;
        if outcome != "completed" || next != "Settled" {
            block_model_task(&tx, &task, "model outcome/usage requires reconciliation")?;
            // A failed execution cannot leave an outstanding dispatch outside
            // the reconciliation queue while the Task is already blocked.
            crate::tool_execution::recover_task_actions(&tx, &task)?;
        }
        crate::model_queue::settled(&tx, call, next)?;
        let result = json!({"call_id":call,"state":next,"budget":totals(&tx,&budget)?});
        tx.commit()?;
        Ok(result)
    }
}
