//! Linux trusted-host process ownership, independent of ACP/Agent implementation.
//! Optional Linux container boundary with durable host resource admission.
use crate::{AnyResult, sandbox, service};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    net::Shutdown,
    os::{
        fd::AsRawFd,
        unix::{
            fs::{OpenOptionsExt, PermissionsExt},
            net::{UnixListener, UnixStream},
            process::CommandExt,
        },
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub agent_id: String,
    pub command: Vec<String>,
    pub cwd: PathBuf,
    pub runtime_home: PathBuf,
    pub max_restarts: u32,
    pub restart_delay_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sandbox: Option<sandbox::Sandbox>,
}
struct Process {
    child: Child,
    transport: Option<UnixStream>,
    sandbox_agent: Option<String>,
}
impl Process {
    fn stop(&mut self) -> AnyResult<()> {
        if let Some(s) = &self.transport {
            let _ = s.shutdown(Shutdown::Both);
        }
        drop(self.child.stdin.take());
        if let Some(agent) = &self.sandbox_agent {
            sandbox::remove(agent)?;
        }
        for _ in 0..100 {
            if self.child.try_wait()?.is_some() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(20));
        }
        self.child.kill()?;
        self.child.wait()?;
        Ok(())
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if let Some(s) = &self.transport {
            let _ = s.shutdown(Shutdown::Both);
        }
        if let Some(agent) = &self.sandbox_agent
            && sandbox::remove(agent).is_err()
        {
            // The guardian keeps the lock and retries cleanup after this
            // Supervisor exits. Killing it here would lose that ownership.
            eprintln!("sandbox cleanup unconfirmed; guardian retained");
            return;
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
struct Socket(PathBuf);
impl Drop for Socket {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
fn call(core: &Path, id: &str, op: &str) -> AnyResult<Value> {
    service::request(core, &json!({"op":op,"id":id}))
}
fn record(core: &Path, id: &str, token: &str, attempt: u64, mut data: Value) -> AnyResult<Value> {
    data["owner_token"] = json!(token);
    data["attempt"] = json!(attempt);
    let state = call(core, id, "runtime_status")?;
    let result = service::request(
        core,
        &json!({"op":"runtime_event","id":id,"version":state["agent"]["version"],"data":data}),
    )?;
    eprintln!(
        "{}",
        json!({"agent_id":id,"lifecycle":result["last"],"agent_state":result["agent"]["state"]})
    );
    Ok(result)
}
fn release_resources(core: &Path, id: &str) -> AnyResult<()> {
    let state = service::request(core, &json!({"op":"host_resources"}))?;
    if let Some(claim) = state["claims"]
        .as_array()
        .and_then(|v| v.iter().find(|v| v["agent_id"] == id && v["held"] == true))
    {
        service::request(
            core,
            &json!({"op":"release_host_resources","id":id,"token":claim["token"]}),
        )?;
    }
    Ok(())
}
struct ResourceLease {
    core: PathBuf,
    id: String,
    token: String,
}
impl Drop for ResourceLease {
    fn drop(&mut self) {
        if sandbox::remove(&self.id).is_ok() {
            if service::request(
                &self.core,
                &json!({"op":"release_host_resources","id":self.id,"token":self.token}),
            )
            .is_err()
            {
                eprintln!("resource release unconfirmed; durable reservation retained");
            }
        } else {
            eprintln!("sandbox absence unconfirmed; durable reservation retained");
        }
    }
}
/// Owner maintenance only. Kernel lock + verified absence precede release.
/// This never replays a Task, settles a model hold, or changes a tool outcome.
pub fn reconcile(core: &Path, id: &str) -> AnyResult<()> {
    if id.is_empty() || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return Err("invalid agent id".into());
    }
    let core = fs::canonicalize(core)?;
    let path = core
        .parent()
        .ok_or("core parent")?
        .join(format!("{id}.runtime.lock"));
    let events = service::request(&core, &json!({"op":"events"}))?;
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let reboot_proven = reboot_lock_proven(&events, id, boot.trim());
    let _lock = service::recovery_lock(&path, reboot_proven)?;
    sandbox::remove(id)?;
    release_resources(&core, id)
}
fn reboot_lock_proven(events: &Value, id: &str, boot: &str) -> bool {
    let Some(rows) = events.as_array() else {
        return false;
    };
    let Some(last) = rows
        .iter()
        .rev()
        .find(|e| e["object_id"] == id && e["kind"] == "runtime_lifecycle")
    else {
        return false;
    };
    if boot.is_empty() {
        return false;
    }
    if last["payload"]["detail"]["event"] == "host_reboot_observed"
        && last["payload"]["detail"]["boot_id"] == boot
    {
        return true;
    }
    // Terminal Agents remain terminal across reboot. Their old physical resource
    // holds may still need cleanup, without resetting state or retry allowance.
    last["seq"]
        .as_u64()
        .and_then(|cut| {
            rows.iter().rev().find(|e| {
                e["kind"] == "host_boot" && e["seq"].as_u64().is_some_and(|seq| seq < cut)
            })
        })
        .and_then(|e| e["payload"]["boot_id"].as_str())
        .is_some_and(|previous| !previous.is_empty() && previous != boot)
}
fn relay(input: &mut impl Read, output: &mut impl Write) -> std::io::Result<()> {
    let mut buffer = [0u8; 8192];
    loop {
        let size = input.read(&mut buffer)?;
        if size == 0 {
            return Ok(());
        }
        output.write_all(&buffer[..size])?;
        output.flush()?;
    }
}
pub fn exec_guard(parent: u32, command: Vec<String>) -> AnyResult<()> {
    // Safe prctl before exec avoids pre_exec/unsafe in the multithreaded parent.
    rustix::process::set_parent_process_death_signal(Some(rustix::process::Signal::KILL))?;
    if rustix::process::getppid().map(|p| p.as_raw_nonzero().get() as u32) != Some(parent) {
        return Err("supervisor disappeared before exec".into());
    }
    let executable = command.first().ok_or("empty command")?;
    Err(Command::new(executable).args(&command[1..]).exec().into())
}

pub fn supervise(core: &Path, spec_path: &Path) -> AnyResult<()> {
    let spec: Spec = serde_json::from_slice(&fs::read(spec_path)?)?;
    if spec.command.is_empty()
        || !Path::new(&spec.command[0]).is_absolute()
        || spec.max_restarts > 100
        || !(100..=60_000).contains(&spec.restart_delay_ms)
    {
        return Err(
            "absolute executable, max_restarts <= 100 and delay 100..60000 ms required".into(),
        );
    }
    if spec.agent_id.is_empty()
        || !spec
            .agent_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("invalid agent id".into());
    }
    if !spec.cwd.is_absolute()
        || fs::canonicalize(&spec.cwd)? != spec.cwd
        || !spec.cwd.is_dir()
        || !spec.runtime_home.is_absolute()
        || fs::canonicalize(&spec.runtime_home)? != spec.runtime_home
    {
        return Err("canonical workspace/runtime home required".into());
    }
    let core = fs::canonicalize(core)?;
    sandbox::validate(&spec, &core)?;
    let dir = core.parent().ok_or("core parent required")?;
    if fs::metadata(dir)?.permissions().mode() & 0o077 != 0 {
        return Err("private core directory required".into());
    }
    let socket = dir.join(format!("{}.runtime.sock", spec.agent_id));
    let lock_path = dir.join(format!("{}.runtime.lock", spec.agent_id));
    if fs::symlink_metadata(&lock_path).is_ok_and(|m| !m.is_file() || m.file_type().is_symlink()) {
        return Err("invalid runtime lock".into());
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(lock_path)?;
    // A killed runtime may have closed its main PID while an inherited host
    // descriptor is still being released. Briefly wait on the actual kernel
    // lock; never infer ownership solely from PID death or remove the lock file.
    let lock_deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match lock.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) if Instant::now() < lock_deadline => {
                thread::sleep(Duration::from_millis(20));
            }
            Err(error) => return Err(error.into()),
        }
    }
    // Kernel lock survives supervisor death in its child until exec/runtime exit.
    // Thus a new supervisor cannot overlap the dying previous generation.
    rustix::io::fcntl_setfd(&lock, rustix::io::FdFlags::empty())?;
    if let Some(sandbox) = &spec.sandbox {
        let binding = call(&core, &spec.agent_id, "runtime_status")?;
        let task = call(
            &core,
            binding["agent"]["task_id"].as_str().ok_or("task binding")?,
            "task",
        )?;
        sandbox::validate_memory(sandbox, &task)?;
        // Also clean after BOTH supervisor and guardian have died, even if the
        // persistent restart budget is exhausted or the Agent is terminal.
        // Failure is closed: no new lifecycle/session or process is started.
        sandbox::remove(&spec.agent_id)?;
        release_resources(&core, &spec.agent_id)?;
    }
    let config = serde_json::to_value(&spec)?;
    service::request(
        &core,
        &json!({"op":"runtime_configure","id":spec.agent_id,"config":config}),
    )?;
    // The existing kernel lock and container absence check precede this claim.
    let ownership = service::request(
        &core,
        &json!({"op":"runtime_acquire","id":spec.agent_id,"absence_confirmed":true}),
    )?;
    let token = ownership["control"]["owner_token"]
        .as_str()
        .ok_or("runtime ownership token")?;
    if matches!(
        ownership["agent"]["state"].as_str(),
        Some("Failed" | "Exited" | "Terminated")
    ) {
        return Err("terminal Agent; no automatic reset".into());
    }
    let _resources = if spec.sandbox.as_ref().is_some_and(|s| s.host_resources) {
        let state = service::request(&core, &json!({"op":"host_resources"}))?;
        let policy: kane_core::HostResourcePolicy =
            serde_json::from_value(state["policy"].clone())?;
        // IPC may live on /run tmpfs in the installed OS. Storage admission
        // concerns the actual native Workspace filesystem, not the socket.
        crate::storage::validate_host_capacity(&spec.cwd, &policy)?;
        let held = service::request(
            &core,
            &json!({"op":"reserve_host_resources","id":spec.agent_id}),
        )?;
        Some(ResourceLease {
            core: core.clone(),
            id: spec.agent_id.clone(),
            token: held["token"].as_str().ok_or("resource token")?.into(),
        })
    } else {
        None
    };
    if socket.exists() {
        fs::remove_file(&socket)?;
    }
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    let _socket_guard = Socket(socket);
    listener.set_nonblocking(true)?;
    loop {
        let launch = service::request(
            &core,
            &json!({"op":"runtime_begin","id":spec.agent_id,"owner_token":token}),
        )?;
        if launch["allowed"] != true {
            return Err("persistent restart allowance exhausted".into());
        }
        let attempts = launch["attempt"].as_u64().ok_or("persistent attempt")?;
        let executable_ok = spec.sandbox.is_some()
            || fs::metadata(&spec.command[0])
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0);
        let child = if !executable_ok {
            Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "runtime executable missing or not executable",
            ))
        } else {
            let mut command = Command::new(std::env::current_exe()?);
            if spec.sandbox.is_some() {
                command
                    .arg("runtime-container")
                    .arg("--parent")
                    .arg(std::process::id().to_string())
                    .arg("--spec")
                    .arg(fs::canonicalize(spec_path)?);
            } else {
                command
                    .arg("runtime-exec")
                    .arg("--parent")
                    .arg(std::process::id().to_string())
                    .arg("--")
                    .args(&spec.command);
            }
            command
                .current_dir(&spec.cwd)
                .env_clear()
                .env("PATH", "/usr/local/bin:/usr/bin:/bin")
                .env("HOME", &spec.runtime_home)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
        };
        let child = match child {
            Ok(c) => c,
            Err(e) => {
                let decision = record(
                    &core,
                    &spec.agent_id,
                    token,
                    attempts,
                    json!({"event":"start_failed","attempt":attempts,"reason":e.to_string()}),
                )?;
                if decision["agent"]["state"] == "Failed" {
                    return Err("persistent restart allowance exhausted after start failure".into());
                }
                thread::sleep(Duration::from_millis(spec.restart_delay_ms));
                continue;
            }
        };
        let mut process = Process {
            child,
            transport: None,
            sandbox_agent: spec.sandbox.as_ref().map(|_| spec.agent_id.clone()),
        };
        record(
            &core,
            &spec.agent_id,
            token,
            attempts,
            json!({"event":"started","pid":process.child.id(),"pid_kind":if spec.sandbox.is_some(){"container_guardian"}else{"native_process"},"attempt":attempts,"restart_count":attempts-1,"lock_fd":lock.as_raw_fd(),"container":spec.sandbox.as_ref().map(|_|sandbox::name(&spec.agent_id)),"health":"process alive; ACP readiness tested by adapter"}),
        )?;
        // 1: controller detached; 2: runtime output/pipe failed. First cause
        // wins, before shutting the other half of the ACP transport.
        let disconnected = Arc::new(AtomicU8::new(0));
        loop {
            if let Some(exit) = process.child.try_wait()? {
                let decision = record(
                    &core,
                    &spec.agent_id,
                    token,
                    attempts,
                    json!({"event":"process_exited","success":exit.success(),"attempt":attempts,"exit_code":exit.code(),"reason":exit.to_string()}),
                )?;
                if exit.success() {
                    return Ok(());
                }
                if decision["agent"]["state"] == "Failed" {
                    return Err(
                        "persistent restart allowance exhausted after process failure".into(),
                    );
                }
                break;
            }
            let disconnect_cause = disconnected.load(Ordering::SeqCst);
            if disconnect_cause != 0 {
                process.stop()?;
                if disconnect_cause == 2 {
                    let exit = process
                        .child
                        .try_wait()?
                        .ok_or("runtime exit not confirmed")?;
                    let decision = record(
                        &core,
                        &spec.agent_id,
                        token,
                        attempts,
                        json!({"event":"process_exited","success":exit.success(),"exit_code":exit.code(),"attempt":attempts,"reason":"runtime ACP output/pipe closed; no task replay"}),
                    )?;
                    if exit.success() {
                        return Ok(());
                    }
                    if decision["agent"]["state"] == "Failed" {
                        return Err(
                            "persistent restart allowance exhausted after transport failure".into(),
                        );
                    }
                    break;
                }
                drop(process);
                record(
                    &core,
                    &spec.agent_id,
                    token,
                    attempts,
                    json!({"event":"stopped","attempt":attempts,"reason":"transport detached; no automatic restart"}),
                )?;
                return Ok(());
            }
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let peer = rustix::net::sockopt::socket_peercred(&stream)?;
                    if peer.uid != rustix::process::geteuid() {
                        continue;
                    }
                    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
                    let mut line = String::new();
                    // One byte reader avoids consuming ACP bytes after attach handshake.
                    let mut reader = BufReader::with_capacity(1, &mut stream);
                    if reader.by_ref().take(4097).read_line(&mut line).is_err() || line.len() > 4096
                    {
                        continue;
                    }
                    let request: Value = match serde_json::from_str(&line) {
                        Ok(r) => r,
                        Err(_) => continue,
                    };
                    let op = request["op"].as_str().unwrap_or("");
                    if matches!(op, "stop" | "pause" | "finish" | "terminate") {
                        if op == "terminate" {
                            record(
                                &core,
                                &spec.agent_id,
                                token,
                                attempts,
                                json!({"event":"cancelling","reason":"explicit owner termination"}),
                            )?;
                        }
                        process.stop()?;
                        drop(process);
                        record(
                            &core,
                            &spec.agent_id,
                            token,
                            attempts,
                            json!({"event":match op {"finish"=>"completed","terminate"=>"terminated",_=>"stopped"},"attempt":attempts,"reason":"explicit owner control"}),
                        )?;
                        let _ = writeln!(stream, "{{\"ok\":true}}");
                        return Ok(());
                    }
                    if op == "status" {
                        // A disconnected observer must not terminate the owner.
                        let _ = writeln!(
                            stream,
                            "{}",
                            json!({"ok":true,"pid":process.child.id(),"pid_kind":if spec.sandbox.is_some(){"container_guardian"}else{"native_process"},"attached":process.transport.is_some(),"container":spec.sandbox.as_ref().map(|_|sandbox::name(&spec.agent_id)),"registry":call(&core,&spec.agent_id,"runtime_status")?})
                        );
                    } else if op == "attach" && process.transport.is_none() {
                        if writeln!(
                            stream,
                            "{}",
                            json!({"ok":true,"pid":process.child.id(),"pid_kind":if spec.sandbox.is_some(){"container_guardian"}else{"native_process"},"agent_id":spec.agent_id,"cwd":spec.cwd})
                        ).is_err() { continue; }
                        stream.set_read_timeout(None)?;
                        let mut incoming = stream.try_clone()?;
                        let mut outgoing = stream.try_clone()?;
                        let mut stdin = process.child.stdin.take().ok_or("stdin unavailable")?;
                        let mut stdout = process.child.stdout.take().ok_or("stdout unavailable")?;
                        let flag = disconnected.clone();
                        thread::spawn(move || {
                            let cause = if relay(&mut incoming, &mut stdin).is_ok() {
                                1
                            } else {
                                2
                            };
                            let _ =
                                flag.compare_exchange(0, cause, Ordering::SeqCst, Ordering::SeqCst);
                        });
                        let flag = disconnected.clone();
                        thread::spawn(move || {
                            let _ = relay(&mut stdout, &mut outgoing);
                            let _ = flag.compare_exchange(0, 2, Ordering::SeqCst, Ordering::SeqCst);
                            let _ = outgoing.shutdown(Shutdown::Both);
                        });
                        process.transport = Some(stream);
                    } else {
                        let _ = writeln!(stream, "{{\"ok\":false}}");
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
                Err(e) => return Err(e.into()),
            }
            thread::sleep(Duration::from_millis(20));
        }
        drop(process);
        thread::sleep(Duration::from_millis(spec.restart_delay_ms));
    }
}

