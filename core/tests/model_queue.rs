mod support;
use kane_core::*;
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Barrier},
    thread,
};

fn registry(path: &Path) -> Registry {
    let mut r = Registry::open(path).unwrap();
    r.register_model(&Actor::Owner,"mock",json!({"cloud":false,"scheduling":{"mode":"mock_fifo","backend_kind":"mock"},"budget_profile":{"provider":"fixture","model":"mock","upper_bound_tokens":32,"bound_evidence":"fixed test"}})).unwrap();
    r
}
fn agent(r: &mut Registry, tag: &str, tokens: u64) -> (Task, Agent) {
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let b = r.create_budget(&Actor::Owner, tokens, 1).unwrap();
    let t = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "queue test".into(),
                acceptance: vec!["independent".into()],
                workload: Workload {
                    class: WorkloadClass::Inference,
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
        .register_agent(&Actor::Owner, "existing-agent", &t.id, "mock")
        .unwrap();
    support::launch(r, &a.id, 123);
    r.dispatch_harness(&Actor::Owner, &a.id, tag, tag, "prompt", tag)
        .unwrap();
    r.enable_model_budget(&Actor::Core, tag).unwrap();
    (t, a)
}
fn enqueue(r: &mut Registry, tag: &str) -> Value {
    r.reserve_model(&Actor::Core, tag, tag, tag, "fixture", "mock")
        .unwrap()
}
fn claim(r: &mut Registry, tag: &str) -> kane_core::Result<Value> {
    r.claim_model(&Actor::Core, tag, tag, tag)
}
fn finish(r: &mut Registry, tag: &str) {
    r.settle_model(
        &Actor::Core,
        tag,
        tag,
        json!({"outcome":"completed","usage":{"inputTokens":10,"outputTokens":5,"totalTokens":15}}),
    )
    .unwrap();
}
fn queue(r: &Registry) -> Vec<Value> {
    r.model_queue(&Actor::Owner)
        .unwrap()
        .as_array()
        .unwrap()
        .clone()
}

#[test]
fn shared_fifo_single_use_identity_and_queued_cancellation() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (ta, a) = agent(&mut r, "a", 100);
    let (tb, _) = agent(&mut r, "b", 100);
    agent(&mut r, "c", 100);
    let first = enqueue(&mut r, "a");
    thread::sleep(std::time::Duration::from_millis(5));
    assert_eq!(enqueue(&mut r, "a"), first);
    enqueue(&mut r, "b");
    enqueue(&mut r, "c");
    assert_eq!(queue(&r).len(), 3);
    assert!(
        r.claim_model(&Actor::Agent(a.id.clone()), "a", "a", "a")
            .is_err()
    );
    assert!(r.claim_model(&Actor::Core, "b", "a", "a").is_err());
    assert_eq!(claim(&mut r, "c").unwrap()["permit"], false);
    assert_eq!(claim(&mut r, "a").unwrap()["permit"], true);
    assert!(claim(&mut r, "a").is_err());
    assert_eq!(claim(&mut r, "b").unwrap()["permit"], false);
    assert_eq!(
        r.cancel_model(&Actor::Core, "b", "b", "b").unwrap()["state"],
        "Cancelled"
    );
    assert_eq!(
        r.model_budget(&Actor::Core, &tb.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    finish(&mut r, "a");
    finish(&mut r, "a");
    assert_eq!(
        r.model_budget(&Actor::Core, &ta.spec.budget_id).unwrap()["used_tokens"],
        15
    );
    assert_eq!(claim(&mut r, "c").unwrap()["permit"], true);
    finish(&mut r, "c");
    let rows = queue(&r);
    assert_eq!(
        rows.iter()
            .map(|v| v["state"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["Completed", "Cancelled", "Completed"]
    );
    assert_eq!(rows[0]["agent_id"], a.id);
    assert_eq!(rows[0]["task_id"], ta.id);
}
#[test]
fn insufficient_budget_advances_without_reservation_and_cannot_reenter() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 31);
    agent(&mut r, "b", 100);
    enqueue(&mut r, "a");
    enqueue(&mut r, "b");
    assert_eq!(claim(&mut r, "a").unwrap()["state"], "Denied");
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    assert!(
        r.reserve_model(&Actor::Core, "a", "a", "a", "fixture", "mock")
            .is_err()
    );
    assert_eq!(claim(&mut r, "b").unwrap()["permit"], true);
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "model_budget_denied")
            .count(),
        1
    );
}
#[test]
fn dispatch_event_failure_rolls_back_slot_and_budget_together() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    let (t, _) = agent(&mut r, "a", 100);
    enqueue(&mut r, "a");
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute_batch("CREATE TRIGGER fail_queue BEFORE INSERT ON ledger WHEN NEW.kind='model_dispatched' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(claim(&mut r, "a").is_err());
    assert_eq!(queue(&r)[0]["state"], "Queued");
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    assert!(
        !r.events()
            .unwrap()
            .iter()
            .any(|e| e.kind == "model_reserved")
    );
    db.execute_batch("DROP TRIGGER fail_queue").unwrap();
    assert_eq!(claim(&mut r, "a").unwrap()["permit"], true);
}
#[test]
fn competing_connections_issue_exactly_one_permit() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    agent(&mut r, "a", 100);
    enqueue(&mut r, "a");
    drop(r);
    let barrier = Arc::new(Barrier::new(8));
    let threads = (0..8)
        .map(|_| {
            let p = p.clone();
            let b = barrier.clone();
            thread::spawn(move || {
                let mut r = Registry::open(p).unwrap();
                b.wait();
                claim(&mut r, "a").is_ok_and(|v| v["permit"] == true)
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        threads
            .into_iter()
            .map(|t| t.join().unwrap() as usize)
            .sum::<usize>(),
        1
    );
    let r = Registry::open(&p).unwrap();
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "model_dispatched")
            .count(),
        1
    );
}
#[test]
fn core_restart_unknown_holds_budget_and_slot_without_replay_and_snapshot_preserves_it() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    let (ta, _) = agent(&mut r, "a", 100);
    let (tb, _) = agent(&mut r, "b", 100);
    enqueue(&mut r, "a");
    enqueue(&mut r, "b");
    claim(&mut r, "a").unwrap();
    drop(r);
    let mut r = Registry::open(&p).unwrap();
    r.recover_model_queue(&Actor::Core).unwrap();
    r.recover_model_queue(&Actor::Core).unwrap();
    assert_eq!(queue(&r)[0]["state"], "Unknown");
    assert_eq!(queue(&r)[1]["state"], "Cancelled");
    assert_eq!(
        r.model_budget(&Actor::Core, &ta.spec.budget_id).unwrap()["reserved_tokens"],
        32
    );
    assert_eq!(
        r.model_budget(&Actor::Core, &tb.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    agent(&mut r, "c", 100);
    enqueue(&mut r, "c");
    assert_eq!(claim(&mut r, "c").unwrap()["permit"], false);
    let snapshot = d.path().join("snapshot");
    r.backup(&Actor::Owner, &snapshot).unwrap();
    let restored = d.path().join("restored");
    Registry::restore_new(&Actor::Owner, &snapshot, &restored).unwrap();
    let mut restored = Registry::open(restored).unwrap();
    assert_eq!(queue(&restored)[0]["state"], "Unknown");
    assert_eq!(claim(&mut restored, "c").unwrap()["permit"], false);
}
#[test]
fn active_cancellation_retains_hold_and_expired_unsent_head_can_be_skipped() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    agent(&mut r, "a", 100);
    let (tb, _) = agent(&mut r, "b", 100);
    enqueue(&mut r, "a");
    enqueue(&mut r, "b");
    rusqlite::Connection::open(&p)
        .unwrap()
        .execute("UPDATE model_queue SET expires_ms=0 WHERE call_id='a'", [])
        .unwrap();
    assert_eq!(claim(&mut r, "b").unwrap()["permit"], true);
    assert_eq!(queue(&r)[0]["state"], "Cancelled");
    assert_eq!(
        r.cancel_model(&Actor::Core, "b", "b", "b").unwrap()["state"],
        "Unknown"
    );
    assert_eq!(
        r.model_budget(&Actor::Core, &tb.spec.budget_id).unwrap()["reserved_tokens"],
        32
    );
    assert!(claim(&mut r, "b").is_err());
}
#[test]
fn pending_queue_is_bounded_and_v3_migration_is_idempotent() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    let (t, _) = agent(&mut r, "a", 100);
    for i in 0..128 {
        r.reserve_model(
            &Actor::Core,
            "a",
            &format!("call-{i}"),
            "a",
            "fixture",
            "mock",
        )
        .unwrap();
    }
    assert!(
        r.reserve_model(&Actor::Core, "a", "overflow", "a", "fixture", "mock")
            .is_err()
    );
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    drop(r);
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute_batch("DROP TABLE model_queue; DELETE FROM schema_version WHERE version=4; PRAGMA user_version=3;").unwrap();
    drop(db);
    for _ in 0..2 {
        Registry::open(&p).unwrap();
    }
    let db = rusqlite::Connection::open(&p).unwrap();
    assert_eq!(
        db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
            .unwrap(),
        9
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM schema_version WHERE version=4",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}

#[test]
fn enqueue_ledger_failure_leaves_no_half_queue_or_charge() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    let (t, _) = agent(&mut r, "a", 100);
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute_batch("CREATE TRIGGER fail_enqueue BEFORE INSERT ON ledger WHEN NEW.kind='model_enqueued' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(
        r.reserve_model(&Actor::Core, "a", "a", "a", "fixture", "mock")
            .is_err()
    );
    assert!(queue(&r).is_empty());
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
}
#[test]
fn non_mock_scheduling_policy_is_rejected_before_any_permit() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let mut r = registry(&p);
    let (t, _) = agent(&mut r, "a", 100);
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute(
        "UPDATE model SET spec_json=json_set(spec_json,'$.cloud',json('true')) WHERE id='mock'",
        [],
    )
    .unwrap();
    assert!(
        r.reserve_model(&Actor::Core, "a", "a", "a", "fixture", "mock")
            .is_err()
    );
    assert!(queue(&r).is_empty());
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
}
#[test]
fn confirmed_failed_final_usage_releases_slot_without_turning_failure_into_success() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 100);
    agent(&mut r, "b", 100);
    enqueue(&mut r, "a");
    enqueue(&mut r, "b");
    claim(&mut r, "a").unwrap();
    r.settle_model(
        &Actor::Core,
        "a",
        "a",
        json!({"outcome":"failed","usage":{"inputTokens":3,"outputTokens":0,"totalTokens":3}}),
    )
    .unwrap();
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
    assert_eq!(
        r.model_budget(&Actor::Core, &t.spec.budget_id).unwrap()["used_tokens"],
        3
    );
    assert_eq!(claim(&mut r, "b").unwrap()["permit"], true);
}

