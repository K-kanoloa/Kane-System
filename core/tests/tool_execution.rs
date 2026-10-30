mod support;
use kane_core::*;
use serde_json::{Value, json};
use std::sync::{Arc, Barrier};

fn fixture(r: &mut Registry, suffix: &str) -> (Task, Agent, String) {
    fixture_config(r, suffix, json!({"cwd":"/isolated"}), 1)
}
fn fixture_config(
    r: &mut Registry,
    suffix: &str,
    config: Value,
    storage: u64,
) -> (Task, Agent, String) {
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 1000, 2).unwrap();
    r.register_model(&Actor::Owner, suffix, json!({})).unwrap();
    let task = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "tool fixture".into(),
                acceptance: vec!["independent".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 1,
                    max_storage_bytes: storage,
                    checkpoint_verified: false,
                },
                budget_id: budget,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let agent = r
        .register_agent(&Actor::Owner, "existing-harness", &task.id, suffix)
        .unwrap();
    r.configure_runtime(&Actor::Core, &agent.id, config)
        .unwrap();
    support::launch(r, &agent.id, 123);
    r.dispatch_harness(
        &Actor::Owner,
        &agent.id,
        suffix,
        "message",
        "prompt",
        suffix,
    )
    .unwrap();
    let target = r
        .tool_target(
            &Actor::Owner,
            &agent.id,
            "read",
            &json!({"file_path":"input.txt"}),
        )
        .unwrap();
    let cap = r
        .grant(&Actor::Owner, &agent.id, "execute", &target, 4_000_000_000)
        .unwrap();
    (task, agent, cap)
}
fn request(r: &mut Registry, turn: &str, op: &str, call: &str) -> kane_core::Result<Value> {
    r.tool_request(&Actor::Core,turn,turn,call,json!({"op":op,"tool":"read","arguments":{"file_path":"input.txt"},"receipt":{"outcome":"completed","result_hash":"a".repeat(64)}}))
}
#[test]
fn intent_claim_receipt_are_durable_and_idempotent_without_second_permit() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    let (t, a, _) = fixture(&mut r, "turn");
    let intent = request(&mut r, "turn", "tool_prepare", "call").unwrap();
    assert_eq!(
        intent,
        request(&mut r, "turn", "tool_prepare", "call").unwrap()
    );
    assert!(
        intent["payload"]["target"]
            .as_str()
            .unwrap()
            .contains(&t.spec.workspace_id)
    );
    assert_eq!(intent["payload"]["body"]["native_session_ref"], "turn");
    drop(r);
    let mut r = Registry::open(&db).unwrap();
    assert_eq!(
        r.action(intent["id"].as_str().unwrap()).unwrap().state,
        "Pending"
    );
    assert!(
        request(&mut r, "turn", "tool_claim", "call").unwrap()["permit"]
            .as_bool()
            .unwrap()
    );
    assert!(request(&mut r, "turn", "tool_claim", "call").is_err());
    let result = request(&mut r, "turn", "tool_result", "call").unwrap();
    assert_eq!(result["state"], "Confirmed");
    assert_eq!(
        result,
        request(&mut r, "turn", "tool_result", "call").unwrap()
    );
    assert_eq!(r.agent(&a.id).unwrap().task_id, t.id);
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "result")
            .count(),
        1
    );
}
#[test]
fn scoped_capability_expiry_revocation_wrong_arguments_and_native_identity_rejected() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    let (_, a, cap) = fixture(&mut r, "turn");
    assert!(
        r.tool_request(
            &Actor::Agent(a.id.clone()),
            "turn",
            "turn",
            "c",
            json!({"op":"tool_prepare"})
        )
        .is_err()
    );
    assert!(
        r.tool_request(
            &Actor::Core,
            "turn",
            "forged",
            "c",
            json!({"op":"tool_prepare"})
        )
        .is_err()
    );
    assert!(
        r.tool_request(
            &Actor::Core,
            "turn",
            "turn",
            "c",
            json!({"op":"tool_prepare","tool":"read","arguments":{"file_path":"secret"}})
        )
        .is_err()
    );
    request(&mut r, "turn", "tool_prepare", "c").unwrap();
    r.revoke(&Actor::Owner, &cap).unwrap();
    assert!(request(&mut r, "turn", "tool_claim", "c").is_err());
    assert!(request(&mut r, "turn", "tool_prepare", "new").is_err());
    assert!(
        r.grant(&Actor::Owner, &a.id, "execute", "anything", 1)
            .is_err()
    );
    assert!(r.events().unwrap().iter().any(|e| e.kind == "tool_denied"));
}
#[test]
fn concurrent_claimants_receive_only_one_permit() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    fixture(&mut r, "turn");
    request(&mut r, "turn", "tool_prepare", "c").unwrap();
    let gate = Arc::new(Barrier::new(5));
    let workers = (0..4)
        .map(|_| {
            let db = db.clone();
            let gate = gate.clone();
            std::thread::spawn(move || {
                let mut r = Registry::open(db).unwrap();
                gate.wait();
                request(&mut r, "turn", "tool_claim", "c").is_ok()
            })
        })
        .collect::<Vec<_>>();
    gate.wait();
    assert_eq!(
        workers
            .into_iter()
            .filter_map(|h| h.join().ok())
            .filter(|v| *v)
            .count(),
        1
    );
}
#[test]
fn runtime_failure_recovers_only_its_actions_and_preserves_other_agent() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    let (t, a, _) = fixture(&mut r, "one");
    let (other, _, _) = fixture(&mut r, "two");
    for turn in ["one", "two"] {
        request(&mut r, turn, "tool_prepare", "c").unwrap();
        request(&mut r, turn, "tool_claim", "c").unwrap();
    }
    support::lost(&mut r, &a.id);
    assert_eq!(
        r.task(&t.id).unwrap().block_reason.as_deref(),
        Some("reconciliation")
    );
    assert_eq!(r.task(&other.id).unwrap().state, TaskState::Running);
    assert_eq!(
        request(&mut r, "two", "tool_result", "c").unwrap()["state"],
        "Confirmed"
    );
    let late = request(&mut r, "one", "tool_result", "c").unwrap();
    assert_eq!(late["state"], "Unknown");
    assert!(request(&mut r, "one", "tool_claim", "c").is_err());
    assert!(
        r.close_unverified(
            &Actor::Owner,
            &t.id,
            r.task(&t.id).unwrap().version,
            "accept"
        )
        .is_err()
    );
    r.reconcile_action(
        &Actor::Owner,
        late["id"].as_str().unwrap(),
        true,
        "independently inspected file",
    )
    .unwrap();
    assert!(request(&mut r, "one", "tool_claim", "c").is_err());
}
#[test]
fn tool_failure_and_changed_receipt_cannot_clear_unknown_or_create_second_result() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    fixture(&mut r, "turn");
    request(&mut r, "turn", "tool_prepare", "c").unwrap();
    request(&mut r, "turn", "tool_claim", "c").unwrap();
    let data =
        json!({"op":"tool_result","receipt":{"outcome":"unknown","result_hash":"b".repeat(64)}});
    let out = r
        .tool_request(&Actor::Core, "turn", "turn", "c", data.clone())
        .unwrap();
    assert_eq!(out["state"], "Unknown");
    assert_eq!(
        out,
        r.tool_request(&Actor::Core, "turn", "turn", "c", data)
            .unwrap()
    );
    assert!(request(&mut r, "turn", "tool_result", "c").is_err());
    assert_eq!(r.recover_actions(&Actor::Core).unwrap(), 0);
}
#[test]
fn intent_and_dispatch_ledger_failures_roll_back_action_state() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    fixture(&mut r, "turn");
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch("CREATE TRIGGER fail_intent BEFORE INSERT ON ledger WHEN NEW.kind='intent' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(request(&mut r, "turn", "tool_prepare", "c").is_err());
    assert_eq!(
        conn.query_row("SELECT count(*) FROM outbox", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    conn.execute_batch("DROP TRIGGER fail_intent;").unwrap();
    let intent = request(&mut r, "turn", "tool_prepare", "c").unwrap();
    conn.execute_batch("CREATE TRIGGER fail_dispatch BEFORE INSERT ON ledger WHEN NEW.kind='dispatch' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(request(&mut r, "turn", "tool_claim", "c").is_err());
    assert_eq!(
        r.action(intent["id"].as_str().unwrap()).unwrap().state,
        "Pending"
    );
    conn.execute_batch("DROP TRIGGER fail_dispatch;").unwrap();
    request(&mut r, "turn", "tool_claim", "c").unwrap();
    conn.execute_batch("CREATE TRIGGER fail_result BEFORE INSERT ON ledger WHEN NEW.kind='result' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(request(&mut r, "turn", "tool_result", "c").is_err());
    assert_eq!(
        r.action(intent["id"].as_str().unwrap()).unwrap().state,
        "Dispatching"
    );
}

#[test]
fn expiry_before_claim_denies_even_prepared_intent() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    let (_, _, cap) = fixture(&mut r, "turn");
    let intent = request(&mut r, "turn", "tool_prepare", "c").unwrap();
    let conn = rusqlite::Connection::open(&db).unwrap();
    // Simulate time passing without nondeterministic sleep.
    conn.execute("UPDATE capability SET expires_at=1 WHERE id=?1", [cap])
        .unwrap();
    assert!(request(&mut r, "turn", "tool_claim", "c").is_err());
    assert_eq!(
        r.action(intent["id"].as_str().unwrap()).unwrap().state,
        "Pending"
    );
}
#[test]
fn unconfirmed_tool_blocks_artifact_submission_even_with_complete_model_observations() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    fixture(&mut r, "turn");
    r.harness_fact(
        &Actor::Core,
        "turn",
        "start",
        "llm_call_start",
        json!({"call_id":"model","native_session_ref":"turn"}),
    )
    .unwrap();
    r.harness_fact(
        &Actor::Core,
        "turn",
        "end",
        "llm_call_result",
        json!({"call_id":"model","native_session_ref":"turn","outcome":"completed"}),
    )
    .unwrap();
    request(&mut r, "turn", "tool_prepare", "c").unwrap();
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("turn"),
            Some(("message-result", "done"))
        )
        .is_err()
    );
    request(&mut r, "turn", "tool_claim", "c").unwrap();
    assert!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("turn"),
            Some(("message-result", "done"))
        )
        .is_err()
    );
    request(&mut r, "turn", "tool_result", "c").unwrap();
    assert_eq!(
        r.ingest_harness(
            &Actor::Core,
            "turn",
            "finished",
            Some("turn"),
            Some(("message-result", "done"))
        )
        .unwrap()
        .state,
        TaskState::Verifying
    );
}

