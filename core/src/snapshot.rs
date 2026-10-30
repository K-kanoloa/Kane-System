//! Consistent backups of the database and its managed file contents only.
//! Native filesystem workspaces are not covered by this snapshot.
use crate::{registry::*, *};
use serde_json::json;
use std::path::Path;

impl Registry {
    pub fn backup(&mut self, actor: &Actor, destination: impl AsRef<Path>) -> Result<()> {
        owner(actor)?;
        let destination = destination.as_ref();
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(destination)?;
        let tx = self.db.transaction()?;
        event(
            &tx,
            "checkpoint",
            "registry",
            json!({"id":new_id("checkpoint"),"scope":"database and database-managed files only"}),
        )?;
        tx.commit()?;
        // VACUUM INTO makes a transactionally consistent snapshot even in WAL mode.
        let result = self
            .db
            .execute("VACUUM INTO ?1", [destination.to_string_lossy().as_ref()]);
        if let Err(error) = result {
            drop(file);
            std::fs::remove_file(destination)?;
            return Err(error.into());
        }
        file.sync_all()?;
        if let Some(parent) = destination.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::File::open(parent)?.sync_all()?;
        }
        Ok(())
    }

    /// Restore to a new pathname; never overwrite an existing database or WAL.
    /// The caller must stop its services before selecting this restored database.
    pub fn restore_new(
        actor: &Actor,
        source: impl AsRef<Path>,
        destination: impl AsRef<Path>,
    ) -> Result<()> {
        owner(actor)?;
        let source = source.as_ref();
        let destination = destination.as_ref();
        let source_db = rusqlite::Connection::open_with_flags(
            source,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let version: i64 = source_db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if !matches!(version, 1..=9) {
            return Err(Error::Invalid("unsupported snapshot schema".into()));
        }
        let integrity: String = source_db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
        let fk: i64 =
            source_db.query_row("SELECT count(*) FROM pragma_foreign_key_check", [], |r| {
                r.get(0)
            })?;
        if integrity != "ok" || fk != 0 {
            return Err(Error::Invalid("snapshot integrity check failed".into()));
        }
        // Read through SQLite rather than copying a possibly live WAL database file.
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(destination)?;
        let result = source_db.execute("VACUUM INTO ?1", [destination.to_string_lossy().as_ref()]);
        if let Err(error) = result {
            drop(file);
            std::fs::remove_file(destination)?;
            return Err(error.into());
        }
        file.sync_all()?;
        if let Some(parent) = destination.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::File::open(parent)?.sync_all()?;
        }
        Ok(())
    }
}
