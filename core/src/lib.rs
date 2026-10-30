//! Trusted-host library. Never load this crate or database into an Agent sandbox.
//! Actor identities are supplied by a trusted caller, not by untrusted JSON.
#![forbid(unsafe_code)]

mod actions;
mod cancellation;
mod collaboration;
mod control_auth;
mod control_workspace;
mod domain;
mod execution;
mod harness;
mod model_budget;
mod model_queue;
mod model_runtime;
mod native_file;
mod policy;
mod recovery;
mod registry;
mod resources;
mod runtime;
mod snapshot;
mod tool_execution;
mod workspace;

pub use collaboration::{CollaborationNode, CollaborationPlan, OutcomeSample};
pub use domain::*;
pub use model_runtime::LocalModelSpec;
pub use policy::*;
pub use registry::Registry;
pub use resources::{HostResourcePolicy, HostResources};

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("filesystem: {0}")]
    Io(#[from] std::io::Error),
    #[error("database: {0}")]
    Database(#[from] rusqlite::Error),
    #[error("JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid specification: {0}")]
    Invalid(String),
    #[error("not found: {0}")]
    NotFound(String),
    #[error("version/lease conflict")]
    Conflict,
    #[error("permission denied: {0}")]
    Denied(String),
    #[error("unsupported workload: {0}")]
    Unsupported(String),
    #[error("reconciliation required")]
    Reconciliation,
    #[error("budget exhausted")]
    Budget,
}

pub fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("host clock before UNIX epoch")
        .as_secs() as i64
}

pub fn new_id(prefix: &str) -> String {
    format!("{prefix}-{}", uuid::Uuid::new_v4())
}