#[cfg(target_os = "linux")]
fn native_setup(r: &mut Registry, root: &std::path::Path, limit: u64) -> (Task, Agent) {
    let (t, a, _) = fixture_config(
        r,
        "native",
        json!({"cwd":root,"sandbox":{"native_file_proxy":true}}),
        limit,
    );
    r.grant(
        &Actor::Owner,
        &a.id,
        "modify",
        &a.workspace_id,
        4_000_000_000,
    )
    .unwrap();
    r.transfer_lease(&Actor::Owner, &a.workspace_id, 0, &a.id, 4_000_000_000)
        .unwrap();
    (t, a)
}
#[cfg(target_os = "linux")]
fn native_intent(r: &mut Registry, a: &Agent, call: &str, args: &Value) -> Value {
    let target = r.tool_target(&Actor::Owner, &a.id, "write", args).unwrap();
    r.grant(&Actor::Owner, &a.id, "execute", &target, 4_000_000_000)
        .unwrap();
    let v = r
        .tool_request(
            &Actor::Core,
            "native",
            "native",
            call,
            json!({"op":"tool_prepare","tool":"write","arguments":args}),
        )
        .unwrap();
    r.tool_request(
        &Actor::Core,
        "native",
        "native",
        call,
        json!({"op":"tool_claim"}),
    )
    .unwrap();
    v
}
#[cfg(target_os = "linux")]
fn native_commit(r: &mut Registry, call: &str, args: &Value) -> kane_core::Result<Value> {
    r.tool_request(
        &Actor::Core,
        "native",
        "native",
        call,
        json!({"op":"tool_file","arguments":args}),
    )
}
#[test]
#[cfg(target_os = "linux")]
fn native_files_enforce_exact_arguments_cumulative_storage_and_no_replay() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    let (_, a) = native_setup(&mut r, d.path(), 3);
    let args = json!({"file_path":"first.txt","content":"ab"});
    native_intent(&mut r, &a, "one", &args);
    assert!(
        native_commit(
            &mut r,
            "one",
            &json!({"file_path":"escape.txt","content":"ab"})
        )
        .is_err()
    );
    native_commit(&mut r, "one", &args).unwrap();
    assert_eq!(std::fs::read(d.path().join("first.txt")).unwrap(), b"ab");
    assert!(native_commit(&mut r, "one", &args).is_err());
    let second = json!({"file_path":"second.txt","content":"cd"});
    native_intent(&mut r, &a, "two", &second);
    assert!(matches!(
        native_commit(&mut r, "two", &second),
        Err(Error::Budget)
    ));
    assert!(!d.path().join("second.txt").exists());
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 1);
}
#[test]
#[cfg(target_os = "linux")]
fn native_files_cannot_claim_success_without_publication_or_escape_flat_workspace() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    let (_, a) = native_setup(&mut r, d.path(), 4096);
    for (n, path) in [
        "../outside",
        "/tmp/absolute",
        "sub/file",
        ".hidden",
        "back\\slash",
    ]
    .iter()
    .enumerate()
    {
        let call = format!("bad-{n}");
        let args = json!({"file_path":path,"content":"x"});
        native_intent(&mut r, &a, &call, &args);
        assert!(native_commit(&mut r, &call, &args).is_err());
        assert!(r.tool_request(&Actor::Core,"native","native",&call,json!({"op":"tool_result","receipt":{"outcome":"completed","result_hash":"a".repeat(64)}})).is_err());
    }
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 0);
}
#[test]
#[cfg(target_os = "linux")]
fn filesystem_success_followed_by_database_failure_stays_unknown_after_recovery() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    let (task, a) = native_setup(&mut r, d.path(), 4096);
    let args = json!({"file_path":"published.txt","content":"persisted"});
    let intent = native_intent(&mut r, &a, "fault", &args);
    let raw = rusqlite::Connection::open(&db).unwrap();
    raw.execute_batch("CREATE TRIGGER fail_file_record BEFORE INSERT ON ledger WHEN NEW.kind='native_file_commit' BEGIN SELECT RAISE(ABORT,'injected post-file failure'); END;").unwrap();
    assert!(native_commit(&mut r, "fault", &args).is_err());
    assert_eq!(
        std::fs::read_to_string(d.path().join("published.txt")).unwrap(),
        "persisted"
    );
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 0);
    raw.execute_batch("DROP TRIGGER fail_file_record").unwrap();
    assert!(native_commit(&mut r, "fault", &args).is_err());
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "native_file_attempt")
            .count(),
        1
    );
    drop(raw);
    drop(r);
    let mut r = Registry::open(&db).unwrap();
    r.recover_actions(&Actor::Core).unwrap();
    assert_eq!(
        r.action(intent["id"].as_str().unwrap()).unwrap().state,
        "Unknown"
    );
    assert_eq!(r.task(&task.id).unwrap().state, TaskState::Blocked);
    assert!(native_commit(&mut r, "fault", &args).is_err());
    assert_eq!(
        std::fs::read_to_string(d.path().join("published.txt")).unwrap(),
        "persisted"
    );
}

