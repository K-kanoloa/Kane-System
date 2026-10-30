mod support;
use kane_core::*;
use rusqlite::Connection;
use serde_json::{Value, json};
fn pair(r: &mut Registry, label: &str) -> Value {
    let code = r
        .control_auth(&Actor::Owner, "pair_code", json!({}))
        .unwrap();
    r.control_auth(
        &Actor::Owner,
        "pair",
        json!({"pairing_code":code["pairing_code"],"label":label}),
    )
    .unwrap()
}
fn setup(path: &std::path::Path) -> Registry {
    let mut r = Registry::open(path).unwrap();
    r.control_auth(
        &Actor::Owner,
        "init",
        json!({"origin":"https://localhost:8443"}),
    )
    .unwrap();
    r
}
#[test]
fn pairing_is_one_use_devices_revoke_sessions_and_pending_pairings() {
    let d = tempfile::tempdir().unwrap();
    let mut r = setup(&d.path().join("db"));
    let a = pair(&mut r, "Owner laptop");
    let b = pair(&mut r, "Owner phone");
    let t = a["session_token"].as_str().unwrap();
    let code = r
        .control_auth(&Actor::Owner, "pair_code", json!({"session_token":t}))
        .unwrap();
    r.control_auth(
        &Actor::Owner,
        "revoke_device",
        json!({"session_token":t,"device_id":b["device_id"]}),
    )
    .unwrap();
    assert!(
        r.control_auth(
            &Actor::Owner,
            "session",
            json!({"session_token":b["session_token"]})
        )
        .is_err()
    );
    assert!(
        r.control_auth(
            &Actor::Owner,
            "pair",
            json!({"pairing_code":code["pairing_code"],"label":"stale delegated code"})
        )
        .is_err()
    );
    let once = r
        .control_auth(&Actor::Owner, "pair_code", json!({}))
        .unwrap();
    let request = json!({"pairing_code":once["pairing_code"],"label":"approved"});
    r.control_auth(&Actor::Owner, "pair", request.clone())
        .unwrap();
    assert!(r.control_auth(&Actor::Owner, "pair", request).is_err());
    assert!(
        r.control_auth(
            &Actor::Agent("fake".into()),
            "session",
            json!({"session_token":t})
        )
        .is_err()
    );
}
#[test]
fn expiry_and_passkey_challenge_consumption_are_authoritative_and_rollback_safe() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = setup(&path);
    let a = pair(&mut r, "paired");
    let t = a["session_token"].as_str().unwrap();
    let c = r
        .control_auth(&Actor::Owner, "begin_register", json!({"session_token":t}))
        .unwrap();
    // Cryptography is verified separately through actual browser/SDK integration;
    // this unit deliberately tests only private Host metadata transitions.
    let finish = json!({"session_token":t,"challenge_id":c["id"],"challenge":c["challenge"],"credential_id":"fixture-public-credential","key":{"public_key":"unit-metadata-only"},"sign_count":0});
    let db = Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER reject_auth BEFORE INSERT ON ledger WHEN NEW.kind='passkey_registered' BEGIN SELECT RAISE(ABORT,'injected ledger fault'); END;").unwrap();
    assert!(
        r.control_auth(&Actor::Owner, "finish_register", finish.clone())
            .is_err()
    );
    assert_eq!(
        r.control_challenge(&Actor::Owner, c["id"].as_str().unwrap())
            .unwrap()["used"],
        false
    );
    db.execute_batch("DROP TRIGGER reject_auth").unwrap();
    r.control_auth(&Actor::Owner, "finish_register", finish.clone())
        .unwrap();
    assert!(
        r.control_auth(&Actor::Owner, "finish_register", finish)
            .is_err()
    );
    db.execute("UPDATE control_session SET expires_at=0", [])
        .unwrap();
    assert!(
        r.control_auth(&Actor::Owner, "session", json!({"session_token":t}))
            .is_err()
    );
    assert!(
        r.control_auth(
            &Actor::Owner,
            "begin_login",
            json!({"device_token":"forged"})
        )
        .is_err()
    );
}
fn workspace_fixture(
    path: &std::path::Path,
    root: &std::path::Path,
) -> (Registry, Task, Agent, Value) {
    let mut r = setup(path);
    let session = pair(&mut r, "browser");
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 10000, 1).unwrap();
    r.register_model(&Actor::Owner, "mock", json!({"cloud":false}))
        .unwrap();
    let t = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "document".into(),
                acceptance: vec!["independent".into()],
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
        .register_agent(&Actor::Owner, "replaceable", &t.id, "mock")
        .unwrap();
    r.configure_runtime(
        &Actor::Core,
        &a.id,
        json!({"cwd":root,"max_restarts":10,"sandbox":{"native_file_proxy":true}}),
    )
    .unwrap();
    support::launch(&mut r, &a.id, 123);
    r.dispatch_harness(
        &Actor::Owner,
        &a.id,
        "turn",
        "original",
        "read document",
        "native",
    )
    .unwrap();
    (r, t, a, session)
}
fn paused(r: &mut Registry, a: &Agent) {
    support::emit(r, &a.id, json!({"event":"stopped"})).unwrap();
}
#[test]
fn native_edit_is_fenced_by_physical_state_epoch_hash_session_and_idempotency() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("workspace");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("input.txt"), "ORIGINAL\n").unwrap();
    let (mut r, t, a, s) = workspace_fixture(&d.path().join("db"), &root);
    let token = s["session_token"].as_str().unwrap();
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, false)
        .unwrap();
    assert!(
        r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, true)
            .is_err()
    );
    paused(&mut r, &a);
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, true)
        .unwrap();
    let read = r
        .control_read(&Actor::Owner, token, &t.id, "input.txt")
        .unwrap();
    let edit = json!({"request_id":"save1","path":"input.txt","base_hash":read["hash"],"epoch":1,"version":0,"content":"HUMAN EDIT\n"});
    let result = r
        .control_edit(&Actor::Owner, token, &t.id, edit.clone())
        .unwrap();
    assert_eq!(result["state"], "Committed");
    r.control_edit(&Actor::Owner, token, &t.id, edit.clone())
        .unwrap();
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 1);
    assert_eq!(
        std::fs::read_to_string(root.join("input.txt")).unwrap(),
        "HUMAN EDIT\n"
    );
    let mut old = edit.clone();
    old["request_id"] = json!("old-epoch");
    assert!(r.control_edit(&Actor::Owner, token, &t.id, old).is_err());
    let mut changed = edit;
    changed["content"] = json!("different retry");
    assert!(
        r.control_edit(&Actor::Owner, token, &t.id, changed)
            .is_err()
    );
    let b = pair(&mut r, "other");
    assert!(
        r.control_takeover(
            &Actor::Owner,
            b["session_token"].as_str().unwrap(),
            &t.id,
            1,
            1,
            false
        )
        .is_err()
    );
    assert!(
        r.control_read(&Actor::Owner, token, &t.id, "../db")
            .is_err()
    );
}
#[test]
fn filesystem_publish_and_failed_database_result_remain_unknown_until_actual_file_reconciliation() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("workspace");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("input.txt"), "BEFORE\n").unwrap();
    let path = d.path().join("db");
    let (mut r, t, a, s) = workspace_fixture(&path, &root);
    let token = s["session_token"].as_str().unwrap();
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, false)
        .unwrap();
    paused(&mut r, &a);
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, true)
        .unwrap();
    let read = r
        .control_read(&Actor::Owner, token, &t.id, "input.txt")
        .unwrap();
    let db = Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER reject_result BEFORE INSERT ON ledger WHEN NEW.kind='human_edit' BEGIN SELECT RAISE(ABORT,'injected after filesystem publication'); END;").unwrap();
    let edit = json!({"request_id":"lost-result","path":"input.txt","content":"PUBLISHED\n","base_hash":read["hash"],"epoch":1,"version":0});
    assert!(
        r.control_edit(&Actor::Owner, token, &t.id, edit.clone())
            .is_err()
    );
    assert_eq!(
        std::fs::read_to_string(root.join("input.txt")).unwrap(),
        "PUBLISHED\n"
    );
    assert_eq!(
        r.control_edit_status(&Actor::Owner, token, "lost-result")
            .unwrap()["state"],
        "Unknown"
    );
    assert!(r.control_edit(&Actor::Owner, token, &t.id, edit).is_err());
    db.execute_batch("DROP TRIGGER reject_result").unwrap();
    r.control_reconcile_edit(&Actor::Owner, token, "lost-result")
        .unwrap();
    r.control_reconcile_edit(&Actor::Owner, token, "lost-result")
        .unwrap();
    assert_eq!(r.workspace(&a.workspace_id).unwrap().version, 1);
}
#[test]
fn snapshot_restore_cannot_reactivate_revoked_web_devices() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = setup(&path);
    let s = pair(&mut r, "later revoked");
    let manifest =
        json!({"id":"cp-auth","bindings":r.checkpoint_bindings(&Actor::Owner).unwrap(),"roots":[]});
    r.record_checkpoint(&Actor::Owner, &manifest).unwrap();
    let backup = d.path().join("snapshot");
    r.backup(&Actor::Owner, &backup).unwrap();
    let target = d.path().join("restored");
    Registry::restore_new(&Actor::Owner, &backup, &target).unwrap();
    let mut restored = Registry::open(target).unwrap();
    restored
        .quarantine_restore(&Actor::Owner, &manifest)
        .unwrap();
    assert!(
        restored
            .control_auth(
                &Actor::Owner,
                "session",
                json!({"session_token":s["session_token"]})
            )
            .is_err()
    );
    assert!(
        restored
            .control_auth(
                &Actor::Owner,
                "begin_login",
                json!({"device_token":s["device_token"]})
            )
            .is_err()
    );
    assert!(
        restored
            .control_auth(&Actor::Owner, "pair_code", json!({}))
            .is_ok()
    );
}
#[test]
fn web_prompt_grant_rechecks_revocation_inside_authoritative_dispatch() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, task, agent) = support::fixture(&d.path().join("db"), 3);
    r.control_auth(
        &Actor::Owner,
        "init",
        json!({"origin":"https://localhost:8443"}),
    )
    .unwrap();
    let first = pair(&mut r, "first local browser");
    let token = first["session_token"].as_str().unwrap();
    support::launch(&mut r, &agent.id, 123);
    r.control_dispatch_authorize(&Actor::Owner, token, &task.id, "original prompt")
        .unwrap();
    r.control_auth(&Actor::Owner, "logout", json!({"session_token":token}))
        .unwrap();
    assert!(
        r.dispatch_harness(
            &Actor::Core,
            &agent.id,
            "turn",
            &format!("system-prompt-{}", task.id),
            "original prompt",
            "native"
        )
        .is_err()
    );
    assert_eq!(r.task(&task.id).unwrap().state, TaskState::Pending);
    let second = pair(&mut r, "fresh authorized browser");
    r.control_dispatch_authorize(
        &Actor::Owner,
        second["session_token"].as_str().unwrap(),
        &task.id,
        "explicit new prompt",
    )
    .unwrap();
    assert!(
        r.dispatch_harness(
            &Actor::Core,
            &agent.id,
            "turn",
            &format!("system-prompt-{}", task.id),
            "forged prompt",
            "native"
        )
        .is_err()
    );
    r.dispatch_harness(
        &Actor::Core,
        &agent.id,
        "turn",
        &format!("system-prompt-{}", task.id),
        "explicit new prompt",
        "native",
    )
    .unwrap();
    assert_eq!(r.task(&task.id).unwrap().state, TaskState::Running);
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "prompt_dispatch")
            .count(),
        1
    );
}

