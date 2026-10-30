#![forbid(unsafe_code)]
#[cfg(target_os = "linux")]
mod checkpoint;
mod config;
mod container;
mod host;
#[cfg(target_os = "linux")]
mod model_gateway;
#[cfg(target_os = "linux")]
mod model_limits;
#[cfg(target_os = "linux")]
mod model_runtime;
#[cfg(target_os = "linux")]
mod native_os;
#[cfg(target_os = "linux")]
mod runtime;
#[cfg(target_os = "linux")]
mod sandbox;
#[cfg(unix)]
mod service;
#[cfg(target_os = "linux")]
mod storage;
use clap::{Parser, Subcommand};
use kane_core::*;
use kane_models::{Backend, DeterministicMock, Request};
use serde_json::{Value, json};
use std::{
    error::Error as StdError,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

type AnyResult<T> = std::result::Result<T, Box<dyn StdError>>;

#[derive(Parser)]
#[command(
    name = "kanesys",
    version,
    about = "Experimental Kane System host CLI. No installable OS release."
)]
struct Cli {
    #[arg(
        long,
        default_value = ".kane/state.db",
        global = true,
        conflicts_with = "config"
    )]
    db: PathBuf,
    /// Parse root-managed DB/socket paths as literal data, never shell commands.
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    /// Private Owner socket. Configuring it never falls back to direct DB access.
    #[arg(long, global = true, conflicts_with = "config")]
    socket: Option<PathBuf>,
    #[command(subcommand)]
    command: Commands,
}
#[derive(Subcommand)]
enum Commands {
    #[cfg(target_os = "linux")]
    /// Native Linux installation and signed system payload inspection.
    Os {
        #[command(subcommand)]
        command: native_os::OsCommand,
    },
    /// Check an existing dedicated persistent storage mount; does not provision it.
    StorageCheck {
        #[arg(long)]
        path: PathBuf,
        #[arg(long)]
        max_bytes: u64,
    },
    /// Own an existing ACP runtime; optional validated Linux container profile.
    Supervise {
        spec: PathBuf,
    },
    /// Reconcile only an absent owned sandbox and its host reservation; no Task replay.
    ReconcileRuntime {
        #[arg(long)]
        agent: String,
    },
    #[command(hide = true)]
    /// Internal model exec guard; joins an already bounded delegated cgroup.
    ModelExec {
        #[arg(long)]
        parent: u32,
        #[arg(long)]
        cgroup: PathBuf,
        #[arg(last = true, required = true)]
        command: Vec<String>,
    },
    RuntimeExec {
        #[arg(long)]
        parent: u32,
        #[arg(last = true)]
        command: Vec<String>,
    },
    #[command(hide = true)]
    RuntimeContainer {
        #[arg(long)]
        parent: u32,
        #[arg(long)]
        spec: PathBuf,
    },
    /// Deterministic model conformance transport; not an Agent or real provider.
    #[command(hide = true)]
    MockInfer,
    /// Long-running core; local Unix socket only, never an Agent endpoint.
    Serve,
    /// Owner-managed local model process lifecycle.
    ModelRun {
        spec: PathBuf,
    },
    /// Reclaim model residency only with manager lock and confirmed child absence.
    ModelReconcile {
        instance: String,
    },
    /// Restricted Unix HTTP model transport; requires Core single-use permits.
    ModelGateway {
        path: PathBuf,
    },
    /// Offline quiescent Registry + native Workspace/HOME checkpoint. Stops are explicit.
    Checkpoint {
        #[arg(long)]
        spec: PathBuf,
        destination: PathBuf,
    },
    /// Validate a published native-data checkpoint without activating or replaying it.
    CheckpointCheck {
        source: PathBuf,
    },
    /// Restore checked local bytes to a new directory; all nonterminal Tasks remain fenced.
    CheckpointRestore {
        source: PathBuf,
        destination: PathBuf,
    },
    /// Install one verified checkpoint root at its original EMPTY offline mapping.
    CheckpointInstall {
        source: PathBuf,
        #[arg(long)]
        root: String,
        /// Explicit offline disaster recovery only: latest Registry is missing/corrupt.
        #[arg(long)]
        registry_unavailable: bool,
    },
    /// Snapshot Registry and database-managed files to a new private file.
    Backup {
        destination: PathBuf,
    },
    /// Restore a checked database snapshot to a NEW --db path. Never overwrites.
    Restore {
        source: PathBuf,
    },
    /// Three isolated fixture containers; no real model is called or checkpoint resumed.
    ContainerDemo {
        #[arg(long)]
        image: String,
        #[arg(long, default_value = "docker")]
        runtime: String,
    },
    /// Trusted local host IPC; input is one JSON request. Not an Agent tool endpoint.
    HostRequest,
    /// Probe this environment; does not install or certify compatibility.
    Doctor {
        #[arg(long)]
        json: bool,
    },
    /// Initialize a local development database (not system-wide initialization).
    Init,
    Status,
    /// Run a deterministic mock document workflow; never calls a cloud model.
    Demo,
    /// Object inspection; output contains local Task/Artifact data.
    Inspect {
        kind: String,
        id: String,
    },
    Ledger,
    /// Mark orphaned dispatches Unknown. Requires exclusive dispatcher ownership.
    Recover,
    /// Repeat mock workflow N times and include every outcome in a JSON report.
    Benchmark {
        #[arg(long, default_value_t = 5)]
        runs: usize,
    },
    /// Test logical identity preservation across child-process loss.
    LifecycleDemo,
    #[command(hide = true)]
    MockWorker {
        #[arg(long)]
        crash: bool,
    },
}

