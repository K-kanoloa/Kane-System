//! Minimum Phase 0 records, not full ModelInstance/Context lifecycle acceptance.
use kane_core::*;
use rusqlite::{Connection, types::Value};
use serde_json::json;

const TABLES: [&str; 10] = [
    "model",
    "model_instance",
    "agent",
    "task",
    "session",
    "context",
    "workspace",
    "artifact",
    "resource_budget",
    "capability",
];

fn records(path: &std::path::Path) -> Vec<Vec<Vec<Value>>> {
    let db = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    TABLES
        .iter()
        .map(|table| {
            let mut statement = db
                .prepare(&format!("SELECT * FROM {table} ORDER BY id"))
                .unwrap();
            let columns = statement.column_count();
            statement
                .query_map([], |row| (0..columns).map(|i| row.get(i)).collect())
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap()
        })
        .collect()
}

#[test]
fn all_ten_minimum_object_records_and_relations_survive_registry_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("core.db");
    let mut r = Registry::open(&path).unwrap();
    r.register_model(
        &Actor::Owner,
        "phase0/mock-v1",
        json!({"backend":"deterministic", "cloud_allowed":false}),
    )
    .unwrap();
    let instance = r
        .register_model_instance(&Actor::Core, "phase0/mock-v1", "local-test")
        .unwrap();
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 256, 1).unwrap();
    let task = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "minimum object persistence".into(),
                acceptance: vec!["exact independent bytes".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 1048576,
                    max_storage_bytes: 1048576,
                    checkpoint_verified: false,
                },
                budget_id: budget.clone(),
                workspace_id: ws.id.clone(),
                cloud_allowed: false,
            },
        )
        .unwrap();
    let agent = r
        .register_agent(
            &Actor::Owner,
            "existing-harness",
            &task.id,
            "phase0/mock-v1",
        )
        .unwrap();
    let agent = r
        .transition_agent(&Actor::Core, &agent.id, 0, AgentState::Ready)
        .unwrap();
    let agent = r
        .transition_agent(&Actor::Core, &agent.id, agent.version, AgentState::Running)
        .unwrap();
    let session = r
        .start_session(&Actor::Core, &agent.id, "process:42")
        .unwrap();
    let ctx = r
        .create_context(
            &Actor::Core,
            &task.id,
            "internal",
            "untrusted",
            json!({"source":"fixed-test"}),
            None,
        )
        .unwrap();
    let cap = r
        .grant(&Actor::Owner, &agent.id, "read", &ctx, now() + 3600)
        .unwrap();
    let task = r
        .transition_task(&Actor::Core, &task.id, 0, TaskState::Ready, None)
        .unwrap();
    r.transition_task(
        &Actor::Core,
        &task.id,
        task.version,
        TaskState::Running,
        None,
    )
    .unwrap();
    let artifact = r
        .submit_artifact(
            &Actor::Agent(agent.id.clone()),
            &task.id,
            "text/markdown",
            true,
            "# fixed outcome\n",
        )
        .unwrap();
    let ids = [
        &agent.id,
        &task.id,
        &ws.id,
        &budget,
        &instance,
        &session,
        &ctx,
        &cap,
        &artifact.id,
    ];
    assert_eq!(
        ids.iter()
            .copied()
            .collect::<std::collections::HashSet<_>>()
            .len(),
        ids.len()
    );
    assert_ne!(agent.id, "process:42");
    r.integrity_check().unwrap();
    let before = records(&path);
    assert!(before.iter().all(|rows| rows.len() == 1));
    drop(r);
    let mut r = Registry::open(&path).unwrap();
    assert_eq!(records(&path), before);
    assert_eq!(r.agent(&agent.id).unwrap().task_id, task.id);
    assert_eq!(r.agent(&agent.id).unwrap().workspace_id, ws.id);
    assert_eq!(r.agent(&agent.id).unwrap().budget_id, budget);
    assert_eq!(
        r.artifacts(&task.id).unwrap()[0].content,
        "# fixed outcome\n"
    );
    assert_eq!(
        r.read_context(&agent.id, &ctx, "internal", false).unwrap()["trust"],
        "untrusted"
    );
    r.revoke(&Actor::Owner, &cap).unwrap();
    assert!(r.read_context(&agent.id, &ctx, "internal", false).is_err());
    r.integrity_check().unwrap();
}
