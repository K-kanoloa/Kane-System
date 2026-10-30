//! Trusted-host lifecycle: Agent identity is independent of process and Session.
use crate::{
    registry::{core, event},
    *,
};
use rusqlite::{OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::{Value, json};

fn configured(db: &rusqlite::Connection, id: &str) -> Result<Option<Value>> {
    let raw: Option<String> = db.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='runtime_configured' ORDER BY seq DESC LIMIT 1",[id],|r|r.get(0)).optional()?;
    raw.map(|s| serde_json::from_str(&s).map_err(Error::from))
        .transpose()
}
fn control(db: &rusqlite::Connection, id: &str) -> Result<Option<Value>> {
    Ok(db.query_row("SELECT owner_token,attempt,phase FROM runtime_control WHERE agent_id=?1",[id],|r|Ok(json!({"owner_token":r.get::<_,Option<String>>(0)?,"attempt":r.get::<_,u64>(1)?,"phase":r.get::<_,String>(2)?}))).optional()?)
}
fn limit(config: &Value) -> Result<u64> {
    config
        .get("max_restarts")
        .unwrap_or(&json!(0))
        .as_u64()
        .filter(|n| *n <= 100)
        .map(|n| n + 1)
        .ok_or_else(|| Error::Invalid("max_restarts must be 0..100".into()))
}
fn apply(
    tx: &Transaction<'_>,
    old: &Agent,
    version: u64,
    next: AgentState,
    data: Value,
) -> Result<()> {
    if !(old.state.permits(next) || old.state == AgentState::Ready && next == AgentState::Ready) {
        return Err(Error::Conflict);
    }
    if tx.execute(
        "UPDATE agent SET state=?1,version=version+1 WHERE id=?2 AND version=?3",
        params![format!("{next:?}"), old.id, version],
    )? != 1
    {
        return Err(Error::Conflict);
    }
    let kind = data["event"]
        .as_str()
        .ok_or_else(|| Error::Invalid("event".into()))?;
    if kind != "cancelling" {
        tx.execute(
            "UPDATE session SET ended_at=?1 WHERE agent_id=?2 AND ended_at IS NULL",
            params![now(), old.id],
        )?;
    }
    if kind == "started" {
        let pid = data["pid"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or_else(|| Error::Invalid("pid required".into()))?;
        tx.execute(
            "INSERT INTO session VALUES(?1,?2,?3,?4,?5,NULL)",
            params![
                new_id("session"),
                old.id,
                old.task_id,
                format!("process:{pid}"),
                now()
            ],
        )?;
    }
    if !matches!(kind, "started" | "starting" | "prepared") {
        crate::model_queue::recover_task(tx, &old.task_id)?;
        crate::tool_execution::recover_task_actions(tx, &old.task_id)?;
        let held = tx.execute(
            "UPDATE model_reservation SET state='Unknown' WHERE task_id=?1 AND state='Reserved'",
            [&old.task_id],
        )?;
        if held > 0 {
            event(
                tx,
                "model_reservations_uncertain",
                &old.task_id,
                json!({"runtime_event":kind,"count":held,"automatic_refund":false}),
            )?;
        }
        let previous: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [&old.task_id], |r| {
                r.get(0)
            })?;
        let changed=tx.execute("UPDATE task SET state='Blocked',block_reason='reconciliation',version=version+1 WHERE id=?1 AND (state='Running' OR (state='Blocked' AND block_reason='human')) AND EXISTS(SELECT 1 FROM harness_binding WHERE task_id=?1)",[&old.task_id])?;
        if changed > 0 {
            event(
                tx,
                "state_change",
                &old.task_id,
                json!({"from":previous,"to":"Blocked","reason":"reconciliation","runtime_event":kind,"automatic_retry":false}),
            )?;
        }
    }
    event(
        tx,
        "runtime_lifecycle",
        &old.id,
        json!({"from":old.state,"to":next,"detail":data}),
    )
}
impl Registry {
    pub fn runtime_status(&self, id: &str) -> Result<Value> {
        let last:Option<String>=self.db.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='runtime_lifecycle' ORDER BY seq DESC LIMIT 1",[id],|r|r.get(0)).optional()?;
        Ok(
            json!({"agent":self.agent(id)?,"config":configured(&self.db,id)?,"control":control(&self.db,id)?,"last":last.map(|s|serde_json::from_str::<Value>(&s)).transpose()?}),
        )
    }
    pub fn configure_runtime(&mut self, actor: &Actor, id: &str, config: Value) -> Result<Value> {
        core(actor)?;
        self.agent(id)?;
        limit(&config)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        match configured(&tx, id)? {
            Some(previous) if previous != config => return Err(Error::Conflict),
            Some(_) => {}
            None => {
                tx.execute("INSERT INTO runtime_control(agent_id) VALUES(?1)", [id])?;
                event(&tx, "runtime_configured", id, config)?;
            }
        }
        tx.commit()?;
        self.runtime_status(id)
    }
    /// Caller holds the original kernel ownership lock and has verified absence.
    /// This trusted-host assertion is not accepted from the Agent relay.
    pub fn acquire_runtime(
        &mut self,
        actor: &Actor,
        id: &str,
        absence_confirmed: bool,
    ) -> Result<Value> {
        core(actor)?;
        if !absence_confirmed {
            return Err(Error::Denied(
                "confirmed old runtime absence required".into(),
            ));
        }
        let old = self.agent(id)?;
        if matches!(
            old.state,
            AgentState::Failed | AgentState::Exited | AgentState::Terminated
        ) {
            return Err(Error::Denied(
                "terminal Agent; no reset/recovery API".into(),
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let c = control(&tx, id)?
            .ok_or_else(|| Error::Invalid("runtime configuration required".into()))?;
        let token = new_id("runtime-owner");
        let next = if old.state == AgentState::Cancelling {
            AgentState::Terminated
        } else {
            AgentState::Ready
        };
        let kind = if old.state == AgentState::Created || old.state == AgentState::Suspended {
            "prepared"
        } else {
            "supervisor_recovered"
        };
        apply(
            &tx,
            &old,
            old.version,
            next,
            json!({"event":kind,"owner_token":token,"attempt":c["attempt"],"reason":"kernel lock held; previous runtime absence confirmed; no task replay"}),
        )?;
        tx.execute(
            "UPDATE runtime_control SET owner_token=?1,phase=?2 WHERE agent_id=?3",
            params![
                token,
                if next == AgentState::Terminated {
                    "closed"
                } else {
                    "idle"
                },
                id
            ],
        )?;
        tx.commit()?;
        self.runtime_status(id)
    }
    /// Durably consume a lifetime launch attempt BEFORE spawning any process.
    pub fn begin_runtime(&mut self, actor: &Actor, id: &str, token: &str) -> Result<Value> {
        core(actor)?;
        let old = self.agent(id)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let c = control(&tx, id)?
            .ok_or_else(|| Error::Invalid("configured runtime required".into()))?;
        if c["owner_token"] != token || c["phase"] != "idle" || old.state != AgentState::Ready {
            return Err(Error::Conflict);
        }
        let terminal: bool = tx.query_row("SELECT state IN ('Done','Failed','Cancelled','ClosedUnverified') FROM task WHERE id=?1",[&old.task_id],|r|r.get(0))?;
        if terminal {
            return Err(Error::Invalid(
                "terminal task cannot consume a launch permit".into(),
            ));
        }
        let previous = c["attempt"].as_u64().ok_or(Error::Conflict)?;
        let cap = limit(&configured(&tx, id)?.ok_or(Error::Conflict)?)?;
        if previous >= cap {
            apply(
                &tx,
                &old,
                old.version,
                AgentState::Failed,
                json!({"event":"restart_exhausted","attempt":previous,"owner_token":token,"reason":"persistent lifetime allowance exhausted"}),
            )?;
            tx.execute(
                "UPDATE runtime_control SET phase='closed' WHERE agent_id=?1",
                [id],
            )?;
            tx.commit()?;
            return Ok(json!({"allowed":false,"attempt":previous}));
        }
        let attempt = previous + 1;
        apply(
            &tx,
            &old,
            old.version,
            AgentState::Ready,
            json!({"event":"starting","attempt":attempt,"restart_count":attempt-1,"owner_token":token}),
        )?;
        tx.execute(
            "UPDATE runtime_control SET attempt=?1,phase='launching' WHERE agent_id=?2",
            params![attempt, id],
        )?;
        tx.commit()?;
        Ok(json!({"allowed":true,"attempt":attempt,"owner_token":token}))
    }
    pub fn runtime_event(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        mut data: Value,
    ) -> Result<Value> {
        core(actor)?;
        if control(&self.db, id)?.is_none() {
            return self.legacy_runtime_event(actor, id, version, data);
        }
        let old = self.agent(id)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let c = control(&tx, id)?.ok_or(Error::Conflict)?;
        if data["owner_token"].as_str().is_none()
            || data["owner_token"] != c["owner_token"]
            || data["attempt"] != c["attempt"]
        {
            return Err(Error::Denied(
                "stale runtime owner or process attempt".into(),
            ));
        }
        let attempt = c["attempt"].as_u64().ok_or(Error::Conflict)?;
        let cap = limit(&configured(&tx, id)?.ok_or(Error::Conflict)?)?;
        let kind = data["event"].as_str().ok_or(Error::Conflict)?;
        let (required, phase, next) = match kind {
            "started" => ("launching", "running", AgentState::Running),
            "start_failed" => (
                "launching",
                if attempt >= cap { "closed" } else { "idle" },
                if attempt >= cap {
                    AgentState::Failed
                } else {
                    AgentState::Ready
                },
            ),
            "process_exited" => {
                let success = data["success"]
                    .as_bool()
                    .ok_or_else(|| Error::Invalid("process success required".into()))?;
                (
                    "running",
                    if !success && attempt >= cap {
                        "closed"
                    } else {
                        "idle"
                    },
                    if success {
                        AgentState::Suspended
                    } else if attempt >= cap {
                        AgentState::Failed
                    } else {
                        AgentState::Ready
                    },
                )
            }
            "stopped" => ("running", "idle", AgentState::Suspended),
            "completed" => ("running", "closed", AgentState::Exited),
            "cancelling" => ("running", "cancelling", AgentState::Cancelling),
            "terminated" => ("cancelling", "closed", AgentState::Terminated),
            _ => return Err(Error::Invalid("unsupported managed runtime event".into())),
        };
        if c["phase"] != required {
            return Err(Error::Conflict);
        }
        if kind == "started" {
            let terminal:bool=tx.query_row("SELECT state IN ('Done','Failed','Cancelled','ClosedUnverified') FROM task WHERE id=?1",[&old.task_id],|r|r.get(0))?;
            if terminal {
                return Err(Error::Invalid("terminal task cannot start runtime".into()));
            }
        }
        data["restart_count"] = json!(attempt.saturating_sub(1));
        data["restart_decision"] = json!(if next == AgentState::Failed {
            "exhausted"
        } else if next == AgentState::Ready {
            "bounded_retry"
        } else {
            "no_automatic_retry"
        });
        apply(&tx, &old, version, next, data)?;
        tx.execute(
            "UPDATE runtime_control SET phase=?1 WHERE agent_id=?2",
            params![phase, id],
        )?;
        tx.commit()?;
        self.runtime_status(id)
    }
    /// State, process metadata, execution segment and ledger commit together.
    /// Starting a fresh process never resumes or completes the bound Task.
    fn legacy_runtime_event(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        data: Value,
    ) -> Result<Value> {
        core(actor)?;
        let old = self.agent(id)?;
        let kind = data["event"]
            .as_str()
            .ok_or_else(|| Error::Invalid("event required".into()))?;
        let next = match kind {
            "started" => AgentState::Running,
            "prepared" | "exited" | "supervisor_recovered" | "start_failed" => AgentState::Ready,
            "stopped" => AgentState::Suspended,
            _ => return Err(Error::Invalid("unknown runtime event".into())),
        };
        if !(old.state.permits(next) || old.state == AgentState::Ready && next == AgentState::Ready)
        {
            return Err(Error::Conflict);
        }
        if kind == "started" && self.task(&old.task_id)?.state.terminal() {
            return Err(Error::Invalid(
                "terminal task cannot start a runtime".into(),
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        if control(&tx, id)?.is_some() {
            return Err(Error::Denied(
                "managed runtime requires generation protocol".into(),
            ));
        }
        if tx.execute(
            "UPDATE agent SET state=?1,version=version+1 WHERE id=?2 AND version=?3",
            params![format!("{next:?}"), id, version],
        )? != 1
        {
            return Err(Error::Conflict);
        }
        tx.execute(
            "UPDATE session SET ended_at=?1 WHERE agent_id=?2 AND ended_at IS NULL",
            params![now(), id],
        )?;
        if kind == "started" {
            let pid = data["pid"]
                .as_u64()
                .filter(|p| *p > 0)
                .ok_or_else(|| Error::Invalid("pid required".into()))?;
            tx.execute(
                "INSERT INTO session VALUES(?1,?2,?3,?4,?5,NULL)",
                params![
                    new_id("session"),
                    id,
                    old.task_id,
                    format!("process:{pid}"),
                    now()
                ],
            )?;
        }
        if kind != "started" && kind != "prepared" {
            crate::model_queue::recover_task(&tx, &old.task_id)?;
            crate::tool_execution::recover_task_actions(&tx, &old.task_id)?;
            let held=tx.execute("UPDATE model_reservation SET state='Unknown' WHERE task_id=?1 AND state='Reserved'",[&old.task_id])?;
            if held > 0 {
                event(
                    &tx,
                    "model_reservations_uncertain",
                    &old.task_id,
                    json!({"runtime_event":kind,"count":held,"automatic_refund":false}),
                )?;
            }

            let previous_task_state: String =
                tx.query_row("SELECT state FROM task WHERE id=?1", [&old.task_id], |r| {
                    r.get(0)
                })?;
            let changed=tx.execute("UPDATE task SET state='Blocked',block_reason='reconciliation',version=version+1 WHERE id=?1 AND (state='Running' OR (state='Blocked' AND block_reason='human')) AND EXISTS(SELECT 1 FROM harness_binding WHERE task_id=?1)",[&old.task_id])?;
            if changed > 0 {
                event(
                    &tx,
                    "state_change",
                    &old.task_id,
                    json!({"from":previous_task_state,"to":"Blocked","reason":"reconciliation","runtime_event":kind,"automatic_retry":false}),
                )?;
            }
        }
        event(
            &tx,
            "runtime_lifecycle",
            id,
            json!({"from":old.state,"to":next,"detail":data}),
        )?;
        tx.commit()?;
        self.runtime_status(id)
    }
}
