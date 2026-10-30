use crate::*;
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::Path;

pub struct Registry {
    pub(crate) db: Connection,
}

pub(crate) fn encode<T: serde::Serialize>(v: &T) -> Result<String> {
    Ok(serde_json::to_string(v)?)
}
pub(crate) fn decode<T: serde::de::DeserializeOwned>(v: &str) -> Result<T> {
    Ok(serde_json::from_str(v)?)
}
pub(crate) fn event(tx: &Transaction<'_>, kind: &str, id: &str, payload: Value) -> Result<()> {
    tx.execute(
        "INSERT INTO ledger(ts,kind,object_id,payload_json) VALUES(?1,?2,?3,?4)",
        params![now(), kind, id, encode(&payload)?],
    )?;
    Ok(())
}
pub(crate) fn owner(actor: &Actor) -> Result<()> {
    if *actor == Actor::Owner {
        Ok(())
    } else {
        Err(Error::Denied("Owner required".into()))
    }
}
pub(crate) fn core(actor: &Actor) -> Result<()> {
    if matches!(actor, Actor::Owner | Actor::Core) {
        Ok(())
    } else {
        Err(Error::Denied("trusted core required".into()))
    }
}
pub(crate) fn digest(content: &[u8]) -> String {
    format!("{:x}", Sha256::digest(content))
}

