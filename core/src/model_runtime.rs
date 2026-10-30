//! Trusted local model residency, generation fencing and single-use gateway admission.
use crate::{registry::*, resources, *};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LocalModelSpec {
    pub endpoint: String,
    pub backend_model: String,
    pub context_tokens: u64,
    pub parallelism: u32,
    pub resources: HostResources,
}
impl LocalModelSpec {
    pub fn validate(&self) -> Result<()> {
        // No URL parser ambiguity, DNS, credentials, redirects, paths or cloud egress.
        let port = self
            .endpoint
            .strip_prefix("http://127.0.0.1:")
            .and_then(|s| s.parse::<u16>().ok())
            .filter(|p| *p > 0);
        if port.is_none()
            || self.backend_model.is_empty()
            || self.backend_model.len() > 128
            || !(1..=16).contains(&self.parallelism)
            || self.context_tokens == 0
            || self.context_tokens > 1_000_000
            || self.resources.cpu_millis == 0
            || self.resources.memory_bytes == 0
            || self.resources.pids == 0
            || self.resources.storage_bytes == 0
        {
            return Err(Error::Invalid(
                "bounded local model resources and exact IPv4 loopback endpoint required".into(),
            ));
        }
        Ok(())
    }
}
pub(crate) fn gateway_config(db: &rusqlite::Connection, model: &str) -> Result<Option<Value>> {
    let spec: String = db.query_row("SELECT spec_json FROM model WHERE id=?1", [model], |r| {
        r.get(0)
    })?;
    let spec: Value = decode(&spec)?;
    Ok((spec["scheduling"]["mode"] == "gateway_fifo").then_some(spec))
}
fn live_scope(
    tx: &rusqlite::Transaction<'_>,
    spec: &Value,
    b: &crate::model_budget::ModelBinding,
    instance: &str,
    generation: &str,
) -> Result<()> {
    let class: String = tx.query_row(
        "SELECT workload_class FROM task WHERE id=?1",
        [&b.task],
        |r| r.get(0),
    )?;
    if spec["cloud"] != false
        || spec["gateway"]["cache"] != "disabled"
        || spec["gateway"]["instance_id"] != instance
        || !spec["gateway"]["workspaces"]
            .as_array()
            .is_some_and(|xs| xs.iter().any(|x| x.as_str() == Some(&b.workspace)))
        || !spec["gateway"]["workload_classes"]
            .as_array()
            .is_some_and(|xs| xs.iter().any(|x| x.as_str() == Some(&class)))
    {
        return Err(Error::Denied(
            "live model data/Workspace/workload route policy".into(),
        ));
    }
    if spec["gateway"]["requires_kernel_limits"] == true {
        let proof:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE kind='model_kernel_limits' AND object_id=?1 AND json_extract(payload_json,'$.generation')=?2 AND json_extract(payload_json,'$.evidence.phase')='healthy_ready' ORDER BY seq DESC LIMIT 1",params![instance,generation],|r|r.get(0)).optional()?;
        let proof: Value = proof
            .map(|s| decode(&s))
            .transpose()?
            .ok_or_else(|| Error::Denied("verified model kernel limits required".into()))?;
        let data: String = tx.query_row(
            "SELECT resources_json FROM model_runtime WHERE instance_id=?1",
            [instance],
            |r| r.get(0),
        )?;
        let resources: HostResources = decode(&data)?;
        let p = &proof["evidence"];
        let quota = resources.cpu_millis.checked_mul(100).ok_or(Error::Budget)?;
        if p["mode"] != "cgroup_v2"
            || p["kernel_enforced"] != true
            || p["cpu.max"] != format!("{quota} 100000")
            || p["memory.max"].as_str().and_then(|s| s.parse::<u64>().ok())
                != Some(resources.memory_bytes)
            || p["pids.max"].as_str().and_then(|s| s.parse::<u64>().ok()) != Some(resources.pids)
        {
            return Err(Error::Denied(
                "model kernel proof does not match reservation".into(),
            ));
        }
    }
    let pinned:Option<String>=tx.query_row("SELECT json_extract(payload_json,'$.task_id') FROM ledger WHERE object_id=?1 AND kind='model_private_scope' AND json_extract(payload_json,'$.generation')=?2",params![instance,generation],|r|r.get(0)).optional()?;
    if pinned.as_ref().is_some_and(|task| task != &b.task) {
        return Err(Error::Denied(
            "model residency pinned to another private Task".into(),
        ));
    }
    if spec["gateway"]["sensitivity"] != "public" {
        if spec["gateway"]["exclusive_task"] != b.task {
            return Err(Error::Denied("private Task scope".into()));
        }
        let previous:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM model_gateway_call g JOIN model_queue q ON q.call_id=g.call_id WHERE g.instance_id=?1 AND g.generation=?2 AND q.task_id!=?3)",params![instance,generation,b.task],|r|r.get(0))?;
        if previous {
            return Err(Error::Denied(
                "private Task requires fresh exclusive residency".into(),
            ));
        }
        if pinned.is_none() {
            event(
                tx,
                "model_private_scope",
                instance,
                json!({"generation":generation,"task_id":b.task,"cache":"disabled","other_tasks_denied_until_fresh_process_generation":true}),
            )?;
        }
    }
    Ok(())
}
pub(crate) fn prepare_permit(
    tx: &rusqlite::Transaction<'_>,
    b: &crate::model_budget::ModelBinding,
    call: &str,
) -> Result<Option<String>> {
    let Some(spec) = gateway_config(tx, &b.model_id)? else {
        return Ok(None);
    };
    if spec["cloud"] != false || spec["gateway"]["cache"] != "disabled" {
        return Err(Error::Denied("local policy/cache".into()));
    }
    let allow = spec["gateway"]["workspaces"]
        .as_array()
        .ok_or_else(|| Error::Denied("explicit Workspace scope required".into()))?;
    if !allow.iter().any(|w| w.as_str() == Some(&b.workspace)) {
        return Err(Error::Denied("Workspace model scope".into()));
    }
    let class: String = tx.query_row(
        "SELECT workload_class FROM task WHERE id=?1",
        [&b.task],
        |r| r.get(0),
    )?;
    if !spec["gateway"]["workload_classes"]
        .as_array()
        .is_some_and(|xs| xs.iter().any(|v| v.as_str() == Some(&class)))
    {
        return Err(Error::Unsupported(
            "no verified backend for Task workload class".into(),
        ));
    }
    let instance = spec["gateway"]["instance_id"]
        .as_str()
        .ok_or_else(|| Error::Invalid("instance binding".into()))?;
    let (model,state,generation,verified,context,parallel):(String,String,String,bool,u64,u32)=tx.query_row(
        "SELECT i.model_id,i.state,r.generation,r.verified,r.context_tokens,r.parallelism FROM model_instance i JOIN model_runtime r ON r.instance_id=i.id WHERE i.id=?1 AND r.held=1",[instance],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?;
    if model != b.model_id || state != "Ready" || !verified {
        return Err(Error::Denied("model instance not healthy Ready".into()));
    }
    live_scope(tx, &spec, b, instance, &generation)?;
    let active:u32=tx.query_row("SELECT count(*) FROM model_gateway_call WHERE instance_id=?1 AND state IN ('Prepared','Claimed','Unknown')",[instance],|r|r.get(0))?;
    if active >= parallel {
        return Err(Error::Budget);
    }
    let output = spec["gateway"]["max_output_tokens"]
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or(Error::Budget)?;
    if b.profile["upper_bound_tokens"]
        .as_u64()
        .is_none_or(|n| n < context.saturating_add(output))
    {
        return Err(Error::Budget);
    }
    // Confidential/restricted inputs require an exclusive Task on this residency.
    if spec["gateway"]["sensitivity"] != "public" {
        let task = spec["gateway"]["exclusive_task"].as_str();
        if task != Some(b.task.as_str()) || parallel != 1 {
            return Err(Error::Denied(
                "sensitive model needs exclusive Task instance".into(),
            ));
        }
    }
    let token = new_id("permit");
    tx.execute(
        "INSERT INTO model_gateway_call VALUES(?1,?2,?3,?4,'Prepared',NULL)",
        params![call, token, instance, generation],
    )?;
    event(
        tx,
        "model_gateway_prepared",
        &b.task,
        json!({"call_id":call,"agent_id":b.agent,"task_id":b.task,"workspace_id":b.workspace,"session_id":b.session,"model_instance_id":instance,"generation":generation}),
    )?;
    Ok(Some(token))
}
impl Registry {
    /// Kernel evidence is written by the trusted Host supervisor, never by an Agent.
    pub fn record_model_limits(
        &mut self,
        actor: &Actor,
        instance: &str,
        generation: &str,
        evidence: Value,
    ) -> Result<()> {
        core(actor)?;
        let tx = self.db.transaction()?;
        let current: Option<String> = tx
            .query_row(
                "SELECT generation FROM model_runtime WHERE instance_id=?1",
                [instance],
                |r| r.get(0),
            )
            .optional()?;
        let kind = if generation.is_empty() && evidence["refused"] == true {
            let exists: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM model_instance WHERE id=?1)",
                [instance],
                |r| r.get(0),
            )?;
            if !exists {
                return Err(Error::Conflict);
            }
            "model_kernel_limits_denied"
        } else {
            if current.as_deref() != Some(generation) {
                return Err(Error::Conflict);
            }
            "model_kernel_limits"
        };
        event(
            &tx,
            kind,
            instance,
            json!({"generation":generation,"evidence":evidence}),
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Trusted broker reports its actual bound filesystem inode. This is internal
    /// service metadata, not another native object or an Agent registration API.
    pub fn register_model_listener(
        &mut self,
        actor: &Actor,
        path: &str,
        pid: u32,
        device: u64,
        inode: u64,
    ) -> Result<()> {
        core(actor)?;
        if pid == 0 || inode == 0 || !std::path::Path::new(path).is_absolute() {
            return Err(Error::Invalid("listener identity".into()));
        }
        let tx = self.db.transaction()?;
        event(
            &tx,
            "model_gateway_listener",
            "models",
            json!({"path":path,"pid":pid,"device":device,"inode":inode,"authority":"trusted host bound listener, not Agent-supplied metadata"}),
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn model_listeners(&self, actor: &Actor) -> Result<Value> {
        core(actor)?;
        let mut q = self.db.prepare(
            "SELECT payload_json FROM ledger WHERE kind='model_gateway_listener' ORDER BY seq",
        )?;
        let rows = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(Value::Array(
            rows.iter().map(|s| decode(s)).collect::<Result<Vec<_>>>()?,
        ))
    }

    pub fn models(&self, actor: &Actor) -> Result<Value> {
        core(actor)?;
        let mut q = self
            .db
            .prepare("SELECT id,spec_json FROM model ORDER BY id")?;
        let rows = q
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(Value::Array(
            rows.into_iter()
                .map(|(id, spec)| Ok(json!({"id":id,"spec":decode::<Value>(&spec)?})))
                .collect::<Result<Vec<_>>>()?,
        ))
    }

    pub fn configure_model_gateway(
        &mut self,
        actor: &Actor,
        model: &str,
        version: u64,
        policy: Value,
    ) -> Result<Value> {
        owner(actor)?;
        if policy["cache"] != "disabled"
            || policy["workspaces"]
                .as_array()
                .is_none_or(|v| v.is_empty() || v.iter().any(|s| s.as_str().is_none()))
            || policy["max_output_tokens"]
                .as_u64()
                .is_none_or(|n| n == 0 || n > 65536)
            || policy["workload_classes"].as_array().is_none_or(|xs| {
                xs.is_empty()
                    || xs.iter().any(|v| {
                        !matches!(v.as_str(), Some("inference" | "generation" | "execution"))
                    })
            })
            || !matches!(
                policy["sensitivity"].as_str(),
                Some("public" | "confidential" | "restricted")
            )
        {
            return Err(Error::Invalid(
                "explicit bounded gateway policy required".into(),
            ));
        }
        if policy
            .get("timeout_seconds")
            .is_some_and(|n| n.as_u64().is_none_or(|v| !(1..=180).contains(&v)))
        {
            return Err(Error::Invalid(
                "bounded Owner model timeout 1..180s (default 45s)".into(),
            ));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old:Option<String>=tx.query_row("SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='model_gateway_policy' ORDER BY seq DESC LIMIT 1",[model],|r|r.get(0)).optional()?;
        let old: Value = old
            .map(|s| decode(&s))
            .transpose()?
            .unwrap_or(json!({"version":0}));
        if old["version"] != version {
            return Err(Error::Conflict);
        }
        let data: String =
            tx.query_row("SELECT spec_json FROM model WHERE id=?1", [model], |r| {
                r.get(0)
            })?;
        let mut spec: Value = decode(&data)?;
        let (bound_model,): (String,) = tx.query_row(
            "SELECT model_id FROM model_instance WHERE id=?1",
            [policy["instance_id"].as_str().ok_or(Error::Conflict)?],
            |r| Ok((r.get(0)?,)),
        )?;
        if bound_model != model
            || spec["cloud"] != false
            || spec["scheduling"]["mode"] != "gateway_fifo"
        {
            return Err(Error::Denied("local instance/model route policy".into()));
        }
        for workspace in policy["workspaces"].as_array().unwrap() {
            let exists: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM workspace WHERE id=?1)",
                [workspace.as_str().unwrap()],
                |r| r.get(0),
            )?;
            if !exists {
                return Err(Error::NotFound("Workspace scope".into()));
            }
        }
        spec["gateway"] = policy.clone();
        tx.execute(
            "UPDATE model SET spec_json=?1 WHERE id=?2",
            params![encode(&spec)?, model],
        )?;
        let result = json!({"version":version+1,"policy":policy});
        event(&tx, "model_gateway_policy", model, result.clone())?;
        tx.commit()?;
        Ok(result)
    }

    pub fn model_instances(&self, actor: &Actor) -> Result<Value> {
        core(actor)?;
        let mut q=self.db.prepare("SELECT i.id,i.model_id,i.state,r.generation,r.version,r.pid,r.endpoint,r.backend_model,r.resources_json,r.held,r.verified FROM model_instance i LEFT JOIN model_runtime r ON r.instance_id=i.id ORDER BY i.id")?;
        Ok(Value::Array(q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"model_id":r.get::<_,String>(1)?,"state":r.get::<_,String>(2)?,"generation":r.get::<_,Option<String>>(3)?,"version":r.get::<_,Option<u64>>(4)?,"pid":r.get::<_,Option<u32>>(5)?,"endpoint":r.get::<_,Option<String>>(6)?,"backend_model":r.get::<_,Option<String>>(7)?,"resources_json":r.get::<_,Option<String>>(8)?,"held":r.get::<_,Option<bool>>(9)?,"verified":r.get::<_,Option<bool>>(10)?})))?.collect::<rusqlite::Result<Vec<_>>>()?))
    }
    pub fn load_local_model(
        &mut self,
        actor: &Actor,
        instance: &str,
        spec: LocalModelSpec,
    ) -> Result<Value> {
        owner(actor)?;
        spec.validate()?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let state: String = tx.query_row(
            "SELECT state FROM model_instance WHERE id=?1",
            [instance],
            |r| r.get(0),
        )?;
        let previous: Option<(String, bool)> = tx
            .query_row(
                "SELECT resources_json,held FROM model_runtime WHERE instance_id=?1",
                [instance],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if !matches!(state.as_str(), "Loading" | "Unloaded" | "Failed")
            || previous.as_ref().is_some_and(|p| p.1)
        {
            return Err(Error::Conflict);
        }
        let policy: String = tx
            .query_row(
                "SELECT policy_json FROM host_resource_policy WHERE id=1",
                [],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| Error::Denied("host capacity policy required".into()))?;
        let policy: HostResourcePolicy = decode(&policy)?;
        let mut used = resources::used(&tx)?;
        if let Some((data, false)) = previous {
            used.storage_bytes = used
                .storage_bytes
                .checked_sub(decode::<HostResources>(&data)?.storage_bytes)
                .ok_or(Error::Budget)?;
        }
        if !used
            .checked(spec.resources, true)?
            .fits(policy.capacity.checked(policy.reserved, false)?)
        {
            return Err(Error::Budget);
        }
        let generation = new_id("model-run");
        tx.execute("INSERT INTO model_runtime(instance_id,generation,endpoint,backend_model,context_tokens,parallelism,resources_json,held) VALUES(?1,?2,?3,?4,?5,?6,?7,1) ON CONFLICT(instance_id) DO UPDATE SET generation=excluded.generation,version=version+1,endpoint=excluded.endpoint,backend_model=excluded.backend_model,context_tokens=excluded.context_tokens,parallelism=excluded.parallelism,pid=NULL,resources_json=excluded.resources_json,held=1,verified=0",params![instance,generation,spec.endpoint,spec.backend_model,spec.context_tokens,spec.parallelism,encode(&spec.resources)?])?;
        tx.execute(
            "UPDATE model_instance SET state='Loading' WHERE id=?1",
            [instance],
        )?;
        event(
            &tx,
            "model_loading",
            instance,
            json!({"generation":generation,"resources":spec.resources,"endpoint":spec.endpoint}),
        )?;
        tx.commit()?;
        Ok(json!({"generation":generation,"state":"Loading"}))
    }
    pub fn model_lifecycle(
        &mut self,
        actor: &Actor,
        instance: &str,
        generation: &str,
        next: &str,
        pid: Option<u32>,
        evidence: &str,
    ) -> Result<()> {
        core(actor)?;
        if evidence.is_empty() {
            return Err(Error::Invalid("model lifecycle evidence".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (old,expected):(String,String)=tx.query_row("SELECT i.state,r.generation FROM model_instance i JOIN model_runtime r ON r.instance_id=i.id WHERE i.id=?1",[instance],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if generation != expected {
            return Err(Error::Conflict);
        }
        let valid = matches!(
            (old.as_str(), next),
            ("Loading", "Loading")
                | ("Loading", "Ready")
                | ("Loading", "Failed")
                | ("Ready", "Draining")
                | ("Ready", "Failed")
                | ("Draining", "Unloaded")
                | ("Draining", "Failed")
                | ("Failed", "Unloaded")
        );
        if !valid || (matches!(next, "Ready" | "Loading") && pid.is_none_or(|n| n == 0)) {
            return Err(Error::Conflict);
        }
        if next == "Unloaded" {
            let active:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM model_gateway_call WHERE instance_id=?1 AND state IN ('Prepared','Claimed'))",[instance],|r|r.get(0))?;
            if active {
                return Err(Error::Reconciliation);
            }
        }
        if next == "Failed" {
            let mut q=tx.prepare("SELECT q.task_id,q.call_id FROM model_queue q JOIN model_gateway_call g ON g.call_id=q.call_id WHERE g.instance_id=?1 AND q.state='Dispatched'")?;
            let calls = q
                .query_map([instance], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            drop(q);
            for (task, call) in calls {
                tx.execute("UPDATE model_reservation SET state='Unknown' WHERE call_id=?1 AND state='Reserved'",[&call])?;
                tx.execute(
                    "UPDATE model_queue SET state='Unknown' WHERE call_id=?1",
                    [&call],
                )?;
                tx.execute(
                    "UPDATE model_gateway_call SET state='Unknown' WHERE call_id=?1",
                    [&call],
                )?;
                crate::model_budget::block_model_task(
                    &tx,
                    &task,
                    "model instance failed; no replay",
                )?;
            }
        }
        tx.execute(
            "UPDATE model_instance SET state=?1 WHERE id=?2",
            params![next, instance],
        )?;
        tx.execute("UPDATE model_runtime SET version=version+1,pid=CASE WHEN ?3='Failed' THEN COALESCE(?1,pid) ELSE ?1 END,verified=?2,held=CASE WHEN ?3='Unloaded' THEN 0 ELSE held END WHERE instance_id=?4",params![pid,next=="Ready",next,instance])?;
        event(
            &tx,
            "model_lifecycle",
            instance,
            json!({"from":old,"to":next,"generation":generation,"pid":pid,"evidence":evidence}),
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn set_model_priority(&mut self, actor: &Actor, task: &str, priority: u32) -> Result<()> {
        owner(actor)?;
        if priority > 100 {
            return Err(Error::Invalid("priority 0..100".into()));
        }
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO model_task_priority VALUES(?1,?2) ON CONFLICT(task_id) DO UPDATE SET priority=excluded.priority",params![task,priority])?;
        event(
            &tx,
            "model_priority",
            task,
            json!({"priority":priority,"equal_priority":"FIFO"}),
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn claim_gateway(&mut self, actor: &Actor, token: &str, body: &Value) -> Result<Value> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (call, instance, generation, state): (String, String, String, String) = tx.query_row(
            "SELECT call_id,instance_id,generation,state FROM model_gateway_call WHERE token=?1",
            [token],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
        if state != "Prepared" {
            return Err(Error::Reconciliation);
        }
        let binding: String = tx.query_row(
            "SELECT binding_json FROM model_queue WHERE call_id=?1 AND state='Dispatched' AND expires_ms>unixepoch()*1000",
            [&call],
            |r| r.get(0),
        )?;
        let binding: Value = decode(&binding)?;
        let turn = binding["turn"].as_str().ok_or(Error::Conflict)?;
        let native = binding["native"].as_str().ok_or(Error::Conflict)?;
        let b = crate::model_budget::model_binding(
            &tx,
            turn,
            native,
            binding["provider"].as_str().ok_or(Error::Conflict)?,
            binding["model"].as_str().ok_or(Error::Conflict)?,
        )?;
        let spec = gateway_config(&tx, &b.model_id)?.ok_or(Error::Conflict)?;
        if body["model"] != spec["budget_profile"]["model"]
            || body["stream"] != true
            || !body["messages"].is_array()
            || body["max_tokens"].as_u64().is_none_or(|n| {
                n == 0 || n > spec["gateway"]["max_output_tokens"].as_u64().unwrap_or(0)
            })
        {
            return Err(Error::Denied(
                "gateway request model/output/stream bounds".into(),
            ));
        }
        let (endpoint,backend,current):(String,String,String)=tx.query_row("SELECT r.endpoint,r.backend_model,r.generation FROM model_runtime r JOIN model_instance i ON i.id=r.instance_id WHERE i.id=?1 AND i.state='Ready' AND r.verified=1 AND r.held=1",[&instance],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        live_scope(&tx, &spec, &b, &instance, &generation)?;
        if current != generation {
            return Err(Error::Conflict);
        }
        // Recheck Workspace scope at actual dispatch, not just at permit issuance.
        if !spec["gateway"]["workspaces"]
            .as_array()
            .is_some_and(|xs| xs.iter().any(|w| w.as_str() == Some(&b.workspace)))
        {
            return Err(Error::Denied("Workspace scope revoked".into()));
        }
        // A handed-back native file must be read through the original tool.
        // Request the tool from the real backend; never fabricate a tool/result.
        let required_read:Option<String>=tx.query_row("SELECT read_path FROM workspace_control WHERE task_id=?1 AND phase='Agent' AND read_path IS NOT NULL",[&b.task],|r|r.get(0)).optional()?;
        let forced_tool = if required_read.is_some() {
            if !body["tools"].as_array().is_some_and(|tools| {
                tools
                    .iter()
                    .any(|t| t["type"] == "function" && t["function"]["name"] == "read")
            }) {
                return Err(Error::Denied(
                    "handed-back workspace requires declared original read tool".into(),
                ));
            }
            Some(json!("required"))
        } else {
            None
        };
        tx.execute(
            "UPDATE model_gateway_call SET state='Claimed',request_hash=?1 WHERE call_id=?2",
            params![digest(&serde_json::to_vec(body)?), call],
        )?;
        event(
            &tx,
            "model_gateway_dispatch",
            &b.task,
            json!({"call_id":call,"model_instance_id":instance,"generation":generation,"agent_id":b.agent,"workspace_id":b.workspace,"runtime_session_id":b.session,"intent_before_backend":true,"forced_fresh_read":required_read,"tool_choice":forced_tool}),
        )?;
        tx.commit()?;
        Ok(
            json!({"call_id":call,"turn_id":turn,"native_session_ref":native,"model_instance_id":instance,"endpoint":endpoint,"backend_model":backend,"generation":generation,"timeout_seconds":spec["gateway"]["timeout_seconds"].as_u64().unwrap_or(45),"forced_tool":forced_tool,"required_read_path":required_read}),
        )
    }
    pub fn gateway_result(&mut self, actor: &Actor, token: &str, receipt: Value) -> Result<Value> {
        core(actor)?;
        let (call,turn):(String,String)=self.db.query_row("SELECT g.call_id,r.turn_id FROM model_gateway_call g JOIN model_reservation r ON r.call_id=g.call_id WHERE g.token=?1 AND g.state IN ('Claimed','Unknown','Completed')",[token],|r|Ok((r.get(0)?,r.get(1)?)))?;
        self.settle_model_trusted(actor, &turn, &call, receipt)
    }

    pub fn gateway_status(&self, actor: &Actor, turn: &str, call: &str) -> Result<Value> {
        core(actor)?;
        let state: String = self.db.query_row(
            "SELECT state FROM model_reservation WHERE turn_id=?1 AND call_id=?2",
            params![turn, call],
            |r| r.get(0),
        )?;
        Ok(json!({"call_id":call,"state":state}))
    }
}
