mod support;
use kane_core::*;
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Barrier},
    thread,
};
const M: u64 = 1024 * 1024;
fn agent(r: &mut Registry, storage: u64) -> (Task, Agent) {
    let ws = r.create_workspace(&Actor::Owner, "document").unwrap();
    let budget = r.create_budget(&Actor::Owner, 100, 1).unwrap();
    let t = r
        .create_task(
            &Actor::Owner,
            TaskSpec {
                goal: "host admission".into(),
                acceptance: vec!["independent".into()],
                workload: Workload {
                    class: WorkloadClass::Execution,
                    interruptible: false,
                    resumable: Resume::None,
                    realtime: false,
                    max_memory_bytes: 512 * M,
                    max_storage_bytes: storage,
                    checkpoint_verified: false,
                },
                budget_id: budget,
                workspace_id: ws.id,
                cloud_allowed: false,
            },
        )
        .unwrap();
    let a = r
        .register_agent(&Actor::Owner, "replaceable-harness", &t.id, "mock")
        .unwrap();
    r.configure_runtime(&Actor::Core,&a.id,json!({"sandbox":{"host_resources":true,"cpu_millis":500,"memory_bytes":512*M,"pids":96,"home_capacity_bytes":64*M}})).unwrap();
    (t, a)
}
fn registry(path: &Path) -> Registry {
    let mut r = Registry::open(path).unwrap();
    r.register_model(&Actor::Owner, "mock", json!({"verified":false}))
        .unwrap();
    r
}
fn policy() -> HostResourcePolicy {
    HostResourcePolicy {
        capacity: HostResources {
            cpu_millis: 8000,
            memory_bytes: 2048 * M,
            pids: 2048,
            storage_bytes: 8192 * M,
        },
        reserved: HostResources {
            cpu_millis: 1000,
            memory_bytes: 1024 * M,
            pids: 128,
            storage_bytes: 1024 * M,
        },
    }
}
fn status(r: &Registry) -> Value {
    r.host_resources(&Actor::Owner).unwrap()
}
#[test]
fn concurrent_agents_cannot_over_admit_shared_host_memory() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = registry(&path);
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    let agents: Vec<_> = (0..8).map(|_| agent(&mut r, 256 * M).1.id).collect();
    drop(r);
    let barrier = Arc::new(Barrier::new(8));
    let handles: Vec<_> = agents
        .into_iter()
        .map(|id| {
            let path = path.clone();
            let b = barrier.clone();
            thread::spawn(move || {
                let mut r = Registry::open(path).unwrap();
                b.wait();
                r.reserve_host_resources(&Actor::Core, &id).is_ok()
            })
        })
        .collect();
    assert_eq!(
        handles
            .into_iter()
            .filter_map(|h| h.join().ok())
            .filter(|v| *v)
            .count(),
        2
    );
    let r = Registry::open(path).unwrap();
    assert_eq!(status(&r)["used"]["memory_bytes"], 1024 * M);
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "host_resources_reserved")
            .count(),
        2
    );
    assert_eq!(
        r.events()
            .unwrap()
            .iter()
            .filter(|e| e.kind == "host_resources_denied")
            .count(),
        6
    );
}
#[test]
fn restart_holds_unknown_cleanup_and_stale_release_cannot_free_replacement() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = registry(&path);
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    let (_, a) = agent(&mut r, 256 * M);
    let first = r.reserve_host_resources(&Actor::Core, &a.id).unwrap();
    assert_eq!(
        r.reserve_host_resources(&Actor::Core, &a.id).unwrap(),
        first
    );
    drop(r);
    let mut r = Registry::open(path).unwrap();
    assert_eq!(status(&r)["used"]["memory_bytes"], 512 * M);
    assert!(
        r.release_host_resources(&Actor::Core, &a.id, "wrong-generation")
            .is_err()
    );
    r.release_host_resources(&Actor::Core, &a.id, first["token"].as_str().unwrap())
        .unwrap();
    assert_eq!(status(&r)["used"]["memory_bytes"], 0);
    assert_eq!(status(&r)["used"]["storage_bytes"], 256 * M);
    let second = r.reserve_host_resources(&Actor::Core, &a.id).unwrap();
    assert_ne!(first["token"], second["token"]);
    assert_eq!(status(&r)["used"]["storage_bytes"], 256 * M);
    assert!(
        r.release_host_resources(&Actor::Core, &a.id, first["token"].as_str().unwrap())
            .is_err()
    );
    assert_eq!(status(&r)["used"]["memory_bytes"], 512 * M);
}
#[test]
fn admission_and_policy_fail_closed_with_atomic_ledger() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = registry(&path);
    let (_, a) = agent(&mut r, 256 * M);
    assert!(r.reserve_host_resources(&Actor::Core, &a.id).is_err());
    assert!(
        r.configure_host_resources(&Actor::Agent(a.id.clone()), 0, policy())
            .is_err()
    );
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    assert!(
        r.configure_host_resources(&Actor::Owner, 0, policy())
            .is_err()
    );
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch("CREATE TRIGGER fail_reservation BEFORE INSERT ON ledger WHEN NEW.kind='host_resources_reserved' BEGIN SELECT RAISE(ABORT,'injected ledger failure'); END;").unwrap();
    assert!(r.reserve_host_resources(&Actor::Core, &a.id).is_err());
    assert!(status(&r)["claims"].as_array().unwrap().is_empty());
    c.execute_batch("DROP TRIGGER fail_reservation").unwrap();
    let held = r.reserve_host_resources(&Actor::Core, &a.id).unwrap();
    assert!(
        r.reserve_host_resources(&Actor::Agent(a.id.clone()), &a.id)
            .is_err()
    );
    let mut shrink = policy();
    shrink.capacity.memory_bytes = 1024 * M;
    assert!(
        r.configure_host_resources(&Actor::Owner, 1, shrink)
            .is_err()
    );
    r.release_host_resources(&Actor::Core, &a.id, held["token"].as_str().unwrap())
        .unwrap();
    let mut shrink = policy();
    shrink.capacity.storage_bytes = 1024 * M;
    assert!(
        r.configure_host_resources(&Actor::Owner, 1, shrink)
            .is_err()
    );
}
#[test]
fn managed_home_tmp_workspace_and_utf8_artifacts_share_task_storage() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (t, a) = agent(&mut r, 128 * M + 8);
    support::launch(&mut r, &a.id, 123);
    r.dispatch_harness(&Actor::Owner, &a.id, "turn", "message", "prompt", "native")
        .unwrap();
    r.submit_artifact(&Actor::Agent(a.id.clone()), &t.id, "txt", true, "汉字")
        .unwrap();
    assert!(
        r.submit_artifact(&Actor::Agent(a.id), &t.id, "txt", true, "汉")
            .is_err()
    );
    r.transfer_lease(&Actor::Owner, &t.spec.workspace_id, 0, "human", now() + 60)
        .unwrap();
    r.write_file(&Actor::Owner, &t.spec.workspace_id, 1, 0, "x", b"ab")
        .unwrap();
    assert!(
        r.write_file(&Actor::Owner, &t.spec.workspace_id, 1, 1, "x", b"abc")
            .is_err()
    );
    assert_eq!(r.workspace(&t.spec.workspace_id).unwrap().version, 1);
    assert_eq!(
        r.read_file(&Actor::Owner, &t.spec.workspace_id, "x")
            .unwrap(),
        b"ab"
    );
}
#[test]
fn schema_two_upgrade_and_snapshot_preserve_resource_holds() {
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("db");
    let mut r = registry(&path);
    let (_, a) = agent(&mut r, 256 * M);
    drop(r);
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch("DROP TABLE sandbox_reservation; DROP TABLE host_resource_policy; DELETE FROM schema_version WHERE version=3; PRAGMA user_version=2;").unwrap();
    drop(c);
    let mut r = Registry::open(&path).unwrap();
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    r.reserve_host_resources(&Actor::Core, &a.id).unwrap();
    let expected = status(&r);
    let snapshot = d.path().join("snapshot");
    r.backup(&Actor::Owner, &snapshot).unwrap();
    let restored = d.path().join("restored");
    Registry::restore_new(&Actor::Owner, &snapshot, &restored).unwrap();
    assert_eq!(status(&Registry::open(restored).unwrap()), expected);
}

