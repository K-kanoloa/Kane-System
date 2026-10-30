mod support;
use kane_core::*;
use rusqlite::Connection;
use serde_json::{Value, json};
fn resources(cpu: u64) -> HostResources {
    HostResources {
        cpu_millis: cpu,
        memory_bytes: 1024,
        pids: 4,
        storage_bytes: 1024,
    }
}
fn setup(path: &std::path::Path) -> (Registry, String, String) {
    setup_with_parallel(path, 1)
}
fn setup_with_parallel(path: &std::path::Path, parallelism: u32) -> (Registry, String, String) {
    let mut r = Registry::open(path).unwrap();
    r.configure_host_resources(
        &Actor::Owner,
        0,
        HostResourcePolicy {
            capacity: HostResources {
                cpu_millis: 3000,
                memory_bytes: 8192,
                pids: 32,
                storage_bytes: 16384,
            },
            reserved: resources(500),
        },
    )
    .unwrap();
    r.register_model(&Actor::Owner,"local",json!({"cloud":false,"scheduling":{"mode":"gateway_fifo","backend_kind":"local_http"},"budget_profile":{"provider":"fixture","model":"wire-model","upper_bound_tokens":144,"bound_evidence":"128 context + 16 output"}})).unwrap();
    let instance = r
        .register_model_instance(&Actor::Core, "local", "local")
        .unwrap();
    let spec = LocalModelSpec {
        endpoint: "http://127.0.0.1:12000".into(),
        backend_model: "local-model".into(),
        context_tokens: 128,
        parallelism,
        resources: resources(1000),
    };
    let generation = r.load_local_model(&Actor::Owner, &instance, spec).unwrap()["generation"]
        .as_str()
        .unwrap()
        .to_owned();
    r.model_lifecycle(
        &Actor::Core,
        &instance,
        &generation,
        "Ready",
        Some(777),
        "unit-test injected health; not real model evidence",
    )
    .unwrap();
    (r, instance, generation)
}
fn agent(r: &mut Registry, tag: &str, tokens: u64) -> (Task, Agent) {
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, tokens, 1).unwrap();
    let t = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "model request".into(),
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
    let a = r
        .register_agent(&Actor::Owner, "existing-harness", &t.id, "local")
        .unwrap();
    support::launch(r, &a.id, 123);
    r.dispatch_harness(&Actor::Owner, &a.id, tag, tag, "prompt", tag)
        .unwrap();
    r.enable_model_budget(&Actor::Core, tag).unwrap();
    (t, a)
}
fn policy(r: &mut Registry, instance: &str, tasks: &[&Task]) {
    r.configure_model_gateway(&Actor::Owner,"local",0,json!({"instance_id":instance,"max_output_tokens":16,"cache":"disabled","sensitivity":"public","workload_classes":["inference","execution"],"workspaces":tasks.iter().map(|t|&t.spec.workspace_id).collect::<Vec<_>>()})).unwrap();
}
fn queue(r: &mut Registry, tag: &str) -> Value {
    r.reserve_model(&Actor::Core, tag, tag, tag, "fixture", "wire-model")
        .unwrap()
}
fn claim(r: &mut Registry, tag: &str) -> Value {
    r.claim_model(&Actor::Core, tag, tag, tag).unwrap()
}
fn body() -> Value {
    json!({"model":"wire-model","messages":[{"role":"user","content":"hello"}],"stream":true,"max_tokens":16})
}
fn receipt() -> Value {
    json!({"outcome":"completed","usage":{"inputTokens":11,"outputTokens":3,"totalTokens":14}})
}
#[test]
fn model_lifecycle_resources_generation_and_restart_persistence() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("db");
    let (mut r, i, g) = setup(&path);
    assert_eq!(
        r.host_resources(&Actor::Owner).unwrap()["used"]["cpu_millis"],
        1000
    );
    assert!(
        r.model_lifecycle(&Actor::Core, &i, "old-generation", "Failed", None, "late")
            .is_err()
    );
    r.model_lifecycle(&Actor::Core, &i, &g, "Draining", Some(777), "stop")
        .unwrap();
    r.model_lifecycle(&Actor::Core, &i, &g, "Unloaded", None, "absence confirmed")
        .unwrap();
    let used = r.host_resources(&Actor::Owner).unwrap()["used"].clone();
    assert_eq!(used["cpu_millis"], 0);
    assert_eq!(used["storage_bytes"], 1024);
    drop(r);
    let r = Registry::open(path).unwrap();
    assert_eq!(
        r.model_instances(&Actor::Owner).unwrap()[0]["state"],
        "Unloaded"
    );
}
#[test]
fn exact_loopback_only_and_capacity_refuse_without_partial_state() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, _, _) = setup(&dir.path().join("db"));
    let second = r
        .register_model_instance(&Actor::Owner, "local", "local")
        .unwrap();
    for url in [
        "https://cloud.example",
        "http://localhost:12000",
        "http://127.0.0.1:12000/path",
        "http://127.0.0.1:12000@evil",
    ] {
        let spec = LocalModelSpec {
            endpoint: url.into(),
            backend_model: "model".into(),
            context_tokens: 128,
            parallelism: 1,
            resources: resources(1000),
        };
        assert!(r.load_local_model(&Actor::Owner, &second, spec).is_err());
    }
    let spec = LocalModelSpec {
        endpoint: "http://127.0.0.1:12001".into(),
        backend_model: "model".into(),
        context_tokens: 128,
        parallelism: 1,
        resources: resources(2500),
    };
    assert!(matches!(
        r.load_local_model(&Actor::Owner, &second, spec),
        Err(Error::Budget)
    ));
    assert_eq!(
        r.model_instances(&Actor::Owner)
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .find(|x| x["id"] == second)
            .unwrap()["state"],
        "Loading"
    );
    assert_eq!(
        r.host_resources(&Actor::Owner).unwrap()["used"]["cpu_millis"],
        1000
    );
}
#[test]
fn owner_priority_then_fifo_and_authoritative_usage_are_single_use() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (a, _) = agent(&mut r, "a", 1000);
    let (b, _) = agent(&mut r, "b", 1000);
    let (c, _) = agent(&mut r, "c", 1000);
    policy(&mut r, &i, &[&a, &b, &c]);
    let first = queue(&mut r, "a");
    std::thread::sleep(std::time::Duration::from_millis(5));
    let repeated = queue(&mut r, "a");
    for field in ["state", "permit", "call_id", "queue_seq"] {
        assert_eq!(first[field], repeated[field]);
    }
    assert!(
        (1..first["wait_timeout_ms"].as_i64().unwrap())
            .contains(&repeated["wait_timeout_ms"].as_i64().unwrap())
    );
    queue(&mut r, "b");
    queue(&mut r, "c");
    r.set_model_priority(&Actor::Owner, &b.id, 10).unwrap();
    assert!(!claim(&mut r, "a")["permit"].as_bool().unwrap());
    let permit = claim(&mut r, "b");
    let token = permit["gateway_token"].as_str().unwrap();
    let first = r.claim_gateway(&Actor::Core, token, &body()).unwrap();
    assert_eq!(first["model_instance_id"], i);
    assert!(r.claim_gateway(&Actor::Core, token, &body()).is_err());
    assert!(r.settle_model(&Actor::Core, "b", "b", receipt()).is_err());
    let result = r.gateway_result(&Actor::Core, token, receipt()).unwrap();
    assert_eq!(result["budget"]["used_tokens"], 14);
    assert_eq!(
        r.gateway_result(&Actor::Core, token, receipt()).unwrap(),
        result
    );
    assert_eq!(claim(&mut r, "c")["permit"], false);
    assert_eq!(claim(&mut r, "a")["permit"], true);
}
#[test]
fn actual_dispatch_rechecks_identity_scope_parameters_and_cancel_fence() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (t, a) = agent(&mut r, "a", 1000);
    policy(&mut r, &i, &[&t]);
    queue(&mut r, "a");
    let p = claim(&mut r, "a");
    let token = p["gateway_token"].as_str().unwrap();
    assert!(
        r.claim_gateway(&Actor::Agent(a.id), token, &body())
            .is_err()
    );
    let mut bad = body();
    bad["model"] = json!("cloud");
    assert!(r.claim_gateway(&Actor::Core, token, &bad).is_err());
    bad = body();
    bad["max_tokens"] = json!(100000);
    assert!(r.claim_gateway(&Actor::Core, token, &bad).is_err());
    r.cancel_task(&Actor::Core, &t.id, "owner cancel").unwrap();
    assert!(r.claim_gateway(&Actor::Core, token, &body()).is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()["reserved_tokens"],
        144
    );
}
#[test]
fn instance_failure_freezes_route_and_late_receipt_cannot_refund() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, g) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1000);
    let (t2, _) = agent(&mut r, "b", 1000);
    policy(&mut r, &i, &[&t, &t2]);
    queue(&mut r, "a");
    let p = claim(&mut r, "a");
    let token = p["gateway_token"].as_str().unwrap();
    r.claim_gateway(&Actor::Core, token, &body()).unwrap();
    queue(&mut r, "b");
    r.model_lifecycle(
        &Actor::Core,
        &i,
        &g,
        "Failed",
        None,
        "injected real process exit scenario (unit)",
    )
    .unwrap();
    assert_eq!(r.task(&t.id).unwrap().state, TaskState::Blocked);
    let result = r.gateway_result(&Actor::Core, token, receipt()).unwrap();
    assert_eq!(result["state"], "Unknown");
    assert_eq!(result["budget"]["reserved_tokens"], 144);
    assert!(r.claim_model(&Actor::Core, "b", "b", "b").is_err());
}
#[test]
fn ledger_fault_rolls_back_queue_permit_and_budget_atomically() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let (mut r, i, _) = setup(&path);
    let (t, _) = agent(&mut r, "a", 1000);
    policy(&mut r, &i, &[&t]);
    queue(&mut r, "a");
    let db = Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER phase2_fault BEFORE INSERT ON ledger WHEN NEW.kind='model_gateway_prepared' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(r.claim_model(&Actor::Core, "a", "a", "a").is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    assert_eq!(r.model_queue(&Actor::Owner).unwrap()[0]["state"], "Queued");
    db.execute_batch("DROP TRIGGER phase2_fault").unwrap();
    assert_eq!(claim(&mut r, "a")["permit"], true);
}
#[test]
fn token_quota_denial_never_prepares_gateway_or_calls_provider() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1);
    policy(&mut r, &i, &[&t]);
    queue(&mut r, "a");
    assert_eq!(claim(&mut r, "a")["state"], "Denied");
    assert_eq!(
        r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()["used_tokens"],
        0
    );
}
#[test]
fn revoked_workspace_and_private_instance_policy_refuse_at_real_claim() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1000);
    let (t2, _) = agent(&mut r, "b", 1000);
    policy(&mut r, &i, &[&t]);
    queue(&mut r, "a");
    let p = claim(&mut r, "a");
    r.configure_model_gateway(&Actor::Owner,"local",1,json!({"instance_id":i,"max_output_tokens":16,"cache":"disabled","sensitivity":"public","workload_classes":["inference","execution"],"workspaces":[t2.spec.workspace_id]})).unwrap();
    assert!(
        r.claim_gateway(&Actor::Core, p["gateway_token"].as_str().unwrap(), &body())
            .is_err()
    );
    assert!(
        r.configure_model_gateway(&Actor::Agent("x".into()), "local", 2, json!({}))
            .is_err()
    );
}

