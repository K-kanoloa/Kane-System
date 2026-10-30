mod support;
use kane_core::*;
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Barrier},
    thread,
};

fn fixture(path: &Path, limit: u64, concurrency: u32) -> (Registry, Task, Agent) {
    let mut r = Registry::open(path).unwrap();
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, limit, concurrency).unwrap();
    r.register_model(&Actor::Owner,"fixture",json!({"cloud":false,"budget_profile":{"provider":"test","model":"bounded","upper_bound_tokens":32,"bound_evidence":"deterministic backend test bound"}})).unwrap();
    let task = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "budget test".into(),
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
                budget_id: budget,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let agent = r
        .register_agent(&Actor::Owner, "original-agent", &task.id, "fixture")
        .unwrap();
    r.configure_runtime(
        &Actor::Core,
        &agent.id,
        json!({"cwd":"/test","max_restarts":10}),
    )
    .unwrap();
    support::launch(&mut r, &agent.id, 123);
    r.dispatch_harness(
        &Actor::Owner,
        &agent.id,
        "turn",
        "message",
        "prompt",
        "native",
    )
    .unwrap();
    r.enable_model_budget(&Actor::Core, "turn").unwrap();
    (r, task, agent)
}
fn reserve(r: &mut Registry, id: &str) -> kane_core::Result<Value> {
    r.reserve_model(&Actor::Core, "turn", id, "native", "test", "bounded")
}
fn usage(outcome: &str, tokens: u64) -> Value {
    json!({"outcome":outcome,"usage":{"inputTokens":tokens,"outputTokens":0,"totalTokens":tokens}})
}
fn budget(r: &Registry, t: &Task) -> Value {
    r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()
}

