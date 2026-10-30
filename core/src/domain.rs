use crate::{Error, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum WorkloadClass {
    #[serde(rename = "inference")]
    Inference,
    #[serde(rename = "generation")]
    Generation,
    #[serde(rename = "execution")]
    Execution,
    #[serde(rename = "training")]
    Training,
    #[serde(rename = "realtime")]
    Realtime,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workload {
    pub class: WorkloadClass,
    #[serde(default)]
    pub interruptible: bool,
    #[serde(default)]
    pub resumable: Resume,
    #[serde(default)]
    pub realtime: bool,
    pub max_memory_bytes: u64,
    pub max_storage_bytes: u64,
    #[serde(default)]
    pub checkpoint_verified: bool,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Resume {
    #[default]
    None,
    Restart,
    Checkpoint,
}

impl Workload {
    pub fn validate(&self) -> Result<()> {
        if matches!(
            self.class,
            WorkloadClass::Training | WorkloadClass::Realtime
        ) || self.realtime
        {
            return Err(Error::Unsupported(format!(
                "{:?}; no training/realtime guarantees",
                self.class
            )));
        }
        if self.max_memory_bytes == 0 || self.max_storage_bytes == 0 {
            return Err(Error::Invalid(
                "finite nonzero memory/storage limits required".into(),
            ));
        }
        if self.resumable == Resume::Checkpoint && !self.checkpoint_verified {
            return Err(Error::Invalid(
                "checkpoint capability is not verified".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TaskState {
    Pending,
    Ready,
    Running,
    Blocked,
    Verifying,
    Done,
    Failed,
    Cancelled,
    ClosedUnverified,
}

impl TaskState {
    pub fn terminal(self) -> bool {
        matches!(
            self,
            Self::Done | Self::Failed | Self::Cancelled | Self::ClosedUnverified
        )
    }

    pub fn permits(self, next: Self) -> bool {
        use TaskState::*;
        matches!(
            (self, next),
            (Pending, Ready)
                | (Ready, Running)
                | (Running, Verifying | Blocked)
                | (Blocked, Running | Verifying | ClosedUnverified)
                | (Verifying, Done | Running | Blocked)
        ) || (!self.terminal() && matches!(next, Failed | Cancelled))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AgentState {
    Created,
    Ready,
    Running,
    Blocked,
    Suspended,
    Cancelling,
    Terminated,
    Exited,
    Failed,
}

impl AgentState {
    pub fn permits(self, next: Self) -> bool {
        use AgentState::*;
        matches!(
            (self, next),
            (Created, Ready)
                | (Ready, Running | Failed)
                | (
                    Running,
                    Blocked | Suspended | Cancelling | Exited | Ready | Failed
                )
                | (Blocked, Running | Suspended | Cancelling | Ready | Failed)
                | (Suspended, Ready | Cancelling)
                | (Cancelling, Terminated)
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerificationState {
    Pending,
    Passed,
    Failed,
    Unverified,
}

/// This is a trusted host identity, never accepted from an Agent-controlled body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Actor {
    Owner,
    Core,
    Verifier(String),
    Agent(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskSpec {
    pub goal: String,
    pub acceptance: Vec<String>,
    pub workload: Workload,
    pub budget_id: String,
    pub workspace_id: String,
    #[serde(default)]
    pub cloud_allowed: bool,
}

impl TaskSpec {
    pub fn validate(&self) -> Result<()> {
        self.workload.validate()?;
        if self.goal.trim().is_empty()
            || self.acceptance.is_empty()
            || self.acceptance.iter().any(|s| s.trim().is_empty())
        {
            return Err(Error::Invalid(
                "goal and nonempty acceptance criteria required".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub state: TaskState,
    pub verification_state: VerificationState,
    pub block_reason: Option<String>,
    pub version: u64,
    pub spec: TaskSpec,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Agent {
    pub id: String,
    pub role: String,
    pub task_id: String,
    pub workspace_id: String,
    pub budget_id: String,
    pub model_id: String,
    pub state: AgentState,
    pub version: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Artifact {
    pub id: String,
    pub task_id: String,
    pub producer_id: String,
    pub format: String,
    pub editable: bool,
    pub content: String,
    pub content_hash: String,
    pub verification_state: VerificationState,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Workspace {
    pub id: String,
    pub kind: String,
    pub version: u64,
    pub lease_epoch: u64,
    pub holder: Option<String>,
    pub expires_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub seq: i64,
    pub ts: i64,
    pub kind: String,
    pub object_id: String,
    pub payload: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutboxAction {
    pub id: String,
    pub task_id: String,
    pub idempotency_key: String,
    pub state: String,
    pub payload: serde_json::Value,
}