fn print_json(value: impl serde::Serialize) -> AnyResult<()> {
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}
fn command_output(bin: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(bin)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_owned())
}

fn doctor() -> Value {
    let cgroup = std::fs::read_to_string("/sys/fs/cgroup/cgroup.controllers").ok();
    let runtime = if command_output("podman", &["--version"]).is_some() {
        "podman"
    } else if command_output("docker", &["--version"]).is_some() {
        "docker"
    } else {
        "missing"
    };
    json!({"schema_version":1,"platform":std::env::consts::OS,"arch":std::env::consts::ARCH,
        "cpu_threads_visible":std::thread::available_parallelism().map(|p|p.get()).ok(),
        "os_release":std::fs::read_to_string("/etc/os-release").ok(),
        "kernel":command_output("uname",&["-r"]),
        "memory":std::fs::read_to_string("/proc/meminfo").ok().map(|s|s.lines().filter(|l|l.starts_with("MemTotal:")||l.starts_with("MemAvailable:")).collect::<Vec<_>>().join("\n")),
        "cgroup":{"version":if cgroup.is_some(){2}else{0},"visible_controllers":cgroup,"delegation":"Unknown"},
        "systemd_pid1":std::fs::read_to_string("/proc/1/comm").ok().is_some_and(|s|s.trim()=="systemd"),
        "container_runtime":{"binary":runtime,"access_and_isolation":"Unknown"},
        "kvm_device_exists":Path::new("/dev/kvm").exists(),"accelerators":"Unknown","snapshots":"Unknown",
        "backend":"deterministic-mock only","compatibility":"Unknown",
        "install_gate":if cgroup.is_some()&&runtime!="missing"{"WARN: runtime delegation and systemd require testing"}else{"FAIL: missing cgroups v2 or runtime"}})
}

fn prepare_db(path: &Path) -> AnyResult<()> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty())
        && !parent.exists()
    {
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(parent)?;
        }
        #[cfg(not(unix))]
        std::fs::create_dir_all(parent)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        if !path.exists() {
            std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)?;
        }
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_file()
            || metadata.file_type().is_symlink()
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err("database must be a private regular file (0600), not a symlink".into());
        }
    }
    Ok(())
}

fn open_db(path: &Path) -> AnyResult<Registry> {
    prepare_db(path)?;
    Ok(Registry::open(path)?)
}

