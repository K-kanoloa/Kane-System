use crate::{registry::*, *};
use rusqlite::{OptionalExtension, params};
use serde_json::json;

fn safe_path(path: &str) -> Result<()> {
    if path.is_empty()
        || path.starts_with('/')
        || path.contains('\\')
        || path.contains('\0')
        || path
            .split('/')
            .any(|p| p.is_empty() || matches!(p, ".." | "."))
    {
        return Err(Error::Invalid(
            "canonical relative workspace path required".into(),
        ));
    }
    Ok(())
}

impl Registry {
    pub fn create_workspace(&mut self, actor: &Actor, kind: &str) -> Result<Workspace> {
        owner(actor)?;
        if !matches!(
            kind,
            "code" | "website" | "design" | "video" | "audio" | "data" | "document" | "research"
        ) {
            return Err(Error::Invalid("workspace kind".into()));
        }
        let id = new_id("ws");
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO workspace(id,kind) VALUES(?1,?2)",
            params![id, kind],
        )?;
        event(
            &tx,
            "workspace_created",
            &id,
            json!({"kind":kind,"storage":"database-managed experimental files"}),
        )?;
        tx.commit()?;
        self.workspace(&id)
    }

    pub fn workspace(&self, id: &str) -> Result<Workspace> {
        self.db
            .query_row(
                "SELECT kind,version,lease_epoch,holder,expires_at FROM workspace WHERE id=?1",
                [id],
                |r| {
                    Ok(Workspace {
                        id: id.into(),
                        kind: r.get(0)?,
                        version: r.get(1)?,
                        lease_epoch: r.get(2)?,
                        holder: r.get(3)?,
                        expires_at: r.get(4)?,
                    })
                },
            )
            .optional()?
            .ok_or_else(|| Error::NotFound(id.into()))
    }

    /// Serializes with controlled commits by SQLite's single writer. This covers only
    /// database-managed files, not arbitrary host files or file descriptors.
    pub fn transfer_lease(
        &mut self,
        actor: &Actor,
        id: &str,
        expected_epoch: u64,
        holder: &str,
        expires_at: i64,
    ) -> Result<Workspace> {
        owner(actor)?;
        if expires_at <= now() || holder.is_empty() {
            return Err(Error::Invalid("lease expiry/holder".into()));
        }
        let tx = self.db.transaction()?;
        if tx.execute("UPDATE workspace SET lease_epoch=lease_epoch+1,holder=?1,expires_at=?2 WHERE id=?3 AND lease_epoch=?4",params![holder,expires_at,id,expected_epoch])?!=1 {return Err(Error::Conflict);}
        event(
            &tx,
            "lease_transferred",
            id,
            json!({"epoch":expected_epoch+1,"holder":holder,"expires_at":expires_at}),
        )?;
        tx.commit()?;
        self.workspace(id)
    }

    pub fn write_file(
        &mut self,
        actor: &Actor,
        id: &str,
        epoch: u64,
        version: u64,
        path: &str,
        content: &[u8],
    ) -> Result<Workspace> {
        safe_path(path)?;
        if let Actor::Agent(agent) = actor
            && self.agent(agent)?.state != AgentState::Running
        {
            return Err(Error::Denied(
                "inactive Agent cannot commit a workspace write".into(),
            ));
        }
        let holder = match actor {
            Actor::Owner => "human",
            Actor::Agent(id) => id,
            _ => return Err(Error::Denied("writer identity".into())),
        };
        let tx = self
            .db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if let Actor::Agent(agent) = actor {
            Self::check_capability(&tx, agent, "modify", id)?;
        }
        if tx.execute("UPDATE workspace SET version=version+1 WHERE id=?1 AND lease_epoch=?2 AND version=?3 AND holder=?4 AND expires_at>?5",params![id,epoch,version,holder,now()])?!=1 {return Err(Error::Conflict);}
        tx.execute("INSERT INTO workspace_file VALUES(?1,?2,?3) ON CONFLICT(workspace_id,path) DO UPDATE SET content=excluded.content",params![id,path,content])?;
        // Check the resulting size (including replacements), in the same write
        // transaction. A failed quota check rolls back the version and content.
        let tasks = {
            let mut q = tx
                .prepare("SELECT id FROM task WHERE json_extract(spec_json,'$.workspace_id')=?1")?;
            q.query_map([id], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for task in tasks {
            crate::resources::check_task_storage(&tx, &task, id, 0)?;
        }
        event(
            &tx,
            if *actor == Actor::Owner {
                "human_edit"
            } else {
                "file_change"
            },
            id,
            json!({"path":path,"hash":digest(content),"epoch":epoch,"version":version+1}),
        )?;
        tx.commit()?;
        self.workspace(id)
    }

    pub fn read_file(&mut self, actor: &Actor, id: &str, path: &str) -> Result<Vec<u8>> {
        safe_path(path)?;
        match actor {
            Actor::Owner | Actor::Core => {}
            Actor::Agent(agent) => self.authorize(agent, "read", id)?,
            _ => return Err(Error::Denied("reader".into())),
        }
        self.db
            .query_row(
                "SELECT content FROM workspace_file WHERE workspace_id=?1 AND path=?2",
                params![id, path],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| Error::NotFound(path.into()))
    }
}
