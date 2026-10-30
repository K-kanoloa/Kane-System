mod support;
use kane_core::*;
use serde_json::json;
#[test]
fn runtime_events_are_atomic_and_keep_bindings_without_claiming_task_completion() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, t, a) = support::fixture(&path, 3);
    assert!(
        r.acquire_runtime(&Actor::Agent(a.id.clone()), &a.id, true)
            .is_err()
    );
    assert!(
        r.configure_runtime(&Actor::Core, &a.id, json!({"cwd":"/other"}))
            .is_err()
    );
    assert!(
        r.transition_agent(&Actor::Core, &a.id, 0, AgentState::Ready)
            .is_err()
    );
    let s = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    let token = s["control"]["owner_token"].as_str().unwrap();
    r.begin_runtime(&Actor::Core, &a.id, token).unwrap();
    let before = r.runtime_status(&a.id).unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER fail_runtime BEFORE INSERT ON ledger WHEN NEW.kind='runtime_lifecycle' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(support::emit(&mut r, &a.id, json!({"event":"started","pid":42})).is_err());
    assert_eq!(r.runtime_status(&a.id).unwrap(), before);
    assert_eq!(
        db.query_row("SELECT count(*) FROM session", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    db.execute_batch("DROP TRIGGER fail_runtime").unwrap();
    support::emit(&mut r, &a.id, json!({"event":"started","pid":42})).unwrap();
    support::lost(&mut r, &a.id);
    support::launch(&mut r, &a.id, 43);
    let current = r.agent(&a.id).unwrap();
    assert_eq!(
        (current.task_id, current.workspace_id, current.budget_id),
        (t.id.clone(), t.spec.workspace_id, t.spec.budget_id)
    );
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Pending);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM session WHERE ended_at IS NULL",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}
#[test]
fn old_owner_and_old_attempt_cannot_overwrite_even_with_current_cas_version() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, _, a) = support::fixture(&d.path().join("db"), 4);
    support::launch(&mut r, &a.id, 1);
    let old = r.runtime_status(&a.id).unwrap()["control"].clone();
    let before_takeover = r.runtime_status(&a.id).unwrap();
    let db = rusqlite::Connection::open(d.path().join("db")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_takeover BEFORE INSERT ON ledger WHEN NEW.kind='runtime_lifecycle' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(r.acquire_runtime(&Actor::Core, &a.id, true).is_err());
    assert_eq!(r.runtime_status(&a.id).unwrap(), before_takeover);
    db.execute_batch("DROP TRIGGER fail_takeover").unwrap();

    assert!(r.acquire_runtime(&Actor::Core, &a.id, false).is_err());
    r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    support::launch(&mut r, &a.id, 2);
    let before = r.runtime_status(&a.id).unwrap();
    assert!(r.runtime_event(&Actor::Core,&a.id,r.agent(&a.id).unwrap().version,json!({"event":"process_exited","success":false,"owner_token":old["owner_token"],"attempt":old["attempt"]})).is_err());
    assert_eq!(r.runtime_status(&a.id).unwrap(), before);
    support::lost(&mut r, &a.id);
    support::launch(&mut r, &a.id, 3);
    let c = r.runtime_status(&a.id).unwrap()["control"].clone();
    assert!(r.runtime_event(&Actor::Core,&a.id,r.agent(&a.id).unwrap().version,json!({"event":"process_exited","success":false,"owner_token":c["owner_token"],"attempt":2})).is_err());
    assert!(support::emit(&mut r, &a.id, json!({"event":"started","pid":99})).is_err());
}
#[test]
fn normal_process_exit_suspends_identity_and_explicit_finish_terminates_it() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = support::fixture(&d.path().join("db"), 3);
    support::launch(&mut r, &a.id, 1);
    support::emit(
        &mut r,
        &a.id,
        json!({"event":"process_exited","success":true}),
    )
    .unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Suspended);
    support::launch(&mut r, &a.id, 2);
    support::emit(&mut r, &a.id, json!({"event":"completed"})).unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Exited);
    assert!(r.acquire_runtime(&Actor::Owner, &a.id, true).is_err());
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Pending);
}
#[test]
fn failure_exhaustion_survives_reopen_and_owner_cannot_reset_policy() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let (mut r, _, a) = support::fixture(&p, 1);
    support::launch(&mut r, &a.id, 1);
    support::lost(&mut r, &a.id);
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Ready);
    let s = r.runtime_status(&a.id).unwrap();
    r.begin_runtime(
        &Actor::Core,
        &a.id,
        s["control"]["owner_token"].as_str().unwrap(),
    )
    .unwrap();
    support::emit(
        &mut r,
        &a.id,
        json!({"event":"start_failed","reason":"missing executable"}),
    )
    .unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Failed);
    drop(r);
    let mut r = Registry::open(p).unwrap();
    assert!(r.acquire_runtime(&Actor::Owner, &a.id, true).is_err());
    assert!(
        r.configure_runtime(&Actor::Owner, &a.id, json!({"max_restarts":100}))
            .is_err()
    );
    assert_eq!(r.runtime_status(&a.id).unwrap()["control"]["attempt"], 2);
}
#[test]
fn pause_never_resets_allowance_and_owner_termination_is_two_phase() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, _, a) = support::fixture(&d.path().join("db"), 0);
    support::launch(&mut r, &a.id, 1);
    support::emit(&mut r, &a.id, json!({"event":"stopped"})).unwrap();
    let s = r.acquire_runtime(&Actor::Owner, &a.id, true).unwrap();
    assert_eq!(
        r.begin_runtime(
            &Actor::Owner,
            &a.id,
            s["control"]["owner_token"].as_str().unwrap()
        )
        .unwrap()["allowed"],
        false
    );
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Failed);
    let (mut r, t, a) = support::fixture(&d.path().join("other"), 2);
    support::launch(&mut r, &a.id, 1);
    support::emit(&mut r, &a.id, json!({"event":"cancelling"})).unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Cancelling);
    support::emit(&mut r, &a.id, json!({"event":"terminated"})).unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Terminated);
    assert_ne!(r.task(&t.id).unwrap().state, TaskState::Done);
    assert!(r.acquire_runtime(&Actor::Owner, &a.id, true).is_err());
}
#[test]
fn owner_loss_during_cancelling_finishes_only_after_absence_confirmation() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, _, a) = support::fixture(&d.path().join("db"), 2);
    support::launch(&mut r, &a.id, 1);
    support::emit(&mut r, &a.id, json!({"event":"cancelling"})).unwrap();
    assert!(r.acquire_runtime(&Actor::Core, &a.id, false).is_err());
    r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Terminated);
}
#[test]
fn launch_reservation_is_atomic_bounded_and_not_reissued() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let (mut r, _, a) = support::fixture(&p, 3);
    let s = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    let token = s["control"]["owner_token"].as_str().unwrap().to_owned();
    let before = r.runtime_status(&a.id).unwrap();
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute_batch("CREATE TRIGGER fail_attempt BEFORE INSERT ON ledger WHEN NEW.kind='runtime_lifecycle' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(r.begin_runtime(&Actor::Core, &a.id, &token).is_err());
    assert_eq!(r.runtime_status(&a.id).unwrap(), before);
    db.execute_batch("DROP TRIGGER fail_attempt").unwrap();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let mut threads = vec![];
    for _ in 0..8 {
        let p = p.clone();
        let id = a.id.clone();
        let token = token.clone();
        let barrier = barrier.clone();
        threads.push(std::thread::spawn(move || {
            let mut r = Registry::open(p).unwrap();
            barrier.wait();
            r.begin_runtime(&Actor::Core, &id, &token).is_ok()
        }));
    }
    assert_eq!(
        threads
            .into_iter()
            .map(|t| t.join().unwrap() as usize)
            .sum::<usize>(),
        1
    );
    assert_eq!(r.runtime_status(&a.id).unwrap()["control"]["attempt"], 1);
}
#[test]
fn old_schema_preserves_consumed_attempts_and_schema_five_backups_restore() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("db");
    let (mut r, _, a) = support::fixture(&p, 2);
    drop(r);
    let db = rusqlite::Connection::open(&p).unwrap();
    db.execute_batch("DROP TABLE runtime_control; DELETE FROM schema_version WHERE version=5; PRAGMA user_version=4;").unwrap();
    db.execute(
        "INSERT INTO ledger(ts,kind,object_id,payload_json) VALUES(0,'runtime_lifecycle',?1,?2)",
        rusqlite::params![a.id, json!({"detail":{"attempt":3}}).to_string()],
    )
    .unwrap();
    drop(db);
    r = Registry::open(&p).unwrap();
    let s = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    assert_eq!(
        r.begin_runtime(
            &Actor::Core,
            &a.id,
            s["control"]["owner_token"].as_str().unwrap()
        )
        .unwrap()["allowed"],
        false
    );
    let backup = d.path().join("backup");
    r.backup(&Actor::Owner, &backup).unwrap();
    let restored = d.path().join("restored");
    Registry::restore_new(&Actor::Owner, backup, &restored).unwrap();
    let r = Registry::open(restored).unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Failed);
    assert_eq!(r.runtime_status(&a.id).unwrap()["control"]["attempt"], 3);
}