fn fixture(r: &mut Registry, goal: &str) -> AnyResult<(Task, Agent)> {
    let owner = Actor::Owner;
    let ws = r.create_workspace(&owner, "document")?;
    let budget = r.create_budget(&owner, 10_000, 1)?;
    let model = new_id("mock-model");
    r.register_model(
        &owner,
        &model,
        json!({"backend":"deterministic-mock-v1","cloud":false}),
    )?;
    r.register_model_instance(&Actor::Core, &model, "local-test")?;
    let t = r.create_task(
        &owner,
        TaskSpec {
            goal: goal.into(),
            acceptance: vec![
                "Markdown document exactly matches the independent fixture".into(),
                "Editable Markdown available".into(),
            ],
            workload: Workload {
                class: WorkloadClass::Execution,
                interruptible: false,
                resumable: Resume::None,
                realtime: false,
                max_memory_bytes: 64 * 1024 * 1024,
                max_storage_bytes: 1024 * 1024,
                checkpoint_verified: false,
            },
            budget_id: budget,
            workspace_id: ws.id,
            cloud_allowed: false,
        },
    )?;
    let a = r.register_agent(&owner, "document-writer", &t.id, &model)?;
    Ok((t, a))
}

fn make_running(r: &mut Registry, t: Task, a: Agent) -> AnyResult<(Task, Agent)> {
    let t = r.transition_task(&Actor::Core, &t.id, t.version, TaskState::Ready, None)?;
    let t = r.transition_task(&Actor::Core, &t.id, t.version, TaskState::Running, None)?;
    let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Ready)?;
    let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Running)?;
    Ok((t, a))
}

fn demo(r: &mut Registry) -> AnyResult<Value> {
    let start = Instant::now();
    let (t, a) = fixture(r, "Kane System editable outcome acceptance fixture")?;
    let (t, a) = make_running(r, t, a)?;
    r.start_session(&Actor::Core, &a.id, "in-process-mock-not-sandboxed")?;
    let actor = Actor::Agent(a.id.clone());
    let cap = r.grant(
        &Actor::Owner,
        &a.id,
        "modify",
        &a.workspace_id,
        now() + 3600,
    )?;
    let mut ws = r.transfer_lease(&Actor::Owner, &a.workspace_id, 0, &a.id, now() + 3600)?;
    let response = DeterministicMock::default().infer(&Request {
        task_id: t.id.clone(),
        input: t.spec.goal.clone(),
        max_tokens: 1000,
    })?;
    r.charge_tokens(&Actor::Core, &t.id, response.tokens)?;
    ws = r.write_file(
        &actor,
        &ws.id,
        ws.lease_epoch,
        ws.version,
        "outcome.md",
        response.content.as_bytes(),
    )?;
    let artifact = r.submit_artifact(&actor, &t.id, "text/markdown", true, &response.content)?;
    let t = r.transition_task(&Actor::Core, &t.id, t.version, TaskState::Verifying, None)?;
    // Independent acceptance uses a fixed expected fixture, not the backend response as its oracle.
    let expected = "# Verified test document\n\nKane System editable outcome acceptance fixture\n";
    let passed = artifact.content == expected
        && r.read_file(&Actor::Owner, &ws.id, "outcome.md")? == expected.as_bytes();
    let t = r.verify(
        &Actor::Verifier("external-fixture-check-v1".into()),
        &t.id,
        t.version,
        passed,
        "exact independent fixture bytes + editable Markdown workspace",
    )?;
    let ws = r.transfer_lease(&Actor::Owner, &ws.id, ws.lease_epoch, "human", now() + 3600)?;
    let stale = r.write_file(
        &actor,
        &ws.id,
        ws.lease_epoch - 1,
        ws.version,
        "outcome.md",
        b"stale overwrite",
    );
    if !matches!(stale, Err(Error::Conflict)) {
        return Err("stale epoch unexpectedly accepted".into());
    }
    r.revoke(&Actor::Owner, &cap)?;
    let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Exited)?;
    r.integrity_check()?;
    Ok(
        json!({"task":t,"agent":a,"artifact":artifact.id,"allocation":allocate_direct(),
        "verified_success":t.state==TaskState::Done,"stale_epoch_rejected":true,"tokens":response.tokens,
        "wall_ms":start.elapsed().as_secs_f64()*1000.0,"backend":"deterministic-mock-v1",
        "scope":"in-process contract test; no sandbox, real model or cross-platform claims"}),
    )
}

