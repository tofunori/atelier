//! Project lifecycle states (wired in Phase 3).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum ProjectLifecycle {
    RegisteredIdle,
    Starting,
    Active,
    IdleGrace,
    Suspended,
    Faulted,
    Removed,
}

impl Default for ProjectLifecycle {
    fn default() -> Self {
        Self::RegisteredIdle
    }
}