#[test]
fn nonmatching_workload_and_sensitive_cross_task_access_are_refused() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1000);
    let (t2, _) = agent(&mut r, "b", 1000);
    r.configure_model_gateway(&Actor::Owner,"local",0,json!({"instance_id":i,"max_output_tokens":16,"cache":"disabled","sensitivity":"confidential","exclusive_task":t.id,"workload_classes":["inference"],"workspaces":[t.spec.workspace_id,t2.spec.workspace_id]})).unwrap();
    queue(&mut r, "b");
    assert!(r.claim_model(&Actor::Core, "b", "b", "b").is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &t2.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    r.configure_model_gateway(&Actor::Owner,"local",1,json!({"instance_id":i,"max_output_tokens":16,"cache":"disabled","sensitivity":"public","workload_classes":["generation"],"workspaces":[t.spec.workspace_id]})).unwrap();
    queue(&mut r, "a");
    r.set_model_priority(&Actor::Owner, &t.id, 100).unwrap();
    assert!(matches!(
        r.claim_model(&Actor::Core, "a", "a", "a"),
        Err(Error::Unsupported(_))
    ));
    assert_eq!(
        r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
}

#[test]
fn private_residency_cannot_be_reused_by_other_task_after_public_policy_change() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, _) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1000);
    let (t2, _) = agent(&mut r, "b", 1000);
    let private = json!({"instance_id":i,"max_output_tokens":16,"cache":"disabled","sensitivity":"confidential","exclusive_task":t.id,"workload_classes":["inference"],"workspaces":[t.spec.workspace_id,t2.spec.workspace_id]});
    r.configure_model_gateway(&Actor::Owner, "local", 0, private.clone())
        .unwrap();
    queue(&mut r, "a");
    let a = claim(&mut r, "a");
    let token = a["gateway_token"].as_str().unwrap();
    r.claim_gateway(&Actor::Core, token, &body()).unwrap();
    r.gateway_result(&Actor::Core, token, receipt()).unwrap();
    let mut public = private;
    public["sensitivity"] = json!("public");
    r.configure_model_gateway(&Actor::Owner, "local", 1, public)
        .unwrap();
    queue(&mut r, "b");
    assert!(r.claim_model(&Actor::Core, "b", "b", "b").is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &t2.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
}