#[test]
fn final_usage_settlement_is_exact_idempotent_and_releases_unused_hold() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, _) = fixture(&dir.path().join("db"), 64, 1);
    reserve(&mut r, "call").unwrap();
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    assert!(reserve(&mut r, "call").is_err());
    assert!(reserve(&mut r, "parallel").is_err());
    r.settle_model(&Actor::Core, "turn", "call", usage("completed", 15))
        .unwrap();
    r.settle_model(&Actor::Core, "turn", "call", usage("completed", 15))
        .unwrap();
    assert!(
        r.settle_model(&Actor::Core, "turn", "call", usage("completed", 16))
            .is_err()
    );
    assert_eq!(budget(&r, &t)["used_tokens"], 15);
    assert_eq!(budget(&r, &t)["reserved_tokens"], 0);
    reserve(&mut r, "next").unwrap();
    r.settle_model(&Actor::Core, "turn", "next", usage("failed", 7))
        .unwrap();
    assert_eq!(budget(&r, &t)["used_tokens"], 22);
    assert_eq!(budget(&r, &t)["reserved_tokens"], 0);
    assert_eq!(
        r.task(&t.id).unwrap().block_reason.as_deref(),
        Some("reconciliation")
    );
    assert!(reserve(&mut r, "after-failure").is_err());
}
#[test]
fn parallel_connections_cannot_over_reserve_shared_budget() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (r, t, _) = fixture(&path, 100, 8);
    drop(r);
    let barrier = Arc::new(Barrier::new(8));
    let joins: Vec<_> = (0..8)
        .map(|i| {
            let path = path.clone();
            let barrier = barrier.clone();
            thread::spawn(move || {
                let mut r = Registry::open(path).unwrap();
                barrier.wait();
                reserve(&mut r, &format!("call-{i}")).is_ok()
            })
        })
        .collect();
    let accepted = joins
        .into_iter()
        .map(|t| usize::from(t.join().unwrap()))
        .sum::<usize>();
    assert_eq!(accepted, 3);
    let r = Registry::open(path).unwrap();
    assert_eq!(budget(&r, &t)["reserved_tokens"], 96);
    assert_eq!(budget(&r, &t)["available_tokens"], 4);
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "model_budget_denied")
            .count(),
        5
    );
}
#[test]
fn concurrent_call_limit_is_independent_of_token_limit() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, _) = fixture(&path, 1000, 1);
    reserve(&mut r, "first").unwrap();
    let mut other = Registry::open(path).unwrap();
    assert!(reserve(&mut other, "second").is_err());
    assert_eq!(budget(&r, &t)["active_calls"], 1);
}
#[test]
fn state_and_ledger_roll_back_together_for_reserve_and_settlement() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, _) = fixture(&path, 64, 1);
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER fail BEFORE INSERT ON ledger WHEN NEW.kind='model_reserved' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(reserve(&mut r, "call").is_err());
    assert_eq!(budget(&r, &t)["reserved_tokens"], 0);
    db.execute_batch("DROP TRIGGER fail").unwrap();
    reserve(&mut r, "call").unwrap();
    db.execute_batch("CREATE TRIGGER fail BEFORE INSERT ON ledger WHEN NEW.kind='model_settled' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(
        r.settle_model(&Actor::Core, "turn", "call", usage("completed", 15))
            .is_err()
    );
    assert_eq!(budget(&r, &t)["used_tokens"], 0);
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    db.execute_batch("DROP TRIGGER fail").unwrap();
    r.settle_model(&Actor::Core, "turn", "call", usage("completed", 15))
        .unwrap();
}
#[test]
fn unknown_usage_survives_restart_backup_and_restore_without_refund() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, _) = fixture(&path, 64, 1);
    reserve(&mut r, "call").unwrap();
    r.settle_model(
        &Actor::Core,
        "turn",
        "call",
        json!({"outcome":"unknown","usage":null}),
    )
    .unwrap();
    let snapshot = dir.path().join("snapshot");
    r.backup(&Actor::Owner, &snapshot).unwrap();
    drop(r);
    let restored = dir.path().join("restored");
    Registry::restore_new(&Actor::Owner, &snapshot, &restored).unwrap();
    let mut r = Registry::open(restored).unwrap();
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    assert_eq!(budget(&r, &t)["used_tokens"], 0);
    assert!(reserve(&mut r, "call").is_err());
    assert!(reserve(&mut r, "different-call").is_err());
}
#[test]
fn lost_reservation_ack_and_runtime_crash_retain_hold() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = fixture(&path, 64, 1);
    reserve(&mut r, "lost-ack").unwrap();
    drop(r);
    let mut r = Registry::open(path).unwrap();
    assert!(reserve(&mut r, "lost-ack").is_err());
    support::lost(&mut r, &a.id);
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
}
#[test]
fn overrun_records_full_actual_and_freezes_budget_instead_of_clamping() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, _) = fixture(&dir.path().join("db"), 32, 1);
    reserve(&mut r, "call").unwrap();
    let result = r
        .settle_model(&Actor::Core, "turn", "call", usage("completed", 45))
        .unwrap();
    assert_eq!(result["state"], "Overrun");
    assert_eq!(budget(&r, &t)["used_tokens"], 45);
    assert_eq!(budget(&r, &t)["frozen"], true);
    assert!(r.charge_tokens(&Actor::Core, &t.id, 0).is_err());
}
#[test]
fn identity_route_usage_validation_and_legacy_charges_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&dir.path().join("db"), 40, 1);
    assert!(
        r.reserve_model(
            &Actor::Agent(a.id),
            "turn",
            "call",
            "native",
            "test",
            "bounded"
        )
        .is_err()
    );
    assert!(
        r.reserve_model(&Actor::Core, "turn", "call", "wrong", "test", "bounded")
            .is_err()
    );
    assert!(
        r.reserve_model(&Actor::Core, "turn", "call", "native", "cloud", "bounded")
            .is_err()
    );
    reserve(&mut r, "call").unwrap();
    assert!(r.charge_tokens(&Actor::Core, &t.id, 9).is_err());
    r.charge_tokens(&Actor::Core, &t.id, 8).unwrap();
    for invalid in [
        json!({"outcome":"completed","usage":{"inputTokens":-1,"outputTokens":1,"totalTokens":0}}),
        json!({"outcome":"completed","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":3}}),
        json!({"outcome":"completed","usage":{"inputTokens":1.5,"outputTokens":1,"totalTokens":2.5}}),
        json!({"outcome":"unknown","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2}}),
    ] {
        assert!(
            r.settle_model(&Actor::Core, "turn", "call", invalid)
                .is_err()
        );
    }
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    r.harness_fact(
        &Actor::Core,
        "turn",
        "ctx",
        "context_usage",
        json!({"native_session_ref":"native","used":99999,"size":100000}),
    )
    .unwrap();
    assert_eq!(budget(&r, &t)["used_tokens"], 8);
}
#[test]
fn schema_one_migrates_idempotently_and_snapshot_restore_supports_both() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("v1");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch(include_str!("../src/schema.sql")).unwrap();
    db.pragma_update(None, "user_version", 1).unwrap();
    drop(db);
    let restored = dir.path().join("v1-restored");
    Registry::restore_new(&Actor::Owner, &path, &restored).unwrap();
    Registry::open(&restored).unwrap();
    for _ in 0..2 {
        Registry::open(&path).unwrap();
    }
    let db = rusqlite::Connection::open(&path).unwrap();
    assert_eq!(
        db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
            .unwrap(),
        9
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM schema_version", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        9
    );
}