#[test]
fn expired_same_owner_must_explicitly_reacquire_a_new_fenced_epoch() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("workspace");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("input.txt"), "ORIGINAL\n").unwrap();
    let path = d.path().join("db");
    let (mut r, t, a, s) = workspace_fixture(&path, &root);
    let token = s["session_token"].as_str().unwrap();
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, false)
        .unwrap();
    paused(&mut r, &a);
    r.control_takeover(&Actor::Owner, token, &t.id, 0, 0, true)
        .unwrap();
    let old = r.workspace(&a.workspace_id).unwrap();
    Connection::open(&path)
        .unwrap()
        .execute(
            "UPDATE workspace SET expires_at=0 WHERE id=?1",
            [&a.workspace_id],
        )
        .unwrap();
    let read = r
        .control_read(&Actor::Owner, token, &t.id, "input.txt")
        .unwrap();
    let edit = json!({"request_id":"expired-no-write","path":"input.txt","base_hash":read["hash"],"epoch":old.lease_epoch,"version":old.version,"content":"EXPLICIT RECOVERY\n"});
    assert!(
        r.control_edit(&Actor::Owner, token, &t.id, edit.clone())
            .is_err()
    );
    let pending = r
        .control_takeover(
            &Actor::Owner,
            token,
            &t.id,
            old.lease_epoch,
            old.version,
            false,
        )
        .unwrap();
    assert_eq!(pending["phase"], "TakingOver");
    r.control_takeover(
        &Actor::Owner,
        token,
        &t.id,
        old.lease_epoch,
        old.version,
        true,
    )
    .unwrap();
    let new = r.workspace(&a.workspace_id).unwrap();
    assert!(new.lease_epoch > old.lease_epoch);
    assert!(
        r.control_edit(&Actor::Owner, token, &t.id, edit.clone())
            .is_err()
    );
    let mut fresh = edit;
    fresh["epoch"] = json!(new.lease_epoch);
    r.control_edit(&Actor::Owner, token, &t.id, fresh).unwrap();
    assert_eq!(
        std::fs::read_to_string(root.join("input.txt")).unwrap(),
        "EXPLICIT RECOVERY\n"
    );
    assert_eq!(r.agent(&a.id).unwrap().task_id, a.task_id);
}