#[test]
fn shared_parallel_slots_are_bounded_and_unknown_stops_further_dispatches() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let (mut r, i, _) = setup_with_parallel(&path, 2);
    let (a, _) = agent(&mut r, "a", 1000);
    let (b, _) = agent(&mut r, "b", 1000);
    let (c, _) = agent(&mut r, "c", 1000);
    policy(&mut r, &i, &[&a, &b, &c]);
    for tag in ["a", "b", "c"] {
        queue(&mut r, tag);
    }
    assert_eq!(claim(&mut r, "a")["permit"], true);
    assert_eq!(claim(&mut r, "b")["permit"], true);
    assert_eq!(claim(&mut r, "c")["permit"], false);
    r.cancel_model(&Actor::Core, "b", "b", "b").unwrap();
    let p = r.model_queue(&Actor::Owner).unwrap();
    assert_eq!(p[1]["state"], "Unknown");
    assert_eq!(claim(&mut r, "c")["permit"], false);
    assert_eq!(
        r.host_resources(&Actor::Owner).unwrap()["used"]["cpu_millis"],
        1000
    );
}

#[test]
fn model_metadata_survives_restart_and_is_not_an_agent_authority_endpoint() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = Registry::open(&path).unwrap();
    let spec = json!({"version":"test-v2","capabilities":["text-inference"],"source":{"sha256":"public-fixture"},"cloud":false,"pricing":{"status":"UNKNOWN"}});
    r.register_model(&Actor::Owner, "portable-model", spec.clone())
        .unwrap();
    drop(r);
    let r = Registry::open(path).unwrap();
    let records = r.models(&Actor::Owner).unwrap();
    assert_eq!(records[0]["spec"], spec);
    assert!(r.models(&Actor::Agent("untrusted".into())).is_err());
}
#[test]
fn failed_model_state_event_transaction_cannot_partially_drain_or_release() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let (mut r, i, g) = setup(&path);
    let db = Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER model_state_fault BEFORE INSERT ON ledger WHEN NEW.kind='model_lifecycle' BEGIN SELECT RAISE(ABORT,'injected model state ledger fault'); END;").unwrap();
    assert!(
        r.model_lifecycle(&Actor::Core, &i, &g, "Draining", Some(777), "injected")
            .is_err()
    );
    assert_eq!(
        r.model_instances(&Actor::Owner).unwrap()[0]["state"],
        "Ready"
    );
    assert_eq!(
        r.host_resources(&Actor::Owner).unwrap()["used"]["cpu_millis"],
        1000
    );
}

