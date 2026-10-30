mod support;
use kane_core::*;
use serde_json::{Value, json};

fn fixture(path: &std::path::Path) -> (Registry, Task, Agent) {
    let mut r = Registry::open(path).unwrap();
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 100, 1).unwrap();
    r.register_model(&Actor::Owner,"model",json!({"cloud":false,"budget_profile":{"provider":"test","model":"bounded","upper_bound_tokens":32,"bound_evidence":"deterministic bound"}})).unwrap();
    let t = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "recover existing workload".into(),
                acceptance: vec!["independent content verification".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 1048576,
                    max_storage_bytes: 1048576,
                    checkpoint_verified: false,
                },
                budget_id: budget,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let a = r
        .register_agent(&Actor::Owner, "original", &t.id, "model")
        .unwrap();
    support::launch(&mut r, &a.id, 123);
    r.dispatch_harness(
        &Actor::Owner,
        &a.id,
        "turn",
        "original-message",
        "old prompt",
        "native",
    )
    .unwrap();
    r.enable_model_budget(&Actor::Core, "turn").unwrap();
    (r, t, a)
}
fn reserve(r: &mut Registry) {
    r.reserve_model(&Actor::Core, "turn", "call", "native", "test", "bounded")
        .unwrap();
}
fn proof() -> Value {
    json!({"source_ref":"independently inspected test evidence","sha256":"a".repeat(64),"post_checkpoint_effects_checked":true,"checkpoint_id":"cp"})
}
fn checkpoint(r: &mut Registry) -> Value {
    let m = json!({"id":"cp","bindings":r.checkpoint_bindings(&Actor::Owner).unwrap(),"roots":[]});
    r.record_checkpoint(&Actor::Owner, &m).unwrap();
    m
}
fn lost(r: &mut Registry, a: &Agent) {
    support::emit(r, &a.id, json!({"event":"process_exited","success":false})).unwrap();
}
#[test]
fn unknown_remains_reserved_until_explicit_owner_evidence_and_duplicate_cannot_charge_twice() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    reserve(&mut r);
    lost(&mut r, &a);
    assert_eq!(
        r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
        32
    );
    let result = json!({"call_id":"call","outcome":"Confirmed","usage":{"inputTokens":10,"outputTokens":5,"totalTokens":15}});
    assert!(
        r.reconcile_model_call(&Actor::Agent(a.id.clone()), "call", result.clone(), proof())
            .is_err()
    );
    assert!(
        r.reconcile_model_call(&Actor::Owner, "call", result.clone(), json!({}))
            .is_err()
    );
    r.reconcile_model_call(&Actor::Owner, "call", result.clone(), proof())
        .unwrap();
    r.reconcile_model_call(&Actor::Owner, "call", result, proof())
        .unwrap();
    let b = r.model_budget(&Actor::Owner, &a.budget_id).unwrap();
    assert_eq!(b["reserved_tokens"], 0);
    assert_eq!(b["used_tokens"], 15);
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
    assert!(
        r.recovery_queue(&Actor::Owner).unwrap()["models"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
#[test]
fn reconciliation_ledger_failure_rolls_back_reserved_amount_and_resolution() {
    let d = tempfile::tempdir().unwrap();
    let db = d.path().join("db");
    let (mut r, _, a) = fixture(&db);
    reserve(&mut r);
    lost(&mut r, &a);
    let other = rusqlite::Connection::open(&db).unwrap();
    other.execute_batch("CREATE TRIGGER reject_reconcile BEFORE INSERT ON ledger WHEN NEW.kind='model_owner_reconciliation' BEGIN SELECT RAISE(ABORT,'injected ledger fault'); END;").unwrap();
    let response = r.reconcile_model_call(
        &Actor::Owner,
        "call",
        json!({"call_id":"call","outcome":"NotExecuted"}),
        proof(),
    );
    assert!(response.is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
        32
    );
    assert_eq!(
        r.recovery_queue(&Actor::Owner).unwrap()["models"][0]["state"],
        "Unknown"
    );
    other
        .execute_batch("DROP TRIGGER reject_reconcile")
        .unwrap();
    r.reconcile_model_call(
        &Actor::Owner,
        "call",
        json!({"call_id":"call","outcome":"NotExecuted"}),
        proof(),
    )
    .unwrap();
    let b = r.model_budget(&Actor::Owner, &a.budget_id).unwrap();
    assert_eq!(b["used_tokens"], 0);
    assert_eq!(b["reserved_tokens"], 0);
}
#[test]
fn checkpoint_requires_quiescence_and_restore_does_not_erase_unknown_holds() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    reserve(&mut r);
    assert!(r.checkpoint_bindings(&Actor::Owner).is_err());
    lost(&mut r, &a);
    let m = checkpoint(&mut r);
    r.quarantine_restore(&Actor::Owner, &m).unwrap();
    let state = r.task(&t.id).unwrap();
    assert_eq!(state.block_reason.as_deref(), Some("restore-review"));
    assert!(
        r.review_restored_task(&Actor::Owner, &t.id, state.version, proof())
            .is_err()
    );
    assert_eq!(
        r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
        32
    );
    r.quarantine_restore(&Actor::Owner, &m).unwrap();
    assert_eq!(r.task(&t.id).unwrap().version, state.version);
}
#[test]
fn explicit_new_continuation_preserves_context_but_cannot_repeat_prior_message_or_old_session() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    lost(&mut r, &a);
    let m = checkpoint(&mut r);
    r.quarantine_restore(&Actor::Owner, &m).unwrap();
    let state = r.task(&t.id).unwrap();
    assert!(
        r.transition_task(
            &Actor::Owner,
            &t.id,
            state.version,
            TaskState::Running,
            None
        )
        .is_err()
    );
    let mut p = proof();
    p["continuation"] = json!({"turn_id":"turn","message_id":"new-message","content":"explicit new step","native_session_ref":"native"});
    assert!(
        r.review_restored_task(&Actor::Core, &t.id, state.version, p.clone())
            .is_err()
    );
    r.review_restored_task(&Actor::Owner, &t.id, state.version, p)
        .unwrap();
    support::launch(&mut r, &a.id, 456);
    assert!(
        r.dispatch_harness(
            &Actor::Owner,
            &a.id,
            "turn",
            "original-message",
            "old prompt",
            "native"
        )
        .is_err()
    );
    assert!(
        r.dispatch_harness(
            &Actor::Owner,
            &a.id,
            "turn",
            "new-message",
            "wrong prompt",
            "native"
        )
        .is_err()
    );
    r.dispatch_harness(
        &Actor::Owner,
        &a.id,
        "turn",
        "new-message",
        "explicit new step",
        "native",
    )
    .unwrap();
    assert!(
        r.dispatch_harness(
            &Actor::Owner,
            &a.id,
            "turn",
            "new-message",
            "explicit new step",
            "native"
        )
        .is_err()
    );
    assert!(
        r.check_recovery_observation("turn", Some("old-session"))
            .is_err()
    );
    let current = r.current_runtime_session(&a.id).unwrap();
    r.check_recovery_observation("turn", Some(&current))
        .unwrap();
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "prompt_dispatch")
            .count(),
        2
    );
    let now = r.agent(&a.id).unwrap();
    assert_eq!(
        (now.task_id, now.workspace_id, now.budget_id),
        (a.task_id, a.workspace_id, a.budget_id)
    );
}
#[test]
fn snapshot_artifact_requires_owner_review_then_independent_verification() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    // The unit fixture uses direct Artifact API; real DSH production is separately integrated.
    let artifact = r
        .submit_artifact(
            &Actor::Agent(a.id.clone()),
            &t.id,
            "text/plain",
            true,
            "independent bytes",
        )
        .unwrap();
    let task = r.task(&t.id).unwrap();
    r.transition_task(
        &Actor::Core,
        &t.id,
        task.version,
        TaskState::Verifying,
        None,
    )
    .unwrap();
    lost(&mut r, &a);
    let m = checkpoint(&mut r);
    r.quarantine_restore(&Actor::Owner, &m).unwrap();
    let task = r.task(&t.id).unwrap();
    assert!(
        r.verify(
            &Actor::Owner,
            &t.id,
            task.version,
            true,
            "unchecked restore"
        )
        .is_err()
    );
    let mut bad = proof();
    bad["post_checkpoint_effects_checked"] = json!(false);
    assert!(
        r.review_restored_task(&Actor::Owner, &t.id, task.version, bad)
            .is_err()
    );
    let reviewed = r
        .review_restored_task(&Actor::Owner, &t.id, task.version, proof())
        .unwrap();
    assert_eq!(reviewed.state, TaskState::Verifying);
    // This pure Core fixture has no in-flight model call. The independent byte
    // oracle, after Owner recovery review, can now verify the existing Artifact.
    assert_eq!(r.artifacts(&t.id).unwrap()[0].content, "independent bytes");
    let done = r
        .verify(
            &Actor::Verifier("independent-fixed-bytes".into()),
            &t.id,
            reviewed.version,
            true,
            "exact independent bytes",
        )
        .unwrap();
    assert_eq!(done.state, TaskState::Done);
    assert_eq!(r.artifacts(&t.id).unwrap()[0].id, artifact.id);
}
#[test]
fn checkpoint_hash_and_ledger_cut_are_bound_and_cancellation_survives_restore() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    lost(&mut r, &a);
    let mut m =
        json!({"id":"cp","bindings":r.checkpoint_bindings(&Actor::Owner).unwrap(),"roots":[]});
    r.cancel_task(&Actor::Owner, &t.id, "explicit Owner cancellation")
        .unwrap();
    assert!(r.record_checkpoint(&Actor::Owner, &m).is_err());
    m["bindings"] = r.checkpoint_bindings(&Actor::Owner).unwrap();
    r.record_checkpoint(&Actor::Owner, &m).unwrap();
    let mut altered = m.clone();
    altered["roots"] = json!(["different files"]);
    assert!(r.validate_checkpoint(&Actor::Owner, &altered).is_err());
    r.quarantine_restore(&Actor::Owner, &m).unwrap();
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Cancelled);
}

#[test]
fn new_boot_invalidates_old_process_sessions_without_resetting_identity_or_restart_budget() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, t, a) = fixture(&d.path().join("db"));
    r.recover_host_boot(&Actor::Core, "first-kernel-boot")
        .unwrap();
    reserve(&mut r);
    let old = r.runtime_status(&a.id).unwrap();
    let session = r.current_runtime_session(&a.id).unwrap();
    r.recover_host_boot(&Actor::Core, "second-kernel-boot")
        .unwrap();
    assert_eq!(r.agent(&a.id).unwrap().state, AgentState::Ready);
    assert!(r.current_runtime_session(&a.id).is_err());
    let after = r.runtime_status(&a.id).unwrap();
    assert_eq!(after["control"]["attempt"], old["control"]["attempt"]);
    assert!(after["control"]["owner_token"].is_null());
    assert_eq!(
        r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
        32
    );
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
    assert_ne!(session, "");
    let events = r.events().unwrap().len();
    r.recover_host_boot(&Actor::Core, "second-kernel-boot")
        .unwrap();
    assert_eq!(r.events().unwrap().len(), events);
}
