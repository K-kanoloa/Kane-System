//! Bounded Linux runtime adapter. No shell, inherited secrets or database mounts.
//! Used for lifecycle fixtures; not evidence of native Agent checkpoint recovery.
use serde_json::Value;
use std::{
    error::Error,
    process::{Command, Stdio},
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

pub struct ContainerRuntime {
    pub executable: String,
}
impl ContainerRuntime {
    fn invoke(&self, args: &[&str]) -> Result<String> {
        if !matches!(self.executable.as_str(), "docker" | "podman") {
            return Err("runtime must be docker or podman".into());
        }
        let output = Command::new(&self.executable)
            .args(args)
            .stdin(Stdio::null())
            .output()?;
        if !output.status.success() {
            return Err(format!(
                "runtime command failed: {}",
                String::from_utf8_lossy(&output.stderr)
                    .chars()
                    .take(1500)
                    .collect::<String>()
            )
            .into());
        }
        Ok(String::from_utf8(output.stdout)?.trim().into())
    }

    pub fn create_fixture(&self, agent_id: &str, image: &str) -> Result<String> {
        if image.is_empty() || image.starts_with('-') || image.chars().any(char::is_whitespace) {
            return Err("invalid image reference".into());
        }
        let label = format!("kane.system.agent={agent_id}");
        let cid = self.invoke(&[
            "create",
            "--pull=never",
            "-i",
            "--label",
            &label,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=1000:1000",
            "--pids-limit=64",
            "--memory=256m",
            "--memory-swap=256m",
            "--cpus=1",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=64m,mode=1777",
            image,
        ])?;
        if let Err(error) = self.invoke(&["start", &cid]) {
            let _ = self.invoke(&["rm", "--force", &cid]);
            return Err(error);
        }
        Ok(cid)
    }

    pub fn inspect(&self, cid: &str, agent_id: &str) -> Result<Value> {
        if cid.is_empty() || !cid.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err("invalid container id".into());
        }
        let value: Value = serde_json::from_str(&self.invoke(&["inspect", cid])?)?;
        let c = value
            .as_array()
            .and_then(|v| v.first())
            .ok_or("missing container")?;
        if c["Config"]["Labels"]["kane.system.agent"] != agent_id {
            return Err("container ownership mismatch".into());
        }
        Ok(c.clone())
    }

    pub fn kill_fixture(&self, cid: &str, agent_id: &str) -> Result<()> {
        self.inspect(cid, agent_id)?;
        self.invoke(&["kill", cid])?;
        Ok(())
    }

    pub fn remove_fixture(&self, cid: &str, agent_id: &str) -> Result<()> {
        self.inspect(cid, agent_id)?;
        self.invoke(&["rm", "--force", cid])?;
        Ok(())
    }
}
