//! Durable Task cancellation fence, independent of process and Session lifetime.
use crate::{registry::*, *};
use rusqlite::{TransactionBehavior, params};
use serde_json::json;

pub(crate) fn cancelled(db: &rusqlite::Connection, task: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='task_cancel_requested')",
        [task],
        |r| r.get(0),
    )?)
}
impl Registry {
    /// Commit the fence and conservative effect classification BEFORE transport cancellation.
    /// A lost reply is safe: repeat requests inspect the durable fence, never repeat actions.
    pub fn cancel_task(&mut self, actor: &Actor, id: &str, reason: &str) -> Result<Task> {
        core(actor)?;
        if reason.trim().is_empty() {
            return Err(Error::Invalid("cancellation reason required".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old: String = tx.query_row("SELECT state FROM task WHERE id=?1", [id], |r| r.get(0))?;
        if cancelled(&tx, id)? {
            tx.commit()?;
            return self.task(id);
        }
        if matches!(
            old.as_str(),
            "Done" | "Failed" | "Cancelled" | "ClosedUnverified"
        ) {
            return Err(Error::Conflict);
        }
        let children = {
            let mut q=tx.prepare("SELECT n.task_id FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.root=?1 AND n.task_id<>?1 AND t.state NOT IN('Done','Failed','Cancelled','ClosedUnverified')")?;
            q.query_map([id], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for child in children {
            if !cancelled(&tx, &child)? {
                let from: String =
                    tx.query_row("SELECT state FROM task WHERE id=?1", [&child], |r| r.get(0))?;
                cancel_one(&tx, &child, reason, &from)?;
            }
        }
        cancel_one(&tx, id, reason, &old)?;
        tx.commit()?;
        self.task(id)
    }
}

fn cancel_one(tx: &rusqlite::Transaction<'_>, id: &str, reason: &str, old: &str) -> Result<()> {
    // Serialize against model/tool admission using the same authoritative writer lock.
    tx.execute(
        "UPDATE task SET state='Blocked',block_reason='cancellation',version=version+1 WHERE id=?1",
        [id],
    )?;
    event(
        tx,
        "task_cancel_requested",
        id,
        json!({"reason":reason,"from":old,"new_calls":"denied","automatic_retry":false}),
    )?;
    crate::model_queue::recover_task(tx, id)?;
    let n = tx.execute(
        "UPDATE model_reservation SET state='Unknown' WHERE task_id=?1 AND state='Reserved'",
        [id],
    )?;
    if n > 0 {
        event(
            tx,
            "model_cancel_unknown",
            id,
            json!({"calls":n,"refund":false}),
        )?;
    }
    crate::tool_execution::recover_task_actions(tx, id)?;
    let pending:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown'))",[id],|r|r.get(0))?;
    let next = if pending
        || crate::model_budget::unresolved_model_calls(tx, id)?
        || crate::collaboration::unresolved_children(tx, id)?
    {
        "Blocked"
    } else {
        "Cancelled"
    };
    tx.execute(
        "UPDATE task SET state=?1,block_reason=?2 WHERE id=?3",
        params![
            next,
            if next == "Blocked" {
                Some("reconciliation")
            } else {
                None
            },
            id
        ],
    )?;
    event(
        tx,
        "state_change",
        id,
        json!({"from":old,"to":next,"reason":"cancel requested; dispatch absence required for closure","counts_as_success":false}),
    )?;

    Ok(())
}
