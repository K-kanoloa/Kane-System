//! Durable single-host admission. Unknown cleanup retains reservations.
use crate::{registry::*, *};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const SANDBOX_TMP_BYTES: u64 = 64 * 1024 * 1024;
pub(crate) fn check_task_storage(
    tx: &rusqlite::Transaction<'_>,
    task: &str,
    workspace: &str,
    additional: u64,
) -> Result<()> {
    let mut reserved = 0u64;
    let mut managed = false;
    let mut q=tx.prepare("SELECT l.payload_json FROM ledger l JOIN agent a ON l.object_id=a.id WHERE a.task_id=?1 AND l.kind='runtime_configured'")?;
    for row in q.query_map([task], |r| r.get::<_, String>(0))? {
        let config: Value = decode(&row?)?;
        if config["sandbox"]["host_resources"] == true {
            managed = true;
            reserved = reserved
                .checked_add(
                    config["sandbox"]["home_capacity_bytes"]
                        .as_u64()
                        .ok_or(Error::Budget)?,
                )
                .and_then(|v| v.checked_add(SANDBOX_TMP_BYTES))
                .ok_or(Error::Budget)?;
        }
    }
    if !managed {
        return Ok(());
    }
    let spec: String = tx.query_row("SELECT spec_json FROM task WHERE id=?1", [task], |r| {
        r.get(0)
    })?;
    let spec: TaskSpec = decode(&spec)?;
    let files: u64 = tx.query_row(
        "SELECT COALESCE(SUM(length(content)),0) FROM workspace_file WHERE workspace_id=?1",
        [workspace],
        |r| r.get(0),
    )?;
    let artifacts: u64 = tx.query_row(
        "SELECT COALESCE(SUM(length(CAST(content AS BLOB))),0) FROM artifact WHERE task_id=?1",
        [task],
        |r| r.get(0),
    )?;
    let total = reserved
        .checked_add(files)
        .and_then(|v| v.checked_add(artifacts))
        .and_then(|v| v.checked_add(additional))
        .ok_or(Error::Budget)?;
    if total > spec.workload.max_storage_bytes {
        return Err(Error::Budget);
    }
    Ok(())
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostResources {
    pub cpu_millis: u64,
    pub memory_bytes: u64,
    pub pids: u64,
    pub storage_bytes: u64,
}
impl HostResources {
    fn array(self) -> [u64; 4] {
        [
            self.cpu_millis,
            self.memory_bytes,
            self.pids,
            self.storage_bytes,
        ]
    }
    fn from_array(a: [u64; 4]) -> Self {
        Self {
            cpu_millis: a[0],
            memory_bytes: a[1],
            pids: a[2],
            storage_bytes: a[3],
        }
    }
    pub(crate) fn checked(self, other: Self, add: bool) -> Result<Self> {
        let mut out = [0; 4];
        for (i, (a, b)) in self.array().into_iter().zip(other.array()).enumerate() {
            out[i] = if add {
                a.checked_add(b)
            } else {
                a.checked_sub(b)
            }
            .filter(|v| *v <= i64::MAX as u64)
            .ok_or(Error::Budget)?;
        }
        Ok(Self::from_array(out))
    }
    pub(crate) fn fits(self, limit: Self) -> bool {
        self.array()
            .into_iter()
            .zip(limit.array())
            .all(|(a, b)| a <= b)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostResourcePolicy {
    pub capacity: HostResources,
    pub reserved: HostResources,
}
pub(crate) fn used(db: &rusqlite::Connection) -> Result<HostResources> {
    let mut total = HostResources::default();
    let mut q = db.prepare("SELECT resources_json,held FROM sandbox_reservation")?;
    for row in q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, bool>(1)?)))? {
        let (data, held) = row?;
        let mut amount: HostResources = decode(&data)?;
        if !held {
            amount.cpu_millis = 0;
            amount.memory_bytes = 0;
            amount.pids = 0;
        }
        total = total.checked(amount, true)?;
    }
    let mut models = db.prepare("SELECT resources_json,held FROM model_runtime")?;
    for row in models.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, bool>(1)?)))? {
        let (data, held) = row?;
        let mut amount: HostResources = decode(&data)?;
        if !held {
            amount.cpu_millis = 0;
            amount.memory_bytes = 0;
            amount.pids = 0;
        }
        total = total.checked(amount, true)?;
    }
    Ok(total)
}
impl Registry {
    pub fn configure_host_resources(
        &mut self,
        actor: &Actor,
        version: u64,
        policy: HostResourcePolicy,
    ) -> Result<Value> {
        owner(actor)?;
        if policy
            .capacity
            .array()
            .iter()
            .any(|v| *v == 0 || *v > i64::MAX as u64)
        {
            return Err(Error::Budget);
        }
        let available = policy.capacity.checked(policy.reserved, false)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old: Option<u64> = tx
            .query_row(
                "SELECT version FROM host_resource_policy WHERE id=1",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if old.unwrap_or(0) != version || !used(&tx)?.fits(available) {
            return Err(Error::Conflict);
        }
        tx.execute("INSERT INTO host_resource_policy VALUES(1,?1,?2) ON CONFLICT(id) DO UPDATE SET version=excluded.version,policy_json=excluded.policy_json",params![version+1,encode(&policy)?])?;
        event(
            &tx,
            "host_resource_policy",
            "host",
            json!({"version":version+1,"policy":policy}),
        )?;
        tx.commit()?;
        self.host_resources(actor)
    }
    pub fn host_resources(&self, actor: &Actor) -> Result<Value> {
        core(actor)?;
        let tx = self.db.unchecked_transaction()?;
        let policy: Option<(u64, String)> = tx
            .query_row(
                "SELECT version,policy_json FROM host_resource_policy WHERE id=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let mut q = tx.prepare(
            "SELECT agent_id,task_id,token,resources_json,held FROM sandbox_reservation",
        )?;
        let claims=q.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,bool>(4)?)))?.map(|r|{let (agent,task,token,resources,held)=r?;Ok(json!({"agent_id":agent,"task_id":task,"token":token,"resources":decode::<Value>(&resources)?,"held":held}))}).collect::<Result<Vec<_>>>()?;
        Ok(
            json!({"version":policy.as_ref().map(|p|p.0).unwrap_or(0),"policy":policy.map(|p|decode::<Value>(&p.1)).transpose()?,"used":used(&tx)?,"claims":claims,"unknown_cleanup_releases":false}),
        )
    }
    pub fn reserve_host_resources(&mut self, actor: &Actor, id: &str) -> Result<Value> {
        core(actor)?;
        let result = self.try_reserve_host_resources(id);
        if matches!(&result, Err(Error::Budget)) {
            let tx = self.db.transaction()?;
            event(
                &tx,
                "host_resources_denied",
                id,
                json!({"reason":"host or Task capacity exceeded","automatic_fallback":false}),
            )?;
            tx.commit()?;
        }
        result
    }
    fn try_reserve_host_resources(&mut self, id: &str) -> Result<Value> {
        let a = self.agent(id)?;
        let task = self.task(&a.task_id)?;
        if task.state.terminal() {
            return Err(Error::Conflict);
        }
        let config = self.runtime_status(id)?["config"].clone();
        let s = &config["sandbox"];
        if s["host_resources"] != true {
            return Err(Error::Denied("managed host resources required".into()));
        }
        let home = s["home_capacity_bytes"].as_u64().ok_or(Error::Budget)?;
        if home == 0
            || home
                .checked_add(SANDBOX_TMP_BYTES)
                .is_none_or(|n| n > task.spec.workload.max_storage_bytes)
        {
            return Err(Error::Budget);
        }
        let amount = HostResources {
            cpu_millis: s["cpu_millis"].as_u64().ok_or(Error::Budget)?,
            memory_bytes: s["memory_bytes"].as_u64().ok_or(Error::Budget)?,
            pids: s["pids"].as_u64().ok_or(Error::Budget)?,
            storage_bytes: task.spec.workload.max_storage_bytes,
        };
        if amount.array().contains(&0) || amount.memory_bytes > task.spec.workload.max_memory_bytes
        {
            return Err(Error::Budget);
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        check_task_storage(&tx, &a.task_id, &a.workspace_id, 0)?;
        let previous: Option<(String, String, bool)> = tx
            .query_row(
                "SELECT token,resources_json,held FROM sandbox_reservation WHERE agent_id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        if let Some((token, old, true)) = &previous {
            if decode::<HostResources>(old)? != amount {
                return Err(Error::Conflict);
            }
            return Ok(json!({"token":token,"resources":amount,"held":true}));
        }
        let p: String = tx
            .query_row(
                "SELECT policy_json FROM host_resource_policy WHERE id=1",
                [],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| Error::Denied("host policy not configured".into()))?;
        let p: HostResourcePolicy = decode(&p)?;
        let mut total = used(&tx)?;
        if let Some((_, old, false)) = &previous {
            total.storage_bytes = total
                .storage_bytes
                .checked_sub(decode::<HostResources>(old)?.storage_bytes)
                .ok_or(Error::Budget)?;
        }
        if !total
            .checked(amount, true)?
            .fits(p.capacity.checked(p.reserved, false)?)
        {
            return Err(Error::Budget);
        }
        let token = new_id("resource");
        tx.execute("INSERT INTO sandbox_reservation VALUES(?1,?2,?3,?4,1) ON CONFLICT(agent_id) DO UPDATE SET task_id=excluded.task_id,token=excluded.token,resources_json=excluded.resources_json,held=1",params![id,a.task_id,token,encode(&amount)?])?;
        event(
            &tx,
            "host_resources_reserved",
            id,
            json!({"token":token,"task_id":a.task_id,"resources":amount}),
        )?;
        tx.commit()?;
        Ok(json!({"token":token,"resources":amount,"held":true}))
    }
    pub fn release_host_resources(&mut self, actor: &Actor, id: &str, token: &str) -> Result<()> {
        core(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let old: Option<(String, bool)> = tx
            .query_row(
                "SELECT token,held FROM sandbox_reservation WHERE agent_id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let Some((expected, held)) = old else {
            return Err(Error::Conflict);
        };
        if expected != token {
            return Err(Error::Conflict);
        }
        if held {
            tx.execute(
                "UPDATE sandbox_reservation SET held=0 WHERE agent_id=?1",
                [id],
            )?;
            event(
                &tx,
                "host_resources_released",
                id,
                json!({"token":token,"basis":"trusted host asserts confirmed sandbox absence"}),
            )?;
        }
        tx.commit()?;
        Ok(())
    }
}