#[test]
fn every_resource_dimension_and_insufficient_task_storage_denies_admission() {
    for dimension in 0..4 {
        let d = tempfile::tempdir().unwrap();
        let mut r = registry(&d.path().join("db"));
        let (_, a) = agent(&mut r, 256 * M);
        let mut p = policy();
        match dimension {
            0 => p.capacity.cpu_millis = p.reserved.cpu_millis + 499,
            1 => p.capacity.memory_bytes = p.reserved.memory_bytes + 512 * M - 1,
            2 => p.capacity.pids = p.reserved.pids + 95,
            _ => p.capacity.storage_bytes = p.reserved.storage_bytes + 256 * M - 1,
        }
        r.configure_host_resources(&Actor::Owner, 0, p).unwrap();
        assert!(r.reserve_host_resources(&Actor::Core, &a.id).is_err());
        assert!(status(&r)["claims"].as_array().unwrap().is_empty());
    }
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (_, a) = agent(&mut r, 128 * M - 1);
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    assert!(r.reserve_host_resources(&Actor::Core, &a.id).is_err());
}

#[test]
fn multiple_agents_cannot_allocate_more_persistent_homes_than_task_storage() {
    let d = tempfile::tempdir().unwrap();
    let mut r = registry(&d.path().join("db"));
    let (task, a) = agent(&mut r, 256 * M);
    let config = r.runtime_status(&a.id).unwrap()["config"].clone();
    r.configure_host_resources(&Actor::Owner, 0, policy())
        .unwrap();
    r.reserve_host_resources(&Actor::Core, &a.id).unwrap();
    let second = r
        .register_agent(&Actor::Owner, "another-harness", &task.id, "mock")
        .unwrap();
    r.configure_runtime(&Actor::Core, &second.id, config.clone())
        .unwrap();
    r.reserve_host_resources(&Actor::Core, &second.id).unwrap();
    let third = r
        .register_agent(&Actor::Owner, "third-harness", &task.id, "mock")
        .unwrap();
    r.configure_runtime(&Actor::Core, &third.id, config)
        .unwrap();
    assert!(r.reserve_host_resources(&Actor::Core, &third.id).is_err());
    assert_eq!(status(&r)["claims"].as_array().unwrap().len(), 2);
}