#[test]
fn task_cancel_fences_queued_and_inflight_calls_and_late_usage_never_refunds() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (ta, _) = agent(&mut r, "a", 100);
    let (tb, _) = agent(&mut r, "b", 100);
    enqueue(&mut r, "a");
    enqueue(&mut r, "b");
    claim(&mut r, "a").unwrap();
    assert_eq!(
        r.cancel_task(&Actor::Owner, &tb.id, "queued")
            .unwrap()
            .state,
        TaskState::Cancelled
    );
    assert_eq!(
        r.model_budget(&Actor::Owner, &tb.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    let blocked = r.cancel_task(&Actor::Owner, &ta.id, "inflight").unwrap();
    assert_eq!(blocked.state, TaskState::Blocked);
    assert_eq!(queue(&r)[0]["state"], "Unknown");
    assert_eq!(queue(&r)[1]["state"], "Cancelled");
    finish(&mut r, "a"); // A late completed stream is not an Owner reconciliation.
    assert_eq!(
        r.model_budget(&Actor::Owner, &ta.spec.budget_id).unwrap()["reserved_tokens"],
        32
    );
    assert_eq!(queue(&r)[0]["state"], "Unknown");
    let current = r.task(&ta.id).unwrap();
    assert!(
        r.transition_task(
            &Actor::Core,
            &ta.id,
            current.version,
            TaskState::Running,
            None
        )
        .is_err()
    );
    assert!(
        r.transition_task(
            &Actor::Core,
            &ta.id,
            current.version,
            TaskState::Cancelled,
            None
        )
        .is_err()
    );
    assert!(
        r.close_unverified(&Actor::Owner, &ta.id, current.version, "hide")
            .is_err()
    );
}
