//! Trusted model process supervisor. Reuses the existing parent-death exec guard.
use crate::{AnyResult, model_gateway, model_limits, service};
use kane_core::LocalModelSpec;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{OpenOptionsExt, PermissionsExt},
        net::UnixListener,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Spec {
    #[serde(default)]
    enforcement: Enforcement,
    instance_id: String,
    command: Vec<String>,
    runtime: LocalModelSpec,
    #[serde(default = "default_health_timeout")]
    health_timeout_seconds: u64,
}
#[derive(Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
enum Enforcement {
    CgroupV2 { root: PathBuf },
    DiagnosticUnmanaged,
}
impl Default for Enforcement {
    fn default() -> Self {
        Self::CgroupV2 {
            root: PathBuf::from("/sys/fs/cgroup/kane-models"),
        }
    }
}
fn default_health_timeout() -> u64 {
    45
}
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn endpoint_absent(endpoint: &str) -> bool {
    let Some(address) = endpoint
        .strip_prefix("http://")
        .and_then(|s| s.parse::<std::net::SocketAddr>().ok())
    else {
        return false;
    };
    matches!(std::net::TcpStream::connect_timeout(&address,Duration::from_millis(250)),Err(error) if error.kind()==std::io::ErrorKind::ConnectionRefused)
}
fn owns_endpoint(pid: u32, endpoint: &str) -> bool {
    let Some(port) = endpoint
        .rsplit(':')
        .next()
        .and_then(|p| p.parse::<u16>().ok())
    else {
        return false;
    };
    let address = format!("0100007F:{port:04X}");
    let sockets = fs::read_to_string("/proc/net/tcp").unwrap_or_default();
    let inodes = sockets
        .lines()
        .filter_map(|line| {
            let fields = line.split_whitespace().collect::<Vec<_>>();
            (fields.len() > 9 && fields[1] == address && fields[3] == "0A")
                .then(|| format!("socket:[{}]", fields[9]))
        })
        .collect::<Vec<_>>();
    fs::read_dir(format!("/proc/{pid}/fd")).is_ok_and(|fds| {
        fds.flatten().any(|fd| {
            fs::read_link(fd.path()).is_ok_and(|p| {
                inodes
                    .iter()
                    .any(|inode| p.to_string_lossy() == inode.as_str())
            })
        })
    })
}
pub fn run(core: &Path, path: &Path) -> AnyResult<()> {
    let spec: Spec = serde_json::from_slice(&fs::read(path)?)?;
    spec.runtime.validate()?;
    if !(1..=180).contains(&spec.health_timeout_seconds) {
        return Err("health timeout 1..180s; explicit bounded Owner load budget".into());
    }
    if spec.instance_id.is_empty()
        || spec.command.is_empty()
        || !Path::new(&spec.command[0]).is_absolute()
        || !spec
            .instance_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("absolute Owner-selected command and instance identity required".into());
    }
    let core = fs::canonicalize(core)?;
    let core = core.as_path();
    let directory = core.parent().ok_or("core directory")?;
    if fs::metadata(directory)?.permissions().mode() & 0o077 != 0 {
        return Err("model control requires canonical private Owner IPC directory".into());
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(directory.join(format!("{}.model.lock", spec.instance_id)))?;
    lock.try_lock()?;
    let socket = directory.join(format!("{}.model.sock", spec.instance_id));
    if socket.exists() {
        if std::os::unix::net::UnixStream::connect(&socket).is_ok() {
            return Err("model supervisor alive".into());
        }
        fs::remove_file(&socket)?;
    }
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    // Establish all kernel limits BEFORE resource admission or any backend exec.
    // Historical diagnostics require an explicit Owner mode; never a fallback.
    let group = match &spec.enforcement {
        Enforcement::CgroupV2 { root } => Some(
            match model_limits::ModelCgroup::create(root, &spec.instance_id, spec.runtime.resources)
            {
                Ok(group) => group,
                Err(error) => {
                    service::request(
                        core,
                        &json!({"op":"record_model_limits","id":spec.instance_id,"generation":"","evidence":{"refused":true,"reason":error.to_string(),"backend_started":false,"fallback":false}}),
                    )?;
                    return Err(error);
                }
            },
        ),
        Enforcement::DiagnosticUnmanaged => None,
    };
    let loaded = service::request(
        core,
        &json!({"op":"load_local_model","id":spec.instance_id,"spec":spec.runtime}),
    )?;
    let generation = loaded["generation"].as_str().ok_or("generation")?;
    let lifecycle = |state: &str, pid: Option<u32>, evidence: &str| {
        service::request(
            core,
            &json!({"op":"model_lifecycle","id":spec.instance_id,"generation":generation,"state":state,"pid":pid,"evidence":evidence}),
        )
    };
    let evidence = |phase: &str| -> AnyResult<()> {
        let mut data = if let Some(g) = &group {
            g.snapshot()?
        } else {
            json!({"mode":"diagnostic_unmanaged","kernel_enforced":false})
        };
        data["phase"] = json!(phase);
        data["boot_id"] = json!(fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim());
        service::request(
            core,
            &json!({"op":"record_model_limits","id":spec.instance_id,"generation":generation,"evidence":data}),
        )?;
        Ok(())
    };
    evidence("before_exec")?;
    let mut launch = Command::new(std::env::current_exe()?);
    launch.env_clear().env("PATH", "/usr/bin:/bin");
    if let Some(g) = &group {
        launch
            .args([
                "model-exec",
                "--parent",
                &std::process::id().to_string(),
                "--cgroup",
            ])
            .arg(g.path())
            .arg("--");
    } else {
        launch.args([
            "runtime-exec",
            "--parent",
            &std::process::id().to_string(),
            "--",
        ]);
    }
    let child = launch
        .args(&spec.command)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn();
    let mut child = match child {
        Ok(c) => ChildGuard(c),
        Err(e) => {
            let _ = lifecycle(
                "Failed",
                None,
                "spawn failed; resources retained for cleanup",
            );
            return Err(e.into());
        }
    };
    lifecycle(
        "Loading",
        Some(child.0.id()),
        "foreground child spawned; health not yet proven",
    )?;
    let start = Instant::now();
    loop {
        if child.0.try_wait()?.is_some() {
            evidence("process_exit")?;
            lifecycle(
                "Failed",
                None,
                "model process exited before health confirmation",
            )?;
            return Err("model load failed".into());
        }
        if owns_endpoint(child.0.id(), &spec.runtime.endpoint)
            && model_gateway::health(&spec.runtime.endpoint, &spec.runtime.backend_model).is_ok()
        {
            break;
        }
        if start.elapsed() > Duration::from_secs(spec.health_timeout_seconds) {
            lifecycle("Failed", None, "health timeout; no backend fallback")?;
            return Err("model health timeout".into());
        }
        thread::sleep(Duration::from_millis(100));
    }
    evidence("healthy_ready")?;
    lifecycle(
        "Ready",
        Some(child.0.id()),
        "actual child alive; HTTP health ok and exact backend Model ID matched",
    )?;
    let mut last = Instant::now();
    loop {
        if child.0.try_wait()?.is_some() {
            evidence("process_exit")?;
            lifecycle(
                "Failed",
                None,
                "observed backend exit; unknown calls retained",
            )?;
            break;
        }
        if let Ok((mut stream, _)) = listener.accept() {
            let peer = rustix::net::sockopt::socket_peercred(&stream)?;
            if peer.uid != rustix::process::geteuid() && peer.uid.as_raw() != 0 {
                eprintln!("model control: local peer denied");
                continue;
            }
            stream.set_read_timeout(Some(Duration::from_secs(1)))?;
            let mut command = [0; 32];
            let n = stream.read(&mut command)?;
            if &command[..n] == b"stop\n" {
                lifecycle(
                    "Draining",
                    Some(child.0.id()),
                    "Owner stop; no new dispatch",
                )?;
                let drain = Instant::now();
                loop {
                    // In-flight work may complete. Do not kill it then pretend NotExecuted.
                    let rows = service::request(core, &json!({"op":"model_queue"}))?;
                    let instances = service::request(core, &json!({"op":"model_instances"}))?;
                    let model = instances
                        .as_array()
                        .and_then(|xs| xs.iter().find(|i| i["id"] == spec.instance_id))
                        .ok_or("instance")?["model_id"]
                        .clone();
                    let active = rows.as_array().is_some_and(|xs| {
                        xs.iter()
                            .any(|q| q["model_id"] == model && q["state"] == "Dispatched")
                    });
                    if !active {
                        break;
                    }
                    if drain.elapsed() > Duration::from_secs(45) {
                        lifecycle("Failed", None, "drain timeout; unknown effects retained")?;
                        break;
                    }
                    thread::sleep(Duration::from_millis(50));
                }
                child.0.kill()?;
                child.0.wait()?;
                if !endpoint_absent(&spec.runtime.endpoint) {
                    lifecycle(
                        "Failed",
                        None,
                        "foreground child absent but endpoint still reachable; no unsafe resource release",
                    )?;
                    return Err("model endpoint absence unproven".into());
                }
                evidence("stopped")?;
                if let Some(g) = &group {
                    g.cleanup()?;
                }
                lifecycle(
                    "Unloaded",
                    None,
                    "child wait confirmed process absent; CPU/memory released; model storage retained",
                )?;
                let _ = stream.write_all(b"stopped\n");
                break;
            }
        }
        if last.elapsed() > Duration::from_secs(2) {
            if model_gateway::health(&spec.runtime.endpoint, &spec.runtime.backend_model).is_err() {
                // Health failure can race kernel OOM/process exit. Preserve the
                // actual cgroup counters before RAII removes the group, and
                // confirm child absence before recording the terminal evidence.
                evidence("health_failed")?;
                let _ = child.0.kill();
                child.0.wait()?;
                evidence("process_exit")?;
                lifecycle(
                    "Failed",
                    None,
                    "active health failed; child wait confirmed; refuse new work",
                )?;
                break;
            }
            last = Instant::now();
        }
        thread::sleep(Duration::from_millis(30));
    }
    fs::remove_file(socket)?;
    Ok(())
}