#[test]
fn managed_unqualified_events_and_terminal_task_launches_are_refused() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = support::fixture(&d.path().join("db"), 2);
    assert!(
        r.runtime_event(&Actor::Core, &a.id, 0, json!({"event":"prepared"}))
            .is_err()
    );
    let s = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    r.transition_task(&Actor::Core, &t.id, 0, TaskState::Cancelled, None)
        .unwrap();
    let before = r.runtime_status(&a.id).unwrap();
    assert!(
        r.begin_runtime(
            &Actor::Core,
            &a.id,
            s["control"]["owner_token"].as_str().unwrap()
        )
        .is_err()
    );
    assert_eq!(r.runtime_status(&a.id).unwrap(), before);
}

#[test]
fn lost_launch_reply_cannot_refund_or_reissue_the_reserved_lifetime_attempt() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = support::fixture(&d.path().join("db"), 0);
    let first = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    r.begin_runtime(
        &Actor::Core,
        &a.id,
        first["control"]["owner_token"].as_str().unwrap(),
    )
    .unwrap();
    // Caller disappears after commit but before any process starts. No refund.
    let next = r.acquire_runtime(&Actor::Core, &a.id, true).unwrap();
    assert_eq!(next["control"]["attempt"], 1);
    assert_eq!(
        r.begin_runtime(
            &Actor::Core,
            &a.id,
            next["control"]["owner_token"].as_str().unwrap()
        )
        .unwrap()["allowed"],
        false
    );
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Failed);
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Pending);
}