#[cfg(test)]
mod reboot_lock_tests {
    use super::*;
    #[test]
    fn runtime_lock_proof_is_current_and_invalid_after_a_new_lifecycle() {
        let mut events = json!([{"object_id":"agent-a","kind":"runtime_lifecycle","payload":{"detail":{"event":"host_reboot_observed","boot_id":"boot-new"}}}]);
        assert!(reboot_lock_proven(&events, "agent-a", "boot-new"));
        assert!(!reboot_lock_proven(&events, "agent-b", "boot-new"));
        assert!(!reboot_lock_proven(&events, "agent-a", "boot-old"));
        assert!(!reboot_lock_proven(&json!([]), "agent-a", "boot-new"));
        events.as_array_mut().unwrap().push(json!({"object_id":"agent-a","kind":"runtime_lifecycle","payload":{"detail":{"event":"starting"}}}));
        assert!(!reboot_lock_proven(&events, "agent-a", "boot-new"));
        let old_terminal = json!([{"seq":1,"kind":"host_boot","payload":{"boot_id":"boot-old"}},{"seq":2,"object_id":"agent-a","kind":"runtime_lifecycle","payload":{"detail":{"event":"failed"}}},{"seq":3,"kind":"host_boot","payload":{"boot_id":"boot-new"}}]);
        assert!(reboot_lock_proven(&old_terminal, "agent-a", "boot-new"));
        assert!(!reboot_lock_proven(&old_terminal, "agent-a", "boot-old"));
    }
}
