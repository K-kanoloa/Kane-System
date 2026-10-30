//! Rust policy and Task-DAG admission over the existing execution/model schedulers.
//! Owner-only operations: a plan is not authority to execute tools or verify output.
use crate::{registry::*, *};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollaborationNode {
    pub task_id: String,
    pub agent_id: String,
    pub role: String,
    pub requires: Vec<String>,
    pub depends_on: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollaborationPlan {
    pub root: String,
    pub scenario: String,
    /// Exact environment/model/tool/budget/acceptance fingerprint, not a free-form label.
    pub environment: String,
    pub nodes: Vec<CollaborationNode>,
    pub max_agents: u32,
    pub experimental: bool,
    pub reason: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutcomeSample {
    pub task_id: String,
    pub scenario: String,
    pub environment: String,
    pub mode: String,
    pub elapsed_ms: u64,
    pub coordination_ms: u64,
    pub cpu_ms: u64,
    pub peak_memory_bytes: u64,
    pub quality_bps: u32,
    pub evidence_hash: String,
}

fn group(db: &Connection, task: &str) -> Result<Option<String>> {
    Ok(db
        .query_row(
            "SELECT root FROM collaboration_node WHERE task_id=?1",
            [task],
            |r| r.get(0),
        )
        .optional()?)
}
fn pending(db: &Connection, task: &str) -> Result<bool> {
    let effects: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND state IN('Pending','Dispatching','Unknown'))",[task],|r|r.get(0))?;
    Ok(effects || crate::model_budget::unresolved_model_calls(db, task)?)
}
/// Checked again at prompt dispatch, reservation, Intent, Claim and file commit.
/// Settlement/result paths deliberately remain available while paused.
pub(crate) fn admission(db: &Connection, task: &str, agent: Option<&str>) -> Result<()> {
    let Some(root) = group(db, task)? else {
        return Ok(());
    };
    let (paused,claimed,assigned):(bool,bool,String)=db.query_row("SELECT c.paused,n.claimed,n.agent_id FROM collaboration c JOIN collaboration_node n ON n.root=c.root WHERE n.task_id=?1",[task],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    let root_state: String =
        db.query_row("SELECT state FROM task WHERE id=?1", [&root], |r| r.get(0))?;
    let human:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM workspace_control WHERE task_id=?1 AND phase IN('TakingOver','Human','Returning'))",[&root],|r|r.get(0))?;
    if paused
        || !claimed
        || agent.is_some_and(|a| a != assigned)
        || human
        || matches!(
            root_state.as_str(),
            "Failed" | "Cancelled" | "ClosedUnverified" | "Done"
        )
        || crate::cancellation::cancelled(db, &root)?
    {
        return Err(Error::Denied(
            "DAG paused, unclaimed, obsolete assignment or parent control fence".into(),
        ));
    }
    let unmet:i64=db.query_row("SELECT count(*) FROM task_dependency d JOIN task t ON t.id=d.prerequisite WHERE d.task_id=?1 AND (t.state<>'Done' OR t.verification_state<>'passed')",[task],|r|r.get(0))?;
    if unmet != 0 {
        return Err(Error::Denied(
            "DAG dependency requires independently verified Done".into(),
        ));
    }
    let spec: String = db.query_row(
        "SELECT spec_json FROM collaboration WHERE root=?1",
        [&root],
        |r| r.get(0),
    )?;
    let plan: CollaborationPlan = decode(&spec)?;
    let requirements = &plan
        .nodes
        .iter()
        .find(|n| n.task_id == task)
        .ok_or(Error::Conflict)?
        .requires;
    let (profile, expiry): (String, i64) = db.query_row(
        "SELECT capabilities_json,expires_at FROM collaboration_harness_profile WHERE agent_id=?1",
        [&assigned],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let capabilities: Vec<String> = decode(&profile)?;
    let model: String = db.query_row(
        "SELECT m.spec_json FROM agent a JOIN model m ON m.id=a.model_id WHERE a.id=?1",
        [&assigned],
        |r| r.get(0),
    )?;
    let model: Value = decode(&model)?;
    if expiry <= now()
        || model["cloud"] != false
        || requirements.iter().any(|r| {
            !capabilities.contains(r)
                || !model["capabilities"]
                    .as_array()
                    .is_some_and(|c| c.iter().any(|v| v == r))
        })
    {
        return Err(Error::Denied(
            "stale Harness/model capability profile".into(),
        ));
    }
    validate_context_refs(db, task)?;
    Ok(())
}
pub(crate) fn completion(db: &Connection, task: &str) -> Result<()> {
    validate_context_refs(db, task)?;
    let unfinished:i64=db.query_row("SELECT count(*) FROM collaboration_node n JOIN task t ON n.task_id=t.id WHERE n.root=?1 AND n.task_id<>?1 AND (t.state<>'Done' OR t.verification_state<>'passed')",[task],|r|r.get(0))?;
    if unfinished > 0 {
        return Err(Error::Denied(
            "aggregate requires all child outcomes verified".into(),
        ));
    }
    Ok(())
}
pub(crate) fn group_budget(db: &Connection, task: &str, additional: u64) -> Result<()> {
    let Some(root) = group(db, task)? else {
        return Ok(());
    };
    let cap: u64 = db.query_row(
        "SELECT token_limit FROM collaboration WHERE root=?1",
        [&root],
        |r| r.get(0),
    )?;
    let mut stmt=db.prepare("SELECT DISTINCT t.budget_id FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.root=?1")?;
    let budgets = stmt
        .query_map([&root], |r| r.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut occupied = additional;
    for b in budgets {
        let t = crate::model_budget::totals(db, &b)?;
        if t["frozen"] == true {
            return Err(Error::Reconciliation);
        }
        occupied = occupied
            .checked_add(t["used_tokens"].as_u64().unwrap_or(0))
            .and_then(|v| v.checked_add(t["reserved_tokens"].as_u64().unwrap_or(0)))
            .ok_or(Error::Budget)?;
    }
    if occupied > cap {
        return Err(Error::Budget);
    }
    Ok(())
}

impl Registry {
    pub fn collaboration_create(&mut self, actor: &Actor, p: CollaborationPlan) -> Result<Value> {
        owner(actor)?;
        if p.nodes.is_empty()
            || p.nodes.len() > 128
            || p.max_agents == 0
            || p.max_agents as usize > p.nodes.len()
            || p.reason.trim().is_empty()
            || p.scenario.is_empty()
            || p.environment.len() != 64
        {
            return Err(Error::Invalid(
                "bounded plan, reason and configuration SHA256 required".into(),
            ));
        }
        let decision = self.allocation_decide(actor, &p.scenario, &p.environment, p.max_agents)?;
        if p.nodes.len() > decision["max_agents"].as_u64().unwrap_or(1) as usize && !p.experimental
        {
            return Err(Error::Denied(
                "no validated net benefit: Direct or explicit Owner experiment required".into(),
            ));
        }
        let mut graph = BTreeMap::new();
        let mut budgets = BTreeSet::new();
        let mut workspaces = BTreeSet::new();
        let mut agents = BTreeSet::new();
        for n in &p.nodes {
            let t = self.task(&n.task_id)?;
            let a = self.agent(&n.agent_id)?;
            if !matches!(t.state, TaskState::Pending | TaskState::Ready)
                || a.task_id != t.id
                || n.role.trim().is_empty()
                || n.requires.is_empty()
                || !budgets.insert(t.spec.budget_id.clone())
                || !workspaces.insert(t.spec.workspace_id.clone())
                || !agents.insert(a.id.clone())
                || graph
                    .insert(n.task_id.clone(), n.depends_on.clone())
                    .is_some()
            {
                return Err(Error::Invalid(
                    "unique idle Tasks, Agents, workspaces, budgets and role contracts required"
                        .into(),
                ));
            }
            let spec: String = self.db.query_row(
                "SELECT spec_json FROM model WHERE id=?1",
                [&a.model_id],
                |r| r.get(0),
            )?;
            let spec: Value = decode(&spec)?;
            let (capabilities, expiry): (String,i64)=self.db.query_row("SELECT capabilities_json,expires_at FROM collaboration_harness_profile WHERE agent_id=?1",[&a.id],|r|Ok((r.get(0)?,r.get(1)?)))?;
            let capabilities: Vec<String> = decode(&capabilities)?;
            if expiry <= now() || n.requires.iter().any(|v| !capabilities.contains(v)) {
                return Err(Error::Denied(
                    "Harness capability lacks unexpired Owner-verified evidence".into(),
                ));
            }
            if spec["cloud"] != false
                || n.requires.iter().any(|r| {
                    !spec["capabilities"]
                        .as_array()
                        .is_some_and(|c| c.iter().any(|v| v == r))
                })
            {
                return Err(Error::Denied(
                    "model capability/locality mismatch; no implicit fallback".into(),
                ));
            }
        }
        if !graph.contains_key(&p.root)
            || graph
                .iter()
                .any(|(_, deps)| deps.iter().any(|d| !graph.contains_key(d)))
        {
            return Err(Error::Invalid(
                "root and dependencies must belong to plan".into(),
            ));
        }
        let mut visited = BTreeSet::new();
        loop {
            let old = visited.len();
            for (n, d) in &graph {
                if d.iter().all(|d| visited.contains(d)) {
                    visited.insert(n.clone());
                }
            }
            if old == visited.len() {
                break;
            }
        }
        if visited.len() != graph.len() {
            return Err(Error::Invalid("cyclic Task DAG".into()));
        }
        // All children must be prerequisites of the aggregate, directly or transitively.
        let mut ancestors = BTreeSet::new();
        let mut todo = graph[&p.root].clone();
        while let Some(n) = todo.pop() {
            if ancestors.insert(n.clone()) {
                todo.extend(graph[&n].clone());
            }
        }
        if ancestors.len() + 1 != graph.len() {
            return Err(Error::Invalid("disconnected aggregate".into()));
        }
        let cap: u64 = self.db.query_row(
            "SELECT token_limit FROM resource_budget WHERE id=?1",
            [&self.task(&p.root)?.spec.budget_id],
            |r| r.get(0),
        )?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        for n in &p.nodes {
            let started:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM task WHERE id=?1 AND state NOT IN('Pending','Ready')) OR EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch')",[&n.task_id],|r|r.get(0))?;
            if started {
                return Err(Error::Conflict);
            }
        }
        tx.execute(
            "INSERT INTO collaboration(root,spec_json,max_agents,token_limit) VALUES(?1,?2,?3,?4)",
            params![p.root, encode(&p)?, p.max_agents, cap],
        )?;
        for n in &p.nodes {
            tx.execute(
                "INSERT INTO collaboration_node(task_id,root,agent_id,role) VALUES(?1,?2,?3,?4)",
                params![n.task_id, p.root, n.agent_id, n.role],
            )?;
        }
        for n in &p.nodes {
            for d in &n.depends_on {
                tx.execute(
                    "INSERT INTO task_dependency VALUES(?1,?2)",
                    params![n.task_id, d],
                )?;
            }
        }
        event(
            &tx,
            "allocation_decision",
            &p.root,
            json!({"plan":p,"decision":decision,"experimental":p.experimental,"token_limit":cap,"resource_admission":"existing Supervisor and ModelInstance pool; no extra scheduler"}),
        )?;
        tx.commit()?;
        self.collaboration_status(actor, &p.root)
    }
    pub fn collaboration_status(&self, actor: &Actor, root: &str) -> Result<Value> {
        core(actor)?;
        let (spec,version,max_agents,paused,limit):(String,u64,u32,bool,u64)=self.db.query_row("SELECT spec_json,version,max_agents,paused,token_limit FROM collaboration WHERE root=?1",[root],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
        let mut q=self.db.prepare("SELECT n.task_id,n.agent_id,n.role,n.claimed,n.generation,t.state,t.verification_state FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.root=?1 ORDER BY n.task_id")?;
        let nodes=q.query_map([root],|r|Ok(json!({"task_id":r.get::<_,String>(0)?,"agent_id":r.get::<_,String>(1)?,"role":r.get::<_,String>(2)?,"claimed":r.get::<_,bool>(3)?,"generation":r.get::<_,u64>(4)?,"state":r.get::<_,String>(5)?,"verification_state":r.get::<_,String>(6)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"root":root,"spec":decode::<Value>(&spec)?,"version":version,"max_agents":max_agents,"paused":paused,"token_limit":limit,"nodes":nodes}),
        )
    }
    /// Reconfiguration never cancels/refunds in-flight work. Explicit cancellation uses existing barriers.
    pub fn collaboration_policy(
        &mut self,
        actor: &Actor,
        root: &str,
        version: u64,
        limit: u32,
        paused: bool,
        reason: &str,
    ) -> Result<Value> {
        owner(actor)?;
        if limit == 0 || reason.trim().is_empty() {
            return Err(Error::Invalid("limit and reason".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let (active,total):(u32,u32)=tx.query_row("SELECT sum(CASE WHEN n.claimed=1 AND t.state NOT IN('Done','Failed','Cancelled','ClosedUnverified') THEN 1 ELSE 0 END),count(*) FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.root=?1",[root],|r|Ok((r.get::<_,Option<u32>>(0)?.unwrap_or(0),r.get(1)?)))?;
        let spec: String = tx.query_row(
            "SELECT spec_json FROM collaboration WHERE root=?1",
            [root],
            |r| r.get(0),
        )?;
        let plan: CollaborationPlan = decode(&spec)?;
        if limit < active || limit > total || limit > plan.max_agents {
            return Err(Error::Denied(
                "cannot bypass Owner cap or shrink across active claims; cancel/resolve first"
                    .into(),
            ));
        }
        if tx.execute("UPDATE collaboration SET max_agents=?1,paused=?2,version=version+1 WHERE root=?3 AND version=?4",params![limit,paused,root,version])?!=1{return Err(Error::Conflict);}
        event(
            &tx,
            "allocation_policy",
            root,
            json!({"max_agents":limit,"paused":paused,"reason":reason,"version":version+1}),
        )?;
        tx.commit()?;
        self.collaboration_status(actor, root)
    }
    pub fn collaboration_claim(&mut self, actor: &Actor, task: &str) -> Result<Value> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let root = group(&tx, task)?.ok_or_else(|| Error::NotFound(task.into()))?;
        let (paused, limit): (bool, u32) = tx.query_row(
            "SELECT paused,max_agents FROM collaboration WHERE root=?1",
            [&root],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let (state,claimed,agent,generation):(String,bool,String,u64)=tx.query_row("SELECT t.state,n.claimed,n.agent_id,n.generation FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.task_id=?1",[task],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        if paused || claimed || !matches!(state.as_str(), "Pending" | "Ready") {
            return Err(Error::Conflict);
        }
        let active:u32=tx.query_row("SELECT count(*) FROM collaboration_node n JOIN task t ON t.id=n.task_id WHERE n.root=?1 AND n.claimed=1 AND t.state NOT IN('Done','Failed','Cancelled','ClosedUnverified')",[&root],|r|r.get(0))?;
        if active >= limit {
            return Err(Error::Budget);
        }
        tx.execute(
            "UPDATE collaboration_node SET claimed=1 WHERE task_id=?1",
            [task],
        )?;
        admission(&tx, task, Some(&agent))?;
        event(
            &tx,
            "dag_claim",
            task,
            json!({"root":root,"agent":agent,"generation":generation}),
        )?;
        tx.commit()?;
        Ok(
            json!({"task_id":task,"agent_id":agent,"generation":generation,"automatic_replay":false}),
        )
    }
    pub fn collaboration_reassign(
        &mut self,
        actor: &Actor,
        task: &str,
        agent: &str,
        version: u64,
    ) -> Result<Value> {
        owner(actor)?;
        let a = self.agent(agent)?;
        if a.task_id != task {
            return Err(Error::Denied("replacement binding".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let started:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='prompt_dispatch') OR EXISTS(SELECT 1 FROM task WHERE id=?1 AND state NOT IN('Pending','Ready'))",[task],|r|r.get(0))?;
        let old: String = tx.query_row(
            "SELECT agent_id FROM collaboration_node WHERE task_id=?1",
            [task],
            |r| r.get(0),
        )?;
        let live: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM session WHERE agent_id=?1 AND ended_at IS NULL)",
            [&old],
            |r| r.get(0),
        )?;
        if started || live || pending(&tx, task)? || old == agent {
            return Err(Error::Denied(
                "only provably unexecuted stopped assignments may move".into(),
            ));
        }
        if tx.execute("UPDATE collaboration_node SET agent_id=?1,generation=generation+1,claimed=0 WHERE task_id=?2 AND generation=?3",params![agent,task,version])?!=1{return Err(Error::Conflict);}
        tx.execute(
            "UPDATE capability SET revoked_at=?1 WHERE subject=?2 AND revoked_at IS NULL",
            params![now(), old],
        )?;
        event(
            &tx,
            "dag_reassigned",
            task,
            json!({"old_agent":old,"new_agent":agent,"generation":version+1,"executed":false}),
        )?;
        tx.commit()?;
        Ok(json!({"generation":version+1}))
    }
}

impl Registry {
    /// Measurements are asserted by the local Owner/independent evaluator, never an Agent.
    /// Success and token usage are derived from authoritative outcomes/receipts.
    pub fn allocation_sample(&mut self, actor: &Actor, s: OutcomeSample) -> Result<Value> {
        owner(actor)?;
        let task = self.task(&s.task_id)?;
        if !task.state.terminal()
            || !matches!(
                s.mode.as_str(),
                "Direct" | "Parallel" | "Collaboration" | "Specialist"
            )
            || s.elapsed_ms == 0
            || s.coordination_ms > s.elapsed_ms
            || s.quality_bps > 10000
            || s.environment.len() != 64
            || s.evidence_hash.len() != 64
            || s.scenario.is_empty()
            || (task.state != TaskState::Done && s.quality_bps != 0)
        {
            return Err(Error::Invalid(
                "terminal independent result and finite, hashed measurement required".into(),
            ));
        }
        if pending(&self.db, &s.task_id)? {
            return Err(Error::Reconciliation);
        }
        if task.state == TaskState::Done {
            completion(&self.db, &s.task_id)?;
        }
        if unresolved_children(&self.db, &s.task_id)? {
            return Err(Error::Reconciliation);
        }
        let success =
            task.state == TaskState::Done && task.verification_state == VerificationState::Passed;
        let budgets: Vec<String> = if group(&self.db, &task.id)?.as_deref() == Some(&task.id) {
            let mut q=self.db.prepare("SELECT DISTINCT t.budget_id FROM collaboration_node n JOIN task t ON n.task_id=t.id WHERE n.root=?1")?;
            q.query_map([&task.id], |r| r.get(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        } else {
            vec![task.spec.budget_id]
        };
        let mut tokens = 0u64;
        for b in budgets {
            let v = crate::model_budget::totals(&self.db, &b)?;
            if v["reserved_tokens"] != 0 || v["frozen"] == true {
                return Err(Error::Reconciliation);
            }
            tokens = tokens
                .checked_add(v["used_tokens"].as_u64().ok_or(Error::Budget)?)
                .ok_or(Error::Budget)?;
        }
        let agents: u32 = self.db.query_row(
            "SELECT count(*) FROM collaboration_node WHERE root=?1",
            [&s.task_id],
            |r| r.get(0),
        )?;
        if (agents > 1 && matches!(s.mode.as_str(), "Direct" | "Specialist"))
            || (agents <= 1 && !matches!(s.mode.as_str(), "Direct" | "Specialist"))
        {
            return Err(Error::Invalid(
                "mode disagrees with actual Task graph".into(),
            ));
        }
        let id = new_id("sample");
        let metrics = json!({"sample":s,"verified_success":success,"actual_tokens":tokens,"agents":agents.max(1),"measurements":"Owner-attested environment evidence; tokens and success from Core"});
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO allocation_sample VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            params![
                id,
                s.task_id,
                s.scenario,
                s.environment,
                s.mode,
                encode(&metrics)?,
                s.evidence_hash,
                now()
            ],
        )?;
        event(&tx, "allocation_measurement", &s.task_id, metrics.clone())?;
        tx.commit()?;
        Ok(metrics)
    }
    pub fn allocation_decide(
        &mut self,
        actor: &Actor,
        scenario: &str,
        environment: &str,
        owner_limit: u32,
    ) -> Result<Value> {
        owner(actor)?;
        if scenario.is_empty() || environment.len() != 64 || owner_limit == 0 {
            return Err(Error::Invalid(
                "scenario, configuration SHA256 and Owner limit".into(),
            ));
        }
        let mut q=self.db.prepare("SELECT id,metrics_json FROM allocation_sample WHERE scenario=?1 AND environment=?2 ORDER BY id")?;
        let rows = q
            .query_map(params![scenario, environment], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(q);
        let mut groups: BTreeMap<(String, u32), Vec<Value>> = BTreeMap::new();
        let mut evidence = vec![];
        for (id, m) in rows {
            let v: Value = decode(&m)?;
            evidence.push(id);
            groups
                .entry((
                    v["sample"]["mode"].as_str().unwrap_or("Direct").into(),
                    v["agents"].as_u64().unwrap_or(1) as u32,
                ))
                .or_default()
                .push(v);
        }
        let direct = groups.get(&("Direct".into(), 1));
        let means = |v: &Vec<Value>| -> Vec<f64> {
            let n = v.len() as f64;
            vec![
                v.iter()
                    .map(|s| s["sample"]["quality_bps"].as_u64().unwrap_or(0) as f64 / 10000.0)
                    .sum::<f64>()
                    / n,
                v.iter().filter(|s| s["verified_success"] == true).count() as f64 / n,
                v.iter()
                    .map(|s| s["sample"]["elapsed_ms"].as_u64().unwrap_or(0) as f64)
                    .sum::<f64>()
                    / n,
                v.iter()
                    .map(|s| s["actual_tokens"].as_u64().unwrap_or(0) as f64)
                    .sum::<f64>()
                    / n,
                v.iter()
                    .map(|s| s["sample"]["cpu_ms"].as_u64().unwrap_or(0) as f64)
                    .sum::<f64>()
                    / n,
                v.iter()
                    .map(|s| s["sample"]["peak_memory_bytes"].as_u64().unwrap_or(0) as f64)
                    .sum::<f64>()
                    / n,
                v.iter()
                    .map(|s| s["sample"]["coordination_ms"].as_u64().unwrap_or(0) as f64)
                    .sum::<f64>()
                    / n,
            ]
        };
        // Pending/Unknown or ungraded outcomes are not absent evidence. A
        // policy-declared attempt cannot disappear from reliability estimates
        // merely because it has not reached an independently graded terminal.
        let ungraded:u64=self.db.query_row("SELECT count(*) FROM collaboration c WHERE json_extract(c.spec_json,'$.scenario')=?1 AND json_extract(c.spec_json,'$.environment')=?2 AND NOT EXISTS(SELECT 1 FROM allocation_sample s WHERE s.task_id=c.root)",params![scenario,environment],|r|r.get(0))?;
        let mut chosen = "Direct".to_string();
        let mut agents = 1;
        let mut delta = 0.0f64;
        let mut comparisons = vec![];
        if let Some(d) = direct.filter(|v| v.len() >= 5) {
            let b = means(d);
            for ((mode, count), samples) in &groups {
                if *count > owner_limit || *count <= 1 || samples.len() < 5 {
                    continue;
                }
                let m = means(samples);
                let relative = |i: usize| (b[i] - m[i]) / b[i].max(m[i]).max(1.0);
                let du = 0.35 * (m[0] - b[0])
                    + 0.25 * (m[1] - b[1])
                    + 0.15 * relative(2)
                    + 0.10 * relative(3)
                    + 0.075 * relative(4)
                    + 0.075 * relative(5)
                    - 0.10 * m[6] / m[2].max(1.0)
                    - 0.02;
                comparisons.push(json!({"mode":mode,"agents":count,"n":samples.len(),"delta_u":du,"quality":m[0],"success":m[1]}));
                if ungraded == 0 && du > 0.05 && du > delta && m[0] >= b[0] && m[1] >= b[1] {
                    chosen = mode.clone();
                    agents = *count;
                    delta = du;
                }
            }
        }
        let result = json!({"mode":chosen,"max_agents":agents,"predicted_delta_u":delta,"scenario":scenario,"environment":environment,"owner_limit":owner_limit,"ungraded_attempts":ungraded,"evidence":evidence,"comparisons":comparisons,"reason":if agents==1{"minimum sufficient Direct: insufficient evidence or no positive net benefit above 0.05"}else{"matched independent N>=5 results exceed normalized cost/coordination/risk threshold"},"formula_version":1,"weights":{"quality":0.35,"success":0.25,"wall":0.15,"tokens":0.10,"cpu":0.075,"memory":0.075,"coordination_penalty":0.10,"risk_penalty":0.02},"scope":"exact scenario/configuration only; no general multi-Agent superiority"});
        let tx = self.db.transaction()?;
        event(&tx, "allocation_decision", scenario, result.clone())?;
        tx.commit()?;
        Ok(result)
    }

    pub fn collaboration_context(
        &mut self,
        actor: &Actor,
        artifact: &str,
        expires_at: i64,
        sensitivity: &str,
    ) -> Result<String> {
        owner(actor)?;
        let (task, content, hash, state): (String, String, String, String) = self.db.query_row(
            "SELECT task_id,content,content_hash,verification_state FROM artifact WHERE id=?1",
            [artifact],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
        let t = self.task(&task)?;
        if state != "passed"
            || t.state != TaskState::Done
            || digest(content.as_bytes()) != hash
            || expires_at <= now()
            || content.len() > 262144
        {
            return Err(Error::Denied("confirmed bounded Artifact required".into()));
        }
        self.create_context(actor,&task,sensitivity,"verified",json!({"artifact_id":artifact,"source_task":task,"artifact_hash":hash,"content":content,"instruction_authority":false}),Some(expires_at))
    }
    pub fn collaboration_share(
        &mut self,
        actor: &Actor,
        context: &str,
        agent: &str,
        expires_at: i64,
        ceiling: &str,
    ) -> Result<Value> {
        owner(actor)?;
        let a = self.agent(agent)?;
        let (source, version, expiry, sensitivity): (String, u64, Option<i64>, String) =
            self.db.query_row(
                "SELECT task_id,version,expires_at,sensitivity FROM context WHERE id=?1",
                [context],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )?;
        let root = group(&self.db, &source)?;
        if root.is_none()
            || root != group(&self.db, &a.task_id)?
            || expires_at <= now()
            || expiry.is_some_and(|e| e < expires_at || e <= now())
            || sensitivity_rank(&sensitivity)? > sensitivity_rank(ceiling)?
        {
            return Err(Error::Denied("Context scope, sensitivity or expiry".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute("INSERT INTO context_share VALUES(?1,?2,?3,?4,?5,?6,NULL) ON CONFLICT(context_id,agent_id) DO UPDATE SET version=excluded.version,model_id=excluded.model_id,ceiling=excluded.ceiling,expires_at=excluded.expires_at,revoked_at=NULL",params![context,agent,version,a.model_id,ceiling,expires_at])?;
        let cap = new_id("cap");
        tx.execute(
            "INSERT INTO capability VALUES(?1,?2,'read',?3,?4,NULL,'owner')",
            params![cap, agent, context, expires_at],
        )?;
        event(
            &tx,
            "context_share",
            context,
            json!({"agent":agent,"task":a.task_id,"root":root,"version":version,"expires_at":expires_at,"capability":cap,"ceiling":ceiling,"model":a.model_id,"grants_tool_authority":false}),
        )?;
        tx.commit()?;
        Ok(json!({"version":version,"capability":cap}))
    }
    pub fn collaboration_context_read(
        &mut self,
        actor: &Actor,
        agent: &str,
        context: &str,
    ) -> Result<Value> {
        core(actor)?;
        let a = self.agent(agent)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let result = (|| {
            let version = context_access(&tx, agent, context)?;
            let (source, sensitivity, trust, content): (String, String, String, String) = tx
                .query_row(
                    "SELECT task_id,sensitivity,trust,spec_json FROM context WHERE id=?1",
                    [context],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )?;
            tx.execute("INSERT INTO context_reference VALUES(?1,?2,?3,?4) ON CONFLICT(task_id,context_id) DO UPDATE SET version=excluded.version,agent_id=excluded.agent_id",params![a.task_id,context,version,agent])?;
            let value: Value = decode(&content)?;
            Ok(
                json!({"id":context,"version":version,"source_task":source,"sensitivity":sensitivity,"trust":trust,"content":value,"bytes":content.len(),"estimated_transfer_tokens":content.len().div_ceil(4),"usage_kind":"transfer estimate, NOT measured model usage","instruction_authority":false}),
            )
        })();
        event(
            &tx,
            if result.is_ok() {
                "context_read"
            } else {
                "context_denied"
            },
            context,
            json!({"agent":agent,"task":a.task_id,"result":result.as_ref().ok().map(|value|json!({"version":value["version"],"bytes":value["bytes"],"estimated_transfer_tokens":value["estimated_transfer_tokens"],"context_payload_sha256":digest(value["content"].to_string().as_bytes()),"usage_kind":"transfer estimate, not provider usage"})),"tool_authority":false}),
        )?;
        tx.commit()?;
        result
    }
    /// Invalidation is irreversible for old pinned references. Re-sharing requires explicit Owner action.
    pub fn collaboration_context_invalidate(
        &mut self,
        actor: &Actor,
        context: &str,
        version: u64,
        reason: &str,
    ) -> Result<Value> {
        owner(actor)?;
        if reason.trim().is_empty() {
            return Err(Error::Invalid("invalidation reason".into()));
        }
        let tx = self.db.transaction()?;
        if tx.execute(
            "UPDATE context SET version=version+1 WHERE id=?1 AND version=?2",
            params![context, version],
        )? != 1
        {
            return Err(Error::Conflict);
        }
        tx.execute(
            "UPDATE context_share SET revoked_at=?1 WHERE context_id=?2",
            params![now(), context],
        )?;
        tx.execute("UPDATE capability SET revoked_at=?1 WHERE target=?2 AND action='read' AND revoked_at IS NULL",params![now(),context])?;
        event(
            &tx,
            "context_invalidated",
            context,
            json!({"version":version+1,"reason":reason,"downstream":"all pinned reads fail closed; re-read requires Owner regrant"}),
        )?;
        tx.commit()?;
        Ok(json!({"version":version+1}))
    }
}
fn sensitivity_rank(s: &str) -> Result<u8> {
    match s {
        "public" => Ok(0),
        "internal" => Ok(1),
        "confidential" => Ok(2),
        "restricted" => Ok(3),
        _ => Err(Error::Invalid("sensitivity".into())),
    }
}
fn context_access(db: &Connection, agent: &str, id: &str) -> Result<u64> {
    let (source, version, expiry, sensitivity, raw): (String, u64, Option<i64>, String, String) =
        db.query_row(
            "SELECT task_id,version,expires_at,sensitivity,spec_json FROM context WHERE id=?1",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )?;
    let (task, model): (String, String) = db.query_row(
        "SELECT task_id,model_id FROM agent WHERE id=?1",
        [agent],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let (v,expected_model,ceiling,expiry_grant,revoked):(u64,String,String,i64,Option<i64>)=db.query_row("SELECT version,model_id,ceiling,expires_at,revoked_at FROM context_share WHERE context_id=?1 AND agent_id=?2",params![id,agent],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?;
    if group(db, &source)?.is_none()
        || group(db, &source)? != group(db, &task)?
        || version != v
        || model != expected_model
        || revoked.is_some()
        || expiry_grant <= now()
        || expiry.is_some_and(|e| e <= now())
        || sensitivity_rank(&sensitivity)? > sensitivity_rank(&ceiling)?
    {
        return Err(Error::Denied(
            "stale/revoked Context grant, model or scope".into(),
        ));
    }
    let spec: String = db.query_row("SELECT spec_json FROM model WHERE id=?1", [&model], |r| {
        r.get(0)
    })?;
    let spec: Value = decode(&spec)?;
    if spec["cloud"] != false
        || sensitivity_rank(&sensitivity)?
            > sensitivity_rank(
                spec["privacy"]["max_sensitivity"]
                    .as_str()
                    .unwrap_or("public"),
            )?
    {
        return Err(Error::Denied(
            "Context model locality/sensitivity policy".into(),
        ));
    }
    // A permissive model metadata ceiling cannot override the actual shared
    // gateway's public/private routing policy. Source data stays data, never
    // authorization to weaken the consumer's execution route.
    if sensitivity_rank(&sensitivity)? > 0 && spec["scheduling"]["mode"] == "gateway_fifo" {
        let gateway = &spec["gateway"];
        if sensitivity_rank(gateway["sensitivity"].as_str().unwrap_or("public"))?
            < sensitivity_rank(&sensitivity)?
            || gateway["exclusive_task"] != task
            || gateway["cache"] != "disabled"
        {
            return Err(Error::Denied(
                "nonpublic Context requires an exclusive matching private gateway policy".into(),
            ));
        }
    }
    Registry::check_capability(db, agent, "read", id)?;
    let content: Value = decode(&raw)?;
    if let Some(artifact) = content["artifact_id"].as_str() {
        let (actual, state): (String, String) = db.query_row(
            "SELECT content_hash,verification_state FROM artifact WHERE id=?1",
            [artifact],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if content["artifact_hash"] != actual || state != "passed" {
            return Err(Error::Denied("Context source invalidated".into()));
        }
    }
    Ok(version)
}
fn validate_context_refs(db: &Connection, task: &str) -> Result<()> {
    let mut q =
        db.prepare("SELECT context_id,version,agent_id FROM context_reference WHERE task_id=?1")?;
    for row in q.query_map([task], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, u64>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (id, version, agent) = row?;
        if context_access(db, &agent, &id)? != version {
            return Err(Error::Denied("stale Context reference".into()));
        }
    }
    Ok(())
}

pub(crate) fn unresolved_children(db: &Connection, root: &str) -> Result<bool> {
    let mut q =
        db.prepare("SELECT task_id FROM collaboration_node WHERE root=?1 AND task_id<>?1")?;
    for row in q.query_map([root], |r| r.get::<_, String>(0))? {
        if pending(db, &row?)? {
            return Ok(true);
        }
    }
    Ok(false)
}

impl Registry {
    #[allow(clippy::too_many_arguments)] // Mirrors the versioned external invocation boundary.
    pub fn external_prepare(
        &mut self,
        actor: &Actor,
        turn: &str,
        native: &str,
        key: &str,
        protocol: &str,
        server: &str,
        tool: &str,
        arguments: Value,
    ) -> Result<Value> {
        core(actor)?;
        if !matches!(protocol, "mcp" | "a2a")
            || server.len() != 64
            || tool.is_empty()
            || !arguments.is_object()
        {
            return Err(Error::Unsupported(
                "only registered MCP/A2A reference transport".into(),
            ));
        }
        let (agent,task):(String,String)=self.db.query_row("SELECT agent_id,task_id FROM harness_binding WHERE turn_id=?1 AND native_session_ref=?2",params![turn,native],|r|Ok((r.get(0)?,r.get(1)?)))?;
        let runtime: String = self.db.query_row(
            "SELECT id FROM session WHERE agent_id=?1 AND ended_at IS NULL",
            [&agent],
            |r| r.get(0),
        )?;
        let args = json!({"server_sha256":server,"tool":tool,"arguments":arguments});
        let target = self.tool_target(actor, &agent, protocol, &args)?;
        let result=self.prepare_action(&Actor::Agent(agent),&task,&format!("{protocol}:{task}:{key}"),&target,json!({"kind":"managed_tool","tool":protocol,"turn_id":turn,"native_session_ref":native,"runtime_session_id":runtime,"external":args}));
        match result {
            Ok(action) => Ok(serde_json::to_value(action)?),
            Err(error) => {
                let tx = self.db.transaction()?;
                event(
                    &tx,
                    "external_denied",
                    &task,
                    json!({"protocol":protocol,"peer":server,"method":tool,"key_hash":digest(key.as_bytes()),"permit":false,"automatic_retry":false}),
                )?;
                tx.commit()?;
                Err(error)
            }
        }
    }
    pub fn external_unknown(&mut self, actor: &Actor, id: &str) -> Result<Value> {
        core(actor)?;
        let action = self.action(id)?;
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::tool_execution::unknown(&tx, id, &action.task_id)?;
        tx.commit()?;
        Ok(serde_json::to_value(self.action(id)?)?)
    }
}

impl Registry {
    pub fn collaboration_harness_profile(
        &mut self,
        actor: &Actor,
        agent: &str,
        capabilities: Vec<String>,
        evidence_hash: &str,
        expires_at: i64,
    ) -> Result<Value> {
        owner(actor)?;
        self.agent(agent)?;
        if capabilities.is_empty()
            || capabilities.len() > 64
            || capabilities.iter().any(|c| c.is_empty() || c.len() > 128)
            || evidence_hash.len() != 64
            || expires_at <= now()
        {
            return Err(Error::Invalid("bounded verified Harness profile".into()));
        }
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO collaboration_harness_profile VALUES(?1,?2,?3,?4) ON CONFLICT(agent_id) DO UPDATE SET capabilities_json=excluded.capabilities_json,evidence_hash=excluded.evidence_hash,expires_at=excluded.expires_at",params![agent,encode(&capabilities)?,evidence_hash,expires_at])?;
        event(
            &tx,
            "harness_capability_verified",
            agent,
            json!({"capabilities":capabilities,"evidence_hash":evidence_hash,"expires_at":expires_at,"authority":"Owner attestation; interface names alone are insufficient"}),
        )?;
        tx.commit()?;
        Ok(json!({"registered":true}))
    }
}

impl Registry {
    /// Deterministic capability/budget routing among already registered native identities.
    /// Selection is advisory until the existing transactional DAG/model admission.
    pub fn allocation_route(
        &mut self,
        actor: &Actor,
        task: &str,
        requires: Vec<String>,
    ) -> Result<Value> {
        owner(actor)?;
        let t = self.task(task)?;
        if requires.is_empty() || t.state.terminal() {
            return Err(Error::Invalid(
                "active Task and capability requirements".into(),
            ));
        }
        let available = crate::model_budget::totals(&self.db, &t.spec.budget_id)?;
        let mut q=self.db.prepare("SELECT a.id,a.model_id,m.spec_json,p.capabilities_json,p.expires_at FROM agent a JOIN model m ON m.id=a.model_id JOIN collaboration_harness_profile p ON p.agent_id=a.id WHERE a.task_id=?1 AND a.state NOT IN('Failed','Exited','Terminated','Cancelling') ORDER BY a.id")?;
        let rows = q
            .query_map([task], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(q);
        let mut candidates = vec![];
        for (id, model, spec, profile, expiry) in rows {
            let m: Value = decode(&spec)?;
            let caps: Vec<String> = decode(&profile)?;
            let bound = m["budget_profile"]["upper_bound_tokens"].as_u64();
            if expiry <= now()
                || m["cloud"] != false
                || available["frozen"] == true
                || bound.is_none_or(|b| b > available["available_tokens"].as_u64().unwrap_or(0))
                || requires.iter().any(|c| {
                    !caps.contains(c)
                        || !m["capabilities"]
                            .as_array()
                            .is_some_and(|v| v.iter().any(|x| x == c))
                })
            {
                continue;
            }
            candidates.push((bound.unwrap_or(u64::MAX), id, model));
        }
        candidates.sort();
        let selected = candidates.first().ok_or_else(|| {
            Error::Denied(
                "no verified capable Agent/local model within budget; no cloud fallback".into(),
            )
        })?;
        let result = json!({"agent_id":selected.1,"model_id":selected.2,"upper_bound_tokens":selected.0,"requires":requires,"task_id":task,"reason":"minimum verified per-call token bound, stable ID tie-break; no inferred capabilities","resource_enforcement":"existing Supervisor Host pool, cgroups and gateway transaction recheck; selection is not reservation"});
        let tx = self.db.transaction()?;
        event(&tx, "allocation_route", task, result.clone())?;
        tx.commit()?;
        Ok(result)
    }
}
