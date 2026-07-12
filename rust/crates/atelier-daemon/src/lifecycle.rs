//! Project lifecycle states (wired in Phase 3).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub enum ProjectLifecycle {
    #[default]
    RegisteredIdle,
    Starting,
    Active,
    IdleGrace,
    Suspended,
    Faulted,
    Removed,
}