#[test]
#[cfg(target_os = "linux")]
fn concurrent_native_commits_share_one_workspace_version() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    let (_, a) = native_setup(&mut r, d.path(), 4096);
    let args = [
        json!({"file_path":"a.txt","content":"a"}),
        json!({"file_path":"b.txt","content":"b"}),
    ];
    for (i, arg) in args.iter().enumerate() {
        native_intent(&mut r, &a, &format!("race-{i}"), arg);
    }
    let barrier = Arc::new(Barrier::new(2));
    let handles: Vec<_> = args
        .into_iter()
        .enumerate()
        .map(|(i, arg)| {
            let db = db.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let mut r = Registry::open(db).unwrap();
                barrier.wait();
                native_commit(&mut r, &format!("race-{i}"), &arg).is_ok()
            })
        })
        .collect();
    assert_eq!(
        handles
            .into_iter()
            .map(|h| usize::from(h.join().unwrap()))
            .sum::<usize>(),
        1
    );
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 1);
    assert_ne!(
        d.path().join("a.txt").exists(),
        d.path().join("b.txt").exists()
    );
}

#[test]
#[cfg(target_os = "linux")]
fn task_stopped_after_file_attempt_cannot_publish() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let mut r = Registry::open(&db).unwrap();
    let (task, a) = native_setup(&mut r, d.path(), 4096);
    let args = json!({"file_path":"cancelled.txt","content":"must not write"});
    native_intent(&mut r, &a, "cancel-race", &args);
    // Inject a state change precisely between durable attempt and publication.
    let raw = rusqlite::Connection::open(&db).unwrap();
    raw.execute_batch("CREATE TRIGGER cancel_after_attempt AFTER INSERT ON ledger WHEN NEW.kind='native_file_attempt' BEGIN UPDATE task SET state='Cancelled'; END;").unwrap();
    assert!(native_commit(&mut r, "cancel-race", &args).is_err());
    assert!(!d.path().join("cancelled.txt").exists());
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 0);
    assert_eq!(r.task(&task.id).unwrap().state, TaskState::Cancelled);
}

#[test]
fn cancel_pending_and_claimed_tools_classifies_without_replay() {
    for claim in [false, true] {
        let d = tempfile::tempdir().unwrap();
        let mut r = Registry::open(d.path().join("db")).unwrap();
        let (t, _, _) = fixture(&mut r, "turn");
        let intent = request(&mut r, "turn", "tool_prepare", "call").unwrap();
        if claim {
            request(&mut r, "turn", "tool_claim", "call").unwrap();
        }
        let t = r
            .cancel_task(&Actor::Owner, &t.id, "Owner cancelled")
            .unwrap();
        assert_eq!(
            t.state,
            if claim {
                TaskState::Blocked
            } else {
                TaskState::Cancelled
            }
        );
        assert_eq!(
            r.action(intent["id"].as_str().unwrap()).unwrap().state,
            if claim { "Unknown" } else { "NotExecuted" }
        );
        assert!(request(&mut r, "turn", "tool_claim", "call").is_err());
        assert!(request(&mut r, "turn", "tool_prepare", "new-call").is_err());
        if claim {
            request(&mut r, "turn", "tool_result", "call").unwrap();
            assert_eq!(
                r.action(intent["id"].as_str().unwrap()).unwrap().state,
                "Unknown"
            );
        }
    }
}
