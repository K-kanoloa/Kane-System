//! Private Unix IPC for the trusted host control plane, not Agent sandboxes.
//! Blocks in accept while idle. A bounded request is serviced transactionally.
//! SIGTERM uses OS termination: SQLite recovers atomic transactions on restart.
use crate::{AnyResult, host, open_db};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

const MAX_REQUEST: usize = 2_000_000;
const MAX_RESPONSE: usize = 16_000_000;

/// /run locks disappear on a real reboot. Recreate only with a caller's
/// authoritative earlier-boot proof, retaining the SAME private lock pathname
/// and exclusive kernel ownership. Physical absence must still be reconciled.
pub(crate) fn recovery_lock(path: &Path, reboot_proven: bool) -> AnyResult<File> {
    let directory = path.parent().ok_or("lock directory")?;
    let parent = fs::metadata(directory)?;
    if !parent.is_dir() || parent.mode() & 0o077 != 0 || fs::canonicalize(directory)? != directory {
        return Err("canonical private recovery lock directory required".into());
    }
    let options = || {
        let mut o = OpenOptions::new();
        o.read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(i32::try_from(rustix::fs::OFlags::NOFOLLOW.bits()).expect("O_NOFOLLOW"));
        o
    };
    let file = if reboot_proven {
        match options().create_new(true).open(path) {
            Ok(file) => {
                // Root Owner maintenance must leave the lock usable by the
                // dedicated service UID. Change the OPEN fd, never a raced path.
                if file.metadata()?.uid() != parent.uid() {
                    rustix::fs::fchown(
                        &file,
                        Some(rustix::process::Uid::from_raw(parent.uid())),
                        Some(rustix::process::Gid::from_raw(parent.gid())),
                    )?;
                }
                file
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => options().open(path)?,
            Err(e) => return Err(e.into()),
        }
    } else {
        options().open(path)?
    };
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != parent.uid()
        || metadata.mode() & 0o077 != 0
    {
        return Err("private regular recovery lock owned by service required".into());
    }
    file.try_lock()?;
    Ok(file)
}

fn read_exact_until(
    stream: &mut UnixStream,
    mut bytes: &mut [u8],
    deadline: Instant,
) -> AnyResult<()> {
    while !bytes.is_empty() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("IPC read deadline exceeded")?;
        stream.set_read_timeout(Some(remaining))?;
        match stream.read(bytes) {
            Ok(0) => return Err("incomplete IPC frame".into()),
            Ok(count) => bytes = &mut bytes[count..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn read_frame(stream: &mut UnixStream, limit: usize, timeout: Duration) -> AnyResult<Value> {
    let deadline = Instant::now() + timeout;
    let mut header = [0u8; 4];
    read_exact_until(stream, &mut header, deadline)?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > limit {
        return Err("IPC frame exceeds limit".into());
    }
    let mut bytes = vec![0; size];
    read_exact_until(stream, &mut bytes, deadline)?;
    Ok(serde_json::from_slice(&bytes)?)
}

fn write_frame(
    stream: &mut UnixStream,
    value: &Value,
    limit: usize,
    timeout: Duration,
) -> AnyResult<()> {
    let bytes = serde_json::to_vec(value)?;
    if bytes.len() > limit {
        return Err("IPC response exceeds limit".into());
    }
    let deadline = Instant::now() + timeout;
    let frame = [&(bytes.len() as u32).to_be_bytes()[..], &bytes].concat();
    let mut remaining_bytes = frame.as_slice();
    while !remaining_bytes.is_empty() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("IPC write deadline exceeded")?;
        stream.set_write_timeout(Some(remaining))?;
        match stream.write(remaining_bytes) {
            Ok(0) => return Err("incomplete IPC write".into()),
            Ok(count) => remaining_bytes = &remaining_bytes[count..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn error_response(error: &(dyn std::error::Error + 'static)) -> Value {
    // No filesystem paths, request payloads, SQL or tool output in responses/logs.
    let (status, code) = match error.downcast_ref::<kane_core::Error>() {
        Some(kane_core::Error::Conflict) => (409, "system_version_conflict"),
        Some(kane_core::Error::NotFound(_)) => (404, "system_object_not_found"),
        Some(kane_core::Error::Denied(_)) => (403, "system_permission_denied"),
        Some(kane_core::Error::Reconciliation) => (409, "system_reconciliation_required"),
        Some(kane_core::Error::Database(_) | kane_core::Error::Io(_)) => {
            (503, "system_database_unavailable")
        }
        _ => (400, "system_request_rejected"),
    };
    json!({"protocol":1,"ok":false,"error":{"status":status,"code":code}})
}

struct SocketGuard(PathBuf);
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

pub fn serve(database: &Path, socket: &Path) -> AnyResult<()> {
    if !database.is_absolute() || !socket.is_absolute() {
        return Err("service DB and socket paths must be absolute".into());
    }
    let parent = socket.parent().ok_or("socket parent required")?;
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err("socket parent must be an existing private directory (0700)".into());
    }
    crate::prepare_db(database)?;
    // Lock follows the database rather than socket path: a second socket cannot
    // start another service for this database. Keep this inode across restarts.
    let lock_path = database.with_added_extension("service-lock");
    if fs::symlink_metadata(&lock_path).is_ok_and(|m| !m.is_file() || m.file_type().is_symlink()) {
        return Err("service lock must be a regular file".into());
    }
    let lock: File = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(lock_path)?;
    lock.try_lock()
        .map_err(|_| "another core service owns this database")?;
    // Registry::open may migrate schema or initialize WAL. Those writes must
    // not run while an offline checkpoint/recovery coordinator owns this lock.
    let mut registry = open_db(database)?;
    if let Ok(metadata) = fs::symlink_metadata(socket) {
        if !metadata.file_type().is_socket() {
            return Err("refusing to replace non-socket path".into());
        }
        // Holding the DB lock does not establish ownership of an arbitrary live
        // socket; never unlink a socket another live service is listening on.
        if UnixStream::connect(socket).is_ok() {
            return Err("socket is already in use".into());
        }
        fs::remove_file(socket)?;
    }
    #[cfg(target_os = "linux")]
    registry.recover_host_boot(
        &kane_core::Actor::Core,
        fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim(),
    )?;
    registry.recover_actions(&kane_core::Actor::Core)?;
    registry.recover_model_queue(&kane_core::Actor::Core)?;
    let listener = UnixListener::bind(socket)?;
    let _guard = SocketGuard(socket.to_owned());
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600))?;
    eprintln!("kane-core ready; private Owner IPC v1; system acceptance incomplete");
    for connection in listener.incoming() {
        let mut stream = connection?;
        // Filesystem mode is the first boundary. Also verify the kernel-supplied
        // connecting UID, so accidentally widened socket permissions do not
        // turn another local account into Owner. Same-UID code remains trusted.
        #[cfg(target_os = "linux")]
        {
            let peer = rustix::net::sockopt::socket_peercred(&stream)?;
            if peer.uid != rustix::process::geteuid() && peer.uid.as_raw() != 0 {
                eprintln!("kane-core: local peer denied");
                continue;
            }
        }
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let response = (|| -> AnyResult<Value> {
            let envelope = read_frame(&mut stream, MAX_REQUEST, Duration::from_secs(2))?;
            if envelope["protocol"] != 1 { return Err("unsupported IPC protocol".into()); }
            Ok(json!({"protocol":1,"ok":true,"result":host::request(&mut registry, envelope["request"].clone())?}))
        })().unwrap_or_else(|error| error_response(error.as_ref()));
        // A disconnected/malformed client must not stop the service or cause a
        // committed operation to run again. The caller must inspect its outcome.
        if write_frame(&mut stream, &response, MAX_RESPONSE, Duration::from_secs(2)).is_err() {
            eprintln!("kane-core: response not delivered; inspect state before retrying mutation");
        }
    }
    Ok(())
}

pub fn request(socket: &Path, request: &Value) -> AnyResult<Value> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(Duration::from_secs(15)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    write_frame(
        &mut stream,
        &json!({"protocol":1,"request":request}),
        MAX_REQUEST,
        Duration::from_secs(5),
    )?;
    let response = read_frame(&mut stream, MAX_RESPONSE, Duration::from_secs(15))?;
    if response["protocol"] != 1 {
        return Err("unsupported IPC protocol".into());
    }
    if response["ok"] == true {
        return Ok(response["result"].clone());
    }
    Err(format!(
        "core rejected: {}",
        response["error"]["code"]
            .as_str()
            .unwrap_or("system_request_rejected")
    )
    .into())
}

#[cfg(test)]
mod recovery_lock_tests {
    use super::*;
    #[test]
    fn lost_run_lock_requires_boot_proof_and_still_excludes_another_manager() {
        let directory = tempfile::tempdir().unwrap();
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = directory.path().join("instance.model.lock");
        assert!(recovery_lock(&path, false).is_err());
        assert!(!path.exists());
        let held = recovery_lock(&path, true).unwrap();
        assert!(recovery_lock(&path, true).is_err());
        drop(held);
        assert!(recovery_lock(&path, false).is_ok());
    }
    #[test]
    fn recovery_never_follows_symlinks_or_shared_lock_inodes() {
        let directory = tempfile::tempdir().unwrap();
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let target = directory.path().join("target");
        fs::write(&target, b"unchanged").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        let path = directory.path().join("instance.runtime.lock");
        std::os::unix::fs::symlink(&target, &path).unwrap();
        assert!(recovery_lock(&path, true).is_err());
        fs::remove_file(&path).unwrap();
        fs::hard_link(&target, &path).unwrap();
        assert!(recovery_lock(&path, true).is_err());
        assert_eq!(fs::read(target).unwrap(), b"unchanged");
    }
}
