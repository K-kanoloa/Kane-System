use crate::{registry::*, *};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

impl Registry {
    /// Persists intent before a trusted dispatcher can claim it. No network tools live here.
    pub fn prepare_action(
        &mut self,
        actor: &Actor,
        task_id: &str,
        key: &str,
        target: &str,
        payload: Value,
    ) -> Result<OutboxAction> {
        let agent = match actor {
            Actor::Agent(id) => id,
            _ => return Err(Error::Denied("Agent identity required".into())),
        };
        let a = self.agent(agent)?;
        let t = self.task(task_id)?;
        if a.task_id != task_id
            || a.state != AgentState::Running
            || t.state != TaskState::Running
            || key.is_empty()
        {
            return Err(Error::Denied("inactive or unbound task".into()));
        }
        let body = json!({"target":target,"body":payload});
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let active:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM agent a JOIN task t ON t.id=a.task_id WHERE a.id=?1 AND t.id=?2 AND a.state='Running' AND t.state='Running')",params![agent,task_id],|r|r.get(0))?;
        if !active {
            return Err(Error::Denied("inactive task/agent".into()));
        }
        crate::collaboration::admission(&tx, task_id, Some(agent))?;
        crate::tool_execution::validate_managed(&tx, agent, task_id, &body)?;
        Self::check_capability(&tx, agent, "execute", target)?;
        let prior = tx
            .query_row(
                "SELECT id,task_id,agent_id,payload_json FROM outbox WHERE idempotency_key=?1",
                [key],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()?;
        if let Some((id, task, producer, old)) = prior {
            if task != task_id || producer != *agent || decode::<Value>(&old)? != body {
                return Err(Error::Conflict);
            }
            tx.commit()?;
            return self.action(&id);
        }
        let id = new_id("action");
        tx.execute("INSERT INTO outbox(id,task_id,agent_id,idempotency_key,payload_json,state,created_at) VALUES(?1,?2,?3,?4,?5,'Pending',?6)",params![id,task_id,agent,key,encode(&body)?,now()])?;
        event(
            &tx,
            "intent",
            task_id,
            json!({"action":id,"key":key,"target":target}),
        )?;
        tx.commit()?;
        self.action(&id)
    }

    pub fn action(&self, id: &str) -> Result<OutboxAction> {
        let row = self
            .db
            .query_row(
                "SELECT task_id,idempotency_key,state,payload_json FROM outbox WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get::<_, String>(3)?)),
            )
            .optional()?
            .ok_or_else(|| Error::NotFound(id.into()))?;
        Ok(OutboxAction {
            id: id.into(),
            task_id: row.0,
            idempotency_key: row.1,
            state: row.2,
            payload: decode(&row.3)?,
        })
    }

    /// Claim is committed before any external call. Single-dispatcher experimental protocol.
    /// On dispatcher loss call recover_actions before accepting more work.
    pub fn claim_action(&mut self, actor: &Actor, id: &str) -> Result<OutboxAction> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row = tx
            .query_row(
                "SELECT task_id,agent_id,payload_json FROM outbox WHERE id=?1 AND state='Pending'",
                [id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()?
            .ok_or(Error::Reconciliation)?;
        let p: Value = decode(&row.2)?;
        let target = p["target"]
            .as_str()
            .ok_or_else(|| Error::Invalid("action target".into()))?;
        crate::collaboration::admission(&tx, &row.0, Some(&row.1))?;
        crate::tool_execution::validate_managed(&tx, &row.1, &row.0, &p)?;
        Self::check_capability(&tx, &row.1, "execute", target)?;
        let state: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [&row.0], |r| r.get(0))?;
        if state != "Running" {
            return Err(Error::Denied("task not running".into()));
        }
        tx.execute("UPDATE outbox SET state='Dispatching' WHERE id=?1", [id])?;
        event(
            &tx,
            "dispatch",
            &row.0,
            json!({"action":id,"phase":"prepared; receipt unknown"}),
        )?;
        tx.commit()?;
        self.action(id)
    }

    pub fn confirm_action(&mut self, actor: &Actor, id: &str, evidence: &str) -> Result<()> {
        core(actor)?;
        if evidence.trim().is_empty() {
            return Err(Error::Invalid("result evidence required".into()));
        }
        let tx = self.db.transaction()?;
        if tx.execute("UPDATE outbox SET state='Confirmed',result_json=?1 WHERE id=?2 AND state='Dispatching'",params![encode(&json!({"evidence":evidence}))?,id])?!=1 {return Err(Error::Conflict);}
        event(&tx, "result", id, json!({"evidence":evidence}))?;
        tx.commit()?;
        Ok(())
    }

    /// Unknown effects are never replayed. Pending intents were never claimed and remain pending.
    pub fn recover_actions(&mut self, actor: &Actor) -> Result<usize> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let pending = {
            let mut q = tx.prepare("SELECT id,task_id FROM outbox WHERE state='Dispatching'")?;
            q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for (id, task) in &pending {
            tx.execute("UPDATE outbox SET state='Unknown' WHERE id=?1", [id])?;
            tx.execute(
                "INSERT INTO reconciliation VALUES(?1,?2,'Unknown',NULL)",
                params![new_id("reconcile"), id],
            )?;
            tx.execute("UPDATE task SET state='Blocked',block_reason='reconciliation',version=version+1 WHERE id=?1",[task])?;
            event(
                &tx,
                "reconcile",
                task,
                json!({"action":id,"status":"Unknown","automatic_retry":false}),
            )?;
        }
        tx.commit()?;
        Ok(pending.len())
    }

    /// Owner provides externally checked evidence. NotExecuted does not automatically retry.
    pub fn reconcile_action(
        &mut self,
        actor: &Actor,
        id: &str,
        executed: bool,
        evidence: &str,
    ) -> Result<()> {
        owner(actor)?;
        if evidence.trim().is_empty() {
            return Err(Error::Invalid("remote evidence required".into()));
        }
        let tx = self.db.transaction()?;
        if tx.execute(
            "UPDATE outbox SET state=?1,result_json=?2 WHERE id=?3 AND state='Unknown'",
            params![
                if executed { "Confirmed" } else { "NotExecuted" },
                encode(&json!({"evidence":evidence}))?,
                id
            ],
        )? != 1
        {
            return Err(Error::Conflict);
        }
        tx.execute(
            "UPDATE reconciliation SET status='Resolved',evidence_ref=?1 WHERE outbox_id=?2",
            params![evidence, id],
        )?;
        event(
            &tx,
            "reconcile",
            id,
            json!({"executed":executed,"evidence":evidence,"automatic_retry":false}),
        )?;
        tx.commit()?;
        Ok(())
    }
}