fn lifecycle(r: &mut Registry) -> AnyResult<Value> {
    let mut rows = Vec::new();
    for index in 0..3 {
        let (t, a) = fixture(r, &format!("lifecycle fixture {index}"))?;
        let (_, a) = make_running(r, t, a)?;
        let start = Instant::now();
        let mut child = Command::new(std::env::current_exe()?)
            .arg("mock-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .spawn()?;
        let first_pid = child.id();
        r.start_session(&Actor::Core, &a.id, &format!("process:{first_pid}"))?;
        child.kill()?;
        child.wait()?;
        let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Ready)?;
        let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Running)?;
        let mut next = Command::new(std::env::current_exe()?)
            .arg("mock-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .spawn()?;
        let next_pid = next.id();
        let session = r.start_session(&Actor::Core, &a.id, &format!("process:{next_pid}"))?;
        next.kill()?;
        next.wait()?;
        let checked = r.agent(&a.id)?;
        if checked.task_id != a.task_id
            || checked.workspace_id != a.workspace_id
            || checked.budget_id != a.budget_id
        {
            return Err("identity binding changed".into());
        }
        r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Exited)?;
        rows.push(json!({"agent_id":a.id,"task_id":a.task_id,"workspace_id":a.workspace_id,"budget_id":a.budget_id,"old_pid":first_pid,"new_pid":next_pid,"new_session":session,"recovery_ms":start.elapsed().as_secs_f64()*1000.0}));
    }
    Ok(
        json!({"scope":"3 sequential child-process lifecycle tests, not container/cgroups acceptance","rows":rows}),
    )
}

