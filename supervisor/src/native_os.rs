//! Native OS delivery boundary. Reuses the existing Core; never replays Tasks.
use crate::{AnyResult, print_json};
use clap::Subcommand;
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Subcommand)]
pub enum OsCommand {
    /// Verify metadata signature and every payload byte. Does not install.
    Verify {
        bundle: PathBuf,
        #[arg(long)]
        key: PathBuf,
    },
    /// Print the fixed reference layout. No disk access or mutation.
    Layout,
    /// Install only from the dedicated installer into an explicitly confirmed EMPTY disk.
    Install {
        bundle: PathBuf,
        #[arg(long)]
        disk: PathBuf,
        #[arg(long)]
        erase_confirmation: String,
    },
    /// Stage a signed compatible system into the inactive slot; reboot remains explicit.
    Upgrade { bundle: PathBuf },
    /// Explicitly select the previous compatible signed slot, preserving current data.
    Rollback,
    /// Inspect the installed slot and services. Never resumes an unknown Task.
    Status,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format: u32,
    pub product: String,
    pub version: String,
    pub sequence: u64,
    pub architecture: String,
    pub payload: String,
    pub payload_sha256: String,
    pub payload_bytes: u64,
    pub core_schema_min: u32,
    pub core_schema_max: u32,
    pub source_fingerprint: String,
    pub source_date_epoch: u64,
}
impl Manifest {
    fn validate(&self) -> AnyResult<()> {
        if self.format != 1
            || self.product != "Kane System"
            || self.architecture != "x86_64"
            || self.payload != "rootfs.tar.zst"
            || self.payload_bytes == 0
            || self.sequence == 0
            || self.core_schema_min > self.core_schema_max
            || self.core_schema_min == 0
            || self.version.is_empty()
            || self.version.len() > 64
            || !self
                .version
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-_".contains(&b))
            || !is_hash(&self.payload_sha256)
            || !is_hash(&self.source_fingerprint)
        {
            return Err("invalid or unsupported signed OS manifest".into());
        }
        Ok(())
    }
}
fn is_hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn output(bin: &str, args: &[&str]) -> AnyResult<String> {
    let result = Command::new(bin)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .output()?;
    if !result.status.success() {
        return Err(format!("{bin} failed: {}", String::from_utf8_lossy(&result.stderr)).into());
    }
    Ok(String::from_utf8(result.stdout)?.trim().to_owned())
}
fn execute_logged(bin: &Path, args: &[&str]) -> AnyResult<()> {
    let status = Command::new(bin)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LC_ALL", "C")
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::inherit())
        .status()?;
    if !status.success() {
        return Err(
            "native OS operation failed; retain console log and inspect before retry".into(),
        );
    }
    Ok(())
}
fn regular(path: &Path) -> AnyResult<()> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_file() || m.file_type().is_symlink() || m.nlink() != 1 {
        return Err("regular non-hardlinked bundle file required".into());
    }
    Ok(())
}
fn text(path: &Path) -> AnyResult<&str> {
    path.to_str().ok_or_else(|| "UTF-8 OS path required".into())
}
fn hash(path: &Path) -> AnyResult<String> {
    regular(path)?;
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let n = f.read(&mut bytes)?;
        if n == 0 {
            break;
        }
        h.update(&bytes[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}
fn signed_metadata(meta: &Path, sig: &Path, key: &Path) -> AnyResult<Manifest> {
    regular(meta)?;
    regular(sig)?;
    regular(key)?;
    if meta.metadata()?.len() > 16384 || sig.metadata()?.len() != 64 {
        return Err("bounded Ed25519 metadata required".into());
    }
    output(
        "/usr/bin/openssl",
        &[
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            text(key)?,
            "-rawin",
            "-in",
            text(meta)?,
            "-sigfile",
            text(sig)?,
        ],
    )?;
    let manifest: Manifest = serde_json::from_slice(&fs::read(meta)?)?;
    manifest.validate()?;
    Ok(manifest)
}
fn verify(bundle: &Path, key: &Path) -> AnyResult<Manifest> {
    let manifest = signed_metadata(
        &bundle.join("manifest.json"),
        &bundle.join("manifest.sig"),
        key,
    )?;
    let payload = bundle.join(&manifest.payload);
    regular(&payload)?;
    if payload.metadata()?.len() != manifest.payload_bytes
        || hash(&payload)? != manifest.payload_sha256
    {
        return Err("OS payload length/hash mismatch; no disk was touched".into());
    }
    Ok(manifest)
}
fn require_root() -> AnyResult<()> {
    if output("/usr/bin/id", &["-u"])? != "0" {
        return Err("local Linux root authority required".into());
    }
    if fs::read_to_string("/proc/1/comm")?.trim() != "systemd" {
        return Err("native Linux systemd host required".into());
    }
    Ok(())
}
fn operation_lock() -> AnyResult<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(i32::try_from(rustix::fs::OFlags::NOFOLLOW.bits())?)
        .open("/run/kanesys-os.lock")?;
    if file.metadata()?.uid() != 0 || file.metadata()?.nlink() != 1 {
        return Err("invalid OS operation lock".into());
    }
    file.try_lock()?;
    Ok(file)
}
fn installed() -> AnyResult<()> {
    require_root()?;
    if fs::read_to_string("/usr/share/kane-system/role")?.trim() != "installed" {
        return Err("installed native OS required".into());
    }
    if output("/usr/bin/findmnt", &["-n", "-o", "FSTYPE", "/"])? != "btrfs" {
        return Err("reference btrfs system required".into());
    }
    Ok(())
}
fn compatible(m: &Manifest) -> AnyResult<()> {
    let config = Path::new("/etc/kane-system/core.env");
    trusted(config)?;
    let (path, _) = crate::config::paths(config)?;
    compatible_database(m, &path)
}
fn compatible_database(m: &Manifest, path: &Path) -> AnyResult<()> {
    // Read-only schema inspection never opens/migrates via an older Registry.
    regular(path)?;
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let schema: u32 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if !(m.core_schema_min..=m.core_schema_max).contains(&schema) {
        return Err("data schema incompatible: preserve data and refuse slot switch".into());
    }
    Ok(())
}
/// Once verification starts no untrusted principal may replace any input ancestor/file.
fn trusted(path: &Path) -> AnyResult<()> {
    let canonical = fs::canonicalize(path)?;
    if canonical != path {
        return Err("canonical root-owned path required".into());
    }
    for p in path.ancestors() {
        let m = fs::symlink_metadata(p)?;
        if m.uid() != 0 || m.mode() & 0o022 != 0 || m.file_type().is_symlink() {
            return Err(
                "OS inputs and ancestors must be root-owned, not group/world writable".into(),
            );
        }
    }
    Ok(())
}
fn check_disk(disk: &Path, confirmation: &str) -> AnyResult<()> {
    if confirmation != format!("ERASE:{}", disk.display()) {
        return Err("explicit exact disk erase confirmation required".into());
    }
    if fs::canonicalize(disk)? != disk
        || !disk.starts_with("/dev")
        || !fs::metadata(disk)?.file_type().is_block_device()
    {
        return Err("canonical whole block device required".into());
    }
    let dev = text(disk)?;
    if output("/usr/bin/lsblk", &["-dnro", "TYPE", dev])? != "disk" {
        return Err("whole disk required".into());
    }
    if !output("/usr/bin/lsblk", &["-nro", "MOUNTPOINTS", dev])?.is_empty()
        || !disk_signatures(disk)?.is_empty()
        || output("/usr/bin/lsblk", &["-nro", "NAME", dev])?
            .lines()
            .count()
            != 1
    {
        return Err(
            "disk must be empty and unmounted; data disks are never silently erased".into(),
        );
    }
    let sectors: u64 = output("/usr/sbin/blockdev", &["--getsize64", dev])?.parse()?;
    if sectors < 12 * 1024 * 1024 * 1024 {
        return Err("reference installation requires a blank disk >=12 GiB".into());
    }
    let name = disk.file_name().ok_or("disk name")?;
    if fs::read_dir(Path::new("/sys/class/block").join(name).join("holders"))?
        .next()
        .is_some()
    {
        return Err("disk has active holders".into());
    }
    // Reject swap even when it has no filesystem mountpoint.
    if fs::read_to_string("/proc/swaps")?
        .lines()
        .skip(1)
        .any(|line| line.split_whitespace().next() == Some(dev))
    {
        return Err("active swap disk rejected".into());
    }
    Ok(())
}
fn disk_signatures(path: &Path) -> AnyResult<String> {
    // util-linux -o is OFFSET. Use the long OUTPUT option unambiguously.
    output(
        "/usr/sbin/wipefs",
        &["--no-act", "--noheadings", "--output", "TYPE", text(path)?],
    )
}
pub fn run(command: OsCommand) -> AnyResult<()> {
    match command {
        OsCommand::Layout => print_json(
            json!({"format":1,"architecture":"x86_64","firmware":"BIOS reference; UEFI unverified","disk_min_bytes":12u64*1024*1024*1024,"partitions":["GPT BIOS boot 2MiB","EFI reserved 256MiB","btrfs remaining"],"roots":["@slots/A","@slots/B"],"shared":"@data","core_schema":9,"task_replay_on_boot":false,"status":"implementation requires real installation acceptance"}),
        ),
        OsCommand::Verify { bundle, key } => print_json(verify(&bundle, &key)?),
        OsCommand::Install {
            bundle,
            disk,
            erase_confirmation,
        } => {
            require_root()?;
            let _lock = operation_lock()?;
            if fs::read_to_string("/usr/share/kane-system/role")?.trim() != "installer" {
                return Err("only the dedicated installer may erase an empty target".into());
            }
            trusted(&bundle)?;
            for name in ["manifest.json", "manifest.sig", "rootfs.tar.zst"] {
                trusted(&bundle.join(name))?;
            }
            let key = Path::new("/usr/share/kane-system/trust/release.pem");
            trusted(key)?;
            check_disk(&disk, &erase_confirmation)?;
            let manifest = verify(&bundle, key)?;
            // Recheck after the potentially long payload read, before any destructive work.
            check_disk(&disk, &erase_confirmation)?;
            // Mechanical partition/mount/GRUB work runs only after the Rust authority checks.
            // The helper is immutable in the trusted installer root, never supplied by the bundle.
            let helper = Path::new("/usr/libexec/kane-system/install-disk");
            trusted(helper)?;
            execute_logged(helper, &[text(&bundle)?, text(&disk)?])?;
            print_json(
                json!({"installed":true,"version":manifest.version,"disk":disk,"first_boot_owner_initialization_required":true,"task_replayed":false}),
            )
        }
        OsCommand::Upgrade { bundle } => {
            installed()?;
            let _lock = operation_lock()?;
            trusted(&bundle)?;
            for name in ["manifest.json", "manifest.sig", "rootfs.tar.zst"] {
                trusted(&bundle.join(name))?;
            }
            let key = Path::new("/usr/share/kane-system/trust/release.pem");
            trusted(key)?;
            let m = verify(&bundle, key)?;
            let old: Manifest = serde_json::from_slice(&fs::read(
                "/usr/share/kane-system/installed-manifest.json",
            )?)?;
            old.validate()?;
            if m.sequence <= old.sequence {
                return Err(
                    "upgrade sequence must increase; rollback is a separate explicit action".into(),
                );
            }
            compatible(&m)?;
            output("/usr/bin/systemctl", &["stop", "kane-system.target"])?;
            compatible(&m)?;
            let helper = Path::new("/usr/libexec/kane-system/update-slot");
            trusted(helper)?;
            execute_logged(helper, &["upgrade", text(&bundle)?])?;
            print_json(
                json!({"staged":true,"reboot_required":true,"sequence":m.sequence,"data_restored_from_old_snapshot":false,"task_replayed":false}),
            )
        }
        OsCommand::Rollback => {
            installed()?;
            let _lock = operation_lock()?;
            // Helper exports only the signed previous manifest path; never untrusted commands.
            let helper = Path::new("/usr/libexec/kane-system/update-slot");
            trusted(helper)?;
            let previous = output(text(helper)?, &["previous"])?;
            let previous = PathBuf::from(previous);
            trusted(&previous)?;
            let meta = signed_metadata(
                &previous,
                &previous.with_file_name("installed-manifest.sig"),
                Path::new("/usr/share/kane-system/trust/release.pem"),
            )?;
            compatible(&meta)?;
            output("/usr/bin/systemctl", &["stop", "kane-system.target"])?;
            compatible(&meta)?;
            execute_logged(helper, &["rollback"])?;
            print_json(
                json!({"rollback_selected":true,"reboot_required":true,"user_data_preserved":true,"task_replayed":false}),
            )
        }
        OsCommand::Status => print_json(
            json!({"role":fs::read_to_string("/usr/share/kane-system/role").ok(),"build":fs::read_to_string("/usr/share/kane-system/build.json").ok(),"kernel":output("/usr/bin/uname", &["-r"])?,"root":output("/usr/bin/findmnt", &["-n", "-o", "SOURCE,FSTYPE,OPTIONS", "/"])?,"phase6_closed":false,"unknown_replay":false}),
        ),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn manifest() -> Manifest {
        Manifest {
            format: 1,
            product: "Kane System".into(),
            version: "0.3.0-reference.1".into(),
            sequence: 1,
            architecture: "x86_64".into(),
            payload: "rootfs.tar.zst".into(),
            payload_sha256: "0".repeat(64),
            payload_bytes: 1,
            core_schema_min: 9,
            core_schema_max: 9,
            source_fingerprint: "a".repeat(64),
            source_date_epoch: 1,
        }
    }
    #[test]
    fn manifest_rejects_traversal_foreign_arch_and_incompatible_schema() {
        let mut m = manifest();
        assert!(m.validate().is_ok());
        m.payload = "../rootfs.tar.zst".into();
        assert!(m.validate().is_err());
        m = manifest();
        m.architecture = "arm64".into();
        assert!(m.validate().is_err());
        m = manifest();
        m.core_schema_min = 10;
        assert!(m.validate().is_err());
    }
    #[test]
    fn unknown_manifest_fields_rejected() {
        let mut v = serde_json::to_value(manifest()).unwrap();
        v["shell_command"] = json!("ignored?");
        assert!(serde_json::from_value::<Manifest>(v).is_err());
    }
    #[test]
    fn restored_selected_registry_controls_schema_check_without_migration() {
        let t = tempfile::tempdir().unwrap();
        let old = t.path().join("state.db");
        let restored = t.path().join("recovered.db");
        for (path, version) in [(&old, 9), (&restored, 10)] {
            let db = rusqlite::Connection::open(path).unwrap();
            db.pragma_update(None, "user_version", version).unwrap();
        }
        let config = t.path().join("core.env");
        fs::write(
            &config,
            format!(
                "KANE_SYSTEM_DB={}\nKANE_SYSTEM_SOCKET=/run/core.sock\n",
                restored.display()
            ),
        )
        .unwrap();
        let (selected, _) = crate::config::paths(&config).unwrap();
        let before = hash(&selected).unwrap();
        assert!(compatible_database(&manifest(), &old).is_ok());
        assert!(
            compatible_database(&manifest(), &selected)
                .unwrap_err()
                .to_string()
                .contains("schema incompatible")
        );
        assert_eq!(before, hash(&selected).unwrap());
        fs::remove_file(&selected).unwrap();
        assert!(compatible_database(&manifest(), &selected).is_err());
        assert!(!selected.exists()); // No fallback or empty database creation.
    }
    #[test]
    fn disk_erase_confirmation_is_exact_and_checked_before_device_access() {
        assert!(
            check_disk(Path::new("/does/not/exist"), "ERASE:/dev/vda")
                .unwrap_err()
                .to_string()
                .contains("confirmation")
        );
    }
    #[test]
    fn real_util_linux_detects_empty_and_existing_ext4_without_writing_it() {
        let t = tempfile::tempdir().unwrap();
        let path = t.path().join("disk.img");
        let file = File::create(&path).unwrap();
        file.set_len(16 * 1024 * 1024).unwrap();
        assert_eq!(disk_signatures(&path).unwrap(), "");
        output("/usr/sbin/mkfs.ext4", &["-q", "-F", text(&path).unwrap()]).unwrap();
        let before = hash(&path).unwrap();
        assert_eq!(disk_signatures(&path).unwrap(), "ext4");
        assert_eq!(before, hash(&path).unwrap());
    }
    #[test]
    fn actual_signature_payload_tamper_and_wrong_key() {
        let t = tempfile::tempdir().unwrap();
        let d = t.path();
        let key = d.join("private.pem");
        let public = d.join("public.pem");
        output(
            "/usr/bin/openssl",
            &[
                "genpkey",
                "-algorithm",
                "ED25519",
                "-out",
                text(&key).unwrap(),
            ],
        )
        .unwrap();
        output(
            "/usr/bin/openssl",
            &[
                "pkey",
                "-in",
                text(&key).unwrap(),
                "-pubout",
                "-out",
                text(&public).unwrap(),
            ],
        )
        .unwrap();
        let payload = d.join("rootfs.tar.zst");
        fs::write(&payload, b"payload").unwrap();
        let mut m = manifest();
        m.payload_bytes = 7;
        m.payload_sha256 = hash(&payload).unwrap();
        let meta = d.join("manifest.json");
        fs::write(&meta, serde_json::to_vec(&m).unwrap()).unwrap();
        let sig = d.join("manifest.sig");
        output(
            "/usr/bin/openssl",
            &[
                "pkeyutl",
                "-sign",
                "-inkey",
                text(&key).unwrap(),
                "-rawin",
                "-in",
                text(&meta).unwrap(),
                "-out",
                text(&sig).unwrap(),
            ],
        )
        .unwrap();
        assert!(verify(d, &public).is_ok());
        fs::write(&payload, b"PAYLOAD").unwrap();
        assert!(verify(d, &public).is_err());
        fs::write(&payload, b"payload").unwrap();
        let wrong = d.join("wrong.pem");
        output(
            "/usr/bin/openssl",
            &[
                "genpkey",
                "-algorithm",
                "ED25519",
                "-out",
                text(&wrong).unwrap(),
            ],
        )
        .unwrap();
        output(
            "/usr/bin/openssl",
            &[
                "pkey",
                "-in",
                text(&wrong).unwrap(),
                "-pubout",
                "-out",
                text(&public).unwrap(),
            ],
        )
        .unwrap();
        assert!(verify(d, &public).is_err());
    }
}
