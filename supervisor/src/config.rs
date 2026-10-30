//! Root-managed paths are parsed as data, never sourced as executable shell.
use crate::AnyResult;
use std::{
    collections::BTreeMap,
    fs,
    path::{Component, Path, PathBuf},
};

pub fn paths(path: &Path) -> AnyResult<(PathBuf, PathBuf)> {
    let input = fs::read_to_string(path)?;
    if input.len() > 16_384 {
        return Err("system config exceeds limit".into());
    }
    let mut values = BTreeMap::new();
    for line in input
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
    {
        let (key, value) = line
            .split_once('=')
            .ok_or("config requires KEY=/absolute/path")?;
        if !["KANE_SYSTEM_DB", "KANE_SYSTEM_SOCKET"].contains(&key) {
            return Err("unknown system config key".into());
        }
        let value_path = PathBuf::from(value);
        if !value_path.is_absolute()
            || value
                .chars()
                .any(|c| c.is_control() || c == '\'' || c == '"' || c == '$' || c == '`')
            || value_path
                .components()
                .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
        {
            return Err("config paths must be literal absolute paths without shell syntax".into());
        }
        if values.insert(key, value_path).is_some() {
            return Err("duplicate system config key".into());
        }
    }
    let db = values
        .remove("KANE_SYSTEM_DB")
        .ok_or("KANE_SYSTEM_DB required")?;
    let socket = values
        .remove("KANE_SYSTEM_SOCKET")
        .ok_or("KANE_SYSTEM_SOCKET required")?;
    Ok((db, socket))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn system_config_is_data_and_cannot_override_identity() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        fs::write(temp.path(),"# paths\nKANE_SYSTEM_DB=/var/lib/kane-system/state.db\nKANE_SYSTEM_SOCKET=/run/kane-system/core.sock\n").unwrap();
        assert_eq!(
            paths(temp.path()).unwrap().0,
            Path::new("/var/lib/kane-system/state.db")
        );
        for input in [
            "KANE_SYSTEM_DB=$(touch /tmp/unsafe)\nKANE_SYSTEM_SOCKET=/run/core.sock",
            "KANE_SYSTEM_DB=/a\nKANE_SYSTEM_DB=/b\nKANE_SYSTEM_SOCKET=/run/core.sock",
            "KANE_SYSTEM_DB=relative.db\nKANE_SYSTEM_SOCKET=/run/core.sock",
            "KANE_SYSTEM_DB=/var/../etc/state.db\nKANE_SYSTEM_SOCKET=/run/core.sock",
            "ACTOR=Owner\nKANE_SYSTEM_DB=/a\nKANE_SYSTEM_SOCKET=/b",
        ] {
            fs::write(temp.path(), input).unwrap();
            assert!(paths(temp.path()).is_err());
        }
    }
}
