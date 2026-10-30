//! Optional Docker sandbox for an existing stdin/stdout Harness. The host is
//! trusted; never mount the Docker/Owner sockets or Core database into an Agent.
use crate::{AnyResult, runtime::Spec};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Sandbox {
    pub image: String,
    pub cpu_millis: u32,
    pub memory_bytes: u64,
    pub pids: u32,
    pub read_only: Vec<PathBuf>,
    pub gateway_dir: PathBuf,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub model_gateway: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub native_file_proxy: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub home_capacity_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub host_resources: bool,
}
pub fn name(agent: &str) -> String {
    format!("kane-{agent}")
}
fn docker() -> Command {
    // The guardian inherits the Supervisor lock intentionally. Docker CLI must
    // NOT inherit it: a surviving `docker start --attach` would otherwise keep
    // the Agent locked after BOTH owners died. Only numeric descriptors from
    // this process are inserted into this fixed shell wrapper; no user text.
    let mut close = String::new();
    let Ok(entries) = fs::read_dir("/proc/self/fd") else {
        return Command::new("/bin/false");
    };
    for entry in entries.flatten() {
        let Ok(fd) = entry.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        if fd > 2
            && fs::read_link(entry.path()).is_ok_and(|p| {
                p.file_name()
                    .is_some_and(|n| n.to_string_lossy().ends_with(".runtime.lock"))
            })
        {
            close.push_str(&format!("exec {fd}>&-; "));
        }
    }
    // Bash supports descriptor numbers above 9 (dash does not).
    let mut command = Command::new("/bin/bash");
    command
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin");
    command.args([
        "-c",
        &format!("{close}exec docker \"$@\""),
        "kane-container-runtime",
    ]);
    command
}
fn output(args: &[&str]) -> AnyResult<String> {
    bounded_output(docker().args(args), Duration::from_secs(5))
}
// Observation and removal commands must not hang the control plane indefinitely.
// A timed-out removal is UNKNOWN, never evidence that a container was removed.
fn bounded_output(command: &mut Command, timeout: Duration) -> AnyResult<String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let result = (|| -> AnyResult<String> {
        let mut pipe = child.stdout.take().ok_or("runtime output missing")?;
        rustix::fs::fcntl_setfl(&pipe, rustix::fs::OFlags::NONBLOCK)?;
        let deadline = Instant::now() + timeout;
        let mut data = Vec::new();
        let mut buffer = [0; 8192];
        let mut eof = false;
        loop {
            match pipe.read(&mut buffer) {
                Ok(0) => eof = true,
                Ok(n) => {
                    data.extend_from_slice(&buffer[..n]);
                    if data.len() > 2_000_000 {
                        return Err("container runtime output exceeds limit".into());
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e.into()),
            }
            if eof && let Some(status) = child.try_wait()? {
                if !status.success() {
                    return Err("container runtime rejected operation".into());
                }
                return Ok(String::from_utf8(data)?);
            }
            if Instant::now() >= deadline {
                return Err("container runtime deadline exceeded; outcome unknown".into());
            }
            thread::sleep(Duration::from_millis(5));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}
fn cleanup_until_confirmed(agent: &str) {
    let mut attempts = 0u64;
    loop {
        match remove(agent) {
            Ok(()) => return,
            Err(_) => {
                // Retain the inherited Agent lock until cleanup is confirmed.
                // New Supervisors must not start a second generation.
                if attempts.is_multiple_of(30) {
                    eprintln!("container cleanup pending; ownership lock retained");
                }
                attempts += 1;
                thread::sleep(Duration::from_secs(1));
            }
        }
    }
}
pub fn inspect(agent: &str) -> AnyResult<Value> {
    let v: Value = serde_json::from_str(&output(&["inspect", &name(agent)])?)?;
    let c = v
        .as_array()
        .and_then(|a| a.first())
        .ok_or("missing container")?;
    if c["Config"]["Labels"]["kane.system.agent"] != agent {
        return Err("container ownership mismatch".into());
    }
    Ok(c.clone())
}
pub fn remove(agent: &str) -> AnyResult<()> {
    // Only this exact Agent's container; never a global prune or host mutation.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let list = output(&[
            "ps",
            "-a",
            "--filter",
            &format!("name=^/{}$", name(agent)),
            "--format",
            "{{.ID}}",
        ])?;
        if list.trim().is_empty() {
            return Ok(());
        }
        if let Ok(container) = inspect(agent) {
            // Use the immutable ID observed with the label, not a potentially
            // reused name. --rm may race this removal; confirm absence below.
            let id = container["Id"].as_str().ok_or("container ID missing")?;
            let _ = output(&["rm", "--force", id]);
        }
        // A failed inspect/removal is not success. Retry observation for Docker
        // auto-removal races; unavailable/foreign containers remain fail-closed.
        if Instant::now() >= deadline {
            return Err("container removal not confirmed".into());
        }
        thread::sleep(Duration::from_millis(50));
    }
}
pub fn validate_memory(sandbox: &Sandbox, task: &Value) -> AnyResult<()> {
    if sandbox.host_resources && sandbox.home_capacity_bytes.is_none() {
        return Err("managed profile requires bounded persistent home".into());
    }
    let bound = task["spec"]["workload"]["max_memory_bytes"]
        .as_u64()
        .ok_or("Task memory bound required")?;
    if sandbox.memory_bytes > bound {
        return Err("sandbox exceeds Task memory bound".into());
    }
    if let Some(storage) = sandbox.home_capacity_bytes
        && storage
            > task["spec"]["workload"]["max_storage_bytes"]
                .as_u64()
                .ok_or("Task storage bound required")?
    {
        return Err("runtime home capacity exceeds Task storage bound".into());
    }
    Ok(())
}
pub fn validate(spec: &Spec, core: &Path) -> AnyResult<()> {
    let Some(s) = &spec.sandbox else {
        return Ok(());
    };
    if s.image.len() != 71
        || !s.image.starts_with("sha256:")
        || !s.image[7..].bytes().all(|c| c.is_ascii_hexdigit())
        || !(100..=64000).contains(&s.cpu_millis)
        || s.memory_bytes < 64 * 1024 * 1024
        || s.memory_bytes > 64 * 1024 * 1024 * 1024
        || !(16..=4096).contains(&s.pids)
        || s.read_only.len() > 32
    {
        return Err("invalid pinned sandbox image or resource limit".into());
    }
    if rustix::process::geteuid().as_raw() == 0 {
        return Err("sandbox reference profile requires an unprivileged host owner".into());
    }
    let protected = core.parent().ok_or("core parent")?;
    if let Some(limit) = s.home_capacity_bytes {
        crate::storage::check(&spec.runtime_home, limit)?;
    }
    for p in [&spec.cwd, &spec.runtime_home] {
        if protected.starts_with(p) || p.to_string_lossy().contains([',', '\n']) {
            return Err("workspace/home cannot expose Core directory".into());
        }
    }
    if spec.cwd.starts_with(&spec.runtime_home)
        || spec.runtime_home.starts_with(&spec.cwd)
        || s.gateway_dir.starts_with(&spec.cwd)
        || s.gateway_dir.starts_with(&spec.runtime_home)
        || spec.cwd.starts_with(&s.gateway_dir)
        || spec.runtime_home.starts_with(&s.gateway_dir)
    {
        return Err("workspace, runtime home and gateway must be disjoint".into());
    }
    for p in &s.read_only {
        if !p.is_absolute()
            || fs::canonicalize(p)? != *p
            || (!p.is_file() && !p.is_dir())
            || protected.starts_with(p)
            || p.starts_with(protected)
            || ["/", "/etc", "/proc", "/sys", "/dev", "/run", "/var/run"]
                .iter()
                .any(|d| p == Path::new(d))
            || p.to_string_lossy().contains([',', '\n'])
        {
            return Err("unsafe read-only runtime mount".into());
        }
    }
    if fs::canonicalize(&s.gateway_dir)? != s.gateway_dir
        || !s.gateway_dir.is_dir()
        || s.gateway_dir == protected
        || s.gateway_dir == spec.cwd
        || s.gateway_dir == spec.runtime_home
        || s.gateway_dir.to_string_lossy().contains([',', '\n'])
    {
        return Err("dedicated gateway directory required".into());
    }
    for entry in fs::read_dir(&s.gateway_dir)? {
        let entry = entry?;
        if entry.file_name() == "model.sock" && s.model_gateway {
            let metadata = entry.metadata()?;
            let listeners =
                crate::service::request(core, &serde_json::json!({"op":"model_listeners"}))?;
            let registered = listeners.as_array().is_some_and(|xs| {
                xs.iter().any(|l| {
                    l["device"].as_u64() == Some(metadata.dev())
                        && l["inode"].as_u64() == Some(metadata.ino())
                        && l["pid"].as_u64().is_some_and(|pid| {
                            fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|s| {
                                s.split_once(") ")
                                    .is_some_and(|(_, rest)| !rest.starts_with('Z'))
                            })
                        })
                })
            });
            if !registered {
                return Err("model socket is not an active Core-registered broker; Owner/foreign IPC must never be mounted".into());
            }
        }
        if entry.file_name() != "gateway.sock"
            && !(s.model_gateway
                && entry.file_name() == "model.sock"
                && entry.file_type()?.is_socket())
        {
            return Err("gateway directory contains unrelated files".into());
        }
    }
    Ok(())
}
fn mount(source: &Path, readonly: bool) -> String {
    format!(
        "type=bind,source={},target={},{}",
        source.display(),
        source.display(),
        if readonly {
            "readonly"
        } else {
            "readonly=false"
        }
    )
}
/// This guardian deliberately survives Supervisor death. It retains the kernel
/// ownership lock until the exact labelled container is removed. Agent children
/// never receive that lock or any host descriptors beyond stdin/stdout/stderr.
pub fn guard(parent: u32, spec_path: &Path) -> AnyResult<()> {
    let spec: Spec = serde_json::from_slice(&fs::read(spec_path)?)?;
    let s = spec
        .sandbox
        .as_ref()
        .ok_or("container specification required")?;
    let alive =
        || rustix::process::getppid().map(|p| p.as_raw_nonzero().get() as u32) == Some(parent);
    if !alive() {
        return Err("supervisor disappeared before sandbox start".into());
    }
    remove(&spec.agent_id)?;
    // Recheck immediately before container creation; no fallback to an
    // unbounded directory if the mount disappeared since startup validation.
    if let Some(limit) = s.home_capacity_bytes {
        crate::storage::check(&spec.runtime_home, limit)?;
    }
    let mut command = docker();
    command.args([
        "create",
        "--pull=never",
        "--rm",
        "-i",
        "--init",
        "--name",
        &name(&spec.agent_id),
        "--label",
        &format!("kane.system.agent={}", spec.agent_id),
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        &format!(
            "{}:{}",
            rustix::process::geteuid().as_raw(),
            rustix::process::getegid().as_raw()
        ),
        "--memory",
        &s.memory_bytes.to_string(),
        "--memory-swap",
        &s.memory_bytes.to_string(),
        "--pids-limit",
        &s.pids.to_string(),
        "--cpus",
        &format!("{}.{:03}", s.cpu_millis / 1000, s.cpu_millis % 1000),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777",
        "--workdir",
        spec.cwd.to_str().ok_or("cwd")?,
        "--env",
        &format!("HOME={}", spec.runtime_home.display()),
    ]);
    for p in &s.read_only {
        command.args(["--mount", &mount(p, true)]);
    }
    command.args([
        "--mount",
        &mount(&spec.cwd, true),
        "--mount",
        &mount(&spec.runtime_home, false),
        "--mount",
        &mount(&s.gateway_dir, true),
    ]);
    command
        .arg(&s.image)
        .args(&spec.command)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    // Finish creation before attaching. Once created, start cannot create a
    // replacement after cleanup; no timeout-based guess about a late run request.
    let created = command.output()?;
    if !created.status.success() || !alive() {
        remove(&spec.agent_id)?;
        return Err("sandbox creation failed or supervisor lost".into());
    }
    inspect(&spec.agent_id)?;
    let mut child = docker()
        .args(["start", "--attach", "--interactive", &name(&spec.agent_id)])
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;
    loop {
        if let Some(status) = child.try_wait()? {
            cleanup_until_confirmed(&spec.agent_id);
            return if status.success() {
                Ok(())
            } else {
                Err("sandbox runtime exited unsuccessfully".into())
            };
        }
        if !alive() {
            // Creation already completed. Remove this exact container before
            // releasing the ownership lock, including any in-container children.
            let _ = child.kill();
            let _ = child.wait();
            cleanup_until_confirmed(&spec.agent_id);
            return Err("supervisor lost; sandbox removed; no task replay".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(root: &Path) -> (Spec, PathBuf) {
        for d in ["core", "workspace", "home", "gateway", "code"] {
            fs::create_dir(root.join(d)).unwrap();
        }
        let core = root.join("core/core.sock");
        let s = Spec {
            agent_id: "test-agent".into(),
            command: vec!["/bin/sh".into()],
            cwd: root.join("workspace"),
            runtime_home: root.join("home"),
            max_restarts: 1,
            restart_delay_ms: 100,
            sandbox: Some(Sandbox {
                image: format!("sha256:{}", "0".repeat(64)),
                cpu_millis: 500,
                memory_bytes: 128 * 1024 * 1024,
                pids: 64,
                read_only: vec![root.join("code")],
                gateway_dir: root.join("gateway"),
                model_gateway: false,
                native_file_proxy: false,
                home_capacity_bytes: None,
                host_resources: false,
            }),
        };
        (s, core)
    }
    #[test]
    fn pinned_limits_and_separate_gateway_are_required() {
        let d = tempfile::tempdir().unwrap();
        let (mut s, c) = fixture(d.path());
        validate(&s, &c).unwrap();
        s.sandbox.as_mut().unwrap().image = "ubuntu:latest".into();
        assert!(validate(&s, &c).is_err());
        s.sandbox.as_mut().unwrap().image = format!("sha256:{}", "0".repeat(64));
        s.sandbox.as_mut().unwrap().cpu_millis = 0;
        assert!(validate(&s, &c).is_err());
        s.sandbox.as_mut().unwrap().cpu_millis = 500;
        fs::write(
            s.sandbox.as_ref().unwrap().gateway_dir.join("owner.sock"),
            "not gateway",
        )
        .unwrap();
        assert!(validate(&s, &c).is_err());
    }
    #[test]
    fn core_paths_cannot_be_exposed_by_runtime_mounts() {
        let d = tempfile::tempdir().unwrap();
        let (mut s, c) = fixture(d.path());
        s.sandbox.as_mut().unwrap().read_only = vec![d.path().to_owned()];
        assert!(validate(&s, &c).is_err());
        s.sandbox.as_mut().unwrap().read_only = vec![c.parent().unwrap().to_owned()];
        assert!(validate(&s, &c).is_err());
        s.sandbox.as_mut().unwrap().read_only.clear();
        s.runtime_home = c.parent().unwrap().to_owned();
        assert!(validate(&s, &c).is_err());
    }
    #[test]
    fn symlinks_and_overlapping_writable_paths_are_rejected() {
        let d = tempfile::tempdir().unwrap();
        let (mut s, c) = fixture(d.path());
        std::os::unix::fs::symlink(d.path().join("code"), d.path().join("alias")).unwrap();
        s.sandbox.as_mut().unwrap().read_only = vec![d.path().join("alias")];
        assert!(validate(&s, &c).is_err());
        s.sandbox.as_mut().unwrap().read_only.clear();
        s.sandbox.as_mut().unwrap().gateway_dir = s.runtime_home.clone();
        assert!(validate(&s, &c).is_err());
    }
}

#[cfg(test)]
mod resource_tests {
    use super::*;
    #[test]
    fn container_memory_cannot_exceed_registered_task_bound() {
        let s = Sandbox {
            image: String::new(),
            cpu_millis: 500,
            memory_bytes: 128 * 1024 * 1024,
            pids: 64,
            read_only: vec![],
            gateway_dir: PathBuf::new(),
            model_gateway: false,
            native_file_proxy: false,
            home_capacity_bytes: None,
            host_resources: false,
        };
        assert!(
            validate_memory(
                &s,
                &serde_json::json!({"spec":{"workload":{"max_memory_bytes":128*1024*1024}}})
            )
            .is_ok()
        );
        assert!(
            validate_memory(
                &s,
                &serde_json::json!({"spec":{"workload":{"max_memory_bytes":64*1024*1024}}})
            )
            .is_err()
        );
        assert!(validate_memory(&s, &serde_json::json!({})).is_err());
    }
}

#[cfg(test)]
mod command_tests {
    use super::*;
    #[test]
    fn unavailable_or_hung_runtime_is_not_success() {
        assert!(
            bounded_output(
                Command::new("/bin/false").arg("ignored"),
                Duration::from_secs(1)
            )
            .is_err()
        );
        let start = Instant::now();
        assert!(
            bounded_output(
                Command::new("/bin/sleep").arg("10"),
                Duration::from_millis(40)
            )
            .is_err()
        );
        assert!(start.elapsed() < Duration::from_secs(2));
    }
    #[test]
    fn noisy_runtime_output_is_bounded() {
        assert!(
            bounded_output(
                Command::new("/usr/bin/yes").arg("fixture"),
                Duration::from_secs(5)
            )
            .is_err()
        );
        assert_eq!(
            bounded_output(
                Command::new("/bin/echo").arg("fixture"),
                Duration::from_secs(1)
            )
            .unwrap(),
            "fixture\n"
        );
    }
}
