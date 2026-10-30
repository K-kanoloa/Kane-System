//! Quiescent, private native-data checkpoints. No live-process memory snapshots.
use crate::{AnyResult, open_db};
use kane_core::{Actor, Registry, new_id};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Root {
    pub name: String,
    pub path: PathBuf,
    pub kind: String,
    pub max_bytes: u64,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Spec {
    pub roots: Vec<Root>,
    #[serde(default)]
    pub readonly_dependencies: Vec<PathBuf>,
}
fn private_dir(path: &Path) -> AnyResult<()> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir()
        || m.file_type().is_symlink()
        || m.permissions().mode() & 0o077 != 0
        || fs::canonicalize(path)? != path
    {
        return Err("canonical private Owner directory required".into());
    }
    Ok(())
}
fn directory(path: &Path) -> AnyResult<()> {
    fs::DirBuilder::new().mode(0o700).create(path)?;
    Ok(())
}
fn safe_file(path: &Path) -> AnyResult<File> {
    let f = File::from(rustix::fs::open(
        path,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?);
    let m = f.metadata()?;
    if !m.is_file() || m.nlink() != 1 {
        return Err("regular non-hardlinked checkpoint file required".into());
    }
    Ok(f)
}
fn hash(path: &Path) -> AnyResult<String> {
    let mut f = safe_file(path)?;
    let mut h = Sha256::new();
    let mut b = [0u8; 65536];
    loop {
        let n = f.read(&mut b)?;
        if n == 0 {
            break;
        }
        h.update(&b[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}
fn json_file(path: &Path, value: &Value) -> AnyResult<()> {
    let mut f = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(&serde_json::to_vec(value)?)?;
    f.sync_all()?;
    Ok(())
}
fn json_read(path: &Path) -> AnyResult<Value> {
    let mut s = String::new();
    safe_file(path)?.take(8_000_001).read_to_string(&mut s)?;
    if s.len() > 8_000_000 {
        return Err("checkpoint metadata limit".into());
    }
    Ok(serde_json::from_str(&s)?)
}
fn inventory(root: &Path, max: u64, permitted_links: &[PathBuf]) -> AnyResult<Value> {
    fn walk(
        root: &Path,
        path: &Path,
        rows: &mut BTreeMap<String, Value>,
        used: &mut u64,
        max: u64,
        permitted_links: &[PathBuf],
    ) -> AnyResult<()> {
        let mut names = fs::read_dir(path)?
            .map(|e| e.map(|v| v.path()))
            .collect::<std::io::Result<Vec<_>>>()?;
        names.sort();
        for p in names {
            let rel = p
                .strip_prefix(root)?
                .to_str()
                .ok_or("UTF-8 checkpoint name required")?
                .to_owned();
            // ext4's reserved recovery directory is filesystem metadata, not Agent data.
            if rel == "lost+found" {
                if !fs::symlink_metadata(&p)?.is_dir() {
                    return Err("invalid ext4 metadata directory".into());
                }
                continue;
            }
            if rows.len() >= 10000 {
                return Err("checkpoint file count limit".into());
            }
            let m = fs::symlink_metadata(&p)?;
            if m.file_type().is_symlink() {
                let link = fs::read_link(&p)?;
                let raw = if link.is_absolute() {
                    link.clone()
                } else {
                    p.parent().ok_or("link parent")?.join(&link)
                };
                let mut normalized = PathBuf::new();
                for component in raw.components() {
                    match component {
                        std::path::Component::RootDir => normalized.push("/"),
                        std::path::Component::Normal(x) => normalized.push(x),
                        std::path::Component::CurDir => {}
                        std::path::Component::ParentDir => {
                            if !normalized.pop() {
                                return Err("escaping link".into());
                            }
                        }
                        _ => return Err("link path".into()),
                    }
                }
                if !normalized.starts_with(root)
                    && !permitted_links.iter().any(|p| normalized.starts_with(p))
                {
                    return Err(
                        "checkpoint symlink escapes data/declared readonly dependencies".into(),
                    );
                }
                rows.insert(rel, json!({"kind":"symlink","target":link}));
                continue;
            }
            if m.is_dir() {
                rows.insert(rel, json!({"kind":"directory"}));
                walk(root, &p, rows, used, max, permitted_links)?;
            } else if m.is_file() {
                *used = used
                    .checked_add(m.len())
                    .ok_or("checkpoint size overflow")?;
                if *used > max {
                    return Err("checkpoint root exceeds declared capacity".into());
                }
                rows.insert(rel,json!({"kind":"file","bytes":m.len(),"sha256":hash(&p)?,"mode":m.permissions().mode()&0o700}));
            } else {
                return Err("special file in checkpoint root".into());
            }
        }
        Ok(())
    }
    let mut entries = BTreeMap::new();
    let mut used = 0;
    walk(root, root, &mut entries, &mut used, max, permitted_links)?;
    Ok(json!({"entries":entries,"bytes":used,"excluded_filesystem_metadata":["lost+found"]}))
}
fn copy_tree(source: &Path, dest: &Path, files: &Value) -> AnyResult<()> {
    for (name, entry) in files["entries"].as_object().ok_or("checkpoint entries")? {
        let rel = Path::new(name);
        if rel.is_absolute()
            || rel
                .components()
                .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Err("invalid snapshot relative path".into());
        }
        let path = dest.join(rel);
        if entry["kind"] == "directory" {
            directory(&path)?;
        } else if entry["kind"] == "symlink" {
            std::os::unix::fs::symlink(entry["target"].as_str().ok_or("symlink target")?, &path)?;
        } else if entry["kind"] == "file" {
            let mut input = safe_file(&source.join(rel))?;
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)?;
            let n = std::io::copy(&mut input, &mut output)?;
            if entry["bytes"] != n || entry["sha256"] != hash(&path)? {
                return Err("checkpoint changed during copy".into());
            }
            let mode = entry["mode"].as_u64().ok_or("file mode")? as u32;
            if mode & !0o700 != 0 {
                return Err("invalid restored mode".into());
            }
            output.set_permissions(fs::Permissions::from_mode(mode))?;
            output.sync_all()?;
        } else {
            return Err("invalid checkpoint entry".into());
        }
    }
    let mut dirs = vec![dest.to_owned()];
    for (name, e) in files["entries"].as_object().unwrap() {
        if e["kind"] == "directory" {
            dirs.push(dest.join(name));
        }
    }
    dirs.sort_by_key(|p| std::cmp::Reverse(p.components().count()));
    for d in dirs {
        File::open(d)?.sync_all()?;
    }
    Ok(())
}
fn target(path: &Path) -> AnyResult<PathBuf> {
    if !path.is_absolute()
        || path.file_name().is_none()
        || path.exists()
        || fs::symlink_metadata(path).is_ok()
    {
        return Err("new absolute checkpoint destination required".into());
    }
    let parent = path.parent().ok_or("checkpoint parent")?;
    private_dir(parent)?;
    if parent.join(path.file_name().unwrap()) != path {
        return Err("canonical destination parent required".into());
    }
    Ok(parent.to_owned())
}
fn locks(db: &Path) -> AnyResult<Vec<File>> {
    let parent = db.parent().ok_or("db parent")?;
    private_dir(parent)?;
    let mut paths = vec![db.with_added_extension("service-lock")];
    for e in fs::read_dir(parent)? {
        let p = e?.path();
        let n = p.file_name().and_then(|v| v.to_str()).unwrap_or("");
        if n.ends_with(".runtime.lock") || n.ends_with(".model.lock") {
            paths.push(p);
        }
    }
    paths.sort();
    paths.dedup();
    let mut held = vec![];
    for p in paths {
        if fs::symlink_metadata(&p).is_ok_and(|m| !m.is_file() || m.file_type().is_symlink()) {
            return Err("invalid ownership lock".into());
        }
        let f = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(p)?;
        f.try_lock()
            .map_err(|_| "stop Core, runtimes and model managers before checkpoint")?;
        held.push(f);
    }
    Ok(held)
}
fn publish(staging: &Path, dest: &Path) -> AnyResult<()> {
    rustix::fs::renameat_with(
        rustix::fs::CWD,
        staging,
        rustix::fs::CWD,
        dest,
        rustix::fs::RenameFlags::NOREPLACE,
    )?;
    File::open(dest.parent().ok_or("parent")?)?.sync_all()?;
    Ok(())
}
fn validate_spec(spec: &Spec, bindings: &Value, dest: &Path) -> AnyResult<()> {
    if spec.roots.is_empty() || spec.roots.len() > 256 {
        return Err("declare bounded checkpoint roots".into());
    }
    let mut names = std::collections::BTreeSet::new();
    let mut paths = vec![];
    for root in &spec.roots {
        if root.name.is_empty()
            || root.name.len() > 100
            || !root
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || !names.insert(&root.name)
            || !matches!(root.kind.as_str(), "directory" | "sqlite")
            || root.max_bytes == 0
            || root.max_bytes > 1024 * 1024 * 1024 * 1024
        {
            return Err("invalid checkpoint root".into());
        }
        if fs::canonicalize(&root.path)? != root.path || dest.starts_with(&root.path) {
            return Err("canonical non-overlapping checkpoint roots required".into());
        }
        for other in &paths {
            if root.path.starts_with(other) || Path::new(other).starts_with(&root.path) {
                return Err("overlapping snapshot roots".into());
            }
        }
        paths.push(root.path.clone());
        let m = fs::symlink_metadata(&root.path)?;
        if m.file_type().is_symlink()
            || (root.kind == "directory" && !m.is_dir())
            || (root.kind == "sqlite" && !m.is_file())
        {
            return Err("checkpoint root type".into());
        }
    }
    for dependency in &spec.readonly_dependencies {
        if !dependency.is_absolute()
            || !bindings["agents"]
                .as_array()
                .ok_or("bindings")?
                .iter()
                .any(|a| {
                    a["config"]["sandbox"]["read_only"]
                        .as_array()
                        .is_some_and(|paths| {
                            paths.iter().any(|p| p.as_str() == dependency.to_str())
                        })
                })
        {
            return Err(
                "readonly dependency must already be declared by the actual sandbox profile".into(),
            );
        }
    }
    for a in bindings["agents"].as_array().ok_or("bindings")? {
        if a["config"].is_null() {
            continue;
        }
        for key in ["cwd", "runtime_home"] {
            let p = a["config"][key].as_str().ok_or("runtime source mapping")?;
            // A bounded, fully inventoried parent may cover several native
            // Workspace/HOME mappings. Resolve each mapping first: a symlink
            // must never turn lexical containment into an omitted data tree.
            let mapping = Path::new(p);
            if fs::canonicalize(mapping)? != mapping
                || !spec
                    .roots
                    .iter()
                    .any(|root| root.kind == "directory" && mapping.starts_with(&root.path))
            {
                return Err("every configured Workspace and HOME must be included".into());
            }
        }
    }
    Ok(())
}
pub fn create(db: &Path, spec_path: &Path, dest: &Path) -> AnyResult<Value> {
    let parent = target(dest)?;
    let _locks = locks(db)?;
    let mut registry = open_db(db)?;
    let spec: Spec = serde_json::from_value(json_read(spec_path)?)?;
    let bindings = registry.checkpoint_bindings(&Actor::Owner)?;
    validate_spec(&spec, &bindings, dest)?;
    let id = new_id("checkpoint");
    let staging = parent.join(format!(".pending-{id}"));
    directory(&staging)?;
    directory(&staging.join("roots"))?;
    // Failed/incomplete directories are deliberately not treated as published checkpoints.
    let mut roots = vec![];
    for root in &spec.roots {
        let path = staging.join("roots").join(&root.name);
        let files = if root.kind == "directory" {
            directory(&path)?;
            let files = inventory(&root.path, root.max_bytes, &spec.readonly_dependencies)?;
            copy_tree(&root.path, &path, &files)?;
            if inventory(&root.path, root.max_bytes, &spec.readonly_dependencies)? != files {
                return Err("native source changed during checkpoint".into());
            }
            files
        } else {
            let source = rusqlite::Connection::open_with_flags(
                &root.path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )?;
            let integrity: String = source.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
            if integrity != "ok" {
                return Err("source SQLite integrity".into());
            }
            let file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)?;
            source.execute("VACUUM INTO ?1", [path.to_str().ok_or("UTF-8 db path")?])?;
            file.sync_all()?;
            if file.metadata()?.len() > root.max_bytes {
                return Err("SQLite root exceeds declared capacity".into());
            }
            json!({"bytes":file.metadata()?.len(),"sha256":hash(&path)?})
        };
        roots.push(json!({"name":root.name,"source":root.path,"kind":root.kind,"max_bytes":root.max_bytes,"files":files}));
    }
    let manifest = json!({"format":1,"id":id,"bindings":bindings,"roots":roots,"readonly_dependencies":spec.readonly_dependencies,"scope":"quiescent native data; no process memory; ext4 lost+found excluded"});
    registry.record_checkpoint(&Actor::Owner, &manifest)?;
    registry.backup(&Actor::Owner, staging.join("registry.db"))?;
    json_file(&staging.join("manifest.json"), &manifest)?;
    json_file(
        &staging.join("seal.json"),
        &json!({"checkpoint_id":id,"manifest_sha256":hash(&staging.join("manifest.json"))?,"db_sha256":hash(&staging.join("registry.db"))?}),
    )?;
    File::open(staging.join("roots"))?.sync_all()?;
    File::open(&staging)?.sync_all()?;
    publish(&staging, dest)?;
    Ok(
        json!({"id":id,"checkpoint":dest,"manifest":manifest,"publication":"durable-directory-rename","automatic_replay":false}),
    )
}
fn link_policy(manifest: &Value, root: &Value) -> AnyResult<Vec<PathBuf>> {
    let mut allowed: Vec<PathBuf> = serde_json::from_value(
        manifest
            .get("readonly_dependencies")
            .cloned()
            .unwrap_or(json!([])),
    )?;
    allowed.push(PathBuf::from(root["source"].as_str().ok_or("source")?));
    Ok(allowed)
}
pub fn validate(source: &Path) -> AnyResult<Value> {
    private_dir(source)?;
    let seal = json_read(&source.join("seal.json"))?;
    let manifest = json_read(&source.join("manifest.json"))?;
    if manifest["format"] != 1
        || seal["checkpoint_id"] != manifest["id"]
        || seal["manifest_sha256"] != hash(&source.join("manifest.json"))?
        || seal["db_sha256"] != hash(&source.join("registry.db"))?
    {
        return Err("checkpoint seal/hash mismatch".into());
    }
    let spec: Spec = serde_json::from_value(
        json!({"roots":manifest["roots"].as_array().ok_or("roots")?.iter().map(|v|json!({"name":v["name"],"path":source.join("roots").join(v["name"].as_str().unwrap_or("")),"kind":v["kind"],"max_bytes":v["max_bytes"]})).collect::<Vec<_>>()}),
    )?;
    // Validate names/types before joining any untrusted manifest paths for copying.
    for (root, entry) in spec.roots.iter().zip(manifest["roots"].as_array().unwrap()) {
        if root.name.is_empty()
            || !root
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || root.max_bytes == 0
        {
            return Err("invalid checkpoint root manifest".into());
        }
        if root.kind == "directory" {
            if fs::canonicalize(&root.path)? != root.path
                || inventory(&root.path, root.max_bytes, &link_policy(&manifest, entry)?)?
                    != entry["files"]
            {
                return Err("checkpoint native data corrupted".into());
            }
        } else if root.kind == "sqlite" {
            if entry["files"]["sha256"] != hash(&root.path)? {
                return Err("checkpoint SQLite data corrupted".into());
            }
        } else {
            return Err("invalid root kind".into());
        }
    }
    // SQLite integrity is read-only; no schema migration or mutation of the original snapshot.
    let db = rusqlite::Connection::open_with_flags(
        source.join("registry.db"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let check: String = db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    if check != "ok" {
        return Err("checkpoint DB integrity".into());
    }
    let raw: String = db.query_row(
        "SELECT payload_json FROM ledger WHERE object_id=?1 AND kind='checkpoint'",
        [manifest["id"].as_str().ok_or("id")?],
        |r| r.get(0),
    )?;
    if serde_json::from_str::<Value>(&raw)?["manifest_sha256"]
        != format!("{:x}", Sha256::digest(serde_json::to_vec(&manifest)?))
    {
        return Err("checkpoint ledger/manifest mismatch".into());
    }
    Ok(manifest)
}
pub fn restore(source: &Path, dest: &Path) -> AnyResult<Value> {
    let parent = target(dest)?;
    let manifest = validate(source)?;
    let staging = parent.join(format!(".restore-{}", new_id("pending")));
    directory(&staging)?;
    directory(&staging.join("roots"))?;
    for root in manifest["roots"].as_array().ok_or("roots")? {
        let name = root["name"].as_str().ok_or("name")?;
        let from = source.join("roots").join(name);
        let to = staging.join("roots").join(name);
        if root["kind"] == "directory" {
            directory(&to)?;
            copy_tree(&from, &to, &root["files"])?;
        } else {
            let mut input = safe_file(&from)?;
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&to)?;
            std::io::copy(&mut input, &mut output)?;
            output.sync_all()?;
        }
    }
    Registry::restore_new(
        &Actor::Owner,
        source.join("registry.db"),
        staging.join("registry.db"),
    )?;
    let mut registry = open_db(&staging.join("registry.db"))?;
    registry.quarantine_restore(&Actor::Owner, &manifest)?;
    drop(registry);
    File::open(staging.join("registry.db"))?.sync_all()?;
    json_file(
        &staging.join("restore.json"),
        &json!({"checkpoint_id":manifest["id"],"manifest":manifest,"task_state":"Blocked until explicit Owner review","activation":"native roots must be verified/reinstalled under original stopped-runtime mappings; no automatic runtime launch"}),
    )?;
    File::open(staging.join("roots"))?.sync_all()?;
    File::open(&staging)?.sync_all()?;
    publish(&staging, dest)?;
    Ok(
        json!({"restored":dest,"checkpoint_id":manifest["id"],"db":dest.join("registry.db"),"runtime_roots":dest.join("roots"),"automatic_replay":false,"tasks_quarantined":true}),
    )
}

/// Explicit Owner activation of one checked root under its ORIGINAL mapping.
/// Existing data must be archived separately. No overwrite or automatic launch.
pub fn install(db: &Path, source: &Path, name: &str, unavailable: bool) -> AnyResult<Value> {
    let _locks = locks(db)?;
    let manifest = validate(source)?;
    if !unavailable {
        let registry = open_db(db)?;
        registry.checkpoint_bindings(&Actor::Owner)?;
    } else if db.exists() {
        // Inspect read-only: a permission error or newer reader version is not
        // evidence of corruption, and disaster recovery must preserve the old bytes.
        let metadata = fs::symlink_metadata(db)?;
        if !metadata.is_file()
            || metadata.file_type().is_symlink()
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err("private damaged Registry required".into());
        }
        let old =
            rusqlite::Connection::open_with_flags(db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let checked = (|| -> rusqlite::Result<(i64, String)> {
            let version = old.pragma_query_value(None, "user_version", |r| r.get(0))?;
            let integrity = old.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
            Ok((version, integrity))
        })();
        match checked {
            Ok((version, _)) if version > 8 => {
                return Err(
                    "newer Registry schema is not missing/corrupt; rollback refused".into(),
                );
            }
            Ok((_, integrity)) if integrity == "ok" => {
                return Err("Registry is readable; use ordinary explicit maintenance".into());
            }
            Ok(_) => {}
            Err(rusqlite::Error::SqliteFailure(error, _))
                if matches!(
                    error.code,
                    rusqlite::ErrorCode::DatabaseCorrupt | rusqlite::ErrorCode::NotADatabase
                ) => {}
            Err(error) => return Err(error.into()),
        }
    }
    let root = manifest["roots"]
        .as_array()
        .ok_or("roots")?
        .iter()
        .find(|r| r["name"] == name)
        .ok_or("unknown root")?;
    let to = Path::new(root["source"].as_str().ok_or("source mapping")?);
    let from = source.join("roots").join(name);
    if root["kind"] == "directory" {
        private_dir(to)?;
        if fs::read_dir(to)?.any(|e| e.map_or(true, |e| e.file_name() != "lost+found")) {
            return Err(
                "archive old data before explicit checkpoint install; destination must be empty"
                    .into(),
            );
        }
        copy_tree(&from, to, &root["files"])?;
        if inventory(
            to,
            root["max_bytes"].as_u64().ok_or("capacity")?,
            &link_policy(&manifest, root)?,
        )? != root["files"]
        {
            return Err("installed native bytes mismatch".into());
        }
    } else {
        target(to)?;
        let mut input = safe_file(&from)?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(to)?;
        std::io::copy(&mut input, &mut output)?;
        output.sync_all()?;
        File::open(to.parent().ok_or("parent")?)?.sync_all()?;
        if hash(to)? != root["files"]["sha256"] {
            return Err("installed SQLite mismatch".into());
        }
    }
    Ok(
        json!({"checkpoint_id":manifest["id"],"root":name,"destination":to,"verified":true,"latest_registry_unavailable_mode":unavailable,"automatic_launch":false}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_parent_covers_and_inventories_native_mappings() {
        let temp = tempfile::tempdir().unwrap();
        let parent = temp.path().join("runtime");
        let cwd = parent.join("agent/workspace");
        let home = parent.join("agent/home");
        fs::create_dir_all(&cwd).unwrap();
        fs::create_dir_all(&home).unwrap();
        fs::write(cwd.join("document.txt"), b"editable").unwrap();
        fs::write(home.join("session.json"), b"session").unwrap();
        let bindings = json!({"agents":[{"config":{"cwd":cwd,"runtime_home":home}}]});
        let spec = Spec {
            roots: vec![Root {
                name: "runtime".into(),
                path: parent.clone(),
                kind: "directory".into(),
                max_bytes: 64,
            }],
            readonly_dependencies: vec![],
        };
        validate_spec(&spec, &bindings, &temp.path().join("checkpoint")).unwrap();
        let files = inventory(&parent, 64, &[]).unwrap();
        assert_eq!(files["bytes"], 15);
        assert!(files["entries"]["agent/workspace/document.txt"].is_object());
        assert!(files["entries"]["agent/home/session.json"].is_object());
        assert!(inventory(&parent, 14, &[]).is_err());
    }

    #[test]
    fn parent_coverage_rejects_prefix_sibling_and_symlink_mappings() {
        let temp = tempfile::tempdir().unwrap();
        let parent = temp.path().join("runtime");
        let sibling = temp.path().join("runtime-other");
        fs::create_dir(&parent).unwrap();
        fs::create_dir(&sibling).unwrap();
        let spec = Spec {
            roots: vec![Root {
                name: "runtime".into(),
                path: parent.clone(),
                kind: "directory".into(),
                max_bytes: 64,
            }],
            readonly_dependencies: vec![],
        };
        let b = json!({"agents":[{"config":{"cwd":sibling,"runtime_home":parent}}]});
        assert!(validate_spec(&spec, &b, &temp.path().join("checkpoint")).is_err());
        let link = parent.join("linked-home");
        std::os::unix::fs::symlink(&sibling, &link).unwrap();
        let b = json!({"agents":[{"config":{"cwd":parent,"runtime_home":link}}]});
        assert!(validate_spec(&spec, &b, &temp.path().join("checkpoint")).is_err());
        assert!(inventory(&parent, 64, &[]).is_err());
    }
}
