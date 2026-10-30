use crate::{registry::*, *};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

impl Registry {
    pub fn grant(
        &mut self,
        actor: &Actor,
        subject: &str,
        action: &str,
        target: &str,
        expires_at: i64,
    ) -> Result<String> {
        owner(actor)?;
        if expires_at <= now()
            || subject.is_empty()
            || target.is_empty()
            || !matches!(action, "read" | "modify" | "execute" | "publish")
        {
            return Err(Error::Invalid("invalid capability".into()));
        }
        let id = new_id("cap");
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO capability VALUES(?1,?2,?3,?4,?5,NULL,'owner')",
            params![id, subject, action, target, expires_at],
        )?;
        event(
            &tx,
            "capability_granted",
            &id,
            json!({"subject":subject,"action":action,"target":target,"expires_at":expires_at}),
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn revoke(&mut self, actor: &Actor, id: &str) -> Result<()> {
        owner(actor)?;
        let tx = self.db.transaction()?;
        if tx.execute(
            "UPDATE capability SET revoked_at=?1 WHERE id=?2 AND revoked_at IS NULL",
            params![now(), id],
        )? != 1
        {
            return Err(Error::NotFound(id.into()));
        }
        event(&tx, "capability_revoked", id, json!({}))?;
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn check_capability(
        db: &rusqlite::Connection,
        subject: &str,
        action: &str,
        target: &str,
    ) -> Result<()> {
        let count:i64=db.query_row("SELECT count(*) FROM capability WHERE subject=?1 AND action=?2 AND target=?3 AND revoked_at IS NULL AND expires_at>?4",params![subject,action,target,now()],|r|r.get(0))?;
        if count == 0 {
            return Err(Error::Denied(format!("{action} on {target}")));
        }
        Ok(())
    }

    pub fn authorize(&mut self, subject: &str, action: &str, target: &str) -> Result<()> {
        let decision = Self::check_capability(&self.db, subject, action, target);
        if decision.is_err() {
            let tx = self.db.transaction()?;
            event(
                &tx,
                "access_denied",
                target,
                json!({"subject":subject,"action":action}),
            )?;
            tx.commit()?;
        }
        decision
    }

    pub fn authorize_cloud(&mut self, actor: &Actor, task_id: &str) -> Result<()> {
        core(actor)?;
        let t = self.task(task_id)?;
        if !t.spec.cloud_allowed {
            let tx = self.db.transaction()?;
            event(
                &tx,
                "cloud_denied",
                task_id,
                json!({"reason":"cloud_allowed=false"}),
            )?;
            tx.commit()?;
            return Err(Error::Denied("cloud_allowed=false".into()));
        }
        Ok(())
    }

    /// Legacy Task-local creation. Cross-Task access requires explicit collaboration sharing.
    pub fn create_context(
        &mut self,
        actor: &Actor,
        task_id: &str,
        sensitivity: &str,
        trust: &str,
        content: Value,
        expires_at: Option<i64>,
    ) -> Result<String> {
        core(actor)?;
        self.task(task_id)?;
        if !matches!(
            sensitivity,
            "public" | "internal" | "confidential" | "restricted"
        ) || !matches!(trust, "verified" | "unverified" | "untrusted")
        {
            return Err(Error::Invalid("context labels".into()));
        }
        let id = new_id("ctx");
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO context(id,task_id,sensitivity,trust,expires_at,spec_json) VALUES(?1,?2,?3,?4,?5,?6)",params![id,task_id,sensitivity,trust,expires_at,encode(&content)?])?;
        event(
            &tx,
            "context_created",
            &id,
            json!({"task":task_id,"sensitivity":sensitivity,"trust":trust}),
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn read_context(
        &mut self,
        agent_id: &str,
        id: &str,
        max_sensitivity: &str,
        cloud: bool,
    ) -> Result<Value> {
        let shared: bool = self.db.query_row(
            "SELECT EXISTS(SELECT 1 FROM context_share WHERE context_id=?1)",
            [id],
            |r| r.get(0),
        )?;
        if shared {
            return self.collaboration_context_read(&Actor::Core, agent_id, id);
        }
        let result = (|| {
            let a = self.agent(agent_id)?;
            let ctx=self.db.query_row("SELECT task_id,sensitivity,trust,expires_at,spec_json FROM context WHERE id=?1",[id],|r|
                Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<i64>>(3)?,r.get::<_,String>(4)?))).optional()?.ok_or_else(||Error::NotFound(id.into()))?;
            let level = |s: &str| match s {
                "public" => Some(0),
                "internal" => Some(1),
                "confidential" => Some(2),
                "restricted" => Some(3),
                _ => None,
            };
            let max = level(max_sensitivity)
                .ok_or_else(|| Error::Invalid("sensitivity ceiling".into()))?;
            if ctx.0 != a.task_id
                || ctx.3.is_some_and(|e| e <= now())
                || level(&ctx.1).is_none_or(|l| l > max)
            {
                return Err(Error::Denied("context scope, expiry or sensitivity".into()));
            }
            Self::check_capability(&self.db, agent_id, "read", id)?;
            if cloud && !self.task(&a.task_id)?.spec.cloud_allowed {
                return Err(Error::Denied("model data policy".into()));
            }
            Ok(
                json!({"id":id,"sensitivity":ctx.1,"trust":ctx.2,"content":decode::<Value>(&ctx.4)?}),
            )
        })();
        let tx = self.db.transaction()?;
        event(
            &tx,
            if result.is_ok() {
                "context_read"
            } else {
                "access_denied"
            },
            id,
            json!({"agent":agent_id,"cloud":cloud}),
        )?;
        tx.commit()?;
        result
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AllocationDecision {
    pub mode: String,
    pub reason: String,
    pub predicted_delta_u: Option<f64>,
}

/// No measured contribution dataset exists yet. Do not invent parallelism benefits.
pub fn allocate_direct() -> AllocationDecision {
    AllocationDecision {
        mode: "Direct".into(),
        reason: "No validated marginal benefit evidence; minimum sufficient configuration".into(),
        predicted_delta_u: None,
    }
}
