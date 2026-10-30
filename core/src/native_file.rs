//! Restricted native-file creation through the existing managed-tool intent.
//! Agent mounts stay read-only. Only flat, new UTF-8 files are supported here.
//! Intent/Dispatch are already durable; filesystem and SQLite are NOT one
//! transaction. A crash after publication leaves an Unknown action, never replay.
use crate::{registry::*, *};
use rusqlite::params;
use serde_json::{Value, json};

impl Registry {
    pub(crate) fn native_file_commit(
        &mut self,
        id: &str,
        agent: &str,
        args: &Value,
    ) -> Result<Value> {
        let a = self.agent(agent)?;
        let action = self.action(id)?;
        let body = &action.payload["body"];
        let lease = &body["native_file"];
        let filename = args["file_path"]
            .as_str()
            .ok_or_else(|| Error::Invalid("file name".into()))?;
        let content = args["content"]
            .as_str()
            .ok_or_else(|| Error::Invalid("UTF-8 content".into()))?;
        if action.state != "Dispatching"
            || body["tool"] != "write"
            || !lease.is_object()
            || body["arguments_hash"] != digest(&serde_json::to_vec(args)?)
            || filename.is_empty()
            || filename.len() > 200
            || filename.starts_with('.')
            || filename.contains(['/', '\\', '\0'])
            || content.len() > 4096
        {
            return Err(Error::Denied("restricted native file request".into()));
        }
        let status = self.runtime_status(agent)?;
        let root = status["config"]["cwd"]
            .as_str()
            .ok_or_else(|| Error::Invalid("runtime workspace mapping".into()))?;
        if status["config"]["sandbox"]["native_file_proxy"] != true {
            return Err(Error::Denied("native file proxy disabled".into()));
        }
        let task = self.task(&a.task_id)?;
        let data_limit = task
            .spec
            .workload
            .max_storage_bytes
            .checked_sub(
                status["config"]["sandbox"]["home_capacity_bytes"]
                    .as_u64()
                    .unwrap_or(0),
            )
            .ok_or(Error::Budget)?;
        if task.state != TaskState::Running {
            return Err(Error::Denied("inactive task".into()));
        }
        let epoch = lease["epoch"].as_u64().ok_or(Error::Conflict)?;
        let version = lease["version"].as_u64().ok_or(Error::Conflict)?;
        // The actual file proxy, not the Agent, consumes this durable one-shot
        // attempt before touching the filesystem. Missing commit evidence after
        // an attempt is uncertain even if the Core itself did not restart.
        {
            let tx = self
                .db
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            let eligible: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM outbox WHERE id=?1 AND agent_id=?2 AND state='Dispatching') AND NOT EXISTS(SELECT 1 FROM ledger WHERE object_id=?1 AND kind='native_file_attempt')",
                params![id,agent], |r| r.get(0))?;
            if !eligible {
                return Err(Error::Reconciliation);
            }
            event(
                &tx,
                "native_file_attempt",
                id,
                json!({"path":filename,"hash":digest(content.as_bytes()),"automatic_retry":false}),
            )?;
            tx.commit()?;
        }
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::collaboration::admission(&tx, &a.task_id, Some(agent))?;
        let dispatching: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM outbox o JOIN task t ON t.id=o.task_id WHERE o.id=?1 AND o.agent_id=?2 AND o.task_id=?3 AND o.state='Dispatching' AND t.state='Running')",
            params![id,agent,a.task_id], |r| r.get(0))?;
        if !dispatching {
            return Err(Error::Reconciliation);
        }
        // Serialize permission, epoch and publication with every lease transfer.
        crate::tool_execution::validate_managed(&tx, agent, &a.task_id, &action.payload)?;
        Self::check_capability(&tx, agent, "modify", &a.workspace_id)?;
        Self::check_capability(
            &tx,
            agent,
            "execute",
            action.payload["target"].as_str().ok_or(Error::Conflict)?,
        )?;
        let recorded: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM ledger WHERE kind='native_file_commit' AND object_id=?1)",
            [id],
            |r| r.get(0),
        )?;
        if recorded {
            return Err(Error::Reconciliation);
        }
        let used: u64 = tx.query_row(
            "SELECT COALESCE(SUM(length(content)),0) FROM workspace_file WHERE workspace_id=?1",
            [&a.workspace_id],
            |r| r.get(0),
        )?;
        if used
            .checked_add(content.len() as u64)
            .is_none_or(|n| n > data_limit)
        {
            return Err(Error::Budget);
        }
        crate::resources::check_task_storage(
            &tx,
            &a.task_id,
            &a.workspace_id,
            content.len() as u64,
        )?;
        if tx.execute("UPDATE workspace SET version=version+1 WHERE id=?1 AND lease_epoch=?2 AND version=?3 AND holder=?4 AND expires_at>?5",
            params![a.workspace_id,epoch,version,agent,now()])? != 1 {
            return Err(Error::Conflict);
        }
        // Create-only publication: never overwrite a human file or follow its
        // symlink, even if it appeared since Intent. No shell or Agent code here.
        publish_new(root, filename, content.as_bytes())?;
        tx.execute(
            "INSERT INTO workspace_file VALUES(?1,?2,?3)",
            params![a.workspace_id, filename, content.as_bytes()],
        )?;
        let hash = digest(content.as_bytes());
        event(
            &tx,
            "native_file_commit",
            id,
            json!({"workspace_id":a.workspace_id,"path":filename,"hash":hash,"epoch":epoch,"version":version+1,"bytes":content.len(),"operation":"create"}),
        )?;
        event(
            &tx,
            "file_change",
            &a.workspace_id,
            json!({"action":id,"path":filename,"hash":hash,"epoch":epoch,"version":version+1}),
        )?;
        tx.commit()?;
        Ok(
            json!({"operation":"create","version":format!("kane:{hash}"),"before":null,"after":content}),
        )
    }
}

#[cfg(target_os = "linux")]
fn publish_new(root: &str, name: &str, content: &[u8]) -> Result<()> {
    use rustix::fs::{AtFlags, Mode, OFlags, fsync, linkat, open, openat, unlinkat};
    use std::io::Write;
    let err = |e: rustix::io::Errno| Error::Io(std::io::Error::from(e));
    let dir = open(
        root,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(err)?;
    let temp = new_id(".kane-stage");
    let fd = openat(
        &dir,
        temp.as_str(),
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(err)?;
    let result = (|| -> Result<()> {
        let mut file = std::fs::File::from(fd);
        file.write_all(content)?;
        file.sync_all()?;
        // linkat is an atomic no-replace publication on the same filesystem.
        linkat(&dir, temp.as_str(), &dir, name, AtFlags::empty()).map_err(err)?;
        fsync(&dir).map_err(err)?;
        Ok(())
    })();
    let cleanup = unlinkat(&dir, temp.as_str(), AtFlags::empty()).map_err(err);
    result?;
    cleanup?;
    fsync(&dir).map_err(err)?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn publish_new(_: &str, _: &str, _: &[u8]) -> Result<()> {
    Err(Error::Unsupported("native files require Linux".into()))
}
