//! Authoritative local Owner/device/session metadata. WebAuthn cryptographic
//! verification belongs to the trusted Host verifier, never an Agent process.
use crate::{registry::*, *};
use rusqlite::{Connection, OptionalExtension, Transaction, TransactionBehavior, params};
use serde_json::{Value, json};

pub(crate) struct WebSession {
    pub id: String,
    pub device: String,
    pub expires: i64,
}
pub(crate) fn field<'a>(v: &'a Value, name: &str, max: usize) -> Result<&'a str> {
    v[name]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(|| Error::Invalid(format!("bounded {name} required")))
}
fn secret() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}
pub(crate) fn web_session(db: &Connection, token: &str) -> Result<WebSession> {
    if token.len() != 64 {
        return Err(Error::Denied("Owner session required".into()));
    }
    db.query_row("SELECT s.id,s.device_id,s.expires_at FROM control_session s JOIN control_device d ON d.id=s.device_id WHERE s.token_hash=?1 AND s.revoked_at IS NULL AND d.revoked_at IS NULL AND s.expires_at>?2",params![digest(token.as_bytes()),now()],|r|Ok(WebSession{id:r.get(0)?,device:r.get(1)?,expires:r.get(2)?})).optional()?.ok_or_else(||Error::Denied("Owner session expired or revoked".into()))
}
pub(crate) fn require_session_id(db: &Connection, id: &str) -> Result<()> {
    let active:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM control_session s JOIN control_device d ON d.id=s.device_id WHERE s.id=?1 AND s.revoked_at IS NULL AND d.revoked_at IS NULL AND s.expires_at>?2)",params![id,now()],|r|r.get(0))?;
    if !active {
        return Err(Error::Denied(
            "granting Owner session revoked/expired".into(),
        ));
    }
    Ok(())
}
fn device(db: &Connection, token: &str) -> Result<String> {
    if token.len() != 64 {
        return Err(Error::Denied("paired device required".into()));
    }
    db.query_row(
        "SELECT id FROM control_device WHERE token_hash=?1 AND revoked_at IS NULL",
        [digest(token.as_bytes())],
        |r| r.get(0),
    )
    .optional()?
    .ok_or_else(|| Error::Denied("paired device required".into()))
}
fn issue_session(tx: &Transaction<'_>, device: &str) -> Result<Value> {
    let token = secret();
    let id = new_id("web-session");
    let expires = now() + 8 * 60 * 60;
    tx.execute(
        "INSERT INTO control_session VALUES(?1,?2,?3,?4,NULL)",
        params![id, digest(token.as_bytes()), device, expires],
    )?;
    event(
        tx,
        "owner_session_created",
        device,
        json!({"session_id":id,"expires_at":expires,"credential_in_ledger":false}),
    )?;
    Ok(json!({"session_token":token,"session_id":id,"device_id":device,"expires_at":expires}))
}
fn pair_code(tx: &Transaction<'_>) -> Result<Value> {
    let code = secret();
    let expires = now() + 300;
    tx.execute(
        "INSERT INTO control_pairing VALUES(?1,?2,0)",
        params![digest(code.as_bytes()), expires],
    )?;
    event(
        tx,
        "owner_pairing_created",
        "owner-policy",
        json!({"expires_at":expires,"one_time":true}),
    )?;
    Ok(json!({"pairing_code":code,"expires_at":expires}))
}
pub(crate) fn invalidate_restored_auth(tx: &Transaction<'_>) -> Result<()> {
    tx.execute(
        "UPDATE control_session SET revoked_at=?1 WHERE revoked_at IS NULL",
        [now()],
    )?;
    tx.execute(
        "UPDATE control_device SET revoked_at=?1 WHERE revoked_at IS NULL",
        [now()],
    )?;
    tx.execute(
        "UPDATE control_credential SET revoked_at=?1 WHERE revoked_at IS NULL",
        [now()],
    )?;
    tx.execute("UPDATE control_pairing SET used=1", [])?;
    tx.execute("UPDATE control_challenge SET used=1", [])?;
    event(
        tx,
        "owner_restore_access_fenced",
        "owner-policy",
        json!({"local_owner_repair_required":true,"automatic_device_reactivation":false}),
    )
}
impl Registry {
    /// Private trusted Host IPC only. No caller-provided Actor crosses the Web.
    pub fn control_auth(&mut self, actor: &Actor, op: &str, body: Value) -> Result<Value> {
        owner(actor)?;
        if op == "info" {
            let info: Option<(String, String, String)> = self
                .db
                .query_row(
                    "SELECT owner_ref,origin,rp_id FROM control_owner WHERE id=1",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            return Ok(info.map(|(id,origin,rp)|json!({"initialized":true,"owner_id":id,"origin":origin,"rp_id":rp})).unwrap_or(json!({"initialized":false})));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let result = match op {
            "init" => {
                let raw = field(&body, "origin", 512)?.trim_end_matches('/');
                let rest = raw
                    .strip_prefix("https://")
                    .ok_or_else(|| Error::Invalid("HTTPS origin required".into()))?;
                let parts = rest.split(':').collect::<Vec<_>>();
                if parts.len() > 2
                    || parts[0].is_empty()
                    || !parts[0]
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
                    || parts
                        .get(1)
                        .is_some_and(|s| s.parse::<u16>().ok().is_none_or(|p| p == 0))
                {
                    return Err(Error::Invalid(
                        "canonical DNS/localhost HTTPS origin required".into(),
                    ));
                }
                let id = new_id("owner");
                let origin = raw.to_ascii_lowercase();
                let rp = parts[0].to_ascii_lowercase();
                tx.execute(
                    "INSERT INTO control_owner VALUES(1,?1,?2,?3)",
                    params![id, origin, rp],
                )?;
                event(
                    &tx,
                    "local_owner_created",
                    &id,
                    json!({"origin":origin,"rp_id":rp,"cloud_account_required":false}),
                )?;
                let mut paired = pair_code(&tx)?;
                paired["owner_id"] = json!(id);
                paired
            }
            "pair_code" => {
                // Absence is allowed only for a private local Owner CLI caller.
                if let Some(token) = body["session_token"].as_str() {
                    web_session(&tx, token)?;
                }
                let _: String =
                    tx.query_row("SELECT owner_ref FROM control_owner WHERE id=1", [], |r| {
                        r.get(0)
                    })?;
                pair_code(&tx)?
            }
            "pair" => {
                let code = field(&body, "pairing_code", 64)?;
                let label = field(&body, "label", 100)?;
                if code.len()!=64 || tx.execute("UPDATE control_pairing SET used=1 WHERE code_hash=?1 AND used=0 AND expires_at>?2",params![digest(code.as_bytes()),now()])?!=1 { return Err(Error::Denied("pairing code used/invalid/expired".into())); }
                let count: u64 = tx.query_row(
                    "SELECT count(*) FROM control_device WHERE revoked_at IS NULL",
                    [],
                    |r| r.get(0),
                )?;
                if count >= 64 {
                    return Err(Error::Budget);
                }
                let id = new_id("device");
                let key = secret();
                tx.execute(
                    "INSERT INTO control_device VALUES(?1,?2,?3,?4,NULL)",
                    params![id, digest(key.as_bytes()), label, now()],
                )?;
                event(
                    &tx,
                    "owner_device_paired",
                    &id,
                    json!({"label":label,"explicit_pairing":true}),
                )?;
                let mut result = issue_session(&tx, &id)?;
                result["device_token"] = json!(key);
                result
            }
            "begin_login" => {
                let device = device(&tx, field(&body, "device_token", 64)?)?;
                let challenge = secret();
                let id = new_id("auth-challenge");
                tx.execute(
                    "INSERT INTO control_challenge VALUES(?1,'login',?2,?3,NULL,?4,0)",
                    params![id, challenge, device, now() + 120],
                )?;
                let mut q=tx.prepare("SELECT id,key_json,sign_count FROM control_credential WHERE revoked_at IS NULL")?;
                let credentials=q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"key":serde_json::from_str::<Value>(&r.get::<_,String>(1)?).map_err(|e|rusqlite::Error::FromSqlConversionFailure(1,rusqlite::types::Type::Text,Box::new(e)))?,"sign_count":r.get::<_,u64>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
                if credentials.is_empty() {
                    return Err(Error::Denied(
                        "no active Passkey; use local/authorized-device pairing".into(),
                    ));
                }
                json!({"id":id,"challenge":challenge,"credentials":credentials})
            }
            "finish_login" => {
                let device = device(&tx, field(&body, "device_token", 64)?)?;
                let id = field(&body, "challenge_id", 100)?;
                let credential = field(&body, "credential_id", 2048)?;
                let old = body["old_count"].as_u64().ok_or(Error::Conflict)?;
                let new = body["new_count"]
                    .as_u64()
                    .filter(|n| *n <= u32::MAX as u64)
                    .ok_or(Error::Conflict)?;
                if new < old || (old > 0 && new == old) {
                    return Err(Error::Denied("Passkey counter replay".into()));
                }
                if tx.execute("UPDATE control_challenge SET used=1 WHERE id=?1 AND purpose='login' AND device_id=?2 AND challenge=?3 AND used=0 AND expires_at>?4",params![id,device,field(&body,"challenge",64)?,now()])?!=1 {return Err(Error::Denied("login challenge expired/replayed".into()));}
                if tx.execute("UPDATE control_credential SET sign_count=?1 WHERE id=?2 AND sign_count=?3 AND revoked_at IS NULL",params![new,credential,old])?!=1 {return Err(Error::Conflict);}
                event(
                    &tx,
                    "passkey_assertion_verified",
                    &device,
                    json!({"credential_id":credential,"counter":new,"verifier":"trusted-host WebAuthn SDK; UV/Origin/RP/challenge checked"}),
                )?;
                issue_session(&tx, &device)?
            }
            _ => {
                let session = web_session(&tx, field(&body, "session_token", 64)?)?;
                match op {
                    "session" => {
                        json!({"session_id":session.id,"device_id":session.device,"expires_at":session.expires})
                    }
                    "logout" => {
                        tx.execute(
                            "UPDATE control_session SET revoked_at=?1 WHERE id=?2",
                            params![now(), session.id],
                        )?;
                        event(
                            &tx,
                            "owner_session_revoked",
                            &session.device,
                            json!({"session_id":session.id}),
                        )?;
                        json!({"logged_out":true})
                    }
                    "devices" => {
                        let mut q=tx.prepare("SELECT id,label,created_at,revoked_at FROM control_device ORDER BY created_at,id")?;
                        json!(q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"label":r.get::<_,String>(1)?,"created_at":r.get::<_,i64>(2)?,"revoked_at":r.get::<_,Option<i64>>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?)
                    }
                    "revoke_device" => {
                        let id = field(&body, "device_id", 100)?;
                        if tx.execute("UPDATE control_device SET revoked_at=?1 WHERE id=?2 AND revoked_at IS NULL",params![now(),id])?!=1{return Err(Error::Conflict);}
                        tx.execute(
                            "UPDATE control_session SET revoked_at=?1 WHERE device_id=?2",
                            params![now(), id],
                        )?;
                        tx.execute("UPDATE control_pairing SET used=1", [])?;
                        event(
                            &tx,
                            "owner_device_revoked",
                            id,
                            json!({"by_device":session.device,"sessions_invalidated":true}),
                        )?;
                        json!({"revoked":true})
                    }
                    "begin_register" => {
                        let id = new_id("auth-challenge");
                        let challenge = secret();
                        tx.execute(
                            "INSERT INTO control_challenge VALUES(?1,'register',?2,?3,?4,?5,0)",
                            params![id, challenge, session.device, session.id, now() + 120],
                        )?;
                        json!({"id":id,"challenge":challenge})
                    }
                    "finish_register" => {
                        let id = field(&body, "challenge_id", 100)?;
                        let credential = field(&body, "credential_id", 2048)?;
                        if !body["key"].is_object()
                            || body["key"].to_string().len() > 16000
                            || body["key"]["public_key"].as_str().is_none_or(str::is_empty)
                        {
                            return Err(Error::Invalid("verified COSE credential required".into()));
                        }
                        let count = body["sign_count"]
                            .as_u64()
                            .filter(|n| *n <= u32::MAX as u64)
                            .ok_or(Error::Conflict)?;
                        if tx.execute("UPDATE control_challenge SET used=1 WHERE id=?1 AND purpose='register' AND session_id=?2 AND challenge=?3 AND expires_at>?4 AND used=0",params![id,session.id,field(&body,"challenge",64)?,now()])?!=1{return Err(Error::Denied("registration challenge replay/expiry".into()));}
                        tx.execute(
                            "INSERT INTO control_credential VALUES(?1,?2,?3,NULL)",
                            params![credential, encode(&body["key"])?, count],
                        )?;
                        event(
                            &tx,
                            "passkey_registered",
                            &session.device,
                            json!({"credential_id":credential,"verifier":"trusted-host WebAuthn SDK"}),
                        )?;
                        json!({"registered":true})
                    }
                    "credentials" => {
                        let mut q = tx.prepare(
                            "SELECT id,sign_count,revoked_at FROM control_credential ORDER BY id",
                        )?;
                        json!(q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"sign_count":r.get::<_,u64>(1)?,"revoked_at":r.get::<_,Option<i64>>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?)
                    }
                    "revoke_credential" => {
                        let id = field(&body, "credential_id", 2048)?;
                        tx.execute("UPDATE control_credential SET revoked_at=?1 WHERE id=?2 AND revoked_at IS NULL",params![now(),id])?;
                        event(
                            &tx,
                            "passkey_revoked",
                            &session.device,
                            json!({"credential_id":id}),
                        )?;
                        json!({"revoked":true})
                    }
                    _ => {
                        return Err(Error::Denied(
                            "unknown Owner authentication operation".into(),
                        ));
                    }
                }
            }
        };
        tx.commit()?;
        Ok(result)
    }
    pub fn control_dispatch_authorize(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        content: &str,
    ) -> Result<Value> {
        owner(actor)?;
        if content.is_empty() || content.len() > 100000 {
            return Err(Error::Invalid("bounded explicit prompt".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let s = web_session(&tx, token)?;
        let state: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [task], |r| r.get(0))?;
        if !matches!(state.as_str(), "Pending" | "Ready")
            || crate::cancellation::cancelled(&tx, task)?
            || crate::recovery::review_required(&tx, task)?
            || crate::control_workspace::human_fenced(&tx, task)?
        {
            return Err(Error::Conflict);
        }
        event(
            &tx,
            "control_dispatch_authorized",
            task,
            json!({"message_id":format!("system-prompt-{task}"),"content_hash":digest(content.as_bytes()),"owner_session_id":s.id,"device_id":s.device}),
        )?;
        tx.commit()?;
        Ok(json!({"authorized":true}))
    }
    pub fn control_approval_authorize(
        &mut self,
        actor: &Actor,
        token: &str,
        task: &str,
        request: &Value,
    ) -> Result<Value> {
        owner(actor)?;
        let id = field(request, "request_id", 100)?;
        let decision = field(request, "decision", 30)?;
        if !matches!(decision, "allow-once" | "reject-once" | "cancelled") {
            return Err(Error::Invalid("approval decision".into()));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let s = web_session(&tx, token)?;
        let state: String =
            tx.query_row("SELECT state FROM task WHERE id=?1", [task], |r| r.get(0))?;
        if !matches!(state.as_str(), "Running" | "Blocked")
            || crate::cancellation::cancelled(&tx, task)?
            || crate::control_workspace::human_fenced(&tx, task)?
        {
            return Err(Error::Conflict);
        }
        event(
            &tx,
            "owner_approval",
            task,
            json!({"request_id":id,"decision":decision,"owner_session_id":s.id,"device_id":s.device}),
        )?;
        tx.commit()?;
        Ok(json!({"authorized":true}))
    }
    pub fn control_challenge(&self, actor: &Actor, id: &str) -> Result<Value> {
        owner(actor)?;
        self.db.query_row("SELECT purpose,challenge,device_id,session_id,expires_at,used FROM control_challenge WHERE id=?1",[id],|r|Ok(json!({"purpose":r.get::<_,String>(0)?,"challenge":r.get::<_,String>(1)?,"device_id":r.get::<_,String>(2)?,"session_id":r.get::<_,Option<String>>(3)?,"expires_at":r.get::<_,i64>(4)?,"used":r.get::<_,bool>(5)?}))).map_err(Into::into)
    }
    pub fn control_credentials(&self, actor: &Actor) -> Result<Value> {
        owner(actor)?;
        let mut q = self.db.prepare(
            "SELECT id,key_json,sign_count FROM control_credential WHERE revoked_at IS NULL",
        )?;
        Ok(json!(q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"key":serde_json::from_str::<Value>(&r.get::<_,String>(1)?).map_err(|e|rusqlite::Error::FromSqlConversionFailure(1,rusqlite::types::Type::Text,Box::new(e)))?,"sign_count":r.get::<_,u64>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?))
    }
}
