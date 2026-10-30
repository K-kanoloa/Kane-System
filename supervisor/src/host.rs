//! Trusted host operations shared by local CLI and private system service.
//! No caller-supplied Actor is accepted. Only trusted host Owner clients may use IPC.
use crate::AnyResult;
use kane_core::*;
use serde_json::{Value, json};
use std::error::Error as StdError;

pub fn request(r: &mut Registry, request: Value) -> AnyResult<Value> {
    if !request.is_object()
        || ["actor", "subject", "identity"]
            .iter()
            .any(|k| request.get(k).is_some())
    {
        return Err("caller identity cannot be supplied in request".into());
    }
    let operation = request["op"].as_str().ok_or("op required")?;
    let id = request["id"].as_str().unwrap_or("");
    let result = match operation {
        "external_prepare" => r.external_prepare(
            &Actor::Owner,
            request["turn_id"].as_str().ok_or("turn")?,
            request["native_session_ref"].as_str().ok_or("native")?,
            request["key"].as_str().ok_or("key")?,
            request["protocol"].as_str().ok_or("protocol")?,
            request["server_sha256"].as_str().ok_or("server")?,
            request["tool"].as_str().ok_or("tool")?,
            request["arguments"].clone(),
        )?,
        "external_claim" => serde_json::to_value(r.claim_action(&Actor::Owner, id)?)?,
        "external_result" => {
            r.confirm_action(
                &Actor::Owner,
                id,
                request["evidence"].as_str().ok_or("evidence")?,
            )?;
            json!({"confirmed":true})
        }
        "external_unknown" => r.external_unknown(&Actor::Owner, id)?,
        "collaboration_harness_profile" => r.collaboration_harness_profile(
            &Actor::Owner,
            id,
            serde_json::from_value(request["capabilities"].clone())?,
            request["evidence_hash"].as_str().ok_or("evidence")?,
            request["expires_at"].as_i64().ok_or("expiry")?,
        )?,
        "collaboration_create" => r.collaboration_create(
            &Actor::Owner,
            serde_json::from_value(request["plan"].clone())?,
        )?,
        "collaboration_status" => r.collaboration_status(&Actor::Owner, id)?,
        "collaboration_claim" => r.collaboration_claim(&Actor::Owner, id)?,
        "collaboration_policy" => r.collaboration_policy(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version")?,
            u32::try_from(request["max_agents"].as_u64().ok_or("max_agents")?)?,
            request["paused"].as_bool().ok_or("paused")?,
            request["reason"].as_str().ok_or("reason")?,
        )?,
        "collaboration_reassign" => r.collaboration_reassign(
            &Actor::Owner,
            id,
            request["agent_id"].as_str().ok_or("agent_id")?,
            request["generation"].as_u64().ok_or("generation")?,
        )?,
        "allocation_route" => r.allocation_route(
            &Actor::Owner,
            id,
            serde_json::from_value(request["requires"].clone())?,
        )?,
        "allocation_decide" => r.allocation_decide(
            &Actor::Owner,
            request["scenario"].as_str().ok_or("scenario")?,
            request["environment"].as_str().ok_or("environment")?,
            u32::try_from(request["max_agents"].as_u64().ok_or("max_agents")?)?,
        )?,
        "allocation_sample" => r.allocation_sample(
            &Actor::Owner,
            serde_json::from_value(request["sample"].clone())?,
        )?,
        "collaboration_context" => {
            json!({"id":r.collaboration_context(&Actor::Owner,request["artifact_id"].as_str().ok_or("artifact")?,request["expires_at"].as_i64().ok_or("expiry")?,request["sensitivity"].as_str().ok_or("sensitivity")?)?})
        }
        "collaboration_share" => r.collaboration_share(
            &Actor::Owner,
            id,
            request["agent_id"].as_str().ok_or("agent")?,
            request["expires_at"].as_i64().ok_or("expiry")?,
            request["ceiling"].as_str().ok_or("ceiling")?,
        )?,
        "collaboration_context_read" => r.collaboration_context_read(
            &Actor::Owner,
            request["agent_id"].as_str().ok_or("agent")?,
            id,
        )?,
        "collaboration_context_invalidate" => r.collaboration_context_invalidate(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version")?,
            request["reason"].as_str().ok_or("reason")?,
        )?,
        "control_dispatch_authorize" => r.control_dispatch_authorize(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["content"].as_str().ok_or("content")?,
        )?,
        "control_approval_authorize" => r.control_approval_authorize(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            &request["body"],
        )?,
        "control_verify" => serde_json::to_value(r.control_verify(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["version"].as_u64().ok_or("version")?,
            request["passed"].as_bool().ok_or("passed")?,
            request["evidence"].as_str().ok_or("evidence")?,
        )?)?,

        "control_view" => r.control_view(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            request["cursor"].as_u64().unwrap_or(0),
        )?,
        "control_read" => r.control_read(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["path"].as_str().ok_or("path")?,
        )?,
        "control_takeover" => r.control_takeover(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["epoch"].as_u64().ok_or("epoch")?,
            request["version"].as_u64().ok_or("version")?,
            request["finish"].as_bool().unwrap_or(false),
        )?,
        "control_renew" => r.control_renew(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["epoch"].as_u64().ok_or("epoch")?,
            request["version"].as_u64().ok_or("version")?,
        )?,
        "control_edit" => r.control_edit(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["body"].clone(),
        )?,
        "control_edit_status" => {
            r.control_edit_status(&Actor::Owner, request["token"].as_str().ok_or("token")?, id)?
        }
        "control_edit_not_executed" => r.control_edit_not_executed(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
        )?,
        "control_reconcile_edit" => {
            r.control_reconcile_edit(&Actor::Owner, request["token"].as_str().ok_or("token")?, id)?
        }
        "control_handoff" => r.control_handoff(
            &Actor::Owner,
            request["token"].as_str().ok_or("token")?,
            id,
            request["body"].clone(),
        )?,
        "control_handoff_ready" => {
            r.control_handoff_ready(&Actor::Owner, request["token"].as_str().ok_or("token")?, id)?
        }
        "control_read_ack" => r.control_read_ack(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn")?,
            request["call_id"].as_str().ok_or("call")?,
            request["native_session_ref"].as_str().ok_or("native")?,
            request["hash"].as_str().ok_or("hash")?,
        )?,

        "control_auth" => r.control_auth(
            &Actor::Owner,
            request["action"].as_str().ok_or("operation")?,
            request["body"].clone(),
        )?,
        "control_challenge" => r.control_challenge(&Actor::Owner, id)?,
        "control_credentials" => r.control_credentials(&Actor::Owner)?,
        "configure_model_gateway" => r.configure_model_gateway(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version")?,
            request["policy"].clone(),
        )?,
        "record_model_limits" => {
            r.record_model_limits(
                &Actor::Core,
                id,
                request["generation"].as_str().ok_or("generation")?,
                request["evidence"].clone(),
            )?;
            json!({"recorded":true})
        }
        "register_model_listener" => {
            r.register_model_listener(
                &Actor::Core,
                request["path"].as_str().ok_or("path")?,
                request["pid"]
                    .as_u64()
                    .and_then(|v| u32::try_from(v).ok())
                    .ok_or("pid")?,
                request["device"].as_u64().ok_or("device")?,
                request["inode"].as_u64().ok_or("inode")?,
            )?;
            json!({"registered":true})
        }
        "model_listeners" => r.model_listeners(&Actor::Owner)?,
        "models" => r.models(&Actor::Owner)?,
        "model_instances" => r.model_instances(&Actor::Owner)?,
        "register_model_instance" => {
            json!({"id":r.register_model_instance(&Actor::Owner,request["model_id"].as_str().ok_or("model required")?,"local")?})
        }
        "load_local_model" => r.load_local_model(
            &Actor::Owner,
            id,
            serde_json::from_value(request["spec"].clone())?,
        )?,
        "model_lifecycle" => {
            r.model_lifecycle(
                &Actor::Core,
                id,
                request["generation"]
                    .as_str()
                    .ok_or("generation required")?,
                request["state"].as_str().ok_or("state required")?,
                request["pid"].as_u64().map(|n| n as u32),
                request["evidence"].as_str().ok_or("evidence required")?,
            )?;
            json!({"recorded":true})
        }
        "set_model_priority" => {
            r.set_model_priority(
                &Actor::Owner,
                id,
                request["priority"]
                    .as_u64()
                    .and_then(|n| u32::try_from(n).ok())
                    .ok_or("priority required")?,
            )?;
            json!({"updated":true})
        }
        "claim_gateway" => r.claim_gateway(
            &Actor::Core,
            request["token"].as_str().ok_or("token required")?,
            &request["body"],
        )?,
        "gateway_result" => r.gateway_result(
            &Actor::Core,
            request["token"].as_str().ok_or("token required")?,
            request["receipt"].clone(),
        )?,
        "gateway_status" => r.gateway_status(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["call_id"].as_str().ok_or("call required")?,
        )?,
        "host_resources" => r.host_resources(&Actor::Owner)?,
        "configure_host_resources" => r.configure_host_resources(
            &Actor::Owner,
            request["version"].as_u64().ok_or("version required")?,
            serde_json::from_value(request["policy"].clone())?,
        )?,
        "reserve_host_resources" => r.reserve_host_resources(&Actor::Core, id)?,
        "release_host_resources" => {
            r.release_host_resources(
                &Actor::Core,
                id,
                request["token"].as_str().ok_or("token required")?,
            )?;
            json!({"released":true})
        }
        "tool_prepare" | "tool_claim" | "tool_result" | "tool_file" => r.tool_request(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["native_session_ref"]
                .as_str()
                .ok_or("session required")?,
            request["call_id"].as_str().ok_or("call required")?,
            request.clone(),
        )?,
        "grant_tool" => {
            let target = r.tool_target(
                &Actor::Owner,
                id,
                request["tool"].as_str().ok_or("tool required")?,
                &request["arguments"],
            )?;
            json!({"id":r.grant(&Actor::Owner,id,"execute",&target,request["expires_at"].as_i64().ok_or("expiry required")?)?,"target":target})
        }
        "revoke_capability" => {
            r.revoke(&Actor::Owner, id)?;
            json!({"revoked":true})
        }
        "action" => serde_json::to_value(r.action(id)?)?,
        "reconcile_action" => {
            r.reconcile_action(
                &Actor::Owner,
                id,
                request["executed"].as_bool().ok_or("executed required")?,
                request["evidence"].as_str().ok_or("evidence required")?,
            )?;
            json!({"reconciled":true})
        }
        "cancel_task" => serde_json::to_value(r.cancel_task(
            &Actor::Core,
            id,
            request["reason"].as_str().ok_or("reason required")?,
        )?)?,
        #[cfg(target_os = "linux")]
        "reconcile_native_file" => r.reconcile_native_file(&Actor::Owner, id)?,
        "recovery_queue" => r.recovery_queue(&Actor::Owner)?,
        "reconcile_model_call" => r.reconcile_model_call(
            &Actor::Owner,
            request["call_id"].as_str().ok_or("call required")?,
            request["result"].clone(),
            request["evidence"].clone(),
        )?,
        "review_restored_task" => serde_json::to_value(r.review_restored_task(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version required")?,
            request["evidence"].clone(),
        )?)?,
        "model_budget" => r.model_budget(&Actor::Owner, id)?,
        "model_queue" => r.model_queue(&Actor::Owner)?,
        "claim_model" => r.claim_model(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["call_id"].as_str().ok_or("call required")?,
            request["native_session_ref"]
                .as_str()
                .ok_or("session required")?,
        )?,
        "cancel_model" => r.cancel_model(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["call_id"].as_str().ok_or("call required")?,
            request["native_session_ref"]
                .as_str()
                .ok_or("session required")?,
        )?,
        "enable_model_budget" => r.enable_model_budget(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
        )?,
        "reserve_model" => r.reserve_model(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["call_id"].as_str().ok_or("call required")?,
            request["native_session_ref"]
                .as_str()
                .ok_or("session required")?,
            request["provider"].as_str().ok_or("provider required")?,
            request["model"].as_str().ok_or("model required")?,
        )?,
        "settle_model" => r.settle_model(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["call_id"].as_str().ok_or("call required")?,
            request["receipt"].clone(),
        )?,
        "dispatch_receipt" => r.dispatch_receipt(
            &Actor::Core,
            request["turn_id"].as_str().ok_or("turn required")?,
            request["message_id"].as_str().ok_or("message required")?,
        )?,
        "dispatch_harness" => {
            let mut task = serde_json::to_value(
                r.dispatch_harness(
                    &Actor::Owner,
                    id,
                    request["turn_id"].as_str().ok_or("turn required")?,
                    request["message_id"].as_str().ok_or("message required")?,
                    request["content"].as_str().ok_or("content required")?,
                    request["native_session_ref"]
                        .as_str()
                        .ok_or("native session required")?,
                )?,
            )?;
            task["runtime_session_id"] = json!(r.current_runtime_session(id)?);
            task
        }
        "harness_fact" => {
            r.harness_fact(
                &Actor::Core,
                request["turn_id"].as_str().ok_or("turn required")?,
                request["receipt"].as_str().ok_or("receipt required")?,
                request["kind"].as_str().ok_or("kind required")?,
                request["payload"].clone(),
            )?;
            json!({"recorded":true})
        }
        "runtime_acquire" => {
            r.acquire_runtime(&Actor::Core, id, request["absence_confirmed"] == true)?
        }
        "runtime_begin" => r.begin_runtime(
            &Actor::Core,
            id,
            request["owner_token"]
                .as_str()
                .ok_or("owner token required")?,
        )?,
        "runtime_status" => r.runtime_status(id)?,
        "runtime_configure" => r.configure_runtime(&Actor::Core, id, request["config"].clone())?,
        "runtime_event" => r.runtime_event(
            &Actor::Core,
            id,
            request["version"].as_u64().ok_or("version required")?,
            request["data"].clone(),
        )?,
        "register_model" => {
            r.register_model(&Actor::Owner, id, request["spec"].clone())?;
            json!({"id":id})
        }
        "create_task" => serde_json::to_value(r.create_task(
            &Actor::Owner,
            serde_json::from_value(request["spec"].clone())?,
        )?)?,
        "register_agent" => serde_json::to_value(r.register_agent(
            &Actor::Owner,
            request["role"].as_str().ok_or("role required")?,
            request["task_id"].as_str().ok_or("task required")?,
            request["model_id"].as_str().ok_or("model required")?,
        )?)?,
        "tasks" => serde_json::to_value(r.tasks()?)?,
        "bindings" => serde_json::to_value(r.harness_bindings()?)?,
        "task" => serde_json::to_value(r.task(id)?)?,
        "artifacts" => serde_json::to_value(r.artifacts(id)?)?,
        "workspace" => serde_json::to_value(r.workspace(id)?)?,
        "transfer_lease" => serde_json::to_value(r.transfer_lease(
            &Actor::Owner,
            id,
            request["epoch"].as_u64().ok_or("epoch required")?,
            request["holder"].as_str().ok_or("holder required")?,
            request["expires_at"].as_i64().ok_or("expiry required")?,
        )?)?,
        "grant_workspace_modify" => json!({"id":r.grant(
            &Actor::Owner,id,"modify",&r.agent(id)?.workspace_id,
            request["expires_at"].as_i64().ok_or("expiry required")?,
        )?}),
        "events" => serde_json::to_value(r.events()?)?,
        "create_workspace" => serde_json::to_value(r.create_workspace(
            &Actor::Owner,
            request["kind"].as_str().ok_or("kind required")?,
        )?)?,
        "create_budget" => {
            json!({"id":r.create_budget(&Actor::Owner,request["tokens"].as_u64().ok_or("tokens required")?,request["max_concurrency"].as_u64().map(u32::try_from).transpose()?.unwrap_or(1))?})
        }
        "bind" => serde_json::to_value(
            r.bind_harness(
                &Actor::Owner,
                request["turn_id"].as_str().ok_or("turn_id required")?,
                request["harness_agent_ref"]
                    .as_str()
                    .ok_or("harness_agent_ref required")?,
                serde_json::from_value(request["spec"].clone())?,
            )?,
        )?,
        "bound_task" => serde_json::to_value(
            r.bound_task(request["turn_id"].as_str().ok_or("turn_id required")?)?,
        )?,
        "sync" => {
            r.check_recovery_observation(
                request["turn_id"].as_str().ok_or("turn required")?,
                request["runtime_session_id"].as_str(),
            )?;
            let message = request["message"]
                .as_object()
                .map(|m| {
                    Ok::<_, Box<dyn StdError>>((
                        m.get("message_id")
                            .and_then(Value::as_str)
                            .ok_or("message id required")?,
                        m.get("content")
                            .and_then(Value::as_str)
                            .ok_or("message content required")?,
                    ))
                })
                .transpose()?;
            serde_json::to_value(r.ingest_harness(
                &Actor::Core,
                request["turn_id"].as_str().ok_or("turn_id required")?,
                request["status"].as_str().ok_or("status required")?,
                request["native_session_ref"].as_str(),
                message,
            )?)?
        }
        "verify" => serde_json::to_value(r.verify(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version required")?,
            request["passed"].as_bool().ok_or("passed required")?,
            request["evidence"].as_str().ok_or("evidence required")?,
        )?)?,
        "transition" => serde_json::to_value(r.transition_task(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version required")?,
            serde_json::from_value(request["state"].clone())?,
            request["reason"].as_str(),
        )?)?,
        "close_unverified" => serde_json::to_value(r.close_unverified(
            &Actor::Owner,
            id,
            request["version"].as_u64().ok_or("version required")?,
            request["reason"].as_str().ok_or("reason required")?,
        )?)?,
        "health" => {
            json!({"protocol":1,"version":env!("CARGO_PKG_VERSION"),"schema":9,"delivery":"development system profile","complete":false})
        }
        "backup" => {
            r.backup(
                &Actor::Owner,
                request["destination"]
                    .as_str()
                    .ok_or("destination required")?,
            )?;
            json!({"created":true,"scope":"database-managed state only"})
        }
        _ => return Err("unknown operation".into()),
    };
    Ok(result)
}