/// Administrative local-process cleanup only. Never retries model calls, releases
/// Unknown token reservations, or infers absence from an unverified PID alone.
pub fn reconcile(core: &Path, instance: &str) -> AnyResult<()> {
    if instance.is_empty()
        || !instance
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("instance ID".into());
    }
    let rows = service::request(core, &json!({"op":"model_instances"}))?;
    let row = rows
        .as_array()
        .and_then(|xs| xs.iter().find(|x| x["id"] == instance))
        .ok_or("instance missing")?;
    let events = service::request(core, &json!({"op":"events"}))?;
    let generation = row["generation"].as_str().ok_or("generation")?;
    let current_boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let reboot_proven = boot_changed(&events, instance, generation, current_boot.trim());
    let _lock = service::recovery_lock(
        &core
            .parent()
            .ok_or("directory")?
            .join(format!("{instance}.model.lock")),
        reboot_proven,
    )?;
    if let Some(pid) = row["pid"].as_u64() {
        if !reboot_proven {
            match fs::read_to_string(format!("/proc/{pid}/stat")) {
                Ok(stat)
                    if stat
                        .split_once(") ")
                        .is_none_or(|(_, s)| !s.starts_with('Z')) =>
                {
                    return Err("model PID exists; no unsafe reclaim".into());
                }
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
                _ => {}
            }
        }
    } else if !reboot_proven {
        return Err("absence cannot be proven without PID or a recorded earlier boot".into());
    }
    if !endpoint_absent(row["endpoint"].as_str().ok_or("endpoint")?) {
        return Err("model endpoint still reachable; absence unproven".into());
    }
    if row["state"] != "Failed" {
        service::request(
            core,
            &json!({"op":"model_lifecycle","id":instance,"generation":generation,"state":"Failed","evidence":if reboot_proven {"held manager lock; different recorded Linux boot ID proves old process dead; endpoint absent"} else {"held manager kernel lock and confirmed recorded foreground PID absent/zombie"}}),
        )?;
    }
    if let Some(proof) = events.as_array().and_then(|xs| {
        xs.iter().rev().find(|e| {
            e["object_id"] == instance
                && e["kind"] == "model_kernel_limits"
                && e["payload"]["generation"] == generation
        })
    }) && proof["payload"]["evidence"]["mode"] == "cgroup_v2"
    {
        let path = Path::new(
            proof["payload"]["evidence"]["path"]
                .as_str()
                .ok_or("model group path")?,
        );
        if path.file_name().and_then(|n| n.to_str()) != Some(format!("model-{instance}").as_str()) {
            return Err("model group identity mismatch".into());
        }
        model_limits::cleanup(path)?;
    }
    service::request(
        core,
        &json!({"op":"model_lifecycle","id":instance,"generation":generation,"state":"Unloaded","evidence":"confirmed absence; model residency CPU/memory reclaimed; no Task replay, budget refund or route unfreeze"}),
    )?;
    Ok(())
}

