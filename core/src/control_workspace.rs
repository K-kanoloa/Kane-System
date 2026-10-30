//! Native text editing and human/Agent fencing. File publication is not a
//! SQLite transaction: durable outbox intent precedes the filesystem effect.
use crate::{
    control_auth::{field, web_session},
    registry::*,
    *,
};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{Read, Write},
    os::unix::fs::MetadataExt,
};
const FILE_LIMIT: usize = 65536;
fn name(path: &str) -> Result<()> {
    if path.is_empty()
        || path.len() > 200
        || path.starts_with('.')
        || path.contains(['/', '\\', '\0'])
        || path.chars().any(char::is_control)
        || ![".txt", ".md", ".csv", ".json"]
            .iter()
            .any(|ext| path.ends_with(ext))
    {
        return Err(Error::Denied(
            "bounded native document/data filename required".into(),
        ));
    }
    Ok(())
}
fn root(db: &Connection, task: &str) -> Result<(String, String, String)> {
    let (agent, ws): (String, String) = db.query_row(
        "SELECT a.id,a.workspace_id FROM agent a WHERE a.task_id=?1 ORDER BY a.rowid LIMIT 1",
        [task],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let config:String=db.query_row("SELECT payload_json FROM ledger WHERE kind='runtime_configured' AND object_id=?1 ORDER BY seq DESC LIMIT 1",[&agent],|r|r.get(0))?;
    let config: Value = decode(&config)?;
    if config["sandbox"]["native_file_proxy"] != true || !config["sandbox"].is_object() {
        return Err(Error::Denied(
            "controlled read-only Linux workspace required".into(),
        ));
    }
    Ok((agent, ws, field(&config, "cwd", 4096)?.to_owned()))
}
fn dir(root: &str) -> Result<rustix::fd::OwnedFd> {
    rustix::fs::open(
        root,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|e| Error::Io(e.into()))
}
fn read_at(root: &str, path: &str) -> Result<Option<(Vec<u8>, u64)>> {
    name(path)?;
    let directory = dir(root)?;
    let fd = match rustix::fs::openat(
        &directory,
        path,
        rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC
            | rustix::fs::OFlags::NONBLOCK,
        rustix::fs::Mode::empty(),
    ) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(e) => return Err(Error::Io(e.into())),
    };
    let file = std::fs::File::from(fd);
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.len() > FILE_LIMIT as u64 {
        return Err(Error::Denied(
            "regular, unlinked, bounded UTF-8 native text only".into(),
        ));
    }
    let mut bytes = vec![];
    file.take((FILE_LIMIT + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > FILE_LIMIT || std::str::from_utf8(&bytes).is_err() {
        return Err(Error::Denied("bounded UTF-8 document required".into()));
    }
    Ok(Some((bytes, meta.ino())))
}
fn inventory(root: &str) -> Result<BTreeMap<String, Value>> {
    let mut files = BTreeMap::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        let path = entry
            .file_name()
            .to_str()
            .ok_or_else(|| Error::Invalid("UTF-8 filename".into()))?
            .to_owned();
        if name(&path).is_err() {
            continue;
        }
        if files.len() >= 128 {
            return Err(Error::Budget);
        }
        let (content, _) = read_at(root, &path)?.ok_or(Error::Conflict)?;
        files.insert(path, json!({"hash":digest(&content),"bytes":content.len()}));
    }
    Ok(files)
}
fn unsettled(db: &Connection, task: &str) -> Result<bool> {
    Ok(db.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND state IN ('Pending','Dispatching','Unknown')) OR EXISTS(SELECT 1 FROM model_reservation WHERE task_id=?1 AND state!='Settled')",[task],|r|r.get(0))?)
}
pub(crate) fn human_fenced(db: &Connection, task: &str) -> Result<bool> {
    Ok(db.query_row("SELECT EXISTS(SELECT 1 FROM workspace_control WHERE task_id=?1 AND phase IN ('TakingOver','Human','Returning'))",[task],|r|r.get(0))?)
}
pub(crate) fn require_fresh_read(db: &Connection, task: &str) -> Result<()> {
    let expected: Option<String> = db
        .query_row(
            "SELECT read_path FROM workspace_control WHERE task_id=?1 AND phase='Agent'",
            [task],
            |r| r.get(0),
        )
        .optional()?
        .flatten();
    if expected.is_some() {
        return Err(Error::Denied(
            "Agent must acknowledge actual read of handed-back native version".into(),
        ));
    }
    Ok(())
}
pub(crate) fn link_artifact(tx: &Transaction<'_>, artifact: &str, task: &str) -> Result<()> {
    let Ok((_, ws, path)) = root(tx, task) else {
        return Ok(());
    };
    let files = inventory(&path)?;
    let version: u64 = tx.query_row("SELECT version FROM workspace WHERE id=?1", [&ws], |r| {
        r.get(0)
    })?;
    tx.execute(
        "INSERT INTO artifact_workspace VALUES(?1,?2,?3,?4)",
        params![artifact, ws, version, encode(&files)?],
    )?;
    Ok(())
}
fn active_human(
    db: &Connection,
    token: &str,
    task: &str,
    epoch: u64,
    version: u64,
) -> Result<(String, String, String, String)> {
    let session = web_session(db, token)?;
    let (agent, ws, path) = root(db, task)?;
    let valid:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM workspace_control c JOIN workspace w ON w.id=c.workspace_id WHERE c.task_id=?1 AND c.session_id=?2 AND c.phase='Human' AND w.holder=?2 AND w.lease_epoch=?3 AND w.version=?4 AND w.expires_at>?5)",params![task,session.id,epoch,version,now()],|r|r.get(0))?;
    if !valid {
        return Err(Error::Conflict);
    }
    if unsettled(db, task)? {
        return Err(Error::Reconciliation);
    }
    Ok((session.id, agent, ws, path))
}
fn publish(root: &str, path: &str, content: &[u8], before: Option<&str>) -> Result<()> {
    use rustix::fs::{AtFlags, Mode, OFlags, fsync, linkat, openat, renameat, unlinkat};
    let directory = dir(root)?;
    let temp = new_id(".kane-edit");
    let fd = openat(
        &directory,
        temp.as_str(),
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_bits_truncate(0o600),
    )
    .map_err(|e| Error::Io(e.into()))?;
    let result = (|| -> Result<()> {
        let mut file = std::fs::File::from(fd);
        file.write_all(content)?;
        file.sync_all()?;
        let current = read_at(root, path)?;
        let current_hash = current.as_ref().map(|(b, _)| digest(b));
        if current_hash.as_deref() != before {
            return Err(Error::Conflict);
        }
        if before.is_some() {
            renameat(&directory, temp.as_str(), &directory, path)
                .map_err(|e| Error::Io(e.into()))?;
        } else {
            linkat(
                &directory,
                temp.as_str(),
                &directory,
                path,
                AtFlags::empty(),
            )
            .map_err(|e| Error::Io(e.into()))?;
        }
        fsync(&directory).map_err(|e| Error::Io(e.into()))?;
        Ok(())
    })();
    match unlinkat(&directory, temp.as_str(), AtFlags::empty()) {
        Ok(()) | Err(rustix::io::Errno::NOENT) => {}
        Err(e) if result.is_ok() => return Err(Error::Io(e.into())),
        Err(_) => {}
    }
    result
}
impl Registry {
    pub fn control_view(&self, actor: &Actor, token: &str, cursor: u64) -> Result<Value> {
        owner(actor)?;
        let session = web_session(&self.db, token)?;
        let tasks = self.tasks()?;
        let mut workspaces = vec![];
        let mut artifacts = vec![];
        for task in &tasks {
            let ws = self.workspace(&task.spec.workspace_id)?;
            let control:Option<String>=self.db.query_row("SELECT json_object('phase',phase,'session_id',session_id,'read_path',read_path,'snapshot_hash',snapshot_hash) FROM workspace_control WHERE task_id=?1",[&task.id],|r|r.get(0)).optional()?;
            let files = match root(&self.db, &task.id) {
                Ok((_, _, path)) => Some(inventory(&path)?),
                Err(Error::Denied(_)) | Err(Error::Database(_)) => None,
                Err(e) => return Err(e),
            };
            workspaces.push(json!({"task_id":task.id,"workspace":ws,"control":control.map(|s|decode::<Value>(&s)).transpose()?,"files":files,"controlled_native":files.is_some(),"own_lease":ws.holder.as_deref()==Some(&session.id),"unresolved":unsettled(&self.db,&task.id)?}));
            for (index, a) in self.artifacts(&task.id)?.iter().enumerate() {
                let mut a = serde_json::to_value(a)?;
                a["version"] = json!(index + 1);
                let provenance:Option<String>=self.db.query_row("SELECT json_object('workspace_id',workspace_id,'workspace_version',workspace_version,'files',json(files_json)) FROM artifact_workspace WHERE artifact_id=?1",[a["id"].as_str()],|r|r.get(0)).optional()?;
                a["workspace_source"] = provenance
                    .map(|s| decode::<Value>(&s))
                    .transpose()?
                    .unwrap_or(Value::Null);
                artifacts.push(a);
            }
        }
        let max: u64 = self
            .db
            .query_row("SELECT COALESCE(MAX(seq),0) FROM ledger", [], |r| r.get(0))?;
        let start = if cursor > max { 0 } else { cursor };
        let mut query=self.db.prepare("SELECT seq,ts,kind,object_id,payload_json FROM ledger WHERE seq>?1 AND kind IN ('state_change','human_edit','file_change','lease_transferred','human_takeover','takeover_requested','human_handoff','handoff_read_ack','verification','artifact_submitted','owner_device_revoked','allocation_decision','allocation_policy','allocation_route','dag_claim','dag_reassigned','context_share','context_invalidated','harness_capability_verified') ORDER BY seq DESC LIMIT 200")?;
        let events=query.query_map([start],|r|Ok(json!({"seq":r.get::<_,u64>(0)?,"ts":r.get::<_,i64>(1)?,"kind":r.get::<_,String>(2)?,"object_id":r.get::<_,String>(3)?,"data":serde_json::from_str::<Value>(&r.get::<_,String>(4)?).map_err(|e|rusqlite::Error::FromSqlConversionFailure(4,rusqlite::types::Type::Text,Box::new(e)))?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let mut edits_query=self.db.prepare("SELECT e.id,e.task_id,e.path,CASE WHEN e.state='Attempting' AND o.state='Unknown' THEN 'Unknown' ELSE e.state END,e.after_hash,e.before_hash FROM workspace_edit e JOIN outbox o ON o.id=e.outbox_id ORDER BY e.rowid DESC LIMIT 50")?;
        let edits=edits_query.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"task_id":r.get::<_,String>(1)?,"path":r.get::<_,String>(2)?,"state":r.get::<_,String>(3)?,"hash":r.get::<_,String>(4)?,"base_hash":r.get::<_,Option<String>>(5)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"session_id":session.id,"device_id":session.device,"session_expires_at":session.expires,"tasks":tasks,"workspaces":workspaces,"artifacts":artifacts,"bindings":self.harness_bindings()?,"cursor":max,"events":events,"edits":edits,"resync":cursor>max,"authority":"Rust Core"}),
        )
    }
    pub fn control_read(
        &self,
        actor: &Actor,
        token: &str,
        task: &str,
        path: &str,
    ) -> Result<Value> {
        owner(actor)?;
        web_session(&self.db, token)?;
        let (_, ws, root) = root(&self.db, task)?;
        let file = read_at(&root, path)?.ok_or_else(|| Error::NotFound(path.into()))?;
        let tracked: Option<Vec<u8>> = self
            .db
            .query_row(
                "SELECT content FROM workspace_file WHERE workspace_id=?1 AND path=?2",
                params![ws, path],
                |r| r.get(0),
            )
            .optional()?;
        Ok(
            json!({"path":path,"content":String::from_utf8(file.0.clone()).map_err(|_|Error::Invalid("UTF8".into()))?,"hash":digest(&file.0),"tracked_hash":tracked.map(|b|digest(&b)),"workspace":self.workspace(&ws)?,"editable_format":true}),
        )
    }
    pub fn control_takeover(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        epoch: u64,
        version: u64,
        finish: bool,
    ) -> Result<Value> {
        owner(actor)?;
        let (agent, ws, path) = root(&self.db, task)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let session = web_session(&tx, token)?;
        let old: (String, u64) =
            tx.query_row("SELECT state,version FROM task WHERE id=?1", [task], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        if matches!(
            old.0.as_str(),
            "Done" | "Failed" | "Cancelled" | "ClosedUnverified"
        ) || crate::cancellation::cancelled(&tx, task)?
            || crate::recovery::review_required(&tx, task)?
        {
            return Err(Error::Denied(
                "inactive/cancelled/restored Task cannot be taken over".into(),
            ));
        }
        let current: (u64, u64, Option<String>, Option<i64>) = tx.query_row(
            "SELECT lease_epoch,version,holder,expires_at FROM workspace WHERE id=?1",
            [&ws],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
        if current.0 != epoch || current.1 != version {
            return Err(Error::Conflict);
        }
        let control: Option<(String, String)> = tx
            .query_row(
                "SELECT session_id,phase FROM workspace_control WHERE task_id=?1",
                [task],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((holder, phase)) = &control {
            if phase == "Human" && holder != &session.id && current.3.is_some_and(|t| t > now()) {
                let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM control_session s JOIN control_device d ON d.id=s.device_id WHERE s.id=?1 AND s.expires_at>?2 AND s.revoked_at IS NULL AND d.revoked_at IS NULL)",params![holder,now()],|r|r.get(0))?;
                if valid {
                    return Err(Error::Denied(
                        "another authorized device holds the human lease".into(),
                    ));
                }
            }
            if holder == &session.id
                && phase == "Human"
                && current.3.is_some_and(|expires| expires > now())
            {
                tx.commit()?;
                return Ok(json!({"phase":"Human","workspace":self.workspace(&ws)?}));
            }
        }
        if !finish {
            tx.execute("INSERT INTO workspace_control(task_id,workspace_id,agent_id,session_id,phase,previous_state) VALUES(?1,?2,?3,?4,'TakingOver',?5) ON CONFLICT(task_id) DO UPDATE SET session_id=excluded.session_id,phase='TakingOver'",params![task,ws,agent,session.id,old.0])?;
            tx.execute("UPDATE task SET state='Blocked',block_reason='human-takeover',version=version+1 WHERE id=?1 AND version=?2",params![task,old.1])?;
            event(
                &tx,
                "takeover_requested",
                task,
                json!({"workspace_id":ws,"device_id":session.device,"session_id":session.id,"epoch":epoch,"new_agent_calls":"denied"}),
            )?;
            let pending = unsettled(&tx, task)?;
            tx.commit()?;
            return Ok(
                json!({"phase":"TakingOver","unresolved":pending,"workspace":self.workspace(&ws)?}),
            );
        }
        if control.as_ref() != Some(&(session.id.clone(), "TakingOver".to_owned())) {
            return Err(Error::Conflict);
        }
        let active:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM session WHERE agent_id=?1 AND ended_at IS NULL) OR EXISTS(SELECT 1 FROM runtime_control WHERE agent_id=?1 AND phase IN ('launching','running','cancelling')) OR EXISTS(SELECT 1 FROM sandbox_reservation WHERE agent_id=?1 AND held=1)",[&agent],|r|r.get(0))?;
        if active || unsettled(&tx, task)? {
            return Err(Error::Reconciliation);
        }
        let files = inventory(&path)?;
        for filename in files.keys() {
            let (bytes, _) = read_at(&path, filename)?.ok_or(Error::Conflict)?;
            tx.execute("INSERT INTO workspace_file VALUES(?1,?2,?3) ON CONFLICT(workspace_id,path) DO UPDATE SET content=excluded.content",params![ws,filename,bytes])?;
        }
        crate::resources::check_task_storage(&tx, task, &ws, 0)?;
        tx.execute("UPDATE workspace SET lease_epoch=lease_epoch+1,holder=?1,expires_at=?2 WHERE id=?3 AND lease_epoch=?4 AND version=?5",params![session.id,(now()+300).min(session.expires),ws,epoch,version])?;
        tx.execute("UPDATE workspace_control SET phase='Human',expected_epoch=?1,snapshot_hash=?2 WHERE task_id=?3",params![epoch+1,digest(&serde_json::to_vec(&files)?),task])?;
        event(
            &tx,
            "human_takeover",
            task,
            json!({"workspace_id":ws,"epoch":epoch+1,"version":version,"session_id":session.id,"device_id":session.device,"files":files,"kernel_writer_stopped":true}),
        )?;
        tx.commit()?;
        Ok(json!({"phase":"Human","workspace":self.workspace(&ws)?}))
    }
    pub fn control_renew(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        epoch: u64,
        version: u64,
    ) -> Result<Value> {
        owner(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (_, _, ws, _) = active_human(&tx, token, task, epoch, version)?;
        let s = web_session(&tx, token)?;
        tx.execute(
            "UPDATE workspace SET expires_at=?1 WHERE id=?2",
            params![(now() + 300).min(s.expires), ws],
        )?;
        event(
            &tx,
            "lease_renewed",
            &ws,
            json!({"session_id":s.id,"epoch":epoch}),
        )?;
        tx.commit()?;
        Ok(json!({"workspace":self.workspace(&ws)?}))
    }
    pub fn control_edit(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        request: Value,
    ) -> Result<Value> {
        owner(actor)?;
        let id = field(&request, "request_id", 100)?.to_owned();
        let filename = field(&request, "path", 200)?.to_owned();
        name(&filename)?;
        let content = request["content"]
            .as_str()
            .filter(|v| v.len() <= FILE_LIMIT)
            .ok_or_else(|| Error::Invalid("bounded UTF-8 content".into()))?
            .to_owned();
        let epoch = request["epoch"].as_u64().ok_or(Error::Conflict)?;
        let version = request["version"].as_u64().ok_or(Error::Conflict)?;
        let before = request["base_hash"].as_str().map(str::to_owned);
        let bodyhash = digest(&serde_json::to_vec(&request)?);
        let after = digest(content.as_bytes());
        let session = web_session(&self.db, token)?;
        let previous: Option<(String, String, String, String)> = self
            .db
            .query_row(
                "SELECT body_hash,task_id,session_id,state FROM workspace_edit WHERE id=?1",
                [&id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        if let Some((hash, t, s, state)) = previous {
            if hash != bodyhash || t != task || s != session.id {
                return Err(Error::Conflict);
            }
            return if state == "Committed" {
                self.db.query_row("SELECT result_json FROM outbox WHERE id=(SELECT outbox_id FROM workspace_edit WHERE id=?1)",[&id],|r|r.get::<_,String>(0)).map_err(Into::into).and_then(|s|decode(&s))
            } else {
                Err(Error::Reconciliation)
            };
        }
        let outbox = new_id("action");
        {
            let tx = self
                .db
                .transaction_with_behavior(TransactionBehavior::Immediate)?;
            let (sid, agent, ws, path) = active_human(&tx, token, task, epoch, version)?;
            let current = read_at(&path, &filename)?.map(|(b, _)| digest(&b));
            if current != before {
                return Err(Error::Conflict);
            }
            let tracked: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT content FROM workspace_file WHERE workspace_id=?1 AND path=?2",
                    params![ws, filename],
                    |r| r.get(0),
                )
                .optional()?;
            if tracked.as_ref().map(|b| digest(b)) != before {
                return Err(Error::Denied(
                    "untracked external file change requires explicit reconciliation".into(),
                ));
            }
            tx.execute("INSERT INTO outbox VALUES(?1,?2,?3,?4,?5,'Dispatching',?6,NULL)",params![outbox,task,agent,format!("workspace-edit:{id}"),encode(&json!({"target":ws,"body":{"kind":"owner-native-edit","request_id":id,"path":filename,"base_hash":before,"after_hash":after,"epoch":epoch,"version":version}}))?,now()])?;
            tx.execute("INSERT INTO workspace_edit VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,'Attempting')",params![id,outbox,task,sid,filename,bodyhash,before,after,content,epoch,version])?;
            event(
                &tx,
                "intent",
                task,
                json!({"action":outbox,"request_id":id,"kind":"native-owner-edit","session_id":sid,"epoch":epoch,"version":version}),
            )?;
            event(
                &tx,
                "dispatch",
                task,
                json!({"action":outbox,"stage":"publication-prepared","delivery_confirmed":false}),
            )?;
            tx.commit()?;
        }
        let result = (|| -> Result<Value> {
            let tx = self
                .db
                .transaction_with_behavior(TransactionBehavior::Immediate)?;
            // This one in-flight action is our own Intent, not a new unknown effect.
            let s = web_session(&tx, token)?;
            let (_, ws, path) = root(&tx, task)?;
            let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM workspace w JOIN workspace_control c ON c.workspace_id=w.id WHERE c.task_id=?1 AND c.phase='Human' AND c.session_id=?2 AND w.holder=?2 AND w.lease_epoch=?3 AND w.version=?4 AND w.expires_at>?5)",params![task,s.id,epoch,version,now()],|r|r.get(0))?;
            if !valid {
                return Err(Error::Conflict);
            }
            let other:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM outbox WHERE task_id=?1 AND id!=?2 AND state IN ('Pending','Dispatching','Unknown')) OR EXISTS(SELECT 1 FROM model_reservation WHERE task_id=?1 AND state!='Settled')",params![task,outbox],|r|r.get(0))?;
            if other {
                return Err(Error::Reconciliation);
            }
            tx.execute("INSERT INTO workspace_file VALUES(?1,?2,?3) ON CONFLICT(workspace_id,path) DO UPDATE SET content=excluded.content",params![ws,filename,content.as_bytes()])?;
            crate::resources::check_task_storage(&tx, task, &ws, 0)?;
            publish(&path, &filename, content.as_bytes(), before.as_deref())?;
            tx.execute("UPDATE workspace SET version=version+1 WHERE id=?1 AND version=?2 AND lease_epoch=?3",params![ws,version,epoch])?;
            tx.execute(
                "UPDATE artifact SET verification_state='unverified' WHERE task_id=?1",
                [task],
            )?;
            tx.execute(
                "UPDATE task SET verification_state='pending',version=version+1 WHERE id=?1",
                [task],
            )?;
            tx.execute(
                "UPDATE workspace_edit SET state='Committed' WHERE id=?1",
                [&id],
            )?;
            let receipt = json!({"request_id":id,"state":"Committed","path":filename,"hash":after,"version":version+1,"epoch":epoch});
            tx.execute(
                "UPDATE outbox SET state='Confirmed',result_json=?1 WHERE id=?2",
                params![encode(&receipt)?, outbox],
            )?;
            event(
                &tx,
                "human_edit",
                &ws,
                json!({"task_id":task,"action":outbox,"request_id":id,"path":filename,"base_hash":before,"hash":after,"epoch":epoch,"version":version+1,"device_id":s.device}),
            )?;
            event(
                &tx,
                "result",
                task,
                json!({"action":outbox,"state":"Confirmed","hash":after}),
            )?;
            tx.commit()?;
            Ok(receipt)
        })();
        if result.is_err() {
            let tx = self
                .db
                .transaction_with_behavior(TransactionBehavior::Immediate)?;
            tx.execute(
                "UPDATE workspace_edit SET state='Unknown' WHERE id=?1 AND state='Attempting'",
                [&id],
            )?;
            tx.execute(
                "UPDATE outbox SET state='Unknown' WHERE id=?1 AND state='Dispatching'",
                [&outbox],
            )?;
            tx.execute("INSERT INTO reconciliation SELECT ?1,?2,'Unknown',NULL WHERE NOT EXISTS(SELECT 1 FROM reconciliation WHERE outbox_id=?2)",params![new_id("reconcile"),outbox])?;
            event(
                &tx,
                "reconcile",
                task,
                json!({"action":outbox,"kind":"native-owner-edit","automatic_retry":false}),
            )?;
            tx.commit()?;
        }
        result
    }
    pub fn control_edit_status(&self, actor: &Actor, token: &str, id: &str) -> Result<Value> {
        owner(actor)?;
        web_session(&self.db, token)?;
        self.db.query_row("SELECT e.task_id,e.path,e.before_hash,e.after_hash,e.epoch,e.version,CASE WHEN e.state='Attempting' AND o.state='Unknown' THEN 'Unknown' ELSE e.state END,e.outbox_id FROM workspace_edit e JOIN outbox o ON o.id=e.outbox_id WHERE e.id=?1",[id],|r|Ok(json!({"request_id":id,"task_id":r.get::<_,String>(0)?,"path":r.get::<_,String>(1)?,"base_hash":r.get::<_,Option<String>>(2)?,"hash":r.get::<_,String>(3)?,"epoch":r.get::<_,u64>(4)?,"version":r.get::<_,u64>(5)?,"state":r.get::<_,String>(6)?,"action":r.get::<_,String>(7)?}))).map_err(Into::into)
    }
    pub fn control_reconcile_edit(
        &mut self,
        actor: &Actor,
        token: &str,
        id: &str,
    ) -> Result<Value> {
        owner(actor)?;
        let s = web_session(&self.db, token)?;
        let (task,path,after,content,version,action,state):(String,String,String,String,u64,String,String)=self.db.query_row("SELECT task_id,path,after_hash,content,version,outbox_id,state FROM workspace_edit WHERE id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?)))?;
        if state == "Committed" {
            return self.control_edit_status(actor, token, id);
        }
        if state != "Unknown" && state != "Attempting" {
            return Err(Error::Reconciliation);
        }
        let (_, ws, root) = root(&self.db, &task)?;
        let (actual, _) = read_at(&root, &path)?.ok_or(Error::Reconciliation)?;
        if digest(&actual) != after || digest(content.as_bytes()) != after {
            return Err(Error::Reconciliation);
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        web_session(&tx, token)?;
        if tx.execute(
            "UPDATE workspace SET version=version+1 WHERE id=?1 AND version=?2",
            params![ws, version],
        )? != 1
        {
            return Err(Error::Conflict);
        }
        tx.execute("INSERT INTO workspace_file VALUES(?1,?2,?3) ON CONFLICT(workspace_id,path) DO UPDATE SET content=excluded.content",params![ws,path,actual])?;
        tx.execute(
            "UPDATE artifact SET verification_state='unverified' WHERE task_id=?1",
            [&task],
        )?;
        tx.execute(
            "UPDATE task SET verification_state='pending',version=version+1 WHERE id=?1",
            [&task],
        )?;
        tx.execute(
            "UPDATE workspace_edit SET state='Committed' WHERE id=?1",
            [id],
        )?;
        let receipt = json!({"request_id":id,"state":"Committed","path":path,"hash":after,"version":version+1,"epoch":tx.query_row("SELECT epoch FROM workspace_edit WHERE id=?1",[id],|r|r.get::<_,u64>(0))?,"reconciled":true});
        tx.execute(
            "UPDATE outbox SET state='Confirmed',result_json=?2 WHERE id=?1",
            params![action, encode(&receipt)?],
        )?;
        tx.execute(
            "UPDATE reconciliation SET status='Resolved',evidence_ref=?1 WHERE outbox_id=?2",
            params![
                encode(&json!({"sha256":after,"actual_file_checked":true,"republished":false}))?,
                action
            ],
        )?;
        event(
            &tx,
            "human_edit",
            &ws,
            json!({"request_id":id,"action":action,"path":path,"hash":after,"version":version+1,"source":"Owner actual-file reconciliation","republished":false,"device_id":s.device}),
        )?;
        tx.commit()?;
        self.control_edit_status(actor, token, id)
    }
    pub fn control_edit_not_executed(
        &mut self,
        actor: &Actor,
        token: &str,
        id: &str,
    ) -> Result<Value> {
        owner(actor)?;
        let session = web_session(&self.db, token)?;
        let (task, path, before, action, state): (String, String, Option<String>, String, String) =
            self.db.query_row(
                "SELECT task_id,path,before_hash,outbox_id,state FROM workspace_edit WHERE id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )?;
        if state == "NotExecuted" {
            return self.control_edit_status(actor, token, id);
        }
        if !matches!(state.as_str(), "Unknown" | "Attempting") {
            return Err(Error::Reconciliation);
        }
        let (_, _, path_root) = root(&self.db, &task)?;
        if read_at(&path_root, &path)?.map(|(b, _)| digest(&b)) != before {
            return Err(Error::Reconciliation);
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        web_session(&tx, token)?;
        tx.execute(
            "UPDATE workspace_edit SET state='NotExecuted' WHERE id=?1",
            [id],
        )?;
        tx.execute(
            "UPDATE outbox SET state='NotExecuted' WHERE id=?1",
            [&action],
        )?;
        tx.execute("UPDATE reconciliation SET status='Resolved',evidence_ref=?1 WHERE outbox_id=?2",params![encode(&json!({"actual_file_matches_base":true,"base_hash":before,"republished":false,"authorized_by_device":session.device}))?,action])?;
        event(
            &tx,
            "reconcile",
            &task,
            json!({"action":action,"outcome":"NotExecuted","actual_file_matches_base":true,"automatic_retry":false,"model_refund":false}),
        )?;
        tx.commit()?;
        self.control_edit_status(actor, token, id)
    }
    pub fn control_handoff(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        request: Value,
    ) -> Result<Value> {
        owner(actor)?;
        let epoch = request["epoch"].as_u64().ok_or(Error::Conflict)?;
        let version = request["version"].as_u64().ok_or(Error::Conflict)?;
        let filename = field(&request, "read_path", 200)?;
        name(filename)?;
        let message = field(&request, "message_id", 100)?;
        let content = field(&request, "content", 100000)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (sid, agent, ws, path) = active_human(&tx, token, task, epoch, version)?;
        let target = format!(
            "tool:{task}:{ws}:read:{}",
            digest(&serde_json::to_vec(&json!({"file_path":filename}))?)
        );
        Self::check_capability(&tx, &agent, "execute", &target)?;
        let files = inventory(&path)?;
        let file = files
            .get(filename)
            .ok_or_else(|| Error::NotFound(filename.into()))?;
        let (turn, native): (String, String) = tx.query_row(
            "SELECT turn_id,native_session_ref FROM harness_binding WHERE task_id=?1",
            [task],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if crate::cancellation::cancelled(&tx, task)?
            || crate::recovery::review_required(&tx, task)?
        {
            return Err(Error::Reconciliation);
        }
        tx.execute(
            "UPDATE workspace SET lease_epoch=lease_epoch+1,holder=?1,expires_at=?2 WHERE id=?3",
            params![agent, now() + 300, ws],
        )?;
        tx.execute("UPDATE workspace_control SET phase='Returning',read_path=?1,snapshot_hash=?2,expected_epoch=?3 WHERE task_id=?4",params![filename,file["hash"].as_str(),epoch+1,task])?;
        event(
            &tx,
            "handoff_continuation_authorized",
            task,
            json!({"turn_id":turn,"message_id":message,"content_hash":digest(content.as_bytes()),"native_session_ref":native,"workspace_id":ws,"epoch":epoch+1,"version":version,"expected_file_hash":file["hash"],"read_path":filename,"owner_session_id":sid,"automatic_send":false}),
        )?;
        event(
            &tx,
            "human_handoff",
            task,
            json!({"workspace_id":ws,"epoch":epoch+1,"version":version,"read_path":filename,"read_required":true}),
        )?;
        tx.commit()?;
        Ok(
            json!({"phase":"Returning","turn_id":turn,"native_session_ref":native,"workspace":self.workspace(&ws)?}),
        )
    }
    pub fn control_handoff_ready(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
    ) -> Result<Value> {
        owner(actor)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let s = web_session(&tx, token)?;
        let (agent,path,expected):(String,String,String)=tx.query_row("SELECT agent_id,read_path,snapshot_hash FROM workspace_control WHERE task_id=?1 AND session_id=?2 AND phase='Returning'",params![task,s.id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        let (_, _, root) = root(&tx, task)?;
        if read_at(&root, &path)?.map(|(b, _)| digest(&b)).as_deref() != Some(&expected) {
            return Err(Error::Conflict);
        }
        let active:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM session s JOIN agent a ON a.id=s.agent_id WHERE a.id=?1 AND a.state='Running' AND s.ended_at IS NULL)",[agent],|r|r.get(0))?;
        if !active || unsettled(&tx, task)? {
            return Err(Error::Reconciliation);
        }
        tx.execute(
            "UPDATE workspace_control SET phase='Agent' WHERE task_id=?1",
            [task],
        )?;
        tx.execute("UPDATE task SET state='Ready',block_reason=NULL,version=version+1 WHERE id=?1 AND state='Blocked'",[task])?;
        event(
            &tx,
            "state_change",
            task,
            json!({"from":"Blocked","to":"Ready","reason":"Owner handoff; fresh explicit dispatch required"}),
        )?;
        tx.commit()?;
        Ok(json!({"ready":true}))
    }
    /// Trusted original-tool relay supplies the actual read version; Core checks
    /// the original confirmed action and its current execution/expected bytes.
    pub fn control_read_ack(
        &mut self,
        actor: &Actor,
        turn: &str,
        call: &str,
        native: &str,
        hash: &str,
    ) -> Result<Value> {
        core(actor)?;
        let task = self.bound_task(turn)?.ok_or(Error::Conflict)?.id;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let expected:Option<(String,String,String,u64)>=tx.query_row("SELECT agent_id,read_path,snapshot_hash,expected_epoch FROM workspace_control WHERE task_id=?1 AND phase='Agent' AND read_path IS NOT NULL",[&task],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
        let Some((agent, path, expected, epoch)) = expected else {
            tx.commit()?;
            return Ok(json!({"required":false}));
        };
        let (_, ws, root) = root(&tx, &task)?;
        let row:Option<String>=tx.query_row("SELECT payload_json FROM outbox WHERE idempotency_key=?1 AND task_id=?2 AND agent_id=?3 AND state='Confirmed'",params![format!("tool:{task}:{call}"),task,agent],|r|r.get(0)).optional()?;
        let body: Value = decode(&row.ok_or(Error::Reconciliation)?)?;
        let session: String = tx.query_row(
            "SELECT id FROM session WHERE agent_id=?1 AND ended_at IS NULL",
            [&agent],
            |r| r.get(0),
        )?;
        if body["body"]["tool"] != "read"
            || body["body"]["runtime_session_id"] != session
            || body["body"]["native_session_ref"] != native
            || body["body"]["arguments_hash"]
                != digest(&serde_json::to_vec(&json!({"file_path":path}))?)
            || hash != expected
            || read_at(&root, &path)?.map(|(b, _)| digest(&b)).as_deref() != Some(hash)
        {
            return Err(Error::Denied("handed-back file read proof mismatch".into()));
        }
        let valid:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM workspace WHERE id=?1 AND holder=?2 AND lease_epoch=?3 AND expires_at>?4)",params![ws,agent,epoch,now()],|r|r.get(0))?;
        if !valid {
            return Err(Error::Conflict);
        }
        tx.execute(
            "UPDATE workspace_control SET read_path=NULL WHERE task_id=?1",
            [&task],
        )?;
        event(
            &tx,
            "handoff_read_ack",
            &task,
            json!({"path":path,"hash":hash,"epoch":epoch,"runtime_session_id":session,"call_id":call}),
        )?;
        tx.commit()?;
        Ok(json!({"acknowledged":true}))
    }
}
