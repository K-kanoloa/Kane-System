//! Capacity admission for a dedicated, persistent ext4 filesystem. This does
//! not provision mounts or confuse a free-space observation with a hard quota.
use crate::AnyResult;
use serde_json::{Value, json};
use std::{fs, os::unix::fs::MetadataExt, path::Path};

/// Capacity, not availability or a performance guarantee. Honor every visible
/// cgroup ancestor; the kernel may impose a tighter parent than the Agent's.
pub fn host_capacity(path: &Path) -> AnyResult<kane_core::HostResources> {
    let mut cpu = std::thread::available_parallelism()?.get() as u64 * 1000;
    let mem = fs::read_to_string("/proc/meminfo")?;
    let mut memory = mem
        .lines()
        .find_map(|line| {
            line.strip_prefix("MemTotal:")
                .and_then(|v| v.split_whitespace().next()?.parse::<u64>().ok())
        })
        .ok_or("MemTotal missing")?
        .checked_mul(1024)
        .ok_or("memory overflow")?;
    let mut pids = fs::read_to_string("/proc/sys/kernel/pid_max")?
        .trim()
        .parse::<u64>()?;
    let membership = fs::read_to_string("/proc/self/cgroup")?;
    let relative = membership
        .lines()
        .find_map(|l| l.strip_prefix("0::"))
        .ok_or("cgroups v2 required")?
        .trim_start_matches('/');
    if Path::new(relative)
        .components()
        .any(|c| !matches!(c, std::path::Component::Normal(_)))
    {
        return Err("invalid cgroup membership path".into());
    }
    let root = Path::new("/sys/fs/cgroup");
    let own = root.join(relative);
    // Namespaced cgroup mounts can already point at this process's hierarchy.
    let mut dir = if own.is_dir() {
        own
    } else {
        root.to_path_buf()
    };
    loop {
        if let Ok(value) = fs::read_to_string(dir.join("cpu.max")) {
            let v: Vec<_> = value.split_whitespace().collect();
            if v.len() == 2 && v[0] != "max" {
                let period = v[1].parse::<u64>()?;
                if period == 0 {
                    return Err("zero CPU quota period".into());
                }
                cpu = cpu.min(
                    v[0].parse::<u64>()?
                        .checked_mul(1000)
                        .ok_or("CPU quota overflow")?
                        / period,
                );
            }
        }
        for (file, limit) in [("memory.max", &mut memory), ("pids.max", &mut pids)] {
            if let Ok(value) = fs::read_to_string(dir.join(file))
                && value.trim() != "max"
            {
                *limit = (*limit).min(value.trim().parse()?);
            }
        }
        if dir == root {
            break;
        }
        if !dir.pop() || !dir.starts_with(root) {
            return Err("cgroup ancestor escaped".into());
        }
    }
    let stats = rustix::fs::statfs(path)?;
    let storage = stats
        .f_blocks
        .checked_mul(u64::try_from(stats.f_bsize)?)
        .ok_or("storage overflow")?;
    Ok(kane_core::HostResources {
        cpu_millis: cpu,
        memory_bytes: memory,
        pids,
        storage_bytes: storage,
    })
}

pub fn validate_host_capacity(
    path: &Path,
    policy: &kane_core::HostResourcePolicy,
) -> AnyResult<()> {
    let actual = host_capacity(path)?;
    let cap = policy.capacity;
    if cap.cpu_millis > actual.cpu_millis
        || cap.memory_bytes > actual.memory_bytes
        || cap.pids > actual.pids
        || cap.storage_bytes > actual.storage_bytes
    {
        return Err("declared host capacity exceeds probed CPU/memory/PIDs/storage".into());
    }
    Ok(())
}

pub fn check(path: &Path, max_bytes: u64) -> AnyResult<Value> {
    if !path.is_absolute() || fs::canonicalize(path)? != path || max_bytes == 0 {
        return Err("canonical storage mount and positive limit required".into());
    }
    let meta = fs::symlink_metadata(path)?;
    let parent = fs::metadata(path.parent().ok_or("storage parent required")?)?;
    if !meta.is_dir()
        || meta.dev() == parent.dev()
        || meta.uid() != rustix::process::geteuid().as_raw()
        || meta.mode() & 0o077 != 0
    {
        return Err("private owned dedicated filesystem mount required".into());
    }
    let stats = rustix::fs::statfs(path)?;
    // First reference profile only. tmpfs is bounded but not persistent.
    if stats.f_type != 0xef53 {
        return Err("persistent ext4 capacity profile required".into());
    }
    let block_size = u64::try_from(stats.f_bsize)?;
    let capacity = stats
        .f_blocks
        .checked_mul(block_size)
        .ok_or("capacity overflow")?;
    if capacity == 0 || capacity > max_bytes {
        return Err("filesystem capacity exceeds declared storage limit".into());
    }
    Ok(
        json!({"enforcement":"dedicated-ext4-capacity","capacity_bytes":capacity,
        "limit_bytes":max_bytes,"device":meta.dev(),"inode":meta.ino(),
        "scope":"allocated filesystem blocks; sparse logical file sizes are not bounded"}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn host_probe_rejects_overstated_capacity() {
        let root = tempfile::tempdir().unwrap();
        let actual = host_capacity(root.path()).unwrap();
        assert!(
            actual.cpu_millis > 0
                && actual.memory_bytes > 0
                && actual.pids > 0
                && actual.storage_bytes > 0
        );
        let mut policy = kane_core::HostResourcePolicy {
            capacity: actual,
            reserved: Default::default(),
        };
        validate_host_capacity(root.path(), &policy).unwrap();
        policy.capacity.memory_bytes += 1;
        assert!(validate_host_capacity(root.path(), &policy).is_err());
    }
    #[test]
    fn ordinary_directory_or_symlink_is_not_a_storage_limit() {
        let root = tempfile::tempdir().unwrap();
        assert!(check(root.path(), u64::MAX).is_err());
        let link = root.path().join("alias");
        std::os::unix::fs::symlink(root.path(), &link).unwrap();
        assert!(check(&link, u64::MAX).is_err());
        assert!(check(Path::new("relative"), 1024).is_err());
    }
}