// A before-exec proof is preferred. If the manager died before writing it,
// bind its durable Loading generation to the preceding trusted Core boot event.
fn boot_changed(events: &Value, instance: &str, generation: &str, current: &str) -> bool {
    let Some(rows) = events.as_array() else {
        return false;
    };
    let proof = rows
        .iter()
        .rev()
        .find(|e| {
            e["object_id"] == instance
                && e["kind"] == "model_kernel_limits"
                && e["payload"]["generation"] == generation
        })
        .and_then(|e| e["payload"]["evidence"]["boot_id"].as_str());
    let loaded = rows
        .iter()
        .rev()
        .find(|e| {
            e["object_id"] == instance
                && e["kind"] == "model_loading"
                && e["payload"]["generation"] == generation
        })
        .and_then(|e| e["seq"].as_u64());
    let prior = proof.or_else(|| {
        loaded.and_then(|cut| {
            rows.iter()
                .rev()
                .find(|e| e["kind"] == "host_boot" && e["seq"].as_u64().is_some_and(|s| s < cut))
                .and_then(|e| e["payload"]["boot_id"].as_str())
        })
    });
    prior.is_some_and(|id| !id.is_empty() && id != current)
}
#[cfg(test)]
mod boot_proof_tests {
    use super::*;
    #[test]
    fn boot_fallback_is_bound_to_recorded_instance_and_generation_not_pid_number() {
        let events = json!([{"seq":1,"kind":"host_boot","object_id":"host","payload":{"boot_id":"old"}},{"seq":2,"kind":"model_loading","object_id":"instance","payload":{"generation":"generation"}},{"seq":3,"kind":"host_boot","object_id":"host","payload":{"boot_id":"new"}}]);
        assert!(boot_changed(&events, "instance", "generation", "new"));
        assert!(!boot_changed(&events, "instance", "generation", "old"));
        assert!(!boot_changed(&events, "other", "generation", "new"));
        assert!(!boot_changed(&events, "instance", "other", "new"));
        assert!(!boot_changed(&json!([]), "instance", "generation", "new"));
    }
    #[test]
    fn owner_load_budget_is_bounded_without_skipping_readiness_or_default_policy() {
        assert_eq!(default_health_timeout(), 45);
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("model.json");
        for timeout in [0, 45, 180, 181] {
            let value = json!({"instance_id":"test-instance","command":["/usr/bin/true"],"health_timeout_seconds":timeout,"runtime":{"endpoint":"http://127.0.0.1:18181","backend_model":"test","context_tokens":2048,"parallelism":1,"resources":{"cpu_millis":1000,"memory_bytes":1073741824_u64,"pids":64,"storage_bytes":134217728}}});
            fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
            // The absent Core makes execution impossible. Validate the real CLI
            // path rejects invalid bounds BEFORE any process/resource admission;
            // valid explicit bounds proceed to the SAME fail-closed Core check.
            let error = run(&directory.path().join("missing-core.sock"), &path)
                .unwrap_err()
                .to_string();
            assert_eq!(
                error.contains("health timeout"),
                timeout == 0 || timeout == 181,
                "{error}"
            );
        }
    }
}
