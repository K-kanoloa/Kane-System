mod support;
use kane_core::*;
use serde_json::json;

fn fixture(path: &std::path::Path) -> (Registry, Task, Agent) {
    let mut r = Registry::open(path).unwrap();
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 100, 1).unwrap();
    r.register_model(&Actor::Owner, "fixture", json!({"backend":"mock"}))
        .unwrap();
    let task = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "read fixture".into(),
                acceptance: vec!["independent equality".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 1,
                    max_storage_bytes: 1,
                    checkpoint_verified: false,
                },
                budget_id: budget,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let agent = r
        .register_agent(&Actor::Owner, "existing-agent", &task.id, "fixture")
        .unwrap();
    r.configure_runtime(
        &Actor::Core,
        &agent.id,
        json!({"cwd":"/test","max_restarts":10}),
    )
    .unwrap();
    support::launch(&mut r, &agent.id, 123);
    (r, task, agent)
}
fn dispatch(r: &mut Registry, a: &Agent) -> kane_core::Result<Task> {
    r.dispatch_harness(
        &Actor::Owner,
        &a.id,
        "turn",
        "message",
        "read fixture",
        "native",
    )
}
fn fact(
    r: &mut Registry,
    id: &str,
    kind: &str,
    payload: serde_json::Value,
) -> kane_core::Result<()> {
    r.harness_fact(&Actor::Core, "turn", id, kind, payload)
}

#[test]
fn dispatch_rolls_back_and_retry_cannot_replay() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = fixture(&path);
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER fail_dispatch BEFORE INSERT ON ledger WHEN NEW.kind='prompt_dispatch' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(dispatch(&mut r, &a).is_err());
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Pending);
    assert!(r.bound_task("turn").unwrap().is_none());
    db.execute_batch("DROP TRIGGER fail_dispatch").unwrap();
    assert!(
        r.dispatch_harness(
            &Actor::Agent(a.id.clone()),
            &a.id,
            "turn",
            "message",
            "read fixture",
            "native"
        )
        .is_err()
    );
    dispatch(&mut r, &a).unwrap();
    assert!(dispatch(&mut r, &a).is_err());
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "prompt_dispatch")
            .count(),
        1
    );
    // Two DB connections cannot bypass admission even after initial completion.
    let mut other = Registry::open(&path).unwrap();
    assert!(dispatch(&mut other, &a).is_err());
}

#[test]
fn fact_receipts_completion_and_native_process_sessions_stay_distinct() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = fixture(&path);
    dispatch(&mut r, &a).unwrap();
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("reply", "result"))
        )
        .is_err()
    );
    let start = json!({"native_session_ref":"native","call_id":"call"});
    fact(&mut r, "call:start", "llm_call_start", start.clone()).unwrap();
    fact(&mut r, "call:start", "llm_call_start", start).unwrap();
    assert!(
        fact(
            &mut r,
            "call:start",
            "llm_call_start",
            json!({"native_session_ref":"native","call_id":"changed"})
        )
        .is_err()
    );
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("reply", "result"))
        )
        .is_err()
    );
    fact(
        &mut r,
        "call:result",
        "llm_call_result",
        json!({"native_session_ref":"native","call_id":"call","outcome":"completed"}),
    )
    .unwrap();
    let verifying = r
        .ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("reply", "result")),
        )
        .unwrap();
    assert_eq!(verifying.state, TaskState::Verifying);
    r.ingest_harness(
        &Actor::Core,
        "turn",
        "finished",
        Some("native"),
        Some(("reply", "result")),
    )
    .unwrap();
    assert_eq!(r.artifacts(&t.id).unwrap().len(), 1);
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "running",
            Some("different-native"),
            None
        )
        .is_err()
    );
    let db = rusqlite::Connection::open(path).unwrap();
    let refs: String = db
        .query_row(
            "SELECT runtime_ref FROM session WHERE agent_id=?1 AND ended_at IS NULL",
            [a.id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(refs, "process:123");
}

#[test]
fn runtime_loss_blocks_bound_task_and_does_not_replay_on_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = fixture(&path);
    dispatch(&mut r, &a).unwrap();
    r.ingest_harness(&Actor::Core, "turn", "waiting_user", Some("native"), None)
        .unwrap();
    assert_eq!(
        r.task(&t.id).unwrap().block_reason.as_deref(),
        Some("human")
    );
    support::lost(&mut r, &a.id);
    assert_eq!(
        r.task(&t.id).unwrap().block_reason.as_deref(),
        Some("reconciliation")
    );
    assert!(r.events().unwrap().iter().any(|e| e.kind == "state_change"
        && e.payload["from"] == "Blocked"
        && e.payload["runtime_event"] == "process_exited"));
    support::launch(&mut r, &a.id, 124);
    assert!(dispatch(&mut r, &a).is_err());
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("reply", "late result"))
        )
        .is_err()
    );
    assert!(r.artifacts(&t.id).unwrap().is_empty());
}

#[test]
fn cancellation_fence_atomic_idempotent_and_late_completion_cannot_succeed() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = fixture(&path);
    dispatch(&mut r, &a).unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER fail_cancel BEFORE INSERT ON ledger WHEN NEW.kind='task_cancel_requested' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(r.cancel_task(&Actor::Owner, &t.id, "Owner stop").is_err());
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Running);
    db.execute_batch("DROP TRIGGER fail_cancel").unwrap();
    assert!(
        r.cancel_task(&Actor::Agent(a.id.clone()), &t.id, "forged")
            .is_err()
    );
    let cancelled = r.cancel_task(&Actor::Owner, &t.id, "Owner stop").unwrap();
    assert_eq!(cancelled.state, TaskState::Cancelled);
    assert_eq!(
        r.cancel_task(&Actor::Owner, &t.id, "repeat")
            .unwrap()
            .version,
        cancelled.version
    );
    assert!(
        r.submit_artifact(&Actor::Agent(a.id), &t.id, "text/plain", true, "late")
            .is_err()
    );
    assert_eq!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("late", "result"))
        )
        .unwrap()
        .state,
        TaskState::Cancelled
    );
    assert!(r.artifacts(&t.id).unwrap().is_empty());
    drop(r);
    let r = Registry::open(path).unwrap();
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Cancelled);
}

#[test]
fn late_final_message_from_closed_execution_session_cannot_create_candidate() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&dir.path().join("db"));
    dispatch(&mut r, &a).unwrap();
    fact(
        &mut r,
        "start",
        "llm_call_start",
        json!({"native_session_ref":"native","call_id":"old"}),
    )
    .unwrap();
    fact(
        &mut r,
        "result",
        "llm_call_result",
        json!({"native_session_ref":"native","call_id":"old","outcome":"completed"}),
    )
    .unwrap();
    support::lost(&mut r, &a.id);
    support::launch(&mut r, &a.id, 456);
    let state = r.task(&t.id).unwrap();
    if state.state == TaskState::Blocked {
        r.transition_task(
            &Actor::Owner,
            &t.id,
            state.version,
            TaskState::Running,
            None,
        )
        .unwrap();
    }
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("late", "old result"))
        )
        .is_err()
    );
    assert!(r.artifacts(&t.id).unwrap().is_empty());
}