impl Registry {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch(
            "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
        )?;
        let current: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if current > 9 {
            return Err(Error::Invalid("newer schema; rollback unsupported".into()));
        }
        let mut this = Self { db };
        let tx = this
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("schema.sql"))?;
        tx.execute_batch(include_str!("schema-v2.sql"))?;
        tx.execute_batch(include_str!("schema-v3.sql"))?;
        tx.execute_batch(include_str!("schema-v4.sql"))?;
        tx.execute_batch(include_str!("schema-v5.sql"))?;
        tx.execute_batch(include_str!("schema-v6.sql"))?;
        tx.execute_batch(include_str!("schema-v7.sql"))?;
        tx.execute_batch(include_str!("schema-v8.sql"))?;
        tx.execute_batch(include_str!("schema-v9.sql"))?;
        tx.pragma_update(None, "user_version", 9)?;
        tx.commit()?;
        Ok(this)
    }

    pub fn create_budget(
        &mut self,
        actor: &Actor,
        token_limit: u64,
        max_concurrency: u32,
    ) -> Result<String> {
        owner(actor)?;
        if token_limit > i64::MAX as u64 || max_concurrency == 0 {
            return Err(Error::Invalid("budget bounds".into()));
        }
        let id = new_id("budget");
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO resource_budget(id,token_limit,max_concurrency) VALUES(?1,?2,?3)",
            params![id, token_limit, max_concurrency],
        )?;
        event(
            &tx,
            "budget_created",
            &id,
            json!({"tokens":token_limit,"concurrency":max_concurrency}),
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn register_model(&mut self, actor: &Actor, id: &str, spec: Value) -> Result<()> {
        owner(actor)?;
        if id.is_empty() || !spec.is_object() {
            return Err(Error::Invalid("model id".into()));
        }
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO model VALUES(?1,?2)",
            params![id, encode(&spec)?],
        )?;
        event(&tx, "model_registered", id, json!({"id":id}))?;
        tx.commit()?;
        Ok(())
    }

    pub fn register_model_instance(
        &mut self,
        actor: &Actor,
        model: &str,
        node: &str,
    ) -> Result<String> {
        core(actor)?;
        let id = new_id("instance");
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO model_instance VALUES(?1,?2,?3,'Loading')",
            params![id, model, node],
        )?;
        event(
            &tx,
            "model_instance_registered",
            &id,
            json!({"model":model,"node":node}),
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn create_task(&mut self, actor: &Actor, spec: TaskSpec) -> Result<Task> {
        owner(actor)?;
        spec.validate()?;
        let id = new_id("task");
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO task(id,state,budget_id,workspace_id,workload_class,spec_json) VALUES(?1,'Pending',?2,?3,?4,?5)",
            params![id,spec.budget_id,spec.workspace_id,encode(&spec.workload.class)?.trim_matches('"'),encode(&spec)?])?;
        event(
            &tx,
            "task_created",
            &id,
            json!({"state":"Pending","workload":spec.workload.class,"mode":"Direct"}),
        )?;
        tx.commit()?;
        self.task(&id)
    }

    pub fn task(&self, id: &str) -> Result<Task> {
        let row=self.db.query_row("SELECT state,verification_state,block_reason,version,spec_json FROM task WHERE id=?1",[id],|r|
            Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get(2)?,r.get(3)?,r.get::<_,String>(4)?)))
            .optional()?.ok_or_else(||Error::NotFound(id.into()))?;
        Ok(Task {
            id: id.into(),
            state: decode(&format!("\"{}\"", row.0))?,
            verification_state: decode(&format!("\"{}\"", row.1))?,
            block_reason: row.2,
            version: row.3,
            spec: decode(&row.4)?,
        })
    }

    pub fn tasks(&self) -> Result<Vec<Task>> {
        let mut q = self.db.prepare("SELECT id FROM task ORDER BY rowid")?;
        let ids = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        ids.iter().map(|id| self.task(id)).collect()
    }

    pub fn transition_task(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        next: TaskState,
        reason: Option<&str>,
    ) -> Result<Task> {
        core(actor)?;
        if matches!(next, TaskState::Done | TaskState::ClosedUnverified) {
            return Err(Error::Denied(
                "use verification or explicit unverified acceptance".into(),
            ));
        }
        let task = self.task(id)?;
        if task.version != version {
            return Err(Error::Conflict);
        }
        if !task.state.permits(next) {
            return Err(Error::Invalid("illegal task transition".into()));
        }
        if task.state == TaskState::Blocked
            && next == TaskState::Verifying
            && task.block_reason.as_deref() != Some("awaiting-verification")
        {
            return Err(Error::Denied(
                "resolve the actual block before verification".into(),
            ));
        }
        if next == TaskState::Blocked && reason.is_none_or(str::is_empty) {
            return Err(Error::Invalid("block reason required".into()));
        }
        let tx = self.db.transaction()?;
        if next == TaskState::Running {
            crate::collaboration::admission(&tx, id, None)?;
        }
        if crate::recovery::review_required(&tx, id)?
            && !matches!(next, TaskState::Cancelled | TaskState::Blocked)
        {
            return Err(Error::Denied(
                "restored Task requires explicit checkpoint/history review".into(),
            ));
        }
        if crate::cancellation::cancelled(&tx, id)?
            && !matches!(next, TaskState::Cancelled | TaskState::Blocked)
        {
            return Err(Error::Denied(
                "Task cancellation fence is permanent; no replay or verification".into(),
            ));
        }
        if next != TaskState::Blocked && crate::collaboration::unresolved_children(&tx, id)? {
            return Err(Error::Reconciliation);
        }
        let unresolved: i64 = tx.query_row(
            "SELECT count(*) FROM outbox WHERE task_id=?1 AND state IN ('Dispatching','Unknown')",
            [id],
            |r| r.get(0),
        )?;
        if (unresolved > 0 || crate::model_budget::unresolved_model_calls(&tx, id)?)
            && next != TaskState::Blocked
        {
            return Err(Error::Reconciliation);
        }
        // Pending intents can be safely cancelled; dispatching/unknown actions must first be reconciled.
        if matches!(next, TaskState::Cancelled | TaskState::Failed) {
            tx.execute(
                "UPDATE outbox SET state='NotExecuted' WHERE task_id=?1 AND state='Pending'",
                [id],
            )?;
        }
        let reason = if next == TaskState::Blocked {
            reason
        } else {
            None
        };
        let verification = if next == TaskState::Running {
            "pending".to_owned()
        } else {
            encode(&task.verification_state)?
                .trim_matches('"')
                .to_owned()
        };
        let n=tx.execute("UPDATE task SET state=?1,version=version+1,block_reason=?2,verification_state=?3 WHERE id=?4 AND version=?5",
            params![format!("{next:?}"),reason,verification,id,version])?;
        if n != 1 {
            return Err(Error::Conflict);
        }
        event(
            &tx,
            "state_change",
            id,
            json!({"from":task.state,"to":next,"version":version+1,"reason":reason}),
        )?;
        tx.commit()?;
        self.task(id)
    }

    pub fn register_agent(
        &mut self,
        actor: &Actor,
        role: &str,
        task_id: &str,
        model_id: &str,
    ) -> Result<Agent> {
        owner(actor)?;
        let task = self.task(task_id)?;
        if task.state.terminal() {
            return Err(Error::Invalid("terminal task".into()));
        }
        let agent = Agent {
            id: new_id("agent"),
            role: role.into(),
            task_id: task_id.into(),
            workspace_id: task.spec.workspace_id,
            budget_id: task.spec.budget_id,
            model_id: model_id.into(),
            state: AgentState::Created,
            version: 0,
        };
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO agent(id,role,task_id,workspace_id,budget_id,model_id,state) VALUES(?1,?2,?3,?4,?5,?6,'Created')",
            params![agent.id,role,task_id,agent.workspace_id,agent.budget_id,model_id])?;
        event(
            &tx,
            "agent_registered",
            &agent.id,
            json!({"task":task_id,"workspace":agent.workspace_id,"budget":agent.budget_id}),
        )?;
        tx.commit()?;
        Ok(agent)
    }

    pub fn agent(&self, id: &str) -> Result<Agent> {
        let a=self.db.query_row("SELECT role,task_id,workspace_id,budget_id,model_id,state,version FROM agent WHERE id=?1",[id],|r|
            Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get::<_,String>(5)?,r.get(6)?))).optional()?.ok_or_else(||Error::NotFound(id.into()))?;
        Ok(Agent {
            id: id.into(),
            role: a.0,
            task_id: a.1,
            workspace_id: a.2,
            budget_id: a.3,
            model_id: a.4,
            state: decode(&format!("\"{}\"", a.5))?,
            version: a.6,
        })
    }

    pub fn transition_agent(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        next: AgentState,
    ) -> Result<Agent> {
        core(actor)?;
        let old = self.agent(id)?;
        if !old.state.permits(next) {
            return Err(Error::Invalid("illegal agent transition".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let managed: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM runtime_control WHERE agent_id=?1)",
            [id],
            |r| r.get(0),
        )?;
        if managed {
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
        event(
            &tx,
            "state_change",
            id,
            json!({"from":old.state,"to":next,"version":version+1}),
        )?;
        tx.commit()?;
        self.agent(id)
    }

    pub fn start_session(
        &mut self,
        actor: &Actor,
        agent_id: &str,
        runtime_ref: &str,
    ) -> Result<String> {
        core(actor)?;
        let a = self.agent(agent_id)?;
        if a.state != AgentState::Running {
            return Err(Error::Invalid("agent is not running".into()));
        }
        let id = new_id("session");
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE session SET ended_at=?1 WHERE agent_id=?2 AND ended_at IS NULL",
            params![now(), agent_id],
        )?;
        tx.execute(
            "INSERT INTO session VALUES(?1,?2,?3,?4,?5,NULL)",
            params![id, agent_id, a.task_id, runtime_ref, now()],
        )?;
        event(
            &tx,
            "session_started",
            &id,
            json!({"agent":agent_id,"task":a.task_id,"runtime":runtime_ref}),
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn submit_artifact(
        &mut self,
        actor: &Actor,
        task_id: &str,
        format: &str,
        editable: bool,
        content: &str,
    ) -> Result<Artifact> {
        let producer = match actor {
            Actor::Agent(id) => id,
            _ => return Err(Error::Denied("bound Agent submission required".into())),
        };
        let a = self.agent(producer)?;
        let t = self.task(task_id)?;
        if a.task_id != task_id
            || a.state != AgentState::Running
            || t.state != TaskState::Running
            || content.is_empty()
            || format.is_empty()
        {
            return Err(Error::Denied("invalid or inactive producer/task".into()));
        }
        let artifact = Artifact {
            id: new_id("artifact"),
            task_id: task_id.into(),
            producer_id: producer.clone(),
            format: format.into(),
            editable,
            content: content.into(),
            content_hash: digest(content.as_bytes()),
            verification_state: VerificationState::Pending,
        };
        let tx = self.db.transaction()?;
        let active: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM task t JOIN agent a ON a.task_id=t.id WHERE t.id=?1 AND a.id=?2 AND t.state='Running' AND a.state='Running')",params![task_id,producer],|r|r.get(0))?;
        if !active || crate::cancellation::cancelled(&tx, task_id)? {
            return Err(Error::Conflict);
        }
        crate::resources::check_task_storage(
            &tx,
            task_id,
            &t.spec.workspace_id,
            content.len() as u64,
        )?;
        tx.execute("INSERT INTO artifact(id,task_id,producer_id,format,editable,content,content_hash) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![artifact.id,task_id,producer,format,editable,content,artifact.content_hash])?;
        event(
            &tx,
            "artifact_submitted",
            &artifact.id,
            json!({"task":task_id,"hash":artifact.content_hash,"producer":producer}),
        )?;
        tx.commit()?;
        Ok(artifact)
    }

    pub fn artifacts(&self, task_id: &str) -> Result<Vec<Artifact>> {
        let mut q=self.db.prepare("SELECT id,producer_id,format,editable,content,content_hash,verification_state FROM artifact WHERE task_id=?1 ORDER BY rowid")?;
        let rows = q
            .query_map([task_id], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get::<_, String>(6)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|r| {
                Ok(Artifact {
                    id: r.0,
                    task_id: task_id.into(),
                    producer_id: r.1,
                    format: r.2,
                    editable: r.3,
                    content: r.4,
                    content_hash: r.5,
                    verification_state: decode(&format!("\"{}\"", r.6))?,
                })
            })
            .collect()
    }

    /// The trusted verifier supplies evidence derived from its own acceptance procedure.
    /// No assertion here can prove arbitrary semantic quality; verifier quality is measured separately.
    pub fn verify(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        passed: bool,
        evidence: &str,
    ) -> Result<Task> {
        self.verify_with_session(actor, None, id, version, passed, evidence)
    }
    pub fn control_verify(
        &mut self,
        actor: &Actor,
        token: &str,
        id: &str,
        version: u64,
        passed: bool,
        evidence: &str,
    ) -> Result<Task> {
        owner(actor)?;
        self.verify_with_session(actor, Some(token), id, version, passed, evidence)
    }
    fn verify_with_session(
        &mut self,
        actor: &Actor,
        owner_session_token: Option<&str>,
        id: &str,
        version: u64,
        passed: bool,
        evidence: &str,
    ) -> Result<Task> {
        if !matches!(actor, Actor::Owner | Actor::Verifier(_)) {
            return Err(Error::Denied("independent verifier required".into()));
        }
        if evidence.trim().is_empty() {
            return Err(Error::Invalid("verification evidence required".into()));
        }
        let t = self.task(id)?;
        let artifacts = self
            .artifacts(id)?
            .into_iter()
            .filter(|a| a.verification_state == VerificationState::Pending)
            .collect::<Vec<_>>();
        if t.state != TaskState::Verifying || artifacts.is_empty() {
            return Err(Error::Invalid("Verifying and an artifact required".into()));
        }
        if let Actor::Verifier(verifier) = actor
            && artifacts.iter().any(|a| a.producer_id == *verifier)
        {
            return Err(Error::Denied("producer cannot verify itself".into()));
        }
        let tx = self.db.transaction()?;
        if let Some(token) = owner_session_token {
            crate::control_auth::web_session(&tx, token)?;
        }
        crate::control_workspace::require_fresh_read(&tx, id)?;
        crate::collaboration::completion(&tx, id)?;
        if crate::recovery::review_required(&tx, id)? {
            return Err(Error::Denied(
                "restored Task requires explicit checkpoint/history review".into(),
            ));
        }
        let current: (String, u64) =
            tx.query_row("SELECT state,version FROM task WHERE id=?1", [id], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        if current != ("Verifying".to_owned(), version) || crate::cancellation::cancelled(&tx, id)?
        {
            return Err(Error::Conflict);
        }
        if artifacts
            .iter()
            .any(|a| digest(a.content.as_bytes()) != a.content_hash)
        {
            return Err(Error::Invalid("Artifact content/hash mismatch".into()));
        }
        let unknown:i64=tx.query_row("SELECT count(*) FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown')",[id],|r|r.get(0))?;
        if unknown > 0 || crate::model_budget::unresolved_model_calls(&tx, id)? {
            return Err(Error::Reconciliation);
        }
        let state = if passed { "Done" } else { "Running" };
        let verification = if passed { "passed" } else { "failed" };
        if tx.execute("UPDATE task SET state=?1,verification_state=?2,version=version+1 WHERE id=?3 AND version=?4",params![state,verification,id,version])?!=1 {return Err(Error::Conflict);}
        tx.execute("UPDATE artifact SET verification_state=?1 WHERE task_id=?2 AND verification_state='pending'",params![verification,id])?;
        let hashes = artifacts
            .iter()
            .map(|a| json!({"id":a.id,"hash":a.content_hash}))
            .collect::<Vec<_>>();
        tx.execute(
            "INSERT INTO verification VALUES(?1,?2,?3,?4,?5,?6)",
            params![
                new_id("verification"),
                id,
                version,
                format!("{actor:?}"),
                passed,
                encode(&json!({"evidence":evidence,"artifacts":hashes}))?
            ],
        )?;
        event(
            &tx,
            "verification",
            id,
            json!({"passed":passed,"evidence":evidence,"artifacts":hashes,"verifier":format!("{actor:?}")}),
        )?;
        event(
            &tx,
            "state_change",
            id,
            json!({"from":"Verifying","to":state,"version":version+1}),
        )?;
        tx.commit()?;
        self.task(id)
    }

    pub fn close_unverified(
        &mut self,
        actor: &Actor,
        id: &str,
        version: u64,
        explicit_reason: &str,
    ) -> Result<Task> {
        owner(actor)?;
        let t = self.task(id)?;
        if t.state != TaskState::Blocked
            || t.block_reason.as_deref() != Some("awaiting-verification")
            || explicit_reason.trim().is_empty()
        {
            return Err(Error::Invalid(
                "Owner reason and Blocked(awaiting-verification) required".into(),
            ));
        }
        let tx = self.db.transaction()?;
        if crate::cancellation::cancelled(&tx, id)? {
            return Err(Error::Denied(
                "cancelled work cannot be accepted as an unverified outcome".into(),
            ));
        }
        if crate::collaboration::unresolved_children(&tx, id)? {
            return Err(Error::Reconciliation);
        }
        let pending:i64=tx.query_row("SELECT count(*) FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown')",[id],|r|r.get(0))?;
        if pending > 0 || crate::model_budget::unresolved_model_calls(&tx, id)? {
            return Err(Error::Reconciliation);
        }
        if tx.execute("UPDATE task SET state='ClosedUnverified',verification_state='unverified',version=version+1,block_reason=NULL WHERE id=?1 AND version=?2",params![id,version])?!=1 {return Err(Error::Conflict);}
        tx.execute(
            "UPDATE artifact SET verification_state='unverified' WHERE task_id=?1",
            [id],
        )?;
        event(
            &tx,
            "closed_unverified",
            id,
            json!({"reason":explicit_reason,"counts_as_success":false}),
        )?;
        tx.commit()?;
        self.task(id)
    }

    pub fn charge_tokens(&mut self, actor: &Actor, task_id: &str, tokens: u64) -> Result<()> {
        core(actor)?;
        if tokens > i64::MAX as u64 {
            return Err(Error::Budget);
        }
        let t = self.task(task_id)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let budget = crate::model_budget::totals(&tx, &t.spec.budget_id)?;
        if budget["frozen"] == true || tokens > budget["available_tokens"].as_u64().unwrap_or(0) {
            return Err(Error::Budget);
        }
        if tx.execute("UPDATE resource_budget SET used_tokens=used_tokens+?1 WHERE id=?2 AND ?1<=token_limit-used_tokens",params![tokens,t.spec.budget_id])?!=1 {return Err(Error::Budget);}
        event(
            &tx,
            "llm_call",
            task_id,
            json!({"tokens":tokens,"budget":t.spec.budget_id}),
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn events(&self) -> Result<Vec<Event>> {
        let mut q = self
            .db
            .prepare("SELECT seq,ts,kind,object_id,payload_json FROM ledger ORDER BY seq")?;
        let rows = q
            .query_map([], |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get::<_, String>(4)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|r| {
                Ok(Event {
                    seq: r.0,
                    ts: r.1,
                    kind: r.2,
                    object_id: r.3,
                    payload: decode(&r.4)?,
                })
            })
            .collect()
    }

    pub fn integrity_check(&self) -> Result<()> {
        let check: String = self
            .db
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
        if check != "ok" {
            return Err(Error::Invalid(check));
        }
        let fk: i64 =
            self.db
                .query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |r| {
                    r.get(0)
                })?;
        if fk != 0 {
            return Err(Error::Invalid("foreign key inconsistency".into()));
        }
        Ok(())
    }
}