#[test]
fn unresolved_model_calls_cannot_be_hidden_by_task_transition_or_close() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, _) = fixture(&dir.path().join("db"), 64, 1);
    reserve(&mut r, "call").unwrap();
    let task = r.task(&t.id).unwrap();
    assert!(
        r.transition_task(
            &Actor::Owner,
            &t.id,
            task.version,
            TaskState::Verifying,
            None
        )
        .is_err()
    );
    assert!(
        r.transition_task(
            &Actor::Owner,
            &t.id,
            task.version,
            TaskState::Cancelled,
            None
        )
        .is_err()
    );
    let task = r
        .transition_task(
            &Actor::Owner,
            &t.id,
            task.version,
            TaskState::Blocked,
            Some("awaiting-verification"),
        )
        .unwrap();
    assert!(
        r.close_unverified(&Actor::Owner, &t.id, task.version, "accept")
            .is_err()
    );
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
}

#[test]
fn protected_observations_must_reference_the_same_reserved_call() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, _, _) = fixture(&dir.path().join("db"), 64, 1);
    let fact = json!({"call_id":"call","native_session_ref":"native"});
    assert!(
        r.harness_fact(
            &Actor::Core,
            "turn",
            "call:start",
            "llm_call_start",
            fact.clone()
        )
        .is_err()
    );
    reserve(&mut r, "call").unwrap();
    r.harness_fact(&Actor::Core, "turn", "call:start", "llm_call_start", fact)
        .unwrap();
}

#[test]
fn failed_model_and_inflight_tool_enter_reconciliation_atomically() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let (mut r, t, a) = fixture(&db, 64, 1);
    reserve(&mut r, "inflight-model").unwrap();
    r.grant(&Actor::Owner, &a.id, "execute", "test:local", 4_000_000_000)
        .unwrap();
    let action = r
        .prepare_action(
            &Actor::Agent(a.id),
            &t.id,
            "inflight-tool",
            "test:local",
            json!({}),
        )
        .unwrap();
    r.claim_action(&Actor::Core, &action.id).unwrap();
    let raw = rusqlite::Connection::open(&db).unwrap();
    raw.execute_batch("CREATE TRIGGER reject_reconcile BEFORE INSERT ON ledger WHEN NEW.kind='reconcile' BEGIN SELECT RAISE(ABORT,'fault'); END;").unwrap();
    assert!(
        r.settle_model(&Actor::Core, "turn", "inflight-model", usage("failed", 7))
            .is_err()
    );
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Running);
    assert_eq!(r.action(&action.id).unwrap().state, "Dispatching");
    assert_eq!(budget(&r, &t)["reserved_tokens"], 32);
    raw.execute_batch("DROP TRIGGER reject_reconcile").unwrap();
    r.settle_model(&Actor::Core, "turn", "inflight-model", usage("failed", 7))
        .unwrap();
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
    assert_eq!(r.action(&action.id).unwrap().state, "Unknown");
}

#[test]
fn recovered_process_cannot_reuse_old_prompt_session_for_new_model_admission() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&dir.path().join("db"), 100, 1);
    support::lost(&mut r, &a.id);
    support::launch(&mut r, &a.id, 456);
    let task = r.task(&t.id).unwrap();
    if task.state == TaskState::Blocked {
        r.transition_task(&Actor::Owner, &t.id, task.version, TaskState::Running, None)
            .unwrap();
    }
    assert!(reserve(&mut r, "late-old-native").is_err());
    assert_eq!(budget(&r, &t)["reserved_tokens"], 0);
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("native"),
            Some(("late", "fake success"))
        )
        .is_err()
    );
    assert!(r.artifacts(&t.id).unwrap().is_empty());
}

#[test]
fn unclassified_or_cloud_legacy_route_is_denied_before_reservation() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let (mut r, t, _) = fixture(&path, 100, 1);
    let db = rusqlite::Connection::open(&path).unwrap();
    for cloud in [serde_json::Value::Null, serde_json::Value::Bool(true)] {
        let data: String = db
            .query_row(
                "SELECT spec_json FROM model WHERE id='fixture'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let mut spec: Value = serde_json::from_str(&data).unwrap();
        spec["cloud"] = cloud;
        db.execute(
            "UPDATE model SET spec_json=?1 WHERE id='fixture'",
            [serde_json::to_string(&spec).unwrap()],
        )
        .unwrap();
        assert!(matches!(
            reserve(&mut r, "cloud-attempt"),
            Err(Error::Denied(_))
        ));
        assert_eq!(budget(&r, &t)["reserved_tokens"], 0);
    }
}