#[test]
fn agent_cannot_register_or_read_trusted_model_broker_listener_identity() {
    let d = tempfile::tempdir().unwrap();
    let mut r = Registry::open(d.path().join("db")).unwrap();
    assert!(
        r.register_model_listener(
            &Actor::Agent("untrusted".into()),
            "/private/fake.sock",
            123,
            1,
            2
        )
        .is_err()
    );
    assert!(
        r.model_listeners(&Actor::Agent("untrusted".into()))
            .is_err()
    );
    assert_eq!(r.model_listeners(&Actor::Owner).unwrap(), json!([]));
}

#[test]
fn kernel_proof_requires_authority_generation_and_actual_reservation_values() {
    let d = tempfile::tempdir().unwrap();
    let (mut r, i, g) = setup(&d.path().join("db"));
    let (t, _) = agent(&mut r, "a", 1000);
    let p = json!({"instance_id":i,"max_output_tokens":16,"cache":"disabled","sensitivity":"public","workload_classes":["inference"],"workspaces":[t.spec.workspace_id],"requires_kernel_limits":true});
    r.configure_model_gateway(&Actor::Owner, "local", 0, p)
        .unwrap();
    queue(&mut r, "a");
    assert!(r.claim_model(&Actor::Core, "a", "a", "a").is_err());
    assert_eq!(
        r.model_budget(&Actor::Owner, &t.spec.budget_id).unwrap()["reserved_tokens"],
        0
    );
    assert!(
        r.record_model_limits(&Actor::Agent("a".into()), &i, &g, json!({}))
            .is_err()
    );
    assert!(
        r.record_model_limits(&Actor::Core, &i, "stale", json!({}))
            .is_err()
    );
    r.record_model_limits(
        &Actor::Core,
        &i,
        &g,
        json!({"phase":"healthy_ready","mode":"diagnostic_unmanaged","kernel_enforced":false}),
    )
    .unwrap();
    assert!(r.claim_model(&Actor::Core, "a", "a", "a").is_err());
    r.record_model_limits(&Actor::Core,&i,&g,json!({"phase":"healthy_ready","mode":"cgroup_v2","kernel_enforced":true,"cpu.max":"100000 100000","memory.max":"1024","pids.max":"4"})).unwrap();
    assert_eq!(
        r.claim_model(&Actor::Core, "a", "a", "a").unwrap()["permit"],
        true
    );
}

