//! Owner-delegated cgroups v2 for foreground model backends. No unbounded fallback.
use crate::AnyResult;
use kane_core::HostResources;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    thread,
    time::Duration,
};
const CGROUP2: i64 = 0x6367_7270;
pub struct ModelCgroup {
    path: PathBuf,
}
fn real_directory(path: &Path) -> AnyResult<()> {
    if !path.is_absolute()
        || fs::canonicalize(path)? != path
        || !path.starts_with("/sys/fs/cgroup")
        || !fs::metadata(path)?.is_dir()
        || rustix::fs::statfs(path)?.f_type != CGROUP2
    {
        return Err("actual canonical delegated cgroups v2 directory required".into());
    }
    Ok(())
}
impl ModelCgroup {
    pub fn create(root: &Path, id: &str, r: HostResources) -> AnyResult<Self> {
        real_directory(root)?;
        if !id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            || id.is_empty()
            || [r.cpu_millis, r.memory_bytes, r.pids].contains(&0)
        {
            return Err("bounded model identity and limits required".into());
        }
        let available = fs::read_to_string(root.join("cgroup.controllers"))?;
        if !["cpu", "memory", "pids"]
            .iter()
            .all(|x| available.split_whitespace().any(|v| v == *x))
        {
            return Err("model CPU/memory/PIDs controllers unavailable".into());
        }
        fs::write(root.join("cgroup.subtree_control"), "+cpu +memory +pids")?;
        let path = root.join(format!("model-{id}"));
        fs::create_dir(&path)?;
        let guard = Self { path };
        fs::write(
            guard.path.join("cpu.max"),
            format!(
                "{} 100000",
                r.cpu_millis.checked_mul(100).ok_or("CPU overflow")?
            ),
        )?;
        fs::write(guard.path.join("memory.max"), r.memory_bytes.to_string())?;
        fs::write(guard.path.join("memory.swap.max"), "0")?;
        fs::write(guard.path.join("memory.oom.group"), "1")?;
        fs::write(guard.path.join("pids.max"), r.pids.to_string())?;
        let state = guard.snapshot()?;
        if state["cpu.max"] != format!("{} 100000", r.cpu_millis * 100)
            || state["memory.max"]
                .as_str()
                .and_then(|s| s.parse::<u64>().ok())
                != Some(r.memory_bytes)
            || state["pids.max"]
                .as_str()
                .and_then(|s| s.parse::<u64>().ok())
                != Some(r.pids)
        {
            return Err("kernel model limits readback mismatch".into());
        }
        Ok(guard)
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn snapshot(&self) -> AnyResult<Value> {
        snapshot(&self.path)
    }
    pub fn cleanup(&self) -> AnyResult<()> {
        cleanup(&self.path)
    }
}
pub fn snapshot(path: &Path) -> AnyResult<Value> {
    real_directory(path)?;
    let mut result = json!({"mode":"cgroup_v2","path":path,"kernel_enforced":true});
    for name in [
        "cpu.max",
        "cpu.stat",
        "memory.max",
        "memory.current",
        "memory.peak",
        "memory.events",
        "memory.swap.max",
        "memory.oom.group",
        "pids.max",
        "pids.current",
        "pids.events",
        "cgroup.events",
        "cgroup.procs",
    ] {
        result[name] = json!(fs::read_to_string(path.join(name))?.trim());
    }
    Ok(result)
}
pub fn attach(path: &Path) -> AnyResult<()> {
    real_directory(path)?;
    for name in ["cpu.max", "memory.max", "pids.max"] {
        if fs::read_to_string(path.join(name))?
            .split_whitespace()
            .next()
            == Some("max")
        {
            return Err("unbounded model cgroup refused".into());
        }
    }
    fs::write(path.join("cgroup.procs"), "0")?;
    let ids = fs::read_to_string(path.join("cgroup.procs"))?;
    if !ids.lines().any(|x| x == std::process::id().to_string()) {
        return Err("model cgroup membership not established".into());
    }
    Ok(())
}
pub fn cleanup(path: &Path) -> AnyResult<()> {
    if !path.exists() {
        return Ok(());
    }
    real_directory(path)?;
    fs::write(path.join("cgroup.kill"), "1")?;
    for _ in 0..100 {
        if fs::read_to_string(path.join("cgroup.events"))?
            .lines()
            .any(|s| s == "populated 0")
        {
            fs::remove_dir(path)?;
            return Ok(());
        }
        thread::sleep(Duration::from_millis(10));
    }
    Err("model cgroup cleanup unconfirmed; no resource release".into())
}
impl Drop for ModelCgroup {
    fn drop(&mut self) {
        if let Err(error) = self.cleanup() {
            eprintln!("model cgroup retained: {error}");
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ordinary_directories_and_missing_delegation_cannot_disable_limits() {
        let t = tempfile::tempdir().unwrap();
        assert!(
            ModelCgroup::create(
                t.path(),
                "instance",
                HostResources {
                    cpu_millis: 100,
                    memory_bytes: 1024,
                    pids: 2,
                    storage_bytes: 1024
                }
            )
            .is_err()
        );
        assert!(attach(t.path()).is_err());
    }
}