fn run() -> AnyResult<()> {
    let mut cli = Cli::parse();
    if let Some(path) = cli.config.as_deref() {
        let (db, socket) = config::paths(path)?;
        cli.db = db;
        cli.socket = Some(socket);
    }
    match cli.command {
        #[cfg(target_os = "linux")]
        Commands::Os { command } => return native_os::run(command),
        Commands::MockInfer => {
            use std::io::Read;
            let mut input = String::new();
            std::io::stdin()
                .take(1_500_001)
                .read_to_string(&mut input)?;
            if input.len() > 1_500_000 {
                return Err("bounded mock request required".into());
            }
            let request: Request = serde_json::from_str(&input)?;
            return print_json(DeterministicMock::default().infer(&request)?);
        }
        Commands::StorageCheck { path, max_bytes } => {
            #[cfg(target_os = "linux")]
            return print_json(storage::check(&path, max_bytes)?);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::Supervise { spec } => {
            #[cfg(target_os = "linux")]
            return runtime::supervise(
                cli.socket.as_deref().ok_or("supervise requires --socket")?,
                &spec,
            );
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::ReconcileRuntime { agent } => {
            #[cfg(target_os = "linux")]
            return runtime::reconcile(
                cli.socket.as_deref().ok_or("reconcile requires --socket")?,
                &agent,
            );
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::RuntimeContainer { parent, spec } => {
            #[cfg(target_os = "linux")]
            return sandbox::guard(parent, &spec);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::ModelExec {
            parent,
            cgroup,
            command,
        } => {
            #[cfg(target_os = "linux")]
            {
                rustix::process::set_parent_process_death_signal(Some(
                    rustix::process::Signal::KILL,
                ))?;
                model_limits::attach(&cgroup)?;
                return runtime::exec_guard(parent, command);
            }
            #[cfg(not(target_os = "linux"))]
            return Err("Linux model cgroups required".into());
        }
        Commands::RuntimeExec { parent, command } => {
            #[cfg(target_os = "linux")]
            return runtime::exec_guard(parent, command);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::Checkpoint { spec, destination } => {
            #[cfg(target_os = "linux")]
            return print_json(checkpoint::create(&cli.db, &spec, &destination)?);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::CheckpointCheck { source } => {
            #[cfg(target_os = "linux")]
            return print_json(checkpoint::validate(&source)?);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::CheckpointRestore {
            source,
            destination,
        } => {
            #[cfg(target_os = "linux")]
            return print_json(checkpoint::restore(&source, &destination)?);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::CheckpointInstall {
            source,
            root,
            registry_unavailable,
        } => {
            #[cfg(target_os = "linux")]
            return print_json(checkpoint::install(
                &cli.db,
                &source,
                &root,
                registry_unavailable,
            )?);
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::Restore { source } => {
            Registry::restore_new(&Actor::Owner, source, &cli.db)?;
            return print_json(
                json!({"restored":true,"db":cli.db,"scope":"database-managed state only","services_must_be_stopped":true}),
            );
        }
        Commands::ModelReconcile { ref instance } => {
            #[cfg(target_os = "linux")]
            return model_runtime::reconcile(
                cli.socket
                    .as_deref()
                    .ok_or("model-reconcile requires --socket")?,
                instance,
            );
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::ModelRun { ref spec } => {
            #[cfg(target_os = "linux")]
            return model_runtime::run(
                cli.socket.as_deref().ok_or("model-run requires --socket")?,
                spec,
            );
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::ModelGateway { ref path } => {
            #[cfg(target_os = "linux")]
            return model_gateway::serve(
                cli.socket
                    .as_deref()
                    .ok_or("model-gateway requires --socket")?,
                path,
            );
            #[cfg(not(target_os = "linux"))]
            return Err("Linux required".into());
        }
        Commands::Serve => {
            #[cfg(unix)]
            return service::serve(
                &cli.db,
                cli.socket.as_deref().ok_or("serve requires --socket")?,
            );
            #[cfg(not(unix))]
            return Err("system service requires Unix".into());
        }
        Commands::HostRequest => {
            use std::io::Read;
            let mut input = String::new();
            std::io::stdin()
                .take(2_000_001)
                .read_to_string(&mut input)?;
            if input.len() > 2_000_000 {
                return Err("request exceeds 2 MB".into());
            }
            let request: Value = serde_json::from_str(&input)?;
            let result = if let Some(socket) = cli.socket.as_deref() {
                #[cfg(unix)]
                {
                    service::request(socket, &request)?
                }
                #[cfg(not(unix))]
                {
                    return Err("Unix socket required".into());
                }
            } else {
                let mut r = open_db(&cli.db)?;
                host::request(&mut r, request)?
            };
            return print_json(result);
        }
        Commands::Doctor { json: as_json } => {
            let profile = doctor();
            if as_json {
                print_json(profile)?
            } else {
                println!(
                    "Kane System development probe\n{}",
                    serde_json::to_string_pretty(&profile)?
                );
            }
            return Ok(());
        }
        Commands::MockWorker { crash } => {
            if crash {
                std::process::exit(7)
            }
            std::thread::sleep(Duration::from_secs(30));
            return Ok(());
        }
        _ => {}
    }
    let mut r = open_db(&cli.db)?;
    match cli.command {
        Commands::Backup { destination } => {
            r.backup(&Actor::Owner, &destination)?;
            print_json(json!({"snapshot":destination,"scope":"database-managed state only"}))?;
        }
        Commands::Init => print_json(
            json!({"initialized":true,"db":cli.db,"level":"experimental local registry","system_install":false}),
        )?,
        Commands::ContainerDemo { image, runtime } => {
            let runtime = container::ContainerRuntime {
                executable: runtime,
            };
            let mut owned: Vec<(String, String)> = Vec::new();
            let result = (|| -> AnyResult<Value> {
                let mut rows = Vec::new();
                for index in 0..3 {
                    let (t, a) = fixture(
                        &mut r,
                        &format!("independent isolated lifecycle fixture {index}"),
                    )?;
                    let (_, a) = make_running(&mut r, t, a)?;
                    let cid = runtime.create_fixture(&a.id, &image)?;
                    owned.push((cid.clone(), a.id.clone()));
                    let inspect = runtime.inspect(&cid, &a.id)?;
                    if inspect["State"]["Running"] != true {
                        return Err("fixture container did not remain alive".into());
                    }
                    r.start_session(&Actor::Core, &a.id, &cid)?;
                    rows.push(json!({"agent":a,"container_id":cid,"memory_bytes":inspect["HostConfig"]["Memory"],"pids_limit":inspect["HostConfig"]["PidsLimit"],"network":inspect["HostConfig"]["NetworkMode"],"readonly_root":inspect["HostConfig"]["ReadonlyRootfs"]}));
                }
                let old_agent: Agent = serde_json::from_value(rows[0]["agent"].clone())?;
                let first = rows[0]["container_id"]
                    .as_str()
                    .ok_or("container id missing")?;
                let start = Instant::now();
                runtime.kill_fixture(first, &old_agent.id)?;
                runtime.remove_fixture(first, &old_agent.id)?;
                owned.remove(0);
                let a = r.transition_agent(
                    &Actor::Core,
                    &old_agent.id,
                    old_agent.version,
                    AgentState::Ready,
                )?;
                let cid = runtime.create_fixture(&a.id, &image)?;
                owned.push((cid.clone(), a.id.clone()));
                let a = r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Running)?;
                r.start_session(&Actor::Core, &a.id, &cid)?;
                if a.task_id != old_agent.task_id
                    || a.workspace_id != old_agent.workspace_id
                    || a.budget_id != old_agent.budget_id
                    || cid == first
                {
                    return Err("stable binding violated".into());
                }
                Ok(
                    json!({"fixture":"existing private Tool worker; no new Agent reasoning loop","agents":rows,"restart":{"agent_id":a.id,"old_container":first,"new_container":cid,"recovery_ms":start.elapsed().as_secs_f64()*1000.0,"bindings_preserved":true},"scope":"runtime fixture only; no native Agent recovery or isolation certification"}),
                )
            })();
            let mut cleanup_errors = Vec::new();
            for (cid, agent) in owned {
                if let Err(e) = runtime.remove_fixture(&cid, &agent) {
                    cleanup_errors.push(e.to_string());
                } else if let Ok(a) = r.agent(&agent)
                    && a.state == AgentState::Running
                {
                    r.transition_agent(&Actor::Core, &a.id, a.version, AgentState::Exited)?;
                    let task = r.task(&a.task_id)?;
                    if !task.state.terminal() {
                        r.transition_task(
                            &Actor::Core,
                            &task.id,
                            task.version,
                            TaskState::Cancelled,
                            None,
                        )?;
                    }
                }
            }
            if !cleanup_errors.is_empty() {
                return Err(format!("container cleanup failed: {cleanup_errors:?}").into());
            }
            print_json(result?)?;
        }
        Commands::Status => print_json(
            json!({"tasks":r.tasks()?,"release":"0.3.0-dev","system_phase":"incomplete","compatibility":"Unknown"}),
        )?,
        Commands::Demo => print_json(demo(&mut r)?)?,
        Commands::Inspect { kind, id } => match kind.as_str() {
            "task" => print_json(r.task(&id)?)?,
            "agent" => print_json(r.agent(&id)?)?,
            "workspace" => print_json(r.workspace(&id)?)?,
            "artifacts" => print_json(r.artifacts(&id)?)?,
            _ => return Err("expected task|agent|workspace|artifacts".into()),
        },
        Commands::Ledger => print_json(r.events()?)?,
        Commands::Recover => {
            print_json(json!({"unknown_actions":r.recover_actions(&Actor::Owner)?}))?
        }
        Commands::LifecycleDemo => print_json(lifecycle(&mut r)?)?,
        Commands::Benchmark { runs } => {
            if runs < 5 {
                return Err("N must be >= 5".into());
            }
            let mut results = Vec::new();
            for _ in 0..runs {
                results.push(match demo(&mut r) {
                    Ok(v) => v,
                    Err(e) => json!({"verified_success":false,"error":e.to_string()}),
                });
            }
            let succeeded = results
                .iter()
                .filter(|v| v["verified_success"] == true)
                .count();
            print_json(
                json!({"environment":doctor(),"config":"Direct","backend":"deterministic-mock-v1","runs":runs,"verified_success":succeeded,"results":results,
                "conclusion":"mock contract workflow only; no real model quality, multi-Agent net benefit or compatibility claim"}),
            )?;
            if succeeded != runs {
                return Err("benchmark contains failed runs".into());
            }
        }
        #[cfg(target_os = "linux")]
        Commands::Os { .. } => unreachable!(),
        Commands::Supervise { .. }
        | Commands::ReconcileRuntime { .. }
        | Commands::StorageCheck { .. }
        | Commands::ModelExec { .. }
        | Commands::RuntimeExec { .. }
        | Commands::RuntimeContainer { .. }
        | Commands::Serve
        | Commands::MockInfer
        | Commands::ModelRun { .. }
        | Commands::ModelReconcile { .. }
        | Commands::ModelGateway { .. }
        | Commands::Doctor { .. }
        | Commands::MockWorker { .. }
        | Commands::HostRequest
        | Commands::Restore { .. }
        | Commands::Checkpoint { .. }
        | Commands::CheckpointCheck { .. }
        | Commands::CheckpointInstall { .. }
        | Commands::CheckpointRestore { .. } => {
            unreachable!()
        }
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("kanesys: {e}");
        std::process::exit(1);
    }
}