#[test]
fn restart_and_cancel_invalidate_gateway_permits_without_refunding_unknown_calls() {
    for mode in ["restart", "cancel"] {
        for claimed in [false, true] {
            let d = tempfile::tempdir().unwrap();
            let path = d.path().join("db");
            let (mut r, instance, generation) = setup(&path);
            let (task, a) = agent(&mut r, "lost", 1000);
            policy(&mut r, &instance, &[&task]);
            queue(&mut r, "lost");
            let permit = claim(&mut r, "lost");
            let token = permit["gateway_token"].as_str().unwrap();
            if claimed {
                r.claim_gateway(&Actor::Core, token, &body()).unwrap();
            }
            if mode == "restart" {
                r.recover_model_queue(&Actor::Core).unwrap();
            } else {
                r.cancel_task(&Actor::Owner, &task.id, "Owner cancellation")
                    .unwrap();
            }
            let db = Connection::open(&path).unwrap();
            let state: String = db
                .query_row(
                    "SELECT state FROM model_gateway_call WHERE call_id='lost'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(state, "Unknown");
            assert!(r.claim_gateway(&Actor::Core, token, &body()).is_err());
            assert_eq!(
                r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
                144
            );
            // Physical residency release is separate from resolving provider usage.
            r.model_lifecycle(
                &Actor::Core,
                &instance,
                &generation,
                "Failed",
                None,
                "confirmed fixture process absence",
            )
            .unwrap();
            r.model_lifecycle(
                &Actor::Core,
                &instance,
                &generation,
                "Unloaded",
                None,
                "absence after reboot/reconcile",
            )
            .unwrap();
            assert_eq!(
                r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
                144
            );
            let hash: String = db
                .query_row(
                    "SELECT COALESCE(request_hash,'') FROM model_gateway_call WHERE call_id='lost'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            let outcome = serde_json::json!({"call_id":"lost","outcome":"Confirmed","generation":generation,"request_hash":hash,"usage":{"inputTokens":11,"outputTokens":3,"totalTokens":14}});
            let proof = serde_json::json!({"source_ref":"unit-test independently checked provider receipt, not real model evidence","sha256":"b".repeat(64)});
            r.reconcile_model_call(&Actor::Owner, "lost", outcome.clone(), proof.clone())
                .unwrap();
            r.reconcile_model_call(&Actor::Owner, "lost", outcome, proof)
                .unwrap();
            assert_eq!(
                r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["used_tokens"],
                14
            );
            assert_eq!(
                r.model_budget(&Actor::Owner, &a.budget_id).unwrap()["reserved_tokens"],
                0
            );
            assert_eq!(r.task(&task.id).unwrap().state, TaskState::Blocked);
            assert!(r.claim_gateway(&Actor::Core, token, &body()).is_err());
        }
    }
}

#[test]
fn owner_explicit_model_deadline_is_bounded_and_default_stays_45() {
    let dir = tempfile::tempdir().unwrap();
    let (mut r, instance, _) = setup(&dir.path().join("db"));
    let (task, a) = agent(&mut r, "deadline", 1000);
    policy(&mut r, &instance, &[&task]);
    queue(&mut r, "deadline");
    let p = claim(&mut r, "deadline");
    let permit = r
        .claim_gateway(&Actor::Core, p["gateway_token"].as_str().unwrap(), &body())
        .unwrap();
    assert_eq!(permit["timeout_seconds"], 45);
    let base = json!({"instance_id":instance,"max_output_tokens":16,"cache":"disabled","sensitivity":"public","workload_classes":["inference","execution"],"workspaces":[task.spec.workspace_id],"timeout_seconds":180});
    assert!(
        r.configure_model_gateway(&Actor::Agent(a.id), "local", 1, base.clone())
            .is_err()
    );
    for invalid in [json!(0), json!(181), json!(-1), json!(1.5), json!("180")] {
        let mut bad = base.clone();
        bad["timeout_seconds"] = invalid;
        assert!(
            r.configure_model_gateway(&Actor::Owner, "local", 1, bad)
                .is_err()
        );
    }
    let row = r
        .configure_model_gateway(&Actor::Owner, "local", 1, base)
        .unwrap();
    assert_eq!(row["policy"]["timeout_seconds"], 180);
    // Changing Owner policy never settles or replays the already dispatched call.
    assert_eq!(
        r.model_budget(&Actor::Core, &task.spec.budget_id).unwrap()["reserved_tokens"],
        144
    );
}
