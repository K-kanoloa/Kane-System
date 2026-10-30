//! Bounded, durable shared Mock FIFO. Execution stays in the existing provider.
//! A dispatch permit and PR-3A budget reservation commit in one transaction.
use crate::{
    model_budget::{ModelBinding, model_binding, reserve_bound},
    registry::*,
    *,
};
use kane_models::FifoQueue;
use rusqlite::{Transaction, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_PENDING: i64 = 128;
const QUEUE_LIFETIME_MS: i64 = 30_000;
fn clock_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct QueuedCall {
    binding: ModelBinding,
    turn: String,
    call: String,
    native: String,
    provider: String,
    model: String,
}
#[derive(Debug, Clone, Serialize)]
struct Entry {
    seq: i64,
    request: QueuedCall,
    state: String,
    enqueued_ms: i64,
    expires_ms: i64,
    dispatched_ms: Option<i64>,
    finished_ms: Option<i64>,
}
fn entries(
    db: &rusqlite::Connection,
    filter: &str,
    values: impl rusqlite::Params,
) -> Result<Vec<Entry>> {
    // filter strings are internal literals, never an external SQL interface.
    let mut q = db.prepare(&format!("SELECT seq,binding_json,state,enqueued_ms,expires_ms,dispatched_ms,finished_ms FROM model_queue {filter} ORDER BY seq"))?;
    let raw = q
        .query_map(values, |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, i64>(4)?,
                r.get::<_, Option<i64>>(5)?,
                r.get::<_, Option<i64>>(6)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    raw.into_iter()
        .map(
            |(seq, request, state, enqueued_ms, expires_ms, dispatched_ms, finished_ms)| {
                Ok(Entry {
                    seq,
                    request: decode(&request)?,
                    state,
                    enqueued_ms,
                    expires_ms,
                    dispatched_ms,
                    finished_ms,
                })
            },
        )
        .collect()
}
fn entry(db: &rusqlite::Connection, call: &str) -> Result<Entry> {
    entries(db, "WHERE call_id=?1", [call])?
        .into_iter()
        .next()
        .ok_or_else(|| Error::NotFound(call.into()))
}
fn evidence(e: &Entry, state: &str, reason: &str) -> Value {
    let b = &e.request.binding;
    json!({"queue_seq":e.seq,"call_id":e.request.call,"agent_id":b.agent,"task_id":b.task,
        "workspace_id":b.workspace,"budget_id":b.budget,"model_id":b.model_id,
        "runtime_session_id":b.session,"native_session_ref":e.request.native,"state":state,
        "enqueued_ms":e.enqueued_ms,"dispatched_ms":e.dispatched_ms,"finished_ms":e.finished_ms,
        "queue_wait_ms":e.dispatched_ms.map(|t|t.saturating_sub(e.enqueued_ms).max(0)),
        "permit_occupancy_ms":e.finished_ms.zip(e.dispatched_ms).map(|(end,start)|end.saturating_sub(start).max(0)),
        "at_ms":clock_ms(),"reason":reason,"automatic_retry":false})
}
fn change(tx: &Transaction<'_>, e: &Entry, state: &str, kind: &str, reason: &str) -> Result<()> {
    if state == "Unknown" {
        tx.execute("UPDATE model_gateway_call SET state='Unknown' WHERE call_id=?1 AND state IN ('Prepared','Claimed')",[&e.request.call])?;
    }
    let finished = matches!(state, "Completed" | "Denied" | "Cancelled").then(clock_ms);
    tx.execute(
        "UPDATE model_queue SET state=?1,finished_ms=?2 WHERE seq=?3",
        params![state, finished, e.seq],
    )?;
    let mut updated = e.clone();
    updated.finished_ms = finished;
    event(
        tx,
        kind,
        &e.request.binding.task,
        evidence(&updated, state, reason),
    )
}
fn prune(tx: &Transaction<'_>) -> Result<()> {
    let stale = entries(
        tx,
        "WHERE state='Queued' AND (expires_ms<=?1 OR EXISTS(SELECT 1 FROM task t WHERE t.id=model_queue.task_id AND t.state!='Running') OR EXISTS(SELECT 1 FROM agent a WHERE a.id=model_queue.agent_id AND a.state!='Running') OR EXISTS(SELECT 1 FROM session s WHERE s.id=model_queue.runtime_session_id AND s.ended_at IS NOT NULL))",
        [clock_ms()],
    )?;
    for e in stale {
        change(
            tx,
            &e,
            "Cancelled",
            "model_queue_cancelled",
            "expired or inactive binding; never dispatched",
        )?;
    }
    Ok(())
}
fn queued_reply(tx: &Transaction<'_>, e: &Entry) -> Result<Value> {
    let mut reply =
        json!({"state":"Queued","permit":false,"call_id":e.request.call,"queue_seq":e.seq});
    // The original Mock FIFO's duplicate reply remains identical. Gateway
    // clients additionally need the remaining durable deadline, not a renewed
    // timeout each time they repeat admission.
    if crate::model_runtime::gateway_config(tx, &e.request.binding.model_id)?.is_some() {
        reply["wait_timeout_ms"] = json!((e.expires_ms - clock_ms()).clamp(1, 900_000));
    }
    Ok(reply)
}
pub(crate) fn enqueue(
    tx: &Transaction<'_>,
    binding: ModelBinding,
    turn: &str,
    call: &str,
    native: &str,
    provider: &str,
    model: &str,
) -> Result<Value> {
    prune(tx)?;
    let request = QueuedCall {
        binding,
        turn: turn.into(),
        call: call.into(),
        native: native.into(),
        provider: provider.into(),
        model: model.into(),
    };
    let existing = entries(tx, "WHERE call_id=?1", [call])?;
    if let Some(e) = existing.first() {
        if encode(&e.request)? != encode(&request)? {
            return Err(Error::Conflict);
        }
        if e.state != "Queued" {
            return Err(Error::Reconciliation);
        }
        return queued_reply(tx, e);
    }
    let count: i64 = tx.query_row(
        "SELECT count(*) FROM model_queue WHERE state='Queued'",
        [],
        |r| r.get(0),
    )?;
    if count >= MAX_PENDING {
        event(
            tx,
            "model_queue_denied",
            &request.binding.task,
            json!({"call_id":call,"agent_id":request.binding.agent,"model_id":request.binding.model_id,"reason":"shared queue capacity"}),
        )?;
        return Err(Error::Budget);
    }
    let now = clock_ms();
    let b = &request.binding;
    tx.execute("INSERT INTO model_queue(call_id,model_id,task_id,agent_id,runtime_session_id,binding_json,state,enqueued_ms,expires_ms) VALUES(?1,?2,?3,?4,?5,?6,'Queued',?7,?8)",params![call,b.model_id,b.task,b.agent,b.session,encode(&request)?,now,now+if request.binding.profile["gateway_wait_ms"].as_i64().is_some() {request.binding.profile["gateway_wait_ms"].as_i64().unwrap().clamp(1000,900_000)} else {QUEUE_LIFETIME_MS}])?;
    let e = entry(tx, call)?;
    event(
        tx,
        "model_enqueued",
        &b.task,
        evidence(&e, "Queued", "shared Owner-selected model route"),
    )?;
    queued_reply(tx, &e)
}
pub(crate) fn settled(tx: &Transaction<'_>, call: &str, result: &str) -> Result<()> {
    tx.execute(
        "UPDATE model_gateway_call SET state=?1 WHERE call_id=?2",
        params![
            if result == "Settled" {
                "Completed"
            } else {
                "Unknown"
            },
            call
        ],
    )?;
    for e in entries(
        tx,
        "WHERE call_id=?1 AND state IN ('Dispatched','Unknown')",
        [call],
    )? {
        change(
            tx,
            &e,
            if result == "Settled" {
                "Completed"
            } else {
                "Unknown"
            },
            "model_queue_result",
            result,
        )?;
    }
    Ok(())
}
pub(crate) fn recover_task(tx: &Transaction<'_>, task: &str) -> Result<()> {
    for e in entries(
        tx,
        "WHERE task_id=?1 AND state IN ('Queued','Dispatched')",
        [task],
    )? {
        let state = if e.state == "Queued" {
            "Cancelled"
        } else {
            "Unknown"
        };
        if state == "Unknown" {
            tx.execute("UPDATE model_reservation SET state='Unknown' WHERE call_id=?1 AND state='Reserved'",[&e.request.call])?;
        }
        change(
            tx,
            &e,
            state,
            "model_queue_recovered",
            "execution interrupted; no automatic provider replay",
        )?;
    }
    Ok(())
}
impl Registry {
    pub fn model_queue(&self, actor: &Actor) -> Result<Value> {
        core(actor)?;
        Ok(Value::Array(
            entries(&self.db, "", [])?
                .iter()
                .map(|e| evidence(e, &e.state, "inspection"))
                .collect(),
        ))
    }
    pub fn claim_model(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        native: &str,
    ) -> Result<Value> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        prune(&tx)?;
        let e = entry(&tx, call)?;
        if e.request.turn != turn || e.request.native != native {
            return Err(Error::Denied("queue binding".into()));
        }
        if e.state != "Queued" {
            if e.state == "Dispatched" || e.state == "Unknown" {
                return Err(Error::Reconciliation);
            }
            tx.commit()?;
            return Ok(json!({"state":e.state,"permit":false,"call_id":call}));
        }
        let b = model_binding(&tx, turn, native, &e.request.provider, &e.request.model)?;
        if !b.fifo || encode(&b)? != encode(&e.request.binding)? {
            return Err(Error::Conflict);
        }
        let frozen: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM model_queue WHERE model_id=?1 AND state='Unknown')",
            [&b.model_id],
            |r| r.get(0),
        )?;
        let active: u32 = tx.query_row(
            "SELECT count(*) FROM model_queue WHERE model_id=?1 AND state='Dispatched'",
            [&b.model_id],
            |r| r.get(0),
        )?;
        let spec = crate::model_runtime::gateway_config(&tx, &b.model_id)?;
        let capacity = if let Some(ref spec) = spec {
            tx.query_row("SELECT r.parallelism FROM model_runtime r JOIN model_instance i ON i.id=r.instance_id WHERE i.id=?1 AND i.state='Ready' AND r.verified=1",[spec["gateway"]["instance_id"].as_str().ok_or(Error::Conflict)?],|r|r.get::<_,u32>(0))?
        } else {
            1
        };
        // Canonical queue spans all Agents in this model route, ordered by durable
        // enqueue sequence. Reuse the existing Rust FIFO for actual head selection.
        let mut fifo = FifoQueue::<Entry>::default();
        let mut ordered = Vec::new();
        for queued in entries(&tx, "WHERE model_id=?1 AND state='Queued'", [&b.model_id])? {
            let priority: u32 = tx.query_row(
                "SELECT COALESCE((SELECT priority FROM model_task_priority WHERE task_id=?1),0)",
                [&queued.request.binding.task],
                |r| r.get(0),
            )?;
            ordered.push((priority, queued));
        }
        ordered.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.seq.cmp(&b.1.seq)));
        for (_, queued) in ordered {
            fifo.push(queued);
        }
        let head = fifo.pop_next().ok_or(Error::Conflict)?.1;
        if frozen || active >= capacity || head.seq != e.seq {
            tx.commit()?;
            return Ok(
                json!({"state":"Queued","permit":false,"call_id":call,"queue_seq":e.seq,"wait_timeout_ms":(e.expires_ms-clock_ms()).clamp(1,900_000)}),
            );
        }
        let permit = reserve_bound(&tx, &b, turn, call);
        let permit = match permit {
            Ok(p) => p,
            Err(Error::Budget) => {
                change(
                    &tx,
                    &e,
                    "Denied",
                    "model_queue_denied",
                    "budget admission failed before provider",
                )?;
                tx.commit()?;
                return Ok(json!({"state":"Denied","permit":false,"call_id":call}));
            }
            Err(error) => return Err(error),
        };
        let gateway_token = crate::model_runtime::prepare_permit(&tx, &b, call)?;
        let dispatched = clock_ms();
        tx.execute(
            "UPDATE model_queue SET state='Dispatched',dispatched_ms=?1 WHERE seq=?2",
            params![dispatched, e.seq],
        )?;
        let updated = entry(&tx, call)?;
        event(
            &tx,
            "model_dispatched",
            &b.task,
            evidence(
                &updated,
                "Dispatched",
                "single-use provider permit, not delivery confirmation",
            ),
        )?;
        tx.commit()?;
        Ok(
            json!({"state":"Dispatched","permit":true,"call_id":call,"queue_seq":e.seq,"queue_wait_ms":dispatched.saturating_sub(e.enqueued_ms).max(0),"reserved_tokens":permit["reserved_tokens"],"gateway_token":gateway_token}),
        )
    }
    pub fn cancel_model(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        native: &str,
    ) -> Result<Value> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let e = entry(&tx, call)?;
        if e.request.turn != turn || e.request.native != native {
            return Err(Error::Denied("queue binding".into()));
        }
        let state=match e.state.as_str() {
            "Queued"=>{
                change(&tx,&e,"Cancelled","model_queue_cancelled","caller cancelled before dispatch")?;
                "Cancelled"
            }
            "Dispatched"=>{
                change(&tx,&e,"Unknown","model_queue_recovered","caller cancelled after permit; delivery uncertain")?;
                tx.execute("UPDATE model_reservation SET state='Unknown' WHERE call_id=?1 AND state='Reserved'",[call])?;
                crate::model_budget::block_model_task(&tx,&e.request.binding.task,"cancel after model dispatch")?;
                "Unknown"
            }
            other=>other,
        }.to_owned();
        tx.commit()?;
        Ok(json!({"state":state,"call_id":call,"permit":false}))
    }
    /// Invoke only after acquiring the exclusive Core service lock, not on every
    /// DB connection. Unsent calls cancel; issued permits retain their budget/slot.
    pub fn recover_model_queue(&mut self, actor: &Actor) -> Result<()> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let pending = entries(&tx, "WHERE state IN ('Queued','Dispatched')", [])?;
        for e in pending {
            let next = if e.state == "Queued" {
                "Cancelled"
            } else {
                "Unknown"
            };
            change(
                &tx,
                &e,
                next,
                "model_queue_recovered",
                "Core service restarted; no replay",
            )?;
            if next == "Unknown" {
                tx.execute("UPDATE model_reservation SET state='Unknown' WHERE call_id=?1 AND state='Reserved'",[&e.request.call])?;
            }
            crate::model_budget::block_model_task(
                &tx,
                &e.request.binding.task,
                "Core restarted during model scheduling",
            )?;
        }
        tx.commit()?;
        Ok(())
    }
}
