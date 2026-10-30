#![allow(dead_code)]
use kane_core::*;
use serde_json::{Value, json};
pub fn emit(r: &mut Registry, id: &str, mut data: Value) -> kane_core::Result<Value> {
    let s = r.runtime_status(id)?;
    data["owner_token"] = s["control"]["owner_token"].clone();
    data["attempt"] = s["control"]["attempt"].clone();
    r.runtime_event(&Actor::Core, id, r.agent(id)?.version, data)
}
pub fn launch(r: &mut Registry, id: &str, pid: u64) {
    let mut s = r.runtime_status(id).unwrap();
    if s["config"].is_null() {
        r.configure_runtime(&Actor::Core, id, json!({"cwd":"/test","max_restarts":10}))
            .unwrap();
        s = r.runtime_status(id).unwrap();
    }
    if s["control"]["owner_token"].is_null() || s["agent"]["state"] != "Ready" {
        s = r.acquire_runtime(&Actor::Core, id, true).unwrap();
    }
    let token = s["control"]["owner_token"].as_str().unwrap();
    assert_eq!(
        r.begin_runtime(&Actor::Core, id, token).unwrap()["allowed"],
        true
    );
    emit(r, id, json!({"event":"started","pid":pid})).unwrap();
}
pub fn lost(r: &mut Registry, id: &str) {
    emit(r, id, json!({"event":"process_exited","success":false})).unwrap();
}
pub fn fixture(path: &std::path::Path, max: u32) -> (Registry, Task, Agent) {
    let mut r = Registry::open(path).unwrap();
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let b = r.create_budget(&Actor::Owner, 100, 1).unwrap();
    r.register_model(&Actor::Owner, "mock", json!({})).unwrap();
    let task = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "lifecycle".into(),
                acceptance: vec!["independent".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 1,
                    max_storage_bytes: 1,
                    checkpoint_verified: false,
                },
                budget_id: b,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let a = r
        .register_agent(&Actor::Owner, "existing", &task.id, "mock")
        .unwrap();
    r.configure_runtime(
        &Actor::Core,
        &a.id,
        json!({"cwd":"/test","max_restarts":max}),
    )
    .unwrap();
    (r, task, a)
}
